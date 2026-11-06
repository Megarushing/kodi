#!/usr/bin/env python3
"""Host regression tests for xbmc/video/guilib/VideoNextUnwatched.h.

Run: python3 tools/mega/tests/test_video_next_unwatched.py -v
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
MAIN = r'''
#include "video/guilib/VideoNextUnwatched.h"
#include <cstdio>
#include <cstdlib>
#include <string>
using namespace KODI::VIDEO::GUILIB;
// argv: one "season,episode,watched,started" per entry; prints the chosen index.
int main(int argc, char** argv) {
  std::vector<NextUnwatchedEntry> e;
  for (int i = 1; i < argc; ++i) {
    int s, ep, w, st;
    std::sscanf(argv[i], "%d,%d,%d,%d", &s, &ep, &w, &st);
    e.push_back({i - 1, s, ep, w != 0, st != 0});
  }
  std::printf("%d\n", GetNextUnwatchedIndex(e));
}
'''


class NextUnwatchedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        src = pathlib.Path(cls.tmp, 'main.cpp')
        src.write_text(MAIN)
        cls.exe = str(pathlib.Path(cls.tmp, 'next'))
        subprocess.run(['g++', '-std=c++17', '-I', str(ROOT / 'xbmc'), str(src), '-o', cls.exe],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def pick(self, *entries):
        out = subprocess.run([self.exe, *entries], check=True, capture_output=True, text=True)
        return int(out.stdout)

    def test_nothing_watched_picks_first(self):
        self.assertEqual(self.pick('1,0,0,0', '2,0,0,0'), 0)

    def test_seasons_one_and_two_done_picks_three(self):
        self.assertEqual(self.pick('1,0,1,0', '2,0,1,0', '3,0,0,0', '4,0,0,0'), 2)

    def test_started_season_after_unwatched_ones(self):
        # S1 4/11, S2..S5 partly, S6 1/13, S7 untouched: resume at S6, not S1.
        self.assertEqual(self.pick('1,0,0,1', '2,0,0,1', '3,0,0,1', '4,0,0,1', '5,0,0,1',
                                   '6,0,0,1', '7,0,0,0'), 5)

    def test_last_season_watched_falls_back_to_first_unwatched(self):
        self.assertEqual(self.pick('1,0,0,0', '2,0,0,0', '3,0,1,0'), 0)

    def test_episode_after_last_watched_skips_earlier_gap(self):
        # 1-3 and 5 watched, 4 skipped: continue at 6.
        self.assertEqual(self.pick('1,1,1,0', '1,2,1,0', '1,3,1,0', '1,4,0,0', '1,5,1,0',
                                   '1,6,0,0'), 5)

    def test_all_watched(self):
        self.assertEqual(self.pick('1,1,1,0', '1,2,1,0'), -1)

    def test_list_order_does_not_matter(self):
        self.assertEqual(self.pick('3,0,0,0', '1,0,1,0', '2,0,0,0'), 2)


if __name__ == '__main__':
    unittest.main()
