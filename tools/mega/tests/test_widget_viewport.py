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
  assert(ShouldUpdateWidgetRow(0, 0));
  assert(ShouldUpdateWidgetRow(3, 0));
  assert(!ShouldUpdateWidgetRow(4, 0));
  assert(ShouldUpdateWidgetRow(4, 1));
  assert(ShouldUpdateWidgetRow(9, 8));
  assert(ShouldUpdateWidgetRow(7, 8));
  assert(!ShouldUpdateWidgetRow(3, 8));
}
'''
            result = subprocess.run(['c++', '-std=c++17', '-I', str(SOURCE), '-x', 'c++',
                                     '-', '-o', str(binary)], input=source, text=True,
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()
