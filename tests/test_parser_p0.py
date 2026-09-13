"""
COBOL parser facts that were wrong or missing (coverage audit, P0):

  EXEC CICS/SQL/DLI text leaking into COBOL verb extractors, OPEN/CLOSE
  file lists, WRITE naming the record not the file, SORT procedures,
  EJECT/SKIP/TITLE and AUTHOR. comment-entries, CALL USING lists that ran
  into the ELSE branch, dropped CALLs, LENGTH OF / subscripts / OF
  qualifiers, 88-driven dynamic targets, SECTIONs as PERFORM targets, GO TO
  and fall-through edges, PERFORM n TIMES, group USAGE, USAGE inside a
  name, anonymous FILLER, level 77, VALUES ARE, PIC CR/DB, and COPY
  REPLACING renames (field aliases).
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, cobol, copybook, expand, query, reader  # noqa: E402


def _prog(body: str, name: str = "TESTPGM", data: str = "") -> cobol.ProgramFacts:
    src = ("       IDENTIFICATION DIVISION.\n"
           f"       PROGRAM-ID. {name}.\n"
           "       DATA DIVISION.\n"
           "       WORKING-STORAGE SECTION.\n" + data +
           "       PROCEDURE DIVISION.\n" + body)
    return cobol.parse_program(src)


class ExecMasking(unittest.TestCase):

    def test_exec_text_is_not_cobol(self):
        f = _prog("           EXEC CICS READ FILE('POLMAST') INTO(WS-REC) RIDFLD(WS-KEY)\n"
                  "                RESP(WS-RESP) END-EXEC.\n"
                  "           EXEC CICS START TRANSID('CLM2') FROM(WS-REC) END-EXEC.\n"
                  "           EXEC SQL CLOSE POLCUR END-EXEC.\n"
                  "           EXEC SQL CALL PRD.POLPROC (:WS-POL, :WS-RC) END-EXEC.\n"
                  "           EXEC SQL CALL CLM_NOTIFY (:WS-POL) END-EXEC.\n")
        targets = {t for (t, _k, _op, _l) in f.io_ops if _k == "file"}
        self.assertFalse(targets & {"FILE", "TRANSID", "POLCUR"}, targets)
        self.assertEqual([c.kind for c in f.calls], ["cics_start", "sql_call", "sql_call"])
        self.assertEqual([c.target for c in f.calls], ["CLM2", "POLPROC", "CLM_NOTIFY"])
        self.assertEqual(f.unresolved, [])
        self.assertIn(("WS-REC", "write", "EXEC-CICS-READ"), [(n, m, s) for (n, m, s, _l) in f.field_refs])
        self.assertIn(("WS-KEY", "read", "EXEC-CICS-READ"), [(n, m, s) for (n, m, s, _l) in f.field_refs])
        self.assertIn(("WS-RESP", "write", "EXEC-CICS-READ"), [(n, m, s) for (n, m, s, _l) in f.field_refs])
        self.assertNotIn("RIDFLD", [n for (n, _m, _s, _l) in f.field_refs])


class FileIO(unittest.TestCase):

    def test_open_close_lists_and_record_owner(self):
        src = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. WRITER.\n"
               "       ENVIRONMENT DIVISION.\n       INPUT-OUTPUT SECTION.\n       FILE-CONTROL.\n"
               "           SELECT POLICY-IN ASSIGN TO POLIN.\n"
               "           SELECT CLAIM-IN ASSIGN TO CLMIN.\n"
               "           SELECT OUT-FILE ASSIGN TO OUTFILE.\n"
               "           SELECT SORT-FILE ASSIGN TO SORTWK.\n"
               "       DATA DIVISION.\n       FILE SECTION.\n"
               "       FD  POLICY-IN.\n       01  POL-REC PIC X(80).\n"
               "       FD  CLAIM-IN.\n       01  CLM-REC PIC X(80).\n"
               "       FD  OUT-FILE.\n       01  OUT-REC PIC X(80).\n       01  OUT-REC2 PIC X(80).\n"
               "       SD  SORT-FILE.\n       01  SORT-REC PIC X(80).\n"
               "       WORKING-STORAGE SECTION.\n       01  WS-X PIC X.\n"
               "       PROCEDURE DIVISION.\n"
               "           OPEN INPUT POLICY-IN CLAIM-IN\n                OUTPUT OUT-FILE.\n"
               "           WRITE OUT-REC.\n           REWRITE OUT-REC2.\n"
               "           SORT SORT-FILE ON ASCENDING KEY SORT-KEY\n"
               "                INPUT PROCEDURE 2000-INPUT THRU 2000-EXIT\n"
               "                OUTPUT PROCEDURE 3000-OUTPUT.\n"
               "           SORT SORT-FILE ON ASCENDING KEY SORT-KEY\n"
               "                USING POLICY-IN GIVING OUT-FILE.\n"
               "           CLOSE POLICY-IN CLAIM-IN OUT-FILE.\n")
        f = cobol.parse_program(src)
        ops = {(t, op) for (t, _k, op, _l) in f.io_ops}
        self.assertIn(("POLICY-IN", "OPEN INPUT"), ops)
        self.assertIn(("CLAIM-IN", "OPEN INPUT"), ops)
        self.assertIn(("OUT-FILE", "OPEN OUTPUT"), ops)
        self.assertIn(("OUT-FILE", "WRITE"), ops)              # the record's owner, not OUT-REC
        self.assertIn(("OUT-FILE", "REWRITE"), ops)
        self.assertIn(("CLAIM-IN", "CLOSE"), ops)
        self.assertIn(("POLICY-IN", "SORT READ"), ops)
        self.assertIn(("OUT-FILE", "SORT WRITE"), ops)
        fd = next(x for x in f.files if x.select_name == "OUT-FILE")
        self.assertEqual((fd.fd_record, fd.fd_records), ("OUT-REC", ["OUT-REC", "OUT-REC2"]))
        self.assertTrue(next(x for x in f.files if x.select_name == "SORT-FILE").is_sd)
        self.assertIn(("2000-INPUT", "2000-EXIT", "sort_proc"), [(t, th, k) for (_f, t, th, _l, k) in f.performs])
        self.assertIn(("3000-OUTPUT", None, "sort_proc"), [(t, th, k) for (_f, t, th, _l, k) in f.performs])


class ReaderDirectives(unittest.TestCase):

    def test_eject_and_author_apostrophe(self):
        src = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. PE.\n"
               "       AUTHOR. PAT O'BRIEN.\n       DATE-WRITTEN. 01/02/1999.\n"
               "       PROCEDURE DIVISION.\n       0000-MAIN.\n"
               "           PERFORM 2000-INPUT.\n           CALL 'SUBZ' USING WS-A.\n           GOBACK.\n"
               "       EJECT\n       2000-INPUT.\n           CONTINUE.\n       SKIP2\n"
               "       TITLE 'THE NEXT PART'\n       3000-MORE.\n           CONTINUE.\n")
        f = cobol.parse_program(src)
        self.assertEqual([p.name for p in f.paragraphs], ["0000-MAIN", "2000-INPUT", "3000-MORE"])
        self.assertEqual([c.target for c in f.calls], ["SUBZ"])
        lines, _ = reader.read_cobol_lines(src)
        self.assertTrue(next(l for l in lines if "O'BRIEN" in l.raw).is_comment)
        self.assertTrue(next(l for l in lines if "EJECT" in l.raw).is_comment)


class CallParsing(unittest.TestCase):

    def test_using_list_stops_and_every_call_is_seen(self):
        f = _prog("           IF WS-KEY OF WS-REC = 'B'\n"
                  "              CALL 'SUB4' USING WS-REC\n"
                  "           ELSE\n"
                  "              CALL WS-PGM2 USING WS-COMM\n"
                  "           END-IF.\n"
                  "           SET WS-PGM-SUBA TO TRUE.\n"
                  "           CALL WS-PGM USING WS-A.\n"
                  "           CALL 'SUB2' USING BY CONTENT LENGTH OF WS-REC, WS-REC\n"
                  "                             BY REFERENCE ADDRESS OF WS-COMM WS-TAB(1).\n",
                  data="       01  WS-PGM PIC X(8).\n           88  WS-PGM-SUBA VALUE 'SUBA'.\n")
        by = {(c.target or c.via_var): c for c in f.calls}
        self.assertEqual(by["SUB4"].using_args, ["WS-REC"])
        self.assertIn("WS-PGM2", by)
        self.assertEqual(by["WS-PGM"].resolved, ["SUBA"])
        self.assertEqual(by["SUB2"].using_args, ["WS-REC", "WS-REC", "WS-COMM", "WS-TAB"])
        self.assertIn(("B", "compare", "WS-KEY"), [(l, c, fld) for (l, c, fld, _ln) in f.literal_refs])
        self.assertNotIn("WS-REC", [n for (n, m, s, _l) in f.field_refs if m == "test"])


class ControlFlow(unittest.TestCase):

    def test_sections_goto_times_fallthrough(self):
        f = _prog("       0000-MAIN SECTION.\n       0000-START.\n"
                  "           PERFORM 1000-PROCESS.\n"
                  "           PERFORM 3 TIMES DISPLAY 'X' END-PERFORM.\n"
                  "           PERFORM WS-CNT TIMES DISPLAY 'Y' END-PERFORM.\n"
                  "           IF WS-EOF = 'Y' GO TO 9000-END.\n"
                  "           GO TO 1000-A 1000-B DEPENDING ON WS-PTR.\n"
                  "       1000-PROCESS SECTION 50.\n       1000-A.\n           CONTINUE.\n"
                  "       1000-B.\n           CONTINUE.\n       9000-END.\n           GOBACK.\n")
        names = [p.name for p in f.paragraphs]
        self.assertEqual(names, ["0000-MAIN", "0000-START", "1000-PROCESS", "1000-A", "1000-B", "9000-END"])
        self.assertEqual(next(p for p in f.paragraphs if p.name == "1000-A").section, "1000-PROCESS")
        self.assertEqual(next(p for p in f.paragraphs if p.name == "1000-PROCESS").kind, "section")
        edges = {(t, k) for (_f, t, _th, _l, k) in f.performs}
        self.assertIn(("1000-PROCESS", "perform"), edges)
        self.assertNotIn(("3", "perform"), edges)
        self.assertNotIn(("WS-CNT", "perform"), edges)
        self.assertIn(("9000-END", "goto"), edges)
        self.assertIn(("1000-A", "goto_depending"), edges)
        self.assertIn(("1000-B", "goto_depending"), edges)
        self.assertIn(("1000-B", "fallthrough"), edges)          # 1000-A ends with CONTINUE
        self.assertNotIn(("9000-END", "fallthrough"), [(t, k) for (_f, t, _th, _l, k) in f.performs
                                                        if _f == "9000-END"])


class CopybookArithmetic(unittest.TestCase):

    def test_group_usage_anonymous_filler_77_values_crdb(self):
        text = ("       01  WS-AMOUNTS COMP-3.\n"
                "           05  WS-PREM    PIC S9(7)V99.\n"
                "           05  WS-COMM    PIC S9(5)V99.\n"
                "       01  WS-GRP.\n"
                "           05  WS-BINARY-CNT PIC 9(2).\n"
                "           05  WS-TBL PIC X(3) OCCURS 1 TO 10 DEPENDING ON WS-BINARY-CNT.\n"
                "           05  WS-AFTER PIC X(10).\n"
                "       01  WS-REC.\n"
                "           05  WS-A PIC X(5).\n"
                "           05       PIC X(3).\n"
                "           05  WS-B PIC X(5).\n"
                "           05  RPT-AMT  PIC ZZ,ZZ9.99DB.\n"
                "           05  RPT-NEXT PIC X.\n"
                "       77  WS-C PIC S9(4) COMP.\n"
                "       01  WS-STATUS PIC X(2).\n"
                "           88  WS-OK  VALUES 'OK' 'AA'.\n"
                "           88  WS-BAD VALUES ARE 'ER' THRU 'EZ'.\n")
        roots, _w = copybook.parse_copybook(text)
        by = {f.name: f for f in copybook.flatten(roots)}
        self.assertEqual((by["WS-PREM"].length, by["WS-PREM"].usage), (5, "COMP-3"))
        self.assertEqual((by["WS-COMM"].offset, by["WS-AMOUNTS"].length), (5, 9))
        self.assertIsNone(by["WS-TBL"].usage)                    # COMP inside WS-BINARY-CNT is a name
        self.assertEqual(by["WS-TBL"].length, 3)
        self.assertEqual(by["WS-B"].offset, 8)                    # the anonymous FILLER takes 3 bytes
        self.assertEqual(by["RPT-AMT"].length, 11)                # DB is two bytes
        self.assertEqual(by["RPT-NEXT"].offset, 24)
        self.assertEqual((by["WS-C"].level, by["WS-C"].length, by["WS-C"].parent), (77, 2, None))
        conds = {c[0]: c[1] for c in by["WS-STATUS"].conds}
        self.assertEqual(conds["WS-OK"], ["'OK'", "'AA'"])
        self.assertEqual(conds["WS-BAD"], ["'ER'", "THRU", "'EZ'"])


class Replacing(unittest.TestCase):

    def test_numeric_leading_and_literal_corners(self):
        pairs = expand.parse_replacing("==01== BY ==05==")
        self.assertEqual(expand.apply_replacing("01  POL-REC.", pairs), "05  POL-REC.")
        self.assertEqual(expand.apply_replacing("    05 POL-NO PIC X(01) OCCURS 01 TIMES.", pairs),
                         "    05 POL-NO PIC X(01) OCCURS 01 TIMES.")
        lead = expand.parse_replacing("LEADING ==PM-== BY ==LK-==")
        self.assertEqual(lead, [("PM-", "LK-", "LEADING")])
        self.assertEqual(expand.apply_replacing("05 PM-STATUS PIC X. 05 XPM-Z PIC X.", lead),
                         "05 LK-STATUS PIC X. 05 XPM-Z PIC X.")
        self.assertEqual(expand._mask_literals("DISPLAY 'COPY X FAILED' COPY Y"), "DISPLAY " + " " * 15 + " COPY Y")


class ReplacingAliasesEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        os.makedirs(os.path.join(cls.root, "SRC"))
        os.makedirs(os.path.join(cls.root, "COPYLIB"))
        shutil.copy(os.path.join(HERE, "fixtures", "PMASTREC.cpy"), os.path.join(cls.root, "COPYLIB", "PMASTREC.cpy"))
        with open(os.path.join(cls.root, "COPYLIB", "POLREC.cpy"), "w") as fh:
            fh.write("       01  POL-REC.\n           05  POL-NO PIC X(01) OCCURS 01 TIMES.\n")
        with open(os.path.join(cls.root, "SRC", "LKPGM.cbl"), "w") as fh:
            fh.write("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. LKPGM.\n"
                     "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
                     "       01  LK-REC.\n           COPY PMASTREC REPLACING ==PM-== BY ==LK-==.\n"
                     "       01  POL-GROUP.\n           COPY POLREC REPLACING ==01== BY ==05==.\n"
                     "       PROCEDURE DIVISION.\n"
                     "           IF LK-POLICY-STATUS = 'LP'\n"
                     "              MOVE 'AC' TO LK-POLICY-STATUS\n           END-IF.\n"
                     "           DISPLAY 'COPY POLREC FAILED HERE'.\n           GOBACK.\n")
        cls.db = os.path.join(cls.td, "t.db")
        build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_alias_rows_and_expanded_text(self):
        rows = self.conn.execute("SELECT copybook, orig_name, new_name FROM field_alias ORDER BY orig_name").fetchall()
        self.assertIn(("PMASTREC", "PM-POLICY-STATUS", "LK-POLICY-STATUS"), [tuple(r) for r in rows])
        pol = self.conn.execute("SELECT pic, occurs_max FROM field WHERE name='POL-NO'").fetchone()
        self.assertEqual((pol["pic"], pol["occurs_max"]), ("X(01)", 1))       # not X(05) / OCCURS 05
        self.assertIsNone(self.conn.execute("SELECT 1 FROM copy_use WHERE copybook='X'").fetchone())

    def test_layout_and_field_follow_the_alias(self):
        out = query.cmd_layout(self.conn, "PMASTREC", program="LKPGM")
        self.assertIn("LK-POLICY-STATUS", out)
        self.assertIn("record length:", out)


if __name__ == "__main__":
    unittest.main()
