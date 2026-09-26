r"""
The synthetic estate's findings in the expander and the copybook parser (docs/SYNTH-findings-2026-09-25.md, the minimal
reproductions under tools/synth/repro/; ROADMAP re-parse item 27; LESSONS 214). Each finding is pinned here beside the
reproduction that showed it.

F05 - `EXEC SQL` / `INCLUDE name` / `END-EXEC.` on three lines, the way DCLGEN-era programs write it, was not expanded:
only the one-line form was. The program got no copy_use row, the DCLGEN's fields were not its own, `copybook NAME` did
not list it and `flow` could not start from the host variable, while the member read `parse: ok` (the report's F05,
F08, F13, F39, F41, F49). The words of the INCLUDE are now read over the lines after an `EXEC SQL` that ends its line.

F06 - a copybook that COPYs another: its OWN field rows (`layout COPYBOOK`, `field NAME` without --program) ignored the
nested copybook's bytes, so every item after the nested COPY sat that many bytes too early - the program's own view was
right (the report's F28, F29, F32, F33: 58 offsets 121 bytes short). The copybook's layout is now computed over its
text with the nested copybook put in place by the resolver the programs use; its rows are its own items only, each at
its own line (the nested items are the nested member's rows), its copy_use row names the member expanded, and a nested
COPY whose text is not in the layout is a 'layout_warning' on the copybook.

F07 - a copybook written for COPY REPLACING, with a :TAG: of pseudo-text in its names (`:PCB:-STATUS`, the PCB mask
every IMS program copies with `REPLACING ==:PCB:== BY ==xxx==`), had no field row, no 88 and no layout (`layout` said
NOT FOUND), and no field_alias row tied a program's name back to it (the report's F01-F03, F30, F31: 164 facts). A
tagged name is a data name now, in the copybook parser and in the expander's alias rows.

F16 - `COPY DFHAID` (CICS supplies it from SDFHCOB, which no shop keeps among its copybooks) made every CICS program
`partial` and coverage sent him to fetch a library the estate never holds. The copybooks IBM supplies (CICS DFHAID,
DFHBMSCA, DFHEIBLK, DFHEIVAR, DFHMSRCA; MQ CMQV, CMQXV and the CMQ*V / CMQ*L structures), and the names the
manifest's `system_includes` adds, are 'IBM-supplied, not in the estate' when no member carries the name: no gap, no
fetch, their own table in coverage. A shop that keeps one has it expanded like any copybook.

Found while writing F07 (LESSONS 215): `OCCURS 1 TO 10 DEPENDING ON WS-CNT` without TIMES was read as a fixed table - the
clause after the number was never tried.

The stand-ins of this week (partial_kind, is_truly_partial, the chosen-among-several row, recover's arrived / misfiled
/ re-file / disk-check steps) find nothing to do on an index holding these members, and keep working on an index built
before the item.
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

from atlas import build, copybook, expand, fetch, query, reader, recover  # noqa: E402

REPRO = os.path.join(ROOT, "tools", "synth", "repro")


def lines_of(text):
    return reader.read_cobol_lines(text)[0]


def line_of(text, needle):
    """1-based number of the first line of `text` holding `needle`."""
    for i, ln in enumerate(text.splitlines(), 1):
        if needle in ln:
            return i
    raise AssertionError(f"{needle!r} not in the text")


def program(name, data, proc, linkage=""):
    """A fixed-format program: `data` the WORKING-STORAGE lines, `proc` the PROCEDURE DIVISION's, each written from
    column 8 (area A) - indent four more for area B."""
    def block(rows):
        return "".join(f"       {r}\n" for r in rows)
    return ("       IDENTIFICATION DIVISION.\n"
            f"       PROGRAM-ID. {name}.\n"
            "       DATA DIVISION.\n"
            "       WORKING-STORAGE SECTION.\n"
            + block(data)
            + (("       LINKAGE SECTION.\n" + block(linkage)) if linkage else "")
            + "       PROCEDURE DIVISION.\n"
            + block(proc))


# --------------------------------------------------------------------------- the members (fictional names)

INCTBL = ("           EXEC SQL DECLARE PRD.INC_TBL TABLE\n"
          "           ( INC_KEY                        CHAR(12) NOT NULL,\n"
          "             STATUS_CD                      CHAR(2) NOT NULL\n"
          "           ) END-EXEC.\n"
          "       01  DCLINC-TBL.\n"
          "           10  INC-KEY                PIC X(12).\n"
          "           10  STATUS-CD              PIC X(02).\n")
INCPGM3 = program("INCPGM3", [
    "    EXEC SQL",
    "        INCLUDE SQLCA",
    "    END-EXEC.",
    "    EXEC SQL",
    "        INCLUDE INCTBL",
    "    END-EXEC.",
    "01  WS-AFTER               PIC X(4).",
], [
    "0000-MAIN.",
    "    MOVE 'AC' TO STATUS-CD",
    "    EXEC SQL",
    "        SELECT INC_KEY INTO :INC-KEY FROM PRD.INC_TBL",
    "         WHERE STATUS_CD = :STATUS-CD",
    "    END-EXEC",
    "    GOBACK.",
])
INCPGM1 = program("INCPGM1", [
    "    EXEC SQL INCLUDE SQLCA END-EXEC.",
    "    EXEC SQL INCLUDE INCTBL END-EXEC.",
], [
    "0000-MAIN.",
    "    MOVE 'AC' TO STATUS-CD",
    "    GOBACK.",
])

# F06, the group-header shape: the nested COPY fills NST-ADDRESS
NSTOUTR = ("       05  NST-KEY                PIC X(10).\n"
           "       05  NST-ADDRESS.\n"
           "           COPY NSTINNR.\n"
           "       05  NST-AFTER              PIC X(05).\n"
           "           88  NST-AFTER-SET      VALUE 'Y'.\n")
NSTINNR = ("           10  NST-INNER-A            PIC X(20).\n"
           "           10  NST-INNER-B            PIC X(05).\n")
NSTPGM = program("NSTPGM", ["01  NST-RECORD.", "    COPY NSTOUTR."], ["0000-MAIN.", "    MOVE SPACES TO NST-AFTER",
                                                                        "    GOBACK."])
# F06, the synthetic master record's shape: the COPY stands between two 05s and the nested copybook holds its own 05
NSTMASTR = ("      * MASTER RECORD - THE ADDRESS GROUP IS ITS OWN COPYBOOK\n"
            "       05  MST-KEY                PIC X(08).\n"
            "           COPY NSTADDR.\n"
            "       05  MST-AMT                PIC S9(7)V99 COMP-3.\n"
            "       05  MST-TBL                OCCURS 3 TIMES.\n"
            "           10  MST-CODE           PIC X(02).\n"
            "       05  MST-TAIL               PIC X(04).\n")
NSTADDR = ("       05  MST-ADDRESS.\n"
           "           10  MST-LINE           PIC X(30) OCCURS 2 TIMES.\n"
           "           10  MST-CITY           PIC X(20).\n")
NSTPGM2 = program("NSTPGM2", ["01  MST-RECORD.", "    COPY NSTMASTR."], ["0000-MAIN.", "    MOVE SPACES TO MST-TAIL",
                                                                          "    GOBACK."])
# a data copybook whose nested copybook is not in the estate; one that copies itself; one that copies an IBM one
NSTGAP = ("       05  GAP-KEY                PIC X(04).\n"
          "           COPY NSTMISS.\n"
          "       05  GAP-AFTER              PIC X(02).\n")
GAPPGM = program("GAPPGM", ["01  GAP-RECORD.", "    COPY NSTGAP."], ["0000-MAIN.", "    MOVE SPACES TO GAP-AFTER",
                                                                      "    GOBACK."])
NSTSELF = ("       05  SLF-A                  PIC X(02).\n"
           "           COPY NSTSELF.\n"
           "       05  SLF-B                  PIC X(02).\n")
# OS/VS `01 X COPY Y.` inside a copybook: X is the copybook's own item, at the COPY line, with Y's bytes
OSVOUT = ("      * TWO RECORDS\n"
          "       01  OSV-REC COPY OSVIN.\n"
          "       01  OSV-TAIL.\n"
          "           05  OSV-T    PIC X(03).\n")
OSVIN = ("       01  OSVIN-REC.\n"
         "           05  OSV-A    PIC X(02).\n"
         "           05  OSV-B    PIC 9(04).\n")
# an own item under a group the nested copybook opens: it hangs from its nearest own ancestor (none: a root)
NSTHEAD = ("           COPY NSTHDRG.\n"
           "               10  HDR-ITEM       PIC X(03).\n"
           "       05  HDR-NEXT               PIC X(01).\n")
NSTHDRG = ("       05  HDR-GRP.\n"
           "           10  HDR-FIRST          PIC X(02).\n")
NSTIBM = ("       05  IBM-KEY                PIC X(04).\n"
          "           COPY DFHBMSCA.\n"
          "       05  IBM-AFTER              PIC X(02).\n")

# F07: the PCB mask, written for COPY REPLACING
TAGPCB = ("       01  :PCB:-PCB.\n"
          "           05  :PCB:-DBD-NAME         PIC X(08).\n"
          "           05  :PCB:-SEG-LEVEL        PIC X(02).\n"
          "           05  :PCB:-STATUS           PIC X(02).\n"
          "               88  :PCB:-OK           VALUE '  '.\n"
          "               88  :PCB:-END-OF-DB    VALUE 'GB'.\n"
          "           05  :PCB:-PROC-OPT         PIC X(04).\n"
          "           05  :PCB:-KEY-LEN          PIC S9(5) COMP.\n"
          "           05  :PCB:-KEY-ALT REDEFINES :PCB:-KEY-LEN PIC X(4).\n")
TAGPGM = program("TAGPGM", [
    "01  WS-GU            PIC X(04) VALUE 'GU  '.",
    "01  WS-AREA          PIC X(120).",
], [
    "0000-MAIN.",
    "    CALL 'CBLTDLI' USING WS-GU CLM-PCB WS-AREA",
    "    IF CLM-END-OF-DB",
    "        DISPLAY 'END'",
    "    END-IF",
    "    MOVE CLM-STATUS TO WS-AREA",
    "    GOBACK.",
], linkage=["    COPY TAGPCB REPLACING ==:PCB:== BY ==CLM==."])

# F16: the IBM-supplied copybooks
CICSPGM = program("CICSPGM", [
    "01  WS-COMM          PIC X(100).",
    "    COPY DFHAID.",
    "    COPY DFHBMSCA.",
], [
    "0000-MAIN.",
    "    IF EIBAID = DFHENTER",
    "        DISPLAY 'ENTER'",
    "    END-IF",
    "    EXEC CICS RETURN END-EXEC",
    "    GOBACK.",
])
MQPGM = program("MQPGM", ["01  MQ-CONSTANTS.", "    COPY CMQV."], ["0000-MAIN.", "    GOBACK."])

ESTATE = (("GC/PROD.GC.SRC/INCPGM3.cbl", INCPGM3), ("GC/PROD.GC.SRC/INCPGM1.cbl", INCPGM1),
          ("GC/PROD.GC.DCLGEN/INCTBL.cpy", INCTBL),
          ("GC/PROD.GC.SRC/NSTPGM.cbl", NSTPGM), ("GC/PROD.GC.COPYLIB/NSTOUTR.cpy", NSTOUTR),
          ("GC/PROD.GC.COPYLIB/NSTINNR.cpy", NSTINNR),
          ("GC/PROD.GC.SRC/NSTPGM2.cbl", NSTPGM2), ("GC/PROD.GC.COPYLIB/NSTMASTR.cpy", NSTMASTR),
          ("GC/PROD.GC.COPYLIB/NSTADDR.cpy", NSTADDR),
          ("GC/PROD.GC.SRC/GAPPGM.cbl", GAPPGM), ("GC/PROD.GC.COPYLIB/NSTGAP.cpy", NSTGAP),
          ("GC/PROD.GC.COPYLIB/NSTSELF.cpy", NSTSELF), ("GC/PROD.GC.COPYLIB/NSTIBM.cpy", NSTIBM),
          ("GC/PROD.GC.COPYLIB/OSVOUT.cpy", OSVOUT), ("GC/PROD.GC.COPYLIB/OSVIN.cpy", OSVIN),
          ("GC/PROD.GC.COPYLIB/NSTHEAD.cpy", NSTHEAD), ("GC/PROD.GC.COPYLIB/NSTHDRG.cpy", NSTHDRG),
          ("GC/PROD.GC.SRC/TAGPGM.cbl", TAGPGM), ("SHARED/PROD.CMN.COPYLIB/TAGPCB.cpy", TAGPCB),
          ("GC/PROD.GC.SRC/CICSPGM.cbl", CICSPGM), ("GC/PROD.GC.SRC/MQPGM.cbl", MQPGM))


def write_estate(root, files):
    for rel, text in files:
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)


def run_build(root, db, *extra):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = build._main([root, "--db", db, "--quiet", *extra])
    assert rc == 0, buf.getvalue()
    return buf.getvalue()


# ===========================================================================
# F05 - the INCLUDE over several lines
# ===========================================================================

class IncludeOverLines(unittest.TestCase):

    def resolve(self, name, _lib):
        return (77, lines_of(INCTBL), None) if name == "INCTBL" else None

    def expanded(self, text):
        return expand.expand(lines_of(text), 1, self.resolve)

    def test_three_lines_are_expanded_like_the_one_line_form(self):
        three, one = self.expanded(INCPGM3), self.expanded(INCPGM1)
        self.assertEqual(three.copies, [("SQLCA", None, None, line_of(INCPGM3, "INCLUDE SQLCA") - 1, None),
                                        ("INCTBL", None, None, line_of(INCPGM3, "INCLUDE INCTBL") - 1, 77)])
        self.assertEqual([c[0] for c in one.copies], ["SQLCA", "INCTBL"])
        self.assertEqual(three.warnings, [])
        # the three lines are comments in the expansion, the DCLGEN's lines follow at depth 1
        for k in range(line_of(INCPGM3, "INCLUDE INCTBL") - 1, line_of(INCPGM3, "INCLUDE INCTBL") + 2):
            self.assertTrue(three.lines[k - 1 + 0].is_comment or three.origin(k)[2] == 0)
        text = expand.expanded_text(three)
        self.assertIn("10  STATUS-CD", text)
        exp_line = next(i for i, ln in enumerate(three.lines, 1) if "STATUS-CD              PIC" in ln.code)
        self.assertEqual(three.origin(exp_line), (77, 7, 1))
        # the program line after the END-EXEC stays live at its own number
        after = next(i for i, ln in enumerate(three.lines, 1) if "WS-AFTER" in ln.code)
        self.assertFalse(three.lines[after - 1].is_comment)
        self.assertEqual(three.origin(after), (1, line_of(INCPGM3, "WS-AFTER"), 0))

    def test_two_lines_and_the_name_on_a_line_of_its_own(self):
        for rows in (["    EXEC SQL INCLUDE", "        INCTBL", "    END-EXEC."],
                     ["    EXEC SQL", "        INCLUDE INCTBL END-EXEC."],
                     ["    EXEC", "        SQL INCLUDE INCTBL", "    END-EXEC."],
                     ["    EXEC SQL", "*THE TABLE'S DCLGEN", "        INCLUDE INCTBL", "    END-EXEC."]):
            # a row starting with '*' is a comment line: its '*' goes to column 7
            exp = self.expanded(program("TWOPGM", rows, ["0000-MAIN.", "    GOBACK."]).replace("       *THE", "      *THE"))
            self.assertEqual([(c[0], c[4]) for c in exp.copies], [("INCTBL", 77)], rows)
            self.assertIn("10  INC-KEY", expand.expanded_text(exp), rows)

    def test_anything_else_is_left_alone(self):
        for rows in (["    EXEC SQL", "        DECLARE C1 CURSOR FOR SELECT A FROM T", "    END-EXEC."],
                     ["    EXEC CICS", "        INCLUDE INCTBL", "    END-EXEC."],
                     ["    DISPLAY 'EXEC SQL'", "    DISPLAY 'INCLUDE INCTBL'."]):
            text = program("OTHPGM", rows, ["0000-MAIN.", "    GOBACK."])
            exp = self.expanded(text)
            self.assertEqual(exp.copies, [], rows)
            self.assertEqual(sum(1 for ln in exp.lines if ln.is_comment), 0, rows)

    def test_an_include_without_its_end_exec_keeps_the_next_line(self):
        text = program("NOEPGM", ["    EXEC SQL", "        INCLUDE INCTBL", "01  WS-NEXT   PIC X."],
                       ["0000-MAIN.", "    GOBACK."])
        exp = self.expanded(text)
        self.assertEqual([(c[0], c[4]) for c in exp.copies], [("INCTBL", 77)])
        nxt = next(ln for ln in exp.lines if "WS-NEXT" in ln.code)
        self.assertFalse(nxt.is_comment, "the entry after the statement is the program's, not the INCLUDE's")
        # the one-line form without its END-EXEC: the same
        text = program("NOEPGM", ["    EXEC SQL INCLUDE INCTBL", "01  WS-NEXT   PIC X."], ["0000-MAIN.", "    GOBACK."])
        nxt = next(ln for ln in self.expanded(text).lines if "WS-NEXT" in ln.code)
        self.assertFalse(nxt.is_comment)

    def test_the_words_read_over_the_lines(self):
        rows = lines_of("       EXEC SQL\n           INCLUDE INCTBL\n       END-EXEC.\n")
        code = rows[0].code
        self.assertEqual(expand.sql_include_over_lines(rows, 0, code, expand._mask_literals(code)), "INCTBL")
        rows = lines_of("       EXEC SQL\n           SELECT X\n       END-EXEC.\n")
        self.assertIsNone(expand.sql_include_over_lines(rows, 0, rows[0].code, rows[0].code))
        rows = lines_of("       MOVE 'EXEC SQL' TO X\n           INCLUDE Y\n")
        code = rows[0].code
        self.assertIsNone(expand.sql_include_over_lines(rows, 0, code, expand._mask_literals(code)))


# ===========================================================================
# F07 - names with a :TAG:, and OCCURS DEPENDING ON without TIMES
# ===========================================================================

class TaggedNames(unittest.TestCase):

    def test_the_copybook_has_its_items_under_their_tagged_names(self):
        roots, _w = copybook.parse_copybook(TAGPCB)
        by = {f.name: f for f in copybook.flatten(roots)}
        self.assertEqual([f.name for f in copybook.flatten(roots)],
                         [":PCB:-PCB", ":PCB:-DBD-NAME", ":PCB:-SEG-LEVEL", ":PCB:-STATUS", ":PCB:-PROC-OPT",
                          ":PCB:-KEY-LEN", ":PCB:-KEY-ALT"])
        self.assertEqual((by[":PCB:-PCB"].length, by[":PCB:-STATUS"].offset, by[":PCB:-KEY-LEN"].offset), (20, 10, 16))
        self.assertEqual(by[":PCB:-STATUS"].conds, [(":PCB:-OK", ["'  '"], 5), (":PCB:-END-OF-DB", ["'GB'"], 6)])
        self.assertEqual((by[":PCB:-KEY-ALT"].redefines, by[":PCB:-KEY-ALT"].offset), (":PCB:-KEY-LEN", 16))
        self.assertEqual((by[":PCB:-KEY-LEN"].usage, by[":PCB:-KEY-ALT"].usage), ("COMP", None))

    def test_a_tag_anywhere_in_the_name(self):
        roots, _w = copybook.parse_copybook("       01  WS-:SFX:-REC.\n"
                                            "           05  :P:-CNT      PIC 9(2).\n"
                                            "           05  :P:-COMP     REDEFINES :P:-CNT PIC X(2).\n"
                                            "           05  PM-:SFX:     OCCURS 1 TO 5 DEPENDING ON :P:-CNT.\n"
                                            "               10  PM-X     PIC X.\n")
        by = {f.name: f for f in copybook.flatten(roots)}
        self.assertEqual(sorted(by), [":P:-CNT", ":P:-COMP", "PM-:SFX:", "PM-X", "WS-:SFX:-REC"])
        self.assertIsNone(by[":P:-COMP"].usage, "a tagged operand naming COMP is no usage")
        self.assertEqual((by["PM-:SFX:"].odo_on, by["PM-:SFX:"].occurs_max, by["PM-:SFX:"].offset), (":P:-CNT", 5, 2))

    def test_replacing_ties_each_program_name_back(self):
        prog = program("TAGPGM", [], ["0000-MAIN.", "    GOBACK."],
                       linkage=["    COPY TAGPCB REPLACING ==:PCB:== BY ==CLM==."])
        exp = expand.expand(lines_of(prog), 1, lambda n, _l: (5, lines_of(TAGPCB), None) if n == "TAGPCB" else None)
        self.assertEqual(exp.aliases, [
            ("TAGPCB", ":PCB:-PCB", "CLM-PCB", 1), ("TAGPCB", ":PCB:-DBD-NAME", "CLM-DBD-NAME", 2),
            ("TAGPCB", ":PCB:-SEG-LEVEL", "CLM-SEG-LEVEL", 3), ("TAGPCB", ":PCB:-STATUS", "CLM-STATUS", 4),
            ("TAGPCB", ":PCB:-OK", "CLM-OK", 5), ("TAGPCB", ":PCB:-END-OF-DB", "CLM-END-OF-DB", 6),
            ("TAGPCB", ":PCB:-PROC-OPT", "CLM-PROC-OPT", 7), ("TAGPCB", ":PCB:-KEY-LEN", "CLM-KEY-LEN", 8),
            ("TAGPCB", ":PCB:-KEY-ALT", "CLM-KEY-ALT", 9)])


class OccursDependingWithoutTimes(unittest.TestCase):

    def test_the_depending_clause_is_read_with_or_without_times(self):
        # the clause on a line of its own: columns 73-80 are the identification area
        for clause in ("OCCURS 1 TO 10 DEPENDING ON WS-CNT", "OCCURS 1 TO 10 TIMES DEPENDING ON WS-CNT",
                       "OCCURS 1 TO 10 DEPENDING WS-CNT", "OCCURS 1 TO 10\n                   DEPENDING ON WS-CNT"):
            roots, warns = copybook.parse_copybook("       01  WS-GRP.\n"
                                                   "           05  WS-CNT     PIC 9(2).\n"
                                                   "           05  WS-TBL     PIC X(3)\n"
                                                   f"               {clause}.\n"
                                                   "           05  WS-AFTER   PIC X(10).\n")
            by = {f.name: f for f in copybook.flatten(roots)}
            self.assertEqual((by["WS-TBL"].odo_on, by["WS-TBL"].occurs, by["WS-TBL"].occurs_max), ("WS-CNT", 1, 10), clause)
            self.assertEqual(by["WS-AFTER"].offset, 32, clause)
            self.assertTrue(any("VARIABLE length" in w for w in warns), clause)

    def test_a_table_with_no_depending_clause_has_none(self):
        for clause in ("OCCURS 5 TIMES", "OCCURS 5", "OCCURS 5 INDEXED BY WS-IX", "OCCURS 5 TIMES INDEXED BY WS-IX"):
            roots, _w = copybook.parse_copybook(f"       01  WS-GRP.\n           05  WS-TBL  PIC X(3) {clause}.\n")
            tbl = copybook.flatten(roots)[1]
            self.assertEqual((tbl.odo_on, tbl.occurs_max), (None, 5), clause)


# ===========================================================================
# the build and the reports
# ===========================================================================

class _Built(unittest.TestCase):
    files = ESTATE
    manifest = None

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        cls.db = os.path.join(cls.td, "t.db")
        write_estate(cls.root, cls.files)
        extra = []
        if cls.manifest is not None:
            path = os.path.join(cls.td, "manifest.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cls.manifest, fh)
            extra = ["--manifest", path]
        run_build(cls.root, cls.db, "--rebuild", *extra)

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

    def status(self, name):
        return self.q("SELECT parse_status FROM member WHERE name=?", name)[0][0]

    def rows(self, member):
        return self.q("SELECT f.name, f.level, f.offset, f.length, f.is_group, f.line FROM field f "
                      "JOIN member m ON m.id = f.member_id WHERE m.name=? ORDER BY f.id", member)

    def pfield(self, prog, name):
        return self.q("SELECT f.offset, f.length, f.copy_field_id FROM pfield f JOIN program p ON p.id = f.program_id "
                      "WHERE p.program_id=? AND f.name=?", prog, name)

    def warnings(self, member):
        return self.q("SELECT u.detail, u.line FROM unresolved u JOIN member m ON m.id = u.member_id "
                      "WHERE m.name=? AND u.kind='layout_warning' ORDER BY u.id", member)

    def copy_row(self, copier, book):
        return self.q("SELECT c.resolved_member_id FROM copy_use c JOIN member m ON m.id = c.member_id "
                      "WHERE m.name=? AND c.copybook=?", copier, book)


class ThreeLineInclude(_Built):
    """F05 in the build: INCPGM3 (three lines) and INCPGM1 (one line) both expand INCTBL."""

    def test_the_row_the_fields_and_the_status(self):
        for prog in ("INCPGM3", "INCPGM1"):
            self.assertEqual(self.copy_row(prog, "INCTBL"), [(self.member_id("INCTBL"),)], prog)
            self.assertEqual(self.copy_row(prog, "SQLCA"), [(None,)], prog)
            self.assertEqual(len(self.pfield(prog, "STATUS-CD")), 1, prog)
            self.assertEqual(self.status(prog), "ok", prog)
        self.assertEqual(self.q("SELECT c.line FROM copy_use c JOIN member m ON m.id = c.member_id "
                                "WHERE m.name='INCPGM3' ORDER BY c.line"),
                         [(line_of(INCPGM3, "INCLUDE SQLCA") - 1,), (line_of(INCPGM3, "INCLUDE INCTBL") - 1,)])
        # the host variable of the SELECT links to the DCLGEN's item, as in the one-line program
        self.assertEqual(self.q("""SELECT COUNT(*) FROM sql_col_ref s JOIN program p ON p.id = s.program_id
                                   WHERE p.program_id='INCPGM3' AND s.host_var='STATUS-CD' AND s.pfield_id IS NOT NULL"""),
                         [(1,)])

    def test_copybook_and_program_say_it(self):
        page = self.page(query.cmd_copybook, "INCTBL")
        self.assertIn("### Programs including it (2)", page)
        self.assertIn("INCPGM3", page)
        page = self.page(query.cmd_program, "INCPGM3")
        cps = page.split("### Copybooks")[1].split("\n### ")[0]
        self.assertIn("| INCTBL | INCTBL |", cps)
        self.assertIn("supplied by the DB2 precompiler", cps)
        self.assertNotIn("NOT FOUND", cps)


class NestedCopybookLayout(_Built):
    """F06 in the build: the copybook's own rows count the nested copybook's bytes."""

    def test_the_group_header_shape(self):
        self.assertEqual(self.rows("NSTOUTR"), [("NST-KEY", 5, 0, 10, 0, 1), ("NST-ADDRESS", 5, 10, 25, 1, 2),
                                                ("NST-AFTER", 5, 35, 5, 0, 4)])
        self.assertEqual(self.rows("NSTINNR"), [("NST-INNER-A", 10, 0, 20, 0, 1), ("NST-INNER-B", 10, 20, 5, 0, 2)],
                         "the nested items are the nested member's own rows, at its own offsets")
        self.assertEqual(self.q("SELECT c.name, c.line FROM cond88 c JOIN field f ON f.id = c.field_id "
                                "JOIN member m ON m.id = f.member_id WHERE m.name='NSTOUTR'"), [("NST-AFTER-SET", 5)])
        self.assertEqual(self.copy_row("NSTOUTR", "NSTINNR"), [(self.member_id("NSTINNR"),)])
        self.assertEqual(self.warnings("NSTOUTR"), [])
        self.assertEqual(self.status("NSTOUTR"), "ok")

    def test_the_program_view_agrees_and_links_to_the_rows(self):
        after = self.q("SELECT f.id FROM field f JOIN member m ON m.id = f.member_id "
                       "WHERE m.name='NSTOUTR' AND f.name='NST-AFTER'")[0][0]
        inner = self.q("SELECT f.id FROM field f JOIN member m ON m.id = f.member_id "
                       "WHERE m.name='NSTINNR' AND f.name='NST-INNER-B'")[0][0]
        self.assertEqual(self.pfield("NSTPGM", "NST-AFTER"), [(35, 5, after)])
        self.assertEqual(self.pfield("NSTPGM", "NST-INNER-B"), [(30, 5, inner)])

    def test_the_master_record_shape(self):
        self.assertEqual(self.rows("NSTMASTR"), [("MST-KEY", 5, 0, 8, 0, 2), ("MST-AMT", 5, 88, 5, 0, 4),
                                                 ("MST-TBL", 5, 93, 2, 1, 5), ("MST-CODE", 10, 93, 2, 0, 6),
                                                 ("MST-TAIL", 5, 99, 4, 0, 7)])
        self.assertEqual(self.pfield("NSTPGM2", "MST-TAIL")[0][:2], (99, 4))
        page = self.page(query.cmd_layout, "NSTMASTR")
        self.assertIn("    99    4   5  MST-TAIL", page)
        self.assertIn("record length: 103 bytes", page)
        self.assertIn("- `COPY NSTADDR` at line 3: its bytes are counted in the offsets above; its items are NSTADDR's "
                      "own rows, not listed here (`layout NSTADDR`)", page)
        self.assertNotIn("MST-CITY", page)
        page = self.page(query.cmd_field, "MST-TAIL")
        self.assertIn("| NSTMASTR | copybook | 5 | X(04) |  | 99 | 4 |", page)

    def test_layout_says_where_the_nested_bytes_are(self):
        page = self.page(query.cmd_layout, "NSTOUTR")
        self.assertIn("    35    5   5  NST-AFTER", page)
        self.assertIn("- `COPY NSTINNR` at line 3: its bytes are counted in the offsets above; its items are NSTINNR's "
                      "own rows, not listed here (`layout NSTINNR`)\n", page)
        self.assertEqual(page.count("`COPY NSTINNR`"), 1)

    def test_a_nested_copybook_not_in_the_estate(self):
        self.assertEqual(self.rows("NSTGAP"), [("GAP-KEY", 5, 0, 4, 0, 1), ("GAP-AFTER", 5, 4, 2, 0, 3)])
        note = ("L2: COPY NSTMISS NOT FOUND - its bytes are not in this layout: an item after it in the same record sits "
                "further on by its length")
        self.assertEqual(self.warnings("NSTGAP"), [(note, 2)])
        self.assertEqual(self.status("NSTGAP"), "ok", "its own items are all there")
        self.assertEqual(self.copy_row("NSTGAP", "NSTMISS"), [(None,)])
        self.assertEqual(self.status("GAPPGM"), "partial", "the program carries its own NOT FOUND, as before")
        self.assertIn(f"- {note}\n", self.page(query.cmd_layout, "NSTGAP"))
        # coverage's kinds table says what such a row means and what closes it
        row = [ln for ln in self.page(query.cmd_coverage).splitlines() if ln.startswith("| layout_warning |")]
        self.assertEqual(len(row), 1)
        self.assertIn("a copybook's own layout without the text of a COPY in it - not found, skipped, a stub, "
                      "IBM-supplied", row[0])
        self.assertIn("for a COPY not found, fetch that copybook's library and build again", row[0])

    def test_an_osvs_01_copy_inside_a_copybook(self):
        # `01 OSV-REC COPY OSVIN.`: OSV-REC is this copybook's item, written on the COPY line, as long as OSVIN's record
        # (it was a 0-byte item before); OSVIN's items stay OSVIN's rows
        self.assertEqual(self.rows("OSVOUT"), [("OSV-REC", 1, 0, 6, 1, 2), ("OSV-TAIL", 1, 0, 3, 1, 3),
                                               ("OSV-T", 5, 0, 3, 0, 4)])
        self.assertEqual(self.copy_row("OSVOUT", "OSVIN"), [(self.member_id("OSVIN"),)])
        self.assertEqual(self.warnings("OSVOUT"), [])

    def test_an_own_item_under_a_group_the_nested_copybook_opens(self):
        self.assertEqual(self.rows("NSTHEAD"), [("HDR-ITEM", 10, 2, 3, 0, 2), ("HDR-NEXT", 5, 5, 1, 0, 3)])
        self.assertEqual(self.q("SELECT f.parent_id FROM field f JOIN member m ON m.id = f.member_id "
                                "WHERE m.name='NSTHEAD' ORDER BY f.id"), [(None,), (None,)])

    def test_a_copybook_copying_itself_and_one_copying_an_ibm_copybook(self):
        self.assertEqual(self.warnings("NSTSELF"), [(
            "L2: COPY NSTSELF skipped - recursive - its bytes are not in this layout: an item after it in the same "
            "record sits further on by its length", 2)])
        self.assertEqual(self.rows("NSTSELF"), [("SLF-A", 5, 0, 2, 0, 1), ("SLF-B", 5, 2, 2, 0, 3)])
        self.assertEqual(self.warnings("NSTIBM"), [(
            "L2: COPY DFHBMSCA is IBM-supplied (CICS), not in the estate - its bytes are not in this layout: an item "
            "after it in the same record sits further on by its length", 2)])


class TaggedCopybookInTheIndex(_Built):
    """F07 in the build: the PCB mask's rows, 88s, layout and the program's aliases."""

    def test_rows_88s_and_layout(self):
        self.assertEqual([r[:4] for r in self.rows("TAGPCB")], [
            (":PCB:-PCB", 1, 0, 20), (":PCB:-DBD-NAME", 5, 0, 8), (":PCB:-SEG-LEVEL", 5, 8, 2),
            (":PCB:-STATUS", 5, 10, 2), (":PCB:-PROC-OPT", 5, 12, 4), (":PCB:-KEY-LEN", 5, 16, 4),
            (":PCB:-KEY-ALT", 5, 16, 4)])
        self.assertEqual(self.q("SELECT c.name, c.line FROM cond88 c JOIN field f ON f.id = c.field_id "
                                "JOIN member m ON m.id = f.member_id WHERE m.name='TAGPCB' ORDER BY c.id"),
                         [(":PCB:-OK", 5), (":PCB:-END-OF-DB", 6)])
        page = self.page(query.cmd_layout, "TAGPCB")
        self.assertIn("## :PCB:-PCB  in `TAGPCB` (copybook)  line 1", page)
        self.assertIn("    10    2   5    :PCB:-STATUS", page)
        self.assertIn("TAGPGM (:PCB:-STATUS -> CLM-STATUS)", page)
        self.assertNotIn("NOT FOUND", page)

    def test_the_program_names_are_tied_back(self):
        self.assertEqual(self.q("SELECT a.copybook, a.orig_name, a.new_name, a.line FROM field_alias a "
                                "JOIN program p ON p.id = a.program_id WHERE p.program_id='TAGPGM' ORDER BY a.line"), [
            ("TAGPCB", ":PCB:-PCB", "CLM-PCB", 1), ("TAGPCB", ":PCB:-DBD-NAME", "CLM-DBD-NAME", 2),
            ("TAGPCB", ":PCB:-SEG-LEVEL", "CLM-SEG-LEVEL", 3), ("TAGPCB", ":PCB:-STATUS", "CLM-STATUS", 4),
            ("TAGPCB", ":PCB:-OK", "CLM-OK", 5), ("TAGPCB", ":PCB:-END-OF-DB", "CLM-END-OF-DB", 6),
            ("TAGPCB", ":PCB:-PROC-OPT", "CLM-PROC-OPT", 7), ("TAGPCB", ":PCB:-KEY-LEN", "CLM-KEY-LEN", 8),
            ("TAGPCB", ":PCB:-KEY-ALT", "CLM-KEY-ALT", 9)])
        status = self.q("SELECT f.id FROM field f JOIN member m ON m.id = f.member_id "
                        "WHERE m.name='TAGPCB' AND f.name=':PCB:-STATUS'")[0][0]
        self.assertEqual(self.pfield("TAGPGM", "CLM-STATUS"), [(10, 2, status)])
        page = self.page(query.cmd_field, ":PCB:-STATUS")
        self.assertIn("Also known as **CLM-STATUS** (COPY REPLACING)", page)
        self.assertIn("| TAGPCB | copybook | 5 | X(02) |  | 10 | 2 | :PCB:-PCB | 4 |", page)
        self.assertEqual(self.status("TAGPGM"), "ok")


class IbmSuppliedCopybooks(_Built):
    """F16 in the build: DFHAID, DFHBMSCA and CMQV are not in the estate and make nothing partial."""

    def test_the_programs_are_whole_and_the_rows_have_no_member(self):
        for prog, books in (("CICSPGM", ("DFHAID", "DFHBMSCA")), ("MQPGM", ("CMQV",))):
            self.assertEqual(self.status(prog), "ok", prog)
            for book in books:
                self.assertEqual(self.copy_row(prog, book), [(None,)])
            self.assertEqual(self.q("SELECT u.kind, u.detail FROM unresolved u JOIN member m ON m.id = u.member_id "
                                    "WHERE m.name=?", prog), [], prog)

    def test_coverage(self):
        cov = self.page(query.cmd_coverage)
        nf = cov.split("### Copybooks not found")[1].split("\n### ")[0]
        self.assertIn("| NSTMISS | 1 |", nf)
        for book in ("DFHAID", "DFHBMSCA", "CMQV"):
            self.assertNotIn(book, nf)
        ibm = cov.split("### IBM-supplied copybooks, not in the estate")[1].split("\n### ")[0]
        self.assertIn("| copybook | programs parsed only in part for it | programs copying it | supplied with |", ibm)
        self.assertIn("| DFHAID | 0 | 1 | CICS (SDFHCOB) |", ibm)
        self.assertIn("| DFHBMSCA | 0 | 1 | CICS (SDFHCOB) |", ibm)
        self.assertIn("| CMQV | 0 | 1 | MQ (SCSQCOBC) |", ibm)
        self.assertIn("nothing is to fetch, and no program is parsed only in part for one", ibm)
        self.assertNotIn("still say", ibm)
        part = cov.split("### Members parsed only in part")[1].split("\n### ")[0]
        self.assertNotIn("CICSPGM", part)
        self.assertNotIn("MQPGM", part)

    def test_program_and_copybook(self):
        page = self.page(query.cmd_program, "CICSPGM")
        self.assertIn("parse: ok", page)
        self.assertIn("| DFHAID | **IBM-supplied, not in the estate** - `COPY DFHAID` is read by the compile from CICS's "
                      "own library (SDFHCOB), not from the shop's copybook libraries: nothing is to fetch, and its items "
                      "are not in the index |", page)
        self.assertNotIn("no longer linked", page)
        self.assertNotIn("NOT FOUND", page)
        page = self.page(query.cmd_copybook, "DFHAID")
        self.assertIn("**IBM-supplied, not in the estate** - `COPY DFHAID` is read by the compile from CICS's own library "
                      "(SDFHCOB)", page)
        self.assertIn("### Programs including it (1)\nCICSPGM @CICSPGM:" + str(line_of(CICSPGM, "COPY DFHAID")), page)
        self.assertNotIn("NOT FOUND", page)
        self.assertNotIn("still `partial`", page)
        page = self.page(query.cmd_copybook, "CMQV")
        self.assertIn("MQ's own library (SCSQCOBC)", page)

    def test_recover_has_nothing_to_fetch_or_mark(self):
        conn = query.connect(self.db)
        try:
            missing = recover.missing_copybooks(conn)
            self.assertIn("NSTMISS", missing)
            for book in ("DFHAID", "DFHBMSCA", "CMQV"):
                self.assertNotIn(book, missing)
            self.assertEqual(recover.unlinked_ok_programs(conn), {})
            self.assertEqual(set(recover.supplied_copybooks(conn)), set(expand.IBM_COPYBOOKS))
            self.assertEqual(recover.system_copybooks(conn), expand.IBM_COPYBOOKS)
        finally:
            conn.close()


class TheStandInsFindNothingToDo(_Built):
    """On an index built by the batch holding these members the stand-ins of this week say nothing wrong: no program
    but GAPPGM (a nested copybook really missing) is partial, and recover marks, re-files and names nothing."""

    def test_partial_kind_and_the_chosen_row(self):
        conn = query.connect(self.db)
        try:
            for name in ("INCPGM3", "INCPGM1", "NSTPGM", "NSTPGM2", "TAGPGM", "CICSPGM", "MQPGM"):
                mid = self.member_id(name)
                self.assertIsNone(query.partial_kind(conn, mid), name)
                self.assertFalse(query.chose_a_copybook(conn, mid), name)
                self.assertEqual(query.parse_label(conn, mid, "ok"), "ok", name)
            self.assertEqual(query.partial_kind(conn, self.member_id("GAPPGM")), "partial")
        finally:
            conn.close()

    def test_recover_has_nothing_to_do(self):
        said = []
        stats = recover.run(self.db, log=said.append, report=os.path.join(self.td, "work", "recover.md"))
        for key in ("arrived", "misfiled", "refiled", "waiting", "marked"):
            self.assertEqual(stats.get(key), 0, (key, said))
        text = "\n".join(said)
        with open(os.path.join(self.td, "work", "recover.md"), encoding="utf-8") as fh:
            text += fh.read()
        for book in ("DFHAID", "DFHBMSCA", "CMQV"):
            self.assertNotIn(book, text)
        self.assertEqual(self.status("CICSPGM"), "ok")


# ===========================================================================
# F16 - a copy the shop keeps, one filed as another kind, the manifest's names
# ===========================================================================

DFHAID_COPY = ("       01  DFHAID.\n"
               "           02  DFHNULL   PIC X VALUE IS ' '.\n"
               "           02  DFHENTER  PIC X VALUE IS QUOTE.\n")


class TheShopKeepsItsOwnCopy(_Built):
    files = (("GC/PROD.GC.SRC/CICSPGM.cbl", CICSPGM), ("GC/PROD.GC.COPYLIB/DFHAID.cpy", DFHAID_COPY))

    def test_expanded_like_any_copybook(self):
        self.assertEqual(self.copy_row("CICSPGM", "DFHAID"), [(self.member_id("DFHAID"),)])
        self.assertEqual(len(self.pfield("CICSPGM", "DFHENTER")), 1)
        self.assertEqual(self.copy_row("CICSPGM", "DFHBMSCA"), [(None,)])
        self.assertEqual(self.status("CICSPGM"), "ok")
        cov = self.page(query.cmd_coverage)
        ibm = cov.split("### IBM-supplied copybooks, not in the estate")[1].split("\n### ")[0]
        self.assertIn("| DFHBMSCA | 0 | 1 | CICS (SDFHCOB) |", ibm)
        self.assertNotIn("DFHAID", ibm)
        page = self.page(query.cmd_program, "CICSPGM")
        self.assertIn("| DFHAID | DFHAID |", page)


class FiledAsAnotherKind(_Built):
    """A member named DFHAID that the build does not expand (a document in a hand-made folder): the name is the
    shop's then - NOT FOUND with the misfiled advice, never 'IBM-supplied'."""
    files = (("GC/PROD.GC.SRC/CICSPGM.cbl", CICSPGM), ("SHARED/downloads/DFHAID.txt", "SOME NOTES ABOUT THE KEYS\n"))

    def test_not_found_as_before(self):
        self.assertEqual(self.q("SELECT kind FROM member WHERE name='DFHAID'"), [("doc",)])
        self.assertEqual(self.status("CICSPGM"), "partial")
        cov = self.page(query.cmd_coverage)
        nf = cov.split("### Copybooks not found")[1].split("\n### ")[0]
        self.assertIn("| DFHAID | 1 | yes: downloads, filed as doc", nf)
        ibm = cov.split("### IBM-supplied copybooks, not in the estate")[1].split("\n### ")[0]
        self.assertNotIn("DFHAID", ibm)
        self.assertIn("DFHBMSCA", ibm)
        page = self.page(query.cmd_program, "CICSPGM")
        self.assertIn("| DFHAID | **NOT FOUND** - a member with this name exists", page)


VNDPGM = program("VNDPGM", ["01  WS-A   PIC X.", "    COPY VNDRBOOK."], ["0000-MAIN.", "    GOBACK."])


VNDOUTR = ("       05  VND-KEY                PIC X(04).\n"
           "           COPY VNDRBOOK.\n"
           "       05  VND-AFTER              PIC X(02).\n")


class TheManifestAddsNames(_Built):
    files = (("GC/PROD.GC.SRC/VNDPGM.cbl", VNDPGM), ("GC/PROD.GC.COPYLIB/VNDOUTR.cpy", VNDOUTR))
    manifest = {"system_includes": ["vndrbook", "NOT A NAME!", "VNDRBOOK"]}

    def test_a_copybook_copying_one(self):
        self.assertEqual(self.warnings("VNDOUTR"), [(
            "L2: COPY VNDRBOOK is supplied by a product library (the manifest's system_includes), not in the estate - "
            "its bytes are not in this layout: an item after it in the same record sits further on by its length", 2)])
        self.assertIn("- L2: COPY VNDRBOOK is supplied by a product library", self.page(query.cmd_layout, "VNDOUTR"))

    def test_the_name_is_supplied_and_recorded(self):
        self.assertEqual(self.status("VNDPGM"), "ok")
        self.assertEqual(self.q("SELECT system_includes FROM build_run ORDER BY id DESC LIMIT 1"), [('["VNDRBOOK"]',)])
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.system_copybooks(conn)["VNDRBOOK"], expand.MANIFEST_PRODUCT)
            self.assertNotIn("VNDRBOOK", recover.missing_copybooks(conn))
        finally:
            conn.close()
        cov = self.page(query.cmd_coverage)
        self.assertIn("| VNDRBOOK | 0 | 1 | the manifest's system_includes |", cov)
        page = self.page(query.cmd_program, "VNDPGM")
        self.assertIn("| VNDRBOOK | **supplied by a product library (the manifest's system_includes), not in the estate** "
                      "- `COPY VNDRBOOK` is read by the compile from a product library the manifest names", page)

    def test_the_loader(self):
        mem = sqlite3.connect(":memory:")
        self.addCleanup(mem.close)
        ctx = build.Ctx(mem, quiet=True)
        path = os.path.join(self.td, "m2.json")
        for man, want in (({"system_includes": ["ABC", "abc", "", 7, "TOO-LONG-NAME-X"]}, ["7", "ABC"]),
                          ({"system_includes": "ABC"}, []), ({}, []), (["not", "a", "dict"], [])):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(man, fh)
            build.load_system_includes(ctx, path)
            self.assertEqual(ctx.manifest_includes, want, man)
            self.assertEqual(set(ctx.system_includes), set(expand.IBM_COPYBOOKS) | set(want), man)
        build.load_system_includes(ctx, None)
        self.assertEqual((ctx.manifest_includes, ctx.system_includes), ([], expand.IBM_COPYBOOKS))

    def test_sources_json_reaches_the_manifest(self):
        src = os.path.join(self.td, "sources.json")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump({"local_root": self.root, "sources": [], "system_includes": ["vndrbook", " "]}, fh)
        cfg = fetch.load_config(src)
        self.assertEqual(cfg["system_includes"], ["VNDRBOOK"])
        man = fetch.write_manifest(cfg, os.path.join(self.td, "gen-manifest.json"))
        self.assertEqual(man["system_includes"], ["VNDRBOOK"])
        with open(src, "w", encoding="utf-8") as fh:
            json.dump({"local_root": self.root, "sources": []}, fh)
        cfg = fetch.load_config(src)
        self.assertNotIn("system_includes", cfg)
        self.assertNotIn("system_includes", fetch.write_manifest(cfg, os.path.join(self.td, "gen-manifest.json")))


# ===========================================================================
# an index built before the item, and an incremental build
# ===========================================================================

def age_index(db):
    """Give the index the shape a build before ROADMAP re-parse item 27 left: every COPY of an IBM copybook a NOT
    FOUND note of the program's own and the program `partial`; a copybook's own COPY rows unresolved, no nested
    'layout_warning', the items after a nested COPY at the offsets that leave its bytes out; no system_includes
    recorded; the last build recorded as an older toolkit's, as his index is before the re-parse night."""
    conn = sqlite3.connect(db)
    try:
        for (mid, book, line) in conn.execute(
                "SELECT c.member_id, c.copybook, c.line FROM copy_use c JOIN member m ON m.id = c.member_id "
                "WHERE m.kind = 'cobol' AND c.resolved_member_id IS NULL AND c.copybook IN ('DFHAID', 'DFHBMSCA', 'CMQV')"
                ).fetchall():
            conn.execute("INSERT INTO unresolved(member_id, kind, detail, line) VALUES(?, 'expand', ?, NULL)",
                         (mid, f"L{line}: COPY {book} NOT FOUND - fields/code from it are missing from this program's facts"))
            conn.execute("UPDATE member SET parse_status='partial' WHERE id=?", (mid,))
        conn.execute("UPDATE copy_use SET resolved_member_id=NULL WHERE member_id IN (SELECT id FROM member WHERE kind='copybook')")
        conn.execute("DELETE FROM unresolved WHERE kind='layout_warning' AND detail LIKE '%its bytes are not in this layout%'")
        conn.execute("UPDATE field SET offset=10 WHERE name='NST-AFTER'")
        conn.execute("UPDATE field SET length=0, is_group=0 WHERE name='NST-ADDRESS'")
        conn.execute("UPDATE build_run SET system_includes=NULL, fingerprint='an older toolkit'")
        conn.commit()
    finally:
        conn.close()


class AnIndexBuiltBeforeTheItem(_Built):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        age_index(cls.db)

    def test_coverage_program_and_copybook_say_the_old_state_as_it_is(self):
        cov = self.page(query.cmd_coverage)
        nf = cov.split("### Copybooks not found")[1].split("\n### ")[0]
        self.assertNotIn("DFHAID", nf, "no library to fetch, on this index either")
        ibm = cov.split("### IBM-supplied copybooks, not in the estate")[1].split("\n### ")[0]
        self.assertIn("| DFHAID | 1 | 1 | CICS (SDFHCOB) |", ibm)
        self.assertIn("| CMQV | 1 | 1 | MQ (SCSQCOBC) |", ibm)
        self.assertIn("> 2 programs copying them still say NOT FOUND for one and are in 'Members parsed only in part' "
                      "above: the build that made this index counted it as not found and marked them partial for it; the "
                      "next build parses every program again (the toolkit changed) and does not.", ibm)
        page = self.page(query.cmd_program, "CICSPGM")
        self.assertIn("parse: partial", page)
        self.assertIn("its items are not in the index; the build that made this index counted it as not found and marked "
                      "the program partial for it; the next build parses every program again (the toolkit changed) and "
                      "does not |", page)
        page = self.page(query.cmd_copybook, "DFHAID")
        self.assertIn("1 of the programs below is still `partial` for it (CICSPGM): the build that made this index "
                      "counted it as not found and marked the program partial for it", page)
        conn = query.connect(self.db)
        try:
            self.assertEqual(query.partial_kind(conn, self.member_id("CICSPGM")), "partial",
                             "the stand-in reads the index as it is")
            self.assertNotIn("DFHAID", recover.missing_copybooks(conn))
        finally:
            conn.close()

    def test_layout_says_the_nested_bytes_are_not_counted(self):
        page = self.page(query.cmd_layout, "NSTOUTR")
        self.assertIn("    10    5   5  NST-AFTER", page)
        self.assertIn("- `COPY NSTINNR` at line 3: its bytes are NOT counted in the offsets above, so an item after it in "
                      "the same record sits further on by NSTINNR's length. An index built before ROADMAP re-parse item 27 "
                      "leaves every nested copybook out of a copybook's own layout; `layout RECORD --program PGM` gives a "
                      "program's view, which counts it", page)

    def test_the_next_build_makes_them_whole(self):
        td = tempfile.mkdtemp()
        try:
            db = os.path.join(td, "t.db")
            shutil.copy(self.db, db)
            run_build(self.root, db)
            conn = query.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT parse_status FROM member WHERE name='CICSPGM'").fetchone()[0], "ok")
                self.assertEqual(conn.execute("SELECT f.offset FROM field f JOIN member m ON m.id = f.member_id "
                                              "WHERE m.name='NSTOUTR' AND f.name='NST-AFTER'").fetchone()[0], 35)
                cov = query.cmd_coverage(conn)
                self.assertIn("| DFHAID | 0 | 1 | CICS (SDFHCOB) |", cov)
                self.assertNotIn("still say NOT FOUND", cov)
            finally:
                conn.close()
        finally:
            shutil.rmtree(td, ignore_errors=True)


class ANestedCopybookChanges(unittest.TestCase):
    """An incremental build parses again a copybook whose nested copybook changed, arrived or went: its own layout
    counts the new bytes (copiers_to_parse takes every member copying the name, copybooks with programs)."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        self.db = os.path.join(self.td, "t.db")
        write_estate(self.root, (("GC/PROD.GC.SRC/NSTPGM.cbl", NSTPGM), ("GC/PROD.GC.COPYLIB/NSTOUTR.cpy", NSTOUTR),
                                 ("GC/PROD.GC.COPYLIB/NSTINNR.cpy", NSTINNR)))
        run_build(self.root, self.db, "--rebuild")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def after(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT f.offset FROM field f JOIN member m ON m.id = f.member_id "
                                "WHERE m.name='NSTOUTR' AND f.name='NST-AFTER'").fetchone()[0]
        finally:
            conn.close()

    def test_grows_goes_and_comes_back(self):
        self.assertEqual(self.after(), 35)
        write_estate(self.root, (("GC/PROD.GC.COPYLIB/NSTINNR.cpy",
                                  NSTINNR + "           10  NST-INNER-C            PIC X(05).\n"),))
        run_build(self.root, self.db)
        self.assertEqual(self.after(), 40)
        os.remove(os.path.join(self.root, "GC", "PROD.GC.COPYLIB", "NSTINNR.cpy"))
        run_build(self.root, self.db)
        self.assertEqual(self.after(), 10)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT u.detail FROM unresolved u JOIN member m ON m.id = u.member_id "
                                          "WHERE m.name='NSTOUTR' AND u.kind='layout_warning'").fetchall(),
                             [("L3: COPY NSTINNR NOT FOUND - its bytes are not in this layout: an item after it in the "
                               "same record sits further on by its length",)])
        finally:
            conn.close()
        write_estate(self.root, (("GC/PROD.GC.COPYLIB/NSTINNR.cpy", NSTINNR),))
        run_build(self.root, self.db)
        self.assertEqual(self.after(), 35)


# ===========================================================================
# the reproductions
# ===========================================================================

class TheReproductions(unittest.TestCase):
    """tools/synth/repro/F05, F06, F07, F16 built in process: every expect.json truth holds, the SQL ones and the
    reports' (verify.py says FIXED)."""

    def check(self, rid):
        src = os.path.join(REPRO, rid)
        td = tempfile.mkdtemp()
        try:
            shutil.copytree(os.path.join(src, "estate"), os.path.join(td, "estate"))
            db = os.path.join(td, "t.db")
            run_build(os.path.join(td, "estate"), db, "--rebuild")
            with open(os.path.join(src, "expect.json"), encoding="utf-8") as fh:
                spec = json.load(fh)
            conn = sqlite3.connect(db)
            try:
                for k, ck in enumerate(spec["checks"], 1):
                    if "sql" in ck:
                        got = [list(r) if len(r) > 1 else r[0] for r in conn.execute(ck["sql"]).fetchall()]
                        got = got[0] if len(got) == 1 and ck.get("scalar", True) else got
                        self.assertEqual(got, ck["truth"], (rid, ck["sql"]))
                    elif "query" in ck:
                        out = os.path.join(td, f"q{k}.md")
                        with contextlib.redirect_stdout(io.StringIO()):
                            query.main(["--db", db, "--out", out, *ck["query"]])
                        with open(out, encoding="utf-8") as fh:
                            text = fh.read()
                        self.assertIn(ck["truth_has"], text, (rid, ck["query"]))
                        if ck.get("symptom_has") and ck["symptom_has"] != ck["truth_has"]:
                            self.assertNotIn(ck["symptom_has"], text, (rid, ck["query"]))
            finally:
                conn.close()
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_f05_include_over_three_lines(self):
        self.check("F05-include-three-lines")

    def test_f06_nested_copy_offsets(self):
        self.check("F06-nested-copy-offsets")

    def test_f07_tagged_copybook(self):
        self.check("F07-tag-copybook-no-rows")

    def test_f16_dfhaid(self):
        self.check("F16-dfhaid-missing")

    def test_verify_calls_a_control_fixed(self):
        # a check whose truth and symptom are the same (the program's own view, right before the fix too) printed
        # REPRODUCED for ever; verify.py runs the toolkit as the owner does, in a process of its own
        sys.path.insert(0, os.path.join(REPRO))
        import verify                                                   # noqa: E402
        out = tempfile.mkdtemp()
        try:
            got = verify.verify(os.path.join(REPRO, "F06-nested-copy-offsets"), out)
        finally:
            shutil.rmtree(out, ignore_errors=True)
        self.assertEqual([v for _l, v, _d in got], ["FIXED", "FIXED", "FIXED"], got)
        self.assertIn("a control", got[1][2])


class TheDocsSayIt(unittest.TestCase):
    """ROADMAP 27, LESSONS 214 and 215, README's Copybook and Expansion rows, the manifest example, the four
    reproductions' READMEs and the findings report say what changed."""

    def read(self, *path):
        with open(os.path.join(ROOT, *path), encoding="utf-8") as fh:
            return fh.read()

    def test_the_docs(self):
        roadmap = " ".join(self.read("ROADMAP.md").split())
        self.assertIn("27. **Delivered in the batch - an INCLUDE over three lines, a copybook's own layout with its nested "
                      "COPY in place, names with a :TAG:, the copybooks IBM supplies.**", roadmap)
        self.assertIn("310 findings (315 facts) before, 62 (65) after, 11,053 facts matched before and 11,358 after", roadmap)
        lessons = self.read("LESSONS.md")
        self.assertIn("| 214 | Not seen on his estate - the synthetic estate's comparison", lessons)
        self.assertIn("Rule for me: a copybook is read the way the compiler reads it", lessons)
        self.assertIn("| 215 | Not seen on his estate - found while writing row 214's tests", lessons)
        self.assertIn("Rule for me: an optional group behind a greedy blank never runs", lessons)
        readme = self.read("README.md")
        self.assertIn("a copybook that COPYs another is laid out with the nested copybook in place", readme)
        self.assertIn("is 'IBM-supplied, not in the estate' - never a gap", readme)
        self.assertIn('"system_includes": []', self.read("manifest.example.json"))
        for rid in ("F05-include-three-lines", "F06-nested-copy-offsets", "F07-tag-copybook-no-rows", "F16-dfhaid-missing"):
            self.assertIn("Fixed by ROADMAP re-parse item 27 (LESSONS 214)", self.read("tools", "synth", "repro", rid,
                                                                                     "README.md"), rid)
        report = self.read("docs", "SYNTH-findings-2026-09-25.md")
        self.assertIn("verify.py reports FIXED for every check of F05, F06, F07 and F16", report)
        self.assertIn("LESSONS 215", self.read("atlas", "copybook.py"))


if __name__ == "__main__":
    unittest.main()
