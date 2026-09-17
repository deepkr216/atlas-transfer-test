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

from atlas import build, query, supervise, video  # noqa: E402

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

    def test_the_watchdog_knows_every_option_of_the_build(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            build._main(["--help"])
        found = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", buf.getvalue())) - {"--help"}
        self.assertEqual(found, set(supervise.BUILD_OPTIONS), "keep BUILD_OPTIONS in step with the build's parser")

    def test_abbreviated_options_are_spelled_out_before_the_watchdog_looks(self):
        args, notes = supervise.canonical_args(["C:\\e", "--rebuil", "--als=C:\\d", "--memb", "600", "--db", "x.db"])
        self.assertEqual(args, ["C:\\e", "--rebuild", "--also=C:\\d", "--member-limit", "600", "--db", "x.db"])
        self.assertEqual(notes, ["--rebuil read as --rebuild", "--als read as --also", "--memb read as --member-limit"])
        ns = supervise.build_settings(args)
        self.assertEqual((ns.root, ns.also, ns.rebuild), ("C:\\e", ["C:\\d"], True))
        ns = supervise.build_settings(["--manifest", "m.json", "C:\\estate", "--also", "C:\\a", "--also", "C:\\b"])
        self.assertEqual((ns.root, ns.also, ns.rebuild), ("C:\\estate", ["C:\\a", "C:\\b"], False))

    def test_quiet_is_dropped_so_the_watchdog_can_see(self):
        said = []
        rc = supervise.run([os.path.join(self.td, "estate"), "--db", self.db, "--rebuild", "--quiet"], silence=30,
                           say=said.append)
        text = "\n".join(said)
        self.assertEqual(rc, 0, text)
        self.assertIn("--quiet dropped", text)
        self.assertIn("== parsing", text, "the build's status lines are there")

    def test_a_command_naming_fewer_folders_than_the_index_holds_is_refused(self):
        docs = os.path.join(self.td, "Docs Folder")
        os.makedirs(docs)
        video.write_docx(os.path.join(docs, "Release Plan.docx"), "Plan", ["x"], [("Steps", ["Restart from STEP020."])])
        estate = os.path.join(self.td, "estate")
        said = []
        self.assertEqual(supervise.run([estate, "--db", self.db, "--rebuild", "--also", docs], silence=30, say=said.append), 0)
        conn = query.connect(self.db)
        before = conn.execute("SELECT COUNT(*) FROM member").fetchone()[0]
        conn.close()
        said = []
        rc = supervise.run([estate, "--db", self.db], silence=30, say=said.append)      # --also forgotten
        text = "\n".join(said)
        self.assertEqual(rc, 2, text)
        self.assertIn("NOT STARTED - 1 of the", text)
        self.assertIn(docs, text)
        self.assertIn("RELEASE PLAN", text)
        self.assertIn("--allow-prune", text)
        self.assertNotIn("build started", text, "nothing was run")
        conn = query.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM member").fetchone()[0], before, "nothing was removed")
        conn.close()
        if sys.platform == "win32":
            said = []
            rc = supervise.run([os.path.join(self.td, "ESTATE"), "--db", self.db, "--also", docs], silence=30, say=said.append)
            text = "\n".join(said)
            self.assertEqual(rc, 2, text)
            self.assertIn("typed with different capitals from last time", text)
            self.assertIn(os.path.join(self.td, "ESTATE") + " now", text)
        said = []
        rc = supervise.run([estate, "--db", self.db, "--allow-prune"], silence=30, say=said.append)
        text = "\n".join(said)
        self.assertEqual(rc, 0, text)
        self.assertIn("(1 gone from disk)", text, "with --allow-prune the build runs and prunes")
        conn = query.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM member").fetchone()[0], before - 1)
        conn.close()
        self.assertEqual(supervise.uncovered_members(os.path.join(self.td, "no.db"), [estate]), None)

    def test_a_second_freeze_on_a_member_already_on_the_skip_list_stops_with_a_reason(self):
        # a '#' in the path: the build cuts the skip-list line there (a build.py fix waits for the next
        # re-parse), so the member is never skipped and froze the build 50 times over
        hashed = os.path.join(self.td, "estate", "SRC#2")
        os.rename(self.root, hashed)
        said = []
        with mock.patch.dict(os.environ, {"ATLAS_TEST_FREEZE": "WALKPGM.cbl"}):
            rc = supervise.run([os.path.join(self.td, "estate"), "--db", self.db, "--rebuild", "--member-limit", "600"],
                               silence=6, max_restarts=5, say=said.append)
        text = "\n".join(said)
        self.assertEqual(rc, 3, text)
        self.assertIn("FROZEN again on cobol WALKPGM.cbl", text)
        self.assertIn("already on the skip list", text)
        self.assertEqual(text.count("supervisor: build started"), 2, "one restart, then a plain stop")
        self.assertIn("WITHOUT --rebuild", text)

    def test_problems_from_before_a_restart_are_listed_at_the_end(self):
        skip = os.path.join(self.td, "atlas-skip.txt")
        with open(skip, "w", encoding="utf-8") as fh:
            fh.write(os.path.join(self.root, "SAMPPGM.cbl") + "\t# cobol SAMPPGM.cbl: froze the parser at 2026-09-16\n")
        said = []
        with mock.patch.dict(os.environ, {"ATLAS_TEST_FREEZE": "WALKPGM.cbl"}):
            rc = supervise.run([os.path.join(self.td, "estate"), "--db", self.db, "--rebuild", "--member-limit", "600"],
                               silence=6, say=said.append)
        text = "\n".join(said)
        self.assertEqual(rc, 0, text)
        self.assertIn("supervisor: build finished after 1 restart(s)", text)
        tail = text.split("problem line(s) from before the restart(s)", 1)
        self.assertEqual(len(tail), 2, "the first run's problems are shown again at the end:\n" + text)
        self.assertIn("SAMPPGM.cbl", tail[1])
        with open(os.path.join(self.td, "atlas-problems.txt"), encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.count("\t") >= 3]
        self.assertTrue(any("SAMPPGM.cbl" in ln for ln in lines), lines)
        self.assertTrue(any("WALKPGM.cbl" in ln for ln in lines), lines)

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

    def test_selfcheck_says_what_the_suite_skipped_and_why(self):
        sys.path.insert(0, os.path.dirname(HERE))
        import selfcheck
        text = ("test_a (tests.test_video.EndToEnd.test_a) ... skipped 'Windows media composition cannot make a test video here'\n"
                "test_b (tests.test_ocr_images.Metafiles.test_b) ... skipped 'Windows imaging (System.Drawing) not available here'\n"
                "test_c (tests.test_ocr_images.Metafiles.test_c) ... skipped 'Windows imaging (System.Drawing) not available here'\n"
                "test_d (tests.test_x.Y.test_d) ... ok\n")
        self.assertEqual(selfcheck.skip_summary(text),
                         ["skipped (2 tests): Windows imaging (System.Drawing) not available here",
                          "skipped (1 test): Windows media composition cannot make a test video here"])
        self.assertEqual(selfcheck.skip_summary("all ok\n"), [])

    def test_cli_help_and_usage(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(supervise.main(["--help"]), 0)
            self.assertEqual(supervise.main([]), 2)
        self.assertIn("--silence SECONDS", buf.getvalue())
        self.assertIn("--allow-prune", buf.getvalue())
        self.assertIn("usage:", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
