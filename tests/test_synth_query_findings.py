"""
The synthetic estate's findings on the query side (docs/SYNTH-findings-2026-09-25.md;
the reproductions F12, F13 and F17 under tools/synth/repro, and F48 / F51 judged
against the generator). No fact module changes: every answer here reads the
tables an index already holds.

  F12 / report F27 (LESSONS 235) - `values FIELD` reported the length token of a
      sort card's condition (the 2 of `INCLUDE COND=(13,2,CH,EQ,C'AC')`) as a
      value of the field, flagged USED BUT NOT DOCUMENTED. Only the constant a
      condition on the field's bytes compares with is a value.
  F48 (LESSONS 236) - `program X`'s Files table listed a PROC's own step, read
      with its DEFAULT symbolics (TEST.*), beside the job's expanded step
      (PROD.*), while README says a bare PROC shows only when no indexed job
      expands it; `flow` branched into the same rows, `values` counted the
      PROC's default cards. The Files table also took a DD whose name only
      ends with the file's (OLDMAST for MAST).
  F13 / report F50 (LESSONS 237) - `interfaces` listed every FTP put twice: the
      pseudo-DD's row and the step's 'external interface via FTP' row.
  F17 / report F42 (LESSONS 238) - `crud --job JOB` gave a file the first
      dataset any job's step gave its DD: the matrix for job B named job A's file.
  F51 (LESSONS 239) - `flow FIELD --up` from a host variable a SELECT INTO
      fills reaches the DB2 column: the DCLGEN included over three lines is the
      program's (ROADMAP re-parse item 27), pinned here.

Every name here is fictional.
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
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query  # noqa: E402


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _build(root, db):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = build._main([root, "--db", db, "--rebuild", "--quiet"])
    assert rc == 0, buf.getvalue()


def _cbl(name, selects, fds, ws, proc):
    """A small batch program: SELECT lines, FD entries, WORKING-STORAGE, PROCEDURE lines."""
    return ("       IDENTIFICATION DIVISION.\n"
            f"       PROGRAM-ID. {name}.\n"
            "       ENVIRONMENT DIVISION.\n"
            "       INPUT-OUTPUT SECTION.\n"
            "       FILE-CONTROL.\n"
            + "".join(f"           SELECT {s} ASSIGN TO {d}.\n" for s, d in selects)
            + "       DATA DIVISION.\n"
            "       FILE SECTION.\n"
            + "".join(f"       FD  {f}.\n       01  {rec}.\n           05  {fld}  PIC X(04).\n" for f, rec, fld in fds)
            + "       WORKING-STORAGE SECTION.\n"
            + "".join(f"       01  {w}  PIC X(04).\n" for w in ws)
            + "       PROCEDURE DIVISION.\n"
            + "".join(f"           {p}\n" for p in proc)
            + "           GOBACK.\n")


def _table_rows(text, first):
    """The markdown rows whose first cell is `first`."""
    return [ln for ln in text.splitlines() if ln.startswith(f"| {first} |")]


# ===========================================================================
# F12 - a sort card's compared constant is the value; the length is not
# ===========================================================================

class SortCardCompares(unittest.TestCase):
    """query.sort_card_compares, card by card."""

    def test_the_length_is_no_value(self):
        self.assertEqual(query.sort_card_compares("  SORT FIELDS=(1,12,CH,A)\n  INCLUDE COND=(13,2,CH,EQ,C'AC')\n"),
                         [("INCLUDE", 13, 2, "CH", "AC")])

    def test_each_condition_with_its_own_bytes(self):
        deck = ("* KEEP ACTIVE AND IN-FORCE, NOT C'ZZ'\n"
                "  INCLUDE COND=(13,2,CH,EQ,C'AC',OR,\n"
                "               13,2,CH,EQ,C'IN',OR,1,12,CH,EQ,C'000000000000')\n"
                "  OMIT COND=(20,3,ZD,GT,+100,AND,1,2,CH,EQ,5,2,CH)\n"
                "  OUTFIL FNAMES=OUT1,INCLUDE=(13,2,SS,EQ,C'LP,CN')\n"
                "  INREC IFTHEN=(WHEN=(15,1,CH,EQ,C'O''B'),OVERLAY=(80:C'X'))\n"
                "  OMIT COND=(16,1,BI,EQ,X'01',OR,21,8,Y4T,GT,DATE1)\n")
        self.assertEqual(query.sort_card_compares(deck), [
            ("INCLUDE", 13, 2, "CH", "AC"), ("INCLUDE", 13, 2, "CH", "IN"),
            ("INCLUDE", 1, 12, "CH", "000000000000"),
            ("OMIT", 20, 3, "ZD", "+100"),                      # (1,2,CH,EQ,5,2,CH) compares other bytes
            ("OUTFIL", 13, 2, "SS", "LP"), ("OUTFIL", 13, 2, "SS", "CN"),
            ("INREC", 15, 1, "CH", "O'B")])                     # the OVERLAY constant is no comparison; X'01', DATE1 none

    def test_format_given_once(self):
        self.assertEqual(query.sort_card_compares("  INCLUDE COND=(13,2,EQ,C'AC'),FORMAT=CH\n"),
                         [("INCLUDE", 13, 2, "CH", "AC")])

    def test_symbols_for_the_bytes_and_for_the_constant(self):
        deck = ("KV-STATUS,13,2,CH\nKV-ACTIVE,C'AC'\n"
                "  INCLUDE COND=(KV-STATUS,EQ,KV-ACTIVE,OR,KV-STATUS,EQ,C'LP')\n"
                "  OMIT COND=(KV-UNKNOWN,EQ,C'QQ')\n")          # a name no SYMNAMES defines: bytes unknown
        self.assertEqual(query.sort_card_compares(deck),
                         [("INCLUDE", 13, 2, "CH", "AC"), ("INCLUDE", 13, 2, "CH", "LP")])

    def test_a_card_past_120_characters_is_read_whole(self):
        deck = ("  INCLUDE COND=(1,2,CH,EQ,C'AA',OR,1,2,CH,EQ,C'BB',OR,\n"
                "               1,2,CH,EQ,C'CC',OR,1,2,CH,EQ,C'DD',OR,\n"
                "               13,2,CH,EQ,C'EE',OR,13,2,CH,EQ,C'FF')\n")
        got = query.sort_card_compares(deck)
        self.assertIn(("INCLUDE", 13, 2, "CH", "FF"), got)
        self.assertEqual(len(got), 6)

    def test_nothing_to_compare(self):
        self.assertEqual(query.sort_card_compares(""), [])
        self.assertEqual(query.sort_card_compares("  SORT FIELDS=(1,12,CH,A)\n  SUM FIELDS=NONE\n"), [])


KV_REC = ("       01  KV-REC.\n"
          "           05  KV-KEY              PIC X(12).\n"
          "           05  KV-STATUS           PIC X(02).\n"
          "               88  KV-ACTIVE       VALUE 'AC'.\n"
          "           05  KV-TYPE             PIC X(01).\n")
VAL_SORT_PROC = ("//KVVSRT   PROC CARDS=KVVDEF\n"
                 "//SRT      EXEC PGM=SORT\n"
                 "//SORTIN   DD   DSN=PROD.KV.IN,DISP=SHR\n"
                 "//SORTOUT  DD   DSN=PROD.KV.OUT,DISP=(NEW,CATLG,DELETE)\n"
                 "//SYSIN    DD   DSN=PROD.CMN.PARMLIB(&CARDS),DISP=SHR\n")
VAL_LONE_PROC = ("//KVVLONE  PROC\n"
                 "//LN010    EXEC PGM=SORT\n"
                 "//SORTIN   DD   DSN=PROD.KV.LONE.IN,DISP=SHR\n"
                 "//SORTOUT  DD   DSN=PROD.KV.LONE.OUT,DISP=(NEW,CATLG,DELETE)\n"
                 "//SYSIN    DD   DSN=PROD.CMN.PARMLIB(KVVLNC),DISP=SHR\n")


class ValuesFromSortCards(unittest.TestCase):
    """`values KV-STATUS` over a job's instream cards, a PROC a job runs with its own card member, and a PROC no
    job runs."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate")
        _write(root, "POLICY/PROD.KV.COPYLIB/KVREC.cpy", KV_REC)
        _write(root, "POLICY/PROD.KV.JCLLIB/KVVJOB1.jcl",
               "//KVVJOB1  JOB (ACCT),'SORT',CLASS=A\n//STEP010  EXEC PGM=SORT\n"
               "//SORTIN   DD   DSN=PROD.KV.IN,DISP=SHR\n//SORTOUT  DD   DSN=PROD.KV.OUT,DISP=(NEW,CATLG,DELETE)\n"
               "//SYSIN    DD   *\n  SORT FIELDS=(1,12,CH,A)\n"
               "  INCLUDE COND=(13,2,CH,EQ,C'AC',OR,1,12,CH,EQ,C'000000000000')\n/*\n//\n")
        _write(root, "POLICY/PROD.KV.JCLLIB/KVVJOB2.jcl",
               "//KVVJOB2  JOB (ACCT),'SORT',CLASS=A\n//S1       EXEC KVVSRT,CARDS=KVVJBC\n//\n")
        _write(root, "SHARED/PROD.CMN.PROCLIB/KVVSRT.prc", VAL_SORT_PROC)
        _write(root, "SHARED/PROD.CMN.PROCLIB/KVVLONE.prc", VAL_LONE_PROC)
        _write(root, "SHARED/PROD.CMN.PARMLIB/KVVDEF.ctl", "  SORT FIELDS=(1,12,CH,A)\n  INCLUDE COND=(13,2,CH,EQ,C'ZZ')\n")
        _write(root, "SHARED/PROD.CMN.PARMLIB/KVVJBC.ctl", "  SORT FIELDS=(1,12,CH,A)\n  INCLUDE COND=(13,2,CH,EQ,C'IN')\n")
        _write(root, "SHARED/PROD.CMN.PARMLIB/KVVLNC.ctl", "  SORT FIELDS=(1,12,CH,A)\n  OMIT COND=(13,2,CH,EQ,C'LN')\n")
        cls.db = os.path.join(cls.td, "t.db")
        _build(root, cls.db)
        cls.conn = query.connect(cls.db)
        cls.out = query.cmd_values(cls.conn, "KV-STATUS")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_the_cards_are_in_the_index(self):
        # the rows the old reading took its numbers from: (13,2) and (1,12) of one INCLUDE card
        got = {(r[0], r[1]) for r in self.conn.execute(
            "SELECT pos, length FROM card_field_ref c JOIN step s ON s.id=c.step_id JOIN job j ON j.id=s.job_id "
            "WHERE j.job_name='KVVJOB1' AND c.card_kind='INCLUDE'")}
        self.assertEqual(got, {(13, 2), (1, 12)})

    def test_no_position_or_length_is_a_value(self):
        for tok in ("2", "12", "13"):
            self.assertEqual(_table_rows(self.out, tok), [], self.out)
        self.assertNotIn("000000000000", self.out)             # compares KV-KEY's bytes, not KV-STATUS's

    def test_the_compared_constant_is(self):
        self.assertEqual(_table_rows(self.out, "AC"),
                         ["| AC | KV-ACTIVE | 0 | 0 | 0 | 1 | KVVJOB1.STEP010 INCLUDE |"])

    def test_the_job_s_card_member_and_not_the_proc_default(self):
        self.assertEqual(_table_rows(self.out, "IN"), ["| IN | (none) | 0 | 0 | 0 | 1 | KVVJOB2.S1.SRT INCLUDE |"])
        self.assertNotIn("ZZ", self.out)                        # KVVSRT's default KVVDEF: no job reads it

    def test_a_proc_no_job_runs_is_named_as_the_proc(self):
        self.assertEqual(_table_rows(self.out, "LN"), ["| LN | (none) | 0 | 0 | 0 | 1 | PROC KVVLONE LN010 OMIT |"])

    def test_undocumented_are_the_codes_the_cards_compare(self):
        self.assertIn("**USED BUT NOT DOCUMENTED BY ANY 88-LEVEL:** IN, LN - ", self.out)


# ===========================================================================
# F48 - a PROC's own step shows only when no indexed job expands the PROC
# ===========================================================================

PROC_PGM = _cbl("KVPPGM", [("OUT-FILE", "MAST"), ("OLD-FILE", "OLDMAST"), ("FIX-FILE", "FIXDD")],
                [("OUT-FILE", "OUT-REC", "OR-CODE"), ("OLD-FILE", "OLD-REC", "OD-CODE"),
                 ("FIX-FILE", "FIX-REC", "FX-CODE")],
                ["WS-CODE"],
                ["OPEN OUTPUT OUT-FILE FIX-FILE INPUT OLD-FILE.", "READ OLD-FILE.", "MOVE WS-CODE TO OR-CODE.",
                 "WRITE OUT-REC.", "MOVE WS-CODE TO FX-CODE.", "WRITE FIX-REC.", "CLOSE OUT-FILE OLD-FILE FIX-FILE."])
PROC_RDR = _cbl("KVPRDR", [("IN-FILE", "FIXIN")], [("IN-FILE", "IN-REC", "IR-CODE")], ["WS-GOT"],
                ["OPEN INPUT IN-FILE.", "READ IN-FILE.", "MOVE IR-CODE TO WS-GOT.", "DISPLAY WS-GOT.", "CLOSE IN-FILE."])
LONE_PGM = _cbl("KVPPGM2", [("L-FILE", "LONEDD")], [("L-FILE", "L-REC", "LR-CODE")], ["WS-LCODE"],
                ["OPEN OUTPUT L-FILE.", "MOVE WS-LCODE TO LR-CODE.", "WRITE L-REC.", "CLOSE L-FILE."])
PGM_PROC = ("//KVPPRC   PROC HLQ=TEST\n"
            "//P010     EXEC PGM=KVPPGM\n"
            "//MAST     DD   DSN=&HLQ..KV.MAST,DISP=(NEW,CATLG,DELETE)\n"
            "//OLDMAST  DD   DSN=&HLQ..KV.OLDMAST,DISP=SHR\n"
            "//FIXDD    DD   DSN=PROD.KV.FIXED,DISP=(NEW,CATLG,DELETE)\n")
PGM_LONE_PROC = ("//KVPLONE  PROC HLQ=TEST\n"
                 "//L010     EXEC PGM=KVPPGM2\n"
                 "//LONEDD   DD   DSN=&HLQ..KV.LONE,DISP=(NEW,CATLG,DELETE)\n")
PGM_JOB = ("//KVPJOB1  JOB (ACCT),'PROC',CLASS=A\n"
           "//S1       EXEC KVPPRC,HLQ=PROD\n"
           "//S2       EXEC PGM=KVPRDR\n"
           "//FIXIN    DD   DSN=PROD.KV.FIXED,DISP=SHR\n//\n")


class ProcDefaultsAreNoDatasets(unittest.TestCase):
    """KVPJOB1 runs KVPPRC with HLQ=PROD; KVPLONE (HLQ=TEST) is run by no job. `program`, `flow` (both walkers)."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate")
        _write(root, "POLICY/PROD.KV.COBOL/KVPPGM.cbl", PROC_PGM)
        _write(root, "POLICY/PROD.KV.COBOL/KVPRDR.cbl", PROC_RDR)
        _write(root, "POLICY/PROD.KV.COBOL/KVPPGM2.cbl", LONE_PGM)
        _write(root, "POLICY/PROD.KV.JCLLIB/KVPJOB1.jcl", PGM_JOB)
        _write(root, "SHARED/PROD.CMN.PROCLIB/KVPPRC.prc", PGM_PROC)
        _write(root, "SHARED/PROD.CMN.PROCLIB/KVPLONE.prc", PGM_LONE_PROC)
        cls.db = os.path.join(cls.td, "t.db")
        _build(root, cls.db)
        cls.old = os.path.join(cls.td, "old.db")               # the fallback walker: no flow tables
        shutil.copyfile(cls.db, cls.old)
        c = sqlite3.connect(cls.old)
        c.executescript("DROP TABLE data_flow; DROP TABLE pfield;")
        c.close()
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def flow(self, *args, db=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = query._main(["--db", db or self.db, "flow", *args])
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def files(self, name):
        text = query.cmd_program(self.conn, name)
        return text.split("### Files", 1)[1].split("\n### ", 1)[0]

    def test_the_proc_s_own_rows_are_in_the_index(self):
        # the rows the Files table listed beside the job's: the bare PROC's step, its default HLQ
        got = {r[0] for r in self.conn.execute(
            "SELECT d.dsn_resolved FROM dd d JOIN step s ON s.id=d.step_id WHERE s.proc_id IS NOT NULL "
            "AND s.effective_pgm='KVPPGM'")}
        self.assertEqual(got, {"TEST.KV.MAST", "TEST.KV.OLDMAST", "PROD.KV.FIXED"})

    def test_program_lists_the_job_s_datasets_only(self):
        files = self.files("KVPPGM")
        self.assertNotIn("TEST.", files)
        self.assertIn("| OUT-FILE | MAST |", files)
        out_row = [ln for ln in files.splitlines() if ln.startswith("| OUT-FILE |")][0]
        self.assertIn("PROD.KV.MAST [output/open_verb] KVPJOB1", out_row)
        self.assertNotIn("OLDMAST", out_row)                   # OLDMAST ends with MAST: another file's DD
        fix_row = [ln for ln in files.splitlines() if ln.startswith("| FIX-FILE |")][0]
        self.assertEqual(fix_row.count("PROD.KV.FIXED"), 1, fix_row)
        self.assertIn("> 3 row(s) from PROC members' default symbolics hidden: the jobs that expand those PROCs "
                      "are listed with the real names.", files)

    def test_a_proc_no_job_runs_is_shown_as_the_proc(self):
        files = self.files("KVPPGM2")
        self.assertIn("TEST.KV.LONE [output/open_verb] (PROC KVPLONE defaults - no indexed job runs it)", files)
        self.assertNotIn("hidden", files)

    def test_flow_takes_no_branch_into_the_proc_s_own_step(self):
        for db, tag in ((self.db, "exact"), (self.old, "reconstructed")):
            with self.subTest(tag):
                down = self.flow("WS-CODE", "--program", "KVPPGM", db=db)
                self.assertNotIn("TEST.KV", down)
                self.assertNotIn("PROC KVPPRC", down)
                self.assertIn("PROD.KV.MAST (KVPJOB1 S1.P010 DD MAST", down)
                self.assertEqual(down.count("-> PROD.KV.FIXED ("), 1, down)
                up = self.flow("IR-CODE", "--program", "KVPRDR", "--up", db=db)
                self.assertIn("KVPJOB1 S1.P010 DD FIXDD", up)
                self.assertNotIn("PROC KVPPRC", up)

    def test_flow_through_a_proc_no_job_runs(self):
        for db, tag in ((self.db, "exact"), (self.old, "reconstructed")):
            with self.subTest(tag):
                down = self.flow("WS-LCODE", "--program", "KVPPGM2", db=db)
                self.assertIn("TEST.KV.LONE (PROC KVPLONE L010 DD LONEDD", down)


# ===========================================================================
# F13 - one interfaces row per transfer
# ===========================================================================

IF_FTP_PROC = ("//KVTFTP   PROC FT=KVTFTPD\n"
               "//F010     EXEC PGM=FTP,PARM='kvproc.example'\n"
               "//INPUT    DD   DSN=PROD.CMN.PARMLIB(&FT),DISP=SHR\n")


class OneRowPerTransfer(unittest.TestCase):
    """KVTJOB1 puts two datasets and a USS file and gets one; KVTJOB2's cards are not in the estate; KVTJOB3 is a
    Connect:Direct step with a partner node; KVTJOB4 runs an FTP PROC with its own card member, KVTJOB5 with a card
    member the estate does not hold."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate")
        _write(root, "POLICY/PROD.KV.JCLLIB/KVTJOB1.jcl",
               "//KVTJOB1  JOB (ACCT),'FTP',CLASS=A\n//F010     EXEC PGM=FTP,PARM='kvpeer.example (EXIT'\n"
               "//SYSPRINT DD   SYSOUT=*\n//INPUT    DD   *\nput 'PROD.KV.OUT1' out1.txt\nput 'PROD.KV.OUT2' out2.txt\n"
               "put /u/kv/c.txt c.txt\nget in1.txt 'PROD.KV.IN1'\nquit\n/*\n//\n")
        _write(root, "POLICY/PROD.KV.JCLLIB/KVTJOB2.jcl",
               "//KVTJOB2  JOB (ACCT),'FTP',CLASS=A\n//F010     EXEC PGM=FTP,PARM='kvpeer.example (EXIT'\n"
               "//INPUT    DD   DSN=PROD.CMN.PARMLIB(KVTMISS),DISP=SHR\n//\n")
        _write(root, "POLICY/PROD.KV.JCLLIB/KVTJOB3.jcl",
               "//KVTJOB3  JOB (ACCT),'NDM',CLASS=A\n//N010     EXEC PGM=DMBATCH\n//SYSIN    DD   *\n"
               "  SUBMIT PROC=KVTPRC1 SNODE=KVNODE &DSN=PROD.KV.NDM1\n/*\n//\n")
        _write(root, "POLICY/PROD.KV.JCLLIB/KVTJOB4.jcl",
               "//KVTJOB4  JOB (ACCT),'FTP',CLASS=A\n//S1       EXEC KVTFTP,FT=KVTFTPJ\n//\n")
        _write(root, "POLICY/PROD.KV.JCLLIB/KVTJOB5.jcl",
               "//KVTJOB5  JOB (ACCT),'FTP',CLASS=A\n//S1       EXEC KVTFTP,FT=KVTMISS2\n//\n")
        _write(root, "SHARED/PROD.CMN.PROCLIB/KVTFTP.prc", IF_FTP_PROC)
        _write(root, "SHARED/PROD.CMN.PARMLIB/KVTFTPD.ctl", "put 'PROD.KV.DEFAULT' default.txt\nquit\n")
        _write(root, "SHARED/PROD.CMN.PARMLIB/KVTFTPJ.ctl", "put 'PROD.KV.JOBF' job.txt\nquit\n")
        cls.db = os.path.join(cls.td, "t.db")
        _build(root, cls.db)
        # the same index as the build before ROADMAP re-parse item 29 left an expanded FTP step: the PROC's own
        # reading copied beside the job's as `unknown [undetermined]` (LESSONS 226)
        cls.aged_db = os.path.join(cls.td, "aged.db")
        shutil.copyfile(cls.db, cls.aged_db)
        c = sqlite3.connect(cls.aged_db)
        sid, line = c.execute("SELECT s.id, s.line FROM step s JOIN job j ON j.id=s.job_id "
                              "WHERE j.job_name='KVTJOB4' AND s.from_proc='KVTFTP'").fetchone()
        c.execute("INSERT INTO dd(step_id,dd_name,concat_seq,dsn,dsn_resolved,mode,mode_source,is_override,line) "
                  "VALUES(?,'*FTP*',0,'PROD.KV.DEFAULT','PROD.KV.DEFAULT','unknown','undetermined',0,?)", (sid, line))
        c.commit()
        c.close()
        cls.conn = query.connect(cls.db)
        cls.aged = query.connect(cls.aged_db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.aged.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def rows(self, conn, **kw):
        return [r for r in query._interface_rows(conn, **kw) if r[0] in ("ftp", "ndm")]

    def test_the_step_is_stored_twice(self):
        # what the report read twice: the interface_edge row naming the put and the pseudo-DD holding it
        edge = self.conn.execute("SELECT i.detail FROM interface_edge i JOIN member m ON m.id=i.member_id "
                                 "WHERE m.name='KVTJOB1'").fetchone()[0]
        self.assertIn("FTP put PROD.KV.OUT1 -> kvpeer.example (out)", edge)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM dd WHERE dd_name='*FTP*' AND dsn_resolved='PROD.KV.OUT1'")
                         .fetchone()[0], 1)

    def test_one_row_per_put_and_get_with_the_peer(self):
        got = sorted(r[:5] for r in self.rows(self.conn) if r[4].startswith("KVTJOB1"))
        self.assertEqual(got, [
            ("ftp", "in", "kvpeer.example", "PROD.KV.IN1", "KVTJOB1 F010"),
            ("ftp", "out", "kvpeer.example", "PROD.KV.OUT1", "KVTJOB1 F010"),
            ("ftp", "out", "kvpeer.example", "PROD.KV.OUT2", "KVTJOB1 F010"),
            ("ftp", "out", "kvpeer.example", "put /u/kv/c.txt", "KVTJOB1 F010")])

    def test_a_step_whose_cards_are_not_indexed_is_one_row(self):
        got = [r for r in self.rows(self.conn) if r[4].startswith("KVTJOB2")]
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0][:3], ("ftp", "?", "kvpeer.example"))
        self.assertIn("external interface via FTP", got[0][3])

    def test_connect_direct_one_row_with_its_partner(self):
        got = [r[:5] for r in self.rows(self.conn) if r[4].startswith("KVTJOB3")]
        self.assertEqual(got, [("ndm", "unknown", "KVNODE", "PROD.KV.NDM1", "KVTJOB3 N010")])

    def test_a_proc_run_by_a_job_is_the_job_s_file_once(self):
        for conn, tag in ((self.conn, "this batch"), (self.aged, "before item 29")):
            with self.subTest(tag):
                got = [r[:5] + r[6:] for r in self.rows(conn) if "KVTJOB4" in r[4] or "KVTFTP" in r[4]]
                self.assertEqual(got, [("ftp", "out", "kvproc.example", "PROD.KV.JOBF", "KVTJOB4 S1.F010", "KVTFTP:2")])

    def test_an_expanded_step_with_no_transfer_is_cited_in_the_proc(self):
        # its interface_edge row sits on the job with the PROC's line (ROADMAP 'JCL cites: known limits')
        got = [r for r in self.rows(self.conn) if r[4].startswith("KVTJOB5")]
        self.assertEqual([(r[0], r[2], r[4], r[6]) for r in got],
                         [("ftp", "kvproc.example", "KVTJOB5 S1.F010", "KVTFTP:2")])
        self.assertIn("external interface via FTP", got[0][3])

    def test_a_job_s_own_step_is_cited_in_the_job(self):
        self.assertEqual({r[6] for r in self.rows(self.conn) if r[4].startswith("KVTJOB1")}, {"KVTJOB1:2"})

    def test_the_report_and_its_filters(self):
        out = query.cmd_interfaces(self.conn)
        self.assertEqual(out.count("| ftp |"), 7, out)          # 4 transfers, KVTJOB2's and KVTJOB5's steps, KVTJOB4's put
        self.assertEqual(out.count("| ndm |"), 1, out)
        self.assertNotIn("FTP put PROD.KV.OUT1", out)
        only = query.cmd_interfaces(self.conn, dsn="PROD.KV.OUT1")
        self.assertEqual(only.count("| ftp |"), 1, only)
        self.assertEqual(query.cmd_interfaces(self.conn, system="POLICY").count("| ftp |"), 7)
        self.assertEqual(query.cmd_interfaces(self.conn, system="SHARED").count("| ftp |"), 0)   # the PROC's own step

    def test_dataset_says_where_the_file_goes(self):
        out = query.cmd_dataset(self.conn, "PROD.KV.OUT1")
        self.assertIn("**Crosses the mainframe boundary**: ftp out to/from kvpeer.example (KVTJOB1 F010) - see", out)


# ===========================================================================
# F17 - crud --job: the datasets of the job asked about
# ===========================================================================

_WS = "       WORKING-STORAGE SECTION.\n"
CRUD_RPT = _cbl("KVCRPT", [("IN-FILE", "RPTIN")], [("IN-FILE", "IN-REC", "IR-CODE")], ["WS-X"],
                ["OPEN INPUT IN-FILE.", "READ IN-FILE.", "CLOSE IN-FILE."]).replace(_WS, _WS + "           COPY KVCCPY.\n")
CRUD_OTH = _cbl("KVCOTH", [("OTH-FILE", "OTHIN")], [("OTH-FILE", "OTH-REC", "OT-CODE")], ["WS-Y"],
                ["OPEN INPUT OTH-FILE.", "READ OTH-FILE.", "CLOSE OTH-FILE."]).replace(_WS, _WS + "           COPY KVCCPY.\n")


def _job(name, *steps):
    return f"//{name:<8} JOB (ACCT),'C',CLASS=A\n" + "".join(steps) + "//\n"


class CrudDatasetsOfTheJob(unittest.TestCase):
    """KVCRPT reads RPTIN: PROD.KV.DAILY in KVCJOBA, PROD.KV.WEEKLY in KVCJOBB, one dataset per step in KVCJOBC,
    DUMMY in KVCJOBD, a concatenation in KVCJOBE. KVCOTH, copying the same copybook, runs in KVCJOBA only."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate")
        _write(root, "POLICY/PROD.KV.COBOL/KVCRPT.cbl", CRUD_RPT)
        _write(root, "POLICY/PROD.KV.COBOL/KVCOTH.cbl", CRUD_OTH)
        _write(root, "POLICY/PROD.KV.COPYLIB/KVCCPY.cpy", "       01  KVC-WORK  PIC X(04).\n")
        rpt = "//{s:<8} EXEC PGM=KVCRPT\n//RPTIN    DD   {d}\n"
        _write(root, "POLICY/PROD.KV.JCLLIB/KVCJOBA.jcl",
               _job("KVCJOBA", rpt.format(s="S1", d="DSN=PROD.KV.DAILY,DISP=SHR"),
                    "//S2       EXEC PGM=KVCOTH\n//OTHIN    DD   DSN=PROD.KV.OTH,DISP=SHR\n"))
        _write(root, "POLICY/PROD.KV.JCLLIB/KVCJOBB.jcl", _job("KVCJOBB", rpt.format(s="S1", d="DSN=PROD.KV.WEEKLY,DISP=SHR")))
        _write(root, "POLICY/PROD.KV.JCLLIB/KVCJOBC.jcl",
               _job("KVCJOBC", rpt.format(s="S1", d="DSN=PROD.KV.Q1,DISP=SHR"), rpt.format(s="S2", d="DSN=PROD.KV.Q2,DISP=SHR")))
        _write(root, "POLICY/PROD.KV.JCLLIB/KVCJOBD.jcl", _job("KVCJOBD", rpt.format(s="S1", d="DUMMY")))
        _write(root, "POLICY/PROD.KV.JCLLIB/KVCJOBE.jcl",
               _job("KVCJOBE", rpt.format(s="S1", d="DSN=PROD.KV.C1,DISP=SHR") + "//         DD   DSN=PROD.KV.C2,DISP=SHR\n"))
        cls.db = os.path.join(cls.td, "t.db")
        _build(root, cls.db)
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def files(self, text, program="KVCRPT"):
        return [ln.split(" | ")[2] for ln in _table_rows(text, program) if " | file | " in ln]

    def test_the_job_asked_about(self):
        self.assertEqual(self.files(query.cmd_crud(self.conn, [], job="KVCJOBB")), ["PROD.KV.WEEKLY"])
        self.assertEqual(self.files(query.cmd_crud(self.conn, [], job="KVCJOBA")), ["PROD.KV.DAILY"])
        self.assertIn("| KVCRPT | file | PROD.KV.WEEKLY | R | R@", query.cmd_crud(self.conn, [], job="KVCJOBB"))

    def test_two_steps_of_the_job(self):
        self.assertEqual(self.files(query.cmd_crud(self.conn, [], job="KVCJOBC")),
                         ["PROD.KV.Q1 (step S1)", "PROD.KV.Q2 (step S2)"])

    def test_a_concatenation_is_read_whole(self):
        self.assertEqual(self.files(query.cmd_crud(self.conn, [], job="KVCJOBE")), ["PROD.KV.C1", "PROD.KV.C2"])

    def test_dummy_in_the_job_is_the_dd(self):
        self.assertEqual(self.files(query.cmd_crud(self.conn, [], job="KVCJOBD")), ["DD RPTIN"])

    def test_no_job_every_job_s_dataset_with_its_job(self):
        out = query.cmd_crud(self.conn, ["KVCRPT"])
        self.assertEqual(self.files(out), ["PROD.KV.C1 (job KVCJOBE)", "PROD.KV.C2 (job KVCJOBE)",
                                           "PROD.KV.DAILY (job KVCJOBA)", "PROD.KV.Q1 (job KVCJOBC)",
                                           "PROD.KV.Q2 (job KVCJOBC)", "PROD.KV.WEEKLY (job KVCJOBB)"])
        self.assertIn("each dataset is a row of its own with the jobs that give it", out)

    def test_a_program_the_job_does_not_run(self):
        out = query.cmd_crud(self.conn, [], job="KVCJOBB", copybook="KVCCPY")
        self.assertEqual(self.files(out), ["PROD.KV.WEEKLY"])
        self.assertEqual(self.files(out, "KVCOTH"), ["PROD.KV.OTH"])       # one job gives it one dataset: plain
        self.assertIn("The datasets are the ones KVCJOBB's own steps give each file", out)


# ===========================================================================
# F51 - flow --up from a SELECT INTO host variable reaches the DB2 column
# ===========================================================================

KV_DCL = ("           EXEC SQL DECLARE PRD.KV_TBL TABLE\n"
          "           ( KV_KEY CHAR(8) NOT NULL, STATUS_CD CHAR(2),\n"
          "             OLD_AMT CHAR(4) ) END-EXEC.\n"
          "       01  DCLKV-TBL.\n"
          "           10 KV-KEY       PIC X(08).\n"
          "           10 STATUS-CD    PIC X(02).\n"
          "           10 OLD-AMT      PIC X(04).\n")
KV_SQLP = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. KVSQLP.\n       DATA DIVISION.\n"
           "       WORKING-STORAGE SECTION.\n       01  WS-OLD  PIC X(04).\n"
           "           EXEC SQL\n               INCLUDE KVDCL\n           END-EXEC.\n"
           "       PROCEDURE DIVISION.\n"
           "           EXEC SQL\n               SELECT STATUS_CD, OLD_AMT\n               INTO :STATUS-CD, :WS-OLD\n"
           "               FROM PRD.KV_TBL\n               WHERE KV_KEY = :KV-KEY\n           END-EXEC.\n"
           "           DISPLAY STATUS-CD WS-OLD.\n           GOBACK.\n")


class FlowUpToTheColumn(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate")
        _write(root, "POLICY/PROD.KV.COPYLIB/KVDCL.cpy", KV_DCL)
        _write(root, "POLICY/PROD.KV.COBOL/KVSQLP.cbl", KV_SQLP)
        cls.db = os.path.join(cls.td, "t.db")
        _build(root, cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def flow(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = query._main(["--db", self.db, "flow", *args])
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def test_the_dclgen_item_filled_by_select_into(self):
        up = self.flow("STATUS-CD", "--program", "KVSQLP", "--up")
        self.assertNotIn("NOT DEFINED", up)
        self.assertRegex(up, r"EXEC SQL SELECT PRD\.KV_TBL STATUS_CD\s+POLICY/KVSQLP:\d+")

    def test_a_working_storage_item_filled_by_select_into(self):
        up = self.flow("WS-OLD", "--program", "KVSQLP", "--up")
        self.assertRegex(up, r"EXEC SQL SELECT PRD\.KV_TBL OLD_AMT\s+POLICY/KVSQLP:\d+")


# ===========================================================================
# the reproductions and the documents
# ===========================================================================

ROOT = os.path.dirname(HERE)
REPRO = os.path.join(ROOT, "tools", "synth", "repro")
THREE = ("F12-values-sort-length", "F13-interfaces-ftp-twice", "F17-crud-one-dataset-per-file")


class TheReproductions(unittest.TestCase):
    """tools/synth/repro/F12, F13 and F17 built in process: every expect.json truth holds (verify.py says FIXED)."""

    def check(self, rid):
        src = os.path.join(REPRO, rid)
        td = tempfile.mkdtemp()
        try:
            shutil.copytree(os.path.join(src, "estate"), os.path.join(td, "estate"))
            db = os.path.join(td, "t.db")
            _build(os.path.join(td, "estate"), db)
            with open(os.path.join(src, "expect.json"), encoding="utf-8") as fh:
                spec = json.load(fh)
            for k, ck in enumerate(spec["checks"], 1):
                out = os.path.join(td, f"q{k}.md")
                with contextlib.redirect_stdout(io.StringIO()):
                    query.main(["--db", db, "--out", out, *ck["query"]])
                with open(out, encoding="utf-8") as fh:
                    text = fh.read()
                if "count" in ck:
                    self.assertEqual(text.count(ck["count"]), ck["truth"], (rid, ck["count"], text))
                    self.assertNotEqual(ck["truth"], ck["symptom"])
                else:
                    self.assertIn(ck["truth_has"], text, (rid, ck["query"]))
                    self.assertNotIn(ck["symptom_has"], text, (rid, ck["query"]))
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_f12_values_sort_length(self):
        self.check("F12-values-sort-length")

    def test_f13_interfaces_ftp_twice(self):
        self.check("F13-interfaces-ftp-twice")

    def test_f17_crud_one_dataset_per_file(self):
        self.check("F17-crud-one-dataset-per-file")

    def test_verify_says_fixed(self):
        sys.path.insert(0, REPRO)
        import verify                                                   # noqa: E402
        out = tempfile.mkdtemp()
        try:
            got = [g for rid in THREE for g in verify.verify(os.path.join(REPRO, rid), out)]
        finally:
            shutil.rmtree(out, ignore_errors=True)
        self.assertEqual([v for _l, v, _d in got], ["FIXED"] * 4, got)


class TheDocsSayIt(unittest.TestCase):
    """LESSONS 235-239, README's crud / interfaces / bare-PROC sentences, the Field Manual, ROADMAP's closed
    known limit, the three reproductions' READMEs and the findings report's verdicts on F48 and F51."""

    def read(self, *path):
        with open(os.path.join(ROOT, *path), encoding="utf-8") as fh:
            return fh.read()

    def test_lessons_rows(self):
        text = self.read("LESSONS.md")
        for n in range(235, 240):
            row = next((ln for ln in text.splitlines() if ln.startswith(f"| {n} |")), "")
            self.assertIn("tests/test_synth_query_findings.py", row, n)
            self.assertEqual(row.replace("\\|", "").count("|") - 1, 5, n)     # number + four cells

    def test_readme_and_the_field_manual(self):
        readme = " ".join(self.read("README.md").split())
        self.assertIn("with `--job`, that job's own steps'", readme)
        self.assertIn("one row per file sent or received, with the peer the step names", readme)
        self.assertIn("in its 'Runs in' and in its Files table alike", readme)
        manual = self.read("docs", "FieldManual.html")
        self.assertIn("the constant after the operator, never a position or a length", manual)
        self.assertIn("one row per file an FTP / Connect:Direct step sends or receives", manual)
        self.assertIn("a PROC's own default datasets only when no indexed job runs the PROC", manual)

    def test_roadmap_closes_its_known_limit(self):
        self.assertIn("**Closed (LESSONS 237) - `interfaces` cited an expanded step's `interface_edge` row",
                      self.read("ROADMAP.md"))

    def test_the_reproductions_and_the_report(self):
        for rid in THREE:
            self.assertIn("Fixed on the re-parse batch, query side only", self.read("tools", "synth", "repro", rid, "README.md"))
        report = self.read("docs", "SYNTH-findings-2026-09-25.md")
        self.assertIn("(LESSONS 235-239; atlas/query.py and atlas/flow.py, no fact module)", report)
        self.assertIn("**F48 is the toolkit's.**", report)
        self.assertIn("**F51 is the toolkit's, and it was fixed before this stage.**", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
