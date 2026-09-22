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
byte off), calls FLOWSUB by name, by ENTRY alias with the parameters swapped
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
        edge = c.execute("SELECT id, returning FROM call_edge WHERE program_id=? AND returning IS NOT NULL", (self.pid("FLOWSRC"),)).fetchall()
        self.assertEqual([r["returning"] for r in edge], ["WS-R"])
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
        self.assertRegex(out, rf"MOVE -> WS-STAT-ENT \(subscripted: any of 3 elements\)\s+FLOWSRC:{L('TO WS-STAT-ENT (WS-I)')}")
        self.assertRegex(out, r"derived edges \(COMPUTE/STRING/FUNCTION\): 1 \(--derived follows them\)")
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
        self.assertNotIn("WS-HOLD", one)
        narrow = self.flow("WS-STATUS", "--program", "FLOWSRC", "--width", "1")
        drop = [ln for ln in narrow.splitlines() if "(--all)" in ln]
        self.assertEqual(len(drop), 1, narrow)
        self.assertRegex(drop[0], r"\d+ more .*FLOWSRC \d+")              # every dropped program, with its count
        self.assertNotIn("(--all)", self.flow("WS-STATUS", "--program", "FLOWSRC", "--width", "1", "--all"))
        self.assertIn("node cap", self.flow("WS-STATUS", "--program", "FLOWSRC", "--nodes", "1"))
        ping = self.flow("WS-PING", "--program", "FLOWSRC")
        self.assertIn("WS-PONG", ping)
        self.assertEqual(ping.count("(shown as "), 1, ping)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
