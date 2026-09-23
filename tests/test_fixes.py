"""
Regression tests for corrections found in review. Each test names the wrong
answer it prevents. Add a case here BEFORE fixing a newly found mistake.
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import classify, cobol, diag, docs, expand, ims, jcl, reader, verify_citations  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def _load(name):
    return reader.load(os.path.join(FIX, name))


class ErrorCodeTracing(unittest.TestCase):
    """Error code set in one place, tested/displayed in another."""

    @classmethod
    def setUpClass(cls):
        text, data, enc = _load("ERRPGM.cbl")
        cls.f = cobol.parse_program(text, data, enc)
        cls.lits = {(l, c, fld) for (l, c, fld, _ln) in cls.f.literal_refs}
        cls.refs = {(n, m, s) for (n, m, s, _ln) in cls.f.field_refs}

    def test_identifier_containing_call_is_not_a_call(self):
        # `MOVE WS-CALL-FLAG TO WS-KEY` used to yield a dynamic CALL to 'TO'.
        self.assertEqual({c.target for c in self.f.calls}, {"ERRLOG"})

    def test_exec_sql_include_is_a_copy(self):
        self.assertIn("POLDCL", [c[0] for c in self.f.copies])

    def test_assign_with_device_prefix_yields_ddname(self):
        self.assertEqual(self.f.files[0].assign_dd, "CLMFILE")

    def test_select_into_hostvar_is_not_a_table(self):
        sel = [s for s in self.f.sql if s.stmt_type == "SELECT"]
        self.assertEqual(sel[0].tables, ["POLICY_TBL"])
        self.assertNotIn("WS-AMT", sel[0].tables)

    def test_for_update_of_is_not_a_table(self):
        dec = [s for s in self.f.sql if s.stmt_type == "DECLARE"]
        self.assertEqual(dec[0].tables, ["POLICY_TBL"])

    def test_insert_into_is_a_table(self):
        ins = [s for s in self.f.sql if s.stmt_type == "INSERT"]
        self.assertEqual(ins[0].tables, ["AUDIT_TBL"])

    def test_both_moves_inside_compound_if_are_captured(self):
        self.assertIn(("E001", "move_to", "WS-ERR-CD"), self.lits)
        self.assertIn(("E002", "move_to", "WS-ERR-CD"), self.lits)
        lines = sorted(ln for (l, c, _f, ln) in self.f.literal_refs if c == "move_to" and l in ("E001", "E002"))
        self.assertEqual(lines, [30, 32])

    def test_abbreviated_or_comparison_attaches_both_literals(self):
        self.assertIn(("E001", "compare", "WS-ERR-CD"), self.lits)
        self.assertIn(("E002", "compare", "WS-ERR-CD"), self.lits)

    def test_when_literals_attach_to_evaluate_subject(self):
        self.assertIn(("E000", "when", "WS-ERR-CD"), self.lits)
        self.assertIn(("E900", "when", "WS-ERR-CD"), self.lits)
        self.assertIn(("E999", "when", "WS-ERR-CD"), self.lits)

    def test_value_and_88_literals_are_sources(self):
        self.assertIn(("E999", "value", "WS-ERR-CD"), self.lits)
        self.assertIn(("E000", "cond88", "WS-ERR-CD/WS-ERR-NONE"), self.lits)
        self.assertIn(("E900", "cond88", "WS-ERR-CD/WS-ERR-FATAL"), self.lits)

    def test_display_literal_and_display_field(self):
        self.assertIn(("ERROR: ", "display", None), self.lits)
        self.assertIn(("WS-ERR-CD", "display", "DISPLAY"), self.refs)

    def test_write_modes(self):
        self.assertIn(("WS-ERR-CD", "write", "MOVE"), self.refs)
        self.assertIn(("LK-RC", "write", "MOVE"), self.refs)
        self.assertIn(("WS-AMT", "write", "EXEC-SQL"), self.refs)       # SELECT ... INTO :WS-AMT
        self.assertIn(("WS-KEY", "read", "EXEC-SQL"), self.refs)
        self.assertIn(("WS-ERR-CD", "write", "CALL-USING"), self.refs)  # BY REFERENCE default

    def test_linkage_positional(self):
        self.assertEqual(self.f.linkage_using, ["LK-RC"])
        call = [c for c in self.f.calls if c.target == "ERRLOG"][0]
        self.assertEqual(call.using_args, ["WS-ERR-CD", "WS-AMT"])


class QuotedCopyNames(unittest.TestCase):
    """COPY 'NAME' is legal COBOL and some shops write every COPY that way
    (LESSONS 173). The parser saw only bare names: no copy_use row, so the
    copybook was never resolved, never expanded and never reported missing."""

    def test_the_parser_records_a_quoted_copy_like_a_bare_one(self):
        f = cobol.parse_program("\n".join([
            "       IDENTIFICATION DIVISION.",
            "       PROGRAM-ID. QPGM.",
            "       DATA DIVISION.",
            "       WORKING-STORAGE SECTION.",
            "       01  WS-A.",
            "           COPY 'QREC1'.",
            "       01  WS-B.",
            '           COPY "QREC2" OF \'QLIB\'.',
            "       01  WS-C.",
            "           COPY 'QREC3' REPLACING ==:P:== BY ==WS==.",
            "       01  WS-D.",
            "           COPY QREC4.",
            "       PROCEDURE DIVISION.",
            "           DISPLAY 'COPY NOPE FAILED'.",
            "           GOBACK.",
        ]) + "\n")
        by = {c[0]: c for c in f.copies}
        self.assertEqual(set(by), {"QREC1", "QREC2", "QREC3", "QREC4"}, f.copies)
        self.assertEqual(by["QREC2"][1], "QLIB")
        self.assertIn("REPLACING", by["QREC3"][2] or "")
        self.assertTrue(any(u[0] == "copy_replacing" and "QREC3" in u[1] for u in f.unresolved), f.unresolved)

    def test_national_characters_suppress_and_a_literal_that_swallows_the_sentence(self):
        # found in review: the parser's data-name class has no @ # $, so COPY '#REC'. was still invisible and
        # COPY A@REC. was truncated to A; SUPPRESS before REPLACING lost the REPLACING; and a rejected match
        # inside a literal, with a REPLACING tail running to the end of the sentence, hid a real COPY after it
        f = cobol.parse_program("\n".join([
            "       IDENTIFICATION DIVISION.",
            "       PROGRAM-ID. QPGM.",
            "       DATA DIVISION.",
            "       WORKING-STORAGE SECTION.",
            "           COPY '#REC1'.",
            "           COPY $REC2.",
            "           COPY 'A@REC3' OF '#LIB'.",
            "           COPY REC4 SUPPRESS REPLACING ==A== BY ==B==.",
            "       PROCEDURE DIVISION.",
            "           DISPLAY 'COPY X REPLACING' COPY 'REC5'.",
            "           DISPLAY 'COPY Y REPLACING' COPY REC6.",
            "           GOBACK.",
        ]) + "\n")
        by = {c[0]: c for c in f.copies}
        self.assertEqual(set(by), {"#REC1", "$REC2", "A@REC3", "REC4", "REC5", "REC6"}, f.copies)
        self.assertEqual(by["A@REC3"][1], "#LIB")
        self.assertIn("REPLACING", by["REC4"][2] or "")

    def test_an_unclosed_quote_is_not_a_copy_name(self):
        f = cobol.parse_program("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. QPGM.\n       DATA DIVISION.\n"
                                "       WORKING-STORAGE SECTION.\n           COPY 'QREC1.\n")
        self.assertEqual([c[0] for c in f.copies], [], f.copies)


class GivingTargetIsAWrite(unittest.TestCase):
    """`ADD WS-I 1 GIVING WS-Y` recorded WS-Y as READ (LESSONS 174): _ARITH
    wanted TO/FROM/BY/INTO before it looked for GIVING, and a fragment without
    one fell to the every-name-is-a-read branch. `field WS-Y` then never showed
    the statement as a setter."""

    def test_add_giving_without_to_is_a_write(self):
        f = cobol.parse_program("\n".join([
            "       IDENTIFICATION DIVISION.",
            "       PROGRAM-ID. GIVPGM.",
            "       DATA DIVISION.",
            "       WORKING-STORAGE SECTION.",
            "       01  WS-I                 PIC S9(04) COMP.",
            "       01  WS-J                 PIC S9(04) COMP.",
            "       01  WS-Y                 PIC S9(04) COMP.",
            "       01  WS-Z                 PIC S9(04) COMP.",
            "       PROCEDURE DIVISION.",
            "           ADD WS-I 1 GIVING WS-Y.",
            "           ADD WS-I WS-J GIVING WS-Z ROUNDED.",
            "           ADD WS-I TO WS-J GIVING WS-Y.",
            "           GOBACK.",
        ]) + "\n")
        refs = set(f.field_refs)
        self.assertIn(("WS-Y", "write", "ADD", 10), refs)
        self.assertNotIn(("WS-Y", "read", "ADD", 10), refs)
        self.assertIn(("WS-I", "read", "ADD", 10), refs)
        self.assertIn(("WS-Z", "write", "ADD", 11), refs)
        self.assertNotIn(("WS-Z", "read", "ADD", 11), refs)
        self.assertIn(("WS-Y", "write", "ADD", 12), refs)        # `TO b GIVING c` reads b, writes c
        self.assertIn(("WS-J", "read", "ADD", 12), refs)
        self.assertNotIn(("WS-J", "write", "ADD", 12), refs)


class JclDirection(unittest.TestCase):
    JCL = "\n".join([
        "//T1       JOB (A),'X'",
        "//S1       EXEC PGM=PGMX",
        "//OUT1     DD DSN=A.B(+1),DISP=(NEW,CATLG)",
        "//OLD1     DD DSN=A.C,DISP=OLD",
        "//SHR1     DD DSN=A.E,DISP=SHR",
        "//S2       EXEC PGM=SORT",
        "//SORTIN   DD DSN=A.D,DISP=SHR",
        "//SORTOUT  DD DSN=A.F,DISP=(NEW,CATLG)",
        "//SYSIN    DD *",
        "  SORT FIELDS=(1,12,CH,A,25,2,CH,D)",
        "  INCLUDE COND=(25,2,CH,EQ,C'AC')",
        "  OUTREC FIELDS=(1,50,60,10)",
        "/*",
        "//",
    ])

    def setUp(self):
        self.f = jcl.parse_jcl(self.JCL)
        self.dd = {d.dd_name: d for s in self.f.steps for d in s.dds}

    def test_disp_old_is_not_direction(self):
        self.assertEqual((self.dd["OLD1"].mode, self.dd["OLD1"].mode_source), ("unknown", "undetermined"))
        self.assertEqual(self.dd["SHR1"].mode, "unknown")

    def test_gdg_plus_one_is_output(self):
        self.assertEqual((self.dd["OUT1"].mode, self.dd["OUT1"].mode_source), ("output", "gdg_relative"))
        self.assertEqual(self.dd["OUT1"].dsn_resolved, "A.B")

    def test_utility_dd_convention(self):
        self.assertEqual(self.dd["SORTIN"].mode_source, "dd_convention")
        self.assertEqual(self.dd["SORTIN"].mode, "input")
        self.assertEqual(self.dd["SORTOUT"].mode, "output")

    def test_sort_card_byte_positions(self):
        ctl = self.dd["SYSIN"].sysin_text
        got = {(k, p, ln, f) for (k, p, ln, f, _raw) in jcl.sort_card_fields(ctl)}
        self.assertIn(("SORT", 1, 12, "CH"), got)
        self.assertIn(("SORT", 25, 2, "CH"), got)
        self.assertIn(("INCLUDE", 25, 2, "CH"), got)
        self.assertIn(("OUTREC", 1, 50, None), got)
        self.assertIn(("OUTREC", 60, 10, None), got)


class ImsParsing(unittest.TestCase):
    DBD = "\n".join([
        "POLDBD   DBD   NAME=POLDBD,ACCESS=(HDAM,OSAM),RMNAME=(DFSHDC40,4,100)",
        "         DATASET DD1=POLDD,DEVICE=3390",
        "         SEGM  NAME=POLROOT,PARENT=0,BYTES=200",
        "         FIELD NAME=(POLNO,SEQ,U),BYTES=12,START=1",
        "         FIELD NAME=POLSTAT,BYTES=2,START=13",
        "         SEGM  NAME=COVERAGE,PARENT=POLROOT,BYTES=(120,60)",
        "         FIELD NAME=(COVCD,SEQ),BYTES=4,START=1",
        "         DBDGEN",
        "         FINISH",
        "         END",
    ])
    PSB = "\n".join([
        "         PCB   TYPE=DB,DBDNAME=POLDBD,PROCOPT=A,".ljust(71) + "X",
        "               KEYLEN=24",
        "         SENSEG NAME=POLROOT,PARENT=0",
        "         SENSEG NAME=COVERAGE,PARENT=POLROOT",
        "         PCB   TYPE=DB,DBDNAME=CLMDBD,PROCOPT=G",
        "         SENSEG NAME=CLMROOT,PARENT=0",
        "         PSBGEN LANG=COBOL,PSBNAME=POLPSB,CMPAT=YES",
        "         END",
    ])

    def test_dbd_with_label_in_column_1(self):
        f = ims.parse_dbd(self.DBD)
        self.assertEqual((f.name, f.access), ("POLDBD", "HDAM"))
        self.assertEqual([s.name for s in f.segments], ["POLROOT", "COVERAGE"])
        self.assertEqual(f.segments[0].seq_field, "POLNO")
        self.assertEqual(f.segments[1].parent, "POLROOT")
        self.assertTrue(any("VARIABLE" in w for w in f.warnings))

    def test_psb_column72_continuation_and_cmpat(self):
        f = ims.parse_psb(self.PSB)
        self.assertEqual(f.name, "POLPSB")
        self.assertEqual(len(f.pcbs), 2)
        self.assertEqual(f.pcbs[0].keylen, 24)             # came via col-72 continuation
        self.assertEqual([s[0] for s in f.pcbs[0].sensegs], ["POLROOT", "COVERAGE"])
        self.assertTrue(f.io_pcb_first)
        pos = ims.program_positions(f)
        self.assertEqual([(p, lbl) for p, lbl, _ in pos][:1], [(1, "IO-PCB")])
        self.assertEqual(pos[1][2].dbd_name, "POLDBD")
        self.assertEqual(pos[2][2].dbd_name, "CLMDBD")

    def test_psb_without_cmpat_is_undetermined(self):
        f = ims.parse_psb(self.PSB.replace(",CMPAT=YES", ""))
        self.assertIsNone(f.io_pcb_first)
        self.assertTrue(any("region type" in w for w in f.warnings))
        self.assertEqual(ims.program_positions(f, "BMP")[0][1], "IO-PCB")
        self.assertEqual(ims.program_positions(f, "DLI")[0][2].dbd_name, "POLDBD")

    def test_classify_hlasm_label_and_level_49(self):
        self.assertEqual(classify.classify("x/POLDBD", self.DBD)[0], "dbd")
        self.assertEqual(classify.classify("x/POLPSB", self.PSB)[0], "psb")
        dclgen = "       01  DCLPOLICY.\n           10 POL-NO PIC X(12).\n           49 POL-NAME-TEXT PIC X(30).\n"
        self.assertEqual(classify.classify("x/POLDCL", dclgen)[0], "copybook")


class CopybookExpansion(unittest.TestCase):

    def _lines(self, text):
        lines, _ = reader.read_cobol_lines(text, fixed=True)
        return lines

    def test_replacing_and_line_map(self):
        prog = self._lines("\n".join([
            "       01  WS-REC.",
            "           COPY REC1 REPLACING ==:X:== BY ==WS==.",
            "       01  WS-AFTER PIC X.",
        ]))
        cb = self._lines("\n".join([
            "           05  :X:-FIELD-A   PIC X(5).",
            "           05  :X:-FIELD-B   PIC 9(3).",
        ]))
        res = expand.expand(prog, 1, lambda name, lib: (2, cb, None) if name == "REC1" else None)
        text = expand.expanded_text(res)
        self.assertIn("WS-FIELD-A", text)
        self.assertNotIn(":X:-FIELD-A", text.replace("*           COPY", ""))
        exp_line = next(l.no for l in res.lines if "WS-FIELD-B" in l.code)
        self.assertEqual(res.origin(exp_line)[:2], (2, 2))     # copybook member 2, its line 2
        after = next(l.no for l in res.lines if "WS-AFTER" in l.code)
        self.assertEqual(res.origin(after)[:2], (1, 3))        # program line 3, despite the shift
        self.assertEqual(res.copies[0][:2], ("REC1", None))
        self.assertEqual(res.copies[0][4], 2)

    def test_a_copybook_name_in_quotes_is_expanded_like_a_bare_one(self):
        # his shop writes COPY 'NAME'. (LESSONS 173): the name is a literal and the literal mask blanked it,
        # so the whole index was blind to those copybooks - no copy_use row, no fields, no warning
        cb = self._lines("\n".join([
            "           05  :X:-FIELD-A   PIC X(5).",
            "           05  :X:-FIELD-B   PIC 9(3).",
        ]))
        resolver = lambda name, lib: (2, cb, None) if name == "REC1" else None  # noqa: E731
        for stmt in ("           COPY 'REC1'.", '           COPY "REC1".', "           COPY 'REC1' OF 'MYLIB'.",
                     "           COPY 'REC1' REPLACING ==:X:== BY ==WS==."):
            res = expand.expand(self._lines("       01  WS-REC.\n" + stmt + "\n       01  WS-AFTER PIC X.\n"), 1, resolver)
            text = expand.expanded_text(res)
            self.assertIn("FIELD-A", text, stmt)
            self.assertEqual(res.copies[0][0], "REC1", stmt)
            self.assertEqual(res.copies[0][4], 2, stmt)
            self.assertFalse(res.warnings, (stmt, res.warnings))
        self.assertTrue((res.copies[0][2] or "").startswith("==:X:== BY ==WS=="), res.copies[0])
        self.assertIn("WS-FIELD-A", text)
        res = expand.expand(self._lines("           COPY 'REC1' OF 'MYLIB'.\n"), 1, resolver)
        self.assertEqual(res.copies[0][1], "MYLIB")
        # a COPY inside a literal is still text
        res = expand.expand(self._lines("           DISPLAY 'COPY REC1 FAILED'.\n           DISPLAY 'X' COPY 'REC1'.\n"), 1, resolver)
        self.assertEqual([c[0] for c in res.copies], ["REC1"])
        self.assertEqual(res.copies[0][3], 2, "the second line, not the DISPLAY")

    def test_missing_copybook_is_a_warning_not_silence(self):
        prog = self._lines("           COPY NOPE.\n")
        res = expand.expand(prog, 1, lambda n, l: None)
        self.assertTrue(any("NOT FOUND" in w for w in res.warnings))
        self.assertEqual(res.copies[0][4], None)

    def test_recursive_copy_is_stopped(self):
        cb = self._lines("           COPY LOOP.\n")
        res = expand.expand(cb, 1, lambda n, l: (1, cb, None))
        self.assertTrue(any("recursive" in w for w in res.warnings))


class OsvsLevelCopy(unittest.TestCase):
    """OS/VS "01 X COPY Y." (also 77, FD, SD): the compiler puts X in place of
    the library's own 01 name. The whole line used to become a comment, so X
    vanished and every MOVE / READ INTO / DL/I I/O area naming it pointed at
    nothing, while the library's own 01 name took its place (LESSONS 177)."""

    FULL = ("       01  LIB-REC.\n"
            "           05  AAAA-KEY          PIC X(10).\n"
            "           05  AAAA-STAT         PIC X(02).\n")
    FRAG = ("           05  AAAA-KEY          PIC X(10).\n"
            "           05  AAAA-STAT         PIC X(02).\n")

    def _lines(self, text):
        lines, _ = reader.read_cobol_lines(text, fixed=True)
        return lines

    def _expand(self, prog, lib, name="ABCDE"):
        cb = self._lines(lib)
        return expand.expand(self._lines(prog), 1, lambda n, l: (2, cb, None) if n == name else None)

    def _tree(self, res):
        from atlas import copybook
        roots, _w = copybook.parse_data_division(reader.join_cobol_continuations(res.lines))
        return {f.name: f for f in copybook.flatten(roots)}

    def _code(self, res):
        return [(ln.indicator, ln.code.strip()) for ln in res.lines if ln.indicator != " " or ln.code.strip()]

    def test_library_01_takes_the_programs_name(self):
        prog = ("       WORKING-STORAGE SECTION.\n"
                "       01  ABCD-SEG   COPY  'ABCDE'.\n"
                "       01  WS-AFTER PIC X.\n")
        res = self._expand(prog, self.FULL)
        code = self._code(res)
        self.assertIn(("*", "01  ABCD-SEG   COPY  'ABCDE'."), code, "the COPY line stays as a comment")
        self.assertIn((" ", "01  ABCD-SEG."), code)
        self.assertNotIn("LIB-REC", expand.expanded_text(res).replace("*", ""))
        exp_line = next(ln.no for ln in res.lines if ln.code.strip() == "01  ABCD-SEG.")
        self.assertEqual(res.origin(exp_line)[:2], (2, 1), "the renamed line cites the copybook's line 1")
        self.assertEqual(res.aliases, [("ABCDE", "LIB-REC", "ABCD-SEG", 1)])
        self.assertEqual(res.renamed, [exp_line])
        tree = self._tree(res)
        self.assertIn("ABCD-SEG", tree)
        self.assertNotIn("LIB-REC", tree)
        self.assertEqual(tree["AAAA-STAT"].parent.name, "ABCD-SEG")
        self.assertEqual((tree["ABCD-SEG"].length, tree["AAAA-STAT"].offset), (12, 10))
        after = next(ln.no for ln in res.lines if "WS-AFTER" in ln.code)
        self.assertEqual(res.origin(after)[:2], (1, 3))

    def test_a_bare_name_and_the_parser_see_the_programs_name(self):
        prog = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. OSVSP.\n       DATA DIVISION.\n"
                "       WORKING-STORAGE SECTION.\n"
                "       01  ABCD-SEG   COPY  ABCDE.\n"
                "       01  WS-X PIC X(12).\n"
                "       PROCEDURE DIVISION.\n"
                "           MOVE WS-X TO ABCD-SEG.\n"
                "           GOBACK.\n")
        res = self._expand(prog, self.FULL)
        self.assertEqual(res.aliases, [("ABCDE", "LIB-REC", "ABCD-SEG", 1)])
        f = cobol.parse_program(expand.expanded_text(res))
        self.assertIn(("ABCD-SEG", "write"), {(n, m) for (n, m, _s, _l) in f.field_refs})
        self.assertIn(("WS-X", "ABCD-SEG"), {(r.src_name, r.dst_name) for r in f.flows if r.kind == "move"})

    def test_the_build_keeps_the_programs_name(self):
        import shutil
        import sqlite3
        from atlas import build
        td = tempfile.mkdtemp()
        try:
            src = os.path.join(td, "estate")
            os.makedirs(src)
            with open(os.path.join(src, "OSVSPGM.cbl"), "w", encoding="utf-8") as fh:
                fh.write("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. OSVSPGM.\n       DATA DIVISION.\n"
                         "       WORKING-STORAGE SECTION.\n"
                         "       01  ABCD-SEG   COPY  'ABCDE'.\n"
                         "       01  WS-X PIC X(12).\n"
                         "       PROCEDURE DIVISION.\n"
                         "           MOVE WS-X TO ABCD-SEG.\n"
                         "           GOBACK.\n")
            with open(os.path.join(src, "ABCDE.cpy"), "w", encoding="utf-8") as fh:
                fh.write(self.FULL)
            db = os.path.join(td, "t.db")
            import contextlib
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                build._main([src, "--db", db, "--rebuild", "--quiet"])
            conn = sqlite3.connect(db)
            try:
                c = conn.execute
                # the program's own field row, not the copybook's children (copy_use reaches those)
                self.assertEqual(c("SELECT f.name, f.length FROM field f JOIN member m ON m.id=f.member_id "
                                   "WHERE m.name='OSVSPGM' ORDER BY f.id").fetchall(), [("ABCD-SEG", 12), ("WS-X", 12)])
                pf = {r[0]: r[1:] for r in c(
                    "SELECT p.name, p.offset, p.length, par.name, m.name, p.src_line, p.copy_field_id IS NOT NULL "
                    "FROM pfield p LEFT JOIN pfield par ON par.id=p.parent_id JOIN member m ON m.id=p.src_member")}
                self.assertEqual(pf["ABCD-SEG"], (0, 12, None, "ABCDE", 1, 1), "cited in the copybook, linked to its row")
                self.assertEqual(pf["AAAA-STAT"][:3], (10, 2, "ABCD-SEG"))
                self.assertNotIn("LIB-REC", pf)
                row = c("SELECT d.dst_pfield, p.name FROM data_flow d JOIN pfield p ON p.id=d.dst_pfield "
                        "WHERE d.kind='move' AND d.src_name='WS-X'").fetchall()
                self.assertEqual([r[1] for r in row], ["ABCD-SEG"], "the MOVE target resolves")
                self.assertEqual(c("SELECT copybook, orig_name, new_name, line FROM field_alias").fetchall(),
                                 [("ABCDE", "LIB-REC", "ABCD-SEG", 1)])
            finally:
                conn.close()
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_a_fragment_hangs_under_the_programs_01(self):
        prog = ("       WORKING-STORAGE SECTION.\n"
                "       01  ABCD-SEG   COPY  'ABCDE'.\n"
                "       01  WS-AFTER PIC X.\n")
        res = self._expand(prog, self.FRAG)
        code = self._code(res)
        k = code.index(("*", "01  ABCD-SEG   COPY  'ABCDE'."))
        self.assertEqual(code[k + 1], (" ", "01  ABCD-SEG."), "kept as code, the COPY clause blanked")
        self.assertEqual(res.origin(k + 2)[:2], (1, 2), "the kept line cites the program's own line")
        self.assertEqual((res.aliases, res.renamed), ([], []))
        tree = self._tree(res)
        self.assertEqual(tree["AAAA-KEY"].parent.name, "ABCD-SEG")
        self.assertEqual(tree["ABCD-SEG"].length, 12)
        after = next(ln.no for ln in res.lines if "WS-AFTER" in ln.code)
        self.assertEqual(res.origin(after)[:2], (1, 3))

    def test_77_form(self):
        res = self._expand("       77  WS-CTR COPY CTRLIB.\n", "       77  LIB-CTR  PIC S9(4) COMP.\n", "CTRLIB")
        self.assertIn((" ", "77  WS-CTR  PIC S9(4) COMP."), self._code(res))
        self.assertEqual(res.aliases, [("CTRLIB", "LIB-CTR", "WS-CTR", 1)])
        self.assertEqual(len(res.renamed), 1)
        # a 77 over a library that starts at 01 is not renamed: the program's 77 is kept
        res = self._expand("       77  WS-CTR COPY CTRLIB.\n", self.FULL, "CTRLIB")
        self.assertIn((" ", "77  WS-CTR."), self._code(res))
        self.assertEqual(res.aliases, [])
        # library text that is clauses continues the program's entry: no period after the name
        res = self._expand("       WORKING-STORAGE SECTION.\n       01  WS-AMT COPY PICLIB.\n",
                           "                   PIC S9(7)V99 COMP-3.\n", "PICLIB")
        self.assertIn((" ", "01  WS-AMT"), self._code(res))
        self.assertEqual(self._tree(res)["WS-AMT"].length, 5)

    def test_fd_form(self):
        prog = ("       FILE SECTION.\n"
                "       FD  POL-FILE  COPY POLFD.\n"
                "       WORKING-STORAGE SECTION.\n")
        lib = ("       FD  LIB-FILE\n"
               "           RECORDING MODE IS F.\n"
               "       01  LIB-FILE-REC      PIC X(80).\n")
        res = self._expand(prog, lib, "POLFD")
        self.assertIn((" ", "FD  POL-FILE"), self._code(res))
        self.assertEqual(res.aliases, [("POLFD", "LIB-FILE", "POL-FILE", 1)])
        self.assertEqual(res.renamed, [], "an FD is not a field")
        # FD clauses as the library text: they continue the program's FD entry, no period
        res = self._expand(prog, "           RECORDING MODE IS F.\n       01  LIB-FILE-REC      PIC X(80).\n", "POLFD")
        self.assertIn((" ", "FD  POL-FILE"), self._code(res))
        # 01 records as the library text: the program's FD entry ends before them
        res = self._expand(prog, "       01  LIB-FILE-REC      PIC X(80).\n", "POLFD")
        self.assertIn((" ", "FD  POL-FILE."), self._code(res))
        # SD the same way
        res = self._expand("       SD  SRT-FILE COPY POLFD.\n", "       SD  LIB-SORT.\n       01  SR PIC X.\n", "POLFD")
        self.assertIn((" ", "SD  SRT-FILE."), self._code(res))

    def test_copy_after_a_closed_01_is_unchanged(self):
        # "01 X." then COPY on its own line: the 01 statement is closed, the copy is its children
        prog = "       01  WS-REC.\n           COPY ABCDE.\n"
        res = self._expand(prog, self.FULL)
        self.assertEqual(self._code(res), [(" ", "01  WS-REC."), ("*", "COPY ABCDE."), (" ", "01  LIB-REC."),
                                           (" ", "05  AAAA-KEY          PIC X(10)."),
                                           (" ", "05  AAAA-STAT         PIC X(02).")])
        self.assertEqual((res.aliases, res.renamed), ([], []))
        res = self._expand("       01  WS-REC.  COPY ABCDE.\n", self.FULL)
        self.assertEqual(res.aliases, [], "a period ends the 01 before the COPY")

    def test_copy_on_the_next_line_is_the_same_statement(self):
        # "01 X" / "COPY Y.": the text before COPY in the same statement is "01 X" - the OS/VS form
        prog = ("       01  ABCD-SEG\n"
                "      * the segment area\n"
                "               COPY 'ABCDE'.\n"
                "       01  WS-AFTER PIC X.\n")
        res = self._expand(prog, self.FULL)
        code = self._code(res)
        self.assertEqual(code[0], ("*", "01  ABCD-SEG"), "the program line gives way to the renamed library 01")
        self.assertTrue(res.lines[0].is_comment)
        self.assertEqual(code[2], ("*", "COPY 'ABCDE'."))
        self.assertEqual(code[3], (" ", "01  ABCD-SEG."))
        self.assertEqual(res.origin(4)[:2], (2, 1))
        self.assertEqual(res.aliases, [("ABCDE", "LIB-REC", "ABCD-SEG", 1)])
        self.assertIn("ABCD-SEG", self._tree(res))
        after = next(ln.no for ln in res.lines if "WS-AFTER" in ln.code)
        self.assertEqual(res.origin(after)[:2], (1, 4), "line accounting unchanged")
        res = self._expand(prog, self.FRAG)
        code = self._code(res)
        self.assertEqual(code[0], (" ", "01  ABCD-SEG."), "a fragment: the program's 01 stays, closed")
        tree = self._tree(res)
        self.assertEqual(tree["AAAA-STAT"].parent.name, "ABCD-SEG")
        self.assertEqual(res.aliases, [])


class DocumentExtraction(unittest.TestCase):

    def test_docx_headings_tables_images(self):
        W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        doc = f"""<w:document xmlns:w="{W}"><w:body>
        <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Claims Interface</w:t></w:r></w:p>
        <w:p><w:r><w:t>The nightly extract feeds </w:t></w:r><w:r><w:t>the warehouse.</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Field</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Len</w:t></w:r></w:p></w:tc></w:tr>
        <w:tr><w:tc><w:p><w:r><w:t>POL-NO</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>12</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "spec.docx")
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("word/document.xml", doc)
                z.writestr("word/media/image1.png", b"\x89PNG")
            d = docs.extract(p)
        self.assertEqual(d.kind, "docx")
        self.assertEqual(d.sections[0][0], "Claims Interface")
        self.assertIn("feeds the warehouse", d.sections[0][1])
        self.assertEqual(d.tables[0][1], ["POL-NO", "12"])
        self.assertEqual(d.images, ["word/media/image1.png"])

    def test_xlsx_shared_strings(self):
        S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        wb = f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Codes" sheetId="1" r:id="rId1"/></sheets></workbook>'
        rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        ss = f'<sst xmlns="{S}"><si><t>E001</t></si><si><t>Policy not found</t></si></sst>'
        sh = (f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
              f'<c r="B1" t="s"><v>1</v></c><c r="C1"><v>42</v></c></row></sheetData></worksheet>')
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "codes.xlsx")
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("xl/workbook.xml", wb)
                z.writestr("xl/_rels/workbook.xml.rels", rels)
                z.writestr("xl/sharedStrings.xml", ss)
                z.writestr("xl/worksheets/sheet1.xml", sh)
            d = docs.extract(p)
        self.assertEqual(d.tables[0][0], ["E001", "Policy not found", "42"])
        self.assertEqual(d.sections[0][0], "sheet: Codes")

    def test_legacy_binary_is_reported_not_silent(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "old.doc")
            with open(p, "wb") as fh:
                fh.write(b"\xd0\xcf\x11\xe0")
            d = docs.extract(p)
        self.assertFalse(d.ok)
        self.assertIn("Save As", d.notes[0])


class CitationGate(unittest.TestCase):

    def _check(self, text):
        return verify_citations.check_answer(text, root=FIX)

    def test_true_citation_passes(self):
        res, _ = self._check("SAMPPGM calls VALIDATE [[SAMPPGM 27 \"CALL 'VALIDATE'\"]].")
        self.assertEqual(res[0].status, "PASS")

    def test_false_citation_fails(self):
        res, _ = self._check("SAMPPGM calls FOO [[SAMPPGM 27 \"CALL 'FOO'\"]].")
        self.assertEqual(res[0].status, "FAIL")

    def test_citation_to_commented_line_warns(self):
        res, _ = self._check("SAMPPGM calls OLDRATER [[SAMPPGM 28 \"CALL 'OLDRATER'\"]].")
        self.assertEqual(res[0].status, "WARN")

    def test_uncited_assertion_is_flagged(self):
        _, uncited = self._check("- SAMPPGM writes the policy master file every night in STEP010.")
        self.assertEqual(len(uncited), 1)

    def test_unknown_member_fails(self):
        res, _ = self._check("[[NOSUCH 1 \"x\"]]")
        self.assertEqual(res[0].status, "FAIL")


class Diagnostics(unittest.TestCase):
    """Shareable reports must carry no paths, dataset names or (optionally) member names."""

    def test_redact_strips_paths_dsns_and_names(self):
        s = diag.redact("failed C:\\estate\\PROD.SRC\\CLMPOST.cbl reading PROD.POLICY.MASTER.KSDS(+1) "
                        "and /home/u/src/x.cbl in CLMPOST", names=["CLMPOST"])
        for secret in ("estate", "POLICY", "/home", "CLMPOST"):
            self.assertNotIn(secret, s)
        self.assertIn("<path>", s)
        self.assertIn("<dsn>", s)

    def test_crash_report_is_written_and_clean(self):
        try:
            raise ValueError("boom while reading C:\\secret\\lib\\member.cbl")
        except ValueError as e:
            with tempfile.TemporaryDirectory() as td:
                p = os.path.join(td, "crash.txt")
                txt = diag.write_crash(e, ["build", "C:\\secret\\estate"], path=p)
                self.assertTrue(os.path.exists(p))
        self.assertIn("ValueError", txt)
        self.assertNotIn("secret", txt)
        self.assertIn("atlas crash", txt)

    def test_report_runs_without_a_db(self):
        txt = diag.report(os.path.join(FIX, "no-such.db"))
        self.assertIn("not found", txt)
        self.assertIn("python", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
