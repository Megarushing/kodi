#!/usr/bin/env python3
"""Check the Kodi widget viewport gate independently of the Android GUI."""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SOURCE = ROOT / 'xbmc/guilib'


class WidgetViewportTest(unittest.TestCase):
    def test_defer_distant_rows_and_resume_as_viewport_approaches(self):
        with tempfile.TemporaryDirectory() as d:
            binary = pathlib.Path(d) / 'viewport'
            source = r'''
#include <cassert>
#include "WidgetViewport.h"
using KODI::GUI::ShouldUpdateWidgetRow;
int main() {
  assert(ShouldUpdateWidgetRow(580, 550, 0, 2100, false));
  assert(ShouldUpdateWidgetRow(2700, 550, 0, 2100, false));
  assert(!ShouldUpdateWidgetRow(3300, 550, 0, 2100, false));
  assert(ShouldUpdateWidgetRow(3300, 550, 800, 2100, false));
  assert(ShouldUpdateWidgetRow(7000, 550, 0, 2100, true));
  assert(!ShouldUpdateWidgetRow(580, 550, 3000, 2100, false));
}
'''
            result = subprocess.run(['c++', '-std=c++17', '-I', str(SOURCE), '-x', 'c++',
                                     '-', '-o', str(binary)], input=source, text=True,
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()
