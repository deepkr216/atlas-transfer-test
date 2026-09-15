r"""
Errors during a long build are shown so they cannot be missed (LESSONS 149):
each failure is printed with the time, the member and the toolkit line;
written to atlas-problems.txt beside the index; and listed again, grouped,
at the end of the build - a FAILED line printed at 02:00 has scrolled away
by morning. A full disk or an index that cannot be written stops the build
once with a plain message instead of failing every remaining member; a
crash says so in plain words before the crash report, which is saved beside
the index; the supervisor says why the build stopped and does not restart
into the same error.
"""

import builtins
import contextlib
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query, supervise  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


class BuildErrors(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.src = os.path.join(self.td, "estate", "SRC")
        os.makedirs(self.src)
        for fn in ("ERRPGM.cbl", "MEMBRVAL.cbl", "SAMPPGM.cbl", "WALKPGM.cbl", "PMASTREC.cpy", "WALKREC.cpy", "WALKPROC.cpy"):
            shutil.copy(os.path.join(FIX, fn), self.src)
        self.db = os.path.join(self.td, "t.db")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _build(self, *extra, entry=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = (entry or build._main)([os.path.join(self.td, "estate"), "--db", self.db, *extra])
        return rc, out.getvalue(), err.getvalue()

    def _problems_file(self):
        with open(os.path.join(self.td, build.PROBLEMS_FILE), encoding="utf-8") as fh:
            return fh.read()

    def test_a_clean_build_says_there_were_no_problems(self):
        rc, out, _ = self._build("--rebuild")
        self.assertEqual(rc, 0)
        self.assertIn("== problems in this run: none ==", out)
        self.assertTrue(self._problems_file().startswith("# atlas build started"))

    def test_a_failed_member_is_shown_with_time_and_toolkit_line_and_listed_at_the_end(self):
        real = build.HANDLERS["cobol"]

        def bad(ctx, mem):
            if mem.name == "WALKPGM":
                raise ValueError("injected parser error")
            return real(ctx, mem)

        with mock.patch.dict(build.HANDLERS, {"cobol": bad}):
            rc, out, _ = self._build("--rebuild")
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"\d\d:\d\d:\d\d  FAILED cobol .*WALKPGM\.cbl: ValueError: injected parser error  "
                              r"\[build\.py:\d+ in _parse_one\]")
        # listed again at the end, after the summary
        end = out[out.index("== problems in this run"):]
        self.assertIn("== problems in this run: 1 member(s) or file(s) not indexed ==", end)
        self.assertIn("  parse failed: 1", end)
        self.assertIn("    WALKPGM.cbl  (SRC)  ValueError: injected parser error [build.py:", end)
        self.assertIn(f"every one of them, with its folder and reason: {os.path.join(self.td, build.PROBLEMS_FILE)}", end)
        self.assertGreater(out.index("== problems in this run"), out.index("== members by kind / parse status =="))
        # in the file, one tab-separated line
        lines = [ln for ln in self._problems_file().splitlines() if not ln.startswith("#")]
        self.assertEqual(len(lines), 1)
        _when, kind, path, detail = lines[0].split("\t")
        self.assertEqual((kind, os.path.basename(path)), ("parse failed", "WALKPGM.cbl"))
        self.assertIn("ValueError: injected parser error", detail)
        # and on the member, for `coverage`
        conn = query.connect(self.db)
        try:
            st, err = conn.execute("SELECT parse_status, parse_error FROM member WHERE name='WALKPGM'").fetchone()
        finally:
            conn.close()
        self.assertEqual(st, "failed")
        self.assertRegex(err, r"ValueError: injected parser error \[build\.py:\d+ in _parse_one\]")

    def test_an_unreadable_file_is_shown_and_listed(self):
        real_open = builtins.open

        def guarded(file, mode="r", *a, **k):
            if isinstance(file, str) and file.endswith("ERRPGM.cbl") and "b" in mode:
                raise PermissionError(13, "injected: access is denied", file)
            return real_open(file, mode, *a, **k)

        with mock.patch("builtins.open", guarded):
            rc, out, _ = self._build("--rebuild")
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"\d\d:\d\d:\d\d  unreadable: .*ERRPGM\.cbl \(\[Errno 13\] injected: access is denied")
        self.assertIn("  unreadable: 1", out[out.index("== problems in this run"):])

    def test_a_full_disk_stops_the_build_once_with_a_plain_message(self):
        with mock.patch.object(build, "_FAULT", "diskfull:SAMPPGM.cbl"):
            rc, out, _ = self._build("--rebuild")
        self.assertEqual(rc, 4, out)
        self.assertRegex(out, r"\d\d:\d\d:\d\d  STOPPED: the index cannot be written \(database or disk is full\) "
                              r"while parsing cobol SAMPPGM\.cbl")
        self.assertIn("free space on the drive holding", out)
        self.assertIn("members parsed so far are kept", out)
        self.assertNotIn("FAILED", out, "no member after it may fail one by one")
        self.assertIn("  index cannot be written: 1", out)
        conn = query.connect(self.db)
        try:
            pending = conn.execute("SELECT COUNT(*) FROM member WHERE parse_status='pending'").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(pending, 1, "the rest is left for the next run")

    def test_a_crash_says_so_plainly_and_saves_the_report_beside_the_index(self):
        cwd = os.getcwd()
        os.chdir(self.td)                       # a crash file must not land in whatever folder the shell is in...
        work = tempfile.mkdtemp()
        try:
            os.chdir(work)
            with mock.patch.object(build, "_FAULT", "crash:post"):
                rc, out, err = self._build("--rebuild", entry=build.main)
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 2)
        self.assertRegex(err, r"\d\d:\d\d:\d\d  BUILD STOPPED BY AN ERROR: RuntimeError: injected failure in the post pass")
        self.assertIn("Members parsed so far are kept", err)
        self.assertIn("=== atlas crash ===", err)
        self.assertIn("build.py:", err)
        command = [ln for ln in err.splitlines() if ln.startswith("command: ")][0]
        self.assertIn("--rebuild", command)                       # the arguments, not build.py repeated for each
        self.assertIn("--db <path>", command)                     # paths are hidden in a report meant to be shared
        self.assertLessEqual(command.count("build.py"), 1, command)
        crash = os.path.join(self.td, "atlas-crash.txt")          # ...but beside the index
        self.assertIn(f"Saved to {crash}", err)
        self.assertTrue(os.path.isfile(crash))
        self.assertFalse(os.path.exists(os.path.join(work, "atlas-crash.txt")))
        shutil.rmtree(work, ignore_errors=True)

    def test_which_errors_mean_the_index_cannot_be_written(self):
        self.assertEqual(build.fatal_db_error(sqlite3.OperationalError("database or disk is full")), "database or disk is full")
        self.assertTrue(build.fatal_db_error(sqlite3.OperationalError("database is locked")))
        self.assertTrue(build.fatal_db_error(sqlite3.OperationalError("disk I/O error")))
        self.assertTrue(build.fatal_db_error(OSError(28, "No space left on device")))
        self.assertIsNone(build.fatal_db_error(sqlite3.OperationalError("no such column: x")))
        self.assertIsNone(build.fatal_db_error(ValueError("bad")))


class CoverageExplainsTheStatuses(unittest.TestCase):
    """`ok / partial / skipped / failed` on the build's own summary means
    nothing without the reasons: coverage names the members parsed only in
    part and why, and says what each status means."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        src = os.path.join(self.td, "estate", "SRC")
        os.makedirs(src)
        # SAMPPGM and ERRPGM copy copybooks that are NOT here (partial); WALKPROC.cpy in a
        # source library is not a program (skipped); the rest parse
        for fn in ("SAMPPGM.cbl", "ERRPGM.cbl", "WALKPGM.cbl", "WALKREC.cpy", "WALKPROC.cpy", "SAMPJOB.jcl", "RUNCLM.ctl"):
            shutil.copy(os.path.join(FIX, fn), src)
        self.db = os.path.join(self.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([os.path.join(self.td, "estate"), "--db", self.db, "--rebuild"])

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_coverage_names_the_partial_members_and_their_reason(self):
        conn = query.connect(self.db)
        try:
            cov = query.cmd_coverage(conn)
        finally:
            conn.close()
        self.assertIn("**partial**: a parser ran but could not complete the picture", cov)
        self.assertIn("not a program (no PROGRAM-ID and no DIVISION header", cov)
        self.assertIn("### Members parsed only in part (2)", cov)
        self.assertIn("| cobol | 2 | expand (2) |", cov)
        for member, copybook in (("SAMPPGM", "PMASTREC"), ("ERRPGM", "POLDCL")):
            row = [ln for ln in cov.splitlines() if ln.startswith(f"| cobol | {member} |")]
            self.assertEqual(len(row), 1, cov)
            self.assertIn(f"COPY {copybook} NOT FOUND", row[0])
        self.assertIn("usually partial because a copybook it copies is not in the index", cov)

    def test_coverage_says_none_when_every_member_is_complete(self):
        os.remove(os.path.join(self.td, "estate", "SRC", "SAMPPGM.cbl"))
        os.remove(os.path.join(self.td, "estate", "SRC", "ERRPGM.cbl"))
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([os.path.join(self.td, "estate"), "--db", self.db, "--rebuild"])
        conn = query.connect(self.db)
        try:
            cov = query.cmd_coverage(conn)
        finally:
            conn.close()
        self.assertIn("### Members parsed only in part\n_none_", cov)


class SupervisorSaysWhy(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        root = os.path.join(self.td, "estate", "SRC")
        os.makedirs(root)
        for fn in ("SAMPPGM.cbl", "WALKPGM.cbl", "PMASTREC.cpy"):
            shutil.copy(os.path.join(FIX, fn), root)
        self.db = os.path.join(self.td, "t.db")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _run(self, fault):
        said = []
        with mock.patch.dict(os.environ, {"ATLAS_TEST_FAULT": fault}):
            rc = supervise.run([os.path.join(self.td, "estate"), "--db", self.db, "--rebuild"], silence=60, say=said.append)
        return rc, "\n".join(said)

    def test_a_crashed_build_is_explained_and_not_restarted(self):
        rc, text = self._run("crash:post")
        self.assertEqual(rc, 2, text)
        self.assertIn("BUILD STOPPED BY AN ERROR: RuntimeError: injected failure in the post pass", text)   # the child's own words
        self.assertIn("supervisor: the build STOPPED BY AN ERROR (exit code 2) - the message is above and in "
                      + os.path.join(self.td, "atlas-crash.txt"), text)
        self.assertNotIn("(restart 1)", text)

    def test_a_build_that_cannot_write_the_index_is_explained_and_not_restarted(self):
        rc, text = self._run("diskfull:WALKPGM.cbl")
        self.assertEqual(rc, 4, text)
        self.assertIn("STOPPED: the index cannot be written (database or disk is full)", text)
        self.assertIn("supervisor: the build STOPPED - the index cannot be written", text)
        self.assertNotIn("(restart 1)", text)


if __name__ == "__main__":
    unittest.main()
