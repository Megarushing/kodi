#!/usr/bin/env python3
"""Check the Kodi widget viewport gate independently of the Android GUI."""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SOURCE = ROOT / 'xbmc/guilib'


class WidgetViewportTest(unittest.TestCase):
    def test_first_four_rows_get_a_bounded_head_start(self):
        with tempfile.TemporaryDirectory() as d:
            binary = pathlib.Path(d) / 'viewport'
            source = r'''
#include <cassert>
#include "WidgetViewport.h"
using KODI::GUI::WidgetHeadStartMs;
int main() {
  assert(WidgetHeadStartMs(0) == 0);
  assert(WidgetHeadStartMs(3) == 0);
  assert(WidgetHeadStartMs(4) == 3000);
  assert(WidgetHeadStartMs(10) == 3000);
}
'''
            result = subprocess.run(['c++', '-std=c++17', '-I', str(SOURCE), '-x', 'c++',
                                     '-', '-o', str(binary)], input=source, text=True,
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()
