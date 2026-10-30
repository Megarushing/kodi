#!/usr/bin/env python3
"""Host regression tests for the real CGUIVideoControl render methods.

Compile their unchanged source bodies with lightweight engine collaborators so
hardware-layer/GUI dispatch can be tested without an Android display. This is
not a GPU/integration test; validate actual overlays on the Shield as well.
Run: python3 tools/mega/tests/test_video_control_render.py -v
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
STUBS = r'''
#include <cstdint>
#include <cstdio>
#include <string>
#include <type_traits>
namespace KODI { namespace UTILS { namespace COLOR { using Color = uint32_t; } } }
// Piers: dual-pass GUI rendering. The single-pass order keeps the Omega behaviour (Clear).
enum RENDER_ORDER { RENDER_ORDER_ALL_BACK_TO_FRONT, RENDER_ORDER_BACK_TO_FRONT, RENDER_ORDER_FRONT_TO_BACK };
struct CRect { void Intersect(const CRect&) {} };
struct TransformMatrix {};
struct Graphics {
  int transforms=0, clears=0, scissors=0;
  void SetViewWindow(float, float, float, float) {}
  void SetTransform(const TransformMatrix&, double, double) { ++transforms; }
  uint32_t MergeAlpha(uint32_t a) { return a; }
  CRect GetScissors() { return {}; }
  void SetScissors(const CRect&) { ++scissors; }
  void Clear(int) { ++clears; }
  void RemoveTransform() { --transforms; }
  RENDER_ORDER GetRenderOrder() { return RENDER_ORDER_ALL_BACK_TO_FRONT; }
} graphics;
struct CGUITexture { static void DrawQuad(const CRect&, uint32_t, void*, void*, float, bool) {} };
struct CApplicationPlayer {
  bool video=true, surface=true, paused=false;
  int guiCalls=0, nonGuiCalls=0, renderTransform=-1;
  bool IsRenderingVideo() { return video; }
  bool IsPausedPlayback() { return paused; }
  bool IsRenderingVideoLayer() { return surface; }
  void Render(bool clear, uint32_t alpha, bool gui=true) {
    (void)clear; (void)alpha;
    (gui ? guiCalls : nonGuiCalls)++;
    renderTransform=graphics.transforms;
  }
} player;
struct CApplicationPowerHandling {
  int resets=0;
  void ResetScreenSaver() { ++resets; }
} powerHandling;
struct Components {
  template<class T> T* GetComponent() const {
    if constexpr (std::is_same_v<T,CApplicationPlayer>) return &player;
    else return &powerHandling;
  }
} components;
struct WinSystem { Graphics& GetGfxContext() { return graphics; } } winSystem;
struct CServiceBroker {
  static Components& GetAppComponents() { return components; }
  static WinSystem* GetWinSystem() { return &winSystem; }
};
struct CGUIControl {
  int baseRender=0, baseRenderEx=0;
  void Render() { ++baseRender; }
  void RenderEx() { ++baseRenderEx; }
};
struct CGUIVideoControl : CGUIControl {
  float m_posX=10, m_posY=20, m_width=640, m_height=360;
  CRect GetRenderRegion() { return {}; }
  void Render();
  void RenderEx();
};
'''
MAIN = r'''
int main(int argc, char** argv) {
  if (argc!=2) return 2;
  const std::string scenario=argv[1];
  CGUIVideoControl control;
  if (scenario=="texture") player.surface=false;
  if (scenario=="idle") player.video=false;
  if (scenario=="paused") player.paused=true;
  if (scenario=="non-gui") {
    control.RenderEx();
    if (player.guiCalls!=0 || player.nonGuiCalls!=1 || control.baseRenderEx!=1) return 1;
  } else {
    control.Render();
    const int expected=player.video ? 1 : 0;
    if (player.guiCalls!=expected) {
      std::fprintf(stderr,"Expected %d GUI overlay render, got %d\n",expected,player.guiCalls);
      return 1;
    }
    if (player.nonGuiCalls!=0 || control.baseRender!=1 || graphics.transforms!=0) return 1;
    if (player.video && player.renderTransform!=1) return 1;
    if (graphics.clears!=(player.video && player.surface ? 1 : 0)) return 1;
    if (powerHandling.resets!=(player.video && !player.paused ? 1 : 0)) return 1;
  }
  return 0;
}
'''


class VideoControlRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("c++")
        if not compiler:
            raise RuntimeError("A C++17 host compiler is required")
        source = (ROOT / "xbmc/guilib/GUIVideoControl.cpp").read_text()
        start = source.index("void CGUIVideoControl::Render()")
        end = source.index("EVENT_RESULT CGUIVideoControl::OnMouseEvent", start)
        cls.tmp = tempfile.TemporaryDirectory(prefix="kodimega-video-control-")
        harness = pathlib.Path(cls.tmp.name) / "render.cpp"
        cls.binary = pathlib.Path(cls.tmp.name) / "render-test"
        harness.write_text(STUBS + source[start:end] + MAIN)
        subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        str(harness), "-o", str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def check_scenario(self, scenario):
        result = subprocess.run([str(self.binary), scenario], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_surface_video_renders_gui_overlays(self):
        self.check_scenario("surface")

    def test_texture_video_keeps_one_gui_render(self):
        self.check_scenario("texture")

    def test_idle_control_does_not_render_player(self):
        self.check_scenario("idle")

    def test_paused_surface_video_keeps_overlays_without_resetting_screensaver(self):
        self.check_scenario("paused")

    def test_render_ex_remains_non_gui(self):
        self.check_scenario("non-gui")


if __name__ == "__main__":
    unittest.main()
