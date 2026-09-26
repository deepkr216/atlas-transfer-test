r"""
The words EXEC SQL / EXEC CICS / EXEC DLI inside a literal are text, not the start of a block (ROADMAP re-parse item 28,
LESSONS 217). Found by the verifier in passing while checking ROADMAP re-parse item 27; on main since 3a27440.

reader.cobol_statements advanced its EXEC state over the raw text, quoted literals included: `01 WS-ERR PIC X(30) VALUE
'EXEC CICS LINK FAILED'.` opened a block no period closed, and every data item and paragraph after it was lost while
the member read `parse: ok` ('0 paragraphs'). reader.read_cobol_lines took `VALUE 'EXEC SQL'` as the start of an SQL
block too, and cut every later line holding ' --' there as an SQL comment - a heading literal `' -- END -- '` was left
open and ate the rest of the program. In the PROCEDURE DIVISION, cobol.py's EXEC patterns ran over the raw sentence:
`MOVE 'EXEC SQL FAILED' TO WS-MSG` before `EXEC SQL ROLLBACK END-EXEC` was one block from the literal on - the SQL
statement read "FAILED' TO WS-MSG ...", a CICS RETURN was recorded as a LINK, and the MOVE was blanked with the block.
Error-message literals like these are common in CICS and DB2 programs. The names below are fictional.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from atlas import build, cobol, copybook, query, reader  # noqa: E402


def program(name, data, proc):
    def block(rows):
        return "".join(f"       {r}\n" for r in rows)
    return ("       IDENTIFICATION DIVISION.\n"
            f"       PROGRAM-ID. {name}.\n"
            "       DATA DIVISION.\n"
            "       WORKING-STORAGE SECTION.\n"
            + block(data)
            + "       PROCEDURE DIVISION.\n"
            + block(proc))


LNKMSG = program("LNKMSG", [
    "01  WS-ERR           PIC X(30) VALUE 'EXEC CICS LINK FAILED'.",
    "01  WS-B             PIC X(02).",
], [
    "0000-MAIN.",
    "    MOVE 'XY' TO WS-B",
    "    PERFORM 1000-NEXT.",
    "1000-NEXT.",
    "    GOBACK.",
])
SQLMSG = program("SQLMSG", [
    "01  WS-TXT           PIC X(08) VALUE \"EXEC SQL\".",
    "01  WS-SEP           PIC X(20) VALUE ' -- END -- '.",
    "01  WS-AFTER         PIC X(02).",
], [
    "0000-MAIN.",
    "    DISPLAY 'EXEC SQL FAILED'.",
    "    MOVE WS-SEP TO WS-AFTER.",
    "1000-NEXT.",
    "    GOBACK.",
])
# one sentence: a literal with the words, then a real block
ONESENT = program("ONESENT", [
    "01  WS-MSG           PIC X(30).",
    "01  WS-X             PIC X.",
], [
    "0000-MAIN.",
    "    IF WS-X = 'A'",
    "        MOVE 'EXEC SQL FAILED' TO WS-MSG",
    "        EXEC SQL ROLLBACK END-EXEC",
    "    END-IF",
    "    IF WS-X = 'B'",
    "        MOVE 'EXEC CICS LINK FAILED' TO WS-MSG",
    "        EXEC CICS RETURN END-EXEC",
    "    END-IF",
    "    MOVE 'EXEC DLI GU' TO WS-MSG",
    "    DISPLAY 'EXEC SQL INCLUDE NOSUCH END-EXEC'.",
    "    GOBACK.",
])
MSGBOOK = ("       01  MSG-TABLE.\n"
           "           05  MSG-1              PIC X(20) VALUE 'EXEC DLI GU FAILED'.\n"
           "           05  MSG-2              PIC X(20) VALUE 'EXEC SQL'.\n"
           "           05  MSG-3              PIC X(04).\n")


def line_of(text, needle):
    for i, ln in enumerate(text.splitlines(), 1):
        if needle in ln:
            return i
    raise AssertionError(needle)


class TheStatements(unittest.TestCase):

    def test_a_data_item_after_the_literal_is_its_own_statement(self):
        lines = reader.read_cobol_lines("".join(f"       {r}\n" for r in [
            "01  WS-ERR PIC X(30) VALUE 'EXEC CICS LINK FAILED'.", "01  WS-B PIC X(02).",
            "01  WS-C PIC X(08) VALUE \"EXEC SQL\".", "01  WS-D PIC X."]))[0]
        got = [s.text for s in reader.cobol_statements(reader.join_cobol_continuations(lines))]
        self.assertEqual(got, ["01  WS-ERR PIC X(30) VALUE 'EXEC CICS LINK FAILED'.", "01  WS-B PIC X(02).",
                               "01  WS-C PIC X(08) VALUE \"EXEC SQL\".", "01  WS-D PIC X."])

    def test_a_real_block_still_holds_its_periods(self):
        text = "".join(f"       {r}\n" for r in [
            "01  WS-A PIC X.", "    EXEC SQL DECLARE C1 CURSOR FOR", "      SELECT A FROM T WHERE B = 'X. Y'",
            "      -- the rows. all of them", "    END-EXEC.", "01  WS-B PIC X."])
        lines = reader.read_cobol_lines(text)[0]
        got = [s.text for s in reader.cobol_statements(reader.join_cobol_continuations(lines))]
        self.assertEqual(len(got), 3, got)
        self.assertTrue(got[1].startswith("EXEC SQL DECLARE C1") and got[1].endswith("END-EXEC."), got)

    def test_the_sql_comment_state_reads_no_literal(self):
        lines = reader.read_cobol_lines(SQLMSG)[0]
        sep = lines[line_of(SQLMSG, "WS-SEP ") - 1]
        self.assertEqual(sep.code.strip(), "01  WS-SEP           PIC X(20) VALUE ' -- END -- '.")
        # a literal the line does not close (it goes on, on a continuation line) is text too
        self.assertEqual(reader.code_outside_literals("01 A PIC X(40) VALUE 'EXEC SQL AND MORE"), "01 A PIC X(40) VALUE ")
        self.assertEqual(reader.code_outside_literals("EXEC SQL SELECT 'A' INTO :X"), "EXEC SQL SELECT     INTO :X")


class TheProgramFacts(unittest.TestCase):

    def test_the_paragraphs_and_items_after_a_data_literal(self):
        for text, ref in ((LNKMSG, ("WS-B", "write")), (SQLMSG, ("WS-AFTER", "write"))):
            f = cobol.parse_program(text)
            self.assertEqual([p.name for p in f.paragraphs], ["0000-MAIN", "1000-NEXT"])
            self.assertIn(ref, [(r[0], r[1]) for r in f.field_refs])
        f = cobol.parse_program(LNKMSG)
        self.assertEqual(f.cics, [])
        self.assertIn(("0000-MAIN", "1000-NEXT", None, line_of(LNKMSG, "PERFORM 1000-NEXT"), "perform"), f.performs)

    def test_a_real_block_after_a_literal_in_one_sentence(self):
        f = cobol.parse_program(ONESENT)
        self.assertEqual([(s.stmt_type, s.start_line) for s in f.sql], [("ROLLBACK", line_of(ONESENT, "ROLLBACK"))])
        self.assertEqual(f.cics, [("RETURN", "RETURN", line_of(ONESENT, "EXEC CICS RETURN"))])
        self.assertEqual(f.calls, [])
        self.assertEqual(f.dli, [])
        self.assertEqual(f.copies, [], "an INCLUDE in a literal is no copy")
        writes = sorted(r[3] for r in f.field_refs if r[0] == "WS-MSG" and r[1] == "write")
        self.assertEqual(writes, [line_of(ONESENT, "'EXEC SQL FAILED'"), line_of(ONESENT, "'EXEC CICS LINK FAILED'"),
                                  line_of(ONESENT, "'EXEC DLI GU'")])
        self.assertEqual(sorted(x.src_lit for x in f.flows if x.dst_name == "WS-MSG"),
                         ["'EXEC CICS LINK FAILED'", "'EXEC DLI GU'", "'EXEC SQL FAILED'"])

    def test_a_copybook_item_after_a_literal(self):
        lines = reader.read_cobol_lines(MSGBOOK)[0]
        roots, _w = copybook.parse_data_division(reader.join_cobol_continuations(lines))
        self.assertEqual([(c.name, c.offset, c.length) for c in roots[0].children],
                         [("MSG-1", 0, 20), ("MSG-2", 20, 20), ("MSG-3", 40, 4)])


class InTheIndex(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate")
        for rel, text in (("GC/PROD.GC.SRC/LNKMSG.cbl", LNKMSG), ("GC/PROD.GC.SRC/SQLMSG.cbl", SQLMSG),
                          ("GC/PROD.GC.SRC/ONESENT.cbl", ONESENT), ("GC/PROD.GC.COPYLIB/MSGBOOK.cpy", MSGBOOK)):
            p = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            assert build._main([root, "--db", cls.db, "--quiet", "--rebuild"]) == 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_the_pages(self):
        conn = query.connect(self.db)
        try:
            page = query.cmd_program(conn, "LNKMSG")
            self.assertIn("parse: ok", page)
            self.assertIn("- 2 paragraphs;", page)
            self.assertIn("| LNKMSG | cobol | 1 | X(02) |", query.cmd_field(conn, "WS-B"))
            self.assertIn("| SQLMSG | cobol | 1 | X(02) |", query.cmd_field(conn, "WS-AFTER"))
            self.assertIn("- 2 paragraphs;", query.cmd_program(conn, "SQLMSG"))
            page = query.cmd_program(conn, "ONESENT")
            self.assertNotIn("NOSUCH", page)
            self.assertNotIn("LINK", page.split("### Structure")[0])
            self.assertIn("| MSGBOOK | copybook | 5 | X(04) |  | 40 | 4 |", query.cmd_field(conn, "MSG-3"))
        finally:
            conn.close()


class TheDocsSayIt(unittest.TestCase):

    def test_the_docs(self):
        def read(*path):
            with open(os.path.join(ROOT, *path), encoding="utf-8") as fh:
                return fh.read()
        roadmap = " ".join(read("ROADMAP.md").split())
        self.assertIn("28. **Delivered in the batch - the words EXEC SQL / CICS / DLI inside a literal are text.**", roadmap)
        lessons = read("LESSONS.md")
        self.assertIn("| 217 | Not seen on his estate - found by the verifier in passing on ROADMAP re-parse item 27", lessons)
        self.assertIn("Rule for me: every keyword scan runs over the code outside literals", lessons)
        self.assertIn("the words `EXEC CICS` in an error-message literal opening a block", read("README.md"))
        self.assertIn("LESSONS 217", read("atlas", "reader.py"))
        self.assertIn("LESSONS 217", read("atlas", "cobol.py"))


if __name__ == "__main__":
    unittest.main()
