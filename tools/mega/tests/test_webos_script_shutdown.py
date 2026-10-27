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
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>
using namespace std::chrono_literals;
using CCriticalSection = std::recursive_mutex;
constexpr auto SCRIPT_STOP_TIMEOUT = 5s;
constexpr auto SCRIPT_STOP_GRACE = 1s;
constexpr int LOGERROR=1, LOGWARNING=2, InvokerStateFailed=9;
struct CLog { template<class... T> static void Log(T&&...) {} };
namespace XbmcThreads {
template<class T=void> struct EndTime {
  template<class U> explicit EndTime(U) {}
  std::chrono::milliseconds GetTimeLeft() const { return 1ms; }
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
  CCriticalSection m_critSection;
  std::map<int,LanguageInvokerThread> m_scripts;
  std::map<int,int> m_scriptPaths;
  std::shared_ptr<CLanguageInvokerThread> m_lastInvokerThread;
  std::map<int,Handler*> m_invocationHandlers;
  void Process() {}
  void Uninitialize();
};
// Python API collaborators record shutdown ordering, not Python implementation.
struct PyElem {};
struct PyList : std::vector<PyElem> { bool hadSomethingRemoved=false; void lock() {} void unlock() {} };
struct Announcer { void RemoveAnnouncer(void*) {} };
struct CServiceBroker { static Announcer* GetAnnouncementManager() { static Announcer a; return &a; } };
struct PyInterpreterState { int value; };
using PyThreadState = int;
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
PyThreadState* PyInterpreterState_ThreadHead(PyInterpreterState*) { return nullptr; }
void PyThreadState_Swap(PyThreadState*) {}
using PyObject=int;
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
  if (mode=="stubborn" || mode=="cooperative" || mode=="done") {
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
        cls.tmp = tempfile.TemporaryDirectory(prefix='mega-webos-shutdown-')
        source = pathlib.Path(cls.tmp.name) / 'shutdown.cpp'
        source.write_text(STUBS + body(manager, 'void CScriptInvocationManager::Uninitialize()')
                          + body(python, 'void XBPython::Uninitialize()')
                          + body(python, 'XBPython::~XBPython()') + MAIN)
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


if __name__ == '__main__':
    unittest.main()
