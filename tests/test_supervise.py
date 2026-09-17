r"""
The build under an outside watchdog (LESSONS 145): a parser frozen inside
C code prints nothing and honours no limit; atlas.supervise kills the
silent build, records the member in hand in atlas-skip.txt, restarts, and
the restarted build records that member as failed and finishes. Every
status line carries the clock.
"""

import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query, supervise  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


class Supervise(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate", "SRC")
        os.makedirs(self.root)
        for fn in ("SAMPPGM.cbl", "WALKPGM.cbl", "ERRPGM.cbl", "PMASTREC.cpy", "WALKREC.cpy", "WALKPROC.cpy"):
            shutil.copy(os.path.join(FIX, fn), self.root)
        self.db = os.path.join(self.td, "t.db")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_db_of(self):
        self.assertEqual(supervise.db_of(["estate", "--db", "x.db"]), "x.db")
        self.assertEqual(supervise.db_of(["estate", "--db=y.db"]), "y.db")
        self.assertEqual(supervise.db_of(["estate"]), "atlas.db")

    def test_a_frozen_build_is_killed_recorded_and_finished_on_restart(self):
        said = []
        with mock.patch.dict(os.environ, {"ATLAS_TEST_FREEZE": "WALKPGM.cbl"}):
            rc = supervise.run([os.path.join(self.td, "estate"), "--db", self.db, "--rebuild", "--member-limit", "600"],
                               silence=6, say=said.append)
        text = "\n".join(said)
        self.assertEqual(rc, 0, text)
        self.assertIn("supervisor: FROZEN - nothing printed for 6 s while parsing cobol WALKPGM.cbl", text)
        self.assertIn("SKIPPED cobol WALKPGM.cbl: froze the parser on an earlier run (skip list)", text)
        self.assertIn("supervisor: build finished after 1 restart(s)", text)
        self.assertIn("(restart 1, without --rebuild)", text)
        skip = os.path.join(self.td, "atlas-skip.txt")
        with open(skip, encoding="utf-8") as fh:
            entry = fh.read()
        self.assertIn(os.path.join(self.root, "WALKPGM.cbl"), entry)
        self.assertIn("froze the parser", entry)
        conn = query.connect(self.db)
        st = {r[0]: (r[1], r[2] or "") for r in conn.execute("SELECT name, parse_status, parse_error FROM member")}
        self.assertEqual(st["WALKPGM"][0], "failed")
        self.assertTrue(st["WALKPGM"][1].startswith("ParserStuck"), st["WALKPGM"])
        self.assertEqual(st["SAMPPGM"][0], "ok")
        self.assertEqual(st["ERRPGM"][0], "partial")
        self.assertIsNotNone(conn.execute("SELECT finished_at FROM build_run ORDER BY id DESC LIMIT 1").fetchone()[0])
        # the restart kept the index: the frozen run is still recorded beside the one that finished. A restart
        # that passed --rebuild again would have deleted the db, and with it every hour already parsed.
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM build_run").fetchone()[0], 2,
                         "the restart must not delete the index")
        conn.close()
        # the clock is on every status and event line (the 10-s status itself never fires in a 1-s build)
        self.assertRegex(text, r"\d\d:\d\d:\d\d  SKIPPED cobol WALKPGM\.cbl")
        self.assertRegex(text, r"\d\d:\d\d:\d\d supervisor: FROZEN")
        # the current-member file names what was in hand
        self.assertTrue(os.path.isfile(os.path.join(self.td, "atlas-current.txt")))

    def test_a_restart_never_passes_rebuild_and_the_stop_messages_say_so(self):
        args = ["C:\\estate", "--db", "atlas.db", "--rebuild", "--also", "C:\\docs"]
        self.assertEqual(supervise.restart_args(args), ["C:\\estate", "--db", "atlas.db", "--also", "C:\\docs"])
        self.assertEqual(supervise.restart_args(["estate", "--db", "x.db"]), ["estate", "--db", "x.db"])
        self.assertIn("WITHOUT --rebuild", supervise.again_hint(args))
        self.assertEqual(supervise.again_hint(["estate"]), "run the same command again to continue")

    def test_skip_list_by_name_and_settled_on_the_next_run(self):
        skip = os.path.join(self.td, "skip.txt")
        with open(skip, "w", encoding="utf-8") as fh:
            fh.write("# members that froze\nWALKPGM.cbl\t# froze\n")
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = build._main([os.path.join(self.td, "estate"), "--db", self.db, "--rebuild", "--skip-list", skip])
        self.assertEqual(rc, 0)
        self.assertIn("skip list: 1 member(s)", buf.getvalue())
        self.assertIn("SKIPPED cobol WALKPGM.cbl", buf.getvalue())
        conn = query.connect(self.db)
        self.assertEqual(conn.execute("SELECT parse_status FROM member WHERE name='WALKPGM'").fetchone()[0], "failed")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM program WHERE program_id='WALKPGM'").fetchone()[0], 0)
        conn.close()
        # without the list, the same parser: still settled, not retried
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = build._main([os.path.join(self.td, "estate"), "--db", self.db])
        self.assertEqual(rc, 0)
        self.assertRegex(buf.getvalue(), r"== parsing 0 member\(s\)\n  \d+ unchanged and kept; to parse: nothing")

    def test_selfcheck_names_the_failing_test(self):
        sys.path.insert(0, os.path.dirname(HERE))
        import selfcheck
        text = ("..F.\n======================================================================\n"
                "FAIL: test_x (test_mod.Case.test_x)\n----------------------------------------------------------------------\n"
                "Traceback (most recent call last):\n  File \"C:\\a\\test_mod.py\", line 5, in test_x\n"
                "    self.assertLess(took, 2)\nAssertionError: 2.5 not less than 2 : 2.5 s\n\n"
                "ERROR: test_y (test_mod.Case.test_y)\nTraceback (most recent call last):\n"
                "  File \"C:\\a\\test_mod.py\", line 9, in test_y\nOSError: [WinError 5] Access is denied\n")
        out = selfcheck.failure_summary(text)
        self.assertEqual(out, ["FAIL: test_x (test_mod.Case.test_x)", "  AssertionError: 2.5 not less than 2 : 2.5 s",
                               "ERROR: test_y (test_mod.Case.test_y)", "  OSError: [WinError 5] Access is denied"])

    def test_cli_help_and_usage(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(supervise.main(["--help"]), 0)
            self.assertEqual(supervise.main([]), 2)
        self.assertIn("--silence SECONDS", buf.getvalue())
        self.assertIn("usage:", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
