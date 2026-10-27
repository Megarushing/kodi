#!/usr/bin/env python3
"""Compile real widget/weather wait bodies to test cancellation at webOS Quit.
Run: python3 tools/mega/tests/test_webos_job_shutdown.py -v
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
STUBS = r'''
#include <chrono>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
using namespace std::chrono_literals;
static std::string mode;
static int waits=0, stops=0, starts=0, properties=0, messages=0;
struct { bool m_bStop=false; } g_application;
struct Event {
 bool Wait(std::chrono::milliseconds) {
   if (mode=="runner-normal") return true;
   if (++waits>8) throw std::runtime_error("script wait ignored application shutdown");
   if (mode=="runner-mid") g_application.m_bStop=true;
   return false;
 }
};
namespace XbmcThreads {
template<class T=void> struct EndTime {
 int ticks=0;
 template<class U> EndTime(U) {}
 bool IsTimePast() { return ++ticks>2; }
};
}
struct CScriptInvocationManager {
 static CScriptInvocationManager& GetInstance() { static CScriptInvocationManager m; return m; }
 bool IsRunning(int) { return mode!="weather-normal"; }
 void Stop(int) { ++stops; }
 template<class A,class B> int ExecuteAsync(const std::string&,const A&,const B&) { ++starts; return 1; }
};
struct CGUIDialogProgress { bool WaitOnEvent(Event&) { return true; } };
struct CGUIDialogBusy { static bool WaitOnEvent(Event&,int) { return true; } };
struct CRunningScriptObserver { CRunningScriptObserver(int,Event&) {} void Abort() {} };
constexpr int WINDOW_DIALOG_PROGRESS=1,GUI_MSG_NOTIFY_ALL=2,GUI_MSG_WEATHER_FETCHED=3,LOGINFO=4,LOGERROR=5,LOGDEBUG=6;
struct CGUIMessage { template<class... T> CGUIMessage(T&&...) {} };
struct WindowManager {
 bool IsModalDialogTopmost(int) { return false; }
 template<class T> T* GetWindow(int) { static T t; return &t; }
 void SendThreadMessage(CGUIMessage&) { ++messages; }
};
struct GUI { WindowManager& GetWindowManager() { static WindowManager w; return w; } };
struct Messenger { bool IsProcessThread() { return false; } };
struct Network { bool IsAvailable() { return true; } };
namespace ADDON {
struct Addon { std::string LibPath() { return "weather.py"; } };
using AddonPtr=std::shared_ptr<Addon>;
enum class AddonType { SCRIPT_WEATHER };
namespace OnlyEnabled { enum class CHOICE { YES }; constexpr auto CHOICE_YES=CHOICE::YES; }
}
using namespace ADDON;
struct AddonMgr {
 bool GetAddon(const std::string&,AddonPtr& a,AddonType,OnlyEnabled::CHOICE) { a=std::make_shared<Addon>(); return true; }
};
struct CSettings { static constexpr int SETTING_WEATHER_ADDON=1; std::string GetString(int) { return "weather"; } };
struct SettingsComponent { CSettings* GetSettings() { static CSettings s; return &s; } };
struct CServiceBroker {
 static Messenger* GetAppMessenger() { static Messenger m; return &m; }
 static GUI* GetGUI() { static GUI g; return &g; }
 static Network& GetNetwork() { static Network n; return n; }
 static AddonMgr& GetAddonMgr() { static AddonMgr m; return m; }
 static SettingsComponent* GetSettingsComponent() { static SettingsComponent s; return &s; }
};
struct CLog { template<class... T> static void Log(T&&...) {} };
namespace KODI::TIME {
void Sleep(std::chrono::milliseconds) {
 if (++waits>4) throw std::runtime_error("weather wait ignored application shutdown");
 g_application.m_bStop=true;
}
}
struct CScriptRunner {
 Event m_scriptDone;
 bool IsCancelled() const { return mode=="runner-cancelled"; }
 bool IsSuccessful() const { return true; }
 bool WaitOnScriptResult(int,const std::string&,const std::string&);
};
struct CWeatherJob {
 int m_location=1;
 void SetFromProperties() { ++properties; }
 bool DoWork();
};
'''
MAIN = r'''
int main(int argc,char** argv) {
 if (argc!=2) return 2;
 mode=argv[1];
 g_application.m_bStop=mode=="runner-initial" || mode=="weather-initial";
 if (mode.rfind("runner",0)==0) {
   CScriptRunner runner;
   bool ok=runner.WaitOnScriptResult(1,"fixture.py","fixture");
   if (mode=="runner-normal" && !ok) throw std::runtime_error("normal directory result lost");
   if (mode!="runner-normal" && ok) throw std::runtime_error("shutdown/cancel falsely reported success");
   if (mode=="runner-cancelled" && stops!=1) throw std::runtime_error("normal cancellation no longer stops script");
   if (mode!="runner-cancelled" && stops) throw std::runtime_error("Quit blocked worker by stopping Python directly");
 } else {
   CWeatherJob job; bool ok=job.DoWork();
   if (mode=="weather-normal") {
     if (!ok || properties!=1 || messages!=1 || starts!=1) throw std::runtime_error("normal weather completion changed");
   } else {
     if (ok || properties || messages) throw std::runtime_error("cancelled weather published partial result");
     if (mode=="weather-initial" && starts) throw std::runtime_error("weather script launched during exit");
   }
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


class JobShutdownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('c++')
        if not compiler:
            raise RuntimeError('C++17 compiler required')
        cls.tmp = tempfile.TemporaryDirectory(prefix='mega-job-shutdown-')
        source = pathlib.Path(cls.tmp.name) / 'jobs.cpp'
        source.write_text(STUBS
                          + body((ROOT / 'xbmc/interfaces/generic/ScriptRunner.cpp').read_text(),
                                 'bool CScriptRunner::WaitOnScriptResult(')
                          + body((ROOT / 'xbmc/weather/WeatherJob.cpp').read_text(),
                                 'bool CWeatherJob::DoWork()') + MAIN)
        cls.binary = source.with_suffix('')
        subprocess.run([compiler, '-std=c++17', '-DTARGET_WEBOS', str(source), '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def scenario(self, mode):
        r = subprocess.run([str(self.binary), mode], capture_output=True, text=True, timeout=3)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_worker_stops_waiting_when_shutdown_begins(self):
        self.scenario('runner-mid')

    def test_worker_does_not_enter_result_wait_during_exit(self):
        self.scenario('runner-initial')

    def test_normal_directory_result_is_preserved(self):
        self.scenario('runner-normal')

    def test_normal_directory_cancellation_still_stops_script(self):
        self.scenario('runner-cancelled')

    def test_weather_wait_honours_shutdown(self):
        self.scenario('weather-mid')

    def test_weather_does_not_start_new_script_during_exit(self):
        self.scenario('weather-initial')

    def test_normal_weather_still_publishes_result(self):
        self.scenario('weather-normal')


if __name__ == '__main__':
    unittest.main()
