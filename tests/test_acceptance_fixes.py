"""
The acceptance test of the re-parse batch (docs/SYNTH-findings-2026-09-25-after-batch.md) found every synthetic
finding fixed, and left open the wrong facts the stages' verifiers had found in the fact modules - shapes the
synthetic estate never generates. Each is fixed here, before the one re-parse night, with a minimal reproduction
under tools/synth/repro/V01..V15 (`python tools/synth/repro/verify.py`):

  LESSONS 248 - the classifier, the reader and the program test: a data copybook's 88-level VALUES list of six-digit
      codes over three lines was a compiler listing; an MFS or Assembler literal continued on the next line (`MOVE
      THE CURSOR TO ...`) was a COBOL statement; a prose heading `02 Premium values` a data entry; a PL/I program's
      GO TO a COBOL statement; a copybook with a change tag in columns 1-6 and one line of digits a stub; a field
      named CA-PROGRAM-ID a PROGRAM-ID paragraph.
  LESSONS 249 - build.picks_moved: a kept program whose choice among several copies the compiler listings now make
      differently (a listing read, moved or gone since it was parsed) is parsed again - the index no longer depends
      on the order the listings arrived in.
  LESSONS 250 - jcl: a job running a PROC with its default sequential card dataset carries the card_seq_assumed row
      the same PROC run with an override carries; a job step's //PS.DD override row naming a (+1) an earlier step
      wrote reads it, as the effective DD does.
  LESSONS 251 - expand: a COPY whose text-name is on the next line is expanded.
  LESSONS 252 - cobol: `XML PARSE ... ON EXCEPTION GO TO` opening a sentence and a scope terminator of a nested
      statement of the same verb no longer make the sentence always leave; GO TO ... DEPENDING ON with a comma or a
      semicolon, and GO without TO, give their edges.

Every name here is fictional.
"""

import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from atlas import build, classify, cobol, expand, jcl, query, reader, recover  # noqa: E402
from test_copy_sources import DUPREC_CLAIMS, DUPREC_POLICY, listing, program  # noqa: E402
from test_inventory_forcing import facts, facts_differ  # noqa: E402
from test_listing_resolver import build_it, pick_of, write_estate  # noqa: E402

LISTING = build.LISTING_HOW


# ===========================================================================
# LESSONS 248 - the classifier, the reader and the program test
# ===========================================================================

NAICS = "\n".join([
    "       01  XQ-NAICS-REC.",
    "           05  XQ-NAICS-CD            PIC 9(06).",
    "               88  XQ-CONSTRUCTION    VALUES 236115 236116 236117",
    "                                             236118 236210 236220",
    "                                             237110 237120 237130",
    "                                             237210 237310 237990.",
    "           05  XQ-NAICS-DESC          PIC X(40).",
]) + "\n"
# the same entry with ISPF NUM ON COBOL sequence numbers in columns 1-6: two six-digit numbers on each line, counting
# up by 100 - still no listing's own numbering
NAICS_NUMBERED = "\n".join(f"{(k + 1) * 100:06d}{line[6:]}" for k, line in enumerate(NAICS.splitlines())) + "\n"
# numbered source lines of a listing whose heading is gone: the listing's line number, then the source record with its
# own sequence number - the listing's numbers count up by one, a message line between them changes nothing
LISTED = "\n".join(["   000040  004000     MOVE A TO B.",
                    "   000041  004100     MOVE C TO D.",
                    "==000041==> IGYPS2121-S  \"C\" was not defined as a data-name.",
                    "   000042  004200     GOBACK."]) + "\n"
MFS_CONTINUED = "\n".join([
    "XQFM3    FMT",
    "         DEV   TYPE=(3270,2),FEAT=IGNORE",
    "         DIV   TYPE=INOUT",
    "         DPAGE CURSOR=((2,2))",
    "         DFLD  'XQ POLICY INQUIRY - KEY THE POLICY NUMBER, THEN".ljust(71) + "X",
    "               MOVE THE CURSOR TO THE ACTION FIELD',POS=(2,2)",
    "POLNO    DFLD  POS=(4,2),LTH=10",
    "         FMTEND",
    "XQMSG3   MSG   TYPE=OUTPUT,SOR=(XQFM3,IGNORE)",
    "         SEG",
    "         MFLD  POLNO,LTH=10",
    "         MSGEND",
    "         END",
]) + "\n"
ASM_CONTINUED = "\n".join([
    "XQASM    CSECT",
    "         USING *,15",
    "MSG1     DC    C'KEY THE POLICY NUMBER, THEN".ljust(71) + "X",
    "               MOVE THE CURSOR TO THE FIELD'",
    "         BR    14",
    "         END",
]) + "\n"
# a COBOL literal continued with '-' in column 7, its continuation opening with the words of a MOVE
COBOL_CONTINUED = ("       01  XQ-MSGS.\n"
                   "           05  XQ-MSG1   PIC X(80) VALUE 'KEY THE POLICY NUMBER, THEN\n"
                   "      -    'MOVE THE CURSOR TO THE FIELD'.\n")
PLI_VERIFIER = "\n".join([" XQPLI: PROC OPTIONS(MAIN);", "   DCL 1 POLREC,", "         2 POLID CHAR(10),",
                          "         2 AMT FIXED DEC(9,2);", "   ON ENDFILE(POLIN) GO TO EOJ;",
                          "   READ FILE(POLIN) INTO(POLREC);", "   IF AMT > 0 THEN", "      GO TO EOJ;", " EOJ:",
                          "   CLOSE FILE(POLIN);", " END XQPLI;"]) + "\n"
PLI_EDGE = (" XEPLI: PROC OPTIONS(MAIN);\n   DCL POLIN FILE RECORD INPUT;\n"
            "   DCL 1 POLREC, 2 POLID CHAR(10), 2 AMT FIXED DEC(9,2);\n   READ FILE(POLIN) INTO(POLREC);\n"
            "   IF AMT < 0 THEN\n      GO TO EOJ;\n EOJ:\n END XEPLI;\n")
COMMAREA = ("       01  CA-AREA.\n           05  CA-KEY         PIC X(8).\n"
            "           05  CA-PROGRAM-ID  PIC X(8).\n           05  CA-RETURN-CD   PIC 9(4).\n")
TAGGED = "\n".join(["*CR01      05  QX-CODE PIC 9(3).",
                    "*CR01          88  QX-VALID VALUES 001 002 003",
                    "*CR01                           004 005",
                    "*CR01                           006.",
                    "*CR01      05  QX-NAME PIC X(20)."]) + "\n"


def kind(path, text):
    return classify.classify(path, text)[0]


class TheClassifier(unittest.TestCase):

    def test_a_values_list_of_six_digit_codes_is_no_listing(self):
        # Wrong answer guarded: ('listing', 'compiler listing ...') - every program copying XQNAICS said NOT FOUND
        self.assertIsNone(classify.listing_hit(NAICS))
        self.assertEqual(kind(r"C:\e\XQ\PROD.XQ.COPYLIB\XQNAICS.cpy", NAICS), "copybook")
        self.assertEqual(kind(r"C:\e\XQ\PROD.XQ.DATA\XQNAICS.txt", NAICS), "copybook")
        self.assertIsNone(classify.listing_hit(NAICS_NUMBERED))
        self.assertEqual(kind(r"C:\e\XQ\PROD.XQ.DATA\XQNAICS.txt", NAICS_NUMBERED), "copybook")

    def test_a_listings_own_numbered_lines_still_make_a_listing(self):
        self.assertEqual(classify.listing_hit(LISTED), (0, "numbered source lines"))
        self.assertEqual(kind(r"C:\e\XQ\PROD.XQ.DATA\XQPGM.txt", LISTED), "listing")
        # three numbered lines that do not count up by one are no run
        jumbled = LISTED.replace("000041  004100", "000047  004100")
        self.assertIsNone(classify.listing_hit(jumbled))

    def test_a_continued_literal_is_text(self):
        # Wrong answer guarded: ('copybook', 'COBOL procedure statements ...') - `screen XQFM3` said NOT FOUND
        for folder in ("PROD.XQ.MFS", "PROD.XQ.SCREENS"):
            self.assertEqual(kind(rf"C:\e\XQ\{folder}\XQFM3.txt", MFS_CONTINUED), "mfs", folder)
        self.assertEqual(kind(r"C:\e\XQ\PROD.XQ.MACLIB\XQASM.txt", ASM_CONTINUED), "asm")
        view = classify.code_view(classify.normalized(MFS_CONTINUED))
        self.assertEqual(view.count("\n"), MFS_CONTINUED.count("\n"))
        self.assertEqual([len(x) for x in view.split("\n")], [len(x) for x in MFS_CONTINUED.split("\n")])
        self.assertNotIn("MOVE", view)
        self.assertIn(",POS=(2,2)", view)

    def test_cobol_text_around_a_continued_literal_still_counts(self):
        self.assertEqual(kind(r"C:\e\XQ\PROD.XQ.DATA\XQMSGS.txt", COBOL_CONTINUED), "copybook")
        self.assertNotIn("MOVE THE CURSOR", classify.code_view(COBOL_CONTINUED))
        # a literal left open with no continuation (neither '-' in column 7 nor a character in column 72): the next
        # line is read as it stands
        proc = "       A-110-DO.\n           DISPLAY 'IT''S OPEN\n           MOVE 1 TO WS-X.\n"
        self.assertIn("MOVE 1 TO WS-X", classify.code_view(proc))
        self.assertEqual(kind(r"C:\e\XQ\PROD.XQ.PROCS\XQPROCB.txt", proc), "copybook")

    def test_a_prose_heading_is_no_data_entry(self):
        # Wrong answer guarded: ('copybook', 'COBOL level numbers ...') for a design note
        doc = "Design note\n\n02 Premium values\nThe premium is computed monthly.\n"
        self.assertEqual(kind(r"C:\e\XQ\XQDOCS\note.md", doc), "doc")
        self.assertEqual(kind(r"C:\e\XQ\notes\note.md", doc), "doc")
        self.assertEqual(kind(r"C:\e\XQ\notes\note.txt", doc), "doc")
        self.assertEqual(kind(r"C:\e\XQ\XQDOCS\toc.txt", "   01 Overview\n   02 Premium values\n"), "doc")

    def test_a_data_entry_with_its_operand_still_types_a_copybook(self):
        column1 = "05 WS-ID PIC X(10).\n05 WS-AMT PIC 9(5) VALUE 0.\n"
        self.assertEqual(kind(r"C:\e\XQ\PROD.XQ.DATA\XQCOL1.txt", column1), "copybook")
        for text in ("01 WS-FLAG VALUE 'Y'.\n", "01 WS-TAB OCCURS 5.\n", "01 WS-ALT REDEFINES WS-ID.\n",
                     "01 WS-N USAGE COMP.\n", "01 WS-Z VALUE ZEROS.\n"):
            self.assertEqual(kind(r"C:\e\XQ\PROD.XQ.DATA\XQCOL2.txt", text), "copybook", text)
        # a copybook filed in a documents library is still one, by an entry with its clause
        rec = "       01  XQ-REC.\n           05  XQ-ID   PIC X(10).\n"
        self.assertEqual(kind(r"C:\e\XQ\XQDOCS\rec.txt", rec), "copybook")

    def test_pli_is_not_cobol(self):
        # Wrong answer guarded: ('copybook', 'COBOL procedure statements ...') by the PL/I GO TO
        for text in (PLI_VERIFIER, PLI_EDGE):
            k, why = classify.classify(r"C:\e\XQ\PROD.XQ.PLI\XQPLI.txt", text)
            self.assertEqual(k, "unknown")
            self.assertIn(classify.PLI_WORDS, why)
        self.assertEqual(classify.not_cobol(classify.normalized(PLI_VERIFIER)), classify.PLI_WORDS)
        # COBOL that merely begins a paragraph name with DCL, or a DB2 cursor declared in a copybook, is not PL/I
        self.assertEqual(classify.not_cobol("       DCL-100-START.\n           MOVE 1 TO WS-X.\n"), "")
        self.assertEqual(classify.not_cobol("           EXEC SQL\n             DECLARE CSR1 CURSOR FOR\n"), "")
        # nor is a REXX routine's label and PROCEDURE: PL/I ends its PROC statement with a semicolon or opens options
        self.assertEqual(classify.not_cobol("MAIN: PROCEDURE\n  SAY 'HI'\n  RETURN\n"), "")
        self.assertEqual(classify.not_cobol("CHECK: PROCEDURE EXPOSE RC\n"), "")
        self.assertEqual(classify.not_cobol(" SUB1: PROC;\n"), classify.PLI_WORDS)

    def test_a_field_named_program_id_is_no_program(self):
        # Wrong answer guarded: ('cobol', 'IDENTIFICATION DIVISION / PROGRAM-ID') and a program row named PIC
        self.assertEqual(kind(r"C:\e\GC\PROD.GC.COPYLIB\KCCOMM1.cpy", COMMAREA), "copybook")
        lines = reader.read_cobol_lines(COMMAREA)[0]
        self.assertFalse(build.holds_program(lines))
        self.assertIsNone(cobol.parse_program(COMMAREA).program_id)
        msgs = "       01  XQ-MSGS.\n           05  FILLER PIC X(20) VALUE 'PROCEDURE DIVISION'.\n"
        self.assertFalse(build.holds_program(reader.read_cobol_lines(msgs)[0]))
        # the build's program test and index_cobol's read the words outside literals alike (build.division_header)
        self.assertFalse(build.division_header("       01  X PIC X(20) VALUE 'PROCEDURE DIVISION'.\n"))
        self.assertTrue(build.division_header("       PROCEDURE DIVISION USING LK-AREA.\n"))
        said = "       A-100-SAY.\n           DISPLAY 'PROGRAM-ID IS KVX'.\n"
        self.assertIsNone(cobol.parse_program(said).program_id)
        self.assertFalse(build.holds_program(reader.read_cobol_lines(said)[0]))
        real = program("KCPGM", "KCCOMM1")
        self.assertTrue(build.holds_program(reader.read_cobol_lines(real)[0]))
        self.assertEqual(cobol.parse_program(real).program_id, "KCPGM")
        self.assertEqual(kind(r"C:\e\GC\PROD.GC.SRC\KCPGM.cbl", real), "cobol")


class TheStubCount(unittest.TestCase):

    def test_a_change_tag_in_the_sequence_area_is_no_remark(self):
        # Wrong answer guarded: 1 - the copybook filed `stub`, never expanded, every copier partial
        self.assertEqual(reader.stub_count(TAGGED), 0)

    def test_the_stubs_stay_stubs(self):
        self.assertGreater(reader.stub_count("*0000100\n"), 0)                      # LESSONS 206: a digit in column 7
        self.assertEqual(reader.stub_count("*> RETIRED - SEE PROD.QX.COPYLIB\n0000100\n"), 1)   # a floating remark
        self.assertEqual(reader.stub_count("*CR01  0000100\n"), 1)                 # a tag, and only digits where read
        self.assertEqual(reader.stub_count("0000100\n0000200\n"), 2)


# ===========================================================================
# LESSONS 251 - a COPY whose text-name is on the next line
# ===========================================================================

SPLIT_PROG = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. KVSPLIT.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-REC.
           COPY
               KVBOOK.
       01  WS-B            PIC X(5).
       01  WS-MSG          PIC X(20) VALUE 'SEE COPY'.
       PROCEDURE DIVISION.
           MOVE KV-KEY TO WS-B.
           GOBACK.
"""
KVBOOK = "           05  KV-KEY  PIC X(5).\n           05  KV-AMT  PIC 9(3).\n"


class ACopyOverTwoLines(unittest.TestCase):

    def expand(self, text):
        lines = reader.read_cobol_lines(text)[0]
        book = reader.read_cobol_lines(KVBOOK)[0]
        return expand.expand(lines, 1, lambda name, lib: (7, book, None) if name == "KVBOOK" else None)

    def test_the_name_on_the_next_line(self):
        # Wrong answer guarded: no copy_use row, no KV-KEY, `parse: ok`
        e = self.expand(SPLIT_PROG)
        self.assertEqual(e.copies, [("KVBOOK", None, None, 6, 7)])
        self.assertIn("05  KV-KEY  PIC X(5).", expand.expanded_text(e))
        # past a comment line, with the library and REPLACING after the name
        e = self.expand(SPLIT_PROG.replace("           COPY\n", "           COPY\n      * THE KEY AREA\n")
                        .replace("KVBOOK.", "KVBOOK OF KVLIB."))
        self.assertEqual([c[:2] for c in e.copies], [("KVBOOK", "KVLIB")])

    def test_copy_inside_a_literal_or_a_word_is_none(self):
        text = SPLIT_PROG.replace("           COPY\n               KVBOOK.\n",
                                  "           05  WS-T PIC X(9) VALUE 'SEE COPY'.\n           05  WS-COPY PIC X.\n")
        self.assertEqual(self.expand(text).copies, [])


# ===========================================================================
# LESSONS 252 - a sentence that leaves, and the GO TO edges
# ===========================================================================

LEAVE_PROG = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. KVLEAVE.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-DOC          PIC X(80).
       01  WS-IX           PIC 9.
       PROCEDURE DIVISION.
       1000-MAIN.
           XML PARSE WS-DOC PROCESSING PROCEDURE 3000-HANDLER
               ON EXCEPTION GO TO 9000-ERR
           END-XML.
       2000-CALLS.
           CALL 'KVSUB' USING WS-DOC ON EXCEPTION
               CALL 'KVABND' USING WS-DOC END-CALL
               GO TO 9000-ERR
           END-CALL.
       2500-BRANCH.
           GO TO 3100-A, 3200-B DEPENDING ON WS-IX;
           GO 9000-ERR.
       3000-HANDLER.
           EXIT.
       3100-A.
           EXIT.
       3200-B.
           EXIT.
       9000-ERR.
           GOBACK.
"""


class ASentenceThatLeaves(unittest.TestCase):

    def test_the_sentences(self):
        # Wrong answer guarded: True for both - the next paragraph 'reached by nothing'
        self.assertFalse(cobol._leaves("XML PARSE WS-DOC PROCESSING PROCEDURE 3000-HANDLER ON EXCEPTION GO TO 9000-ERR "
                                       "END-XML."))
        self.assertFalse(cobol._leaves("CALL 'KVSUB' USING WS-PARM ON EXCEPTION CALL 'KVABND' USING WS-PARM END-CALL "
                                       "GO TO 9000-ERR END-CALL."))
        self.assertFalse(cobol._leaves("READ F AT END READ G END-READ GO TO X END-READ."))
        # still leaving: the phrase-less statement closed before the GO TO, a statement with no phrase at all
        self.assertTrue(cobol._leaves("XML PARSE WS-DOC PROCESSING PROCEDURE 3000-H END-XML GO TO 9000-ERR."))
        self.assertTrue(cobol._leaves("CALL 'A' ON EXCEPTION DISPLAY 'E' END-CALL GO TO X."))
        self.assertTrue(cobol._leaves("READ F AT END MOVE 'Y' TO EOF END-READ GO TO 1000-READ."))
        self.assertTrue(cobol._leaves("ADD 1 TO X END-ADD GO TO Y."))
        self.assertTrue(cobol._leaves("GO 9000-ERR."))

    def test_the_edges(self):
        f = cobol.parse_program(LEAVE_PROG)
        edges = {(a, b, k) for a, b, _t, _l, k in f.performs}
        self.assertIn(("1000-MAIN", "2000-CALLS", "fallthrough"), edges)
        self.assertIn(("2000-CALLS", "2500-BRANCH", "fallthrough"), edges)
        # Wrong answer guarded: no edge to 3100-A, 3200-B or 9000-ERR from 2500-BRANCH
        self.assertIn(("2500-BRANCH", "3100-A", "goto_depending"), edges)
        self.assertIn(("2500-BRANCH", "3200-B", "goto_depending"), edges)
        self.assertIn(("2500-BRANCH", "9000-ERR", "goto"), edges)
        self.assertNotIn(("2500-BRANCH", "3000-HANDLER", "fallthrough"), edges)
        self.assertEqual([m.groups() for m in cobol._GO_TO.finditer("GO TO.")], [])


# ===========================================================================
# LESSONS 250 - the jobs
# ===========================================================================

SEQ_PROC = """//KVSORT   PROC CARDS=KVDEFC
//SRT010   EXEC PGM=SORT
//SORTIN   DD   DSN=PROD.KV.SORTIN,DISP=SHR
//SORTOUT  DD   DSN=PROD.KV.SORTOUT,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD   DSN=PROD.KV.CARDS.&CARDS,DISP=SHR
//SYSOUT   DD   SYSOUT=*
"""
SEQ_CARDS = {"KVJOBC": "  SORT FIELDS=(1,12,CH,A)\n", "KVDEFC": "  SORT FIELDS=(40,8,CH,A)\n"}
GDG_PROC = """//KVGDG    PROC
//PS1      EXEC PGM=KVEXTR
//OUT      DD   DSN=PROD.KV.EXTRACT(+1),DISP=(NEW,CATLG,DELETE)
//PS2      EXEC PGM=KVREAD
//KVIN     DD   DSN=PROD.KV.OTHER,DISP=SHR
"""
GDG_JOB = """//KVGJOB   JOB (ACCT),'G',CLASS=A
//S1       EXEC KVGDG
//PS2.KVIN DD DSN=PROD.KV.EXTRACT(+1),DISP=SHR
//
"""


def expand_job(text, procs, cards=None):
    cards = cards or {}
    job = jcl.parse_jcl(text, member_lookup=cards.get)
    parsed = {k: jcl.parse_jcl(v, member_lookup=cards.get) for k, v in procs.items()}
    return job, {s.step_name: s for s in jcl.expand_job(job, parsed.get, member_lookup=cards.get)}


class TheJobs(unittest.TestCase):

    def test_the_default_sequential_card_dataset_is_said_too(self):
        # Wrong answer guarded: a row for S1 (the override) and none for S2 (the PROC's default)
        job, _by = expand_job("//KVCJOB   JOB (ACCT),'C',CLASS=A\n//S1       EXEC KVSORT,CARDS=KVJOBC\n"
                              "//S2       EXEC KVSORT\n//\n", {"KVSORT": SEQ_PROC}, SEQ_CARDS)
        rows = sorted(u[1] for u in job.unresolved if u[0] == "card_seq_assumed")
        self.assertEqual(rows, ["S1.SRT010 SYSIN: cards taken from member KVJOBC matching the last qualifier of "
                                "PROD.KV.CARDS.KVJOBC",
                                "S2.SRT010 SYSIN: cards taken from member KVDEFC matching the last qualifier of "
                                "PROD.KV.CARDS.KVDEFC"])

    def test_the_override_row_reads_the_generation(self):
        # Wrong answer guarded: S1's PS2.KVIN row 'output [gdg_relative]' - a second writer of the (+1)
        for disp in (",DISP=SHR", ",DISP=OLD", ""):
            job, by = expand_job(GDG_JOB.replace(",DISP=SHR\n//\n", disp + "\n//\n"), {"KVGDG": GDG_PROC})
            own = next(d for d in job.steps[0].dds if d.dd_name == "PS2.KVIN")
            eff = next(d for d in by["S1.PS2"].dds if d.dd_name == "KVIN")
            self.assertEqual((own.mode, own.mode_source), ("input", "gdg_same_job"), disp)
            self.assertEqual((eff.mode, eff.mode_source), ("input", "gdg_same_job"), disp)
        # the writer stays the writer; an override naming a generation nothing earlier wrote keeps its direction
        _job, by = expand_job(GDG_JOB, {"KVGDG": GDG_PROC})
        self.assertEqual(next((d.mode, d.mode_source) for d in by["S1.PS1"].dds if d.dd_name == "OUT"),
                         ("output", "gdg_relative"))
        job, _by = expand_job(GDG_JOB.replace("PROD.KV.EXTRACT(+1),DISP=SHR", "PROD.KV.OTHERGDG(+1),DISP=SHR"),
                              {"KVGDG": GDG_PROC})
        own = next(d for d in job.steps[0].dds if d.dd_name == "PS2.KVIN")
        self.assertEqual((own.mode, own.mode_source), ("output", "gdg_relative"))


# ===========================================================================
# the fixes in one build: what the index holds
# ===========================================================================

KCPGM = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. KCPGM.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-OUT              PIC X(8).
       COPY KCCOMM1.
       PROCEDURE DIVISION.
           MOVE CA-PROGRAM-ID TO WS-OUT.
           GOBACK.
"""
KCCOMM1_SHARED = "       01  CA-AREA.\n           05  CA-KEY         PIC X(8).\n"
QXPGM = program("QXPGM", "QXTAG").replace("MOVE DUP-FIELD-A TO WS-OUT", "MOVE QX-NAME TO WS-OUT")


class InTheIndex(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        write_estate(cls.root, {
            "GC/PROD.GC.SRC/KCPGM.cbl": KCPGM,
            "GC/PROD.GC.COPYLIB/KCCOMM1.cpy": COMMAREA,
            "SHARED/PROD.CMN.COPYLIB/KCCOMM1.cpy": KCCOMM1_SHARED,
            "QX/PROD.QX.SRC/QXPGM.cbl": QXPGM,
            "QX/PROD.QX.COPYLIB/QXTAG.cpy": TAGGED,
            "QX/PROD.QX.COPYLIB/XQNAICS.cpy": NAICS,
            "QX/PROD.QX.SRC/KVSPLIT.cbl": SPLIT_PROG,
            "QX/PROD.QX.COPYLIB/KVBOOK.cpy": KVBOOK,
            "QX/PROD.QX.SCREENS/XQFM3.txt": MFS_CONTINUED,
            # a literal copied into a VALUE clause, filed cobol by its SRC folder: no program - its DIVISION words are text
            "QX/PROD.QX.SRC/KVLIT.cbl": "      * THE MESSAGE, COPIED INTO A VALUE CLAUSE\n"
                                        "               'DATA DIVISION MISSING'.\n",
        })
        cls.db = os.path.join(cls.td, "t.db")
        build_it(cls.root, cls.db, rebuild=True)
        cls.conn = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def test_the_kinds(self):
        kinds = dict(self.q("SELECT name || '@' || library, kind FROM member"))
        self.assertEqual(kinds["KCCOMM1@PROD.GC.COPYLIB"], "copybook")
        self.assertEqual(kinds["QXTAG@PROD.QX.COPYLIB"], "copybook")
        self.assertEqual(kinds["XQNAICS@PROD.QX.COPYLIB"], "copybook")
        self.assertEqual(kinds["XQFM3@PROD.QX.SCREENS"], "mfs")
        self.assertEqual(self.q("SELECT program_id FROM program WHERE program_id='PIC'"), [])
        self.assertEqual(self.q("SELECT kind, parse_status, parse_error FROM member WHERE name='KVLIT'"),
                         [("cobol", "skipped", "no PROGRAM-ID and no DIVISION header - not a program")])
        self.assertEqual(self.q("SELECT COUNT(*) FROM program WHERE program_id='KVLIT'"), [(0,)])

    def test_the_program_expands_its_own_systems_commarea_and_says_the_choice(self):
        # Wrong answer guarded: SHARED's KCCOMM1 expanded, no 'ambiguous_copybook' row, CA-PROGRAM-ID unknown
        note = self.q("SELECT u.detail FROM unresolved u JOIN member m ON m.id=u.member_id "
                      "WHERE m.name='KCPGM' AND u.kind='ambiguous_copybook'")
        self.assertEqual(len(note), 1, note)
        self.assertIn(os.path.join("GC", "PROD.GC.COPYLIB", "KCCOMM1.cpy") + " (same system)", note[0][0])
        self.assertEqual(self.q("SELECT COUNT(*) FROM pfield f JOIN program p ON p.id=f.program_id "
                                "WHERE p.program_id='KCPGM' AND f.name='CA-PROGRAM-ID'"), [(1,)])
        self.assertEqual(self.q("SELECT parse_status FROM member WHERE name='KCPGM'"), [("ok",)])

    def test_the_tagged_copybook_is_expanded(self):
        self.assertEqual(self.q("SELECT parse_status FROM member WHERE name='QXPGM'"), [("ok",)])
        self.assertEqual(self.q("SELECT r.name FROM copy_use c JOIN member m ON m.id=c.member_id "
                                "JOIN member r ON r.id=c.resolved_member_id WHERE m.name='QXPGM'"), [("QXTAG",)])

    def test_the_split_copy_is_expanded(self):
        self.assertEqual(self.q("SELECT c.copybook, c.line, r.name FROM copy_use c JOIN member m ON m.id=c.member_id "
                                "LEFT JOIN member r ON r.id=c.resolved_member_id WHERE m.name='KVSPLIT'"),
                         [("KVBOOK", 6, "KVBOOK")])
        self.assertEqual(self.q("SELECT COUNT(*) FROM pfield f JOIN program p ON p.id=f.program_id "
                                "WHERE p.program_id='KVSPLIT' AND f.name='KV-KEY'"), [(1,)])


# ===========================================================================
# LESSONS 249 - a kept choice among several, asked again
# ===========================================================================

def full_parse_of(db, td):
    """A copy of `db` whose programs are all parsed again by one more build: what the index holds when nothing is
    kept (a --rebuild deletes the listing rows, so the programs are parsed again instead)."""
    other = os.path.join(td, "full.db")
    shutil.copy(db, other)
    conn = sqlite3.connect(other)
    try:
        conn.execute("UPDATE member SET parse_status='pending' WHERE kind='cobol'")
        conn.commit()
    finally:
        conn.close()
    return other


class _Listings(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        self.db = os.path.join(self.td, "t.db")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def recover(self):
        said = []
        recover.run(self.db, log=said.append, report=os.path.join(self.td, "work", "recover.md"))
        return "\n".join(said)

    def build(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = build._main([self.root, "--db", self.db])
        self.assertEqual(rc, 0, buf.getvalue())
        return buf.getvalue()

    def pick(self, name, system=None):
        conn = sqlite3.connect(self.db)
        try:
            if system is None:
                return pick_of(conn, name)
            mid = conn.execute("SELECT id FROM member WHERE name=? AND kind='cobol' AND system=?", (name, system)).fetchone()[0]
            note = conn.execute("SELECT detail FROM unresolved WHERE member_id=? AND kind='ambiguous_copybook'",
                                (mid,)).fetchone()
            row = conn.execute("SELECT r.path FROM copy_use u JOIN member r ON r.id = u.resolved_member_id "
                               "WHERE u.member_id=? AND UPPER(u.copybook)='DUPREC'", (mid,)).fetchone()
            return (note[0] if note else None), (row[0] if row else None)
        finally:
            conn.close()

    def assert_as_full(self):
        full = full_parse_of(self.db, self.td)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build._main([self.root, "--db", full]), 0)
        self.assertEqual(facts_differ(facts(self.db), facts(full)), {})

    def assert_settled(self):
        out = self.build()
        self.assertIn("to parse: nothing", out)
        self.assertNotIn("parsed again: the compiler listings", out)

    def path(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def move(self, a, b):
        os.makedirs(os.path.dirname(self.path(b)), exist_ok=True)
        shutil.move(self.path(a), self.path(b))


class TheOwnListingArrivesAfterASharedOne(_Listings):
    """ARRNH in CLAIMS; its listing filed under SHARED names POLICY's copy of DUPREC, and the build follows it. Then
    its own current listing arrives in CLAIMS naming a staging library the index does not hold: its own listing
    decides (item 19) and keeps the chain's copy - CLAIMS's. The build kept POLICY's (LESSONS 249)."""

    def setUp(self):
        super().setUp()
        write_estate(self.root, {
            "CLAIMS/PROD.CLAIMS.SRC/ARRNH.cbl": program("ARRNH", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.SRC/ARRCL.cbl": program("ARRCL", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "SHARED/PROD.LISTINGS/ARRNH.lst": listing("ARRNH", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB")]),
            "SHARED/PROD.LISTINGS/ARRCL.lst": listing("ARRCL", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB")]),
        })
        build_it(self.root, self.db, rebuild=True)
        self.recover()
        self.build()
        self.claims = self.path("CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy")
        self.policy = self.path("POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy")
        for name in ("ARRNH", "ARRCL"):
            note, used = self.pick(name)
            self.assertEqual(used, self.policy)
            self.assertTrue(note.endswith(f"({LISTING}PROD.POLICY.COPYLIB)"), note)

    def test_its_own_listing_decides_at_the_next_build(self):
        write_estate(self.root, {
            "CLAIMS/PROD.CLAIMS.LISTING/ARRNH.lst": listing("ARRNH", ["DUPREC"], [("DUPREC", "SYSLIB", "STG.GONE.COPYLIB")]),
            "CLAIMS/PROD.CLAIMS.LISTING/ARRCL.lst": listing("ARRCL", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB")]),
        })
        self.build()
        self.recover()
        out = self.build()
        # Wrong answer guarded: ARRNH kept POLICY's copy (the SHARED listing's word) - a full parse gives CLAIMS's
        note, used = self.pick("ARRNH")
        self.assertEqual(used, self.claims)
        self.assertTrue(note.endswith("(same system)"), note)
        note, used = self.pick("ARRCL")                                      # recover marked it: contradicted
        self.assertEqual(used, self.claims)
        self.assertTrue(note.endswith(f"({LISTING}PROD.CLAIMS.COPYLIB)"), note)
        self.assertIn("1 program(s) parsed again: the compiler listings now decide a copybook chosen among several "
                      "differently", out)
        self.assert_as_full()
        self.assert_settled()

    def test_the_build_before_the_fix_kept_the_shared_listings_copy(self):
        write_estate(self.root, {
            "CLAIMS/PROD.CLAIMS.LISTING/ARRNH.lst": listing("ARRNH", ["DUPREC"], [("DUPREC", "SYSLIB", "STG.GONE.COPYLIB")]),
        })
        self.build()
        self.recover()
        with mock.patch.object(build, "picks_moved", lambda ctx: []):
            self.build()
        self.assertEqual(self.pick("ARRNH")[1], self.policy)


class TheListingLeavesWhileATwinExists(_Listings):
    """KVTPGM in KVA and in KVB, each system with its own DUPREC; KVA's current listing names KVB's library and the
    build follows it. The listing is then moved to SHARED\\LISTINGS, or deleted: it no longer speaks for KVA's
    program (a twin exists, so only KVA's own listings do), and a full parse gives KVA's own copy. No member KVTPGM
    copies moved, so nothing was parsed again (LESSONS 249)."""

    LST = "KVA/TST.KVA.LISTING/KVTPGM.lst"

    def setUp(self):
        super().setUp()
        write_estate(self.root, {
            "KVA/TST.KVA.SRC/KVTPGM.cbl": program("KVTPGM", "DUPREC"),
            "KVB/TST.KVB.SRC/KVTPGM.cbl": program("KVTPGM", "DUPREC"),
            "KVA/TST.KVA.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "KVB/TST.KVB.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            self.LST: listing("KVTPGM", ["DUPREC"], [("DUPREC", "SYSLIB", "TST.KVB.COPYLIB")]),
        })
        build_it(self.root, self.db, rebuild=True)
        self.recover()
        self.build()
        self.kva = self.path("KVA/TST.KVA.COPYLIB/DUPREC.cpy")
        self.kvb = self.path("KVB/TST.KVB.COPYLIB/DUPREC.cpy")
        note, used = self.pick("KVTPGM", "KVA")
        self.assertEqual(used, self.kvb)
        self.assertTrue(note.endswith(f"({LISTING}TST.KVB.COPYLIB)"), note)

    def check_back_to_its_own(self):
        note, used = self.pick("KVTPGM", "KVA")
        self.assertEqual(used, self.kva)
        self.assertTrue(note.endswith("(same system)"), note)
        self.assert_as_full()

    def test_moved_to_shared(self):
        self.move(self.LST, "SHARED/LISTINGS/KVTPGM.lst")
        out = self.build()
        # Wrong answer guarded: KVA's KVTPGM kept KVB's copy, and recover said 'unknown'
        self.assertIn("1 program(s) parsed again", out)
        self.check_back_to_its_own()
        self.recover()                                                       # it reads the listing where it now is
        self.check_back_to_its_own()
        self.assert_settled()

    def test_deleted(self):
        os.remove(self.path(self.LST))
        self.build()
        self.check_back_to_its_own()
        self.assert_settled()


class PicksMoved(_Listings):
    """build.picks_moved itself."""

    def setUp(self):
        super().setUp()
        write_estate(self.root, {
            "CLAIMS/PROD.CLAIMS.SRC/KVONE.cbl": program("KVONE", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.SRC/KVPLAIN.cbl": program("KVPLAIN", "NOPE"),
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "CLAIMS/PROD.CLAIMS.COPYLIB/NOPE.cpy": "           05  NOPE-FIELD    PIC X(3).\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
        })
        build_it(self.root, self.db, rebuild=True)

    def moved(self):
        """picks_moved over the index as the next build finds it (every member kept)."""
        conn = build.open_db(self.db)
        try:
            ctx = build.Ctx(conn, quiet=True)
            with contextlib.redirect_stdout(io.StringIO()):
                build.inventory(ctx, [self.root])
                build.derive_systems(ctx, self.root)
            ctx.listing_sources = build.load_listing_sources(conn)
            ctx.folder_dataset = build.load_folder_datasets(conn)
            return sorted(m.name for m in build.picks_moved(ctx))
        finally:
            conn.rollback()
            conn.close()

    def test_nothing_moved_nothing_parsed(self):
        self.assertEqual(self.moved(), [])
        self.assert_settled()

    def test_a_listing_row_that_changes_the_pick(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(recover.COPY_SOURCE_TABLE)
            conn.execute("INSERT INTO listing_copy_source VALUES('KVONE','DUPREC','SYSLIB','PROD.POLICY.COPYLIB',?,"
                         "'2026-09-26',1,100)", (os.path.join(self.td, "from", "KVONE.lst"),))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.moved(), ["KVONE"])                            # KVPLAIN chose nothing: never asked
        out = self.build()
        self.assertIn("to parse: programs 1", out)
        note, _used = self.pick("KVONE")
        self.assertTrue(note.endswith(f"({LISTING}PROD.POLICY.COPYLIB)"), note)
        self.assertEqual(self.moved(), [])
        # an older compile's row, or one not dated, changes nothing
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE listing_copy_source SET current=0")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.moved(), ["KVONE"])                            # back to the chain's copy
        self.build()
        self.assertTrue(self.pick("KVONE")[0].endswith("(same system)"))
        self.assertEqual(self.moved(), [])

    def test_a_copier_of_the_program_is_parsed_with_it(self):
        # a member copying the program's name expanded the program's own lines, and clearing the program's facts
        # un-links its row: it is parsed again in the same build, never left 'ok' with a NULL row (LESSONS 188)
        write_estate(self.root, {"CLAIMS/PROD.CLAIMS.SRC/KVCALLER.cbl": program("KVCALLER", "KVONE")})
        self.build()
        self.assert_settled()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE unresolved SET detail = replace(detail, '(same system)', '(FIRST FOUND)') "
                         "WHERE kind='ambiguous_copybook' AND member_id=(SELECT id FROM member WHERE name='KVONE')")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.moved(), ["KVONE"])
        out = self.build()
        self.assertIn("1 program(s) parsed again: the compiler listings now decide a copybook chosen among several "
                      "differently (a listing read, moved or gone since they were parsed) - with 1 member(s) copying "
                      "them", out)
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute("SELECT r.name, m.parse_status FROM copy_use c JOIN member m ON m.id=c.member_id "
                               "LEFT JOIN member r ON r.id=c.resolved_member_id WHERE m.name='KVCALLER' "
                               "AND c.copybook='KVONE'").fetchall()
        finally:
            conn.close()
        self.assertEqual(row, [("KVONE", "ok")])
        self.assertEqual(self.moved(), [])
        self.assert_settled()


if __name__ == "__main__":
    unittest.main()
