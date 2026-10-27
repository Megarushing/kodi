#!/usr/bin/env python3
"""Host behavior tests for real webOS abort-broadcast/deadline methods.
Run: python3 tools/mega/tests/test_webos_parallel_abort.py -v
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
STUBS = r'''
#include <algorithm>
#include <chrono>
#include <map>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
using namespace std::chrono_literals;
constexpr auto SCRIPT_STOP_TIMEOUT = 5s;
using CCriticalSection=std::recursive_mutex;
struct Handler {
 int aborts=0;
 void NotifyScriptAborting(void* invoker) {
   if (invoker) throw std::runtime_error("broadcast must address all scripts");
   ++aborts;
 }
};
struct CScriptInvocationManager {
 CCriticalSection m_critSection;
 std::map<int,Handler*> m_invocationHandlers;
 std::optional<std::chrono::steady_clock::time_point> m_shutdownDeadline;
 void NotifyAllScriptsAborting();
 std::chrono::milliseconds GetShutdownScriptTimeout(std::chrono::milliseconds fallback) const;
};
'''.replace('CCriticalSection m_critSection;', 'mutable CCriticalSection m_critSection;')
MAIN = r'''
int main(int argc,char** argv) {
 if (argc!=2) return 2;
 std::string mode=argv[1]; CScriptInvocationManager m; Handler a,b;
 m.m_invocationHandlers[1]=&a; m.m_invocationHandlers[2]=&b;
 if (mode=="broadcast") {
   m.NotifyAllScriptsAborting();
   if (a.aborts!=1 || b.aborts!=1) throw std::runtime_error("not every handler notified");
   auto remaining=m.GetShutdownScriptTimeout(5s);
   if (remaining<=0ms || remaining>5s) throw std::runtime_error("no bounded shared deadline");
 } else if (mode=="expired") {
   m.m_shutdownDeadline=std::chrono::steady_clock::now()-1ms;
   m.NotifyAllScriptsAborting();
   for (int i=0;i<3;++i)
     if (m.GetShutdownScriptTimeout(5s)!=0ms) throw std::runtime_error("deadline renewed for another script");
 } else if (mode=="ordinary") {
   if (m.GetShutdownScriptTimeout(5s)!=5s) throw std::runtime_error("ordinary per-script timeout changed");
 } else if (mode=="cap") {
   m.m_shutdownDeadline=std::chrono::steady_clock::now()+5s;
   if (m.GetShutdownScriptTimeout(25ms)!=25ms) throw std::runtime_error("request limit exceeded");
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


class ParallelAbortTest(unittest.TestCase):
    def scenario(self, name):
        source = (ROOT / 'xbmc/interfaces/generic/ScriptInvocationManager.cpp').read_text()
        notify = 'void CScriptInvocationManager::NotifyAllScriptsAborting()'
        budget = 'std::chrono::milliseconds CScriptInvocationManager::GetShutdownScriptTimeout('
        self.assertTrue(notify in source, 'Missing all-script abort broadcast')
        self.assertTrue(budget in source, 'Missing shared shutdown deadline')
        compiler = shutil.which('c++')
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory(prefix='mega-parallel-abort-') as d:
            cpp = pathlib.Path(d) / 'abort.cpp'
            binary = pathlib.Path(d) / 'abort'
            cpp.write_text(STUBS + body(source, notify) + body(source, budget) + MAIN)
            subprocess.run([str(compiler), '-std=c++17', '-DTARGET_WEBOS', str(cpp), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary), name], capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_abort_reaches_all_handlers_before_waiting(self):
        self.scenario('broadcast')

    def test_expired_global_deadline_does_not_renew_for_later_scripts(self):
        self.scenario('expired')

    def test_normal_script_stop_keeps_its_existing_budget(self):
        self.scenario('ordinary')

    def test_shared_budget_never_exceeds_requested_timeout(self):
        self.scenario('cap')

    def test_application_broadcasts_before_service_stops(self):
        source = (ROOT / 'xbmc/application/Application.cpp').read_text()
        stop = body(source, 'bool CApplication::Stop(int exitCode)')
        marker = 'NotifyAllScriptsAborting()'
        self.assertTrue(marker in stop, 'Application does not notify scripts before sequential stops')
        self.assertLess(stop.index(marker), stop.index('GetServiceAddons().Stop()'))
        self.assertLess(stop.index('m_bStop = true'), stop.index(marker))


if __name__ == '__main__':
    unittest.main()
