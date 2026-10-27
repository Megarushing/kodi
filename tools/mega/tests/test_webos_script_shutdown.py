#!/usr/bin/env python3
"""Compile real shutdown bodies with host collaborators; TV tests remain required.
Run: python3 tools/mega/tests/test_webos_script_shutdown.py -v
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
STUBS = r'''
#include <chrono>
#include <atomic>
#include <algorithm>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
using namespace std::chrono_literals;
using CCriticalSection = std::recursive_mutex;
constexpr auto SCRIPT_STOP_TIMEOUT = 5s;
constexpr auto SCRIPT_STOP_GRACE = 1s;
constexpr int LOGERROR=1, LOGWARNING=2, LOGDEBUG=3, InvokerStateStopping=2, InvokerStateExecutionDone=3, InvokerStateFailed=9;
constexpr auto PYTHON_SCRIPT_TIMEOUT=5000ms;
struct { bool m_bStop=false; } g_application;
struct CLog { template<class... T> static void Log(T&&...) {} };
namespace XbmcThreads {
template<class T=void> struct EndTime {
  template<class U> explicit EndTime(U) {}
  std::chrono::milliseconds GetTimeLeft() const { return 1ms; }
  bool IsTimePast() const { return true; }
};
}
struct CLanguageInvokerThread {
  bool stubborn=false, blockingStop=false, stopAsked=false;
  int state=0;
  void Stop(bool wait) { stopAsked=true; blockingStop=wait; }
  bool WaitForExit(std::chrono::milliseconds) { return !stubborn; }
  int GetState() const { return state; }
};
struct LanguageInvokerThread { bool done=false; std::shared_ptr<CLanguageInvokerThread> thread; std::string script; };
struct Handler { int calls=0; void Uninitialize() { ++calls; } };
struct CScriptInvocationManager {
  mutable CCriticalSection m_critSection;
  std::optional<std::chrono::steady_clock::time_point> m_shutdownDeadline;
  std::map<int,LanguageInvokerThread> m_scripts;
  std::map<int,int> m_scriptPaths;
  std::shared_ptr<CLanguageInvokerThread> m_lastInvokerThread;
  std::map<int,Handler*> m_invocationHandlers;
  void Process() {}
  void Uninitialize();
  static CScriptInvocationManager& GetInstance() { static CScriptInvocationManager m; return m; }
  std::chrono::milliseconds GetShutdownScriptTimeout(std::chrono::milliseconds fallback) const;
};
// Python API collaborators record shutdown ordering, not Python implementation.
struct PyElem {};
struct PyList : std::vector<PyElem> { bool hadSomethingRemoved=false; void lock() {} void unlock() {} };
struct Announcer { void RemoveAnnouncer(void*) {} };
struct Messenger { bool IsProcessThread() { return false; } void ProcessMessages() {} };
struct CServiceBroker {
 static Announcer* GetAnnouncementManager() { static Announcer a; return &a; }
 static Messenger* GetAppMessenger() { static Messenger m; return &m; }
};
struct PyInterpreterState { int value; };
using PyObject=int;
struct PyThreadState { PyInterpreterState* interp=nullptr; PyObject* async_exc=nullptr; };
using PyGILState_STATE = int;
static PyInterpreterState mainInterp{0}, orphanInterp{1};
static bool orphan=false, gilHeld=false;
static int finalized=0;
int Py_IsInitialized() { return 1; }
PyGILState_STATE PyGILState_Ensure() { gilHeld=true; return 0; }
void PyGILState_Release(PyGILState_STATE) { gilHeld=false; }
PyInterpreterState* PyInterpreterState_Main() { return &mainInterp; }
PyInterpreterState* PyInterpreterState_Head() { return orphan ? &orphanInterp : &mainInterp; }
PyInterpreterState* PyInterpreterState_Next(PyInterpreterState* p) { return p==&orphanInterp ? &mainInterp : nullptr; }
static PyThreadState fakeState;
PyThreadState* PyInterpreterState_ThreadHead(PyInterpreterState*) { return &fakeState; }
PyThreadState* PyThreadState_Next(PyThreadState*) { return nullptr; }
void PyThreadState_Swap(PyThreadState*) {}
void PyEval_RestoreThread(PyThreadState*) {}
void PyEval_ReleaseThread(PyThreadState*) {}
static PyObject exception=1;
static PyObject* PyExc_SystemExit=&exception;
static int injections=0;
void Py_XDECREF(PyObject*) {}
void Py_XINCREF(PyObject*) { ++injections; }
struct CSingleExit {
 CCriticalSection& c; CSingleExit(CCriticalSection& v):c(v) { c.unlock(); }
 ~CSingleExit() { c.lock(); }
};
struct CPythonInvoker {
 CCriticalSection m_critical;
 bool m_stop=false, m_addon=false;
 PyThreadState* m_threadState=&fakeState;
 std::string m_sourceFile="fixture.py";
 struct Event { bool Wait(std::chrono::milliseconds) { return false; } } m_stoppedEvent;
 bool IsRunning() { return true; }
 void setState(int) {}
 int GetId() { return 1; }
 void AbortNotification() {}
 void pulseGlobalEvent() {}
 bool stop(bool);
};
PyObject* PyImport_GetModuleDict() { return nullptr; }
void PyDict_Clear(PyObject*) {}
void Py_Finalize() { ++finalized; }
#define XBMC_TRACE do {} while(false)
#define LOCK_AND_COPY(type,dest,src) if (!m_bInitialized) return; std::unique_lock<PyList> lock(src); type dest; dest=src
struct XBPython {
  bool m_bInitialized=true, m_skipFinalize=false;
  int m_iDllScriptCounter=0;
  CCriticalSection m_critSection;
  PyList m_vecPyList;
  void Uninitialize();
  ~XBPython();
};
'''
MAIN = r'''
int main(int argc,char** argv) {
  std::string mode=argc>1?argv[1]:"";
  if (mode=="stop-quitting" || mode=="stop-ordinary" || mode=="stop-busy") {
    CPythonInvoker invoker;
    g_application.m_bStop=mode!="stop-ordinary";
    std::atomic<bool> locked{false};
    std::thread worker;
    if (mode=="stop-busy") {
      worker=std::thread([&]{ std::lock_guard<CCriticalSection> guard(invoker.m_critical); locked=true; std::this_thread::sleep_for(200ms); });
      while (!locked) std::this_thread::yield();
    }
    auto start=std::chrono::steady_clock::now();
    invoker.stop(false);
    auto elapsed=std::chrono::steady_clock::now()-start;
    if (worker.joinable()) worker.join();
    if (mode=="stop-busy" && elapsed>100ms) throw std::runtime_error("shutdown waited for an invoker critical section");
    if (mode=="stop-quitting" && injections) throw std::runtime_error("shutdown injected async SystemExit into imports");
    if (mode=="stop-ordinary" && !injections) throw std::runtime_error("ordinary script stop lost interruption");
  } else if (mode=="stubborn" || mode=="cooperative" || mode=="done") {
    CScriptInvocationManager m; Handler handler; m.m_invocationHandlers[1]=&handler;
    auto thread=std::make_shared<CLanguageInvokerThread>(); thread->stubborn=mode=="stubborn";
    m.m_scripts.emplace(1,LanguageInvokerThread{mode=="done",thread,"fixture.py"});
    m.Uninitialize();
    if (thread->blockingStop) throw std::runtime_error("shutdown used an unbounded joining Stop(true)");
    if (mode!="done" && !thread->stopAsked) throw std::runtime_error("stop request missing");
    if (handler.calls!=1 || !m.m_scripts.empty()) return 2;
  } else {
    orphan=mode=="orphan";
    { XBPython py;
      py.m_iDllScriptCounter=mode=="running"?1:0;
      py.Uninitialize();
      const bool unsafe=mode=="orphan" || mode=="running";
      if (py.m_skipFinalize!=unsafe || gilHeld!=unsafe) throw std::runtime_error("unsafe Python was not quarantined");
      py.Uninitialize(); // idempotent, including the GIL retained path
      if (gilHeld!=unsafe) return 3;
    }
    if (finalized!=((mode=="orphan" || mode=="running")?0:1)) throw std::runtime_error("finalized orphan Python");
  }
}
'''


def body(source, signature):
    start = source.index(signature)
    pos = source.index('{', start)
    depth = 1
    end = pos + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end] + '\n'


class WebOSShutdownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manager = (ROOT / 'xbmc/interfaces/generic/ScriptInvocationManager.cpp').read_text()
        python = (ROOT / 'xbmc/interfaces/python/XBPython.cpp').read_text()
        invoker = (ROOT / 'xbmc/interfaces/python/PythonInvoker.cpp').read_text()
        cls.tmp = tempfile.TemporaryDirectory(prefix='mega-webos-shutdown-')
        source = pathlib.Path(cls.tmp.name) / 'shutdown.cpp'
        source.write_text(STUBS + body(manager, 'void CScriptInvocationManager::Uninitialize()')
                          + body(manager, 'std::chrono::milliseconds CScriptInvocationManager::GetShutdownScriptTimeout(')
                          + body(python, 'void XBPython::Uninitialize()')
                          + body(python, 'XBPython::~XBPython()')
                          + body(invoker, 'bool CPythonInvoker::stop(bool abort)') + MAIN)
        cls.binary = source.with_suffix('')
        compiler = shutil.which('c++')
        if not compiler:
            raise RuntimeError('C++17 compiler required')
        subprocess.run([compiler, '-std=c++17', '-DTARGET_WEBOS',
                        '-DPY_VERSION_HEX=0x030B0000', str(source), '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def scenario(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stuck_invoker_cannot_cause_unbounded_join(self):
        self.scenario('stubborn')

    def test_cooperative_invoker_is_asked_to_stop(self):
        self.scenario('cooperative')

    def test_finished_invoker_needs_no_stop(self):
        self.scenario('done')

    def test_running_python_is_quarantined_without_finalization(self):
        self.scenario('running')

    def test_orphan_interpreter_is_quarantined_without_finalization(self):
        self.scenario('orphan')

    def test_clean_python_still_finalizes(self):
        self.scenario('clean')

    def test_shutdown_does_not_interrupt_an_import_with_async_exception(self):
        self.scenario('stop-quitting')

    def test_shutdown_does_not_wait_for_invoker_critical_section(self):
        self.scenario('stop-busy')

    def test_ordinary_script_stop_retains_async_interruption(self):
        self.scenario('stop-ordinary')


if __name__ == '__main__':
    unittest.main()
