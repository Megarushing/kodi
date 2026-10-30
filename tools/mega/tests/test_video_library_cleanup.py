#!/usr/bin/env python3
"""Execute Kodi's actual cleanup candidate SQL and C++ path-decision loop.

SQLite fixtures exercise show/link deletion. Filesystem and scraper collaborators
are controlled host doubles; this does not replace a real SMB/device scan test.
Run: python3 tools/mega/tests/test_video_library_cleanup.py -v
"""
import json
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
STUBS = r'''
#include <cstdlib>
#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <vector>
struct Field {
  std::string value;
  int get_asInt() const { return std::atoi(value.c_str()); }
  bool get_asBool() const { return get_asInt() != 0; }
  std::string get_asString() const { return value; }
};
struct Dataset {
  std::vector<std::vector<Field>> rows;
  size_t current = 0;
  bool eof() const { return current >= rows.size(); }
  Field fv(size_t i) const { return rows.at(current).at(i); }
  void next() { ++current; }
  void close() {}
};
static bool folderExists = false;
static bool sourceExists = true;
static bool hasSource = true;
static bool pluginPath = false;
struct SScanSettings {};
struct Scraper { int Content() const { return 0; } };
using ScraperPtr = std::shared_ptr<Scraper>;
static ScraperPtr GetScraperForPath(const std::string&, SScanSettings&, bool&) { return {}; }
static int TranslateContent(int content) { return content; }
struct URIUtils { static bool IsPlugin(const std::string&) { return pluginPath; } };
struct CPluginDirectory {
  static bool CheckExists(int, const std::string&) { return folderExists; }
};
struct CDirectory {
  static bool Exists(const std::string& path, bool) {
    return path == "source/" ? sourceExists : folderExists;
  }
};
// Piers: GetSourcePath(path, source) reports whether a source is configured; GetPathId
// looks up a delete decision for that source (none in these fixtures).
static bool GetSourcePath(const std::string&, std::string& source) {
  source = hasSource ? "source/" : "";
  return hasSource;
}
static int GetPathId(const std::string&) { return 1; }
int main(int argc, char** argv) {
  if (argc != 8) return 2;
  folderExists = std::atoi(argv[1]);
  sourceExists = std::atoi(argv[2]);
  hasSource = std::atoi(argv[3]);
  pluginPath = std::atoi(argv[4]);
  std::map<int, bool> pathsDeleteDecisions;
  if (std::string(argv[5]) != "none") pathsDeleteDecisions[145] = std::atoi(argv[5]);
  if (std::string(argv[6]) != "none") pathsDeleteDecisions[92] = std::atoi(argv[6]);
  Dataset dataset;
  dataset.rows.push_back({{"145"}, {"show/"}, {"92"}, {argv[7]}});
  Dataset* m_pDS2 = &dataset;
'''


class VideoLibraryCleanupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "xbmc/video/VideoDatabase.cpp").read_text()
        start = source.index('"Cleaning paths that don\'t exist and have content set..."')
        end = source.index('      m_pDS2->close();', start) + len('      m_pDS2->close();')
        block = source[start:end]
        # Piers interleaves // comments between the SQL string pieces
        uncommented = re.sub(r'^\s*//.*\n', '', block, flags=re.M)
        assignment = re.search(r'sql = ((?:"(?:\\.|[^"\\])*"\s*)+);', uncommented)
        assert assignment is not None, "Cleanup candidate SQL not found"
        cls.candidate_sql = ''.join(json.loads(s) for s in re.findall(r'"(?:\\.|[^"\\])*"', assignment[1]))
        a = block.index('      std::string strIds;')
        b = block.index('      m_pDS2->close();', a) + len('      m_pDS2->close();')
        cls.tmp = tempfile.TemporaryDirectory(prefix="kodimega-library-cleanup-")
        harness = pathlib.Path(cls.tmp.name) / "cleanup.cpp"
        cls.binary = pathlib.Path(cls.tmp.name) / "cleanup-test"
        harness.write_text(STUBS + block[a:b] + '\nstd::cout << strIds;\n}\n')
        compiler = shutil.which("c++")
        if not compiler:
            raise RuntimeError("A C++17 host compiler is required")
        subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        str(harness), "-o", str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def clean(self, *, folder=False, source=True, configured=True, plugin=False,
              hash_value="", linked=True, path_decision: str | int="none",
              parent_decision: str | int="none"):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE path(idPath INTEGER, strPath TEXT, idParentPath INTEGER,
                              strContent TEXT, strSettings TEXT, strHash TEXT, exclude INTEGER);
            CREATE TABLE tvshow(idShow INTEGER);
            CREATE TABLE tvshowlinkpath(idShow INTEGER, idPath INTEGER);
            CREATE TABLE files(idFile INTEGER, idPath INTEGER);
            CREATE TABLE episode(idEpisode INTEGER, idShow INTEGER);
            INSERT INTO tvshow VALUES(10);
        """)
        db.execute("INSERT INTO path VALUES(145,'show/',92,NULL,NULL,?,NULL)", (hash_value,))
        if linked:
            db.execute("INSERT INTO tvshowlinkpath VALUES(10,145)")
        candidates = db.execute(self.candidate_sql).fetchall()
        deleted = []
        for row in candidates:
            result = subprocess.run([str(self.binary), str(int(folder)), str(int(source)),
                                     str(int(configured)), str(int(plugin)), str(path_decision),
                                     str(parent_decision), str(row[3] if len(row) > 3 else 0)],
                                    capture_output=True, text=True, check=True)
            deleted.extend(int(v) for v in result.stdout.split(',') if v)
        for path_id in deleted:
            db.execute("DELETE FROM path WHERE idPath=?", (path_id,))
        # Same linked-path and orphan-show predicates as CleanDatabase.
        db.execute("DELETE FROM tvshowlinkpath WHERE NOT EXISTS "
                   "(SELECT 1 FROM path WHERE path.idPath=tvshowlinkpath.idPath)")
        db.execute("DELETE FROM tvshow WHERE NOT EXISTS "
                   "(SELECT 1 FROM tvshowlinkpath WHERE tvshowlinkpath.idShow=tvshow.idShow)")
        return db.execute("SELECT count(*) FROM tvshow").fetchone()[0], deleted

    def test_deleted_empty_hash_show_is_removed(self):
        self.assertEqual(self.clean(), (0, [145]))

    def test_existing_empty_show_is_preserved(self):
        self.assertEqual(self.clean(folder=True), (1, []))

    def test_unavailable_source_preserves_show(self):
        self.assertEqual(self.clean(source=False), (1, []))

    def test_unknown_source_removes_show(self):
        # Piers: a missing folder outside any configured source is gone (upstream policy, kept).
        self.assertEqual(self.clean(configured=False), (0, [145]))

    def test_offline_hashed_show_is_also_preserved(self):
        self.assertEqual(self.clean(source=False, hash_value="old-hash"), (1, []))

    def test_existing_hashed_show_is_preserved(self):
        self.assertEqual(self.clean(folder=True, hash_value="old-hash"), (1, []))

    def test_parent_keep_decision_is_respected(self):
        self.assertEqual(self.clean(parent_decision=0), (1, []))

    def test_path_keep_decision_is_respected(self):
        self.assertEqual(self.clean(path_decision=0), (1, []))

    def test_unlinked_empty_path_is_not_newly_selected(self):
        self.assertEqual(self.clean(linked=False), (0, []))


if __name__ == "__main__":
    unittest.main()
