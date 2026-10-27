#!/usr/bin/env python3
"""Compile the real Wayland RoundtripQueue body; verify no pump self-wait.
Run: python3 tools/mega/tests/test_webos_wayland_roundtrip.py -v
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
STUBS = r'''
#include <atomic>
#include <mutex>
#include <stdexcept>
#include <string>
using CCriticalSection=std::recursive_mutex;
namespace wayland {
struct event_queue_t {};
struct display_t { int calls=0; void roundtrip_queue(const event_queue_t&) { ++calls; } };
}
struct Event {
 bool ready=false;
 int waits=0;
 void Reset() { ready=false; }
 void Set() { ready=true; }
 void Wait() { ++waits; if (!ready) throw std::runtime_error("event pump tried to wait on itself"); }
};
struct Pump {
 bool self=false, m_bStop=false;
 wayland::display_t m_display;
 CCriticalSection m_roundtripQueueMutex;
 std::atomic<wayland::event_queue_t*> m_roundtripQueue{nullptr};
 Event m_roundtripQueueEvent;
 int interrupts=0;
 bool IsCurrentThread() { return self; }
 void InterruptPoll() {
   ++interrupts;
   // A foreign caller can wake the pump, but the pump cannot service itself
   // while its own callback is synchronously waiting for the completion event.
   if (!self && !m_bStop) {
     auto* q=m_roundtripQueue.exchange(nullptr);
     if (q) { m_display.roundtrip_queue(*q); m_roundtripQueueEvent.Set(); }
   }
 }
'''
MAIN = r'''
};
int main(int argc,char** argv) {
 if (argc!=2) return 2;
 std::string mode=argv[1]; Pump p; wayland::event_queue_t q;
 p.self=mode=="self" || mode=="self-stopping";
 p.m_bStop=mode=="stopping" || mode=="self-stopping";
 p.RoundtripQueue(q);
 if (p.self) {
   if (p.m_display.calls!=1 || p.interrupts || p.m_roundtripQueueEvent.waits || p.m_roundtripQueue.load())
     throw std::runtime_error("own-thread roundtrip was delegated back to the event pump");
 } else if (mode=="foreign") {
   if (p.m_display.calls!=1 || p.interrupts!=1 || p.m_roundtripQueueEvent.waits!=1)
     throw std::runtime_error("foreign-thread delegation changed");
 } else {
   if (p.m_display.calls || p.m_roundtripQueueEvent.waits)
     throw std::runtime_error("stopped foreign caller waited for a stopped pump");
 }
}
'''


def method(source):
    start = source.index('  void RoundtripQueue(wayland::event_queue_t const& queue)')
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end] + '\n'


class WaylandRoundtripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('c++')
        if not compiler:
            raise RuntimeError('C++17 compiler required')
        cls.tmp = tempfile.TemporaryDirectory(prefix='mega-wayland-roundtrip-')
        source = pathlib.Path(cls.tmp.name) / 'roundtrip.cpp'
        source.write_text(STUBS + method((ROOT / 'xbmc/windowing/wayland/WinEventsWayland.cpp').read_text()) + MAIN)
        cls.binary = source.with_suffix('')
        subprocess.run([compiler, '-std=c++17', '-DTARGET_WEBOS', str(source), '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def scenario(self, name):
        r = subprocess.run([str(self.binary), name], capture_output=True, text=True, timeout=3)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_pump_callback_does_not_wait_for_itself(self):
        self.scenario('self')

    def test_foreign_thread_keeps_delegated_roundtrip(self):
        self.scenario('foreign')

    def test_stopped_foreign_caller_does_not_wait(self):
        self.scenario('stopping')

    def test_pump_can_finish_inflight_callback_during_shutdown(self):
        self.scenario('self-stopping')


if __name__ == '__main__':
    unittest.main()
