r"""
What the library says, and the words about a declared kind (LESSONS 199).

ROADMAP re-parse items 20 and 22 made the COBOL statements type a procedure
copybook before the folder name and before the kind declared for its library.
The statements are words other texts use too, and the verifier's round found
what that cost before he ran it:

  * an Easytrieve program in a CNTL library (FILE, JOB INPUT, IF ... END-IF)
    and a Connect:Direct process (PROCESS SNODE=, IF (SENDIT = 0) THEN ...
    EIF) were filed as copybooks, also when the library was declared
    ctlcard: build._card_text never reads a copybook, so `job` lost the
    Easytrieve files and fields and the Connect:Direct datasets, and said
    the card member was not indexed;
  * an upper-case run book (PERFORM THE FOLLOWING STEPS) in DOCS, a member of
    a library declared doc and a Markdown design note quoting a paragraph
    were filed as copybooks and dropped out of `docs`;
  * an Assembler remark `CREATE TABLE OF RATES` made a member sql; a program
    or copybook whose literal names the compiler was filed as a listing; a
    REXX exec whose header sits six blanks in was no longer rexx;
  * recover and coverage said 'by its declared kind' for a member the build
    had retyped itself (a card member in a JCL folder), with no manifest; on
    an index built before the batch they told him to declare a library
    copybook that was declared already; and a program whole over an
    Assembler member in a library declared copybook said only 'ok'.

Now an Easytrieve program or a Connect:Direct process is never typed by a
COBOL signature; in a library of documents the COBOL statements count for
nothing, in a library of control cards two statement lines are needed, one of
a form no card language has; CREATE TABLE counts where a statement begins; the
compiler's banner at the start of a line; the build records the kinds it was
declared, recover reads them (or the manifest beside an older index), and a
declared copybook over a shape carries a note the pages print.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from atlas import classify, query, recover  # noqa: E402
from test_refiled_copybooks import (ASMBK, PLAINBK, PRE_BATCH, STARTBK, _Estate, data_program,  # noqa: E402
                                    section_program)

# ---- fictional members (the KW system) ---------------------------------------------------------------------------
EZT = ("FILE POLIN\n  IN-POL-ID     1  10 A\n  IN-POL-AMT   11   5 P 2\nFILE RPTOUT PRINTER\n"
       "W-TOTAL  W  8 P 2\n"
       "JOB INPUT POLIN\n  IF IN-POL-AMT > 1000\n    MOVE IN-POL-ID TO W-ID\n    PERFORM ADD-TOTAL\n    PRINT RPT1\n"
       "  END-IF\n"
       "ADD-TOTAL. PROC\n  W-TOTAL = W-TOTAL + IN-POL-AMT\nEND-PROC\n"
       "REPORT RPT1 PRINTER RPTOUT\n  LINE IN-POL-ID IN-POL-AMT\n")
EZT_JOB = ("//KWEZTJ  JOB (ACCT),'KW EZT',CLASS=A\n//S010    EXEC PGM=EZTPA00\n"
           "//POLIN    DD DSN=KW.POLICY.MASTER,DISP=SHR\n//RPTOUT   DD SYSOUT=*\n"
           "//SYSIN    DD DSN=KW.PROD.CNTL(KWEZT1),DISP=SHR\n")
NDM = ("KWNDM1   PROCESS SNODE=KWREMOTE\n"
       "SENDIT   COPY FROM (DSN=KW.POLICY.EXTRACT PNODE DISP=SHR) -\n"
       "              TO (DSN=/data/in/policy.dat SNODE DISP=RPL)\n"
       "         IF (SENDIT = 0) THEN\n"
       "           RUN TASK (PGM=DMRTSUB) PNODE\n"
       "         EIF\n")
# the label STEP01 read as level 01 by the loose level-number signature, before this batch too
NDM_STEP01 = NDM.replace("SENDIT", "STEP01").replace("KWNDM1", "KWNDM2")
NDM_JOB = ("//KWNDMJ  JOB (ACCT),'KW NDM',CLASS=A\n//S010    EXEC PGM=DMBATCH,PARM=(YYSLYNN)\n"
           "//DMNETMAP DD DSN=KW.NDM.NETMAP,DISP=SHR\n//DMPRINT  DD SYSOUT=*\n//SYSIN    DD *\n"
           "  SIGNON USERID=(XXXX,YYYY)\n  SUBMIT PROC=KWNDM1 CASE=YES\n  SUBMIT PROC=KWNDM2 CASE=YES\n  SIGNOFF\n/*\n")
RUNBOOK = ("KW NIGHTLY RESTART INSTRUCTIONS\n\n    IF THE JOB ABENDS WITH S0C7, CALL THE ON-CALL ANALYST.\n"
           "    PERFORM THE FOLLOWING STEPS:\n      01 CANCEL THE JOB.\n      02 RESTART FROM STEP S020.\n")
RESTART = "RESTART CHECKLIST\n    MOVE THE FAILED GENERATION TO THE HOLD QUEUE.\n    PERFORM A RESTART FROM S020.\n"
NOTES = ("# Policy update - design notes\n\nThe edit paragraph does this:\n\n```\n       2000-EDIT.\n"
         "           IF POL-STATUS = 'A'\n              PERFORM 2100-ACTIVE\n           END-IF.\n```\n\n"
         "Restart it from the paragraph after a failure.\n")
# a procedure copybook in the CNTL library: still a copybook (ROADMAP item 20)
PRCB = "       A-110-DO.\n           IF WS-X = 1\n              MOVE 2 TO WS-Y\n           END-IF.\n"
SRTCARD = "  SORT FIELDS=(1,10,CH,A)\n"


def kind(path, text, declared=None):
    return classify.classify("C:/estate/" + path, text, declared=declared)[0]


class TheClassifierReadsTheLibrary(unittest.TestCase):
    """classify.classify directly."""

    def test_easytrieve_and_connect_direct_are_never_copybooks(self):
        for text in (EZT, NDM, NDM_STEP01):
            self.assertEqual(kind("KW/KW.PROD.CNTL/X.txt", text), "ctlcard", text)
            self.assertEqual(kind("KW/KW.PROD.EZTLIB/X.txt", text, "ctlcard"), "ctlcard", text)
            self.assertEqual(kind("KW/KW.PROD.EZTLIB/X.txt", text), "unknown", text)     # no hint: as before the batch
            self.assertEqual(kind("KW/KW.PROD.COPYLIB/X.txt", text), "copybook", text)   # the folder still types it
        self.assertEqual(classify.not_cobol(EZT), classify.EZT_WORDS)
        self.assertEqual(classify.not_cobol(NDM), classify.NDM_WORDS)
        self.assertIn("an Easytrieve program, whose statements are not COBOL",
                      classify.classify("C:/estate/KW/KW.PROD.CNTL/X.txt", EZT)[1])
        # an Easytrieve FILE statement counts only with an Easytrieve field definition: a COBOL FILE STATUS clause is
        # no Easytrieve, nor is ICETOOL's COPY FROM(IN)
        for text in ("           SELECT CUSTFILE ASSIGN TO CUSTDD\n               FILE STATUS IS WS-FS.\n"
                     "           MOVE 1 TO WS-X.\n", STARTBK, PLAINBK, "  COPY FROM(IN) TO(OUT)\n"):
            self.assertEqual(classify.not_cobol(text), "", text)

    def test_documents(self):
        for path, text, declared in (("DOCS/RUNBOOK.txt", RUNBOOK, None), ("KW/KW.PROD.DOCLIB/RESTART.txt", RESTART, "doc"),
                                     ("docsroot/DESIGN/KWNOTES.md", NOTES, None), ("notes/KWNOTES.md", NOTES, None),
                                     ("ops/restart.txt", "Restart notes\n\n    IF THE STEP FAILS, RESUBMIT FROM STEP020\n",
                                      None),
                                     ("ops/list.txt", "Checklist\n  1.   VERIFY.\n", None),
                                     ("DOCS/RB2.txt", "STEP 1 PERFORM A BACKUP OF THE FILE\nSTEP 2 MOVE THE DATASET TO PROD\n",
                                      None)):
            self.assertEqual(kind(path, text, declared), "doc", (path, text))
        # a procedure copybook downloaded as .txt into a plain folder, no line of prose: a copybook (item 20)
        self.assertEqual(kind("handfetch/PRCB.txt", PRCB), "copybook")
        self.assertEqual(kind("handfetch/PRCB.txt", "           ADD 1 TO WS-X.\n"), "copybook")
        # a data description entry is no English: level numbers still type a copybook in DOCS, as before the batch
        self.assertEqual(kind("DOCS/LAYOUT.txt", STARTBK), "copybook")
        # a copybook declared copybook in a DOCS folder: the declaration speaks for the library
        self.assertEqual(kind("DOCS/PRCB.txt", PRCB, "copybook"), "copybook")

    def test_card_libraries(self):
        # one statement line, or statements every card language shares: the folder's kind (the declared one)
        for text in ("           MOVE WS-A TO WS-B.\n", "       END.\n", "01 20260925\n",
                     "  DISPLAY QLOCAL(KW.REQ.QUEUE)\n  DISPLAY CHANNEL(KW.SVRCONN)\n",
                     "  IF LASTCC = 8 THEN SET MAXCC = 0\n"):
            self.assertEqual(kind("KW/KW.PROD.CNTL/X.txt", text), "ctlcard", text)
            self.assertEqual(kind("KW/KW.PROD.MISC/X.txt", text, "ctlcard"), "ctlcard", text)
        # a procedure copybook: two statement lines, one of a form no card language has (ROADMAP item 20)
        for text in (PRCB, PLAINBK, "       A-100.\n           CALL 'KWSUB' USING WS-A.\n",
                     "           PERFORM A-100.\n           PERFORM A-200.\n"):
            self.assertEqual(kind("KW/KW.PROD.CNTL/X.txt", text), "copybook", text)
            self.assertEqual(kind("KW/KW.PROD.MISC/X.txt", text, "ctlcard"), "copybook", text)
        # elsewhere one statement is enough, as the batch made it
        self.assertEqual(kind("KW/KW.PROD.MISC/X.txt", "           MOVE WS-A TO WS-B.\n"), "copybook")
        self.assertEqual(kind("KW/KW.PROD.PROCS/X.txt", "           MOVE WS-A TO WS-B.\n"), "copybook")
        # 'cobol', the UI table's default, says nothing about the library: the folder name does
        self.assertEqual(kind("KW/KW.PROD.CNTL/X.txt", "           MOVE WS-A TO WS-B.\n", "cobol"), "ctlcard")
        self.assertEqual(kind("KW/KW.PROD.MISC/X.txt", "           MOVE WS-A TO WS-B.\n", "cobol"), "copybook")

    def test_create_table_where_a_statement_begins(self):
        for text in ("ASMCT    CSECT\n         USING *,15\n         BAL   14,BLDTAB          CREATE TABLE OF RATES\n"
                     "         BR    14\nBLDTAB   DS    0H\n         END\n",
                     "ASMCT2   CSECT\n         USING *,15\n         BAL   14,BLDIX           create index entries\n"
                     "         BR    14\n         END\n"):
            self.assertEqual(kind("KW/KW.PROD.ASM/X.txt", text), "asm", text)
        self.assertEqual(kind("KW/KW.PROD.COPYLIB/X.txt", "       01  KW-REC.\n           05 KW-TXT PIC X(12) VALUE "
                                                           "'CREATE TABLE'.\n"), "copybook")
        for text in ("  CREATE TABLE KW.POLICY\n   (POL_ID CHAR(10) NOT NULL);\n", "CREATE UNIQUE INDEX KW.X1 ON KW.P (A);\n",
                     "DROP TABLE KW.T; CREATE TABLE KW.T (A INTEGER);\n", "  create view kw.v as select * from kw.t;\n",
                     "  CREATE\n    TABLESPACE KWTS IN KWDB;\n"):
            self.assertEqual(kind("KW/KW.PROD.DDL/X.txt", text), "sql", text)

    def test_the_compiler_named_is_no_banner(self):
        self.assertEqual(kind("KW/KW.PROD.SRC/X.txt", "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. KWPGC.\n"
                                                      "       PROCEDURE DIVISION.\n           DISPLAY 'BUILT WITH IBM "
                                                      "ENTERPRISE COBOL'.\n           GOBACK.\n"), "cobol")
        self.assertEqual(kind("KW/KW.PROD.COPYLIB/X.txt", "       01  KW-REC.\n           05 FILLER PIC X(24) VALUE "
                                                          "'IBM ENTERPRISE COBOL V6'.\n"), "copybook")
        for banner in ("1PP 5655-EC6 IBM Enterprise COBOL for z/OS  6.3.0\n", "PP 5655-S71 IBM Enterprise COBOL for z/OS\n",
                       " 1PP 5655-EC6 IBM Enterprise COBOL for z/OS\n", "\f1PP 5655-EC6 IBM Enterprise COBOL for z/OS\n"):
            self.assertEqual(kind("KW/KW.PROD.LISTING/X.txt", banner + STARTBK), "listing", banner)
        # atlas.recover, reading a text known to be a listing, still takes the compiler's name for its banner
        self.assertTrue(recover._LISTING_HEAD.search("   IBM Enterprise COBOL for z/OS 6.3   KWPGM   Page 2"))

    def test_a_rexx_header_six_blanks_in(self):
        for text in ("      /* REXX */\n  SAY 'HI'\n  EXIT 0\n", "      /* REXX */\n  ARG DSN\n  CALL SUB\n  EXIT 0\n"):
            self.assertEqual(kind("KW/KW.PROD.EXEC/X.txt", text), "rexx", text)
        for text in ("      /* REXX STYLE BANNER\n" + STARTBK, "      /* REXX */\n" + PLAINBK):
            self.assertEqual(kind("KW/KW.PROD.COPYLIB/X.txt", text), "copybook", text)
        self.assertEqual(kind("KW/KW.PROD.EXEC/X.txt", "/* REXX */\n  SAY 'HI'\n"), "rexx")


class JobsReadTheirCards(_Estate):
    """The build: an Easytrieve program and two Connect:Direct processes in a CNTL library declared ctlcard are
    control cards, read by their jobs; a procedure copybook beside them is a copybook; a run book, a member of a
    library declared doc and a Markdown design note are documents; recover finds nothing to do."""

    files = (("KW/KW.PROD.JCL/KWEZTJ.jcl", EZT_JOB), ("KW/KW.PROD.CNTL/KWEZT1.txt", EZT),
             ("KW/KW.PROD.JCL/KWNDMJ.jcl", NDM_JOB), ("KW/KW.PROD.CNTL/KWNDM1.txt", NDM),
             ("KW/KW.PROD.CNTL/KWNDM2.txt", NDM_STEP01),
             ("KW/KW.PROD.SRC/KWPGM1.cbl", section_program("KWPGM1", "KWPRCB")), ("KW/KW.PROD.CNTL/KWPRCB.txt", PRCB),
             ("DOCS/RUNBOOK.txt", RUNBOOK), ("SHARED/KW.PROD.DOCLIB/RESTART.txt", RESTART),
             ("DESIGN/KWNOTES.md", NOTES))

    def before_build(self):
        self.manifest = os.path.join(self.td, "manifest.json")
        with open(self.manifest, "w", encoding="utf-8") as fh:
            json.dump({"kinds": {"KW.PROD.CNTL": "ctlcard", "KW.PROD.DOCLIB": "doc", "KW.PROD.JCL": "jcl"}}, fh)

    def test_the_jobs_read_them(self):
        for name in ("KWEZT1", "KWNDM1", "KWNDM2"):
            self.assertEqual(self.member(name)[:2], ("ctlcard", "KW.PROD.CNTL"), name)
        self.assertEqual(self.member("KWPRCB")[:3], ("copybook", "KW.PROD.CNTL", "ok"))
        self.assertEqual(self.status("KWPGM1"), "ok")
        conn = query.connect(self.db)
        try:
            ezt = query.cmd_job(conn, "KWEZTJ")
            ndm = query.cmd_job(conn, "KWNDMJ")
            ds = query.cmd_dataset(conn, "KW.POLICY.EXTRACT")
        finally:
            conn.close()
        self.assertIn("Easytrieve program in SYSIN: files and byte-position fields harvested", ezt)
        self.assertIn("easytrieve_file", ezt)
        self.assertNotIn("Easytrieve with no SYSIN program text", ezt)
        self.assertNotIn("NOT indexed", ezt)
        for name in ("KWNDM1", "KWNDM2"):
            self.assertIn(f"Connect:Direct process {name} read from its member", ndm)
            self.assertNotIn(f"process member {name} not indexed", ndm)
        self.assertIn("ndm_process", ds)
        self.assertIn("**Crosses the mainframe boundary**: ndm out", ds)
        self.assert_nothing_to_do()

    def test_the_documents_are_documents(self):
        for name in ("RUNBOOK", "RESTART", "KWNOTES"):
            self.assertEqual(self.member(name)[0], "doc", name)
        conn = query.connect(self.db)
        try:
            found = query.cmd_docs(conn, "RESTART")
            cov = query.cmd_coverage(conn)
        finally:
            conn.close()
        for name in ("RUNBOOK", "RESTART", "KWNOTES"):
            self.assertIn(f"## {name}  `", found)
        self.assertIn("| documents | 3 |", cov)


class TheBuildsOwnRuleIsNoDeclaration(_Estate):
    """A card member in a JCL-named folder (no JOB, EXEC or PROC statement) is retyped ctlcard by the build itself:
    with no manifest at all, nothing may say a declared kind decided it - the folder sentence, as before the batch.
    Declared ctlcard in the UI's table, the declaration did."""

    files = (("KW/KW.PROD.SRC/KWP20.cbl", data_program("KWP20", "SRTCARD")), ("KW/KW.PROD.JCL/SRTCARD.txt", SRTCARD))
    SEEN = ("the folder name ends in JCL and it holds no JOB, EXEC or PROC statement, so the build filed it as a "
            "control-card member")

    def test_no_manifest(self):
        self.assert_not_found("KWP20", "SRTCARD", "ctlcard", "KW.PROD.JCL")
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.declared_kinds_used(conn), {})
            r = recover.member_readings(recover.members_named(conn, "SRTCARD")[1], conn)[0]
            self.assertEqual((r["kind"], r["by"], r["seen"], r["declared_known"]), ("ctlcard", "folder", self.SEEN, True))
            self.assertEqual(recover.folder_fix(r), recover.MISFILED_FIX)
            cov = query.cmd_coverage(conn)
        finally:
            conn.close()
        self.assertIn("| SRTCARD | 1 | yes: KW.PROD.JCL, filed as ctlcard - not a kind the build expands: rename the folder "
                      "to end in COPYLIB or declare its kind in the UI, then build |", cov)
        self.assertNotIn("declared kind", cov)
        stats, said = self.recover()
        self.assertNotIn("declared kind", self.report_text())

    def test_declared_ctlcard(self):
        self.manifest = os.path.join(self.td, "manifest.json")
        with open(self.manifest, "w", encoding="utf-8") as fh:
            json.dump({"kinds": {"KW.PROD.JCL": "ctlcard"}}, fh)
        self.build()
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.declared_kinds_used(conn), {"KW.PROD.JCL": "ctlcard"})
            r = recover.member_readings(recover.members_named(conn, "SRTCARD")[1], conn)[0]
            self.assertEqual((r["kind"], r["by"], r["over"]), ("ctlcard", "declared", "folder"))
            self.assertIn(f"filed as ctlcard by its declared kind - not a kind the build expands: "
                          f"{recover.DECLARED_OVER_CELL}", query.cmd_coverage(conn))
        finally:
            conn.close()


class ADeclaredCopybookOverAShape(_Estate):
    """ASMBK, a real Assembler member, in a library declared copybook: the declared kind wins over the shape (ROADMAP
    item 22 (c)), the program copying it is whole - and `program`, `copybook` and `coverage` say it is a member of
    an Assembler shape (the 'declared_kind' row). On an index built before the batch - which let a declared kind
    replace 'unknown' only, so ASMBK stayed asm - recover reads the manifest beside the index and re-files it with
    words that say why; with the manifest elsewhere, it cannot tell, and says so instead of 'declare it'."""

    files = (("KW/KW.PROD.SRC/ASMPGM.cbl", data_program("ASMPGM", "ASMBK")), ("SHARED/KW.PROD.ASMCPY/ASMBK.txt", ASMBK),
             ("KW/KW.PROD.SRC/STPGM.cbl", data_program("STPGM", "STARTBK", "START-DATE")),
             ("SHARED/KW.PROD.COPYLIB/STARTBK.txt", STARTBK))
    NOTE = ("filed copybook by the kind declared for library KW.PROD.ASMCPY in the UI's table, over the shape of an "
            "Assembler member on line 1: every program copying it expands this text - if it is not the COBOL copybook they "
            "copy, correct the library's kind in the UI's table and run the build")

    def before_build(self):
        self.manifest = os.path.join(self.td, "manifest.json")
        with open(self.manifest, "w", encoding="utf-8") as fh:
            json.dump({"kinds": {"KW.PROD.ASMCPY": "copybook", "KW.PROD.COPYLIB": "copybook"}}, fh)

    def age_it(self):
        """ASMBK as the build before the batch filed it (asm), that build's run recorded no declared kinds."""
        self.age("ASMBK", "asm", fingerprint=PRE_BATCH)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE build_run SET declared_kinds=NULL")
            conn.commit()
        finally:
            conn.close()
        self.assert_not_found("ASMPGM", "ASMBK", "asm", "KW.PROD.ASMCPY")

    def test_a_fresh_build_says_it(self):
        self.assertEqual(self.member("ASMBK")[:3], ("copybook", "KW.PROD.ASMCPY", "ok"))
        self.assertEqual(self.status("ASMPGM"), "ok")
        self.assertEqual(self.q("SELECT kind, detail, line FROM unresolved WHERE member_id=?", self.member_id("ASMBK")),
                         [("declared_kind", self.NOTE, 1)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM unresolved WHERE kind='declared_kind'"), [(1,)])   # not STARTBK
        conn = query.connect(self.db)
        try:
            prog = query.cmd_program(conn, "ASMPGM")
            book = query.cmd_copybook(conn, "ASMBK")
            cov = query.cmd_coverage(conn)
            self.assertEqual(recover.declared_kinds_used(conn), {"KW.PROD.ASMCPY": "copybook", "KW.PROD.COPYLIB": "copybook"})
        finally:
            conn.close()
        self.assertIn("| ASMBK | ASMBK (filed copybook by its library's declared kind, over the shape of an Assembler member "
                      "on line 1 - see `copybook ASMBK`) |", prog)
        self.assertIn(f"**Declared copybook over a shape** (`{os.path.join(self.root, 'SHARED', 'KW.PROD.ASMCPY', 'ASMBK.txt')}`): "
                      f"{self.NOTE}.", book)
        self.assertIn("| declared_kind | 1 |", cov)
        self.assert_nothing_to_do()
        # a build that re-parses nothing keeps the row; one that re-parses every member writes it again, once
        self.build()
        self.build(["--rebuild"])
        self.assertEqual(self.q("SELECT COUNT(*) FROM unresolved WHERE kind='declared_kind'"), [(1,)])

    def test_an_older_index_with_its_manifest_beside_it(self):
        self.age_it()
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.declared_kinds_used(conn), {"KW.PROD.ASMCPY": "copybook", "KW.PROD.COPYLIB": "copybook"})
            r = recover.member_readings(recover.members_named(conn, "ASMBK")[1], conn)[0]
            self.assertEqual((r["kind"], r["by"], r["earlier"], r["declared_over"], r["refile"]),
                             ("asm", "content", "content", True, True))
            self.assertTrue(r["seen"].endswith("; its library KW.PROD.ASMCPY is declared copybook in the UI's table, which "
                                               "the build of this toolkit puts before asm (the shape of an Assembler member) "
                                               "- ROADMAP re-parse items 20 and 22"), r["seen"])
            self.assertEqual(recover.content_fix(r), recover.EARLIER_DECLARED_FIX)
            self.assertEqual(recover.content_fix(r, dry_run=True), recover.EARLIER_DECLARED_FIX_DRY)
        finally:
            conn.close()
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["marked"]), (1, 1), said)
        self.assertIn("the build of this toolkit files them as copybooks - by their own lines, or by the copybook kind "
                      "declared for their library in the UI's table, which it puts before a shape", said)
        self.assertNotIn("declare its library copybook", said + self.report_text())
        self.assertIn("its library is declared copybook in the UI's table, which the build of this toolkit puts before the "
                      "shape", self.member("ASMBK")[3])
        out = self.build()
        self.assertIn("changed 4  unchanged 0", out)                    # the toolkit changed: every member re-parsed
        self.assertEqual((self.member("ASMBK")[:3], self.status("ASMPGM")), (("copybook", "KW.PROD.ASMCPY", "ok"), "ok"))
        self.assertEqual(self.q("SELECT kind FROM unresolved WHERE member_id=?", self.member_id("ASMBK")), [("declared_kind",)])
        self.assert_nothing_to_do()

    def test_an_older_index_whose_manifest_is_elsewhere(self):
        elsewhere = os.path.join(self.td, "cfg", "manifest.json")
        os.makedirs(os.path.dirname(elsewhere))
        shutil.move(self.manifest, elsewhere)                           # the same bytes: the manifest did not change
        self.manifest = elsewhere
        self.age_it()
        conn = query.connect(self.db)
        try:
            self.assertIsNone(recover.declared_kinds_used(conn))
            r = recover.member_readings(recover.members_named(conn, "ASMBK")[1], conn)[0]
            self.assertEqual((r["kind"], r["by"], r["declared_known"], r["refile"]), ("asm", "content", False, False))
            self.assertTrue(r["why_not"].endswith("(a declared kind wins over the shape)" + recover.DECLARED_UNKNOWN),
                            r["why_not"])
            nf = query.cmd_coverage(conn).split("### Copybooks not found")[1].split("\n###")[0]
        finally:
            conn.close()
        self.assertIn(recover.DECLARED_UNKNOWN, nf)
        stats, said = self.recover()
        self.assertEqual(stats["refiled"], 0, said)
        self.assertIn("(a library declared copybook in the UI's table already needs only the build: this index predates "
                      "ROADMAP re-parse item 22 and its manifest.json is not beside it to tell)", said)
        self.assertIn(recover.DECLARED_UNKNOWN, self.report_text())
        self.build()
        self.assertEqual((self.member("ASMBK")[:3], self.status("ASMPGM")), (("copybook", "KW.PROD.ASMCPY", "ok"), "ok"))
        self.assert_nothing_to_do()


class DeclaredKindsUsed(unittest.TestCase):
    """recover.declared_kinds_used on the build_run rows each kind of index holds."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.db = os.path.join(self.td, "atlas.db")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def used(self, cols, row):
        if os.path.exists(self.db):
            os.remove(self.db)
        conn = sqlite3.connect(self.db)
        try:
            if cols:
                conn.execute(f"CREATE TABLE build_run (id INTEGER PRIMARY KEY, {', '.join(c + ' TEXT' for c in cols)})")
                if row is not None:
                    conn.execute(f"INSERT INTO build_run({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", row)
            conn.commit()
            return recover.declared_kinds_used(conn)
        finally:
            conn.close()

    def test_each_index(self):
        full = ("fingerprint", "manifest_sha", "declared_kinds")
        self.assertIsNone(self.used((), None))                                   # no build_run: nothing built
        self.assertIsNone(self.used(full, None))                                 # no run recorded
        self.assertEqual(self.used(full, ("fp", "abc", json.dumps({"kw.prod.copylib": "COPYBOOK", "X": "asm"}))),
                         {"KW.PROD.COPYLIB": "copybook"})                         # what a build of this batch recorded
        self.assertEqual(self.used(full, ("fp", None, "{}")), {})
        self.assertEqual(self.used(full, ("fp", None, None)), {})                 # an older build with no manifest
        self.assertEqual(self.used(("fingerprint", "manifest_sha"), ("fp", None)), {})
        self.assertIsNone(self.used(full, (None, None, None)))                   # older still: it recorded nothing
        data = json.dumps({"kinds": {"KW.PROD.ASMCPY": "copybook"}}).encode()
        sha = hashlib.sha256(data).hexdigest()[:16]
        self.assertIsNone(self.used(full, ("fp", sha, None)))                    # its manifest is not beside it
        with open(os.path.join(self.td, "manifest.json"), "wb") as fh:
            fh.write(data)
        self.assertEqual(self.used(full, ("fp", sha, None)), {"KW.PROD.ASMCPY": "copybook"})
        self.assertIsNone(self.used(full, ("fp", "0" * 16, None)))               # a manifest changed since: not that one


if __name__ == "__main__":
    unittest.main()
