r"""
`diff` - two versions of a member (LESSONS 134): the production copy in
estate\GC and the changed copy in estate\GC-TEST are two systems by folder
layout alone; the report names the facts that changed (paragraphs, calls,
fields, the fields that only shifted) and the changed lines of both sides
with their own numbers, citable through the gate on either side; no member
named = the release contents.
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query, verify_citations  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def _read(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def _write(folder, name, text):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class Diff(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        estate = os.path.join(cls.td, "estate")
        prod_src, prod_cpy = os.path.join(estate, "GC", "PROD.GC.SRC"), os.path.join(estate, "GC", "PROD.GC.CPY")
        test_src, test_cpy = os.path.join(estate, "GC-TEST", "TEST.GC.SRC"), os.path.join(estate, "GC-TEST", "TEST.GC.CPY")
        pgm, rec, proc = _read("WALKPGM.cbl"), _read("WALKREC.cpy"), _read("WALKPROC.cpy")
        _write(prod_src, "WALKPGM.cbl", pgm)
        _write(prod_cpy, "WALKREC.cpy", rec)
        _write(prod_cpy, "WALKPROC.cpy", proc)
        # the release: a MOVE changed, a CALL added, a paragraph added, a field inserted
        new_pgm = (pgm.replace("           MOVE 0 TO WS-READ-COUNT.", "           MOVE 1 TO WS-READ-COUNT.")
                      .replace("           PERFORM 2100-RATE.\n", "           PERFORM 2100-RATE.\n           PERFORM 2200-AUDIT.\n")
                      .replace("           ADD 1 TO WS-READ-COUNT.\n",
                               "           CALL 'NEWRATE' USING WS-RATE-PARM.\n           ADD 1 TO WS-READ-COUNT.\n"
                               "       2200-AUDIT.\n           DISPLAY 'AUDIT ' WR-POLICY-NO.\n"))
        # sequence numbers in columns 1-6 on the test copy: not a change
        seq = "\n".join((f"{(i + 1) * 100:06d}" + ln[6:]) if len(ln) > 6 else ln for i, ln in enumerate(new_pgm.split("\n")))
        _write(test_src, "WALKPGM.cbl", seq)
        _write(test_cpy, "WALKREC.cpy", rec.replace("           05  WR-CLAIM-AMT",
                                                    "           05  WR-GENDER            PIC X(01).\n           05  WR-CLAIM-AMT"))
        _write(test_cpy, "WALKPROC.cpy", proc)
        cls.loose = os.path.join(cls.td, "downloaded", "WALKPGM.cbl")
        _write(os.path.dirname(cls.loose), "WALKPGM.cbl", new_pgm.replace("'AUDIT '", "'AUDIT2 '"))
        cls.db = os.path.join(cls.td, "t.db")
        build._main([estate, "--db", cls.db, "--rebuild", "--quiet"])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_folder_layout_names_the_systems(self):
        rows = self.conn.execute("SELECT name, system, library FROM member WHERE name='WALKPGM' ORDER BY system").fetchall()
        self.assertEqual([(r["system"], r["library"]) for r in rows], [("GC", "PROD.GC.SRC"), ("GC-TEST", "TEST.GC.SRC")])
        # each environment's program expands its own environment's copybook
        for sysname in ("GC", "GC-TEST"):
            pid = self.conn.execute("SELECT p.id FROM program p JOIN member m ON m.id=p.member_id WHERE m.system=?",
                                    (sysname,)).fetchone()[0]
            n = self.conn.execute("SELECT COUNT(*) FROM expand_run r JOIN member s ON s.id=r.src_member "
                                  "WHERE r.program_id=? AND s.name='WALKREC' AND s.system=?", (pid, sysname)).fetchone()[0]
            self.assertGreaterEqual(n, 1, f"{sysname} program must expand the {sysname} copybook")

    def test_program_diff_facts_and_lines(self):
        t = query.cmd_diff(self.conn, "WALKPGM")
        self.assertIn("# Diff GC/WALKPGM -> GC-TEST/WALKPGM", t)
        self.assertIn("old = the copy whose library name says PROD", t)
        self.assertIn("(columns 1-6 and 73-80 ignored)", t)
        self.assertIn("**Paragraphs**: added `2200-AUDIT`", t)
        self.assertIn("**Calls**: added `CALL NEWRATE`", t)
        self.assertIn("**Paragraphs with changed lines**:", t)
        line = t.split("Paragraphs with changed lines")[1].split("\n")[0]
        for p in ("1000-INIT", "2000-PROCESS", "2100-RATE"):
            self.assertIn(f"`{p}`", line)
        for p in ("2050-ADJ", "3000-WRAP"):          # the paragraph AFTER an insertion point did not change
            self.assertNotIn(f"`{p}`", line)
        self.assertNotIn("`2200-AUDIT`", line)       # already listed as added
        self.assertRegex(t, r"-\s+\d+ \| \s+MOVE 0 TO WS-READ-COUNT\.")
        self.assertRegex(t, r"\+\s+\d+ \| 0\d{5} +MOVE 1 TO WS-READ-COUNT\.")     # the new side's own text and number
        self.assertIn('cite the old side as `[[GC/WALKPGM line "token"]]`, the new side as `[[GC-TEST/WALKPGM line "token"]]`', t)
        # the gate accepts both sides
        new_line = next(int(ln.split("|")[0][1:]) for ln in t.splitlines() if ln.startswith("+") and "NEWRATE" in ln)
        res, _u = verify_citations.check_answer(f'[[GC-TEST/WALKPGM {new_line} "NEWRATE"]]', db_path=self.db)
        self.assertEqual([r.status for r in res], ["PASS"], res)
        res, _u = verify_citations.check_answer(f'[[GC/WALKPGM {new_line} "NEWRATE"]]', db_path=self.db)
        self.assertNotEqual([r.status for r in res], ["PASS"], "the old side does not contain the new call")
        # NAME@LIBRARY reaches the right copy too (the gate's name was greedy and swallowed the library - LESSONS 135)
        res, _u = verify_citations.check_answer(f'[[WALKPGM@TEST.GC.SRC {new_line} "NEWRATE"]]', db_path=self.db)
        self.assertEqual([r.status for r in res], ["PASS"], res)
        res, _u = verify_citations.check_answer(f'[[WALKPGM@PROD.GC.SRC {new_line} "NEWRATE"]]', db_path=self.db)
        self.assertNotEqual([r.status for r in res], ["PASS"], res)
        # explicit forms
        self.assertIn("# Diff GC/WALKPGM -> GC-TEST/WALKPGM", query.cmd_diff(self.conn, "GC/WALKPGM", "GC-TEST/WALKPGM"))
        self.assertIn("# Diff GC/WALKPGM -> GC-TEST/WALKPGM", query.cmd_diff(self.conn, "WALKPGM@PROD.GC.SRC", "WALKPGM@TEST"))
        self.assertIn("# Diff GC/WALKPGM -> GC-TEST/WALKPGM", query.cmd_diff(self.conn, "WALKPGM", system="GC-TEST"))

    def test_copybook_diff_names_the_shift(self):
        t = query.cmd_diff(self.conn, "GC/WALKREC", "GC-TEST/WALKREC")
        self.assertIn("**Fields**: added `05 WR-GENDER` X(01) @10 len 1", t)
        self.assertIn("**Fields shifted** by +1 byte(s), same picture: 2 field(s), from `05 WR-CLAIM-AMT` to `05 WR-OTHER`", t)
        self.assertRegex(t, r"\+\s+\d+ \| \s+05  WR-GENDER")

    def test_identical_and_budget(self):
        t = query.cmd_diff(self.conn, "GC/WALKPROC", "GC-TEST/WALKPROC")
        self.assertIn("**identical**", t)
        self.assertNotIn("## Lines", t)
        t = query.cmd_diff(self.conn, "WALKPGM", budget=900)
        self.assertIn("more changed region(s) not shown", t)
        self.assertIn("**Calls**: added", t)                                   # the facts survive the budget

    def test_loose_file_lines_only(self):
        t = query.cmd_diff(self.conn, "GC/WALKPGM", self.loose)
        self.assertIn("(not in the index: lines only)", t)
        self.assertIn("has no facts: lines only", t)
        self.assertIn("'AUDIT2 '", t)
        self.assertIn('cite `[[GC/WALKPGM line "token"]]` on the other side', t)

    def test_not_found_and_single_copy(self):
        self.assertIn("**NOT FOUND**", query.cmd_diff(self.conn, "NOPGM"))
        self.assertIn("**NOT FOUND**: new side", query.cmd_diff(self.conn, "GC/WALKPGM", "GC-PROD2/WALKPGM"))
        self.assertIn("both references name the same copy", query.cmd_diff(self.conn, "GC/WALKPGM", "GC/WALKPGM"))

    def test_release_contents(self):
        t = query.cmd_diff(self.conn)
        self.assertIn("| WALKPGM | cobol | GC/WALKPGM | GC-TEST/WALKPGM |", t)
        self.assertIn("| WALKREC | copybook | GC/WALKREC | GC-TEST/WALKREC |", t)
        self.assertNotIn("| WALKPROC |", t)                                    # identical copies are not a change
        self.assertIn("`diff GC/WALKPGM GC-TEST/WALKPGM`", t)
        self.assertIn("2 member(s) differ", t)
        t = query.cmd_diff(self.conn, system="GC-TEST")
        self.assertIn("(changed copy in GC-TEST)", t)
        self.assertIn("| WALKPGM | cobol | GC/WALKPGM | GC-TEST/WALKPGM |", t)
        self.assertNotIn("New in GC-TEST", t)
        self.assertIn("no member has two copies", query.cmd_diff(self.conn, system="NOSUCH"))

    def test_cli(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            query._main(["--db", self.db, "diff", "WALKPGM", "--budget", "4000"])
        self.assertIn("# Diff GC/WALKPGM -> GC-TEST/WALKPGM", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            query._main(["--db", self.db, "diff", "--system", ""])
        self.assertIn("# Release contents", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
