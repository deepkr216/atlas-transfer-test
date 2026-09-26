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

More of the family, found by the verifier's second round on item 27 and the same on main: the OF / IN qualifier
blanking of cobol.py ran inside literals, so `MOVE 'END OF FILE' TO WS-M` stored the literal 'END ' (LESSONS 220);
and the reader cut an EXEC SQL line at ' --' inside an SQL literal, and never cut a comment on the EXEC SQL line or a
`/* */` one - the open literal, or an apostrophe in the comment, ate the rest of the program (LESSONS 221).
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
# message literals holding OF / IN, and a real qualifier beside them (LESSONS 220)
OFLIT = program("OFLIT", [
    "01  WS-M             PIC X(40).",
    "01  WS-N             PIC X(20) VALUE 'END OF FILE'.",
    "01  WS-REC.",
    "    05  WS-K         PIC X(02).",
    "01  WS-X             PIC X.",
], [
    "0000-MAIN.",
    "    MOVE 'END OF FILE' TO WS-M",
    "    IF WS-M = 'LACK OF FUNDS'",
    "        DISPLAY 'CHECK IN PROGRESS'",
    "    END-IF",
    "    EVALUATE WS-X",
    "        WHEN 'A'",
    "            MOVE 'RECORD NOT IN FILE' TO WS-M",
    "    END-EVALUATE",
    "    MOVE 'THE CALL OF EXEC CICS LINK FAILED ON THE PROGRAM KY",
    "-    'SUB01 TODAY' TO WS-M",
    "    MOVE 'IN' TO WS-K OF WS-REC",
    "    MOVE WS-K IN WS-REC TO WS-M.",
    "    GOBACK.",
]).replace("       -    'SUB01", "      -    'SUB01")      # the hyphen in column 7: a continuation line
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


class QualifierWordsInALiteral(unittest.TestCase):
    """The OF / IN qualifier blanking ran over the literals too: `MOVE 'END OF FILE' TO WS-M` kept the literal 'END ',
    `literal "END OF FILE"` found no MOVE of it (LESSONS 220)."""

    def test_blank_qualifiers(self):
        text = "MOVE 'END OF FILE' TO WS-K OF WS-REC"
        self.assertEqual(cobol.blank_qualifiers(text), text[:-len("OF WS-REC")] + " " * len("OF WS-REC"))
        self.assertEqual(cobol.blank_qualifiers("IF A = \"CHECK IN PROGRESS\" OR B IN C"),
                         "IF A = \"CHECK IN PROGRESS\" OR B     ")
        # a literal the text leaves open (it goes on, on a continuation line) is text to its end
        self.assertEqual(cobol.blank_qualifiers("DISPLAY 'RECORD NOT IN FILE"), "DISPLAY 'RECORD NOT IN FILE")
        self.assertEqual(cobol.blank_qualifiers("MOVE WS-Q-IN TO WM-STAT"), "MOVE WS-Q-IN TO WM-STAT")
        self.assertEqual(cobol.blank_qualifiers("MOVE 'IT''S IN' TO X IN Y"), "MOVE 'IT''S IN' TO X     ")

    def test_the_literals_keep_their_words(self):
        f = cobol.parse_program(OFLIT)
        got = [(lit, how, tgt, ln) for lit, how, tgt, ln in f.literal_refs]
        self.assertIn(("END OF FILE", "move_to", "WS-M", line_of(OFLIT, "MOVE 'END OF FILE'")), got)
        self.assertIn(("LACK OF FUNDS", "compare", "WS-M", line_of(OFLIT, "'LACK OF FUNDS'")), got)
        self.assertIn(("CHECK IN PROGRESS", "display", None, line_of(OFLIT, "'CHECK IN PROGRESS'")), got)
        self.assertIn(("RECORD NOT IN FILE", "move_to", "WS-M", line_of(OFLIT, "'RECORD NOT IN FILE'")), got)
        long = [lit for lit, how, tgt, _ln in got if lit.startswith("THE CALL")]
        self.assertEqual(len(long), 1, got)
        self.assertTrue(long[0].startswith("THE CALL OF EXEC CICS LINK FAILED ON THE PROGRAM KY"), long)
        self.assertTrue(long[0].endswith("SUB01 TODAY"), long)
        self.assertIn(("IN", "move_to", "WS-K", line_of(OFLIT, "MOVE 'IN' TO WS-K")), got)
        # a real qualifier is still no reference of its own
        refs = [(n, mode) for n, mode, _v, _ln in f.field_refs]
        self.assertIn(("WS-K", "write"), refs)
        self.assertIn(("WS-K", "read"), refs)
        self.assertNotIn("WS-REC", [n for n, _m in refs])
        self.assertEqual(f.cics, [], "the words EXEC CICS LINK in the literal are text (LESSONS 217)")


def sql_program(name, block):
    """A program whose 0000-MAIN holds `block` (an EXEC SQL statement, one row a line), then PERFORM 1000-X; 1000-X
    moves WS-B to WS-C - lost when the block eats the rest of the program."""
    return program(name, ["01  WS-B  PIC X(02).", "01  WS-C  PIC X(02)."],
                   ["0000-MAIN."] + block + ["    PERFORM 1000-X.", "    GOBACK.", "1000-X.", "    MOVE WS-B TO WS-C."])


# the verifier's three (LESSONS 221), and more of the same
SQLLIT = sql_program("SQLLIT", ["    EXEC SQL", "        UPDATE KV.TBL SET D = ' -- ' WHERE C = :WS-C", "    END-EXEC."])
SQLOPEN = sql_program("SQLOPEN", ["    EXEC SQL -- get the customer's row", "        SELECT A INTO :WS-B FROM KY.TBL",
                                  "    END-EXEC."])
SQLNOTE = sql_program("SQLNOTE", ["    EXEC SQL", "        SELECT A INTO :WS-B FROM KY.TBL",
                                  "    /* the customer's row */ END-EXEC."])
SQLLONG = sql_program("SQLLONG", ["    EXEC SQL /* the customer's row,", "       read once -- it's the master's */",
                                  "        SELECT A INTO :WS-B FROM KY.TBL WHERE C = 'IT''S -- X' -- the key's",
                                  "    END-EXEC."])


class SqlCommentsOutsideLiterals(unittest.TestCase):
    """The reader cut an EXEC SQL line at ' --' inside an SQL literal, and never cut a comment on the EXEC SQL line or
    a `/* */` one: the apostrophe or the open literal ate the rest of the program, which read 'parse: ok' (LESSONS
    221)."""

    def test_sql_comments_out(self):
        out = reader.sql_comments_out
        self.assertEqual(out("    UPDATE T SET D = ' -- ' WHERE C = :X"), ("    UPDATE T SET D = ' -- ' WHERE C = :X", False))
        self.assertEqual(out("    WHERE C = 'X' -- the customer's code"), ("    WHERE C = 'X'", False))
        self.assertEqual(out("    -- a comment line"), ("", False))
        self.assertEqual(out("EXEC SQL -- get the customer's row", 8), ("EXEC SQL", False))
        self.assertEqual(out("EXEC SQL -- a comment", 0), ("EXEC SQL", False))
        self.assertEqual(out("    /* the customer's row */ END-EXEC."), ("                             END-EXEC.", False))
        self.assertEqual(out("    SELECT A /* the customer's"), ("    SELECT A", True))
        self.assertEqual(out("    row, it's */ FROM T", 0, True), ("                 FROM T", False))
        self.assertEqual(out("    still a note's words", 0, True), ("", True))
        self.assertEqual(out("    no close'here END-EXEC.", 0, True), ("                  END-EXEC.", False),
                         "END-EXEC ends the block and a comment left open in it")
        self.assertEqual(out("    SET D = 'IT''S -- X' -- the key's"), ("    SET D = 'IT''S -- X'", False))
        self.assertEqual(out("    SET D = 'OPEN -- ON THE NEXT LINE"), ("    SET D = 'OPEN -- ON THE NEXT LINE", False),
                         "a literal the line leaves open goes on: nothing after its quote is looked at")
        self.assertEqual(out("    END-EXEC -- not SQL any more"), ("    END-EXEC -- not SQL any more", False))
        self.assertEqual(out("    A--B"), ("    A--B", False), "`--` after no blank is not a comment, as before")

    def test_the_program_after_the_block(self):
        for text in (SQLLIT, SQLOPEN, SQLNOTE, SQLLONG):
            f = cobol.parse_program(text)
            name = f.program_id
            self.assertEqual([p.name for p in f.paragraphs], ["0000-MAIN", "1000-X"], name)
            self.assertIn(("WS-C", "write", "MOVE", line_of(text, "MOVE WS-B TO WS-C")),
                          [tuple(r) for r in f.field_refs], name)
            self.assertEqual(len(f.sql), 1, name)
        self.assertIn("' -- '", cobol.parse_program(SQLLIT).sql[0].text)
        self.assertEqual(cobol.parse_program(SQLOPEN).sql[0].stmt_type, "SELECT")
        self.assertIn("WS-B", cobol.parse_program(SQLNOTE).sql[0].host_vars)
        long = cobol.parse_program(SQLLONG).sql[0]
        self.assertIn("'IT''S -- X'", long.text)
        self.assertNotIn("master", long.text)
        self.assertNotIn("key's", long.text)
        # the comment lines and words are gone from the code, the source lines stay as written
        lines = reader.read_cobol_lines(SQLLONG)[0]
        second = lines[line_of(SQLLONG, "read once") - 1]
        self.assertTrue(second.is_comment)
        self.assertIn("it's the master's", second.raw)


class InTheIndex(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate")
        for rel, text in (("GC/PROD.GC.SRC/LNKMSG.cbl", LNKMSG), ("GC/PROD.GC.SRC/SQLMSG.cbl", SQLMSG),
                          ("GC/PROD.GC.SRC/ONESENT.cbl", ONESENT), ("GC/PROD.GC.COPYLIB/MSGBOOK.cpy", MSGBOOK),
                          ("GC/PROD.GC.SRC/OFLIT.cbl", OFLIT)):
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

    def test_a_literal_holding_of(self):
        # `literal "END OF FILE"` showed only the VALUE clause and 'Set (MOVE / STRING): _none_' (LESSONS 220)
        conn = query.connect(self.db)
        try:
            page = query.cmd_literal(conn, "END OF FILE")
            moves = page.split("### Set (MOVE / STRING)\n")[1].split("\n### ")[0]
            self.assertNotIn("_none_", moves)
            self.assertIn("WS-M", moves)
            self.assertIn(f"OFLIT:{line_of(OFLIT, 'MOVE ' + chr(39) + 'END OF FILE')}", moves)
            self.assertIn("OFLIT", query.cmd_literal(conn, "LACK OF FUNDS"))
            self.assertIn("OFLIT", query.cmd_literal(conn, "RECORD NOT IN FILE"))
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
        # the qualifier blanking (LESSONS 220)
        self.assertIn("**More of the family, found by the verifier's second round on item 27 (LESSONS 220)", roadmap)
        self.assertIn("| 220 | Not seen on his estate - found by the verifier in passing on ROADMAP re-parse item 27",
                      lessons)
        self.assertIn("Rule for me: the rule of row 217 holds for every blanking pass", lessons)
        self.assertIn("LESSONS 220", read("atlas", "cobol.py"))
        # the SQL comment cut (LESSONS 221)
        self.assertIn("The reader's SQL comment cut (LESSONS 221) ignored literals too", roadmap)
        self.assertIn("| 221 | Not seen on his estate - found by the verifier in passing on ROADMAP re-parse item 27",
                      lessons)
        self.assertIn("Rule for me: a cut made on a line's text is made outside its literals", lessons)
        self.assertIn("LESSONS 221", read("atlas", "reader.py"))
        self.assertIn("an SQL `--` read inside an SQL literal (`SET D = ' -- '`)", read("README.md"))


if __name__ == "__main__":
    unittest.main()
