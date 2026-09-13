"""
`walk` against COBOL shapes found by the adversarial probe battery (LESSONS
122-126): a paragraph name that lives in two sections (PERFORM X OF SEC,
GO TO X OF SEC); GO TO ... DEPENDING ON falls through when out of range; a
section whose paragraph was reached is not "never entered"; the entry
section's last paragraph keeps flowing into the next section (only a
PERFORMed section returns); a procedure copybook renamed by COPY REPLACING
says so; a copybook's trailing blank line is not printed; a program with no
paragraphs gets a useful message.
"""

import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query, verify_citations  # noqa: E402

QUAL = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. QUAL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-X                     PIC X.
       PROCEDURE DIVISION.
       MAIN-SEC SECTION.
       M100.
           PERFORM SUB-A.
           PERFORM WORK OF SUB-B.
           IF WS-X = 'G'
               GO TO WORK IN SUB-B.
           PERFORM FINISH.
       SUB-A SECTION.
       WORK.
           DISPLAY 'A-WORK'.
       SUB-B SECTION.
       WORK.
           DISPLAY 'B-WORK'.
       END-SEC SECTION.
       FINISH.
           DISPLAY 'BYE'.
           GOBACK.
"""

DEP = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. DEP.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-K                     PIC 9(01).
       PROCEDURE DIVISION.
       A100-START.
           GO TO B100-ONE B200-TWO DEPENDING ON WS-K.
       A200-FALL.
           DISPLAY 'K OUT OF RANGE'.
           GOBACK.
       B100-ONE.
           DISPLAY 'ONE'.
           GOBACK.
       B200-TWO.
           DISPLAY 'TWO'.
           GOBACK.
"""

REPL = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. REPL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-CODE                  PIC X(04).
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1100-TWO.
           PERFORM LK-PARA.
           GOBACK.
           COPY RPROC.
           COPY RPROC2 REPLACING ==XX-== BY ==LK-==.
"""
RPROC = """       1000-ONE.
           DISPLAY 'ONE'.
       1100-TWO.
           DISPLAY 'TWO'.
"""
RPROC2 = """       XX-PARA.
           DISPLAY 'REPLACED PARA'.
"""

NOPARA = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. NOPARA.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-N                     PIC 9(05).
       PROCEDURE DIVISION.
           ADD 1 TO WS-N.
           GOBACK.
"""


def headers(text):
    return re.findall(r"^####\s+(\S+)", text, re.M)


class Shapes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate")
        os.makedirs(os.path.join(root, "SRC"))
        os.makedirs(os.path.join(root, "COPYLIB"))
        for name, text in (("QUAL", QUAL), ("DEP", DEP), ("REPL", REPL), ("NOPARA", NOPARA)):
            with open(os.path.join(root, "SRC", name + ".cbl"), "w") as fh:
                fh.write(text)
        for name, text in (("RPROC", RPROC), ("RPROC2", RPROC2)):
            with open(os.path.join(root, "COPYLIB", name + ".cpy"), "w") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        build._main([root, "--db", cls.db, "--rebuild", "--quiet"])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def pid(self, name):
        return self.conn.execute("SELECT id FROM program WHERE program_id=?", (name,)).fetchone()[0]

    def test_qualified_perform_and_goto_reach_the_right_paragraph(self):
        edges = {(r["to_para"], r["kind"]) for r in self.conn.execute(
            "SELECT to_para, kind FROM perform_edge WHERE program_id=?", (self.pid("QUAL"),))}
        self.assertIn(("WORK OF SUB-B", "perform"), edges)
        self.assertIn(("WORK OF SUB-B", "goto"), edges)
        self.assertNotIn(("OF", "goto"), edges)
        self.assertNotIn(("SUB-B", "goto"), edges)
        t = query.cmd_walk(self.conn, "QUAL")
        # both WORKs are shown, each with its own lines; nothing is "not reached"
        self.assertIn("WORK  <- in section from SUB-A  [depth 3]  QUAL:15-16", t)
        self.assertIn("WORK  <- PERFORM from M100 @QUAL:10  [depth 2]  QUAL:18-19", t)
        self.assertIn("> WORK <- GO TO from M100 @QUAL:12 - shown above", t)
        self.assertEqual(headers(t).count("WORK"), 2)
        self.assertNotIn("### Not reached", t)
        self.assertIn("0 not reached)", t)
        # `paragraph` and `program` still credit the paragraph reached by the qualified edge
        self.assertIn("perform", query.cmd_paragraph(self.conn, "QUAL", "WORK"))
        self.assertIn("**0 reached by nothing**", query.cmd_program(self.conn, "QUAL"))

    def test_entry_section_keeps_flowing_a_performed_section_returns(self):
        t = query.cmd_walk(self.conn, "QUAL")
        # M100 is the last paragraph of the ENTRY section: control really falls into SUB-A's WORK
        self.assertIn("> WORK <- falls through from M100 @QUAL:13 - shown above", t)
        # SUB-A was PERFORMed: after its WORK the PERFORM returns; WORK's fall-through into SUB-B is not followed
        self.assertNotIn("falls through from WORK", t)

    def test_goto_depending_falls_through_when_out_of_range(self):
        edges = {(r["from_para"], r["to_para"], r["kind"]) for r in self.conn.execute(
            "SELECT from_para, to_para, kind FROM perform_edge WHERE program_id=?", (self.pid("DEP"),))}
        self.assertIn(("A100-START", "A200-FALL", "fallthrough"), edges)
        t = query.cmd_walk(self.conn, "DEP")
        self.assertEqual(headers(t), ["A100-START", "B100-ONE", "B200-TWO", "A200-FALL"], t)
        self.assertIn("A200-FALL  <- falls through from A100-START", t)
        self.assertIn("0 not reached)", t)

    def test_replacing_renamed_copybook_paragraph_says_so_and_blank_lines_vanish(self):
        t = query.cmd_walk(self.conn, "REPL")
        b = re.search(r"^####\s+LK-PARA.*?```\n.*?```\n", t, re.M | re.S).group(0)
        self.assertIn("- note: the copybook line reads `XX-PARA.` - COPY REPLACING renames it to LK-PARA in this program; "
                      "cite the copybook's own text", b)
        self.assertIn("     1 | XX-PARA.", b)
        res, _u = verify_citations.check_answer('[[RPROC2 1 "XX-PARA"]]', db_path=self.db)
        self.assertEqual([r.status for r in res], ["PASS"])
        two = re.search(r"^####\s+1100-TWO.*?```\n.*?```\n", t, re.M | re.S).group(0)
        self.assertIn("     3 | 1100-TWO.", two)
        self.assertIn("     4 |     DISPLAY 'TWO'.", two)
        self.assertNotIn("     5 |", two)                       # the copybook's trailing blank is not a line
        self.assertNotRegex(t, r"^\s+\d+ \| $", "a blank source line was printed")
        self.assertIn("1000-ONE | paragraph |", t)              # never performed: listed, not walked

    def test_program_without_paragraphs(self):
        t = query.cmd_walk(self.conn, "NOPARA")
        self.assertIn("The PROCEDURE DIVISION has no paragraphs or sections in the index", t)
        self.assertIn("`cite NOPARA <first>-<last>`", t)


if __name__ == "__main__":
    unittest.main()
