r"""
The synthetic estate's findings in the COBOL parser and the reader (docs/SYNTH-findings-2026-09-25.md, the minimal
reproductions under tools/synth/repro/; LESSONS 211). Each shape is one sentence - or one name - the parser read
wrongly, and each fact it cost is pinned here beside the reproduction that showed it.

F01 - several CBLTDLI calls in one sentence. IMS programs write GU, then CHKP, then GN without a period between them;
only the first call was kept, cited at the sentence's first line, with the rest of the sentence read as its SSAs (the
report's F43-F46: 29 calls lost). Every call is now indexed at its own line with its own USING list, an ordinary CALL
in the same sentence is still a call edge (the whole sentence used to be skipped), and several MQ calls of a sentence
each keep their own line.

F02 - several EXEC SQL statements in one sentence. Every statement, table verb, column row and host-variable reference
was cited at the sentence's first line (the report's F47, 27 cites, and the field-reference cites F04-F10, F14): each
statement is now cited at its own EXEC SQL line, and its host variables with it.

F04 - a data name beginning with END-. The verb list held `END-[A-Z]+`, so END-DATE, END-TIME and END-BALANCE cut the
statement: `IF END-DATE < START-DATE` lost both references, `MOVE WS-PURGE-DATE TO END-DATE` its read and its write
(the report's F11, F12, F15, F19). The scope terminators are now an exact word list, used by every pattern that had
the prefix: a USING list (`USING WS-END-DATE` gave an argument named `WS-`), a CLOSE inside an IF (END-IF was a file
closed), a dynamic CALL through END-PGM or IN-PGM-NAME (the call, or its literal, vanished), a GO TO ... DEPENDING
naming END-RTN, a SORT USING ONLINE-FILE.

F14 - a SELECT / FD file name and an intrinsic FUNCTION name recorded as field references (the report's F18, F20):
OPEN / CLOSE name files only, START and DELETE name the file first, a file name is never a data item, and the name
after FUNCTION is the function's. SORT / MERGE / CANCEL / ALTER / ENTRY joined the verb list: a SORT after a MOVE in
one sentence made SORT, the sort file, ASCENDING and the files targets of the MOVE.

F15 - an EXEC SQL in WORKING-STORAGE whose END-EXEC lacks its period. Read as one sentence the block swallowed the
PROCEDURE DIVISION header and the program lost every paragraph while it read `parse: ok`. The reader closes the
statement at the END-EXEC (reader.cobol_statements), the member is `partial` with the note 'EXEC SQL at line N has no
period after its END-EXEC; what follows was read as if it had one' (the copybook's line and name when the block came
from a COPY), and coverage's kinds table says what it means. Inside the PROCEDURE DIVISION an END-EXEC without a period
is ordinary and nothing is said. The note says nothing of the compile: whether a compile rejects the member depends on
the step that read the block (a separate precompiler, or the compiler's own SQL option), which the member does not say
(LESSONS 212).

F25 - `program`'s Calls table printed a CICS XCTL through a variable holding one literal as the target alone: the
variable (WS-NEXT-PGM) is now beside the targets it resolves to, as for a CALL through a variable.

The stand-ins of this week (partial_kind, is_truly_partial, the chosen-among-several row, recover's arrived /
misfiled / re-file / disk-check steps) find nothing to do on an index holding these members and call the F15 member
truly partial.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from atlas import build, cobol, copybook, query, reader, recover  # noqa: E402

REPRO = os.path.join(ROOT, "tools", "synth", "repro")
NOTE = "EXEC SQL at line {} has no period after its END-EXEC; what follows was read as if it had one"


def program(name, data, proc, env=""):
    """A fixed-format program: `data` the WORKING-STORAGE lines, `proc` the PROCEDURE DIVISION's, each without its
    seven leading columns (a line starting with a letter or digit sits in area A, one indented four more in area B)."""
    def lines(block):
        return "".join(f"       {ln}\n" for ln in block)
    return ("       IDENTIFICATION DIVISION.\n"
            f"       PROGRAM-ID. {name}.\n"
            + lines(env)
            + "       DATA DIVISION.\n"
            + lines(data)
            + "       PROCEDURE DIVISION.\n"
            + lines(proc))


def line_of(text, needle):
    """1-based number of the first line of `text` holding `needle`."""
    for i, ln in enumerate(text.splitlines(), 1):
        if needle in ln:
            return i
    raise AssertionError(f"{needle!r} not in the program")


def refs(f):
    return {(n, m, s, ln) for (n, m, s, ln) in f.field_refs}


# ---------------------------------------------------------------------------
# F01 - DL/I and MQ calls in one sentence
# ---------------------------------------------------------------------------

DLI = program("DLIPGM", [
    "WORKING-STORAGE SECTION.",
    "01  WS-GU            PIC X(04) VALUE 'GU  '.",
    "01  WS-GN            PIC X(04) VALUE 'GN  '.",
    "01  WS-CHKP          PIC X(04) VALUE 'CHKP'.",
    "01  WS-CHKP-ID       PIC X(08) VALUE 'DLIPGM01'.",
    "01  WS-ROOT-AREA     PIC X(120).",
    "01  WS-SSA           PIC X(40).",
    "LINKAGE SECTION.",
    "01  IO-PCB           PIC X(40).",
    "01  DB-PCB           PIC X(40).",
], [
    "0000-MAIN.",
    "    MOVE SPACES TO WS-ROOT-AREA",
    "    CALL 'CBLTDLI' USING WS-GU DB-PCB WS-ROOT-AREA WS-SSA",
    "    DISPLAY 'READ ONE'",
    "    CALL 'CBLTDLI' USING WS-CHKP IO-PCB WS-CHKP-ID",
    "    CALL 'SUBPGM' USING WS-ROOT-AREA",
    "    IF WS-ROOT-AREA = SPACES",
    "        CALL 'CBLTDLI' USING WS-GN DB-PCB WS-ROOT-AREA",
    "    END-IF",
    "    GOBACK.",
])


class DliCallsInOneSentence(unittest.TestCase):

    def setUp(self):
        self.f = cobol.parse_program(DLI)
        self.gu, self.chkp, self.sub = (line_of(DLI, "WS-GU DB-PCB"), line_of(DLI, "WS-CHKP IO-PCB"),
                                        line_of(DLI, "'SUBPGM'"))
        self.gn = line_of(DLI, "WS-GN DB-PCB")

    def test_every_call_at_its_own_line_with_its_own_arguments(self):
        self.assertEqual([(d.func, d.pcb_arg, d.io_area, d.ssa_args, d.line) for d in self.f.dli],
                         [("GU", "DB-PCB", "WS-ROOT-AREA", ["WS-SSA"], self.gu),
                          ("CHKP", "IO-PCB", "WS-CHKP-ID", [], self.chkp),
                          ("GN", "DB-PCB", "WS-ROOT-AREA", [], self.gn)])
        self.assertEqual([(t, op, ln) for (t, k, op, ln) in self.f.io_ops if k == "ims"],
                         [("DB-PCB", "GU", self.gu), ("IO-PCB", "CHKP", self.chkp), ("DB-PCB", "GN", self.gn)])

    def test_the_io_area_flows_at_the_calls_line_with_its_guard(self):
        rows = [(x.kind, x.dst_name, x.line, x.guard) for x in self.f.flows if x.kind == "dli_in"]
        self.assertEqual(rows, [("dli_in", "WS-ROOT-AREA", self.gu, None),
                                ("dli_in", "WS-ROOT-AREA", self.gn, "WS-ROOT-AREA = SPACES")])

    def test_an_ordinary_call_in_the_same_sentence_is_still_a_call(self):
        self.assertEqual([(c.kind, c.target, c.line) for c in self.f.calls], [("static", "SUBPGM", self.sub)])
        self.assertIn(("WS-CHKP-ID", "read", "CALL-USING", self.chkp), refs(self.f))

    def test_mq_calls_in_one_sentence_keep_their_lines(self):
        src = program("MQPGM", [
            "WORKING-STORAGE SECTION.",
            "01  HCONN PIC S9(9) BINARY.", "01  HOBJ PIC S9(9) BINARY.", "01  OPTS PIC S9(9) BINARY.",
            "01  CC PIC S9(9) BINARY.", "01  RC PIC S9(9) BINARY.", "01  BUFLEN PIC S9(9) BINARY.",
            "01  MQOD. 05 MQOD-OBJECTNAME PIC X(48) VALUE 'POL.OUT.Q'.", "01  MQMD PIC X(364).",
            "01  PMO PIC X(152).", "01  WS-MSG PIC X(200).",
        ], [
            "0000-MAIN.",
            "    CALL 'MQOPEN' USING HCONN MQOD OPTS HOBJ CC RC",
            "    CALL 'MQPUT' USING HCONN HOBJ MQMD PMO BUFLEN WS-MSG CC RC",
            "    GOBACK.",
        ])
        f = cobol.parse_program(src)
        self.assertEqual([(c, q, ln) for (c, q, _d, _l, ln) in f.mq],
                         [("MQOPEN", "POL.OUT.Q", line_of(src, "'MQOPEN'")), ("MQPUT", "POL.OUT.Q", line_of(src, "'MQPUT'"))])
        self.assertEqual([(x.kind, x.src_name, x.line) for x in f.flows if x.kind == "mq_out"],
                         [("mq_out", "WS-MSG", line_of(src, "'MQPUT'"))])
        self.assertEqual(f.calls, [])


# ---------------------------------------------------------------------------
# F02 - EXEC SQL statements in one sentence
# ---------------------------------------------------------------------------

SQL = program("SQLPGM", [
    "WORKING-STORAGE SECTION.",
    "01  WS-STATUS        PIC X(02).",
    "01  WS-KEY           PIC X(12).",
    "01  WS-AMT           PIC S9(09)V99 COMP-3.",
], [
    "0000-MAIN.",
    "    MOVE 'AC' TO WS-STATUS",
    "    EXEC SQL",
    "        SELECT PREMIUM_AMT INTO :WS-AMT",
    "          FROM PRD.POLICY_TBL WHERE POLICY_NO = :WS-KEY",
    "    END-EXEC",
    "    EXEC SQL",
    "        UPDATE PRD.POLICY_TBL SET STATUS_CD = :WS-STATUS",
    "         WHERE POLICY_NO = :WS-KEY",
    "    END-EXEC",
    "    GOBACK.",
    "1000-ALONE.",
    "    EXEC SQL DELETE FROM PRD.POLICY_TBL",
    "         WHERE POLICY_NO = :WS-KEY END-EXEC.",
])


class SqlInOneSentence(unittest.TestCase):

    def setUp(self):
        self.f = cobol.parse_program(SQL)
        lines = [i for i, ln in enumerate(SQL.splitlines(), 1) if "EXEC SQL" in ln]
        ends = [i for i, ln in enumerate(SQL.splitlines(), 1) if "END-EXEC" in ln]
        self.sel, self.upd, self.dele = lines
        self.sel_end, self.upd_end, self.dele_end = ends

    def test_each_statement_at_its_own_exec_line(self):
        self.assertEqual([(s.stmt_type, s.start_line, s.end_line) for s in self.f.sql],
                         [("SELECT", self.sel, self.sel_end), ("UPDATE", self.upd, self.upd_end),
                          ("DELETE", self.dele, self.dele_end)])
        self.assertEqual([(t, op, ln) for (t, k, op, ln) in self.f.io_ops if k == "db2"],
                         [("PRD.POLICY_TBL", "SELECT", self.sel), ("PRD.POLICY_TBL", "UPDATE", self.upd),
                          ("PRD.POLICY_TBL", "DELETE", self.dele)])

    def test_host_variables_and_columns_at_their_statements_line(self):
        got = {(n, m, ln) for (n, m, s, ln) in self.f.field_refs if s == "EXEC-SQL"}
        self.assertEqual(got, {("WS-AMT", "write", self.sel), ("WS-KEY", "read", self.sel),
                               ("WS-STATUS", "read", self.upd), ("WS-KEY", "read", self.upd),
                               ("WS-KEY", "read", self.dele)})
        self.assertEqual({(c, m, ln) for (_t, c, _h, m, _s, ln) in self.f.sql_cols},
                         {("PREMIUM_AMT", "read", self.sel), ("POLICY_NO", "predicate", self.sel),
                          ("STATUS_CD", "write", self.upd), ("POLICY_NO", "predicate", self.upd),
                          ("POLICY_NO", "predicate", self.dele)})


# ---------------------------------------------------------------------------
# F04 - names beginning with END-
# ---------------------------------------------------------------------------

ENDS = program("ENDPGM", [
    "WORKING-STORAGE SECTION.",
    "01  START-DATE       PIC 9(08).",
    "01  END-DATE         PIC 9(08).",
    "01  END-TIME         PIC 9(06).",
    "01  END-BALANCE      PIC S9(09)V99 COMP-3.",
    "01  WS-END-DATE      PIC 9(08).",
    "01  WS-PURGE-DATE    PIC 9(08).",
    "01  WS-DAYS          PIC S9(05) COMP.",
    "01  END-PGM          PIC X(08).",
    "01  IN-PGM-NAME      PIC X(08).",
    "01  WS-EOF           PIC X.",
    "01  WS-I             PIC 9.",
], [
    "0000-MAIN.",
    "    MOVE WS-PURGE-DATE TO END-DATE",
    "    IF END-DATE < START-DATE",
    "        MOVE ZERO TO WS-DAYS",
    "    END-IF",
    "    ADD 1 TO END-BALANCE",
    "    MOVE END-TIME TO WS-DAYS",
    "    CALL 'CMNDATE' USING START-DATE WS-END-DATE",
    "    MOVE 'PGMA' TO END-PGM",
    "    CALL END-PGM USING WS-DAYS",
    "    MOVE 'PGMB' TO IN-PGM-NAME",
    "    CALL IN-PGM-NAME",
    "    PERFORM END-RTN UNTIL WS-EOF = 'Y'",
    "    GO TO 100-A END-RTN DEPENDING ON WS-I.",
    "100-A.",
    "    EXIT.",
    "END-RTN.",
    "    MOVE 'Y' TO WS-EOF.",
])


class NamesBeginningWithEnd(unittest.TestCase):

    def setUp(self):
        self.f = cobol.parse_program(ENDS)
        self.at = lambda needle: line_of(ENDS, needle)

    def test_end_date_is_read_written_and_tested(self):
        r = refs(self.f)
        for row in (("WS-PURGE-DATE", "read", "MOVE", self.at("TO END-DATE")),
                    ("END-DATE", "write", "MOVE", self.at("TO END-DATE")),
                    ("END-DATE", "test", "IF", self.at("IF END-DATE")),
                    ("START-DATE", "test", "IF", self.at("IF END-DATE")),
                    ("END-BALANCE", "write", "ADD", self.at("TO END-BALANCE")),
                    ("END-TIME", "read", "MOVE", self.at("MOVE END-TIME")),
                    ("END-PGM", "write", "MOVE", self.at("TO END-PGM")),
                    ("WS-EOF", "test", "PERFORM", self.at("PERFORM END-RTN"))):
            self.assertIn(row, r)
        guard = [x.guard for x in self.f.flows if x.dst_name == "WS-DAYS" and x.line == self.at("MOVE ZERO")]
        self.assertEqual(guard, ["END-DATE < START-DATE"])
        self.assertIn(("Y", "compare", "WS-EOF", self.at("PERFORM END-RTN")), self.f.literal_refs)

    def test_calls_through_and_with_such_names(self):
        self.assertEqual([(c.kind, c.target, c.via_var, c.resolved, c.using_args, c.line) for c in self.f.calls],
                         [("static", "CMNDATE", None, ["CMNDATE"], ["START-DATE", "WS-END-DATE"], self.at("'CMNDATE'")),
                          ("dynamic", None, "END-PGM", ["PGMA"], ["WS-DAYS"], self.at("CALL END-PGM")),
                          ("dynamic", None, "IN-PGM-NAME", ["PGMB"], [], self.at("CALL IN-PGM-NAME"))])

    def test_paragraphs_and_edges_named_end(self):
        self.assertEqual([p.name for p in self.f.paragraphs], ["0000-MAIN", "100-A", "END-RTN"])
        edges = [(t, k, ln) for (_fr, t, _th, ln, k) in self.f.performs if k != "fallthrough"]
        self.assertEqual(edges, [("END-RTN", "perform", self.at("PERFORM END-RTN")),
                                 ("100-A", "goto_depending", self.at("GO TO")),
                                 ("END-RTN", "goto_depending", self.at("GO TO"))])

    def test_the_terminators_still_end_their_scope(self):
        for word in cobol.SCOPE_TERMINATORS:
            self.assertEqual([v for (v, _f, _o) in cobol._split_verbs(f"MOVE A TO B {word} MOVE C TO D")],
                             ["MOVE", word, "MOVE"], word)
        for name in ("END-DATE", "END-TIME", "END-BALANCE", "END-OF-JOB", "END-RTN"):
            self.assertEqual([v for (v, _f, _o) in cobol._split_verbs(f"MOVE A TO {name} MOVE C TO D")],
                             ["MOVE", "MOVE"], name)
        src = program("CLOSEPGM", ["WORKING-STORAGE SECTION.", "01  WS-X PIC X.", "01  WS-Y PIC X."], [
            "0000-MAIN.",
            "    IF WS-X = 'A'",
            "        CLOSE IN-FILE",
            "    END-IF",
            "    MOVE WS-X TO WS-Y",
            "    SORT SD-FILE ON ASCENDING KEY SD-KEY",
            "         USING IN-FILE ONLINE-FILE GIVING OUTPUT-FILE",
            "    GOBACK.",
        ])
        f = cobol.parse_program(src)
        self.assertEqual([(t, op) for (t, _k, op, _l) in f.io_ops],
                         [("IN-FILE", "CLOSE"), ("IN-FILE", "SORT READ"), ("ONLINE-FILE", "SORT READ"),
                          ("OUTPUT-FILE", "SORT WRITE")])
        self.assertEqual([x.guard for x in f.flows if x.dst_name == "WS-Y"], [None])


class LiteralMovesInOneSentence(unittest.TestCase):
    """Found while writing NamesBeginningWithEnd: the literal map behind a dynamic CALL read a MOVE's target list on
    over the next statement - the second `MOVE 'X'` of a sentence was never seen, and a DISPLAY after a MOVE took the
    literal as if it were a target, with the name it displays."""

    def test_every_literal_move_counts_and_a_verb_is_no_target(self):
        src = program("LITPGM", ["WORKING-STORAGE SECTION.", "01  WS-P PIC X(8).", "01  WS-Q PIC X(8).",
                                 "01  WS-R PIC X(8)."], [
            "0000-MAIN.",
            "    MOVE 'PGMA' TO WS-P",
            "    DISPLAY WS-Q",
            "    MOVE 'PGMB' TO WS-R",
            "    CALL WS-Q",
            "    CALL WS-R",
            "    GOBACK.",
        ])
        lines, _fixed = reader.read_cobol_lines(src)
        stmts = list(reader.cobol_statements(reader.join_cobol_continuations(lines)))
        self.assertEqual(cobol._build_literal_map(stmts), {"WS-P": {"PGMA"}, "WS-R": {"PGMB"}})
        f = cobol.parse_program(src)
        self.assertEqual([(c.via_var, c.resolved, c.resolution) for c in f.calls],
                         [("WS-Q", [], "unresolved"), ("WS-R", ["PGMB"], "move_literal")])


# ---------------------------------------------------------------------------
# F14 - file names and FUNCTION names
# ---------------------------------------------------------------------------

FILES = program("FILEPGM", [
    "FILE SECTION.",
    "FD  IN-FILE.",
    "01  IN-RECORD.",
    "    05  IN-KEY           PIC X(10).",
    "    05  IN-DATA          PIC X(70).",
    "FD  OUT-FILE.",
    "01  OUT-RECORD           PIC X(80).",
    "SD  SD-FILE.",
    "01  SD-REC.",
    "    05  SD-KEY           PIC X(10).",
    "WORKING-STORAGE SECTION.",
    "01  WS-DATE-INT      PIC S9(09) COMP.",
    "01  WS-DATE          PIC 9(08).",
    "01  WS-KEY           PIC X(10).",
    "01  WS-TODAY         PIC X(21).",
    "01  WS-LEN           PIC 9(04).",
    "01  WS-A             PIC S9(04).",
    "01  WS-B             PIC S9(04).",
    "01  WS-MAX           PIC S9(04).",
    "01  WS-PGM           PIC X(08).",
], [
    "0000-MAIN.",
    "    OPEN I-O IN-FILE OUTPUT OUT-FILE",
    "    COMPUTE WS-DATE-INT = FUNCTION INTEGER-OF-DATE(WS-DATE)",
    "    MOVE FUNCTION CURRENT-DATE TO WS-TODAY",
    "    COMPUTE WS-LEN = FUNCTION LENGTH(WS-TODAY)",
    "    COMPUTE WS-MAX = FUNCTION MAX(FUNCTION ABS(WS-A) WS-B)",
    "    START IN-FILE KEY IS NOT LESS THAN WS-KEY",
    "    DELETE IN-FILE RECORD",
    "    MOVE WS-KEY TO IN-KEY",
    "    SORT SD-FILE ON ASCENDING KEY SD-KEY",
    "         USING IN-FILE GIVING OUT-FILE",
    "    MOVE WS-KEY TO IN-KEY",
    "    CANCEL WS-PGM",
    "    CLOSE IN-FILE OUT-FILE",
    "    GOBACK.",
], env=[
    "ENVIRONMENT DIVISION.",
    "INPUT-OUTPUT SECTION.",
    "FILE-CONTROL.",
    "    SELECT IN-FILE ASSIGN TO INFILE",
    "        ORGANIZATION IS INDEXED ACCESS IS DYNAMIC",
    "        RECORD KEY IS IN-KEY.",
    "    SELECT OUT-FILE ASSIGN TO OUTFILE.",
    "    SELECT SD-FILE ASSIGN TO SORTWK.",
])


class FileAndFunctionNames(unittest.TestCase):

    def setUp(self):
        self.f = cobol.parse_program(FILES)
        self.at = lambda needle: line_of(FILES, needle)

    def test_no_file_and_no_function_is_a_field(self):
        names = {n for (n, _m, _s, _l) in self.f.field_refs}
        self.assertFalse(names & {"IN-FILE", "OUT-FILE", "SD-FILE", "INTEGER-OF-DATE", "CURRENT-DATE", "LENGTH",
                                  "MAX", "ABS", "SORT", "ASCENDING", "RECORD"}, names)

    def test_the_data_items_those_statements_name_are(self):
        r = refs(self.f)
        for row in (("WS-DATE", "read", "COMPUTE", self.at("INTEGER-OF-DATE")),
                    ("WS-DATE-INT", "write", "COMPUTE", self.at("INTEGER-OF-DATE")),
                    ("WS-TODAY", "write", "MOVE", self.at("CURRENT-DATE")),
                    ("WS-TODAY", "read", "COMPUTE", self.at("FUNCTION LENGTH")),
                    ("WS-A", "read", "COMPUTE", self.at("FUNCTION MAX")),
                    ("WS-B", "read", "COMPUTE", self.at("FUNCTION MAX")),
                    ("WS-KEY", "read", "START", self.at("START IN-FILE"))):
            self.assertIn(row, r)
        # the MOVE before the SORT and the one before CANCEL write IN-KEY only
        for needle in ("SORT SD-FILE", "CANCEL"):
            ln = self.at(needle) - 1                                    # the MOVE on the line before
            self.assertEqual(sorted((n, m) for (n, m, _s, l) in self.f.field_refs if l == ln),
                             [("IN-KEY", "write"), ("WS-KEY", "read")], needle)
        self.assertEqual([(x.src_name, x.dst_name, x.note) for x in self.f.flows
                          if x.line == self.at("INTEGER-OF-DATE")], [("WS-DATE", "WS-DATE-INT", "INTEGER-OF-DATE")])

    def test_the_file_facts_stay(self):
        self.assertEqual(sorted(((t, op, ln) for (t, _k, op, ln) in self.f.io_ops), key=lambda r: r[2]),
                         [("IN-FILE", "OPEN I-O", self.at("OPEN I-O")), ("OUT-FILE", "OPEN OUTPUT", self.at("OPEN I-O")),
                          ("IN-FILE", "START", self.at("START IN-FILE")), ("IN-FILE", "DELETE", self.at("DELETE IN-FILE")),
                          ("IN-FILE", "SORT READ", self.at("USING IN-FILE")),
                          ("OUT-FILE", "SORT WRITE", self.at("USING IN-FILE")),
                          ("IN-FILE", "CLOSE", self.at("CLOSE IN-FILE")), ("OUT-FILE", "CLOSE", self.at("CLOSE IN-FILE"))])


# ---------------------------------------------------------------------------
# F15 - an EXEC block before the PROCEDURE DIVISION without its period
# ---------------------------------------------------------------------------

NOPERIOD = program("NOPGM", [
    "WORKING-STORAGE SECTION.",
    "01  WS-STATUS        PIC X(02).",
    "    EXEC SQL DECLARE NOCUR1 CURSOR FOR",
    "        SELECT STATUS_CD FROM PRD.NO_TBL",
    "    END-EXEC",
    "01  WS-AFTER         PIC X(04).",
    "    EXEC SQL DECLARE NOCUR2 CURSOR FOR",
    "        SELECT STATUS_CD FROM PRD.NO_TBL",
    "    END-EXEC",
    "    .",
    "01  WS-LAST          PIC X(04).",
], [
    "0000-MAIN.",
    "    MOVE 'AC' TO WS-STATUS",
    "    EXEC SQL OPEN NOCUR1 END-EXEC",
    "    EXEC SQL FETCH NOCUR1 INTO :WS-AFTER END-EXEC",
    "    PERFORM 1000-NEXT",
    "    GOBACK.",
    "1000-NEXT.",
    "    EXEC SQL CLOSE NOCUR1 END-EXEC.",
])

BOOK = ("           EXEC SQL DECLARE PRD.NO_TBL TABLE\n"
        "               ( STATUS_CD CHAR(2) NOT NULL )\n"
        "           END-EXEC\n"
        "       01  DCLNO-TBL.\n"
        "           10  STATUS-CD        PIC X(2).\n")

BOOKPGM = program("BOOKPGM", ["WORKING-STORAGE SECTION.", "    COPY NOBOOK.", "01  WS-X PIC X."],
                  ["0000-MAIN.", "    MOVE STATUS-CD TO WS-X", "    GOBACK."])


class ExecWithoutItsPeriod(unittest.TestCase):

    def statements(self, text):
        lines, _fixed = reader.read_cobol_lines(text)
        return list(reader.cobol_statements(reader.join_cobol_continuations(lines)))

    def test_the_reader_closes_the_statement_at_end_exec(self):
        sts = self.statements(NOPERIOD)
        cut = [st for st in sts if st.no_period]
        self.assertEqual([(st.no_period, st.start, st.end) for st in cut],
                         [(("SQL", line_of(NOPERIOD, "NOCUR1 CURSOR")), line_of(NOPERIOD, "NOCUR1 CURSOR"),
                           line_of(NOPERIOD, "NOCUR1 CURSOR") + 2)])
        self.assertTrue(cut[0].text.endswith("END-EXEC"))
        texts = [st.text for st in sts]
        self.assertIn("01  WS-AFTER         PIC X(04).", texts)
        header = [st for st in sts if st.text == "PROCEDURE DIVISION."]
        self.assertEqual(len(header), 1)
        self.assertTrue(header[0].area_a)

    def test_the_program_keeps_its_procedure_division_and_says_why_it_is_partial(self):
        f = cobol.parse_program(NOPERIOD)
        self.assertEqual([p.name for p in f.paragraphs], ["0000-MAIN", "1000-NEXT"])
        self.assertIn(("0000-MAIN", "1000-NEXT", None, line_of(NOPERIOD, "PERFORM 1000-NEXT"), "perform"), f.performs)
        self.assertEqual([(s.stmt_type, s.start_line) for s in f.sql],
                         [("DECLARE", line_of(NOPERIOD, "NOCUR1 CURSOR")), ("DECLARE", line_of(NOPERIOD, "NOCUR2 CURSOR")),
                          ("OPEN", line_of(NOPERIOD, "OPEN NOCUR1")), ("FETCH", line_of(NOPERIOD, "FETCH NOCUR1")),
                          ("CLOSE", line_of(NOPERIOD, "CLOSE NOCUR1"))])
        self.assertEqual([(k, d, ln) for (k, d, ln) in f.unresolved if k == "exec_no_period"],
                         [("exec_no_period", NOTE.format(line_of(NOPERIOD, "NOCUR1 CURSOR")),
                           line_of(NOPERIOD, "NOCUR1 CURSOR"))])
        roots, _w = copybook.parse_data_division(reader.join_cobol_continuations(reader.read_cobol_lines(NOPERIOD)[0]))
        self.assertEqual([r.name for r in roots], ["WS-STATUS", "WS-AFTER", "WS-LAST"])

    def test_inside_the_procedure_division_nothing_is_cut_or_said(self):
        f = cobol.parse_program(SQL)
        self.assertFalse([u for u in f.unresolved if u[0] == "exec_no_period"])
        sts = self.statements(SQL)
        self.assertFalse([st for st in sts if st.no_period])
        # a data entry that ran on into the PROCEDURE DIVISION header (ROADMAP re-parse item 25): the division is
        # entered all the same, so an END-EXEC without a period after it is procedure code - no note
        src = program("RUNON", ["WORKING-STORAGE SECTION.", "01  WS-STATES PIC X(10) VALUE"], [
            "0000-MAIN.", "    EXEC SQL OPEN C1 END-EXEC", "    MOVE 'A' TO WS-STATES", "    GOBACK."])
        self.assertFalse([st for st in self.statements(src) if st.no_period])
        self.assertFalse([u for u in cobol.parse_program(src).unresolved if u[0] == "exec_no_period"])

    def test_a_copybook_read_alone_cuts_before_a_level_number_only(self):
        sts = self.statements(BOOK)
        self.assertEqual([st.no_period for st in sts if st.no_period], [("SQL", 1)])
        roots, _w = copybook.parse_data_division(reader.join_cobol_continuations(reader.read_cobol_lines(BOOK)[0]))
        self.assertEqual([r.name for r in roots], ["DCLNO-TBL"])
        self.assertEqual(cobol.parse_program(BOOK).declared_tables, {"PRD.NO_TBL": [("STATUS_CD", "CHAR(2) NOT NULL")]})
        # a procedure copybook: the statement after the END-EXEC is not a data entry, nothing is cut
        proc = "           EXEC SQL OPEN C1 END-EXEC\n           MOVE 'A' TO WS-X.\n"
        self.assertFalse([st for st in self.statements(proc) if st.no_period])


# ---------------------------------------------------------------------------
# the build, the reports and the stand-ins
# ---------------------------------------------------------------------------

ONLINE = program("ONLPGM", [
    "WORKING-STORAGE SECTION.",
    "01  WS-NEXT-PGM      PIC X(08) VALUE 'ONLPGM2'.",
    "01  WS-OTHER-PGM     PIC X(08).",
    "01  WS-COMM          PIC X(100).",
], [
    "0000-MAIN.",
    "    EXEC CICS XCTL PROGRAM(WS-NEXT-PGM) COMMAREA(WS-COMM)",
    "         LENGTH(100) END-EXEC",
    "    CALL WS-OTHER-PGM",
    "    CALL 'STATPGM'",
    "    GOBACK.",
])


class _Built(unittest.TestCase):

    files = (("GC/PROD.GC.SRC/NOPGM.cbl", NOPERIOD), ("GC/PROD.GC.SRC/BOOKPGM.cbl", BOOKPGM),
             ("GC/PROD.GC.SRC/SQLPGM.cbl", SQL), ("GC/PROD.GC.SRC/DLIPGM.cbl", DLI),
             ("GC/PROD.GC.SRC/ENDPGM.cbl", ENDS), ("GC/PROD.GC.SRC/FILEPGM.cbl", FILES),
             ("GC/PROD.GC.SRC/ONLPGM.cbl", ONLINE), ("SHARED/PROD.GC.COPYLIB/NOBOOK.cpy", BOOK))

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        cls.db = os.path.join(cls.td, "t.db")
        for rel, text in cls.files:
            p = os.path.join(cls.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"])
        assert rc == 0, buf.getvalue()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def q(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def page(self, fn, *args):
        conn = query.connect(self.db)
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    def member_id(self, name):
        return self.q("SELECT id FROM member WHERE name=?", name)[0][0]


class TheBuildAndTheReports(_Built):

    def test_the_program_without_the_period_is_partial_with_the_note(self):
        self.assertEqual(self.q("SELECT parse_status FROM member WHERE name='NOPGM'"), [("partial",)])
        self.assertEqual(self.q("""SELECT u.detail, u.line FROM unresolved u JOIN member m ON m.id = u.member_id
                                   WHERE m.name='NOPGM' AND u.kind='exec_no_period'"""),
                         [(NOTE.format(line_of(NOPERIOD, "NOCUR1 CURSOR")), line_of(NOPERIOD, "NOCUR1 CURSOR"))])
        self.assertEqual(self.q("""SELECT p.name FROM paragraph p JOIN program g ON g.id = p.program_id
                                   JOIN member m ON m.id = g.member_id WHERE m.name='NOPGM' ORDER BY p.start_line"""),
                         [("0000-MAIN",), ("1000-NEXT",)])
        self.assertEqual(sorted(r[0] for r in self.q("SELECT f.name FROM field f JOIN member m ON m.id = f.member_id "
                                                     "WHERE m.name='NOPGM'")), ["WS-AFTER", "WS-LAST", "WS-STATUS"])

    def test_a_copybooks_block_is_named_with_its_own_line(self):
        self.assertEqual(self.q("""SELECT u.detail FROM unresolved u JOIN member m ON m.id = u.member_id
                                   WHERE m.name='BOOKPGM' AND u.kind='exec_no_period'"""),
                         [("EXEC SQL at line 1 of copybook NOBOOK has no period after its END-EXEC; what follows was "
                           "read as if it had one",)])
        self.assertEqual(self.q("SELECT parse_status FROM member WHERE name='BOOKPGM'"), [("partial",)])
        self.assertEqual(self.q("SELECT kind, parse_status FROM member WHERE name='NOBOOK'"), [("copybook", "ok")])
        self.assertEqual(sorted(r[0] for r in self.q("SELECT f.name FROM field f JOIN member m ON m.id = f.member_id "
                                                     "WHERE m.name='NOBOOK'")), ["DCLNO-TBL", "STATUS-CD"])

    def test_coverage_names_it_and_says_what_it_means(self):
        cov = self.page(query.cmd_coverage)
        part = cov.split("### Members parsed only in part")[1].split("\n### ")[0]
        self.assertIn("| cobol | NOPGM | PROD.GC.SRC | exec_no_period: "
                      + NOTE.format(line_of(NOPERIOD, 'NOCUR1 CURSOR')) + " |", part)
        row = [ln for ln in cov.splitlines() if ln.startswith("| exec_no_period |")]
        self.assertEqual(len(row), 1, cov)
        self.assertIn("the member is partial because the facts after that line rest on it", row[0])
        self.assertIn("the program's compile JCL or listing says which", row[0])
        self.assertIn("the facts after that line are right when the period is all that is missing", row[0])
        self.assertNotIn("reject", row[0])
        page = self.page(query.cmd_program, "NOPGM")
        self.assertIn("parse: partial", page)
        self.assertIn("| NOPGM | exec_no_period | EXEC SQL at line", page)

    def test_the_cites_of_the_sentence(self):
        sel, upd = [i for i, ln in enumerate(SQL.splitlines(), 1) if "EXEC SQL" in ln][:2]
        self.assertEqual(self.q("""SELECT stmt_type, start_line FROM sql_stmt s JOIN program g ON g.id = s.program_id
                                   JOIN member m ON m.id = g.member_id WHERE m.name='SQLPGM' ORDER BY start_line"""),
                         [("SELECT", sel), ("UPDATE", upd), ("DELETE", line_of(SQL, "DELETE FROM"))])
        table = self.page(query.cmd_table, "PRD.POLICY_TBL")
        self.assertIn(f"SQLPGM:{sel}", table)
        self.assertIn(f"SQLPGM:{upd}", table)
        self.assertEqual(self.q("""SELECT func, line FROM dli_call d JOIN program g ON g.id = d.program_id
                                   JOIN member m ON m.id = g.member_id WHERE m.name='DLIPGM' ORDER BY line"""),
                         [("GU", line_of(DLI, "WS-GU DB-PCB")), ("CHKP", line_of(DLI, "WS-CHKP IO-PCB")),
                          ("GN", line_of(DLI, "WS-GN DB-PCB"))])

    def test_field_answers_for_end_date_and_a_file(self):
        page = self.page(query.cmd_field, "END-DATE")
        self.assertIn(f"ENDPGM:{line_of(ENDS, 'TO END-DATE')}", page)
        self.assertIn(f"ENDPGM:{line_of(ENDS, 'IF END-DATE')}", page)
        self.assertEqual(self.q("SELECT COUNT(*) FROM field_ref WHERE name IN ('IN-FILE', 'OUT-FILE', 'SD-FILE', "
                                "'INTEGER-OF-DATE', 'CURRENT-DATE')"), [(0,)])

    def test_the_calls_table_shows_the_variable_beside_its_targets(self):
        page = self.page(query.cmd_program, "ONLPGM")
        calls = page.split("### Calls")[1].split("\n### ")[0]
        self.assertIn("| cics_xctl | WS-NEXT-PGM -> ONLPGM2 |", calls)
        self.assertIn("| dynamic | WS-OTHER-PGM -> UNRESOLVED |", calls)
        self.assertIn("| static | STATPGM |", calls)
        page = self.page(query.cmd_program, "ENDPGM")
        self.assertIn("| dynamic | END-PGM -> PGMA |", page)

    def test_the_cell_on_either_index(self):
        # call_edge has the same columns on an index built before the batch: the cell is read from them alone
        self.assertEqual(query.call_target_cell("ONLPGM2", "WS-NEXT-PGM", ["ONLPGM2"]), "WS-NEXT-PGM -> ONLPGM2")
        self.assertEqual(query.call_target_cell(None, "WS-X", ["A", "B"]), "WS-X -> A, B")
        self.assertEqual(query.call_target_cell(None, "WS-X", []), "WS-X -> UNRESOLVED")
        self.assertEqual(query.call_target_cell("PGMA", None, ["PGMA"]), "PGMA")
        self.assertEqual(query.call_target_cell(None, None, []), "UNRESOLVED")


class TheStandInsFindNothingToDo(_Built):
    """On an index built by the batch holding these members, the stand-ins of this week say nothing wrong: the F15
    member is truly partial (not a copybook chosen among several), and recover has nothing to mark, re-file or name."""

    def test_partial_kind_and_the_chosen_row(self):
        conn = query.connect(self.db)
        try:
            mid = self.member_id("NOPGM")
            self.assertEqual(query.partial_kind(conn, mid), "partial")
            self.assertTrue(query.is_truly_partial(conn, mid))
            self.assertFalse(query.chose_a_copybook(conn, mid))
            self.assertEqual(query.parse_label(conn, mid, "partial"), "partial")
            for name in ("SQLPGM", "DLIPGM", "ENDPGM", "FILEPGM", "ONLPGM"):
                self.assertIsNone(query.partial_kind(conn, self.member_id(name)), name)
        finally:
            conn.close()
        cov = self.page(query.cmd_coverage)
        self.assertNotIn("NOPGM", cov.split("### Members parsed only in part")[0])

    def test_recover_has_nothing_to_do(self):
        said = []
        stats = recover.run(self.db, log=said.append, report=os.path.join(self.td, "work", "recover.md"))
        for key in ("arrived", "misfiled", "refiled", "waiting", "marked"):
            self.assertEqual(stats.get(key), 0, (key, said))
        self.assertEqual(self.q("SELECT parse_status FROM member WHERE name='NOPGM'"), [("partial",)])


# ---------------------------------------------------------------------------
# the verifier's first round on this stage (LESSONS 212)
# ---------------------------------------------------------------------------

DISPATCH = program("DSPPGM", [
    "WORKING-STORAGE SECTION.",
    "01  WS-TRAN-IDX      PIC 9.",
    "01  END-SW           PIC X.",
], [
    "0000-MAIN.",
    "    GO TO 1000-ADD 2000-CHG 3000-DEL",
    "          DEPENDING ON WS-TRAN-IDX",
    "    GO TO 9000-BAD-TRAN.",
    "0100-NEXT.",
    "    GO TO 1000-ADD 2000-CHG DEPENDING ON WS-TRAN-IDX",
    "    PERFORM 9000-BAD-TRAN.",
    "0200-NEXT.",
    "    GO TO 1000-ADD DEPENDING ON WS-TRAN-IDX",
    "    MOVE SPACE TO END-SW",
    "    GO TO 999-EXIT.",
    "1000-ADD.",
    "    READ IN-FILE AT END GO TO 999-EXIT.",
    "2000-CHG.",
    "    DISPLAY 'GO TO THE DESK'.",
    "3000-DEL.",
    "    PERFORM UNTIL END-SW = 'Y' GO TO 999-EXIT END-PERFORM.",
    "9000-BAD-TRAN.",
    "    DISPLAY 'IF IT FAILS' GOBACK.",
    "999-EXIT.",
    "    EXIT.",
])


class GoToDependingInASentence(unittest.TestCase):
    """`GO TO A B C DEPENDING ON IX` with more statements in its sentence: the target list ended only at a period, a
    scope terminator, ELSE or WHEN, so the next GO TO or PERFORM of the sentence became more plain GO TO targets -
    DEPENDING, ON, the index, GO, TO - all at the first line, the index had no reference, and a fall-through was
    recorded after a sentence whose last GO TO always leaves."""

    def setUp(self):
        self.f = cobol.parse_program(DISPATCH)
        self.at = lambda needle: line_of(DISPATCH, needle)

    def edges(self, frm):
        return [(t, k, ln) for (fr, t, _th, ln, k) in self.f.performs if fr == frm]

    def test_each_go_to_of_the_sentence_is_its_own_edge(self):
        first = self.at("GO TO 1000-ADD 2000-CHG 3000-DEL")
        self.assertEqual(self.edges("0000-MAIN"),
                         [("1000-ADD", "goto_depending", first), ("2000-CHG", "goto_depending", first),
                          ("3000-DEL", "goto_depending", first), ("9000-BAD-TRAN", "goto", self.at("GO TO 9000-BAD"))])
        second = self.at("GO TO 1000-ADD 2000-CHG DEPENDING")
        self.assertEqual(sorted(self.edges("0100-NEXT")),
                         sorted([("9000-BAD-TRAN", "perform", self.at("PERFORM 9000-BAD-TRAN")),
                                 ("1000-ADD", "goto_depending", second), ("2000-CHG", "goto_depending", second),
                                 ("0200-NEXT", "fallthrough", self.at("PERFORM 9000-BAD-TRAN"))]))
        # a name beginning with END- no longer ended the list, so the later GO TO was swallowed at the first line
        third = self.at("GO TO 1000-ADD DEPENDING")
        self.assertEqual(self.edges("0200-NEXT"),
                         [("1000-ADD", "goto_depending", third), ("999-EXIT", "goto", self.at("GO TO 999-EXIT."))])
        targets = {t for (_fr, t, _th, _ln, _k) in self.f.performs}
        self.assertFalse(targets & {"DEPENDING", "ON", "GO", "TO", "WS-TRAN-IDX", "PERFORM", "MOVE", "END-SW"})

    def test_the_index_is_tested(self):
        # at the GO TO's line, as every reference is cited at its verb's
        r = refs(self.f)
        for needle in ("GO TO 1000-ADD 2000-CHG 3000-DEL", "GO TO 1000-ADD 2000-CHG DEPENDING", "GO TO 1000-ADD DEPENDING"):
            self.assertIn(("WS-TRAN-IDX", "test", "GO TO", self.at(needle)), r, needle)

    def test_fall_through_is_read_statement_by_statement(self):
        falls = {fr for (fr, _t, _th, _ln, k) in self.f.performs if k == "fallthrough"}
        # 0000-MAIN and 0200-NEXT end with a GO TO that always leaves; 0100-NEXT's PERFORM returns
        self.assertNotIn("0000-MAIN", falls)
        self.assertNotIn("0200-NEXT", falls)
        self.assertIn("0100-NEXT", falls)
        # a GO TO under AT END, a GO TO in a literal, a GO TO inside a loop that may not run: the paragraph goes on
        for para in ("1000-ADD", "2000-CHG", "3000-DEL"):
            self.assertIn(para, falls, para)
        # a literal holding IF does not make the GOBACK after it conditional
        self.assertNotIn("9000-BAD-TRAN", falls)
        self.assertTrue(cobol._leaves("GO TO 999-EXIT."))
        self.assertTrue(cobol._leaves("STOP RUN."))
        self.assertTrue(cobol._leaves("EXIT PROGRAM."))
        self.assertFalse(cobol._leaves("STOP 'WAIT'."))
        self.assertFalse(cobol._leaves("GO TO A B DEPENDING ON IX."))
        self.assertFalse(cobol._leaves("IF A = B GO TO X END-IF."))
        self.assertFalse(cobol._leaves("ADD 1 TO X ON SIZE ERROR GO TO 999-EXIT."))
        self.assertFalse(cobol._leaves("EXEC CICS RETURN END-EXEC."))

    def test_a_lower_case_or_exec_dli_call_is_still_read(self):
        # the cheap guard before the DL/I split reads the statement in upper case
        src = program("LOWDLI", ["WORKING-STORAGE SECTION.", "01  WS-GU PIC X(4) VALUE 'GU  '.",
                                 "01  WS-AREA PIC X(80).", "LINKAGE SECTION.", "01  DB-PCB PIC X(40)."],
                      ["0000-MAIN.", "    call 'cbltdli' using ws-gu db-pcb ws-area",
                       "    EXEC DLI GU USING PCB(1) SEGMENT(ROOTSEG) INTO(WS-AREA)",
                       "    END-EXEC",
                       "    GOBACK."])
        f = cobol.parse_program(src)
        self.assertEqual([(d.interface, d.func, d.line) for d in f.dli],
                         [("CBLTDLI", "GU", line_of(src, "cbltdli")), ("EXEC DLI", "GU", line_of(src, "EXEC DLI"))])


class TheNoteIsReadWhole(unittest.TestCase):

    def test_the_note_fits_where_it_is_printed(self):
        longest = cobol.exec_no_period_note("CICS", 99999, "ABCDEFGH")
        self.assertLessEqual(len(longest), 120)               # `program` printed 120 characters of a detail
        self.assertNotIn("reject", longest)                  # nothing said of a compile the member cannot show

    def test_clip_cuts_at_a_word(self):
        self.assertEqual(query.clip("short", 10), "short")
        self.assertEqual(query.clip("", 10), "")
        self.assertEqual(query.clip(None, 10), "")
        cut = query.clip("the facts after it were read as if the period were there", 30)
        self.assertEqual(cut, "the facts after it were ...")
        self.assertLessEqual(len(cut), 30)
        self.assertEqual(query.clip("A" * 50, 20), "A" * 16 + " ...")


RNDREC = ("       01  RND-REC.\n"
          "           05  RND-KEY          PIC X(10).\n"
          "           05  RND-NAME         PIC X(30).\n"
          "           05  RND-AMT          PIC S9(7)V99 COMP-3.\n"
          "           05  RND-CODE         PIC X(02).\n")

RNDCOPY = program("RNDCOPY1", [
    "WORKING-STORAGE SECTION.",
    "    COPY RNDREC.",
    "01  WS-PGM           PIC X(08).",
    "    EXEC SQL DECLARE RNDCUR2 CURSOR FOR",
    "        SELECT STATUS_CD FROM PRD.RND_TBL",
    "    END-EXEC",
    "01  WS-LAST          PIC X(04).",
], [
    "0000-MAIN.",
    "    EXEC SQL OPEN RNDCUR2 END-EXEC",
    "    CALL WS-PGM",
    "    GOBACK.",
])

RNDCICS = program("RNDCICS1", [
    "WORKING-STORAGE SECTION.",
    "01  WS-COMM          PIC X(100).",
    "    EXEC SQL DECLARE RNDCUR CURSOR FOR",
    "        SELECT STATUS_CD FROM PRD.RND_TBL",
    "    END-EXEC",
    "01  WS-STATUS        PIC X(02).",
], [
    "0000-MAIN.",
    "    IF EIBCALEN = 0",
    "        MOVE WS-STATUS TO WS-COMM",
    "    END-IF",
    "    EXEC SQL OPEN RNDCUR END-EXEC",
    "    EXEC CICS RETURN END-EXEC.",
])

RNDMISS = program("RNDMISS1", ["WORKING-STORAGE SECTION.", "    COPY RNDGONE.", "01  WS-X PIC X."],
                  ["0000-MAIN.", "    GOBACK."])


class _RoundOne(_Built):

    files = (("RN/PROD.RN.SRC/RNDCOPY1.cbl", RNDCOPY), ("RN/PROD.RN.SRC/RNDCICS1.cbl", RNDCICS),
             ("RN/PROD.RN.SRC/RNDMISS1.cbl", RNDMISS), ("RN/PROD.RN.SRC/FILEPGM.cbl", FILES),
             ("RN/PROD.RN.SRC/DSPPGM.cbl", DISPATCH), ("SHARED/PROD.RN.COPYLIB/RNDREC.cpy", RNDREC))


class CoverageGivesTheReasonThatMadeItPartial(_RoundOne):
    """A CICS program that tests EIBCALEN with no DFHCOMMAREA, and whose DECLARE CURSOR in WORKING-STORAGE has no period
    after its END-EXEC: coverage named no_commarea - a note that makes no member partial, written first - as the
    reason, the advice under the table sent the reader to fetch a copybook library, and flow said 'facts incomplete'."""

    def test_the_reason_is_exec_no_period(self):
        cov = self.page(query.cmd_coverage)
        part = cov.split("### Members parsed only in part")[1].split("\n### ")[0]
        self.assertIn("(3)", part.splitlines()[0])
        self.assertIn("| cobol | 3 | exec_no_period (2) |", part)
        self.assertIn("| cobol | RNDCICS1 | PROD.RN.SRC | exec_no_period: "
                      + NOTE.format(line_of(RNDCICS, "RNDCUR CURSOR")) + " |", part)
        self.assertIn("| cobol | RNDCOPY1 | PROD.RN.SRC | exec_no_period: "
                      + NOTE.format(line_of(RNDCOPY, "RNDCUR2 CURSOR")) + " |", part)
        self.assertIn("| cobol | RNDMISS1 | PROD.RN.SRC | expand: L5: COPY RNDGONE NOT FOUND", part)
        self.assertNotIn("no_commarea", part)
        # the advice speaks of both reasons the table holds
        self.assertIn("usually partial because a copybook it copies is not in the index", part)
        self.assertIn("2 of the members above are partial because an EXEC block before the PROCEDURE DIVISION has no "
                      "period after its END-EXEC (exec_no_period): nothing to fetch", part)
        # the note the CICS program also carries is still said, as what it is
        self.assertEqual(self.q("""SELECT u.kind FROM unresolved u JOIN member m ON m.id = u.member_id
                                   WHERE m.name = 'RNDCICS1' ORDER BY u.id"""),
                         [("no_commarea",), ("exec_no_period",)])

    def test_flow_names_the_reason(self):
        out = self.page(query.cmd_flow, "WS-COMM", "RNDCICS1")
        self.assertIn(f"[program partial: EXEC SQL at line {line_of(RNDCICS, 'RNDCUR CURSOR')} has no period after "
                      "its END-EXEC]", out)
        self.assertNotIn("facts incomplete", out)

    def test_the_line_column_is_the_members_own(self):
        page = self.page(query.cmd_program, "RNDCOPY1")
        decl, call = line_of(RNDCOPY, "RNDCUR2 CURSOR"), line_of(RNDCOPY, "CALL WS-PGM")
        self.assertIn(f"| RNDCOPY1 | exec_no_period | {NOTE.format(decl)} | {decl} |", page)
        self.assertRegex(page, rf"\| RNDCOPY1 \| dynamic_call \| CALL WS-PGM [^|]* \| {call} \|")
        # the rows are stored at their lines in the expanded text, the copybook's lines before them
        stored = self.q("""SELECT u.kind, u.line FROM unresolved u JOIN member m ON m.id = u.member_id
                             WHERE m.name = 'RNDCOPY1' AND u.kind IN ('exec_no_period', 'dynamic_call')
                             ORDER BY u.line""")
        self.assertEqual([k for k, _ln in stored], ["exec_no_period", "dynamic_call"])
        self.assertEqual(stored[0][1] - decl, stored[1][1] - call)
        self.assertGreaterEqual(stored[0][1] - decl, 5)

    def test_an_index_without_the_line_map_keeps_the_stored_number(self):
        conn = query.connect(self.db)
        try:
            pid = conn.execute("SELECT p.id FROM program p JOIN member m ON m.id = p.member_id "
                               "WHERE m.name = 'RNDCOPY1'").fetchone()[0]
            self.assertEqual(query.unresolved_line_cell(conn, pid, 99999), 99999)      # no run holds it
            self.assertEqual(query.unresolved_line_cell(conn, None, 7), 7)             # no program row
            self.assertIsNone(query.unresolved_line_cell(conn, pid, None))
        finally:
            conn.close()

    def test_field_of_a_file_name_says_it_is_a_file(self):
        page = self.page(query.cmd_field, "IN-FILE")
        self.assertNotIn("NOT DEFINED", page)
        self.assertIn("`IN-FILE` is a **file** (SELECT ... ASSIGN, FD), not a data item - declared in 1 program:", page)
        self.assertIn(f"| FILEPGM | INFILE | INDEXED | IN-RECORD | RN/FILEPGM:{line_of(FILES, 'SELECT IN-FILE')} |", page)
        self.assertIn("`flow IN-RECORD --program P`", page)          # a file's bytes travel as its record
        self.assertIn("`program FILEPGM` shows what the program does with the file", page)
        self.assertIn("`field IN-RECORD` the reads and writes of its record", page)
        self.assertNotIn("an index built before", page)
        self.assertIn("**NOT DEFINED**", self.page(query.cmd_field, "NO-SUCH-NAME"))

    def test_field_of_a_file_name_on_an_older_index(self):
        # an index built before ROADMAP re-parse item 26 recorded OPEN / CLOSE as reads of the file
        td = tempfile.mkdtemp()
        try:
            db = os.path.join(td, "old.db")
            shutil.copy(self.db, db)
            conn = sqlite3.connect(db)
            pid = conn.execute("SELECT p.id FROM program p JOIN member m ON m.id = p.member_id "
                               "WHERE m.name = 'FILEPGM'").fetchone()[0]
            conn.execute("INSERT INTO field_ref(program_id, name, mode, stmt, line) VALUES(?, 'IN-FILE', 'read', "
                         "'OPEN', ?)", (pid, line_of(FILES, "OPEN I-O")))
            conn.commit()
            conn.close()
            conn = query.connect(db)
            try:
                page = query.cmd_field(conn, "IN-FILE")
            finally:
                conn.close()
            self.assertIn("is a **file**", page)
            self.assertIn("file statements (OPEN, CLOSE, START, DELETE) that an index built before ROADMAP re-parse "
                          "item 26 recorded as reads", page)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_the_dispatchers_index_answers_field(self):
        page = self.page(query.cmd_field, "WS-TRAN-IDX")
        self.assertIn(f"DSPPGM GO TO x3 @RN/DSPPGM:{line_of(DISPATCH, 'GO TO 1000-ADD 2000-CHG 3000-DEL')}", page)

    def test_the_stand_ins_find_nothing_to_do(self):
        conn = query.connect(self.db)
        try:
            for name in ("RNDCOPY1", "RNDCICS1", "RNDMISS1"):
                self.assertEqual(query.partial_kind(conn, self.member_id(name)), "partial", name)
            for name in ("FILEPGM", "DSPPGM"):
                self.assertIsNone(query.partial_kind(conn, self.member_id(name)), name)
        finally:
            conn.close()
        cov = self.page(query.cmd_coverage)
        self.assertNotIn("chosen among several", cov.split("### Members parsed only in part")[1].split("\n### ")[0])


class OnlyTheExecReason(_Built):
    """Every partial member of the estate is partial for its EXEC block: the copybook advice is not printed."""

    def test_the_advice_is_the_exec_sentence_alone(self):
        cov = self.page(query.cmd_coverage)
        part = cov.split("### Members parsed only in part")[1].split("\n### ")[0]
        self.assertNotIn("usually partial because a copybook", part)
        self.assertIn("2 of the members above are partial because an EXEC block", part)
        page = self.page(query.cmd_program, "BOOKPGM")
        self.assertRegex(page, r"\| BOOKPGM \| exec_no_period \| EXEC SQL at line 1 of copybook NOBOOK has no period "
                               r"after its END-EXEC; what follows was read as if it had one \| (?:\w+/)?NOBOOK:1 "
                               r"\(via COPY NOBOOK\) \|")


# ---------------------------------------------------------------------------
# the reproductions
# ---------------------------------------------------------------------------

class TheReproductions(unittest.TestCase):
    """tools/synth/repro/F01, F02, F04, F14, F15 built in process: every expect.json truth holds (verify.py says
    FIXED)."""

    def check(self, rid):
        src = os.path.join(REPRO, rid)
        td = tempfile.mkdtemp()
        try:
            shutil.copytree(os.path.join(src, "estate"), os.path.join(td, "estate"))
            db = os.path.join(td, "t.db")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(build._main([os.path.join(td, "estate"), "--db", db, "--rebuild", "--quiet"]), 0)
            with open(os.path.join(src, "expect.json"), encoding="utf-8") as fh:
                spec = json.load(fh)
            conn = sqlite3.connect(db)
            try:
                for ck in spec["checks"]:
                    got = [list(r) if len(r) > 1 else r[0] for r in conn.execute(ck["sql"]).fetchall()]
                    got = got[0] if len(got) == 1 and ck.get("scalar", True) else got
                    self.assertEqual(got, ck["truth"], (rid, ck["sql"]))
            finally:
                conn.close()
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_f01_dli_calls_in_one_sentence(self):
        self.check("F01-dli-two-calls-one-sentence")

    def test_f02_sql_statements_in_one_sentence(self):
        self.check("F02-sql-sentence-cite")

    def test_f04_end_date(self):
        self.check("F04-end-date-reference")

    def test_f14_file_and_function_names(self):
        self.check("F14-file-and-function-as-field")

    def test_f15_exec_sql_without_its_period(self):
        self.check("F15-ws-exec-sql-no-period")


class TheDocsSayIt(unittest.TestCase):
    """ROADMAP 26 (and 25's remainder), LESSONS 211, README's COBOL row, the reproductions' READMEs and the findings
    report say what changed."""

    def read(self, *path):
        with open(os.path.join(ROOT, *path), encoding="utf-8") as fh:
            return fh.read()

    def test_the_docs(self):
        roadmap = self.read("ROADMAP.md")
        self.assertIn("26. **Delivered in the batch - every fact of a sentence at its own line, END-DATE a data name", roadmap)
        self.assertIn("so this item's program still loses its paragraphs", roadmap)
        lessons = self.read("LESSONS.md")
        self.assertIn("| 211 | Not seen on his estate - the synthetic estate's first comparison", lessons)
        self.assertIn("Rule for me: a sentence is a container, not a fact", lessons)
        self.assertIn("`END-DATE` taken for a scope terminator", self.read("README.md"))
        for rid in ("F01-dli-two-calls-one-sentence", "F02-sql-sentence-cite", "F04-end-date-reference",
                    "F14-file-and-function-as-field", "F15-ws-exec-sql-no-period"):
            self.assertIn("Fixed by ROADMAP re-parse item 26 (LESSONS 211)", self.read("tools", "synth", "repro", rid,
                                                                                     "README.md"), rid)
        # the verifier's first round
        self.assertIn("| 212 | Not seen on his estate - the verifier's first round on ROADMAP re-parse item 26", lessons)
        self.assertIn("a sentence written into the index says what the index did, never the verdict of a tool", lessons)
        self.assertIn("(`query.unresolved_line_cell` - a parser note after a COPY printed its expanded line)", roadmap)
        self.assertIn("so the note says nothing of the compile", roadmap)
        self.assertNotIn("would reject", roadmap)
        f15 = self.read("tools", "synth", "repro", "F15-ws-exec-sql-no-period", "README.md")
        self.assertIn("'EXEC SQL at line 6 has no period after its END-EXEC; what follows was read as if it had one'", f15)
        self.assertNotIn("which the compiler rejects", f15)
        report = self.read("docs", "SYNTH-findings-2026-09-25.md")
        self.assertIn("F25 is the toolkit's, not the generator's", report)
        self.assertIn("POLUPD05's rows F16, F17, F21-F24 did not come from F15's shape", report)


if __name__ == "__main__":
    unittest.main()
