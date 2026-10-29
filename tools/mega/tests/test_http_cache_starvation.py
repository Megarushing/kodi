#!/usr/bin/env python3
"""Compiled regression of production CFileCache::Read / WaitForData.

Run manually: python3 tools/mega/tests/test_http_cache_starvation.py
Requires only Python and a native C++17 compiler. Production function bodies are
compiled verbatim; fake locks, clock, event delivery and cache producer isolate
starvation without real 10/60-second waits. Production Open reset/policy and
Process terminal-error publication snippets are also exercised. Curl cancellation
is tested separately; device Stop and real HTTP remain integration tests.
"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


def function(path, signature):
    text = (ROOT / path).read_text()
    start = text.index(signature)
    end = text.index('{', start) + 1
    depth = 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


HARNESS = r'''
#include <algorithm>
#include <atomic>
#include <chrono>
#include <climits>
#include <cstdint>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <sys/types.h>
using namespace std::chrono_literals;
using CCriticalSection = std::recursive_mutex;
constexpr int CACHE_RC_WOULD_BLOCK=-2, CACHE_RC_TIMEOUT=-3;
constexpr int LOGERROR=1, LOGWARNING=2;
struct CLog { template<class... T> static void Log(T...) {} };
static std::chrono::milliseconds now{0}, readyAt{40000}, cancelAt{1000000};
static bool eof=false, cacheError=false, finalDataRace=false;
static std::atomic<bool> cancelled{false};
static int64_t* produced=nullptr;
struct Event {
 void Wait(std::chrono::milliseconds t) {
  now+=t;
  if(now>=readyAt && produced) *produced=7;
  if(now>=cancelAt) cancelled=true;
 }
};
namespace XbmcThreads {
template<class T=void> struct EndTime {
 std::chrono::milliseconds end;
 explicit EndTime(std::chrono::milliseconds t):end(now+t){}
 bool IsTimePast() { return now>=end; }
};
}
class CCircularCache {
public:
 CCriticalSection m_sync;
 int64_t m_end=0, m_cur=0;
 uint32_t m_size=1024, m_size_back=0;
 Event m_written;
 bool IsEndOfInput() {
  if(finalDataRace) { finalDataRace=false; m_end=7; eof=true; }
  return eof;
 }
 int64_t WaitForData(uint32_t minimum, std::chrono::milliseconds timeout);
 int ReadFromCache(char*, size_t) {
  if(cacheError) return -1;
  if(m_end>m_cur) return 7;
  return eof ? 0 : CACHE_RC_WOULD_BLOCK;
 }
};
class CFileCache {
public:
 CCriticalSection m_sync;
 std::unique_ptr<CCircularCache> m_pCache{new CCircularCache};
 std::string m_sourcePath="http://fixture.invalid/test.mkv";
 int64_t m_readPos=0;
 std::atomic<bool>& m_readAborted=cancelled;
 std::atomic<bool> m_sourceReadError{false};
 std::chrono::milliseconds m_httpReadPatience{60000};
 void Init(bool http, int patience);
 void PublishTerminal(ssize_t iRead, int64_t m_fileSize, int64_t m_writePos);
 ssize_t Read(void* lpBuf, size_t uiBufSize);
};
'''
MAIN = r'''
int main(int argc, char** argv) {
 std::string test=argv[1];
 CFileCache file;
 produced=&file.m_pCache->m_end;
 if(test=="true_eof") eof=true;
 if(test=="final_data_race") finalDataRace=true;
 if(test=="source_error") file.PublishTerminal(-1, 0, 0);
 if(test=="premature_zero") file.PublishTerminal(0, 100, 7);
 if(test=="unknown_eof") file.PublishTerminal(0, 0, 7);
 if(test=="known_eof") file.PublishTerminal(0, 7, 7);
 if(test=="reopen") {
   cancelled=true; file.m_sourceReadError=true;
   file.Init(true, 600);
   if(cancelled || file.m_sourceReadError || file.m_httpReadPatience!=600s) return 1;
 }
 if(test=="floor") { file.Init(true, 1); readyAt=1000000ms; }
 if(test=="non_http_reset") {
   file.Init(false, 600);
   if(file.m_httpReadPatience!=0ms) return 1;
 }
 if(test=="non_http") file.m_httpReadPatience=0ms;
 if(test=="data_at_eof") { eof=true; file.m_pCache->m_end=7; }
 if(test=="starvation_600") file.m_httpReadPatience=600s;
 if(test=="cache_error") cacheError=true;
 if(test=="expiry") readyAt=1000000ms;
 if(test=="cancel_before") cancelled=true;
 if(test=="cancel_during") cancelAt=100ms;
 char b[16]{};
 auto result=file.Read(b,test=="zero_read" ? 0 : sizeof(b));
 if(test=="zero_read") return result==0 && now==0ms ? 0:1;
 std::cout<<"result="<<result<<" waited_ms="<<now.count()<<" eof="<<eof
          <<" cancelled="<<cancelled<<"\n";
 if(test=="starvation" || test=="starvation_600" || test=="data_at_eof") return result==7 ? 0:1;
 if(test=="source_error" || test=="premature_zero") return result<0 && now==0ms ? 0:1;
 if(test=="known_eof" || test=="unknown_eof") return result==0 && now==0ms ? 0:1;
 if(test=="reopen" || test=="final_data_race") return result==7 ? 0:1;
 if(test=="floor") return result<0 && now==20000ms ? 0:1;
 if(test=="non_http_reset") return result==0 && now==10000ms ? 0:1;
 if(test=="non_http") return result==0 && now==10000ms ? 0:1;
 if(test=="true_eof") return result==0 && now==0ms ? 0:1;
 if(test=="cache_error") return result<0 && now==0ms ? 0:1;
 // Contract: HTTP configured patience of 60 seconds, never false EOF.
 if(test=="expiry") return result<0 && now>=60000ms && now<=60100ms ? 0:1;
 // Cancellation must be observed without waiting for the configured HTTP budget.
 if(test=="cancel_before") return result<0 && now<=100ms ? 0:1;
 if(test=="cancel_during") return result<0 && now<=200ms ? 0:1;
 return 2;
}
'''


class HttpCacheStarvation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="kodi-cache-regression-")
        cls.binary = Path(cls.tmp.name) / "cache-read"
        circular = function("xbmc/filesystem/CircularCache.cpp", "int64_t CCircularCache::WaitForData(")
        read = function("xbmc/filesystem/FileCache.cpp", "ssize_t CFileCache::Read(")
        cpp = Path(cls.tmp.name) / "cache-read.cpp"
        opening = function("xbmc/filesystem/FileCache.cpp", "bool CFileCache::Open(")
        reset = opening[opening.index("  m_readAborted = false;"):opening.index("  m_sourcePath =")]
        # Compile the exact reset/policy statements, with only URL/settings providers stubbed.
        init = """
struct CURL { bool http; bool IsProtocol(const char* p) const {
 return http && std::string(p)=="http"; } };
struct Advanced { int m_curllowspeedtime; };
struct Component { Advanced settings; Advanced* GetAdvancedSettings() { return &settings; } };
struct CServiceBroker { static Component* GetSettingsComponent() { static Component c; return &c; } };
void CFileCache::Init(bool http, int patience) {
 CURL url{http}; CServiceBroker::GetSettingsComponent()->settings.m_curllowspeedtime=patience;
""" + reset + "}\n"
        process = function("xbmc/filesystem/FileCache.cpp", "void CFileCache::Process(")
        start = process.index("        m_sourceReadError = iRead")
        terminal = process[start:process.index("m_pCache->EndOfInput();", start)]
        publish = "void CFileCache::PublishTerminal(ssize_t iRead, int64_t m_fileSize, int64_t m_writePos) {" + terminal + "eof=true;}\n"
        cpp.write_text(HARNESS + init + publish + circular + '\n' + read + MAIN)
        subprocess.run([os.environ.get("CXX", "c++"), "-std=c++17", "-Wall", "-Wextra", "-Wno-unused-parameter", str(cpp), "-o", str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def check(self, case):
        run = subprocess.run([str(self.binary), case], text=True, capture_output=True, timeout=3)
        self.assertEqual(run.returncode, 0, f"{case}: {run.stdout} {run.stderr}")

    def test_zero_size_read_is_immediate(self): self.check("zero_read")
    def test_starvation_is_not_eof(self): self.check("starvation")
    def test_true_eof(self): self.check("true_eof")
    def test_source_error_is_not_eof(self): self.check("source_error")
    def test_premature_known_length_zero(self): self.check("premature_zero")
    def test_unknown_length_eof(self): self.check("unknown_eof")
    def test_known_length_eof(self): self.check("known_eof")
    def test_reopen_resets_abort_error_and_patience(self): self.check("reopen")
    def test_http_policy_floor(self): self.check("floor")
    def test_reopen_non_http_clears_policy(self): self.check("non_http_reset")
    def test_non_http_preserves_ten_seconds(self): self.check("non_http")
    def test_cached_data_precedes_eof(self): self.check("data_at_eof")
    def test_final_write_before_eoi_observation(self): self.check("final_data_race")
    def test_40_second_stall_with_600_second_patience(self): self.check("starvation_600")
    def test_explicit_cache_error(self): self.check("cache_error")
    def test_http_patience_expiry_is_error(self): self.check("expiry")
    def test_cancel_before_read(self): self.check("cancel_before")
    def test_cancel_during_wait(self): self.check("cancel_during")


if __name__ == '__main__':
    unittest.main(verbosity=2)
