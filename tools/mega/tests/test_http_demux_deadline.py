#!/usr/bin/env python3
"""Compile production deadline policy (manual host test, no FFmpeg/devices).
Before helper introduction, compile the actual baseline Set(20s) expression.
"""
import os
import pathlib
import re
import subprocess
import tempfile
import unittest
ROOT = pathlib.Path(__file__).resolve().parents[3]
DEMUX = ROOT / "xbmc/cores/VideoPlayer/DVDDemuxers"
class HttpDemuxDeadline(unittest.TestCase):
    def test_compiled_deadline_policy(self):
        header = DEMUX / "DemuxReadDeadline.h"
        if header.exists():
            policy = '#include "DemuxReadDeadline.h"\n'
        else:
            source = (DEMUX / "DVDDemuxFFmpeg.cpp").read_text()
            old = re.search(r"m_timeout.Set\((\d+s)\);\s*m_pkt.result = av_read_frame", source)
            assert old is not None, "Cannot locate production baseline"
            policy = "#include <chrono>\nusing namespace std::chrono_literals;\nconstexpr auto DemuxReadDeadline(bool, int) { return " + old[1] + "; }\n"
        program = policy + r'''
#include <cstdio>
int main() {
  struct Case { bool http; int configured; int expected; };
  const Case cases[] = {{true,600,600}, {true,20,20}, {true,1,20},
                        {true,1000,1000}, {false,600,20}, {false,20,20}};
  for (const auto& c : cases) {
    const auto actual = DemuxReadDeadline(c.http,c.configured).count();
    if (actual != c.expected) {
      std::fprintf(stderr,"http=%d configured=%d expected=%d actual=%lld\n",
                   c.http,c.configured,c.expected,static_cast<long long>(actual));
      return 1;
    }
  }
  std::puts("PASS: HTTP 600/default/floor/max and non-HTTP unchanged");
}
'''
        with tempfile.TemporaryDirectory(prefix="kodi-demux-", dir=os.environ.get("TMPDIR")) as td:
            src = pathlib.Path(td)/"policy.cpp"
            exe = pathlib.Path(td)/"policy"
            src.write_text(program)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-I",str(DEMUX),str(src),"-o",str(exe)],check=True)
            result = subprocess.run([str(exe)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            print(result.stdout.strip())
if __name__ == "__main__":
    unittest.main()
