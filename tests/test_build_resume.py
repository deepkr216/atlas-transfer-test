"""
A 121,000-member estate takes hours (LESSONS 138): every progress line
carries the time left at the current rate; Ctrl+C stops cleanly and the
same command continues with what was parsed kept; a `git pull` that only
changes the query / gate / UI side does not force a re-parse - only the
modules that decide what a member's facts are do.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


class Fingerprint(unittest.TestCase):

    def test_only_fact_modules_count(self):
        td = tempfile.mkdtemp()
        try:
            src = os.path.dirname(build.__file__)
            for fn in os.listdir(src):
                if fn.endswith((".py", ".sql")):
                    shutil.copy(os.path.join(src, fn), td)
            base = build.tool_fingerprint(td)
            self.assertEqual(base, build.tool_fingerprint())            # a faithful copy fingerprints the same
            with open(os.path.join(td, "query.py"), "a", encoding="utf-8") as fh:
                fh.write("\n# a query-side change\n")
            with open(os.path.join(td, "ui.py"), "a", encoding="utf-8") as fh:
                fh.write("\n# a UI change\n")
            self.assertEqual(build.tool_fingerprint(td), base, "query / UI changes must not force a re-parse")
            with open(os.path.join(td, "cobol.py"), "a", encoding="utf-8") as fh:
                fh.write("\n# a parser change\n")
            self.assertNotEqual(build.tool_fingerprint(td), base, "a parser change must force a re-parse")
        finally:
            shutil.rmtree(td, ignore_errors=True)
        for fn in build.FACT_MODULES:
            self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(build.__file__), fn)), fn)

    def test_hms(self):
        self.assertEqual(build._hms(42), "42 s")
        self.assertEqual(build._hms(600), "10 min")
        self.assertEqual(build._hms(5 * 3600 + 7 * 60), "5 h 07 min")


class Resume(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate", "SRC")
        os.makedirs(self.root)
        for fn in ("SAMPPGM.cbl", "WALKPGM.cbl", "ERRPGM.cbl", "MULTILN.cbl", "PMASTREC.cpy", "WALKREC.cpy", "WALKPROC.cpy"):
            shutil.copy(os.path.join(FIX, fn), self.root)
        self.db = os.path.join(self.td, "t.db")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _build(self, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = build._main([os.path.join(self.td, "estate"), "--db", self.db, *kw.get("extra", [])])
        return rc, buf.getvalue()

    def test_ctrl_c_stops_cleanly_and_the_next_run_continues(self):
        real = build.HANDLERS["cobol"]
        calls = {"n": 0}

        def flaky(ctx, mem):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return real(ctx, mem)

        with mock.patch.dict(build.HANDLERS, {"cobol": flaky}):
            rc, out = self._build(extra=["--rebuild"])
        self.assertEqual(rc, 130)
        self.assertIn("stopped by Ctrl+C at", out)
        self.assertIn("Run the same build command again (without --rebuild) to continue", out)
        conn = query.connect(self.db)
        st = {r[0]: r[1] for r in conn.execute("SELECT name, parse_status FROM member")}
        conn.close()
        programs = ("SAMPPGM", "WALKPGM", "ERRPGM", "MULTILN")
        self.assertEqual(sum(1 for k in programs if st.get(k) in ("ok", "partial")), 1, "one program parsed before the interrupt")
        self.assertGreaterEqual(sum(1 for v in st.values() if v == "pending"), 3, st)
        conn = query.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM program").fetchone()[0], 1, "its facts were committed")
        conn.close()
        # the same command again: parsed members kept, the rest parsed, run finished
        rc, out = self._build()
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"parsing \d+ member\(s\) \([1-9]\d* unchanged, kept\)")
        self.assertNotIn("toolkit changed since the last build", out)
        conn = query.connect(self.db)
        st = {r[0]: r[1] for r in conn.execute("SELECT name, parse_status FROM member")}
        self.assertTrue(all(v in ("ok", "partial", "skipped") for v in st.values()), st)   # nothing left pending
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM program").fetchone()[0], 4)
        self.assertIsNotNone(conn.execute("SELECT finished_at FROM build_run ORDER BY id DESC LIMIT 1").fetchone()[0])
        conn.close()

    def test_a_toolkit_change_after_an_interrupted_run_redoes_the_parsed_members(self):
        # 4,000 members parsed by the OLD toolkit, Ctrl+C, git pull, build again:
        # the unfinished run's fingerprint must count, or stale facts are kept
        real = build.HANDLERS["cobol"]
        calls = {"n": 0}

        def flaky(ctx, mem):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return real(ctx, mem)

        with mock.patch.dict(build.HANDLERS, {"cobol": flaky}):
            rc, _out = self._build(extra=["--rebuild"])
        self.assertEqual(rc, 130)
        with mock.patch.object(build, "tool_fingerprint", return_value="feedfacefeedface"):
            rc, out = self._build()
        self.assertEqual(rc, 0)
        self.assertIn("toolkit changed since the last build: every member is re-parsed", out)
        self.assertRegex(out, r"parsing \d+ member\(s\) \(0 unchanged, kept\)")

    def test_a_member_over_the_time_limit_is_skipped_and_not_retried(self):
        import time as _t
        real = build.HANDLERS["cobol"]

        def slow(ctx, mem):
            if mem.name == "WALKPGM":
                while True:                       # a parser loop that never ends (interruptible between statements)
                    _t.sleep(0.01)
            return real(ctx, mem)

        with mock.patch.dict(build.HANDLERS, {"cobol": slow}):
            rc, out = self._build(extra=["--rebuild", "--member-limit", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("FAILED cobol", out)
        self.assertIn("MemberTimeout: exceeded the 1 s member limit (cobol WALKPGM.cbl)", out)
        conn = query.connect(self.db)
        st = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT name, parse_status, parse_error FROM member")}
        self.assertEqual(st["WALKPGM"][0], "failed")
        self.assertTrue(st["WALKPGM"][1].startswith("MemberTimeout"))
        self.assertEqual(st["SAMPPGM"][0], "ok")                         # the build went on
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM program WHERE program_id='WALKPGM'").fetchone()[0], 0,
                         "no half-written program row")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM program").fetchone()[0], 3)
        conn.close()
        # the next run does not sit on it again for another limit
        with mock.patch.dict(build.HANDLERS, {"cobol": slow}):
            t0 = _t.time()
            rc, out = self._build(extra=["--member-limit", "5"])
        self.assertEqual(rc, 0)
        self.assertLess(_t.time() - t0, 4, "the timed-out member must be kept, not parsed again")
        self.assertNotIn("FAILED cobol", out)

    def test_a_member_that_cannot_be_interrupted_stops_the_build_with_its_name(self):
        import time as _t
        real = build.HANDLERS["cobol"]

        def stuck(ctx, mem):
            if mem.name == "WALKPGM":
                _t.sleep(3)                       # C code: the interruption lands only when it returns
                return
            return real(ctx, mem)

        with mock.patch.dict(build.HANDLERS, {"cobol": stuck}), mock.patch.object(build, "STUCK_GRACE", 0.3):
            rc, out = self._build(extra=["--rebuild", "--member-limit", "0.5"])
        self.assertEqual(rc, 3)
        self.assertIn("STOPPED: cobol WALKPGM.cbl has been parsing for", out)
        self.assertIn("cannot be interrupted", out)
        self.assertIn("run the same build command again", out)
        _t.sleep(3.5)                             # let the worker finish before the temp dir goes

    def test_inventory_gives_up_on_a_file_that_will_not_classify(self):
        import time as _t
        real = build.classify.classify

        def slow(path, head, ext_hint=None):
            if path.endswith("STUCK.txt"):
                while True:
                    _t.sleep(0.01)
            return real(path, head, ext_hint)

        with open(os.path.join(self.root, "STUCK.txt"), "w") as fh:
            fh.write("just text\n")
        with mock.patch.object(build.classify, "classify", slow), mock.patch.object(build, "INVENTORY_TIME_LIMIT", 1):
            rc, out = self._build(extra=["--rebuild"])
        self.assertEqual(rc, 0)
        self.assertIn("gave up on file STUCK.txt", out)
        conn = query.connect(self.db)
        st, err = conn.execute("SELECT parse_status, parse_error FROM member WHERE name='STUCK'").fetchone()
        self.assertEqual(st, "failed")
        self.assertTrue(err.startswith("InventoryTimeout"), err)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM program").fetchone()[0], 4)       # the rest went on
        conn.close()
        # the next run does not read it again for another limit: same bytes,
        # same parser, the stored outcome stands (and so does every other
        # unchanged file's classification - the inventory of an unchanged
        # estate is a hash per file, nothing more)
        with mock.patch.object(build.classify, "classify", slow), mock.patch.object(build, "INVENTORY_TIME_LIMIT", 5):
            t0 = _t.time()
            rc, out = self._build()
        self.assertEqual(rc, 0)
        self.assertLess(_t.time() - t0, 4)
        self.assertNotIn("gave up on", out)
        conn = query.connect(self.db)
        st, err = conn.execute("SELECT parse_status, parse_error FROM member WHERE name='STUCK'").fetchone()
        self.assertEqual((st, err[:16]), ("failed", "InventoryTimeout"))
        conn.close()
        # a parser change (new fingerprint) reads it again
        with mock.patch.object(build.classify, "classify", slow), mock.patch.object(build, "INVENTORY_TIME_LIMIT", 1), \
                mock.patch.object(build, "tool_fingerprint", return_value="feedfacefeedface"):
            rc, out = self._build()
        self.assertEqual(rc, 0)
        self.assertIn("gave up on file STUCK.txt", out)

    def test_inventory_names_a_file_that_cannot_be_interrupted(self):
        import time as _t
        real = build.classify.classify

        def stuck(path, head, ext_hint=None):
            if path.endswith("STUCK.txt"):
                _t.sleep(3)
            return real(path, head, ext_hint)

        with open(os.path.join(self.root, "STUCK.txt"), "w") as fh:
            fh.write("just text\n")
        with mock.patch.object(build.classify, "classify", stuck), mock.patch.object(build, "INVENTORY_TIME_LIMIT", 0.5), \
                mock.patch.object(build, "STUCK_GRACE", 0.3):
            rc, out = self._build(extra=["--rebuild"])
        self.assertEqual(rc, 3)
        self.assertIn("STOPPED: file STUCK.txt", out)
        self.assertIn("cannot be interrupted", out)
        _t.sleep(3.5)

    def test_a_huge_text_file_is_a_document_not_a_member(self):
        p = os.path.join(self.root, "EXPORT.txt")
        with open(p, "w") as fh:
            fh.write("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. FAKE.\n" + ("x" * 70 + "\n") * 40)
        with mock.patch.object(build, "BIG_TEXT_BYTES", 1000):
            rc, out = self._build(extra=["--rebuild"])
        self.assertEqual(rc, 0)
        conn = query.connect(self.db)
        kind, lines = conn.execute("SELECT kind, lines FROM member WHERE name='EXPORT'").fetchone()
        self.assertEqual(kind, "doc")
        self.assertEqual(lines, 42)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM program WHERE program_id='FAKE'").fetchone()[0], 0)
        conn.close()

    def test_progress_line_carries_the_rate_and_time_left(self):
        with mock.patch.object(build.time, "time", side_effect=[1000.0 + 20 * k for k in range(4000)]):
            rc, out = self._build(extra=["--rebuild"])
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"parsed in .*?, \d+\.\d/s, about .* left at this rate - now ")


if __name__ == "__main__":
    unittest.main()
