"""
`flow FIELD` - where a VALUE goes (docs/PLAN-value-flow.md).

  the parser records one row per (source, target) pair a verb moves data
  between, with the enclosing IF as its guard, and CALL arguments with how
  they are passed; the build stores each program's data items at THIS
  program's offsets (pfield), the pairs (data_flow), the CALL arguments
  (call_arg), the callee side (param) and every 01 under an FD (file_record);
  `flow` walks the chain - file bytes through the job to the reader, CALL
  USING positions into the callee, DB2 columns to their readers, CICS
  carriers - and LABELS every place it widens or stops with a fixed string
  instead of following silently. Written before the code, so every test here
  fails until its stage lands (LESSONS 3-5): the parser has no `flows`, the
  schema has no `data_flow`, `atlas.query` has no `flow` command.

The fixtures are the FLOW* members: FLOWSRC writes WS-STATUS to a file that
FLOWJOB sorts and FLOWRDR reads (through a DIFFERENT copybook, one prefix
byte off) and FLOWJOB's STEP4 sends by FTP, calls FLOWSUB by name, by ENTRY alias with the parameters swapped
and through a variable with two candidates, and updates POLICY_TBL; FLOWCICS
LINKs to FLOWCOMM with a COMMAREA, which answers through a TS queue; FLOWJOB2
re-arranges the bytes with OUTREC on the way to the same reader. FLOWSRC also
calls FLOWSUB with a third argument the callee never declares and moves a
COMP item to a DISPLAY one; ERRPGM (already a fixture) has a COPY nobody can
find, so its facts are partial.
"""

import contextlib
import io
import json
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

from atlas import build, cobol, query, reader, verify_citations  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
SNAPSHOT = os.path.join(HERE, "snapshots", "flow_refs.json")

FOOTER = "Flow-insensitive: statement order and IF guards are not evaluated; a hop is a copy that CAN happen."
# MEMBER:line "token" / MEMBER:lo-hi "token" / MEMBER:line (via COPY X) "token", as the report prints them
CITE = re.compile(r"\b([A-Z][A-Z0-9]*):(\d+)(?:-(\d+))?(?:\s*\(via COPY [A-Z0-9]+\))?\s+\"((?:[^\"\\]|\\.)*)\"")


def _line(member: str, needle: str) -> int:
    """1-based number of the first physical line of a fixture holding `needle`."""
    with open(os.path.join(FIX, member), encoding="utf-8") as fh:
        for i, ln in enumerate(fh, 1):
            if needle in ln:
                return i
    raise AssertionError(f"{needle!r} not in fixture {member}")


def _lines_of(text: str, needle: str) -> str:
    """Every output line holding `needle`, joined - for assertions about ONE hop."""
    return "\n".join(ln for ln in text.splitlines() if needle in ln)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

SHAPES = [
    "       IDENTIFICATION DIVISION.",
    "       PROGRAM-ID. SHAPES.",
    "       ENVIRONMENT DIVISION.",
    "       INPUT-OUTPUT SECTION.",
    "       FILE-CONTROL.",
    "           SELECT IN-F  ASSIGN TO INDD.",
    "           SELECT OUT-F ASSIGN TO OUTDD.",
    "       DATA DIVISION.",
    "       FILE SECTION.",
    "       FD  IN-F.",
    "       01  IN-REC-A                 PIC X(40).",
    "       01  IN-REC-B                 PIC X(40).",
    "       FD  OUT-F.",
    "       01  OUT-REC                  PIC X(40).",
    "       WORKING-STORAGE SECTION.",
    "       01  REC-A.",
    "           05  WS-Q                 PIC X(02).",
    "       01  REC-B.",
    "           05  WS-Q                 PIC X(02).",
    "       01  WS-X                     PIC X(02).",
    "       01  WS-Y                     PIC X(02).",
    "       01  WS-Z                     PIC X(02).",
    "       01  WS-TBL.",
    "           05  WS-ENT               PIC X(02) OCCURS 3 TIMES.",
    "       01  WS-I                     PIC S9(04) COMP.",
    "       01  WS-BUF                   PIC X(40).",
    "       01  WS-A                     PIC X(10).",
    "       01  WS-B                     PIC X(10).",
    "       01  WS-C                     PIC X(20).",
    "       01  WS-N                     PIC 9(04).",
    "       01  WS-M                     PIC 9(04).",
    "       01  WS-P                     PIC 9(02).",
    "       01  WS-DATE                  PIC 9(06).",
    "       01  WS-FLAG                  PIC X.",
    "           88  WS-FLAG-ON           VALUE 'Y'.",
    "       01  WS-PTR                   POINTER.",
    "       01  WS-Q-AREA                PIC X(08).",
    "       01  WS-MAP-AREA              PIC X(20).",
    "       01  WS-COMM                  PIC X(10).",
    "       LINKAGE SECTION.",
    "       01  LK-A                     PIC X(02).",
    "       01  LK-B                     PIC X(02).",
    "       01  LK-RET                   PIC S9(04) COMP.",
    "       01  LK-AREA                  PIC X(40).",
    "       PROCEDURE DIVISION USING LK-A LK-B RETURNING LK-RET.",
    "       0000-MAIN.",
    "           MOVE WS-Q OF REC-A TO WS-Q OF REC-B.",
    "           IF WS-N = 1 MOVE WS-X TO WS-Y ELSE MOVE WS-Y TO WS-X END-IF.",
    "           MOVE WS-ENT (WS-I) TO WS-Z.",
    "           MOVE WS-BUF(1:2) TO WS-X.",
    "           MOVE CORR REC-A TO REC-B.",
    "           MOVE FUNCTION UPPER-CASE(WS-A) TO WS-B.",
    "           MOVE WS-X TO WS-Y WS-Z.",
    "           MOVE 'AB' TO WS-X.",
    "           MOVE SPACES TO WS-Y.",
    "           COMPUTE WS-N = WS-M + WS-P.",
    "           ADD WS-M WS-P GIVING WS-N.",
    "           ADD WS-M TO WS-N.",
    "           STRING WS-A DELIMITED BY SIZE WS-B DELIMITED BY SPACE",
    "               INTO WS-C WITH POINTER WS-P.",
    "           UNSTRING WS-C DELIMITED BY ',' INTO WS-A WS-B",
    "               COUNT IN WS-N.",
    "           INITIALIZE REC-A.",
    "           SET WS-I TO WS-N.",
    "           SET WS-FLAG-ON TO TRUE.",
    "           SET ADDRESS OF LK-AREA TO WS-PTR.",
    "           ACCEPT WS-DATE FROM DATE.",
    "           READ IN-F.",
    "           READ IN-F INTO WS-BUF.",
    "           WRITE OUT-REC FROM WS-BUF.",
    "           INSPECT WS-A REPLACING ALL 'A' BY 'B'.",
    "           PERFORM VARYING WS-I FROM 1 BY 1 UNTIL WS-I > 3",
    "               CONTINUE",
    "           END-PERFORM.",
    "           CALL 'S' USING BY CONTENT WS-X BY REFERENCE WS-Y",
    "                LENGTH OF WS-X ADDRESS OF WS-Z RETURNING WS-I.",
    "           IF WS-N > 0",
    "               MOVE WS-A TO WS-B",
    "           ELSE",
    "               MOVE WS-B TO WS-A",
    "           END-IF.",
    "           EXEC CICS WRITEQ TS QUEUE('Q1') FROM(WS-Q-AREA) END-EXEC.",
    "           EXEC CICS READQ TS QUEUE('Q1') INTO(WS-Q-AREA) END-EXEC.",
    "           EXEC CICS SEND MAP('M1') MAPSET('S1') FROM(WS-MAP-AREA)",
    "           END-EXEC.",
    "           EXEC CICS LINK PROGRAM('SUBP') COMMAREA(WS-COMM) END-EXEC.",
    "           EXEC CICS READ FILE('F1') SET(ADDRESS OF LK-AREA)",
    "               RIDFLD(WS-X) END-EXEC.",
    "           EXEC CICS GETMAIN SET(ADDRESS OF LK-AREA) LENGTH(40)",
    "           END-EXEC.",
    "           EXEC CICS READNEXT FILE('F1') SET(WS-PTR) RIDFLD(WS-X)",
    "           END-EXEC.",
    "           GOBACK.",
]


def _ln(needle: str) -> int:
    for i, ln in enumerate(SHAPES, 1):
        if needle in ln:
            return i
    raise AssertionError(needle)


class FlowParser(unittest.TestCase):
    """One data_flow row per (source, target) the compiler moves data between."""

    @classmethod
    def setUpClass(cls):
        cls.f = cobol.parse_program("\n".join(SHAPES) + "\n")
        cls.refs = {(n, m, s, ln) for (n, m, s, ln) in cls.f.field_refs}

    def rows(self, line=None, kind=None):
        # ProgramFacts.flows does not exist until the parser stage lands: that
        # AttributeError is the right failure for every assertion below.
        out = list(self.f.flows)
        if line is not None:
            out = [r for r in out if r.line == line]
        if kind is not None:
            out = [r for r in out if r.kind == kind]
        return out

    def pairs(self, line=None, kind=None):
        return {(r.src_name, r.dst_name) for r in self.rows(line, kind)}

    def test_flow_rows_for_each_verb_shape(self):
        with self.subTest("qualified MOVE keeps both qualifiers"):
            ln = _ln("MOVE WS-Q OF REC-A TO WS-Q OF REC-B")
            rs = self.rows(ln, "move")
            self.assertEqual(len(rs), 1, rs)
            self.assertEqual((rs[0].src_name, rs[0].src_qual, rs[0].dst_name, rs[0].dst_qual),
                             ("WS-Q", "REC-A", "WS-Q", "REC-B"))
            self.assertIsNone(rs[0].guard)
        with self.subTest("one-line IF/ELSE: two MOVEs on one line are never cross-paired"):
            ln = _ln("IF WS-N = 1 MOVE WS-X TO WS-Y ELSE")
            self.assertEqual(self.pairs(ln, "move"), {("WS-X", "WS-Y"), ("WS-Y", "WS-X")})
            self.assertNotIn(("WS-X", "WS-X"), self.pairs(ln))
            by = {(r.src_name, r.dst_name): r.guard for r in self.rows(ln, "move")}
            self.assertEqual(by[("WS-X", "WS-Y")], "WS-N = 1")
            self.assertEqual(by[("WS-Y", "WS-X")], "NOT (WS-N = 1)")
        with self.subTest("a subscript is never an operand"):
            ln = _ln("MOVE WS-ENT (WS-I) TO WS-Z")
            rs = self.rows(ln, "move")
            self.assertEqual([(r.src_name, r.src_sub, r.dst_name) for r in rs], [("WS-ENT", "(WS-I)", "WS-Z")])
            self.assertEqual([r for r in self.rows() if r.src_name == "WS-I"], [])
        with self.subTest("ref-mod kept"):
            ln = _ln("MOVE WS-BUF(1:2) TO WS-X")
            rs = self.rows(ln, "move")
            self.assertEqual([(r.src_name, r.dst_name) for r in rs], [("WS-BUF", "WS-X")])
            self.assertIn("1:2", rs[0].src_refmod or "")
        with self.subTest("MOVE CORR"):
            ln = _ln("MOVE CORR REC-A TO REC-B")
            self.assertEqual(self.pairs(ln, "move_corr"), {("REC-A", "REC-B")})
            self.assertEqual(self.pairs(ln, "move"), set())
        with self.subTest("FUNCTION"):
            ln = _ln("MOVE FUNCTION UPPER-CASE(WS-A) TO WS-B")
            rs = self.rows(ln, "function")
            self.assertEqual([(r.src_name, r.dst_name, r.note) for r in rs], [("WS-A", "WS-B", "UPPER-CASE")])
        with self.subTest("one row per target, in order"):
            ln = _ln("MOVE WS-X TO WS-Y WS-Z")
            rs = sorted(self.rows(ln, "move"), key=lambda r: r.ordinal)
            self.assertEqual([(r.src_name, r.dst_name, r.ordinal) for r in rs],
                             [("WS-X", "WS-Y", 0), ("WS-X", "WS-Z", 1)])
        with self.subTest("literal and figurative sources"):
            rs = self.rows(_ln("MOVE 'AB' TO WS-X"), "literal")
            self.assertEqual([(r.src_name, r.dst_name) for r in rs], [(None, "WS-X")])
            self.assertIn("AB", rs[0].src_lit)
            rs = self.rows(_ln("MOVE SPACES TO WS-Y"), "figurative")
            self.assertEqual([(r.src_lit, r.dst_name) for r in rs], [("SPACES", "WS-Y")])
        with self.subTest("COMPUTE / ADD GIVING / ADD TO are arith rows per identifier x target"):
            self.assertEqual(self.pairs(_ln("COMPUTE WS-N = WS-M + WS-P"), "arith"), {("WS-M", "WS-N"), ("WS-P", "WS-N")})
            ln = _ln("ADD WS-M WS-P GIVING WS-N")
            self.assertEqual(self.pairs(ln, "arith"), {("WS-M", "WS-N"), ("WS-P", "WS-N")})
            self.assertIn(("WS-N", "write", "ADD", ln), self.refs)            # LESSONS 174
            self.assertNotIn(("WS-N", "read", "ADD", ln), self.refs)
            self.assertEqual(self.pairs(_ln("ADD WS-M TO WS-N"), "arith"), {("WS-M", "WS-N")})
        with self.subTest("STRING sources -> INTO; DELIMITED BY and POINTER are not operands"):
            ln = _ln("STRING WS-A DELIMITED BY SIZE")
            self.assertEqual(self.pairs(ln, "string"), {("WS-A", "WS-C"), ("WS-B", "WS-C")})
            names = {r.src_name for r in self.rows(ln)} | {r.dst_name for r in self.rows(ln)}
            self.assertFalse(names & {"WS-P", "SIZE", "SPACE"}, names)
        with self.subTest("UNSTRING source -> each INTO; COUNT IN is not a target"):
            ln = _ln("UNSTRING WS-C DELIMITED BY")
            self.assertEqual(self.pairs(ln, "unstring"), {("WS-C", "WS-A"), ("WS-C", "WS-B")})
            self.assertNotIn("WS-N", {r.dst_name for r in self.rows(ln)})
        with self.subTest("INITIALIZE"):
            self.assertEqual({r.dst_name for r in self.rows(_ln("INITIALIZE REC-A"), "initialize")}, {"REC-A"})
        with self.subTest("SET a TO b; SET .. TO TRUE is nothing; SET ADDRESS OF keeps the LINKAGE item"):
            self.assertEqual(self.pairs(_ln("SET WS-I TO WS-N"), "set"), {("WS-N", "WS-I")})
            self.assertEqual(self.rows(_ln("SET WS-FLAG-ON TO TRUE")), [])
            rs = self.rows(_ln("SET ADDRESS OF LK-AREA TO WS-PTR"), "set_address")
            self.assertEqual([(r.src_name, r.dst_name) for r in rs], [("WS-PTR", "LK-AREA")])
        with self.subTest("ACCEPT"):
            rs = self.rows(_ln("ACCEPT WS-DATE FROM DATE"), "accept")
            self.assertEqual([(r.dst_name, r.note) for r in rs], [("WS-DATE", "DATE")])
        with self.subTest("bare READ: io_in per 01 under the FD, noted multi-record"):
            rs = self.rows(_ln("READ IN-F."), "io_in")
            self.assertEqual({r.dst_name for r in rs}, {"IN-REC-A", "IN-REC-B"})
            for r in rs:
                self.assertIn("IN-F", r.note or "")
                self.assertIn("multi-record", r.note or "")
            self.assertEqual([f.fd_records for f in self.f.files if f.select_name == "IN-F"], [["IN-REC-A", "IN-REC-B"]])
        with self.subTest("READ INTO / WRITE FROM"):
            ln = _ln("READ IN-F INTO WS-BUF")
            self.assertEqual({r.dst_name for r in self.rows(ln, "io_in")}, {"IN-REC-A", "IN-REC-B"})
            ri = self.rows(ln, "read_into")
            self.assertEqual({r.dst_name for r in ri}, {"WS-BUF"})
            self.assertTrue({r.src_name for r in ri} <= {"IN-REC-A", "IN-REC-B"}, ri)
            ln = _ln("WRITE OUT-REC FROM WS-BUF")
            io = self.rows(ln, "io_out")
            self.assertEqual([r.src_name for r in io], ["OUT-REC"])
            self.assertIn("OUT-F", io[0].note or "")
            self.assertEqual(self.pairs(ln, "write_from"), {("WS-BUF", "OUT-REC")})
        with self.subTest("INSPECT REPLACING is a self-row"):
            self.assertEqual(self.pairs(_ln("INSPECT WS-A REPLACING"), "inspect"), {("WS-A", "WS-A")})
        with self.subTest("PERFORM VARYING sets the index"):
            rs = self.rows(_ln("PERFORM VARYING WS-I FROM 1"), "set")
            self.assertEqual([r.dst_name for r in rs], ["WS-I"])
        with self.subTest("CALL arguments: how each is passed; RETURNING kept; LENGTH is not a field"):
            ln = _ln("CALL 'S' USING BY CONTENT WS-X")
            call = [c for c in self.f.calls if c.target == "S"][0]
            self.assertEqual([(a.pos, a.name, a.how) for a in call.args],
                             [(1, "WS-X", "content"), (2, "WS-Y", "reference"), (3, "WS-X", "length_of"), (4, "WS-Z", "address_of")])
            self.assertEqual(call.returning, "WS-I")
            rt = self.rows(ln, "returning")
            self.assertEqual([(r.src_name, r.dst_name) for r in rt], [(None, "WS-I")])
            self.assertIn("S", rt[0].note or "")
            self.assertIn(("WS-X", "read", "CALL-USING", ln), self.refs)
            self.assertNotIn(("WS-X", "write", "CALL-USING", ln), self.refs)       # BY CONTENT: one-way
            self.assertIn(("WS-Y", "write", "CALL-USING", ln), self.refs)
            # LENGTH OF / ADDRESS OF are phrases, not fields: neither word may survive as a ref
            self.assertEqual([r for r in self.refs if r[0] in ("LENGTH", "ADDRESS")], [])
            self.assertIn(("WS-I", "write", "CALL-RETURNING", ln), self.refs)   # the RETURNING item is set (LESSONS 174)
        with self.subTest("guard: the enclosing IF, NOT (...) under ELSE, NULL outside"):
            a = self.rows(_ln("    MOVE WS-A TO WS-B"), "move")
            b = self.rows(_ln("    MOVE WS-B TO WS-A"), "move")
            self.assertEqual([r.guard for r in a], ["WS-N > 0"])
            self.assertEqual([r.guard for r in b], ["NOT (WS-N > 0)"])
            self.assertEqual([r.guard for r in self.rows(_ln("MOVE WS-X TO WS-Y WS-Z"))], [None, None])
        with self.subTest("EXEC CICS FROM / INTO / SEND MAP / COMMAREA"):
            o = self.rows(_ln("WRITEQ TS QUEUE('Q1') FROM(WS-Q-AREA)"), "cics_out")
            self.assertEqual([r.src_name for r in o], ["WS-Q-AREA"])
            self.assertIn("WRITEQ", o[0].note)
            self.assertIn("Q1", o[0].note)
            i = self.rows(_ln("READQ TS QUEUE('Q1') INTO(WS-Q-AREA)"), "cics_in")
            self.assertEqual([r.dst_name for r in i], ["WS-Q-AREA"])
            self.assertIn("READQ", i[0].note)
            self.assertIn("Q1", i[0].note)
            m = self.rows(_ln("SEND MAP('M1') MAPSET('S1')"), "cics_out")
            self.assertEqual([r.src_name for r in m], ["WS-MAP-AREA"])
            self.assertIn("SEND", m[0].note)
            self.assertIn("M1", m[0].note)
            link = [c for c in self.f.calls if c.kind == "cics_link"][0]
            self.assertEqual([(a.pos, a.name, a.how) for a in link.args], [(1, "WS-COMM", "commarea")])
        with self.subTest("EXEC CICS SET(ADDRESS OF x): locate mode fills x, not nothing"):
            # ADDRESS is reserved and OF ate LK-AREA as a qualifier: every
            # locate-mode READ / GETMAIN vanished from data_flow.
            rd = self.rows(_ln("READ FILE('F1') SET(ADDRESS OF LK-AREA)"), "cics_in")
            self.assertEqual([(r.dst_name, r.src_name) for r in rd], [("LK-AREA", None)])
            self.assertEqual(rd[0].note, "READ file F1")
            gm = self.rows(_ln("GETMAIN SET(ADDRESS OF LK-AREA)"), "cics_in")
            self.assertEqual([r.dst_name for r in gm], ["LK-AREA"])
            self.assertEqual(gm[0].note, "GETMAIN")
            self.assertNotIn("ADDRESS", {r.dst_name for r in self.rows()})
            ptr = self.rows(_ln("READNEXT FILE('F1') SET(WS-PTR)"), "cics_in")
            self.assertEqual([(r.dst_name, r.note) for r in ptr], [("WS-PTR", "READNEXT file F1")])
        with self.subTest("section marks and PROCEDURE DIVISION RETURNING"):
            marks = list(self.f.section_marks)
            self.assertEqual([(s, fd) for (_ln_, s, fd) in marks],
                             [("FILE", None), ("FILE", "IN-F"), ("FILE", "OUT-F"), ("WORKING-STORAGE", None), ("LINKAGE", None)])
            lines = [m[0] for m in marks]
            self.assertEqual(lines, sorted(lines))
            self.assertEqual(lines[0], _ln("FILE SECTION"))
            self.assertEqual(lines[3], _ln("WORKING-STORAGE SECTION"))
            self.assertEqual(self.f.returning, "LK-RET")
            self.assertEqual(self.f.linkage_using, ["LK-A", "LK-B"])

    def test_name_ending_in_in_is_not_a_qualifier(self):
        # `\b(OF|IN)` matched after the hyphen of WS-Q-IN and blanked `IN TO`, so
        # `MOVE WS-Q-IN TO WM-STAT` left no field_ref and no data_flow row: the
        # CICS chain (READQ INTO WS-Q-IN, MOVE, SEND MAP) stopped one hop early.
        f = cobol.parse_program("\n".join([
            "       IDENTIFICATION DIVISION.",
            "       PROGRAM-ID. QUALIN.",
            "       DATA DIVISION.",
            "       WORKING-STORAGE SECTION.",
            "       01  WS-Q-IN                  PIC X(02).",
            "       01  REC-OF.",
            "           05  WM-STAT              PIC X(02).",
            "       PROCEDURE DIVISION.",
            "           MOVE WS-Q-IN TO WM-STAT IN REC-OF.",
            "           MOVE 'AB' TO WS-Q-IN.",
            "           GOBACK."]) + "\n")
        self.assertIn(("WS-Q-IN", "read", "MOVE", 9), f.field_refs)
        self.assertIn(("WM-STAT", "write", "MOVE", 9), f.field_refs)
        self.assertNotIn("REC-OF", {r[0] for r in f.field_refs})          # a real qualifier is still blanked
        self.assertEqual([(r.src_name, r.dst_name, r.dst_qual) for r in f.flows if r.kind == "move"],
                         [("WS-Q-IN", "WM-STAT", "REC-OF")])
        self.assertIn(("AB", "move_to", "WS-Q-IN", 10), f.literal_refs)

    def test_existing_refs_unchanged(self):
        # The flow pass reads the same statements; the facts it already made
        # must not move by a line or a mode (guard 26). Snapshot taken from the
        # parser before the flow work; regenerate it only for a deliberate change.
        with open(SNAPSHOT, encoding="utf-8") as fh:
            snap = json.load(fh)

        def rows(seq):
            # the host variables of one EXEC SQL come out in set order: compare as a multiset
            return sorted(json.dumps(list(r)) for r in seq)

        for name in ("ERRPGM", "SAMPPGM", "SQLCOLS"):
            text, data, enc = reader.load(os.path.join(FIX, name + ".cbl"))
            f = cobol.parse_program(text, data, enc)
            self.assertEqual(rows(f.field_refs), rows(snap[name]["field_refs"]), name)
            self.assertEqual(rows(f.literal_refs), rows(snap[name]["literal_refs"]), name)


# ---------------------------------------------------------------------------
# index and query
# ---------------------------------------------------------------------------

class FlowPrunedSelfCheck(unittest.TestCase):
    """Guard 21: a WORKING-STORAGE root is stored without its children when
    _flow_names sees no reference into it. If a later edit of that rule misses
    a reference source, the reference lands in a pruned root and `flow` would
    stop there with no signal - so the build's self-check must raise
    flow_pruned for it. The realistic miss is a reference to a CHILD of the
    root (the child has no pfield row at all, so resolve() finds nothing), not
    to the root by name; both, and a dropped 88, must be caught. The rule is
    patched to miss everything; the fixtures are inline and fictional."""

    COPY = ("           05  CP-A            PIC X(4).\n"
            "           05  CP-B            PIC 9(3).\n")
    REPL = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. TSTREPL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-REC.
           COPY TSTCOPY REPLACING ==CP-== BY ==WR-==.
       PROCEDURE DIVISION.
           MOVE 'ABCD' TO WR-A.
           MOVE WR-A TO WR-B.
           GOBACK.
"""
    ROOT = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. TSTROOT.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-A                PIC X.
       PROCEDURE DIVISION.
           MOVE 'Y' TO WS-A.
           GOBACK.
"""
    COND = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. TST88.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-FLAGS.
           05  WS-F            PIC X.
               88  WS-F-ON     VALUE 'Y'.
       PROCEDURE DIVISION.
           IF WS-F-ON DISPLAY 'ON' END-IF.
           GOBACK.
"""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        for name, text in (("TSTCOPY.cpy", cls.COPY), ("TSTREPL.cbl", cls.REPL),
                           ("TSTROOT.cbl", cls.ROOT), ("TST88.cbl", cls.COND)):
            with open(os.path.join(cls.td, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        with mock.patch.object(build, "_flow_names", lambda facts: set()):
            build._main([cls.td, "--db", cls.db, "--rebuild", "--quiet"])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def notes(self, member: str):
        return [r["detail"] for r in self.conn.execute(
            """SELECT u.detail FROM unresolved u JOIN member m ON m.id=u.member_id
               WHERE m.name=? AND u.kind='flow_pruned'""", (member,))]

    def test_reference_into_a_pruned_root_raises_flow_pruned(self):
        c = self.conn
        # the patched rule pruned every WORKING-STORAGE root: root rows only
        self.assertEqual({r[0] for r in c.execute("SELECT name FROM pfield WHERE pruned=1")},
                         {"WS-REC", "WS-A", "WS-FLAGS"})
        self.assertEqual(c.execute("SELECT COUNT(*) FROM pfield WHERE pruned=0").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM pfield WHERE name IN ('WR-A','WR-B','WS-F')").fetchone()[0], 0)
        # the root named by its own reference (it has a row, so resolve() links it)
        self.assertEqual(len(self.notes("TSTROOT")), 1, self.notes("TSTROOT"))
        self.assertIn("(WS-A)", self.notes("TSTROOT")[0])
        # a dropped child named (through the REPLACING rename): no row to link, still a note naming the ROOT
        self.assertEqual(len(self.notes("TSTREPL")), 1, self.notes("TSTREPL"))
        self.assertIn("(WS-REC)", self.notes("TSTREPL")[0])
        # a dropped 88 named
        self.assertEqual(len(self.notes("TST88")), 1, self.notes("TST88"))
        self.assertIn("(WS-FLAGS)", self.notes("TST88")[0])
        # and the rows themselves still carry no link - the note is the only signal
        self.assertEqual([tuple(r) for r in c.execute(
            """SELECT src_pfield, dst_pfield FROM data_flow d JOIN program p ON p.id=d.program_id
               WHERE p.program_id='TSTREPL' AND d.kind='move'""")], [(None, None)])


class FlowSharedStorage(unittest.TestCase):
    """A CALL BY REFERENCE / LINKAGE hop is the SAME storage, not a copy.
    (1) A parameter reached through one CALL is that caller's argument: the
    value cannot come out at another CALL's argument (another invocation, or
    another caller); every caller is right only when the value entered the
    parameter by a copy inside the callee. (2) The bytes keep their place:
    no PIC conversion, no whole-item fill, so a ref-mod on the caller's bytes
    the callee never touches is not a hop (guards 6, 16). Inline, fictional."""

    SUB = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. SHSUB.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-SAVE                  PIC X(02).
       LINKAGE SECTION.
       01  LK-Q                     PIC X(02).
       PROCEDURE DIVISION USING LK-Q.
           MOVE LK-Q TO WS-SAVE.
           MOVE 'OK' TO LK-Q.
           GOBACK.
"""
    KEEP = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. SHKEEP.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-KEPT                  PIC X(02).
       LINKAGE SECTION.
       01  LK-K                     PIC X(02).
       PROCEDURE DIVISION USING LK-K.
           IF WS-KEPT = SPACES
               MOVE LK-K TO WS-KEPT
           ELSE
               MOVE WS-KEPT TO LK-K
           END-IF.
           GOBACK.
"""
    TWO = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. SHTWO.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WF-A                     PIC X(02).
       01  WF-B                     PIC X(02).
       01  WF-C                     PIC X(02).
       PROCEDURE DIVISION.
           CALL 'SHSUB' USING WF-A.
           CALL 'SHSUB' USING WF-B.
           DISPLAY WF-B.
           CALL 'SHKEEP' USING WF-C.
           GOBACK.
"""
    ONE = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. SHONE.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WC-OTHER                 PIC X(02).
       01  WC-KEEP                  PIC X(02).
       PROCEDURE DIVISION.
           CALL 'SHSUB' USING WC-OTHER.
           DISPLAY WC-OTHER.
           CALL 'SHKEEP' USING WC-KEEP.
           DISPLAY WC-KEEP.
           GOBACK.
"""
    PART = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. SHPART.
       DATA DIVISION.
       LINKAGE SECTION.
       01  LK-P                     PIC X(02).
       PROCEDURE DIVISION USING LK-P.
           MOVE 'ZZ' TO LK-P.
           GOBACK.
"""
    TEXT = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. SHTEXT.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WT-TXT                   PIC X(04).
       01  WT-TAIL                  PIC X(02).
       01  WT-NUM                   PIC 9(04).
       PROCEDURE DIVISION.
           CALL 'SHPART' USING WT-TXT.
           MOVE WT-TXT(3:2) TO WT-TAIL.
           DISPLAY WT-TAIL.
           CALL 'SHPART' USING WT-NUM.
           GOBACK.
"""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        src = os.path.join(cls.td, "estate")
        os.makedirs(src)
        for name, text in (("SHSUB.cbl", cls.SUB), ("SHKEEP.cbl", cls.KEEP), ("SHTWO.cbl", cls.TWO),
                           ("SHONE.cbl", cls.ONE), ("SHPART.cbl", cls.PART), ("SHTEXT.cbl", cls.TEXT)):
            with open(os.path.join(src, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([src, "--db", cls.db, "--rebuild", "--quiet"])
        # the same index as it looks before the re-parse (fallback walker)
        cls.old = os.path.join(cls.td, "old.db")
        shutil.copyfile(cls.db, cls.old)
        c = sqlite3.connect(cls.old)
        c.executescript("DROP TABLE data_flow; DROP TABLE pfield;")
        c.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def flow(self, *args, db=None) -> str:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = query._main(["--db", db or self.db, "flow", *args])
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def test_param_reached_through_a_call_goes_back_to_no_other_call(self):
        for db, tag in ((self.db, "exact"), (self.old, "reconstructed")):
            with self.subTest(tag):
                down = self.flow("WF-A", "--program", "SHTWO", db=db)
                self.assertRegex(down, r"CALL SHSUB arg 1 -> SHSUB\.LK-Q")
                self.assertIn("SHSUB.WS-SAVE", down)                 # the callee's own copy is still followed
                for other in ("WF-B", "WC-OTHER", "back to"):
                    self.assertNotIn(other, down, other)
                up = self.flow("WF-B", "--program", "SHTWO", "--up", db=db)
                self.assertRegex(up, r"CALL SHSUB arg 1 <- SHSUB\.LK-Q")
                self.assertIn("'OK'", up)
                for other in ("WF-A", "WC-OTHER"):
                    self.assertNotIn(other, up, other)
                # starting AT the parameter: whatever is in it goes back to every caller (rule 3)
                lk = self.flow("LK-Q", "--program", "SHSUB", db=db)
                for arg in ("SHONE.WC-OTHER", "SHTWO.WF-A", "SHTWO.WF-B"):
                    self.assertRegex(lk, rf"LINKAGE pos 1 -> back to {re.escape(arg)}\b")
        # entered by a copy inside the callee (kept, then moved back into the parameter on a later
        # call): then every caller's argument CAN receive it
        kept = self.flow("WF-C", "--program", "SHTWO", "--hops", "4")
        self.assertRegex(kept, r"MOVE -> SHKEEP\.LK-K\b")
        self.assertRegex(kept, r"LINKAGE pos 1 -> back to SHONE\.WC-KEEP\b")

    def test_by_reference_hops_share_bytes_never_convert(self):
        # the callee's 2 bytes are bytes 1-2 of the caller's 4: not the whole item
        lk = self.flow("LK-P", "--program", "SHPART")
        self.assertRegex(lk, r"LINKAGE pos 1 -> back to SHTEXT\.WT-TXT \(bytes 1-2 of 4\)")
        self.assertRegex(lk, r"LINKAGE pos 1 -> back to SHTEXT\.WT-NUM \(bytes 1-2 of 4\)")
        # WT-TXT(3:2) is bytes SHPART never touches: no hop
        self.assertNotIn("WT-TAIL", lk)
        self.assertNotIn("converted", lk)
        # into the callee: the first 2 bytes, cut, never a PIC conversion
        num = _lines_of(self.flow("WT-NUM", "--program", "SHTEXT"), "CALL SHPART arg 1")
        self.assertRegex(num, r"CALL SHPART arg 1 -> SHPART\.LK-P \(LINKAGE, X\(02\)\); truncated 4 -> 2")
        self.assertNotIn("converted", num)
        # --up: bytes 3-4 of WT-TXT come from no CALL; the whole item from SHPART's write (bytes 1-2)
        tail = self.flow("WT-TAIL", "--program", "SHTEXT", "--up")
        self.assertIn("WT-TXT", tail)
        self.assertNotIn("SHPART", tail)
        txt = self.flow("WT-TXT", "--program", "SHTEXT", "--up")
        self.assertRegex(txt, r"CALL SHPART arg 1 <- SHPART\.LK-P\b")


class FlowWalkEdges(unittest.TestCase):
    """Review fixes on the walk (inline, fictional): a node first reached at
    the hop limit never hides the same node on a shorter path; a RETURNING
    item goes back to the caller downstream as it does under --up; a temp
    dataset in one PROC is not read by a step of another PROC (guard 17);
    `diff` reads a changed line as its whole statement."""

    HOPP = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. HOPP.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  H-A                      PIC X(02).
       01  H-B                      PIC X(02).
       01  H-C                      PIC X(02).
       01  H-D                      PIC X(02).
       01  H-E                      PIC X(02).
       PROCEDURE DIVISION.
           MOVE H-A TO H-B.
           MOVE H-A TO H-D.
           MOVE H-B TO H-C.
           MOVE H-C TO H-D.
           MOVE H-D TO H-E.
           DISPLAY H-E.
           GOBACK.
"""
    RTS = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. RTS.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-CODE                  PIC X(02).
       LINKAGE SECTION.
       01  LK-IN                    PIC X(02).
       01  LK-OUT                   PIC X(02).
       PROCEDURE DIVISION USING LK-IN RETURNING LK-OUT.
           MOVE WS-CODE TO LK-OUT.
           GOBACK.
"""
    RTC = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. RTC.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WC-IN                    PIC X(02).
       01  WC-OUT                   PIC X(02).
       PROCEDURE DIVISION.
           CALL 'RTS' USING WC-IN RETURNING WC-OUT.
           DISPLAY WC-OUT.
           GOBACK.
"""
    TPW = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. TPW.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT OUT-F ASSIGN TO TMPOUT.
       DATA DIVISION.
       FILE SECTION.
       FD  OUT-F.
       01  OUT-REC.
           05  OR-CODE              PIC X(04).
       WORKING-STORAGE SECTION.
       01  WS-CODE                  PIC X(04).
       PROCEDURE DIVISION.
           OPEN OUTPUT OUT-F.
           MOVE WS-CODE TO OR-CODE.
           WRITE OUT-REC.
           CLOSE OUT-F.
           GOBACK.
"""
    TPR = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. TPR.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT IN-F ASSIGN TO TMPIN.
       DATA DIVISION.
       FILE SECTION.
       FD  IN-F.
       01  IN-REC.
           05  IR-CODE              PIC X(04).
       PROCEDURE DIVISION.
           OPEN INPUT IN-F.
           READ IN-F.
           DISPLAY IR-CODE.
           CLOSE IN-F.
           GOBACK.
"""
    # the same temp DSN written in one PROC / job and read in another: never joined
    JCL = (("PROCA.prc", "//PROCA   PROC\n//W1      EXEC PGM=TPW\n//TMPOUT  DD DSN=&&TMP,DISP=(NEW,PASS)\n"),
           ("PROCB.prc", "//PROCB   PROC\n//R1      EXEC PGM=TPR\n//TMPIN   DD DSN=&&TMP,DISP=(OLD,DELETE)\n"),
           ("JOBA.jcl", "//JOBA     JOB (ACCT),'A'\n//W1      EXEC PGM=TPW\n//TMPOUT  DD DSN=&&TMP,DISP=(NEW,PASS)\n"),
           ("JOBB.jcl", "//JOBB     JOB (ACCT),'B'\n//R1      EXEC PGM=TPR\n//TMPIN   DD DSN=&&TMP,DISP=(OLD,DELETE)\n"),
           # one job whose second step reads the first step's temp dataset: joined
           ("JOBC.jcl", "//JOBC     JOB (ACCT),'C'\n//W1      EXEC PGM=TPW\n//TMPOUT  DD DSN=&&PASS1,DISP=(NEW,PASS)\n"
                        "//R1      EXEC PGM=TPR\n//TMPIN   DD DSN=&&PASS1,DISP=(OLD,DELETE)\n"))
    # a MOVE whose TO is on the next line
    DFW = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. DFW.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-A                     PIC X(02).
       01  WS-B                     PIC X(02).
       01  WS-C                     PIC X(02).
       01  WS-Z                     PIC X(02).
       PROCEDURE DIVISION.
           MOVE WS-A
               TO WS-B.
           MOVE WS-B TO WS-C.
           DISPLAY WS-C WS-Z.
           GOBACK.
"""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        src = os.path.join(cls.td, "estate")
        os.makedirs(src)
        files = [("HOPP.cbl", cls.HOPP), ("RTS.cbl", cls.RTS), ("RTC.cbl", cls.RTC), ("TPW.cbl", cls.TPW),
                 ("TPR.cbl", cls.TPR), ("DFW.cbl", cls.DFW)] + list(cls.JCL)
        for name, text in files:
            with open(os.path.join(src, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([src, "--db", cls.db, "--rebuild", "--quiet"])
        cls.old = os.path.join(cls.td, "old.db")
        shutil.copyfile(cls.db, cls.old)
        c = sqlite3.connect(cls.old)
        c.executescript("DROP TABLE data_flow; DROP TABLE pfield;")
        c.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def flow(self, *args, db=None) -> str:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = query._main(["--db", db or self.db, "flow", *args])
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def test_node_cut_at_the_hop_limit_is_expanded_on_a_shorter_path(self):
        # H-A -> H-B -> H-C -> H-D reaches H-D at hop 3 (cut); H-A -> H-D reaches it at hop 1,
        # and H-E is one more hop: it must be there, never hidden behind "(shown as 1.1.1)"
        for db, tag in ((self.db, "exact"), (self.old, "reconstructed")):
            with self.subTest(tag):
                out = self.flow("H-A", "--program", "HOPP", db=db)
                self.assertRegex(out, r"(?m)^1\.1\.1\s+MOVE -> H-D\b.*\[end: hop limit 3\]")
                self.assertRegex(out, r"(?m)^2\s+MOVE -> H-D\b")
                self.assertNotRegex(_lines_of(out, "MOVE -> H-D"), r"(?m)^2\s.*shown as")
                self.assertRegex(out, r"(?m)^2\.1\s+MOVE -> H-E\b")
                self.assertRegex(_lines_of(out, "DISPLAY"), r"HOPP:16")
        # a node reached again at the same or a deeper hop is still shown once (cycles stop)
        again = self.flow("H-B", "--program", "HOPP", "--hops", "5")
        self.assertEqual(again.count("MOVE -> H-E"), 1, again)

    def test_returning_item_goes_back_to_the_caller(self):
        down = self.flow("WS-CODE", "--program", "RTS")
        self.assertRegex(down, r"(?m)^1\s+MOVE -> LK-OUT \(LINKAGE")
        self.assertRegex(down, r"(?m)^1\.1\s+RETURNING -> back to RTC\.WC-OUT\b.*RTC:8 \"CALL 'RTS' USING WC-IN RETURNING WC-OUT\"")
        self.assertRegex(_lines_of(down, "DISPLAY"), r"RTC:9.*\[end: only tested/displayed here\]")
        self.assertNotIn("no further use in RTS", down)
        # the --up mirror, unchanged
        up = self.flow("WC-OUT", "--program", "RTC", "--up")
        self.assertRegex(up, r"RETURNING <- RTS\.LK-OUT\b")
        self.assertRegex(up, r"MOVE <- RTS\.WS-CODE\b")
        # an input parameter nobody writes back never reaches the RETURNING item's caller slot
        self.assertNotIn("WC-OUT", self.flow("LK-IN", "--program", "RTS"))

    def test_temp_dataset_stays_inside_one_job_or_proc(self):
        out = self.flow("WS-CODE", "--program", "TPW")
        # PROCA's &&TMP is not PROCB's, JOBA's is not JOBB's (guard 17)
        self.assertNotIn("PROCB", out)
        self.assertNotIn("JOBB", out)
        for w in ("PROC PROCA W1 DD TMPOUT", "JOBA W1 DD TMPOUT"):
            hop = out.split(w, 1)[1].split("\n", 2)[1]
            self.assertIn("no step reads &&TMP", hop)
            self.assertIn("[end: no indexed reader]", hop)
        # inside one job the next step does read it
        self.assertRegex(out, r"read by TPR\.IR-CODE bytes 1-4 \(JOBC R1 DD TMPIN")
        up = self.flow("IR-CODE", "--program", "TPR", "--up")
        self.assertNotIn("PROCA", up)
        self.assertNotIn("JOBA", up)
        self.assertRegex(up, r"written by TPW\.OR-CODE bytes 1-4 \(JOBC W1 DD TMPOUT")

    def test_diff_reads_a_changed_line_as_its_whole_statement(self):
        with open(os.path.join(self.td, "estate", "DFW.cbl"), encoding="utf-8") as fh:
            src = fh.read()
        conn = query.connect(self.db)
        try:
            for label, old, new in (("the MOVE line", "MOVE WS-A\n", "MOVE WS-Z\n"),
                                    ("the TO line", "TO WS-B.\n", "TO WS-B .\n")):
                with self.subTest(label):
                    path = os.path.join(self.td, label.replace(" ", "_"), "DFW.cbl")
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    self.assertEqual(src.count(old), 1)
                    with open(path, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(src.replace(old, new))
                    d = query.cmd_diff(conn, "DFW", path)
                    self.assertIn("## Flow from the changed statements", d)
                    self.assertIn("**DFW.WS-B**", d)
                    self.assertRegex(d, r"MOVE -> WS-C\b")
                    self.assertNotIn("**DFW.WS-C**", d)          # the unchanged MOVE below is not a start
        finally:
            conn.close()
        # the same widening for a CALL whose USING is on a continuation line, and for a comment in between
        from atlas import flow
        text = ["000100     CALL 'SUBX'",
                "000200*    A COMMENT LINE",
                "000300         USING WS-P",
                "000400               WS-Q.",
                "000500     MOVE WS-R TO WS-S."]
        self.assertEqual(flow.changed_targets(text, True, [0]), ["WS-P", "WS-Q"])
        self.assertEqual(flow.changed_targets(text, True, [3]), ["WS-P", "WS-Q"])
        self.assertEqual(flow.changed_targets(text, True, [4]), ["WS-S"])
        # without the indexes, the lines are the text (as before)
        self.assertEqual(flow.changed_targets(text[:1], True), [])


class FlowCarriersAndInvocations(unittest.TestCase):
    """Second review of the walk (inline, fictional): an MQ queue is its
    WHOLE name (APP.REQ is not APP.REQ.BACKOUT); an ISRT / GU on the I/O PCB
    is the terminal's message, not a database another program's I/O PCB
    shares; a CICS queue named by a variable the parser could not resolve
    is no queue name at all (two programs' WS-QNAME are two queues, guard
    1); RETURN TRANSID .. COMMAREA has its --up mirror; a value that came
    into a callee through ONE CALL and was copied into another parameter or
    the RETURNING item goes back to THAT caller only; and before the
    re-parse, a twice-declared name is listed, never picked (guard 4)."""

    H = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. {p}.\n       DATA DIVISION.\n"
         "       WORKING-STORAGE SECTION.\n")
    MQ = ("       01  WS-MQOD.\n           05  MQOD-OBJECTNAME PIC X(48) VALUE '{q}'.\n"
          "       01  HCONN PIC S9(9) COMP.\n       01  HOBJ PIC S9(9) COMP.\n       01  OPTS PIC S9(9) COMP.\n"
          "       01  CC PIC S9(9) COMP.\n       01  RC PIC S9(9) COMP.\n       01  MQMD PIC X(324).\n"
          "       01  PMO PIC X(128).\n       01  BUFLEN PIC S9(9) COMP.\n")
    MQGET = ("       01  WS-IN.\n           05  WS-IN-CODE PIC X(02).\n       PROCEDURE DIVISION.\n"
             "           CALL 'MQOPEN' USING HCONN WS-MQOD OPTS HOBJ CC RC.\n"
             "           CALL 'MQGET' USING HCONN HOBJ MQMD PMO BUFLEN WS-IN CC RC.\n"
             "           DISPLAY WS-IN-CODE.\n           GOBACK.\n")
    FILES = {
        "MQP.cbl": H.format(p="MQP") + MQ.format(q="APP.REQ") + (
            "       01  WS-MSG.\n           05  WS-M-CODE PIC X(02).\n       PROCEDURE DIVISION.\n"
            "           CALL 'MQOPEN' USING HCONN WS-MQOD OPTS HOBJ CC RC.\n"
            "           CALL 'MQPUT' USING HCONN HOBJ MQMD PMO BUFLEN WS-MSG CC RC.\n           GOBACK.\n"),
        "MQG1.cbl": H.format(p="MQG1") + MQ.format(q="APP.REQ.BACKOUT") + MQGET,
        "MQG2.cbl": H.format(p="MQG2") + MQ.format(q="APP.REQ") + MQGET,
        # the queue name is built at run time: QUEUE(WS-QNAME) is not a name; 'FIXQ' is
        "CQW.cbl": H.format(p="CQW") + (
            "       01  WS-TERM PIC X(4).\n       01  WS-QNAME PIC X(8).\n"
            "       01  WS-REC.\n           05  WS-R-CODE PIC X(02).\n       PROCEDURE DIVISION.\n"
            "           STRING 'CQW' WS-TERM DELIMITED BY SIZE INTO WS-QNAME.\n"
            "           EXEC CICS WRITEQ TS QUEUE(WS-QNAME) FROM(WS-REC) END-EXEC.\n"
            "           EXEC CICS WRITEQ TS QUEUE('FIXQ') FROM(WS-REC) END-EXEC.\n           GOBACK.\n"),
        "CQR.cbl": H.format(p="CQR") + (
            "       01  WS-TERM PIC X(4).\n       01  WS-QNAME PIC X(8).\n"
            "       01  WS-IN.\n           05  WS-IN-CODE PIC X(02).\n       PROCEDURE DIVISION.\n"
            "           STRING 'CQR' WS-TERM DELIMITED BY SIZE INTO WS-QNAME.\n"
            "           EXEC CICS READQ TS QUEUE(WS-QNAME) INTO(WS-IN) END-EXEC.\n"
            "           EXEC CICS READQ TS QUEUE('FIXQ') INTO(WS-IN) END-EXEC.\n"
            "           DISPLAY WS-IN-CODE.\n           GOBACK.\n"),
        # RETURN TRANSID('TR02') COMMAREA: TR02 runs CSN (CSD), which gets it as DFHCOMMAREA
        "CST.cbl": H.format(p="CST") + (
            "       01  WS-CA.\n           05  WS-CA-CODE PIC X(02).\n       01  WS-D-CODE PIC X(02).\n"
            "       PROCEDURE DIVISION.\n           MOVE WS-D-CODE TO WS-CA-CODE.\n"
            "           EXEC CICS RETURN TRANSID('TR02') COMMAREA(WS-CA) END-EXEC.\n           GOBACK.\n"),
        "CSN.cbl": H.format(p="CSN") + (
            "       LINKAGE SECTION.\n       01  DFHCOMMAREA.\n           05  CA-CODE PIC X(02).\n"
            "       PROCEDURE DIVISION.\n           DISPLAY CA-CODE.\n           EXEC CICS RETURN END-EXEC.\n"),
        "CSD1.csd": "DEFINE TRANSACTION(TR02) GROUP(G1) PROGRAM(CSN)\n",
        # a parameter copied into the RETURNING item / into another parameter: one caller's
        "RTS.cbl": H.format(p="RTS") + (
            "       LINKAGE SECTION.\n       01  LK-IN PIC X(02).\n       01  LK-OUT PIC X(02).\n"
            "       PROCEDURE DIVISION USING LK-IN RETURNING LK-OUT.\n           MOVE LK-IN TO LK-OUT.\n"
            "           GOBACK.\n"),
        "LKS.cbl": H.format(p="LKS") + (
            "       LINKAGE SECTION.\n       01  LK-A PIC X(02).\n       01  LK-B PIC X(02).\n"
            "       PROCEDURE DIVISION USING LK-A LK-B.\n           MOVE LK-A TO LK-B.\n           GOBACK.\n"),
        # a name declared twice (qualified where it is written)
        "AMB.cbl": H.format(p="AMB") + (
            "       01  AM-SRC PIC X(02).\n       01  A-1.\n           05  AM-X PIC X(02).\n"
            "       01  A-2.\n           05  AM-X PIC X(02).\n       PROCEDURE DIVISION.\n"
            "           MOVE AM-SRC TO AM-X OF A-2.\n           DISPLAY AM-X OF A-1.\n           GOBACK.\n"),
        # two MPPs, each GU its input message and ISRT its reply on its own I/O PCB
        "MPA.psb": ("         PCB   TYPE=DB,DBDNAME=POLDBD,PROCOPT=G,KEYLEN=12\n"
                    "         SENSEG NAME=POLICY,PARENT=0\n         PSBGEN LANG=COBOL,PSBNAME=MPA\n"),
        "MPB.psb": ("         PCB   TYPE=DB,DBDNAME=POLDBD,PROCOPT=G,KEYLEN=12\n"
                    "         SENSEG NAME=POLICY,PARENT=0\n         PSBGEN LANG=COBOL,PSBNAME=MPB\n"),
        "STG1.imsgen": ("         APPLCTN PSB=MPA,PGMTYPE=TP\n         TRANSACT CODE=TRA\n"
                        "         APPLCTN PSB=MPB,PGMTYPE=TP\n         TRANSACT CODE=TRB\n"),
    }
    for _c in ("RTX", "RTY"):
        FILES[f"{_c}.cbl"] = H.format(p=_c) + (
            "       01  WC-IN PIC X(02).\n       01  WC-OUT PIC X(02).\n       PROCEDURE DIVISION.\n"
            "           CALL 'RTS' USING WC-IN RETURNING WC-OUT.\n           DISPLAY WC-OUT.\n           GOBACK.\n")
    for _c, _p in (("LKX", "WX"), ("LKY", "WY")):
        FILES[f"{_c}.cbl"] = H.format(p=_c) + (
            f"       01  {_p}-A PIC X(02).\n       01  {_p}-B PIC X(02).\n       PROCEDURE DIVISION.\n"
            f"           CALL 'LKS' USING {_p}-A {_p}-B.\n           DISPLAY {_p}-B.\n           GOBACK.\n")
    for _c in ("MPA", "MPB"):
        FILES[f"{_c}.cbl"] = H.format(p=_c) + (
            "       01  WS-GU PIC X(4) VALUE 'GU  '.\n       01  WS-ISRT PIC X(4) VALUE 'ISRT'.\n"
            "       01  WS-IN.\n           05  WS-IN-CODE PIC X(02).\n"
            "       01  WS-OUT.\n           05  WS-OUT-CODE PIC X(02).\n"
            "       LINKAGE SECTION.\n       01  IO-PCB PIC X(20).\n       01  DB-PCB PIC X(40).\n"
            "       PROCEDURE DIVISION USING IO-PCB DB-PCB.\n"
            "           CALL 'CBLTDLI' USING WS-GU IO-PCB WS-IN.\n"
            "           MOVE WS-IN-CODE TO WS-OUT-CODE.\n"
            "           CALL 'CBLTDLI' USING WS-ISRT IO-PCB WS-OUT.\n           GOBACK.\n")
    del _c, _p

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        src = os.path.join(cls.td, "estate")
        os.makedirs(src)
        for name, text in cls.FILES.items():
            with open(os.path.join(src, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([src, "--db", cls.db, "--rebuild", "--quiet"])
        # his work index before the re-parse: none of the five tables
        cls.old = os.path.join(cls.td, "old.db")
        shutil.copyfile(cls.db, cls.old)
        c = sqlite3.connect(cls.old)
        c.executescript("DROP TABLE data_flow; DROP TABLE pfield; DROP TABLE call_arg; DROP TABLE param; "
                        "DROP TABLE file_record;")
        c.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def flow(self, *args, db=None) -> str:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = query._main(["--db", db or self.db, "flow", *args])
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def test_mq_queue_is_its_whole_name(self):
        down = self.flow("WS-MSG", "--program", "MQP")
        self.assertRegex(down, r"MQ APP\.REQ -> MQG2\.WS-IN-CODE\b")
        self.assertNotIn("MQG1", down)                      # reads APP.REQ.BACKOUT
        up = self.flow("WS-IN-CODE", "--program", "MQG1", "--up")
        self.assertNotIn("MQP", up)
        self.assertIn("[end: no indexed writer]", up)
        self.assertRegex(self.flow("WS-IN-CODE", "--program", "MQG2", "--up"), r"MQ APP\.REQ <- MQP\.WS-M-CODE\b")

    def test_io_pcb_message_is_the_terminal_not_a_database(self):
        down = self.flow("WS-IN-CODE", "--program", "MPA")
        self.assertIn("[end: IMS message to the terminal / I/O PCB - not a database]", down)
        self.assertNotIn("MPB", down)
        self.assertNotIn("*IO-PCB*", down)
        self.assertNotIn("matched by DBD", down)
        up = self.flow("WS-OUT-CODE", "--program", "MPA", "--up")
        self.assertIn("[end: IMS message from the terminal / I/O PCB - not a database]", up)
        self.assertNotIn("MPB", up)

    def test_cics_queue_named_by_a_variable_is_not_joined(self):
        down = self.flow("WS-REC", "--program", "CQW")
        hop = _lines_of(down, "QUEUE(WS-QNAME)")
        self.assertIn("[end: queue name not resolvable (variable WS-QNAME)]", hop)
        self.assertNotIn("CQR:11", down)                    # CQR's own WS-QNAME: another queue
        self.assertRegex(down, r"WRITEQ TSQ FIXQ -> READQ into CQR\.WS-IN-CODE\b")   # a literal name still joins
        up = self.flow("WS-IN-CODE", "--program", "CQR", "--up")
        self.assertIn("[end: queue name not resolvable (variable WS-QNAME)]", _lines_of(up, "QUEUE(WS-QNAME)"))
        self.assertNotIn("CQW:11", up)
        self.assertRegex(up, r"READQ TSQ FIXQ <- WRITEQ from CQW\.WS-R-CODE\b")

    def test_return_transid_commarea_has_its_up_mirror(self):
        down = self.flow("WS-D-CODE", "--program", "CST")
        self.assertRegex(down, r"RETURN TR02 -> CSN\.CA-CODE \(LINKAGE DFHCOMMAREA bytes 1-2, X\(02\)\); by CICS convention")
        up = self.flow("CA-CODE", "--program", "CSN", "--up")
        self.assertRegex(up, r"(?m)^1\s+RETURN TR02 COMMAREA <- CST\.WS-CA-CODE; by CICS convention\s+"
                             r"CST:\d+ \"RETURN TRANSID\('TR02'\) COMMAREA\(WS-CA\"")
        self.assertRegex(up, r"MOVE <- CST\.WS-D-CODE\b")

    def test_a_parameter_copied_inside_the_callee_goes_back_to_its_own_caller(self):
        # RETURNING: RTX's value reaches RTX.WC-OUT, never RTY's (RTY's LK-IN is RTY's argument)
        down = self.flow("WC-IN", "--program", "RTX")
        self.assertRegex(down, r"MOVE -> RTS\.LK-OUT\b")
        self.assertRegex(down, r"RETURNING -> back to RTX\.WC-OUT\b")
        self.assertNotIn("RTY", down)
        up = self.flow("WC-OUT", "--program", "RTX", "--up")
        self.assertRegex(up, r"CALL RTS arg 1 <- RTX\.WC-IN\b")
        self.assertNotIn("RTY", up)
        # started AT the item nobody called through: whatever is in it goes back to every caller (rule 3)
        at = self.flow("LK-OUT", "--program", "RTS")
        for c in ("RTX", "RTY"):
            self.assertRegex(at, rf"RETURNING -> back to {c}\.WC-OUT\b")
        # LINKAGE position 2, reached by MOVE from position 1: the same invocation, exact and reconstructed
        for db, tag in ((self.db, "exact"), (self.old, "reconstructed")):
            with self.subTest(tag):
                lk = self.flow("WX-A", "--program", "LKX", db=db)
                self.assertRegex(lk, r"LINKAGE pos 2 -> back to LKX\.WX-B\b")
                self.assertNotIn("LKY", lk)
                lk_up = self.flow("WX-B", "--program", "LKX", "--up", db=db)
                self.assertRegex(lk_up, r"CALL LKS arg 1 <- LKX\.WX-A\b")
                self.assertNotIn("LKY", lk_up)
                at = self.flow("LK-B", "--program", "LKS", db=db)
                for arg in ("LKX.WX-B", "LKY.WY-B"):
                    self.assertRegex(at, rf"LINKAGE pos 2 -> back to {re.escape(arg)}\b")

    def test_fallback_twice_declared_name_is_listed_not_picked(self):
        # guard 4 before the re-parse: field_ref drops OF/IN, so no statement on AM-X is either one's
        out = self.flow("AM-X", "--program", "AMB", db=self.old)
        self.assertRegex(out, r"AM-X is declared 2 times in AMB: A-1\.AM-X \(AMB:\d+ \"05  AM-X\"\), "
                              r"A-2\.AM-X \(AMB:\d+ \"05  AM-X\"\) - HUMAN MUST VERIFY")
        self.assertNotIn("- defined", out)
        self.assertNotIn("DISPLAY", out)
        self.assertIn("[end: ambiguous name - HUMAN MUST VERIFY]", out)
        # a qualified start is defined at ITS line, and still not followed (its statements are both items')
        q = self.flow("AM-X OF A-2", "--program", "AMB", db=self.old)
        self.assertIn(f'- defined AMB:{self.line("AMB.cbl", "A-2.") + 1} "05  AM-X"', q)
        self.assertIn("not followed until re-parse", q)
        self.assertNotIn("DISPLAY", q)
        # reached by a MOVE: the hop ends there, nothing of A-1's AM-X is attributed to it
        hop = self.flow("AM-SRC", "--program", "AMB", db=self.old)
        self.assertRegex(hop, r"MOVE -> AM-X \(reconstructed\).*\(AM-X is declared 2 times in AMB\)\s+"
                              r"\[end: ambiguous name - HUMAN MUST VERIFY\]")
        self.assertNotIn("DISPLAY", hop)
        # the exact walker on the same names (unchanged)
        self.assertIn("HUMAN MUST VERIFY", self.flow("AM-X", "--program", "AMB"))
        self.assertIn(f'- defined AMB:{self.line("AMB.cbl", "A-1.") + 1} "05  AM-X"', self.flow("AM-X OF A-1", "--program", "AMB"))

    def line(self, member: str, needle: str) -> int:
        for i, ln in enumerate(self.FILES[member].splitlines(), 1):
            if needle in ln:
                return i
        raise AssertionError(needle)


class FlowBytesParentsXctl(unittest.TestCase):
    """Third review of the walk (inline, fictional): an elementary MOVE
    between items of different lengths keeps the value in its bytes - the
    padding of a longer receiver and the cut bytes of a longer sender never
    carry it (guard 6, both ways); before the re-parse a group above the item
    that is moved or passed is a labelled end, never `no further use` (guard
    7); XCTL never returns, so nothing written in the program it transfers
    to goes back to the program that XCTLed."""

    H = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. {p}.\n       DATA DIVISION.\n"
         "       WORKING-STORAGE SECTION.\n")
    FILES = {
        "PA.cbl": H.format(p="PA") + (
            "       01  WS-SRC.\n           05  S-PFX PIC X(10).\n           05  S-CODE PIC X(04).\n"
            "           05  S-REST PIC X(26).\n       01  WS-SMALL PIC X(12).\n       01  WS-TXT PIC X(04).\n"
            "       01  WS-DST.\n           05  D-A PIC X(08).\n           05  D-B PIC X(04).\n"
            "       PROCEDURE DIVISION.\n           MOVE WS-SRC TO WS-SMALL.\n           MOVE WS-SMALL TO WS-TXT.\n"
            "           MOVE WS-SRC TO WS-DST.\n           DISPLAY WS-TXT.\n           GOBACK.\n"),
        "PC.cbl": H.format(p="PC") + (
            "       01  WS-TXT PIC X(04).\n       01  WS-SMALL PIC X(12).\n"
            "       01  WS-DST.\n           05  D-A PIC X(08).\n           05  D-B PIC X(04).\n"
            "       PROCEDURE DIVISION.\n           MOVE WS-TXT TO WS-SMALL.\n           MOVE WS-SMALL TO WS-DST.\n"
            "           DISPLAY D-A.\n           DISPLAY D-B.\n           GOBACK.\n"),
        # XA transfers control to XB with XCTL, XL LINKs to it: only XL gets XB's write back
        "XA.cbl": H.format(p="XA") + (
            "       01  WS-CA.\n           05  WA-CODE PIC X(02).\n       PROCEDURE DIVISION.\n"
            "           EXEC CICS XCTL PROGRAM('XB') COMMAREA(WS-CA) END-EXEC.\n           DISPLAY WA-CODE.\n"
            "           GOBACK.\n"),
        "XL.cbl": H.format(p="XL") + (
            "       01  WS-CA.\n           05  WL-CODE PIC X(02).\n       PROCEDURE DIVISION.\n"
            "           EXEC CICS LINK PROGRAM('XB') COMMAREA(WS-CA) END-EXEC.\n           DISPLAY WL-CODE.\n"
            "           GOBACK.\n"),
        "XB.cbl": H.format(p="XB") + (
            "       01  WB-NEW PIC X(02).\n       LINKAGE SECTION.\n       01  DFHCOMMAREA.\n"
            "           05  CB-CODE PIC X(02).\n       PROCEDURE DIVISION.\n           MOVE WB-NEW TO CB-CODE.\n"
            "           EXEC CICS RETURN END-EXEC.\n"),
        # USING DFHCOMMAREA: the shape the fallback sees as a USING position
        "XD.cbl": H.format(p="XD") + (
            "       01  WD-CA PIC X(02).\n       PROCEDURE DIVISION.\n"
            "           EXEC CICS XCTL PROGRAM('XC') COMMAREA(WD-CA) END-EXEC.\n           DISPLAY WD-CA.\n"
            "           GOBACK.\n"),
        "XC.cbl": H.format(p="XC") + (
            "       01  WC-NEW PIC X(02).\n       LINKAGE SECTION.\n       01  DFHCOMMAREA PIC X(02).\n"
            "       PROCEDURE DIVISION USING DFHCOMMAREA.\n           MOVE WC-NEW TO DFHCOMMAREA.\n"
            "           EXEC CICS RETURN END-EXEC.\n"),
    }

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        src = os.path.join(cls.td, "estate")
        os.makedirs(src)
        for name, text in cls.FILES.items():
            with open(os.path.join(src, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([src, "--db", cls.db, "--rebuild", "--quiet"])
        cls.old = os.path.join(cls.td, "old.db")
        shutil.copyfile(cls.db, cls.old)
        c = sqlite3.connect(cls.old)
        c.executescript("DROP TABLE data_flow; DROP TABLE pfield; DROP TABLE call_arg; DROP TABLE param; "
                        "DROP TABLE file_record;")
        c.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def flow(self, *args, db=None) -> str:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = query._main(["--db", db or self.db, "flow", *args])
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def test_elementary_move_keeps_the_value_in_its_bytes(self):
        # --up: WS-TXT gets bytes 1-4 of WS-SMALL, which came from S-PFX - never S-CODE (bytes 11-14 of WS-SRC)
        up = self.flow("WS-TXT", "--program", "PA", "--up")
        self.assertRegex(up, r"(?m)^1\s+MOVE <- WS-SMALL \(bytes 1-4 of 12\); truncated 12 -> 4\s+PA:\d+ \"MOVE WS-SMALL TO WS-TXT\"")
        self.assertRegex(up, r"(?m)^1\.1\s+MOVE <- S-PFX \(bytes 1-4 of 10\)")
        self.assertNotIn("S-CODE", up)
        self.assertNotIn("S-REST", up)
        # down: WS-TXT fills bytes 1-4 of WS-SMALL; the rest is padding, so D-B (bytes 9-12) is not reached
        down = self.flow("WS-TXT", "--program", "PC")
        self.assertRegex(down, r"(?m)^1\s+MOVE -> WS-SMALL \(bytes 1-4 of 12\)\s+PC:\d+ \"MOVE WS-TXT TO WS-SMALL\"")
        self.assertRegex(down, r"(?m)^1\.1\s+MOVE -> D-A \(bytes 1-4 of 8\)")
        self.assertNotIn("D-B", down)
        self.assertNotIn("unnamed bytes", down)           # an elementary MOVE is not a group MOVE
        # the padding bytes, followed up, end where the sender was shorter (unchanged)
        self.assertIn("[end: truncated 12 -> 4]", self.flow("D-B", "--program", "PC", "--up"))
        # a longer sender is cut: the whole shorter receiver, labelled
        self.assertRegex(self.flow("WS-SMALL", "--program", "PA"), r"(?m)^1\s+MOVE -> WS-TXT; truncated 12 -> 4\s")

    def test_fallback_labels_a_moved_or_passed_parent_group(self):
        down = self.flow("S-PFX", "--program", "PA", db=self.old)
        hops = _lines_of(down, "parent group WS-SRC used by MOVE")
        self.assertEqual(len(hops.splitlines()), 2, down)    # MOVE WS-SRC TO WS-SMALL, MOVE WS-SRC TO WS-DST
        self.assertIn("(reconstructed)", hops)
        self.assertIn("[end: parent group not followed until re-parse]", hops)
        self.assertNotIn("no further use", down)
        up = self.flow("D-A", "--program", "PA", "--up", db=self.old)
        self.assertRegex(up, r"parent group WS-DST set by MOVE: its bytes include D-A \(reconstructed\)\s+"
                             r"PA:\d+ \"MOVE WS-SRC TO WS-DST\"\s+\[end: parent group not followed until re-parse\]")
        self.assertNotIn("no further use", up)
        # an item nobody moves or passes, alone or through a group, still ends as before
        self.assertIn("[end: no further use in PC]", self.flow("WS-TXT", "--program", "PC", "--up", db=self.old))

    def test_xctl_never_returns_the_commarea(self):
        down = self.flow("WB-NEW", "--program", "XB")
        self.assertNotRegex(down, r"back to XA\.")
        self.assertRegex(down, r"LINKAGE COMMAREA: XA\.WS-CA was passed by XCTL\s+XA:\d+ \"XCTL PROGRAM\('XB'\) "
                               r"COMMAREA\(WS-CA\"\s+\[end: XCTL does not return\]")
        self.assertRegex(down, r"LINKAGE COMMAREA -> back to XL\.WL-CODE \(BY REFERENCE\)")   # LINK does return
        up = self.flow("WA-CODE", "--program", "XA", "--up")
        self.assertNotIn("written there, BY REFERENCE", up)
        self.assertNotIn("WB-NEW", up)
        self.assertIn("[end: XCTL does not return]", up)
        self.assertRegex(self.flow("WL-CODE", "--program", "XL", "--up"), r"LINK XB COMMAREA <- XB\.CB-CODE\b.*written there")
        # into the program XCTLed to, the COMMAREA still arrives
        self.assertRegex(self.flow("WA-CODE", "--program", "XA"), r"XCTL XB arg 1 -> XB\.CB-CODE\b")
        # before the re-parse: USING DFHCOMMAREA is a USING position, still not a way back through XCTL
        old_down = self.flow("WC-NEW", "--program", "XC", db=self.old)
        self.assertNotRegex(old_down, r"back to XD\.")
        self.assertIn("[end: XCTL does not return]", _lines_of(old_down, "XD.WD-CA was passed by XCTL"))
        old_up = self.flow("WD-CA", "--program", "XD", "--up", db=self.old)
        self.assertNotRegex(old_up, r"<- XC\.")
        self.assertIn("[end: XCTL does not return]", _lines_of(old_up, "XC.DFHCOMMAREA is written there"))


class FlowIndex(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.db = os.path.join(cls.td, "t.db")
        build._main([FIX, "--db", cls.db, "--rebuild", "--quiet"])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def flow(self, *args, db=None) -> str:
        """`flow` through the command line, so the options are the plan's."""
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = query._main(["--db", db or self.db, "flow", *args])
        except SystemExit as e:           # argparse: no such command
            raise AssertionError(f"atlas.query has no `flow` command (exit {e.code}): {err.getvalue().strip()[-200:]}")
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def pfield(self, program: str, name: str):
        return self.conn.execute("""SELECT f.* FROM pfield f JOIN program p ON p.id=f.program_id
                                    WHERE p.program_id=? AND f.name=? ORDER BY f.id""", (program, name)).fetchall()

    def pid(self, program: str) -> int:
        return self.conn.execute("SELECT id FROM program WHERE program_id=?", (program,)).fetchone()[0]

    # ---- build ---------------------------------------------------------------

    def test_pfield_sections_offsets_and_copy_link(self):
        c = self.conn
        fr = self.pfield("FLOWSRC", "FR-STATUS")
        self.assertEqual(len(fr), 1, fr)
        fr = fr[0]
        self.assertEqual((fr["section"], fr["fd_select"], fr["offset"], fr["length"]), ("FILE", "POL-IN", 13, 2))
        root = c.execute("SELECT name FROM pfield WHERE id=?", (fr["root_id"],)).fetchone()
        self.assertEqual(root["name"], "POL-REC")                  # one prefix byte: 13 here, 12 in the copybook
        cp = c.execute("SELECT f.offset, m.name FROM field f JOIN member m ON m.id=f.member_id WHERE f.id=?",
                       (fr["copy_field_id"],)).fetchone()
        self.assertEqual((cp["name"], cp["offset"]), ("FLOWREC", 12))
        self.assertEqual(self.pfield("FLOWSRC", "FR-FILLER")[0]["after_odo"], 1)
        self.assertEqual(self.pfield("FLOWSRC", "FR-COV-COUNT")[0]["after_odo"], 0)
        self.assertEqual(self.pfield("FLOWSRC", "LK-AREA")[0]["section"], "LINKAGE")
        self.assertEqual(self.pfield("FLOWSRC", "WS-STATUS")[0]["section"], "WORKING-STORAGE")
        self.assertEqual(self.pfield("FLOWSUB", "LK-STATUS")[0]["section"], "LINKAGE")
        # a WORKING-STORAGE root nothing names is stored alone, pruned
        unused = self.pfield("FLOWSRC", "WS-COMM-AREA")[0]
        self.assertEqual(unused["pruned"], 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM pfield WHERE root_id=? AND id<>?",
                                   (unused["id"], unused["id"])).fetchone()[0], 0)
        self.assertEqual(self.pfield("FLOWSRC", "CA-STATUS"), [])
        self.assertEqual(self.pfield("FLOWCOMM", "CA-STATUS")[0]["section"], "LINKAGE")   # the same copybook, kept whole
        # self-check: nothing points into a pruned root
        self.assertEqual(c.execute("SELECT COUNT(*) FROM unresolved WHERE kind='flow_pruned'").fetchone()[0], 0)
        # two WS-X under two 01s, named without a qualifier: nobody is picked
        amb = c.execute("""SELECT u.detail FROM unresolved u JOIN member m ON m.id=u.member_id
                           WHERE m.name='FLOWSRC' AND u.kind='ambiguous_field'""").fetchall()
        self.assertTrue(any("WS-X" in r["detail"] for r in amb), amb)
        wsx = c.execute("SELECT dst_pfield FROM data_flow WHERE program_id=? AND dst_name='WS-X'", (self.pid("FLOWSRC"),)).fetchall()
        self.assertTrue(wsx)
        self.assertEqual([r["dst_pfield"] for r in wsx], [None] * len(wsx))
        # callee side: PROCEDURE DIVISION USING (entry NULL), the ENTRY with its own order, RETURNING at pos 0, DFHCOMMAREA
        params = c.execute("SELECT entry, pos, name FROM param WHERE program_id=? ORDER BY entry, pos", (self.pid("FLOWSUB"),)).fetchall()
        self.assertEqual([tuple(r) for r in params],
                         [(None, 0, "LK-RET"), (None, 1, "LK-STATUS"), (None, 2, "LK-RC"),
                          ("FLOWENT", 1, "LK-RC"), ("FLOWENT", 2, "LK-STATUS")])
        ca = c.execute("SELECT entry, pos, name, pfield FROM param WHERE program_id=?", (self.pid("FLOWCOMM"),)).fetchall()
        self.assertEqual([(r["entry"], r["pos"], r["name"]) for r in ca], [("DFHCOMMAREA", 1, "DFHCOMMAREA")])
        self.assertEqual(ca[0]["pfield"], self.pfield("FLOWCOMM", "DFHCOMMAREA")[0]["id"])
        # every 01 under an FD, in order
        recs = c.execute("""SELECT fd.select_name, r.ordinal, r.name FROM file_record r JOIN file_decl fd ON fd.id=r.file_id
                            WHERE r.program_id=? ORDER BY fd.select_name, r.ordinal""", (self.pid("FLOWSRC"),)).fetchall()
        self.assertEqual([tuple(r) for r in recs], [("POL-IN", 1, "POL-REC"), ("STAT-OUT", 1, "OUT-REC"), ("STAT-OUT", 2, "OUT-TRL")])
        # the caller side, and the new columns on the old tables
        wst = self.pfield("FLOWSRC", "WS-STATUS")[0]["id"]
        # `returning` is an SQLite keyword (the RETURNING clause): the column is returning_item
        edge = c.execute("SELECT id, returning_item FROM call_edge WHERE program_id=? AND returning_item IS NOT NULL",
                         (self.pid("FLOWSRC"),)).fetchall()
        self.assertEqual([r["returning_item"] for r in edge], ["WS-R"])
        args = c.execute("SELECT pos, name, how FROM call_arg WHERE call_id=? ORDER BY pos", (edge[0]["id"],)).fetchall()
        self.assertEqual([tuple(r) for r in args], [(1, "WS-NOTE", "content"), (2, "WS-NOTE", "length_of")])
        link = c.execute("""SELECT a.pos, a.name, a.how FROM call_arg a JOIN call_edge e ON e.id=a.call_id
                            WHERE e.program_id=? AND e.kind='cics_link'""", (self.pid("FLOWCICS"),)).fetchall()
        self.assertEqual([tuple(r) for r in link], [(1, "WS-COMM", "commarea")])
        self.assertEqual({r[0] for r in c.execute("SELECT pfield_id FROM field_ref WHERE program_id=? AND name='WS-STATUS'",
                                                  (self.pid("FLOWSRC"),))}, {wst})
        self.assertEqual({r[0] for r in c.execute("SELECT pfield_id FROM sql_col_ref WHERE program_id=? AND host_var='WS-STATUS'",
                                                  (self.pid("FLOWSRC"),))}, {wst})
        mv = c.execute("""SELECT line, src_pfield, dst_pfield FROM data_flow WHERE program_id=? AND kind='move'
                          AND src_name='WS-STATUS' AND dst_name='OR-STAT'""", (self.pid("FLOWSRC"),)).fetchall()
        self.assertEqual(len(mv), 1, mv)
        # data_flow.line is the EXPANDED line of the verb, as field_ref stores it (plan section 1): the two
        # COPYs above the PROCEDURE DIVISION push it past the physical line, and origin() maps it back for
        # the cite. A build that stored the physical line would agree with _line and break every cite.
        fr_line = c.execute("""SELECT line FROM field_ref WHERE program_id=? AND name='OR-STAT' AND mode='write'
                               AND stmt='MOVE'""", (self.pid("FLOWSRC"),)).fetchall()
        self.assertEqual([r["line"] for r in fr_line], [mv[0]["line"]])
        src_line = _line("FLOWSRC.cbl", "MOVE WS-STATUS TO OR-STAT")
        self.assertNotEqual(mv[0]["line"], src_line)
        self.assertEqual(query.origin(c, self.pid("FLOWSRC"), mv[0]["line"])[1], src_line)
        self.assertEqual((mv[0]["src_pfield"], mv[0]["dst_pfield"]), (wst, self.pfield("FLOWSRC", "OR-STAT")[0]["id"]))
        self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='expand_map'").fetchone())

    # ---- the chain --------------------------------------------------------------

    def test_flow_three_hop_chain_in_order(self):
        out = self.flow("WS-STATUS", "--program", "FLOWSRC")
        L = lambda needle, member="FLOWSRC.cbl": _line(member, needle)      # noqa: E731
        head = out.split("\n", 1)[0]
        self.assertTrue(head.startswith("<!-- flow FLOWSRC.WS-STATUS: ~"), head)
        self.assertIn("down, hops 3", head)
        self.assertIn("# Flow of FLOWSRC.WS-STATUS (downstream", out)
        self.assertRegex(out, rf'defined FLOWSRC:{L("01  WS-STATUS")} "01  WS-STATUS" \(WORKING-STORAGE, X\(02\)\)')
        self.assertIn("also written by", out)
        self.assertRegex(out, rf'set from FR-STATUS \(FILE POL-REC .*FLOWREC:{L("FR-STATUS", "FLOWREC.cpy")}.*'
                              rf'FLOWSRC:{L("MOVE FR-STATUS TO WS-STATUS")} "MOVE FR-STATUS TO WS-STATUS"')
        lp_line = L("MOVE 'LP' TO WS-STATUS")
        self.assertRegex(out, rf"also set here: MOVE 'LP'\s+FLOWSRC:{lp_line} .MOVE 'LP' TO WS-STATUS.\s+guard: WS-RC = 'OK'")

        # 1. the file: MOVE to the record, WRITE, the job, a plain sort passes the bytes through, the reader
        self.assertRegex(out, rf'MOVE -> OR-STAT \(FILE OUT-REC bytes 13-14.*FLOWSRC:{L("MOVE WS-STATUS TO OR-STAT")} "MOVE WS-STATUS TO OR-STAT"')
        self.assertRegex(out, rf'WRITE OUT-REC -> TEST\.STAT\.OUT \(FLOWJOB STEP1 DD STATOUT, output.*'
                              rf'FLOWSRC:{L("WRITE OUT-REC")} "WRITE OUT-REC"; FLOWJOB:{L("//STATOUT", "FLOWJOB.jcl")} "//STATOUT DD DSN=TEST\.STAT\.OUT"')
        self.assertRegex(out, rf'STEP2 SORT .*bytes unchanged -> TEST\.STAT\.SORTED\s+FLOWJOB:{L("//SORTIN", "FLOWJOB.jcl")} "//SORTIN DD DSN=TEST\.STAT\.OUT"')
        rd = _lines_of(out, "read by FLOWRDR.IR-STAT")
        self.assertIn("read by FLOWRDR.IR-STAT bytes 13-14", rd)
        self.assertIn("STEP3 DD STATIN", rd)
        self.assertIn("FLOWRDRC", rd)
        self.assertIn("copybook differs from the writer's: verify layout", rd)
        self.assertRegex(rd, rf'FLOWRDRC:{L("IR-STAT", "FLOWRDRC.cpy")} "\d\d  IR-STAT"')
        self.assertRegex(rd, rf'FLOWJOB:{L("//STATIN", "FLOWJOB.jcl")} "//STATIN DD"')
        self.assertIn("also read as: IR-KEY-AREA (parent group)", out)
        self.assertRegex(out, rf"88 IR-LAPSED 'LP' tested\s+FLOWRDR:{L('IF IR-LAPSED', 'FLOWRDR.cbl')} .IF IR-LAPSED.\s+\[end: only tested/displayed here\]")
        self.assertRegex(out, rf'FLOWJOB2 STEP1 SORT has OUTREC\s+FLOWJOB2:{L("OUTREC", "FLOWJOB2.jcl")} "OUTREC FIELDS=\(13,2,1,12\)"'
                              r"\s+\[end: sort step re-arranges bytes - mapping not indexed\]")
        # 2. CALL USING by position, then the callee's own MOVE and test
        call_line = L("CALL 'FLOWSUB' USING WS-STATUS")
        self.assertRegex(out, rf"CALL FLOWSUB arg 1 -> FLOWSUB\.LK-STATUS \(LINKAGE, X\(02\)\)\s+FLOWSRC:{call_line} .CALL 'FLOWSUB' USING WS-STATUS.")
        self.assertRegex(out, rf'MOVE -> FLOWSUB\.WS-HOLD\s+FLOWSUB:{L("MOVE LK-STATUS TO WS-HOLD", "FLOWSUB.cbl")} "MOVE LK-STATUS TO WS-HOLD"')
        self.assertRegex(out, r"IF WS-HOLD = 'LP'; DISPLAY\s+FLOWSUB:\d+; FLOWSUB:\d+\s+\[end: only tested/displayed here\]")
        # the ENTRY alias is called with the parameters the other way round
        self.assertRegex(out, r"CALL FLOWENT arg 2 -> FLOWSUB\.LK-STATUS")
        self.assertNotRegex(out, r"CALL FLOWENT arg 2 -> FLOWSUB\.LK-RC")
        # a dynamic CALL: one labelled branch per candidate, never the first one alone
        cands = [ln for ln in out.splitlines() if "candidate: resolved via MOVE literal" in ln]
        self.assertGreaterEqual(len(cands), 2, out)
        self.assertTrue(any("LK-STATUS" in ln for ln in cands), cands)      # FLOWSUB pos 1
        self.assertTrue(any("LK-RC" in ln for ln in cands), cands)          # FLOWENT pos 1
        # 3. DB2: the column, then its static readers as ONE table under the node
        self.assertRegex(out, rf'EXEC SQL UPDATE POLICY_TBL SET STATUS_CD\s+FLOWSRC:{L("EXEC SQL")}-{L("END-EXEC")} "STATUS_CD"')
        self.assertRegex(out, r"via table POLICY_TBL\.STATUS_CD: \d+ static readers, no ordering")
        self.assertIn("| SAMPPGM | WS-POLICY-STATUS |", out)
        self.assertIn("| SQLCOLS | WS-STATUS |", out)
        # 4. the local MOVE under ELSE, last
        self.assertRegex(out, rf"MOVE -> WS-RC\s+guard: NOT \(WS-RC = 'OK'\)\s+FLOWSRC:{lp_line} .ELSE MOVE WS-STATUS TO WS-RC.")
        # cross-program edges first, the local copy last
        self.assertLess(out.index("MOVE -> OR-STAT"), out.index("CALL FLOWSUB arg 1"))
        self.assertLess(out.index("CALL FLOWSUB arg 1"), out.index("EXEC SQL UPDATE POLICY_TBL"))
        self.assertLess(out.index("EXEC SQL UPDATE POLICY_TBL"), out.index("MOVE -> WS-RC"))
        # not followed, each with its reason
        self.assertIn("## Not followed", out)
        self.assertRegex(out, rf"MOVE -> WS-STAT-ENT \(subscripted: any of 3 elements\)\s+FLOWSRC:{L('TO WS-STAT-ENT (WS-I)')}"
                              r".*\[end: no further use in FLOWSRC\]")                  # nothing reads the table
        self.assertRegex(out, r"derived edges \(COMPUTE/STRING/FUNCTION\): 1 \(--derived follows them\)")
        self.assertIn("derived - value not preserved (--derived)", out.split("## Ends", 1)[1])   # the STRING
        self.assertNotIn("WS-MSG", out)
        # a subscript is not a source anywhere in the report
        for ln in out.splitlines():
            if re.search(r"\bWS-I\b", ln):
                self.assertIn("WS-STAT-ENT", ln, ln)
        self.assertIn("## Ends", out)
        self.assertIn("## Unresolved in scope", out)
        self.assertEqual(out.rstrip().splitlines()[-1], FOOTER)

        # --derived follows the STRING
        der = self.flow("WS-STATUS", "--program", "FLOWSRC", "--derived")
        self.assertIn("WS-MSG", der)
        self.assertIn("derived (STRING)", der)

        # the source's other copy: a group MOVE lands in bytes nobody named, and is cut to the target's length
        src = self.flow("FR-STATUS", "--program", "FLOWSRC")
        self.assertIn("group MOVE POL-REC -> WS-SAVE", src)
        self.assertIn("unnamed bytes of WS-SAVE (X(30))", src)
        self.assertIn("truncated 40 -> 30", src)
        self.assertIn("-> WS-STATUS", src)
        self.assertIn("group MOVE into unnamed bytes", src.split("## Ends", 1)[1])

        # past an OCCURS DEPENDING ON the offset holds only for the maximum count: the node says so
        odo = self.flow("FR-FILLER", "--program", "FLOWSRC")
        self.assertIn("offset is a maximum (ODO)", odo)

        # the overlay closure is printed with its reasons before any edge
        ov = self.flow("FR-AGENT-ID", "--program", "FLOWSRC")
        self.assertIn("also read as:", ov)
        self.assertIn("FR-AGENT-DATA (parent group)", ov)
        self.assertRegex(ov, r"FR-LEGACY-AREA .*\(REDEFINES\)")
        self.assertRegex(ov, r"FR-OLD-AGENT-KEY .*\(REDEFINES\)")

        # MOVE CORR pairs by NAME: WS-CODE -> WS-CODE, never WS-DESC -> WS-OTHER
        corr = self.flow("WS-CORR-A", "--program", "FLOWSRC")
        self.assertIn("CORR", corr)
        self.assertRegex(corr, r"-> .*WS-CODE")
        self.assertNotRegex(corr, r"-> .*WS-DESC")
        self.assertNotIn("WS-OTHER", corr)
        self.assertIn("also set here: INITIALIZE", corr)

        # a ref-mod with two literals narrows to those bytes; the target is the qualified one
        rm = self.flow("WS-SAVE", "--program", "FLOWSRC")
        self.assertRegex(rm, r"-> .*WS-CODE")
        self.assertIn("WS-CORR-B", rm)
        self.assertNotIn("whole item", rm)
        self.assertIn("no further use in FLOWSRC", _lines_of(rm, "WS-CODE"))     # nothing reads WS-CORR-B

        # BY CONTENT is one-way, LENGTH OF is a number not the bytes, RETURNING comes back
        bc = self.flow("WS-NOTE", "--program", "FLOWSRC")
        self.assertIn("BY CONTENT: one-way", bc)
        self.assertIn("LK-STATUS", bc)
        self.assertIn("callee receives LENGTH OF, not the bytes", bc)
        self.assertIn("RETURNING -> WS-R", bc)
        ret = self.flow("WS-R", "--program", "FLOWSRC", "--up")
        self.assertIn("RETURNING", ret)
        self.assertIn("FLOWSUB", ret)

        # a third argument where the callee declares two: position 3 has no param,
        # so the hop ends and says a human must look - never "arg 3 -> LK-x" (guard 12)
        ext = self.flow("WS-EXTRA", "--program", "FLOWSRC")
        x_line = L("CALL 'FLOWSUB' USING WS-RC WS-Y WS-EXTRA")
        hop = _lines_of(ext, "CALL FLOWSUB arg 3")
        self.assertRegex(hop, rf"""CALL FLOWSUB arg 3\b.*FLOWSRC:{x_line} "CALL 'FLOWSUB' USING WS-RC[^"]*".*"""
                              r"\[end: LINKAGE position out of range / count mismatch - HUMAN MUST VERIFY\]")
        self.assertNotRegex(ext, r"-> FLOWSUB\.LK-")
        self.assertIn("LINKAGE position out of range / count mismatch - HUMAN MUST VERIFY", ext.split("## Ends", 1)[1])

        # a numeric MOVE converts: S9(04) COMP (2 bytes) into 9(04) (4 bytes) is not a byte copy (guard 16)
        num = self.flow("WS-I", "--program", "FLOWSRC")
        n_line = L("MOVE WS-I TO WS-Y")
        hop = _lines_of(num, "MOVE -> WS-Y")
        self.assertRegex(hop, rf'MOVE -> WS-Y\b.*converted S9\(04\)(?: COMP)? -> 9\(04\).*FLOWSRC:{n_line} "MOVE WS-I TO WS-Y"')
        self.assertNotIn("truncated", hop)
        self.assertNotIn("bytes", hop)

        # CICS: LINK COMMAREA -> the callee's DFHCOMMAREA by convention, the copybook's field by bytes,
        # WRITEQ -> READQ on the same queue, SEND MAP ends at the screen
        cx = self.flow("WC-STATUS", "--program", "FLOWCICS", "--hops", "6")
        for s in ("by CICS convention", "FLOWCOMM", "DFHCOMMAREA", "CA-STATUS", "FLOWCOMC", "WS-Q-REC", "WRITEQ",
                  "READQ", "FLOWQ", "WS-Q-IN", "WM-STAT", "screen field - a human sees it"):
            self.assertIn(s, cx, s)
        self.assertLess(cx.index("CA-STATUS"), cx.index("WS-Q-REC"))
        self.assertLess(cx.index("WS-Q-REC"), cx.index("WS-Q-IN"))
        self.assertLess(cx.index("WS-Q-IN"), cx.index("screen field - a human sees it"))

        # without --program: one tree per program that declares the name, never one merged field
        every = self.flow("WS-STATUS")
        for p in ("FLOWSRC", "CONDLOGX", "MULTILN", "SQLCOLS"):
            self.assertIn(f"# Flow of {p}.WS-STATUS", every, p)

    def test_flow_up_and_caps(self):
        up = self.flow("IR-STAT", "--program", "FLOWRDR", "--up", "--hops", "5")
        self.assertIn("(upstream", up)
        for s in ("FLOWSRC.OR-STAT", "WS-STATUS", "MOVE 'LP'", "FR-STATUS", "TEST.POLICY.MASTER", "no indexed writer"):
            self.assertIn(s, up, s)
        self.assertLess(up.index("FLOWSRC.OR-STAT"), up.index("FR-STATUS"))
        rc = self.flow("WS-RC", "--program", "FLOWSRC", "--up")
        self.assertIn("FLOWSUB.LK-RC", rc)
        self.assertIn("'OK'", rc)
        self.assertRegex(rc, r"(arg|pos|position) 2")
        one = self.flow("WS-STATUS", "--program", "FLOWSRC", "--hops", "1")
        self.assertIn("hop limit 1", one)
        # no hop into WS-HOLD (a guard naming it on an `also set here` line is not a hop)
        self.assertNotRegex(one, r"-> (?:FLOWSUB\.)?WS-HOLD")
        narrow = self.flow("WS-STATUS", "--program", "FLOWSRC", "--width", "1")
        drop = [ln for ln in narrow.splitlines() if "(--all)" in ln]
        # the cap is per node (plan section 4): the root's collapse line names every dropped program with
        # its count; a child that has two children of its own (OR-STAT: FLOWJOB and FLOWJOB2) prints one too
        self.assertGreaterEqual(len(drop), 1, narrow)
        self.assertTrue(any(re.search(r"\d+ more .*FLOWSRC \d+", ln) for ln in drop), drop)
        self.assertIn("width cap", narrow.split("## Ends", 1)[1])
        self.assertNotIn("(--all)", self.flow("WS-STATUS", "--program", "FLOWSRC", "--width", "1", "--all"))
        self.assertIn("node cap", self.flow("WS-STATUS", "--program", "FLOWSRC", "--nodes", "1"))
        ping = self.flow("WS-PING", "--program", "FLOWSRC")
        self.assertIn("WS-PONG", ping)
        self.assertEqual(ping.count("(shown as "), 1, ping)

    def test_call_hops_share_storage_in_the_fixtures(self):
        # FLOWSUB.LK-RC reached through `CALL WS-PGM` (FLOWENT, arg 1) IS WS-STATUS's storage: it never goes
        # back to another CALL's argument (WS-RC, WS-Y); and a CALL / LINKAGE hop never converts a PIC
        down = self.flow("WS-STATUS", "--program", "FLOWSRC")
        self.assertNotRegex(down, r"back to FLOWSRC\.WS-(?:RC|Y)\b")
        up = self.flow("WS-RC", "--program", "FLOWSRC", "--up")
        self.assertNotIn("WS-Y", up)
        for text in (down, up):
            for ln in text.splitlines():
                if re.search(r"\b(?:CALL|LINKAGE|COMMAREA)\b", ln):
                    self.assertNotIn("converted", ln)

    def test_flow_partial_program_is_labelled(self):
        # ERRPGM's COPY POLDCL is not in the fixtures (parse_status partial, an
        # `expand` row): a report on one of its fields says so in the node header
        # and lists the missing copybook in scope, never a complete-looking chain (guard 23)
        out = self.flow("WS-ERR-CD", "--program", "ERRPGM")
        self.assertIn("# Flow of ERRPGM.WS-ERR-CD", out)
        self.assertRegex(out, r"\[program partial: [^\]]*POLDCL[^\]]*\]")
        first_hop = re.search(r"^1\s", out, re.M)
        self.assertIsNotNone(first_hop, out)
        self.assertLess(out.index("[program partial:"), first_hop.start())
        self.assertIn("Unresolved in scope", out)
        unres = out.split("Unresolved in scope", 1)[1].split(FOOTER, 1)[0]
        self.assertRegex(unres, r"ERRPGM.*COPY POLDCL NOT FOUND")
        # what the index does hold still ends with its fixed reason
        self.assertRegex(out, r"CALL ERRLOG arg 1\b.*\[end: callee not in index\]")
        self.assertRegex(out, r"AUDIT_TBL.*COL1.*\[end: DB2 column: no static reader\]")
        self.assertEqual(out.rstrip().splitlines()[-1], FOOTER)

    def test_every_flow_cite_passes_the_gate(self):
        texts = [self.flow("WS-STATUS", "--program", "FLOWSRC"),
                 self.flow("WS-RC", "--program", "FLOWSRC"),
                 self.flow("IR-STAT", "--program", "FLOWRDR", "--up", "--hops", "5"),
                 self.flow("WC-STATUS", "--program", "FLOWCICS", "--hops", "6"),
                 self.flow("WS-EXTRA", "--program", "FLOWSRC"),
                 self.flow("WS-I", "--program", "FLOWSRC")]
        cites = [m for t in texts for m in CITE.finditer(t)]
        self.assertGreaterEqual(len(cites), 12)
        # FLOWJOB STEP4 sends TEST.STAT.SORTED by FTP: the pseudo-DD `*FTP*` sits on the EXEC line, so the
        # cite quotes the EXEC text there (never "//*FTP* DD", which is on no line), and the step is the interface
        ftp = _line("FLOWJOB.jcl", "EXEC PGM=FTP")
        self.assertRegex(texts[0], rf'FLOWJOB STEP4 FTP\s+FLOWJOB:{ftp} "//STEP4\s+EXEC PGM=FTP"\s+'
                                   r"\[end: dataset leaves the mainframe \(interfaces: ftp\)\]")
        self.assertRegex(texts[2], rf'TEST\.STAT\.SORTED ftp\s+FLOWJOB:{ftp} "//STEP4\s+EXEC PGM=FTP"')
        self.assertNotIn("*FTP* DD", "\n".join(texts))
        self.assertEqual(texts[0].count("interfaces: ftp)]"), 1, texts[0])      # the step, not the step and its edge
        call_line = _line("FLOWSRC.cbl", "CALL 'FLOWSUB' USING WS-STATUS")
        # the operand WS-RC sits on the CALL's continuation line: cited as a range
        self.assertTrue(any(m.group(1) == "FLOWSRC" and m.group(2) == str(call_line) and m.group(3) == str(call_line + 1)
                            for m in cites), [m.group(0) for m in cites if m.group(1) == "FLOWSRC"])
        for m in cites:
            token = m.group(4)
            self.assertGreaterEqual(len(token), verify_citations.WEAK_TOKEN_CHARS, m.group(0))
            self.assertLessEqual(len(token), 40, m.group(0))
            if m.group(3):
                self.assertLessEqual(int(m.group(3)) - int(m.group(2)), verify_citations.WIDE_RANGE_LINES, m.group(0))
        answer = "\n".join(f'[[{m.group(1)} {m.group(2)}{"-" + m.group(3) if m.group(3) else ""} "{m.group(4)}"]]'
                           for m in cites)
        res, _u = verify_citations.check_answer(answer, db_path=self.db)
        self.assertEqual(len(res), len(cites))
        bad = [(r.raw, r.status, r.detail) for r in res if r.status != "PASS"]
        self.assertEqual(bad, [])

    def test_flow_marks_reconstructed_before_reparse(self):
        old = os.path.join(self.td, "old.db")
        shutil.copy(self.db, old)
        c = sqlite3.connect(old)
        try:
            c.execute("DROP TABLE data_flow")
            c.execute("DROP TABLE pfield")
            c.commit()
        finally:
            c.close()
        out = self.flow("WS-STATUS", "--program", "FLOWSRC", db=old)
        self.assertIn("index has no data_flow - reconstructed from field_ref; exact after the next re-parse", out)
        unpaired = re.search(r"(\d+) MOVE statements? could not be paired \(index predates data_flow\)", out)
        self.assertIsNotNone(unpaired, out)
        moves = [ln for ln in out.splitlines() if "MOVE ->" in ln]
        self.assertTrue(moves, out)
        for ln in moves:
            self.assertIn("(reconstructed)", ln, ln)
        self.assertIn("OR-STAT", out)                       # a single-pair line is still paired
        # guard 2: field_ref alone cannot say which read pairs with which write on a line that holds
        # more than one of either, so such lines are COUNTED, never paired reads x writes. FLOWSRC has
        # two: the one-line IF/ELSE (one MOVE read, two MOVE writes: WS-STATUS and WS-RC) and the
        # subscripted MOVE (two reads: WS-STATUS and WS-I, one write: WS-STAT-ENT). WS-RC and
        # WS-STAT-ENT are reachable from WS-STATUS only through those lines, so a cross-pairing
        # reconstruction is the one that prints them.
        self.assertGreaterEqual(int(unpaired.group(1)), 2, out)
        self.assertNotIn("MOVE -> WS-RC", out)
        self.assertNotIn("WS-STAT-ENT", out)
        self.assertIn("TEST.STAT.OUT", out)                 # file bytes of a program-owned record
        self.assertIn("copybook fields: not followed until re-parse", out)
        self.assertIn("FLOWSUB.LK-STATUS", out)             # CALL from call_edge / linkage_using
        self.assertIn("POLICY_TBL.STATUS_CD", out)          # DB2 from sql_col_ref
        self.assertEqual(out.rstrip().splitlines()[-1], FOOTER)

    def test_fallback_reader_from_a_copybook_is_not_a_layout_claim(self):
        # the old-index shape (none of the five tables): FLOWRDR's IN-REC takes its items from COPY
        # FLOWRDRC, which `field` does not hold for the program - so the fallback cannot follow them.
        # It must say THAT, never "reader layout has no field at bytes 13-14" (IR-STAT is at 13-14)
        old = os.path.join(self.td, "oldest.db")
        shutil.copy(self.db, old)
        c = sqlite3.connect(old)
        try:
            c.executescript("DROP TABLE data_flow; DROP TABLE pfield; DROP TABLE call_arg; DROP TABLE param; "
                            "DROP TABLE file_record;")
        finally:
            c.close()
        out = self.flow("WS-STATUS", "--program", "FLOWSRC", db=old)
        hop = _lines_of(out, "FLOWRDR.IN-REC")
        self.assertTrue(hop, out)
        self.assertIn("[end: copybook fields: not followed until re-parse]", hop)
        self.assertNotIn("reader layout has no field", out)
        self.assertIn("copybook fields: not followed until re-parse:", out.split("## Ends", 1)[1])
        # guard 4 in the same shape: WS-CODE OF WS-CORR-B is line 44, never WS-CORR-A's line 41;
        # an unqualified WS-X / WS-CODE lists both declarations and follows neither
        q = self.flow("WS-CODE OF WS-CORR-B", "--program", "FLOWSRC", db=old)
        self.assertIn(f'- defined FLOWSRC:{_line("FLOWSRC.cbl", "01  WS-CORR-B") + 1} "05  WS-CODE"', q)
        self.assertNotIn(f'FLOWSRC:{_line("FLOWSRC.cbl", "01  WS-CORR-A") + 1} "05  WS-CODE"', q)
        for args in (("WS-X", "--program", "FLOWSRC"), ("WS-CODE",)):
            with self.subTest(args):
                amb = self.flow(*args, db=old)
                self.assertRegex(amb, r"is declared 2 times in FLOWSRC: .* - HUMAN MUST VERIFY which one")
                self.assertNotIn("- defined", amb)
                self.assertNotIn("MOVE SPACES", amb)

    def test_fallback_never_ends_where_a_parent_group_carries_the_item(self):
        # his index before the re-parse: WC-STATUS sits under WS-COMM, which is LINKed as the COMMAREA
        old = os.path.join(self.td, "oldparent.db")
        shutil.copy(self.db, old)
        c = sqlite3.connect(old)
        try:
            c.executescript("DROP TABLE data_flow; DROP TABLE pfield; DROP TABLE call_arg; DROP TABLE param; "
                            "DROP TABLE file_record;")
        finally:
            c.close()
        cx = self.flow("WC-STATUS", "--program", "FLOWCICS", db=old)
        self.assertRegex(cx, rf"parent group WS-COMM used by EXEC CICS LINK: its bytes include WC-STATUS \(reconstructed\)\s+"
                             rf"FLOWCICS:{_line('FLOWCICS.cbl', 'LINK PROGRAM')}-\d+ \"[^\"]*COMMAREA\(WS-COMM\"\s+"
                             r"\[end: parent group not followed until re-parse\]")
        self.assertNotIn("no further use", cx)
        # a copied item: the fallback cannot see its record, so it never claims the walk is complete
        rd = self.flow("IR-STAT", "--program", "FLOWRDR", "--up", db=old)
        self.assertNotIn("no further use", rd)
        self.assertRegex(rd, rf"IF IR-LAPSED\s+FLOWRDR:{_line('FLOWRDR.cbl', 'IF IR-LAPSED')}\s+"
                             r"\[end: copybook fields: not followed until re-parse\]")
        conn = query.connect(old)
        try:
            lit = query.cmd_literal(conn, "LP")
        finally:
            conn.close()
        sec = lit.split("**FLOWCICS.WC-STATUS**", 1)[1].split("**", 1)[0]
        self.assertIn("parent group WS-COMM", sec)

    def test_diff_never_starts_at_a_subscript(self):
        from atlas import flow
        for text, want in (("       MOVE WS-A TO WS-B (WS-I).", ["WS-B"]),
                           ("       MOVE WS-A TO WS-B(WS-I:2).", ["WS-B"]),
                           ("       MOVE WS-A TO WS-TBL(WS-IDX WS-J) WS-C.", ["WS-TBL", "WS-C"]),
                           ("       CALL 'SUBX' USING WS-T (WS-I) WS-U.", ["WS-T", "WS-U"])):
            with self.subTest(text):
                self.assertEqual(flow.changed_targets([text], False), want)
        # end to end: the changed MOVE's target is the table, never its index WS-I (guard 3)
        new = os.path.join(self.td, "sub", "FLOWSRC.cbl")
        os.makedirs(os.path.dirname(new), exist_ok=True)
        with open(os.path.join(FIX, "FLOWSRC.cbl"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(src.count("MOVE WS-STATUS TO WS-STAT-ENT (WS-I)"), 1)
        with open(new, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src.replace("MOVE WS-STATUS TO WS-STAT-ENT (WS-I)", "MOVE WS-RC     TO WS-STAT-ENT (WS-I)"))
        d = query.cmd_diff(self.conn, "FLOWSRC", new)
        self.assertIn("## Flow from the changed statements", d)
        self.assertIn("**FLOWSRC.WS-STAT-ENT**", d)
        self.assertNotIn("**FLOWSRC.WS-I**", d)

    def test_returning_item_goes_back_to_the_caller_in_the_fixtures(self):
        # FLOWSUB's RETURNING item comes back into FLOWSRC.WS-R at GOBACK (`CALL .. RETURNING WS-R`)
        out = self.flow("LK-RET", "--program", "FLOWSUB")
        self.assertRegex(out, r"(?m)^1\s+RETURNING -> back to FLOWSRC\.WS-R\b")
        self.assertNotIn("no further use in FLOWSUB", out)
        cites = list(CITE.finditer(out))
        self.assertTrue(cites, out)
        answer = "\n".join(f'[[{m.group(1)} {m.group(2)}{"-" + m.group(3) if m.group(3) else ""} "{m.group(4)}"]]'
                           for m in cites)
        res, _u = verify_citations.check_answer(answer, db_path=self.db)
        self.assertEqual([(r.raw, r.status) for r in res if r.status != "PASS"], [])

    def test_literal_pack_diff_use_flow(self):
        lit = query.cmd_literal(self.conn, "LP")
        marker = "### Where the value goes after it is set (cross-program)"
        self.assertIn(marker, lit)
        after = lit.split(marker, 1)[1]                     # FLOWRDR's 88 is in the Defined table above: not that
        self.assertIn("FLOWRDR", after)                     # through MOVE, WRITE and the job - not name-traceable before
        self.assertIn("TEST.STAT.OUT", after)
        self.assertNotIn("no CALL/LINKAGE/file route found", lit)
        pack = query.cmd_pack(self.conn, "WS-STATUS", kind="field", budget=3000)
        self.assertIn("Flow of", pack)
        self.assertLessEqual(len(pack), 3000 + len(pack.split("\n", 1)[0]) + 1)
        # one changed MOVE: the diff says where its new target goes
        new = os.path.join(self.td, "FLOWSRC.cbl")
        with open(os.path.join(FIX, "FLOWSRC.cbl"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("MOVE WS-STATUS TO OR-STAT", src)
        with open(new, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src.replace("MOVE WS-STATUS TO OR-STAT", "MOVE WS-RC     TO OR-STAT"))
        d = query.cmd_diff(self.conn, "FLOWSRC", new)
        self.assertIn("OR-STAT", d)
        self.assertIn("FLOWRDR", d)
        self.assertNotIn("no fact the index tracks changed", d)


class FlowKnownLimits(unittest.TestCase):
    """The review's open findings on the walk (ROADMAP "flow: known limits",
    inline and fictional): `diff` reads a keyword as a whole COBOL word and
    takes the GIVING target; every branch --up cannot follow into a callee
    prints a labelled end, in both walkers."""

    H = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. {p}.\n       DATA DIVISION.\n"
         "       WORKING-STORAGE SECTION.\n")
    FILES = {
        # a sender whose name ends in -TO, and an ADD .. TO .. GIVING that changes only its GIVING target
        "KDIFF.cbl": H.format(p="KDIFF") + (
            "       01  WS-BILL-TO               PIC X(04).\n       01  OUT-BILL-TO              PIC X(04).\n"
            "       01  WS-A                     PIC 9(04).\n       01  WS-B                     PIC 9(04).\n"
            "       01  WS-C                     PIC 9(04).\n       PROCEDURE DIVISION.\n"
            "           MOVE WS-BILL-TO TO OUT-BILL-TO.\n           ADD WS-A TO WS-B GIVING WS-C.\n"
            "           DISPLAY OUT-BILL-TO WS-B WS-C.\n           GOBACK.\n"),
        # WU-CODE passed BY REFERENCE to callees --up cannot follow: not in the index, a CALL through a
        # LINKAGE item (unresolved), 41 candidates (the parser keeps 40 and a `(+1 more)`), a parameter
        # from a missing copybook, a COMMAREA to a program with no DFHCOMMAREA
        "KUP.cbl": H.format(p="KUP") + (
            "       01  WS-MANY                  PIC X(08).\n       01  WU-CODE                  PIC X(02).\n"
            "       01  WU-RET                   PIC X(02).\n"
            "       LINKAGE SECTION.\n       01  LK-PGM                   PIC X(08).\n"
            "       PROCEDURE DIVISION USING LK-PGM.\n"
            + "".join(f"           MOVE 'KP{i:02d}' TO WS-MANY.\n" for i in range(1, 42))
            + "           CALL 'KNOWHERE' USING WU-CODE.\n           CALL LK-PGM USING WU-CODE.\n"
            "           CALL WS-MANY USING WU-CODE.\n           CALL 'KMISS' USING WU-CODE.\n"
            "           EXEC CICS LINK PROGRAM('KNOCA') COMMAREA(WU-CODE) END-EXEC.\n"
            "           CALL WS-MANY RETURNING WU-RET.\n"
            "           DISPLAY WU-CODE WU-RET.\n           GOBACK.\n"),
        "KMISS.cbl": ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. KMISS.\n       DATA DIVISION.\n"
                      "       LINKAGE SECTION.\n           COPY KNOCPY.\n       PROCEDURE DIVISION USING LK-MISS.\n"
                      "           MOVE 'XX' TO LK-MISS.\n           GOBACK.\n"),
        "KNOCA.cbl": H.format(p="KNOCA") + (
            "       01  WN-X                     PIC X(02).\n       PROCEDURE DIVISION.\n"
            "           MOVE 'NO' TO WN-X.\n           EXEC CICS RETURN END-EXEC.\n"),
        # a group passed to callees that write (KCE) or read (KCN) only a field under their parameter, and
        # LINKed to a CICS program that has USING DFHCOMMAREA (KCU): before the re-parse, labelled ends
        "KCA.cbl": H.format(p="KCA") + (
            "       01  WK-AREA.\n           05  WK-A                 PIC X(02).\n"
            "           05  WK-B                 PIC X(02).\n       PROCEDURE DIVISION.\n"
            "           CALL 'KCE' USING WK-AREA.\n           CALL 'KCN' USING WK-AREA.\n"
            "           EXEC CICS LINK PROGRAM('KCU') COMMAREA(WK-AREA) END-EXEC.\n"
            "           DISPLAY WK-AREA.\n           GOBACK.\n"),
        "KCE.cbl": ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. KCE.\n       DATA DIVISION.\n"
                    "       LINKAGE SECTION.\n       01  LK-AREA.\n           05  LK-A                 PIC X(02).\n"
                    "           05  LK-B                 PIC X(02).\n       PROCEDURE DIVISION USING LK-AREA.\n"
                    "           MOVE 'XX' TO LK-B.\n           GOBACK.\n"),
        "KCN.cbl": ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. KCN.\n       DATA DIVISION.\n"
                    "       LINKAGE SECTION.\n       01  LN-AREA.\n           05  LN-A                 PIC X(02).\n"
                    "           05  LN-B                 PIC X(02).\n       PROCEDURE DIVISION USING LN-AREA.\n"
                    "           DISPLAY LN-A.\n           GOBACK.\n"),
        "KCU.cbl": ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. KCU.\n       DATA DIVISION.\n"
                    "       LINKAGE SECTION.\n       01  DFHCOMMAREA.\n           05  CU-A                 PIC X(02).\n"
                    "           05  CU-B                 PIC X(02).\n       PROCEDURE DIVISION USING DFHCOMMAREA.\n"
                    "           MOVE 'YY' TO CU-B.\n           EXEC CICS RETURN END-EXEC.\n"),
        # KGW writes a file that ICEGENER copies (SYSUT1 -> SYSUT2, as IEBGENER) to the file KGR reads
        "KGW.cbl": ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. KGW.\n       ENVIRONMENT DIVISION.\n"
                    "       INPUT-OUTPUT SECTION.\n       FILE-CONTROL.\n           SELECT OUT-F ASSIGN TO GOUT.\n"
                    "       DATA DIVISION.\n       FILE SECTION.\n       FD  OUT-F.\n       01  OUT-REC.\n"
                    "           05  OR-CODE              PIC X(04).\n       WORKING-STORAGE SECTION.\n"
                    "       01  WS-GCODE                 PIC X(04).\n       PROCEDURE DIVISION.\n"
                    "           OPEN OUTPUT OUT-F.\n           MOVE WS-GCODE TO OR-CODE.\n           WRITE OUT-REC.\n"
                    "           CLOSE OUT-F.\n           GOBACK.\n"),
        "KGR.cbl": ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. KGR.\n       ENVIRONMENT DIVISION.\n"
                    "       INPUT-OUTPUT SECTION.\n       FILE-CONTROL.\n           SELECT IN-F ASSIGN TO GIN.\n"
                    "       DATA DIVISION.\n       FILE SECTION.\n       FD  IN-F.\n       01  IN-REC.\n"
                    "           05  IR-GCODE             PIC X(04).\n       PROCEDURE DIVISION.\n"
                    "           OPEN INPUT IN-F.\n           READ IN-F.\n           DISPLAY IR-GCODE.\n"
                    "           CLOSE IN-F.\n           GOBACK.\n"),
        "KGJOB.jcl": ("//KGJOB    JOB (ACCT),'COPY'\n//W1       EXEC PGM=KGW\n"
                      "//GOUT     DD DSN=TEST.KG.RAW,DISP=(NEW,CATLG,DELETE)\n"
                      "//C1       EXEC PGM=ICEGENER\n//SYSPRINT DD SYSOUT=*\n//SYSIN    DD DUMMY\n"
                      "//SYSUT1   DD DSN=TEST.KG.RAW,DISP=SHR\n"
                      "//SYSUT2   DD DSN=TEST.KG.COPY,DISP=(NEW,CATLG,DELETE)\n"
                      "//R1       EXEC PGM=KGR\n//GIN      DD DSN=TEST.KG.COPY,DISP=SHR\n"),
        # IEBGENER with RECORD FIELD= re-arranges the bytes: not a plain copy
        "KGJOB2.jcl": ("//KGJOB2   JOB (ACCT),'GEN'\n//C2       EXEC PGM=IEBGENER\n//SYSPRINT DD SYSOUT=*\n"
                       "//SYSIN    DD *\n  GENERATE MAXFLDS=1\n  RECORD FIELD=(2,3,,1)\n/*\n"
                       "//SYSUT1   DD DSN=TEST.KG.RAW,DISP=SHR\n"
                       "//SYSUT2   DD DSN=TEST.KG.GEN,DISP=(NEW,CATLG,DELETE)\n"
                       "//R2       EXEC PGM=KGR\n//GIN      DD DSN=TEST.KG.GEN,DISP=SHR\n"),
        # ICEGENER / IEBGENER with SYSIN from a card member the estate does not hold: the cards are unknown
        "KGJOB3.jcl": ("//KGJOB3   JOB (ACCT),'GEN'\n//C3       EXEC PGM=ICEGENER\n//SYSPRINT DD SYSOUT=*\n"
                       "//SYSIN    DD DSN=TEST.CTL.LIB(GENCTL),DISP=SHR\n"
                       "//SYSUT1   DD DSN=TEST.KG.RAW,DISP=SHR\n"
                       "//SYSUT2   DD DSN=TEST.KG.C3,DISP=(NEW,CATLG,DELETE)\n"
                       "//R3       EXEC PGM=KGR\n//GIN      DD DSN=TEST.KG.C3,DISP=SHR\n"
                       "//C4       EXEC PGM=IEBGENER\n//SYSPRINT DD SYSOUT=*\n"
                       "//SYSIN    DD DSN=TEST.CTL.SEQ,DISP=SHR\n"
                       "//SYSUT1   DD DSN=TEST.KG.RAW,DISP=SHR\n"
                       "//SYSUT2   DD DSN=TEST.KG.C4,DISP=(NEW,CATLG,DELETE)\n"
                       "//R4       EXEC PGM=KGR\n//GIN      DD DSN=TEST.KG.C4,DISP=SHR\n"),
        # IDCAMS REPRO: the parser puts the pseudo-DD *REPRO* on the EXEC line, as an FTP step's
        "KGJOB4.jcl": ("//KGJOB4   JOB (ACCT),'REP'\n//C5       EXEC PGM=IDCAMS\n//SYSPRINT DD SYSOUT=*\n"
                       "//SYSIN    DD *\n  REPRO INDATASET(TEST.KG.RAW) OUTDATASET(TEST.KG.REP)\n/*\n"
                       "//R5       EXEC PGM=KGR\n//GIN      DD DSN=TEST.KG.REP,DISP=SHR\n"),
        # PROC steps expanded into a job keep the PROC's line numbers: KPJOB:5 is a comment, RDPROC:5 the DD;
        # KFJOB:2 is F1 (a GET of another dataset), FTPPROC:2 the PUT of TEST.KP.RAW that S1 runs
        "KPW.cbl": ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. KPW.\n       ENVIRONMENT DIVISION.\n"
                    "       INPUT-OUTPUT SECTION.\n       FILE-CONTROL.\n           SELECT OUT-F ASSIGN TO POUT.\n"
                    "       DATA DIVISION.\n       FILE SECTION.\n       FD  OUT-F.\n       01  OUT-REC.\n"
                    "           05  OP-CODE              PIC X(04).\n       WORKING-STORAGE SECTION.\n"
                    "       01  WS-PCODE                 PIC X(04).\n       PROCEDURE DIVISION.\n"
                    "           OPEN OUTPUT OUT-F.\n           MOVE WS-PCODE TO OP-CODE.\n           WRITE OUT-REC.\n"
                    "           CLOSE OUT-F.\n           GOBACK.\n"),
        "KPR.cbl": ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. KPR.\n       ENVIRONMENT DIVISION.\n"
                    "       INPUT-OUTPUT SECTION.\n       FILE-CONTROL.\n           SELECT IN-F ASSIGN TO PIN.\n"
                    "       DATA DIVISION.\n       FILE SECTION.\n       FD  IN-F.\n       01  IN-REC.\n"
                    "           05  IP-CODE              PIC X(04).\n       PROCEDURE DIVISION.\n"
                    "           OPEN INPUT IN-F.\n           READ IN-F.\n           DISPLAY IP-CODE.\n"
                    "           CLOSE IN-F.\n           GOBACK.\n"),
        "KPJOB.jcl": ("//KPJOB    JOB (ACCT),'P'\n//W1       EXEC PGM=KPW\n"
                      "//POUT     DD DSN=TEST.KP.RAW,DISP=(NEW,CATLG,DELETE)\n//*\n//*\n//*\n//S1       EXEC RDPROC\n"),
        "RDPROC.prc": "//RDPROC   PROC\n//*\n//*\n//P1       EXEC PGM=KPR\n//PIN      DD DSN=TEST.KP.RAW,DISP=SHR\n//         PEND\n",
        "KFJOB.jcl": ("//KFJOB    JOB (ACCT),'F'\n//F1       EXEC PGM=FTP,PARM='PEERHOST (EXIT'\n//INPUT    DD *\n"
                      "get stat.txt 'TEST.KP.IN'\nquit\n/*\n//S1       EXEC FTPPROC\n"),
        "FTPPROC.prc": ("//FTPPROC  PROC\n//P1       EXEC PGM=FTP,PARM='PEERHOST (EXIT'\n//INPUT    DD *\n"
                        "put 'TEST.KP.RAW' raw.txt\nquit\n/*\n//         PEND\n"),
        # concatenated DDs: R1 and KCPROC's P1 read TEST.KC.RAW as the second dataset of CIN, C2's SYSIN
        # is DUMMY plus a card dataset the estate does not hold. The parser stores each continuation under
        # the previous DD's name with its own line, which has no name field
        "KCW.cbl": ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. KCW.\n       ENVIRONMENT DIVISION.\n"
                    "       INPUT-OUTPUT SECTION.\n       FILE-CONTROL.\n           SELECT OUT-F ASSIGN TO COUT.\n"
                    "       DATA DIVISION.\n       FILE SECTION.\n       FD  OUT-F.\n       01  OUT-REC.\n"
                    "           05  OC-CODE              PIC X(04).\n       WORKING-STORAGE SECTION.\n"
                    "       01  WS-CCODE                 PIC X(04).\n       PROCEDURE DIVISION.\n"
                    "           OPEN OUTPUT OUT-F.\n           MOVE WS-CCODE TO OC-CODE.\n           WRITE OUT-REC.\n"
                    "           CLOSE OUT-F.\n           GOBACK.\n"),
        "KCR.cbl": ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. KCR.\n       ENVIRONMENT DIVISION.\n"
                    "       INPUT-OUTPUT SECTION.\n       FILE-CONTROL.\n           SELECT IN-F ASSIGN TO CIN.\n"
                    "       DATA DIVISION.\n       FILE SECTION.\n       FD  IN-F.\n       01  IN-REC.\n"
                    "           05  IC-CODE              PIC X(04).\n       PROCEDURE DIVISION.\n"
                    "           OPEN INPUT IN-F.\n           READ IN-F.\n           DISPLAY IC-CODE.\n"
                    "           CLOSE IN-F.\n           GOBACK.\n"),
        "KCJOB.jcl": ("//KCJOB    JOB (ACCT),'C'\n//W1       EXEC PGM=KCW\n"
                      "//COUT     DD DSN=TEST.KC.RAW,DISP=(NEW,CATLG,DELETE)\n"
                      "//R1       EXEC PGM=KCR\n//CIN      DD DSN=TEST.KC.OTHER,DISP=SHR\n"
                      "//         DD DSN=TEST.KC.RAW,DISP=SHR\n"
                      "//C2       EXEC PGM=IEBGENER\n//SYSPRINT DD SYSOUT=*\n//SYSIN    DD DUMMY\n"
                      "//         DD DSN=TEST.CTL.KCSEQ,DISP=SHR\n//SYSUT1   DD DSN=TEST.KC.RAW,DISP=SHR\n"
                      "//SYSUT2   DD DSN=TEST.KC.C2,DISP=(NEW,CATLG,DELETE)\n//S3       EXEC KCPROC\n"),
        "KCPROC.prc": ("//KCPROC   PROC\n//P1       EXEC PGM=KCR\n//CIN      DD DSN=TEST.KC.OTHER,DISP=SHR\n"
                       "//         DD DSN=TEST.KC.RAW,DISP=SHR\n//         PEND\n"),
    }

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        src = os.path.join(cls.td, "estate")
        os.makedirs(src)
        for name, text in cls.FILES.items():
            with open(os.path.join(src, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([src, "--db", cls.db, "--rebuild", "--quiet"])
        cls.old = os.path.join(cls.td, "old.db")
        shutil.copyfile(cls.db, cls.old)
        c = sqlite3.connect(cls.old)
        c.executescript("DROP TABLE data_flow; DROP TABLE pfield; DROP TABLE call_arg; DROP TABLE param; "
                        "DROP TABLE file_record;")
        c.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def flow(self, *args, db=None) -> str:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = query._main(["--db", db or self.db, "flow", *args])
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def test_diff_keywords_are_whole_words_and_giving_is_the_target(self):
        from atlas import flow
        for text, want in (("       MOVE WS-BILL-TO TO OUT-BILL-TO.", ["OUT-BILL-TO"]),
                           ("       MOVE WS-MOVE-A TO WS-B.", ["WS-B"]),
                           ("       ADD WS-A TO WS-B GIVING WS-C.", ["WS-C"]),
                           ("       ADD WS-A WS-B GIVING WS-C.", ["WS-C"]),
                           ("       ADD 1 TO WS-B ROUNDED WS-C ON SIZE ERROR MOVE WS-A TO WS-D END-ADD.",
                            ["WS-D", "WS-B", "WS-C"]),
                           ("       SUBTRACT WS-A FROM WS-B.", ["WS-B"]),
                           ("       MULTIPLY WS-A BY WS-B.", ["WS-B"]),
                           ("       DIVIDE WS-A INTO WS-B.", ["WS-B"]),
                           ("       DIVIDE WS-A BY WS-B GIVING WS-C REMAINDER WS-D.", ["WS-C", "WS-D"]),
                           ("       SUBTRACT WS-A FROM WS-B. DISPLAY WS-SENT-TO.", ["WS-B"])):
            with self.subTest(text):
                self.assertEqual(flow.changed_targets([text], False), want)
        # end to end: both changed statements start a flow at what they change, never at WS-B
        with open(os.path.join(self.td, "estate", "KDIFF.cbl"), encoding="utf-8") as fh:
            src = fh.read()
        new = os.path.join(self.td, "new", "KDIFF.cbl")
        os.makedirs(os.path.dirname(new), exist_ok=True)
        with open(new, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src.replace("MOVE WS-BILL-TO TO", "MOVE WS-BILL-TO  TO").replace("TO WS-B GIVING", "TO WS-B  GIVING"))
        conn = query.connect(self.db)
        try:
            d = query.cmd_diff(conn, "KDIFF", new)
        finally:
            conn.close()
        self.assertIn("## Flow from the changed statements", d)
        self.assertIn("**KDIFF.OUT-BILL-TO**", d)
        self.assertIn("**KDIFF.WS-C**", d)
        self.assertNotIn("**KDIFF.WS-B**", d)

    def test_up_ends_at_every_callee_it_cannot_follow(self):
        ends = {"KNOWHERE": (r"CALL KNOWHERE arg 1 <- KNOWHERE may set it", "callee not in index"),
                "LK-PGM": (r"CALL LK-PGM arg 1 <- \? may set it", "dynamic CALL unresolved (via LK-PGM)"),
                "WS-MANY": (r"CALL WS-MANY arg 1 <- \(\+1 more\) candidate\(s\) not listed", "dynamic CALL unresolved (via WS-MANY)"),
                "KP01": (r"CALL KP01 \(candidate: resolved via MOVE literal\) arg 1 <- KP01 may set it", "callee not in index")}
        for db, tag in ((self.db, "exact"), (self.old, "reconstructed")):
            with self.subTest(tag):
                up = self.flow("WU-CODE", "--program", "KUP", "--up", "--all", db=db)
                for what, (label, end) in ends.items():
                    hop = _lines_of(up, f"CALL {what} ")
                    self.assertRegex(hop, label, up)
                    self.assertIn(f"[end: {end}]", hop)
                    if tag == "reconstructed":
                        self.assertIn("(reconstructed)", hop)
                self.assertEqual(len(re.findall(r"(?m)^\d+\s+CALL KP\d\d ", up)), 40, up)
                self.assertNotIn("no further use", up)
        # the exact walker knows the callee's parameter and the COMMAREA it has not got
        up = self.flow("WU-CODE", "--program", "KUP", "--up", "--all")
        self.assertRegex(_lines_of(up, "CALL KMISS"), r"CALL KMISS arg 1 <- KMISS\.LK-MISS may set it \(BY REFERENCE\).*"
                                                      r"\[end: field not declared in this program \(missing copybook KNOCPY\)\]")
        self.assertRegex(_lines_of(up, "LINK KNOCA"), r"LINK KNOCA COMMAREA <- KNOCA\s.*\[end: callee has no DFHCOMMAREA 01\]")
        # a RETURNING item from a CALL with more candidates than listed: the rest is an end, not dropped
        ret = self.flow("WU-RET", "--program", "KUP", "--up", "--all")
        self.assertRegex(_lines_of(ret, "(+1 more)"), r"RETURNING <- WS-MANY: \(\+1 more\) candidate\(s\) not listed\s.*"
                                                      r"\[end: dynamic CALL unresolved \(via WS-MANY\)\]")
        self.assertEqual(len(re.findall(r"(?m)^\d+\s+RETURNING <- KP\d\d\s.*\[end: callee not in index\]", ret)), 40, ret)
        old = self.flow("WU-CODE", "--program", "KUP", "--up", db=self.old)
        self.assertRegex(_lines_of(old, "LINK KNOCA"), r"LINK KNOCA COMMAREA <- KNOCA \(reconstructed\)\s.*"
                                                       r"\[end: callee has no DFHCOMMAREA 01\]")

    def fixture_dbs(self):
        """The fixtures' index, and the same before the re-parse (the fallback walker)."""
        fx = os.path.join(self.td, "fx.db")
        old = os.path.join(self.td, "fxold.db")
        if not os.path.exists(old):
            with contextlib.redirect_stdout(io.StringIO()):
                build._main([FIX, "--db", fx, "--rebuild", "--quiet"])
            shutil.copyfile(fx, old)
            c = sqlite3.connect(old)
            c.executescript("DROP TABLE data_flow; DROP TABLE pfield; DROP TABLE call_arg; DROP TABLE param; "
                            "DROP TABLE file_record;")
            c.close()
        return fx, old

    def test_fallback_names_a_dynamic_call_as_the_exact_walker_does(self):
        # FLOWSRC's `CALL WS-PGM` reaches FLOWSUB and its ENTRY FLOWENT through MOVE literals: before the
        # re-parse too, each hop through it says it is a candidate, never `CALL FLOWSUB` as if static
        fx, old = self.fixture_dbs()
        dyn = _line("FLOWSRC.cbl", "CALL WS-PGM USING")
        for db, tag in ((fx, "exact"), (old, "reconstructed")):
            with self.subTest(tag):
                rc = self.flow("WS-RC", "--program", "FLOWSRC", "--up", db=db)
                self.assertRegex(rc, rf"(?m)^2\s+CALL FLOWSUB \(candidate: resolved via MOVE literal\) arg 2 <- FLOWSUB\.LK-RC\b"
                                     rf".*FLOWSRC:{dyn} \"CALL WS-PGM")
                lk = self.flow("LK-RC", "--program", "FLOWSUB", "--up", db=db)
                self.assertRegex(lk, rf"CALL WS-PGM = FLOWSUB \(candidate: resolved via MOVE literal\) arg 2 <- FLOWSRC\.WS-RC\b"
                                     rf".*FLOWSRC:{dyn} \"CALL WS-PGM")
                self.assertRegex(lk, rf"CALL WS-PGM = FLOWENT \(candidate: resolved via MOVE literal\) arg 1 <- FLOWSRC\.WS-STATUS\b")
                down = self.flow("LK-RC", "--program", "FLOWSUB", db=db)
                self.assertRegex(down, r"LINKAGE pos 2 \(CALL WS-PGM: MOVE literal candidate\) -> back to FLOWSRC\.WS-RC\b")
                self.assertRegex(down, r"LINKAGE pos 1 of ENTRY FLOWENT -> back to FLOWSRC\.WS-RC\b")
                for text in (rc, lk, down):
                    for ln in text.splitlines():
                        if f'FLOWSRC:{dyn} "CALL WS-PGM' in ln:
                            self.assertIn("candidate", ln, ln)

    def test_fallback_says_a_program_is_partial(self):
        # guard 23 before the re-parse: ERRPGM's COPY POLDCL is not in the fixtures, so the root says
        # so before any hop, as the exact walker does - not only the Unresolved table at the bottom
        fx, old = self.fixture_dbs()
        for db, tag in ((fx, "exact"), (old, "reconstructed")):
            with self.subTest(tag):
                out = self.flow("WS-ERR-CD", "--program", "ERRPGM", db=db)
                self.assertRegex(out, r"(?m)^- \[program partial: [^\]]*POLDCL[^\]]*\]$")
                self.assertLess(out.index("[program partial:"), re.search(r"(?m)^1\s", out).start())
                self.assertEqual(out.count("[program partial:"), 1, out)
        # a node in another program (KMISS: COPY KNOCPY missing) says it under that node, once
        up = self.flow("WU-CODE", "--program", "KUP", "--up", db=self.old)
        node = re.search(r"(?m)^\d+\s+CALL KMISS arg 1 <- KMISS\.LK-MISS .*\n\s+\[program partial: [^\]]*KNOCPY[^\]]*\]$", up)
        self.assertIsNotNone(node, up)
        self.assertEqual(up.count("[program partial:"), 1, up)

    def test_fallback_finds_the_dfhcommarea_and_the_fields_under_a_parameter(self):
        # FLOWCOMM has `01 DFHCOMMAREA.` (fields from COPY FLOWCOMC) and no PROCEDURE DIVISION USING, the usual
        # CICS shape: before the re-parse the LINK is a labelled "not followed", never "callee has no DFHCOMMAREA"
        fx, old = self.fixture_dbs()
        up = self.flow("WS-COMM", "--program", "FLOWCICS", "--up", db=old)
        self.assertRegex(_lines_of(up, "LINK FLOWCOMM"),
                         r"LINK FLOWCOMM COMMAREA <- FLOWCOMM\.DFHCOMMAREA: its fields are not all in FLOWCOMM's own text, "
                         r"may set it \(reconstructed\).*\[end: callee's fields under the parameter: not followed until re-parse\]")
        self.assertNotIn("no DFHCOMMAREA", up)
        down = self.flow("WS-COMM", "--program", "FLOWCICS", db=old)
        self.assertRegex(_lines_of(down, "LINK FLOWCOMM"), r"LINK FLOWCOMM arg 1 -> FLOWCOMM\.DFHCOMMAREA \(reconstructed\).*"
                                                           r"\[end: fields under the group: not followed until re-parse\]")
        self.assertNotIn("callee declares 0", down)
        # the exact walker follows the same COMMAREA to the field FLOWCOMM writes
        self.assertRegex(self.flow("WS-COMM", "--program", "FLOWCICS", "--up", db=fx),
                         r"LINK FLOWCOMM COMMAREA <- FLOWCOMM\.CA-MESSAGE .*written there, BY REFERENCE")
        # a group parameter written only through a field under it: a labelled end (it was dropped), both for
        # a CALL and for USING DFHCOMMAREA; a callee that writes nothing under it is no origin at all
        up = self.flow("WK-AREA", "--program", "KCA", "--up", db=self.old)
        self.assertRegex(_lines_of(up, "CALL KCE"), r"CALL KCE arg 1 <- KCE\.LK-AREA: a field under it is written there, may set "
                                                    r"it \(reconstructed\).*\[end: callee's fields under the parameter: not "
                                                    r"followed until re-parse\]")
        self.assertRegex(_lines_of(up, "LINK KCU"), r"LINK KCU COMMAREA <- KCU\.DFHCOMMAREA: a field under it is written there.*"
                                                    r"\[end: callee's fields under the parameter: not followed until re-parse\]")
        self.assertNotIn("KCN", up)
        # exact: the same two are followed to the field written
        ex = self.flow("WK-AREA", "--program", "KCA", "--up")
        self.assertRegex(ex, r"CALL KCE arg 1 <- KCE\.LK-B\b.*written there")
        self.assertRegex(ex, r"LINK KCU COMMAREA <- KCU\.CU-B\b.*written there")
        # down: a callee that reads a field under its parameter is not "no further use"
        down = self.flow("WK-AREA", "--program", "KCA", db=self.old)
        self.assertRegex(_lines_of(down, "CALL KCN"), r"CALL KCN arg 1 -> KCN\.LN-AREA \(reconstructed\).*"
                                                      r"\[end: fields under the group: not followed until re-parse\]")
        self.assertIn("[end: no further use in KCE]", _lines_of(down, "CALL KCE"))     # KCE only writes LK-B

    def test_icegener_copies_the_bytes_as_iebgener_does(self):
        job = "KGJOB.jcl"
        sysut1 = self.FILES[job].splitlines().index("//SYSUT1   DD DSN=TEST.KG.RAW,DISP=SHR") + 1
        for db, tag in ((self.db, "exact"), (self.old, "reconstructed")):
            with self.subTest(tag):
                down = self.flow("WS-GCODE", "--program", "KGW", db=db)
                self.assertRegex(down, rf'C1 ICEGENER copy - bytes unchanged -> TEST\.KG\.COPY\s+KGJOB:{sysut1} "//SYSUT1 DD')
                self.assertRegex(down, r"read by KGR\.IR-GCODE bytes 1-4 \(KGJOB R1 DD GIN")
                self.assertNotIn("KGJOB C1 ICEGENER   ", down)          # never the utility end any more
                # control statements re-arrange the fields: a labelled stop, never a pass-through
                self.assertRegex(down, r'KGJOB2 C2 IEBGENER has RECORD control statements\s+KGJOB2:6 "RECORD FIELD=\(2,3,,1\)"\s+'
                                       r"\[end: utility step - bytes not modelled\]")
                self.assertNotIn("KGJOB2 R2", down)
                up = self.flow("IR-GCODE", "--program", "KGR", "--up", db=db)
                self.assertRegex(up, r"C1 ICEGENER copy - bytes unchanged <- TEST\.KG\.RAW")
                self.assertRegex(up, r"written by KGW\.OR-CODE bytes 1-4 \(KGJOB W1 DD GOUT")
                self.assertNotIn("KGJOB C1 ICEGENER   ", up)
                self.assertRegex(up, r"KGJOB2 C2 IEBGENER has RECORD control statements\s.*\[end: utility step - bytes not modelled\]")
                # SYSIN from a dataset whose cards are not indexed: a labelled stop at the SYSIN DD, as SORT
                job3 = self.FILES["KGJOB3.jcl"].splitlines()
                sysin = [i + 1 for i, ln in enumerate(job3) if ln.startswith("//SYSIN")]
                for step, launcher, ln, dsn in (("C3", "ICEGENER", sysin[0], "TEST.CTL.LIB"), ("C4", "IEBGENER", sysin[1], "TEST.CTL.SEQ")):
                    want = (rf'KGJOB3 {step} {launcher}: control cards not indexed\s+KGJOB3:{ln} "//SYSIN DD DSN={re.escape(dsn)}"\s+'
                            r"\[end: utility step - bytes not modelled\]")
                    self.assertRegex(down, want)
                    self.assertRegex(up, want)
                self.assertNotIn("KGJOB3 R3", down)
                self.assertNotIn("KGJOB3 R4", down)
                self.assertNotRegex(down + up, r"C[34] (ICE|IEB)GENER copy")
                up3 = self.flow("IR-GCODE", "--program", "KGR", "--up", "--all", db=db)
                self.assertNotRegex(up3, r"C[34] (ICE|IEB)GENER copy")
                self.assertRegex(up3, r"KGJOB3 C3 ICEGENER: control cards not indexed")
                cites = [m for t in (down, up) for m in CITE.finditer(t)]
                answer = "\n".join(f'[[{m.group(1)} {m.group(2)}{"-" + m.group(3) if m.group(3) else ""} "{m.group(4)}"]]'
                                   for m in cites)
                res, _u = verify_citations.check_answer(answer, db_path=self.db)
                self.assertEqual([(r.raw, r.status) for r in res if r.status != "PASS"], [])

    def gate(self, *texts) -> dict:
        """{cite: status} from verify_citations for every cite in the texts."""
        cites = [m for t in texts for m in CITE.finditer(t)]
        answer = "\n".join(f'[[{m.group(1)} {m.group(2)}{"-" + m.group(3) if m.group(3) else ""} "{m.group(4)}"]]'
                           for m in cites)
        res, _u = verify_citations.check_answer(answer, db_path=self.db)
        return {r.raw: r.status for r in res}

    def test_a_dd_cite_quotes_its_own_statement_or_fails_the_gate(self):
        from atlas import flow
        conn = query.connect(self.db)
        try:
            w = flow._Walker(conn, flow.Opts())
            # a real DD whose line holds no DD (a comment here) quotes the DD it claims: the gate FAILS it,
            # never a cite with no token and never the EXEC text of another statement
            self.assertEqual(w.cite_dd("KPJOB", 5, "PIN", False), 'KPJOB:5 "//PIN DD"')
            self.assertEqual(w.cite_dd("KFJOB", 2, "PIN", False), 'KFJOB:2 "//PIN DD"')
            # another DD on the line is not this one
            self.assertEqual(w.cite_dd("KPJOB", 3, "PIN", True), 'KPJOB:3 "//PIN DD"')
            self.assertEqual(w.cite_dd("KPJOB", 3, "POUT", True), 'KPJOB:3 "//POUT DD DSN=TEST.KP.RAW"')
            # a pseudo-DD quotes the EXEC line only when it is the step's own EXEC
            self.assertEqual(w.cite_dd("KFJOB", 2, "*FTP*", False, "F1"), 'KFJOB:2 "//F1       EXEC PGM=FTP"')
            self.assertEqual(w.cite_dd("FTPPROC", 2, "*FTP*", False, "S1.P1"), 'FTPPROC:2 "//P1       EXEC PGM=FTP"')
            self.assertEqual(w.cite_dd("KFJOB", 2, "*FTP*", False, "S1.P1"), 'KFJOB:2 "//P1 EXEC"')
            # an interface row is cited only where its line is tied to the DSN
            self.assertEqual(w.cite_iface("KFJOB", 2, "TEST.KP.IN"), 'KFJOB:2 "//F1       EXEC PGM=FTP"')
            # the row of the PUT that S1 runs sits on KFJOB with FTPPROC's line: FTPPROC is cited
            self.assertEqual(w.cite_iface("KFJOB", 2, "TEST.KP.RAW"), 'FTPPROC:2 "//P1       EXEC PGM=FTP"')
            self.assertEqual(w.cite_iface("KFJOB", 3, "TEST.KP.RAW"), "")
            self.assertEqual(w.cite_iface("FTPPROC", 2, "TEST.KP.RAW"), 'FTPPROC:2 "//P1       EXEC PGM=FTP"')
        finally:
            conn.close()
        g = self.gate('KPJOB:5 "//PIN DD"', 'KFJOB:2 "//P1 EXEC"', 'KPJOB:3 "//PIN DD"', 'FTPPROC:2 "//P1       EXEC PGM=FTP"')
        self.assertEqual(sorted(g.values()), ["FAIL", "FAIL", "FAIL", "PASS"], g)
        # end to end: the PUT that S1 runs is one branch (the parser stores its pseudo-DD twice), and no
        # line cites F1, the GET of another dataset, for it
        for db in (self.db, self.old):
            down = self.flow("WS-PCODE", "--program", "KPW", db=db)
            self.assertEqual(len(re.findall(r"KFJOB S1\.P1 FTP ", down)), 1, down)
            self.assertNotIn("//F1", down)
            up = self.flow("IP-CODE", "--program", "KPR", "--up", db=db)
            self.assertNotIn("//F1", up)
            self.assertNotRegex(up, r"KFJOB S1\.P1 FTP ")       # a PUT is no writer of the dataset
            for cite, status in self.gate(down, up).items():
                if status == "PASS":
                    self.assertNotRegex(cite, r"KPJOB 5|KFJOB 2", cite)

    def test_a_proc_step_in_a_job_cites_the_proc_member(self):
        # the DDs of a step expanded from a PROC carry the PROC's line numbers: the cite names the PROC member,
        # so it is the DD it claims and the gate passes; the job's own DDs stay on the job
        for db in (self.db, self.old):
            down = self.flow("WS-PCODE", "--program", "KPW", db=db)
            self.assertRegex(down, r'KPJOB S1\.P1 DD PIN\b.*RDPROC:5 "//PIN DD"')
            self.assertRegex(down, r'KFJOB S1\.P1 FTP   FTPPROC:2 "//P1       EXEC PGM=FTP"   \[end: dataset leaves the mainframe')
            self.assertRegex(down, r'KPJOB:3 "//POUT DD DSN=TEST\.KP\.RAW"')
            up = self.flow("IP-CODE", "--program", "KPR", "--up", db=db)
            self.assertRegex(up, r'KPJOB S1\.P1 DD PIN\b.*RDPROC:5 "//PIN DD DSN=TEST\.KP\.RAW"')
            # the PROC's own interface row and its expansion in KFJOB are one line under each reader
            self.assertEqual(len(re.findall(r"(?m)^1\.\d+\s+TEST\.KP\.RAW ftp ", up)), 1, up)
            self.assertNotRegex(up, r"TEST\.KP\.RAW ftp   \[end")          # never without its cite
            g = self.gate(down, up)
            self.assertEqual([c for c, st in g.items() if st != "PASS"], [], g)
            self.assertNotIn("KPJOB 5", " ".join(g))
            self.assertNotIn("KFJOB 2", " ".join(g))

    def test_a_concatenated_dd_cites_its_own_line_and_passes_the_gate(self):
        # a continuation row carries the previous DD's name (CIN, SYSIN) but its line has no name field:
        # quoting `//CIN DD` there FAILED a correct hop; the line's own `// DD DSN=...` is the fact
        job = self.FILES["KCJOB.jcl"].splitlines()
        cin2 = job.index("//         DD DSN=TEST.KC.RAW,DISP=SHR") + 1
        sysin2 = job.index("//         DD DSN=TEST.CTL.KCSEQ,DISP=SHR") + 1
        from atlas import flow
        conn = query.connect(self.db)
        try:
            w = flow._Walker(conn, flow.Opts())
            self.assertEqual(w.cite_dd("KCJOB", cin2, "CIN", False, "R1"), f'KCJOB:{cin2} "// DD DSN=TEST.KC.RAW"')
            self.assertEqual(w.cite_dd("KCJOB", cin2, "CIN", True, "R1"), f'KCJOB:{cin2} "// DD DSN=TEST.KC.RAW"')
            self.assertEqual(w.cite_dd("KCPROC", 4, "CIN", False, "S3.P1"), 'KCPROC:4 "// DD DSN=TEST.KC.RAW"')
            # the first DD of the concatenation keeps its name
            self.assertEqual(w.cite_dd("KCJOB", cin2 - 1, "CIN", True, "R1"), f'KCJOB:{cin2 - 1} "//CIN DD DSN=TEST.KC.OTHER"')
        finally:
            conn.close()
        self.assertEqual(self.gate(f'KCJOB:{cin2} "//CIN DD"'), {f'[[KCJOB {cin2} "//CIN DD"]]': "FAIL"})
        for db, tag in ((self.db, "exact"), (self.old, "reconstructed")):
            with self.subTest(tag):
                down = self.flow("WS-CCODE", "--program", "KCW", db=db)
                self.assertRegex(down, rf'read by KCR\.IC-CODE bytes 1-4 \(KCJOB R1 DD CIN\b.*KCJOB:{cin2} "// DD DSN=TEST\.KC\.RAW"')
                self.assertRegex(down, r'read by KCR\.IC-CODE bytes 1-4 \((PROC KCPROC|KCJOB S3)\.?\S* DD CIN\b.*'
                                       r'KCPROC:4 "// DD DSN=TEST\.KC\.RAW"')
                self.assertRegex(down, rf'KCJOB C2 IEBGENER: control cards not indexed\s+KCJOB:{sysin2} '
                                       r'"// DD DSN=TEST\.CTL\.KCSEQ"\s+\[end: utility step - bytes not modelled\]')
                self.assertNotRegex(down, r'"//(CIN|SYSIN) DD"')
                up = self.flow("IC-CODE", "--program", "KCR", "--up", "--all", db=db)
                self.assertRegex(up, rf'READ IN-REC <- TEST\.KC\.RAW \(KCJOB R1 DD CIN\b.*KCJOB:{cin2} "// DD DSN=TEST\.KC\.RAW"')
                self.assertRegex(up, r'READ IN-REC <- TEST\.KC\.RAW .*KCPROC:4 "// DD DSN=TEST\.KC\.RAW"')
                g = self.gate(down, up)
                self.assertEqual({c: st for c, st in g.items() if st != "PASS"}, {}, g)
                self.assertIn(f'[[KCJOB {cin2} "// DD DSN=TEST.KC.RAW"]]', g)
                self.assertIn(f'[[KCJOB {sysin2} "// DD DSN=TEST.CTL.KCSEQ"]]', g)

    def test_an_idcams_step_cites_its_own_exec_line(self):
        # every pseudo-DD cite knows its step, so the *REPRO* rows on C5's EXEC line quote that EXEC and pass
        for db in (self.db, self.old):
            down = self.flow("WS-GCODE", "--program", "KGW", db=db)
            self.assertRegex(down, r'C5 IDCAMS copy - bytes unchanged -> TEST\.KG\.REP\s+KGJOB4:2 "//C5       EXEC PGM=IDCAMS"')
            self.assertRegex(down, r"read by KGR\.IR-GCODE bytes 1-4 \(KGJOB4 R5 DD GIN")
            up = self.flow("IR-GCODE", "--program", "KGR", "--up", "--all", db=db)
            self.assertRegex(up, r'C5 IDCAMS copy - bytes unchanged <- TEST\.KG\.RAW\s+KGJOB4:2 "//C5       EXEC PGM=IDCAMS"')
            g = self.gate(down, up)
            self.assertEqual({c: st for c, st in g.items() if "KGJOB4" in c and st != "PASS"}, {}, g)
            self.assertIn("PASS", {st for c, st in g.items() if "KGJOB4 2" in c})

    def test_up_ends_at_the_fixture_callees_it_cannot_follow(self):
        # the fixtures' own shapes (ROADMAP example): ERRLOG is not in the index and may set WS-ERR-CD;
        # FLOWSUB declares 2 parameters and is passed WS-EXTRA third
        fx, old = self.fixture_dbs()
        for db, tag in ((fx, "exact"), (old, "reconstructed")):
            with self.subTest(tag):
                err = self.flow("WS-ERR-CD", "--program", "ERRPGM", "--up", db=db)
                self.assertRegex(err, rf"(?m)^1\s+CALL ERRLOG arg 1 <- ERRLOG may set it\b.*ERRPGM:"
                                      rf"{_line('ERRPGM.cbl', 'ERRLOG')} \"CALL 'ERRLOG' USING WS-ERR-CD\"\s+"
                                      r"\[end: callee not in index\]")
                ext = self.flow("WS-EXTRA", "--program", "FLOWSRC", "--up", db=db)
                self.assertRegex(ext, r"(?m)^1\s+CALL FLOWSUB arg 3 <- FLOWSUB \(callee declares 2 parameter\(s\)\).*"
                                      r"\[end: LINKAGE position out of range / count mismatch - HUMAN MUST VERIFY\]")


if __name__ == "__main__":
    unittest.main(verbosity=2)
