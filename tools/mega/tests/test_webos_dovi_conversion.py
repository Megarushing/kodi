#!/usr/bin/env python3
"""Compile the real Starfish HEVC setup case and check conversion/payload order.
Run: python3 tools/mega/tests/test_webos_dovi_conversion.py -v
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[3]
STUBS = r'''
#include <cstdint>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
constexpr int AV_CODEC_ID_HEVC=173, LOGINFO=1;
constexpr uint32_t MKTAG(char a,char b,char c,char d) { return uint32_t(a)|(uint32_t(b)<<8)|(uint32_t(c)<<16)|(uint32_t(d)<<24); }
enum class StreamHdrType { HDR_TYPE_DOLBYVISION, HDR_TYPE_HDR10 };
struct Dovi { int dv_profile=7; bool el_present_flag=true; };
struct Extra {
 bool present=true;
 explicit operator bool() const { return present; }
 const uint8_t* GetData() const { return nullptr; }
 int GetSize() const { return 0; }
};
struct Hints { int codec=AV_CODEC_ID_HEVC; uint32_t codec_tag=MKTAG('h','v','c','1'); StreamHdrType hdrType=StreamHdrType::HDR_TYPE_DOLBYVISION; Dovi dovi; Extra extradata; bool cryptoSession=false; };
static bool compatibility=false, allowed=true, converterOpens=true;
struct CSetting { virtual ~CSetting()=default; };
struct CSettingList : CSetting {};
struct CSettings {
 static constexpr const char* SETTING_VIDEOPLAYER_ALLOWEDHDRFORMATS="allowed";
 static constexpr const char* SETTING_VIDEOPLAYER_CONVERTDOVI="convert";
 static constexpr int VIDEOPLAYER_ALLOWED_HDR_TYPE_DOLBY_VISION=0;
 std::shared_ptr<CSetting> GetSetting(const char*) { return std::make_shared<CSettingList>(); }
 bool GetBool(const char*) { return compatibility; }
};
struct CSettingUtils { static bool FindIntInList(std::shared_ptr<CSettingList>,int) { return allowed; } };
struct SettingsComponent { std::shared_ptr<CSettings> GetSettings() { return std::make_shared<CSettings>(); } };
struct CServiceBroker { static SettingsComponent* GetSettingsComponent() { static SettingsComponent c; return &c; } };
struct CLog { template<class... T> static void LogF(T&&...) {} template<class... T> static void Log(T&&...) {} };
struct CBitstreamConverter {
 bool convert=false, removeDovi=false, removeHdr=false;
 bool Open(int,const uint8_t*,int,bool) { return converterOpens; }
 void SetConvertDovi(bool v) { convert=v; }
 void SetRemoveDovi(bool v) { removeDovi=v; }
 void SetRemoveHdr10Plus(bool v) { removeHdr=v; }
};
struct Value {
 std::map<std::string,Value> children;
 std::string text;
 int number=-1;
 Value& operator[](const std::string& k) { return children[k]; }
 Value& operator=(const char* v) { text=v; return *this; }
 Value& operator=(int v) { number=v; return *this; }
};
struct Decoder {
 Hints m_hints;
 std::string m_formatname="h265";
 std::unique_ptr<CBitstreamConverter> m_bitstream;
 Value payloadArg;
 void Setup() {
 switch(m_hints.codec) {
'''
MAIN = r'''
 }
 }
};
int main(int argc,char** argv) {
 if(argc!=2) return 2;
 std::string mode=argv[1]; Decoder d;
 if(mode=="p7-setting") { d.m_hints.dovi.el_present_flag=false; compatibility=true; }
 if(mode=="p8") { d.m_hints.dovi.dv_profile=8; d.m_hints.dovi.el_present_flag=false; compatibility=true; }
 if(mode=="hdr10") allowed=false;
 if(mode=="failed") converterOpens=false;
 if(mode=="no-extra") d.m_hints.extradata.present=false;
 if(mode=="p7-off") { d.m_hints.dovi.el_present_flag=false; compatibility=false; }
 d.Setup();
 auto& meta=d.payloadArg["option"]["externalStreamingInfo"]["contents"]["DolbyHdrInfo"];
 if(mode=="p7-el" || mode=="p7-setting") {
   if(!d.m_bitstream || !d.m_bitstream->convert) throw std::runtime_error("Profile 7 conversion was not enabled");
   if(meta["profileId"].number!=8 || meta["trackType"].text!="single") throw std::runtime_error("decoder metadata was built before conversion hints were changed");
 } else if(mode=="hdr10") {
   if(!d.m_bitstream || !d.m_bitstream->removeDovi || d.m_bitstream->convert || meta["profileId"].number!=-1) throw std::runtime_error("HDR10 fallback was converted or advertised as Dolby Vision");
 } else if(mode=="p8") {
   if(!d.m_bitstream || d.m_bitstream->convert || meta["profileId"].number!=8) throw std::runtime_error("native Profile 8 changed unnecessarily");
 } else {
   if(d.m_bitstream && d.m_bitstream->convert) throw std::runtime_error("conversion advertised without a working converter/request");
   if(meta["profileId"].number!=7) throw std::runtime_error("profile advertised as converted without a converter");
 }
 if(d.m_bitstream && !d.m_bitstream->removeHdr) throw std::runtime_error("existing HDR10+ stripping lost");
}
'''


def hevc_case():
    text = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecStarfish.cpp').read_text()
    start = text.index('    case AV_CODEC_ID_HEVC:')
    pos = text.index('{', start)
    depth = 1
    end = pos + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end] + '\n'


class DoviSetupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('c++')
        if not compiler:
            raise RuntimeError('C++17 compiler required')
        cls.tmp = tempfile.TemporaryDirectory(prefix='mega-webos-dovi-')
        source = pathlib.Path(cls.tmp.name) / 'dovi.cpp'
        source.write_text(STUBS + hevc_case() + MAIN)
        cls.binary = source.with_suffix('')
        subprocess.run([compiler, '-std=c++17', '-DTARGET_WEBOS', '-DHAVE_LIBDOVI=1', str(source), '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def scenario(self, mode):
        r = subprocess.run([str(self.binary), mode], capture_output=True, text=True, timeout=3)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_profile7_enhancement_layer_forces_compatible_profile_and_single_track(self):
        self.scenario('p7-el')

    def test_compatibility_setting_converts_profile7_without_el_flag(self):
        self.scenario('p7-setting')

    def test_profile8_is_not_reconverted(self):
        self.scenario('p8')

    def test_disabling_dolby_vision_retains_hdr10_fallback(self):
        self.scenario('hdr10')

    def test_failed_converter_keeps_original_decoder_profile(self):
        self.scenario('failed')

    def test_absent_extradata_does_not_advertise_false_conversion(self):
        self.scenario('no-extra')

    def test_profile7_without_el_or_setting_is_not_changed(self):
        self.scenario('p7-off')

    def test_webos_conversion_has_a_required_real_library(self):
        cmake = (ROOT / 'cmake/platform/linux/webos.cmake').read_text()
        self.assertTrue(any('PLATFORM_REQUIRED_DEPS' in line and 'LibDovi' in line for line in cmake.splitlines()),
                        'webOS conversion must fail configuration if libdovi is absent')

    def test_compatibility_setting_is_available_on_webos(self):
        root = ET.parse(ROOT / 'system/settings/settings.xml').getroot()
        s = root.find(".//setting[@id='videoplayer.convertdovi']")
        if s is None:
            self.fail('compatibility setting missing')
        req = s.find('requirement')
        if req is None:
            self.fail('compatibility requirement missing')
        requirement = ET.tostring(req, encoding='unicode')
        self.assertIn('HAVE_WEBOS', requirement)


if __name__ == '__main__':
    unittest.main()
