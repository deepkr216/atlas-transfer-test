r"""
Every step of the build says what it is doing (LESSONS 147): the steps
between the members - removing the old facts after a `git pull`, recording,
the manifest, the post passes - ran silently, and the removal was quadratic
through 44 foreign keys without an index, so a 121,000-member build looked
hung on the last control card read. Now each step is announced and carries
the status line, a folder is reported when it is done, each kind of member
is reported when it is done, the member in hand shows its size, the old
facts go in bulk through indexed keys, and a stop during the inventory
leaves the toolkit change still pending.
"""

import contextlib
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query, supervise  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


class Phases(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.estate = os.path.join(self.td, "estate")
        self.src = os.path.join(self.estate, "GC", "PROD.GC.SRC")
        self.cpy = os.path.join(self.estate, "GC", "PROD.GC.CPY")
        self.jcl = os.path.join(self.estate, "SHARED", "PROD.JCLLIB")
        for d in (self.src, self.cpy, self.jcl):
            os.makedirs(d)
        for fn in ("SAMPPGM.cbl", "WALKPGM.cbl", "ERRPGM.cbl", "MEMBRVAL.cbl"):
            shutil.copy(os.path.join(FIX, fn), self.src)
        for fn in ("PMASTREC.cpy", "WALKREC.cpy", "WALKPROC.cpy"):
            shutil.copy(os.path.join(FIX, fn), self.cpy)
        for fn in ("SAMPJOB.jcl", "NIGHTJOB.jcl"):
            shutil.copy(os.path.join(FIX, fn), self.jcl)
        self.db = os.path.join(self.td, "t.db")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _build(self, *extra):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = build._main([self.estate, "--db", self.db, *extra])
        return rc, buf.getvalue()

    def test_every_step_is_announced_in_order(self):
        with mock.patch.object(build, "FOLDER_REPORT_FILES", 1):
            rc, out = self._build("--rebuild")
        self.assertEqual(rc, 0, out)
        steps = ["== opening the index", "== inventory: reading and fingerprinting every file", "inventory done:",
                 "== comparing with the last build", "== recording ", "== manifest and systems", "== parsing ",
                 "starting with copybooks (3)", "copybooks done: 3 in", "; next: programs (4)", "programs done: 4 in",
                 "parsing done:", "== post: DD directions from OPEN verbs", "== post: DL/I calls mapped",
                 "== summary", "== finished"]
        pos = 0
        for step in steps:
            k = out.find(step, pos)
            self.assertGreaterEqual(k, 0, f"{step!r} missing or out of order after position {pos}:\n{out}")
            pos = k
        self.assertRegex(out, r"\d\d:\d\d:\d\d == parsing ")
        # a folder is reported when it is done, with its path under the estate, count and size
        self.assertIn(f"folder done: {os.path.join('estate', 'GC', 'PROD.GC.CPY')} - 3 file(s),", out)
        self.assertIn(f"folder done: {os.path.join('estate', 'GC', 'PROD.GC.SRC')} - 4 file(s),", out)
        self.assertIn(f"folder done: {os.path.join('estate', 'SHARED', 'PROD.JCLLIB')} - 2 file(s),", out)
        # the phase file says where the build is, for the supervisor
        with open(os.path.join(self.td, build.CURRENT_FILE), encoding="utf-8") as fh:
            self.assertEqual(fh.read().splitlines(), ["PHASE", "finished"])

    def test_small_folders_are_counted_not_listed(self):
        rc, out = self._build("--rebuild")
        self.assertEqual(rc, 0)
        self.assertNotIn("folder done: ", out)                           # every folder here is under 5 files
        self.assertIn("and 3 small folder(s) with 9 file(s)", out)

    def test_the_member_in_hand_shows_its_size(self):
        real = build.HANDLERS["cobol"]

        def slow(ctx, mem):
            if mem.name == "WALKPGM":
                time.sleep(0.6)
            return real(ctx, mem)

        with mock.patch.dict(build.HANDLERS, {"cobol": slow}), mock.patch.object(build, "PROGRESS_SECONDS", 0.1):
            rc, out = self._build("--rebuild")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"\.\.\. \d+/\d+ - \d+/\d+ parsed in .* - now cobol WALKPGM\.cbl \(\d+ (B|KB)\)")

    def test_the_removal_step_is_announced_and_keeps_the_status_line(self):
        rc, _out = self._build("--rebuild")
        self.assertEqual(rc, 0)
        real = build._forget_members

        def slow_forget(conn, mids, progress=None):
            time.sleep(0.5)
            return real(conn, mids, progress)

        with mock.patch.object(build, "tool_fingerprint", return_value="feedfacefeedface"), \
                mock.patch.object(build, "_forget_members", slow_forget), mock.patch.object(build, "PROGRESS_SECONDS", 0.1):
            rc, out = self._build()
        self.assertEqual(rc, 0, out)
        self.assertIn("toolkit changed since the last build", out)
        self.assertIn("0 member(s) unchanged and kept, 9 to redo", out)
        self.assertIn("== removing the old facts of 9 member(s)", out)
        self.assertRegex(out, r"\.\.\. removing the old facts of 9 member\(s\) for \d+ s")

    def test_bulk_removal_leaves_no_orphans_and_matches_a_rebuild(self):
        rc, _ = self._build("--rebuild")
        self.assertEqual(rc, 0)
        with mock.patch.object(build, "tool_fingerprint", return_value="feedfacefeedface"), \
                mock.patch.object(build, "BULK_FORGET", 0):
            rc, out = self._build()
        self.assertEqual(rc, 0, out)
        conn = sqlite3.connect(self.db)
        try:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                                 "AND name NOT LIKE '%fts%'")]
            for t in tables:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{t}')")}
                if "member_id" in cols and t != "member":
                    n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE member_id IS NOT NULL AND member_id NOT IN (SELECT id FROM member)").fetchone()[0]
                    self.assertEqual(n, 0, f"{n} orphan row(s) in {t}")
                if "program_id" in cols and t != "program":
                    n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE program_id IS NOT NULL AND program_id NOT IN (SELECT id FROM program)").fetchone()[0]
                    self.assertEqual(n, 0, f"{n} orphan row(s) in {t}")
            n = conn.execute("SELECT COUNT(*) FROM src_fts WHERE member_id NOT IN (SELECT id FROM member)").fetchone()[0]
            self.assertEqual(n, 0, "orphan search rows")
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("member", "program", "paragraph", "field", "copy_use", "call_edge", "job", "step", "dd", "src_fts")}
        finally:
            conn.close()
        fresh = os.path.join(self.td, "fresh.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([self.estate, "--db", fresh, "--rebuild"])
        conn = sqlite3.connect(fresh)
        try:
            ref = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts}
        finally:
            conn.close()
        self.assertEqual(counts, ref)

    def test_bulk_removal_is_a_fixed_number_of_statements(self):
        rc, _ = self._build("--rebuild")
        self.assertEqual(rc, 0)
        conn = build.open_db(self.db)
        try:
            mids = [r[0] for r in conn.execute("SELECT id FROM member")]
            seen = []
            conn.set_trace_callback(seen.append)
            with mock.patch.object(build, "BULK_FORGET", 0):
                build._forget_members(conn, mids)
            conn.set_trace_callback(None)
            conn.commit()
            # distinct top-level statements only: SQLite traces its own FTS bookkeeping with a "--" prefix
            # and repeats the parent statement's text for each cascade it fires; the ids go in through
            # one executemany. One member at a time this was 5 statements PER member.
            top = {s for s in seen if not s.startswith(("--", "BEGIN", "COMMIT", "INSERT OR IGNORE INTO forget_ids",
                                                        "DELETE FROM src_fts WHERE rowid BETWEEN"))}
            self.assertLessEqual(len(top), 10, top)
            # the search rows go by rowid range (an index read), never by a scan on member_id
            self.assertFalse([s for s in seen if "FROM src_fts WHERE member_id" in s], seen)
            self.assertTrue([s for s in seen if s.startswith("DELETE FROM src_fts WHERE rowid BETWEEN")])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM member").fetchone()[0], 0)
        finally:
            conn.close()

    def test_every_foreign_key_has_an_index(self):
        conn = build.open_db(self.db, rebuild=True)
        try:
            build.ensure_fk_indexes(conn)
            self.assertEqual(build.ensure_fk_indexes(conn), 0, "a second call finds nothing missing")
            conn.execute("DROP INDEX ix_fk_program_member_id")
            self.assertEqual(build.ensure_fk_indexes(conn), 1, "an index built by an older toolkit gets it back")
        finally:
            conn.close()

    def test_a_stop_during_the_inventory_keeps_the_toolkit_change_pending(self):
        rc, _ = self._build("--rebuild")
        self.assertEqual(rc, 0)

        def interrupted(*a, **k):
            raise KeyboardInterrupt

        with mock.patch.object(build, "tool_fingerprint", return_value="feedfacefeedface"), \
                mock.patch.object(build, "_forget_members", interrupted):
            rc, out = self._build()
        self.assertEqual(rc, 130)
        self.assertIn("stopped by Ctrl+C during the inventory - nothing was changed", out)
        with mock.patch.object(build, "tool_fingerprint", return_value="feedfacefeedface"):
            rc, out = self._build()
        self.assertEqual(rc, 0, out)
        self.assertIn("toolkit changed since the last build: every member is re-parsed", out)
        self.assertIn("0 member(s) unchanged and kept, 9 to redo", out)
        # and once done, the change is settled
        with mock.patch.object(build, "tool_fingerprint", return_value="feedfacefeedface"):
            rc, out = self._build()
        self.assertEqual(rc, 0)
        self.assertNotIn("toolkit changed", out)

    def test_a_file_that_froze_the_classifier_is_skipped_by_the_inventory(self):
        skip = os.path.join(self.td, "skip.txt")
        target = os.path.join(self.src, "WALKPGM.cbl")
        with open(skip, "w", encoding="utf-8") as fh:
            fh.write(f"{target}\t# classifying WALKPGM.cbl (2 KB) in PROD.GC.SRC: froze the parser\n")
        rc, out = self._build("--rebuild", "--skip-list", skip)
        self.assertEqual(rc, 0, out)
        self.assertIn("SKIPPED file WALKPGM.cbl", out)
        self.assertIn("froze the classifier on an earlier run (skip list)", out)
        conn = query.connect(self.db)
        try:
            st, err = conn.execute("SELECT parse_status, parse_error FROM member WHERE name='WALKPGM'").fetchone()
        finally:
            conn.close()
        self.assertEqual(st, "failed")
        self.assertTrue(err.startswith("InventoryTimeout"), err)


class SupervisorAndSteps(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate", "SRC")
        os.makedirs(self.root)
        for fn in ("SAMPPGM.cbl", "PMASTREC.cpy"):
            shutil.copy(os.path.join(FIX, fn), self.root)
        self.db = os.path.join(self.td, "t.db")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_a_step_that_goes_silent_is_restarted_once_and_never_put_on_the_skip_list(self):
        said = []
        with mock.patch.dict(os.environ, {"ATLAS_TEST_FREEZE": "PHASE:post"}):
            rc = supervise.run([os.path.join(self.td, "estate"), "--db", self.db, "--rebuild"], silence=6, say=said.append)
        text = "\n".join(said)
        self.assertEqual(rc, 3, text)
        self.assertIn("FROZEN - nothing printed for 6 s during the step 'post: DD directions from OPEN verbs'", text)
        self.assertIn("FROZEN twice in the same step (post: DD directions from OPEN verbs) - stopping", text)
        self.assertFalse(os.path.exists(os.path.join(self.td, "atlas-skip.txt")), "a step is not a member")


if __name__ == "__main__":
    unittest.main()
