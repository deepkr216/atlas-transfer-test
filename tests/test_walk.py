"""
`walk PGM` - the program in reading order, the read an analyst does by hand:

  the entry paragraph first, then each paragraph the first time control
  reaches it; a PERFORM returns at the end of its target (the paragraph
  after a performed one is NOT reached by falling out of it); GO TO does
  not return; a performed SECTION runs its paragraphs; a THRU range is
  walked whole; a paragraph reached twice is shown once and noted after;
  ENTRY points are extra roots; DECLARATIVES are listed, not walked;
  paragraphs from a procedure copybook cite the copybook; source keeps its
  indentation and stops at the last real line (the parser's end_line may
  run past the expanded text); the data section holds only the fields the
  PROCEDURE DIVISION names (files are not fields, an 01 named as a whole
  is said on its header, 88 values are spelled out); a budget keeps the
  order and the facts and drops source from the end; --from / --depth /
  --no-source; the lines it prints pass the citation gate.
"""

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query, verify_citations  # noqa: E402

FIX = os.path.join(HERE, "fixtures")

DECLPGM = """       IDENTIFICATION DIVISION.
       PROGRAM-ID.    DECLPGM.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT IN-FILE ASSIGN TO INDD.
       DATA DIVISION.
       FILE SECTION.
       FD  IN-FILE.
       01  IN-REC                   PIC X(80).
       WORKING-STORAGE SECTION.
       01  WS-STATUS                PIC X(02).
       PROCEDURE DIVISION.
       DECLARATIVES.
       ERR-SECT SECTION.
           USE AFTER STANDARD ERROR PROCEDURE ON IN-FILE.
       ERR-PARA.
           DISPLAY 'IO ERROR ' WS-STATUS.
       END DECLARATIVES.
       MAIN-SECT SECTION.
       0000-MAIN.
           OPEN INPUT IN-FILE.
           PERFORM 1000-READ.
           CLOSE IN-FILE.
           GOBACK.
       1000-READ.
           READ IN-FILE.
"""

WALKJOB = """//WALKJOB JOB (A),'WALK',CLASS=A
//S1       EXEC PGM=WALKPGM
//CLMIN    DD DSN=TEST.CLAIMS.IN,DISP=SHR
"""


def headers(text: str):
    """The walked paragraph / section names in the order they are shown."""
    return re.findall(r"^####\s+(\S+)", text, re.M)


class Walk(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        for rel, src in (("SRC/WALKPGM.cbl", "WALKPGM.cbl"), ("COPYLIB/WALKREC.cpy", "WALKREC.cpy"),
                         ("COPYLIB/WALKPROC.cpy", "WALKPROC.cpy")):
            p = os.path.join(cls.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            shutil.copy(os.path.join(FIX, src), p)
        with open(os.path.join(cls.root, "SRC", "DECLPGM.cbl"), "w") as fh:
            fh.write(DECLPGM)
        os.makedirs(os.path.join(cls.root, "JCLLIB"))
        with open(os.path.join(cls.root, "JCLLIB", "WALKJOB.jcl"), "w") as fh:
            fh.write(WALKJOB)
        cls.db = os.path.join(cls.td, "t.db")
        build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"])
        cls.conn = query.connect(cls.db)
        cls.text = query.cmd_walk(cls.conn, "WALKPGM")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def block(self, name: str, text: str = None) -> str:
        """The text of one walked block: from its header to the next header."""
        m = re.search(r"^####\s+" + re.escape(name) + r"\b.*?(?=^####\s|^###\s|\Z)", text or self.text, re.M | re.S)
        self.assertIsNotNone(m, name)
        return m.group(0)

    def data_section(self, text: str = None) -> str:
        m = re.search(r"### Data it touches.*?(?=\n## Walk)", text or self.text, re.S)
        self.assertIsNotNone(m)
        return m.group(0)

    # ---- order -------------------------------------------------------------

    def test_reading_order(self):
        self.assertEqual(headers(self.text),
                         ["0000-MAIN", "1000-INIT", "2000-PROCESS", "2000-EXIT", "2100-RATE", "2050-ADJ",
                          "3000-WRAP", "8000-COMMON", "9000-ERRORS", "9010-SHOW", "9020-DONE", "7000-ENTRY"],
                         self.text)

    def test_perform_returns_instead_of_falling_through(self):
        # 1000-INIT does not end with a terminal statement, so the parser has a
        # fall-through edge to 1500-UNUSED; but PERFORM 1000-INIT returns there.
        self.assertNotIn("1500-UNUSED", headers(self.text))
        m = re.search(r"### Not reached from the entry \(1\)\n(.*?)(?=\n###|\Z)", self.text, re.S)
        self.assertIsNotNone(m, self.text)
        self.assertIn("| 1500-UNUSED | paragraph | WALKPGM:35 | no PERFORM, GO TO, fall-through", m.group(1))

    def test_goto_thru_and_repeat_notes(self):
        self.assertEqual(headers(self.text).count("2000-EXIT"), 1)
        self.assertIn("<- GO TO from 2000-PROCESS @WALKPGM:41  [depth 2]", self.block("2000-EXIT"))
        self.assertIn("> 2000-EXIT <- falls through from 2050-ADJ @WALKPGM:44 - shown above", self.text)
        self.assertIn("<- falls through from 2000-PROCESS @WALKPGM:42  [depth 1]", self.block("2050-ADJ"))
        self.assertIn("| 2000-EXIT | falls through | 1 |", self.text)

    def test_section_walks_its_paragraphs(self):
        self.assertIn("9000-ERRORS SECTION  <- PERFORM from 3000-WRAP @WALKPGM:52  [depth 2]  WALKPGM:59-59",
                      self.block("9000-ERRORS"))
        self.assertIn("<- in section from 9000-ERRORS  [depth 3]", self.block("9010-SHOW"))
        self.assertIn("<- falls through from 9010-SHOW", self.block("9020-DONE"))

    def test_entry_point_is_a_root_not_dead(self):
        self.assertIn("### Entry point `WALKENT` (ENTRY @WALKPGM:56)", self.text)
        self.assertIn("7000-ENTRY  - ENTRY point  [depth 0]  WALKPGM:55-58", self.block("7000-ENTRY"))
        self.assertNotIn("| 7000-ENTRY |", self.text)

    # ---- source, cites, facts ---------------------------------------------

    def test_copybook_paragraph_cites_the_copybook(self):
        b = self.block("8000-COMMON")
        self.assertIn("WALKPROC:1-2 (via COPY WALKPROC)", b)          # not 1-3: the trailing blank is not a line
        self.assertIn('------ from COPY WALKPROC - cite as [[WALKPROC line "token"]] ------', b)
        self.assertIn("     2 |     DISPLAY 'COMMON ROUTINE'.", b)
        res, _u = verify_citations.check_answer('It says so [[WALKPROC 2 "DISPLAY \'COMMON ROUTINE\'"]].', db_path=self.db)
        self.assertEqual([r.status for r in res], ["PASS"], res)

    def test_source_lines_keep_indentation_and_stop_at_the_last_real_line(self):
        b = self.block("2100-RATE")
        self.assertIn("    48 |     CALL 'RATECALC' USING WS-RATE-PARM WR-POLICY-NO.", b)
        res, _u = verify_citations.check_answer('[[WALKPGM 48 "CALL \'RATECALC\'"]]', db_path=self.db)
        self.assertEqual([r.status for r in res], ["PASS"], res)
        last = self.block("9020-DONE")
        self.assertIn("[depth 3]  WALKPGM:62-63\n", last)            # the parser's end_line runs past the text
        self.assertNotIn("None", last)
        self.assertNotIn("    64 |", last)

    def test_facts_are_resolved(self):
        self.assertIn("- facts: CALL RATECALC", self.block("2100-RATE"))
        self.assertIn("OPEN INPUT CLAIM-FILE", self.block("1000-INIT"))
        self.assertIn("sets '0'->WS-READ-COUNT", self.block("1000-INIT"))

    def test_header_runs_in_and_touches(self):
        self.assertIn("- runs in: WALKJOB.S1", self.text)
        self.assertIn("- files: CLAIM-FILE (DD CLMIN, SEQUENTIAL) CLOSE/OPEN INPUT/READ", self.text)
        self.assertIn("- calls: CALL RATECALC", self.text)
        self.assertIn("# Walk WALKPGM  (member WALKPGM, 12 paragraphs in 1 section; entry 0000-MAIN; 12 reached, 1 not reached)",
                      self.text)

    # ---- data ---------------------------------------------------------------

    def test_data_holds_only_touched_fields(self):
        d = self.data_section()
        for wanted in ("| 05 WS-READ-COUNT | S9(07) COMP-3 | 0 | 4 | wd | WALKPGM:19 | WALKPGM:34 |",
                       "| 05 WR-POLICY-NO | X(10) | 0 | 10 | wr | WALKREC:1 | WALKPGM:48 |",
                       "| 05 WR-CLAIM-AMT | S9(07)V99 COMP-3 | 10 | 5 |",
                       "| 05 WS-EOF-FLAG (88 WS-EOF = 'Y') | X(01) | 0 | 1 | wt |",
                       "**COPY WALKREC** (fragment - its 01 is in the program) - 2 of 3 fields used",
                       "**01 WS-FLAGS** (WALKPGM:14, 2 bytes) - 1 of 2 fields used",
                       "**01 WS-RATE-PARM** (WALKPGM:21, 6 bytes, the group itself used as wr @WALKPGM:48) - 1 of 1 fields used",
                       "**01 LS-ENTRY-PARM** (WALKPGM:24, 10 bytes)\n",
                       "| 01 LS-ENTRY-PARM | X(10) | 0 | 10 | d | WALKPGM:24 | WALKPGM:57 |"):
            self.assertIn(wanted, d, wanted)
        for absent in ("WR-OTHER", "WS-UNUSED-FLAG", "CLAIM-FILE", "referenced but not defined"):
            self.assertNotIn(absent, d, absent)
        # WS-NEVER-COUNT is named by an unreached paragraph - still "named", so present
        self.assertIn("WS-NEVER-COUNT", d)

    # ---- options -----------------------------------------------------------

    def test_budget_keeps_order_and_facts_drops_source_from_the_end(self):
        budget = len(self.text) - 700
        small = query.cmd_walk(self.conn, "WALKPGM", budget=budget)
        self.assertEqual(headers(small), headers(self.text))
        self.assertIn(f"budget {budget}: source omitted for", small.split("\n", 1)[0])
        self.assertLessEqual(len(small), budget + 60)
        # the entry keeps its source; the last walked paragraph lost its
        self.assertIn("```", self.block("0000-MAIN", small))
        last = re.search(r"^####\s+7000-ENTRY.*?(?=^###\s|\Z)", small, re.M | re.S).group(0)
        self.assertNotIn("```", last)
        self.assertIn("[depth 0]  WALKPGM:55-58", last)               # the span stays: it is the `cite` range
        self.assertIn("- facts: CALL RATECALC", small)               # facts survive the budget

    def test_from_depth_and_no_source(self):
        part = query.cmd_walk(self.conn, "WALKPGM", start="3000-WRAP")
        self.assertEqual(headers(part)[:5], ["3000-WRAP", "8000-COMMON", "9000-ERRORS", "9010-SHOW", "9020-DONE"])
        self.assertIn("3000-WRAP  - entry", part)
        shallow = query.cmd_walk(self.conn, "WALKPGM", max_depth=0)
        self.assertIn("```", self.block("0000-MAIN", shallow))
        self.assertNotIn("```", self.block("1000-INIT", shallow))
        self.assertIn("- facts: OPEN INPUT CLAIM-FILE", self.block("1000-INIT", shallow))
        bare = query.cmd_walk(self.conn, "WALKPGM", source=False)
        self.assertNotIn("```", bare)
        self.assertEqual(headers(bare), headers(self.text))
        self.assertIn("**NOT FOUND** - no paragraph or section named NOPE", query.cmd_walk(self.conn, "WALKPGM", start="nope"))

    def test_declaratives_are_listed_not_walked(self):
        t = query.cmd_walk(self.conn, "DECLPGM")
        self.assertEqual(headers(t), ["MAIN-SECT", "0000-MAIN", "1000-READ"], t)
        self.assertIn("| ERR-PARA | paragraph | DECLPGM:17 | DECLARATIVES - runs on its USE condition", t)
        self.assertIn("| ERR-SECT | section | DECLPGM:15 | DECLARATIVES", t)
        self.assertNotIn("| DECLARATIVES |", t)

    def test_cli_and_out(self):
        out = os.path.join(self.td, "work", "walk.md")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = query._main(["--db", self.db, "--out", out, "walk", "WALKPGM", "--no-source", "--budget", "6000"])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            t = fh.read()
        self.assertTrue(t.startswith("<!-- walk WALKPGM: ~"))
        self.assertNotIn("```", t)
        self.assertIn("program NOPE not found", query.cmd_walk(self.conn, "NOPE"))


if __name__ == "__main__":
    unittest.main()
