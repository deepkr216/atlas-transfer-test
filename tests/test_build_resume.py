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

    def test_progress_line_carries_the_rate_and_time_left(self):
        with mock.patch.object(build.time, "time", side_effect=[1000.0 + 20 * k for k in range(4000)]):
            rc, out = self._build(extra=["--rebuild"])
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"parsed in .*?, \d+\.\d/s, about .* left at this rate - now ")


if __name__ == "__main__":
    unittest.main()
