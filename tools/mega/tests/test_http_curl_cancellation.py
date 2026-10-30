#!/usr/bin/env python3
"""Compile actual curl cancellation/poll bodies; real threads and POSIX select.
No live network, libcurl transfer, Kodi build, devices, or TSAN claim.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_http_cache_starvation import ROOT, function

HARNESS = r"""
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstring>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <thread>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>
#include <curl/curl.h>
using namespace std::chrono_literals;
constexpr int FILLBUFFER_OK=1, FILLBUFFER_NO_DATA=0, FILLBUFFER_FAIL=-1;
constexpr int LOGERROR=1, LOGWARNING=2, LOGDEBUG=3, SOCKET_ERROR=-1;
enum class IOControl { CANCEL_READ, SEEK_POSSIBLE, SET_RETRY, CACHE_STATUS, CACHE_SETRATE }; // enum class since Piers
struct CLog { template<class... T> static void Log(T...) {} };
namespace fmt { template<class T> auto ptr(T* p) { return p; } }
namespace XbmcThreads { template<class T=void> struct EndTime {
 std::chrono::steady_clock::time_point end;
 explicit EndTime(std::chrono::milliseconds t):end(std::chrono::steady_clock::now()+t){}
 auto GetTimeLeft() { return std::chrono::duration_cast<std::chrono::milliseconds>(end-std::chrono::steady_clock::now()); }
}; }
struct Advanced { int m_curlretries=0; };
struct Component { Advanced a; auto GetAdvancedSettings(){ return &a; } };
struct CServiceBroker { static auto GetSettingsComponent(){static Component c;return &c;} };
static std::atomic<int> polls{0};
static int socketFd=-1;
static std::atomic<bool> inSelect{false};
int observed_select(int n, fd_set* r, fd_set* w, fd_set* e, timeval* t) {
 inSelect=true; return ::select(n,r,w,e,t);
}
struct Interface {
 CURLMcode multi_perform(void*,int* running) { *running=1; ++polls; return CURLM_OK; }
 CURLMsg* multi_info_read(void*,int*) { return nullptr; }
 template<class... T> int easy_getinfo(T...) { return 0; }
 const char* easy_strerror(CURLcode) { return "stub"; }
 template<class... T> void multi_remove_handle(T...) {}
 template<class... T> void multi_add_handle(T...) {}
 CURLMcode multi_fdset(void*,fd_set* r,fd_set*,fd_set*,int* maxfd) {
  *maxfd=socketFd; if(socketFd>=0) FD_SET(socketFd,r); return CURLM_OK;
 }
 CURLMcode multi_timeout(void*,long* t){*t=600000;return CURLM_OK;}
} g_curlInterface;
void* realloc_simple(void* p, size_t n) { return std::realloc(p,n); }
struct Buffer {
 unsigned getMaxReadSize(){return 0;}
 unsigned getMaxWriteSize(){return 1024;}
 void WriteData(char*,unsigned){}
 void Clear(){}
};
class CCurlFile {
public:
 class CReadState {
 public:
  Buffer m_buffer;
  bool m_cancelled=false;
  ABORT_POINTER
  char* m_overflowBuffer=nullptr;
  unsigned m_overflowSize=0;
  int m_stillRunning=1;
  void* m_multiHandle=nullptr;
  void* m_easyHandle=nullptr;
  bool m_bFirstLoop=false,m_sendRange=false,m_bLastError=false,m_bRetry=false;
  int64_t m_filePos=0;
  void SetResume(){}
  int8_t FillBuffer(unsigned int want);
 };
 ABORT_OWNER
 CReadState* m_state=nullptr;
 CReadState* m_oldState=nullptr;
 bool m_seekable=true,m_allowRetry=false;
 int IoControl(IOControl request,void* param);
 void Reset();
 void Bind(CReadState* state);
};
"""
MAIN = r"""
int main(int argc,char** argv) {
 std::string test=argv[1]; CCurlFile file;
 if(test=="poisoned_states") {
  file.m_state=reinterpret_cast<CCurlFile::CReadState*>(1);
  file.m_oldState=reinterpret_cast<CCurlFile::CReadState*>(2);
  auto start=std::chrono::steady_clock::now();
  for(int i=0;i<10000;i++) if(file.IoControl(IOControl::CANCEL_READ,nullptr)!=0) return 1;
  auto elapsed=std::chrono::steady_clock::now()-start;
  std::cout<<"10000 cancel ioctls us="<<std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count()<<"\n";
  return file.m_readAbortRequested && elapsed<500ms ? 0:1;
 }
 CCurlFile::CReadState state; file.m_state=&state; file.Bind(&state);
 if(test=="reset") {
  file.IoControl(IOControl::CANCEL_READ,nullptr);state.m_cancelled=true;file.Reset();
  return !file.m_readAbortRequested && !state.m_cancelled ? 0:1;
 }
 if(test=="replacement") {
  std::thread replace([&]{for(int i=0;i<10000;i++) {
   auto* fresh=new CCurlFile::CReadState; file.Bind(fresh);
   file.m_state=fresh; file.m_oldState=&state;
   if(fresh->m_readAbortRequested!=&file.m_readAbortRequested) std::abort();
   delete fresh;
  }});
  for(int i=0;i<10000;i++) file.IoControl(IOControl::CANCEL_READ,nullptr);
  replace.join();return file.m_readAbortRequested ? 0:1;
 }
 int sockets[2];
 if(test=="fd_poll") {if(socketpair(AF_UNIX,SOCK_STREAM,0,sockets)!=0) return 2;socketFd=sockets[0];}
 int result=99;
 std::thread reader([&]{result=state.FillBuffer(1);});
 while(!inSelect.load()) std::this_thread::yield();
 auto start=std::chrono::steady_clock::now();
 file.IoControl(IOControl::CANCEL_READ,nullptr);
 reader.join();
 auto ms=std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now()-start).count();
 if(socketFd>=0) {close(sockets[0]);close(sockets[1]);}
 std::cout<<"result="<<result<<" cancel_join_ms="<<ms<<"\n";
 return result==FILLBUFFER_NO_DATA && ms<500 ? 0:1;
}
"""

class HttpCurlCancellation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="kodi-curl-abort-")
        cls.binary = Path(cls.tmp.name)/"curl-abort"
        header = (ROOT/"xbmc/filesystem/CurlFile.h").read_text()
        pointer = next(l.strip() for l in header.splitlines() if "m_readAbortRequested = nullptr;" in l)
        owner = next(l.strip() for l in header.splitlines() if "m_readAbortRequested{false};" in l)
        cpp = HARNESS.replace("ABORT_POINTER",pointer).replace("ABORT_OWNER",owner)+"\n#define select observed_select\n"
        for sig in ["int CCurlFile::IoControl(", "void CCurlFile::Reset(", "int8_t CCurlFile::CReadState::FillBuffer("]:
            text=(ROOT/"xbmc/filesystem/CurlFile.cpp").read_text()
            start=text.index(sig)
            # Column-zero close avoids counting both #ifdef select-loop endings.
            cpp += text[start:text.index("\n}",start)+2]+"\n"
        cpp = cpp.replace("int CCurlFile::IoControl(", function("xbmc/filesystem/CurlFile.cpp","static bool IsTransientCurlError(")+"\n\nint CCurlFile::IoControl(",1)
        common = function("xbmc/filesystem/CurlFile.cpp","void CCurlFile::SetCommonOptions(")
        bind = common[common.index("  state->m_readAbortRequested"):common.index("  CURL_HANDLE* h")]
        cpp += "\n#undef select\nvoid CCurlFile::Bind(CReadState* state) {"+bind+"}\n"+MAIN
        source=Path(cls.tmp.name)/"curl-abort.cpp"
        source.write_text(cpp)
        subprocess.run([os.environ.get("CXX","c++"),"-std=c++17","-pthread","-Wall","-Wextra","-Werror","-Wno-unused-parameter",str(source),"-o",str(cls.binary)],check=True)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def check(self, case):
        run=subprocess.run([str(self.binary),case],capture_output=True,text=True,timeout=3)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        if run.stdout: print(case+": "+run.stdout.strip())

    def test_ioctl_never_dereferences_seek_states(self): self.check("poisoned_states")
    def test_cancel_with_seek_state_replacement(self): self.check("replacement")
    def test_reset_clears_abort(self): self.check("reset")
    def test_bounded_real_select_with_fd(self): self.check("fd_poll")
    def test_bounded_real_select_without_fd(self): self.check("no_fd_poll")

    def test_source_lifecycle_contract(self):
        cache=function("xbmc/filesystem/FileCache.cpp","int CFileCache::IoControl(")
        cancel=cache[:cache.index("  if (request == IOControl::CACHE_STATUS)")]
        self.assertNotIn("lock(m_sync)",cancel)
        self.assertNotIn("StopThread(",cancel)
        self.assertIn("m_source->IoControl(IOControl::CANCEL_READ, nullptr)",cancel)
        self.assertLess(cancel.index("m_bStop = true"),cancel.index("m_seekEvent.Set()"))
        stop=function("xbmc/filesystem/FileCache.cpp","void CFileCache::StopThread(")
        self.assertLess(stop.index("IoControl(IOControl::CANCEL_READ"),stop.index("CThread::StopThread("))
        close=function("xbmc/filesystem/FileCache.cpp","void CFileCache::Close(")
        self.assertLess(close.index("StopThread();"),close.index("m_source->Close();"))
        header=(ROOT/"xbmc/filesystem/CurlFile.h").read_text()
        self.assertLess(header.index("std::atomic<bool> m_readAbortRequested{false}"),header.index("CReadState* m_state;"))
        reopen=function("xbmc/filesystem/CurlFile.cpp","bool CCurlFile::ReOpen(")
        self.assertLess(reopen.index("Close();"),reopen.index("Open(url)"))
        opening=function("xbmc/filesystem/CurlFile.cpp","bool CCurlFile::Open(")
        self.assertLess(opening.index("m_readAbortRequested = false"),opening.index("SetCommonOptions("))
        abort=function("xbmc/cores/VideoPlayer/DVDInputStreams/DVDInputStreamFile.cpp","void CDVDInputStreamFile::Abort(")
        self.assertIn("m_pFile->IoControl(IOControl::CANCEL_READ, nullptr)",abort)
        player=function("xbmc/cores/VideoPlayer/VideoPlayer.cpp","bool CVideoPlayer::CloseFile(")
        self.assertLess(player.index("m_pInputStream->Abort();"),player.index("StopThread();"))

if __name__=="__main__": unittest.main(verbosity=2)
