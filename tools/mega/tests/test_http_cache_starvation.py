#!/usr/bin/env python3
"""Runnable RED regression of production CFileCache::Read / WaitForData.

Run manually: python3 tools/mega/tests/test_http_cache_starvation.py
Requires only Python and a native C++17 compiler. Production function bodies are
compiled verbatim; fake locks, clock, event delivery and cache producer isolate
starvation without real 10/60-second waits. This intentionally fails until HTTP
patience and a safe cancellation path are implemented. This does NOT exercise
CFileCache::Process source-error conversion, curl, input Abort, or device Stop.
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
static bool eof=false, cacheError=false, cancelled=false;
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
 bool IsEndOfInput() { return eof; }
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
 ssize_t Read(void* lpBuf, size_t uiBufSize);
};
'''
MAIN = r'''
int main(int argc, char** argv) {
 std::string test=argv[1];
 CFileCache file;
 produced=&file.m_pCache->m_end;
 if(test=="true_eof") eof=true;
 if(test=="cache_error") cacheError=true;
 if(test=="expiry") readyAt=1000000ms;
 if(test=="cancel_before") cancelled=true;
 if(test=="cancel_during") cancelAt=100ms;
 char b[16]{};
 auto result=file.Read(b,sizeof(b));
 std::cout<<"result="<<result<<" waited_ms="<<now.count()<<" eof="<<eof
          <<" cancelled="<<cancelled<<"\n";
 if(test=="starvation") return result==7 ? 0:1;
 if(test=="true_eof") return result==0 && now==0ms ? 0:1;
 if(test=="cache_error") return result<0 && now==0ms ? 0:1;
 // Contract: HTTP configured patience of 60 seconds, never false EOF.
 if(test=="expiry") return result<0 && now>=60000ms && now<=60100ms ? 0:1;
 // Cancellation state is intentionally not yet wired into production.
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
        cpp.write_text(HARNESS + circular + '\n' + read + MAIN)
        subprocess.run([os.environ.get("CXX", "c++"), "-std=c++17", "-Wall", "-Wextra", "-Wno-unused-parameter", str(cpp), "-o", str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def check(self, case):
        run = subprocess.run([str(self.binary), case], text=True, capture_output=True, timeout=3)
        self.assertEqual(run.returncode, 0, f"{case}: {run.stdout} {run.stderr}")

    def test_starvation_is_not_eof(self): self.check("starvation")
    def test_true_eof(self): self.check("true_eof")
    def test_explicit_cache_error(self): self.check("cache_error")
    def test_http_patience_expiry_is_error(self): self.check("expiry")
    def test_cancel_before_read(self): self.check("cancel_before")
    def test_cancel_during_wait(self): self.check("cancel_during")


if __name__ == '__main__':
    unittest.main(verbosity=2)
