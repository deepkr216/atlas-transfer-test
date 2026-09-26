"""
ROADMAP re-parse item 29 (LESSONS 222-224): the synthetic estate's findings in
the JCL parser, and one found on the way.

  F10 (LESSONS 222) - a PROC's `//SYSIN DD DSN=LIB(&CARDS)` called with
      `EXEC PROC=...,CARDS=MEMBER`: the effective step's DSN was resolved with
      the job's symbols but its card member, the member's text, the launcher
      read from it and the sort byte positions stayed the PROC default's, and
      coverage listed the PROC default as a member 'referenced by JCL but NOT
      indexed'. Now the card member is resolved with the job's symbols (EXEC
      override > PROC default > SET, jcl.PROC_DEFAULT_BEATS_SET as it is).
  F09 (LESSONS 223) - JES resolves relative generation numbers once per job:
      the (+1) an earlier step of the job wrote and a later step names (+1)
      again to read it is the same new generation - input 'gdg_same_job', not
      a second writer. A utility's own DD (SORTIN / SYSUT1, SORTOUT / SYSUT2)
      decides where the generation number says the other way.
  LESSONS 224 - `//PS.SYSIN DD *` overriding a PROC's `SYSIN DD DSN=LIB(MEMBER)`
      kept the PROC's dataset and member beside the instream cards.
  LESSONS 226-228 - the verifier's first round: a PROC's FTP / Connect:Direct /
      IMS utility step kept the rows its own default cards or PARM gave beside
      the job's; coverage and `program NAME` on an index built before the item
      read the member from the row's dataset, tested on rows the older build
      really wrote; `program NAME` says a member's text is loaded only when it
      is indexed.

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
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from atlas import build, jcl, query  # noqa: E402

REPRO = os.path.join(ROOT, "tools", "synth", "repro")


def _dd(step, name):
    return next(d for d in step.dds if d.dd_name.upper().split(".")[-1] == name)


def _expand(job_text, procs=None, cards=None):
    cards = cards or {}
    job = jcl.parse_jcl(job_text, member_lookup=cards.get)
    parsed = {k: jcl.parse_jcl(v, member_lookup=cards.get) for k, v in (procs or {}).items()}
    eff = jcl.expand_job(job, parsed.get, member_lookup=cards.get)
    return job, {s.step_name: s for s in eff}


def _dir(d):
    return (d.mode, d.mode_source)


def _positions(step):
    ctl = "\n".join(d.sysin_text for d in step.dds if d.sysin_text)
    return [(k, p, n) for (k, p, n, _f, _r) in jcl.sort_card_fields(ctl)]


# ===========================================================================
# F09 - a (+1) named again later in the same job is the same new generation
# ===========================================================================

GDG_JOB = """//KVGDGJOB JOB (ACCT),'GDG SAME JOB',CLASS=A,MSGCLASS=X
//STEP010  EXEC PGM=KVEXT01
//EXTOUT   DD   DSN=PROD.KV.EXTRACT(+1),DISP=(NEW,CATLG,DELETE),
//          UNIT=SYSDA,SPACE=(CYL,(5,5))
//STEP020  EXEC PGM=SORT
//SORTIN   DD   DSN=PROD.KV.EXTRACT(+1),DISP=SHR
//SORTOUT  DD   DSN=PROD.KV.EXTRACT.SORTED,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD   *
  SORT FIELDS=(1,12,CH,A)
/*
//STEP030  EXEC PGM=IEBGENER
//SYSUT1   DD   DSN=PROD.KV.EXTRACT(+1),DISP=SHR
//SYSUT2   DD   DSN=PROD.KV.EXTRACT.COPY,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD   DUMMY
//STEP040  EXEC PGM=KVRDR02
//KVIN2    DD   DSN=PROD.KV.EXTRACT(+1),DISP=SHR
//STEP050  EXEC PGM=KVRDR03
//KVIN3    DD   DSN=PROD.KV.EXTRACT(+1),DISP=OLD
//STEP060  EXEC PGM=SORT
//IN1      DD   DSN=PROD.KV.EXTRACT(+1)
//IN2      DD   DSN=PROD.KV.OTHER,DISP=SHR
//SORTOUT  DD   DSN=PROD.KV.JOINED,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD   *
  JOINKEYS F1=IN1,FIELDS=(1,10,A)
  JOINKEYS F2=IN2,FIELDS=(1,10,A)
/*
//
"""


class SameJobGenerations(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.job, cls.by = _expand(GDG_JOB)

    def test_the_sort_reads_the_generation_the_earlier_step_wrote(self):
        # Wrong answer guarded: STEP020 'output [gdg_relative]' - the sort a second writer of the GDG (F09).
        w = _dd(self.by["STEP010"], "EXTOUT")
        self.assertEqual((w.dsn_resolved, w.gdg_rel) + _dir(w), ("PROD.KV.EXTRACT", "+1", "output", "gdg_relative"))
        r = _dd(self.by["STEP020"], "SORTIN")
        self.assertEqual((r.dsn_resolved, r.gdg_rel) + _dir(r), ("PROD.KV.EXTRACT", "+1", "input", "gdg_same_job"))

    def test_every_dd_that_reads_it_reads_it(self):
        # SYSUT1, DISP=SHR, DISP=OLD, a JOINKEYS input by the sort's own cards
        self.assertEqual(_dir(_dd(self.by["STEP030"], "SYSUT1")), ("input", "gdg_same_job"))
        self.assertEqual(_dir(_dd(self.by["STEP040"], "KVIN2")), ("input", "gdg_same_job"))
        self.assertEqual(_dir(_dd(self.by["STEP050"], "KVIN3")), ("input", "gdg_same_job"))
        self.assertEqual(_dir(_dd(self.by["STEP060"], "IN1")), ("input", "gdg_same_job"))

    def test_the_job_as_parsed_says_the_same(self):
        # the job's own rows, before expansion (a job with no PROC: its steps are its effective steps)
        by = {s.step_name: s for s in self.job.steps}
        self.assertEqual(_dir(_dd(by["STEP020"], "SORTIN")), ("input", "gdg_same_job"))
        self.assertEqual(_dir(_dd(by["STEP010"], "EXTOUT")), ("output", "gdg_relative"))

    def test_a_dd_that_writes_it_again_stays_a_writer(self):
        _job, by = _expand("""//KVWRT2   JOB (ACCT),'WRITE AGAIN',CLASS=A
//S1       EXEC PGM=KVEXT01
//OUT      DD   DSN=PROD.KV.EXTRACT(+1),DISP=(NEW,CATLG,DELETE)
//S2       EXEC PGM=SORT
//SORTIN   DD   DSN=PROD.KV.INPUT,DISP=SHR
//SORTOUT  DD   DSN=PROD.KV.EXTRACT(+1),DISP=OLD
//SYSIN    DD   *
  SORT FIELDS=(1,12,CH,A)
/*
//S3       EXEC PGM=KVAPP01
//APPEND   DD   DSN=PROD.KV.EXTRACT(+1),DISP=MOD
//S4       EXEC PGM=IEBGENER
//SYSUT1   DD   DSN=PROD.KV.INPUT,DISP=SHR
//SYSUT2   DD   DSN=PROD.KV.EXTRACT(+1),DISP=OLD
//S5       EXEC PGM=SORT
//SORTIN   DD   DSN=PROD.KV.INPUT,DISP=SHR
//OUT1     DD   DSN=PROD.KV.EXTRACT(+1),DISP=OLD
//SYSIN    DD   *
  SORT FIELDS=COPY
  OUTFIL FNAMES=OUT1,INCLUDE=(1,2,CH,EQ,C'AC')
/*
//
""")
        self.assertEqual(_dir(_dd(by["S2"], "SORTOUT")), ("output", "gdg_relative"))
        self.assertEqual(_dir(_dd(by["S3"], "APPEND")), ("output", "gdg_relative"))
        self.assertEqual(_dir(_dd(by["S4"], "SYSUT2")), ("output", "gdg_relative"))
        self.assertEqual(_dir(_dd(by["S5"], "OUT1")), ("output", "sort_card"))      # the OUTFIL writes it, DISP=OLD

    def test_another_generation_is_another_dataset(self):
        _job, by = _expand("""//KVGEN2   JOB (ACCT),'TWO GENERATIONS',CLASS=A
//S1       EXEC PGM=KVEXT01
//OUT      DD   DSN=PROD.KV.EXTRACT(+1),DISP=(NEW,CATLG,DELETE)
//S2       EXEC PGM=KVEXT02
//OUT      DD   DSN=PROD.KV.EXTRACT(+2),DISP=(NEW,CATLG,DELETE)
//S3       EXEC PGM=KVRDR02
//IN1      DD   DSN=PROD.KV.EXTRACT(+1),DISP=SHR
//IN2      DD   DSN=PROD.KV.EXTRACT(+2),DISP=SHR
//IN0      DD   DSN=PROD.KV.EXTRACT(0),DISP=SHR
//
""")
        self.assertEqual(_dir(_dd(by["S2"], "OUT")), ("output", "gdg_relative"))       # (+2) is not the (+1)
        self.assertEqual(_dir(_dd(by["S3"], "IN1")), ("input", "gdg_same_job"))
        self.assertEqual(_dir(_dd(by["S3"], "IN2")), ("input", "gdg_same_job"))
        self.assertEqual(_dir(_dd(by["S3"], "IN0")), ("input", "gdg_relative"))        # the generation before the job

    def test_an_iefbr14_allocation_writes_nothing(self):
        # the pre-allocate-then-write pattern: the program writing with DISP=OLD is the writer
        _job, by = _expand("""//KVALLOC  JOB (ACCT),'PREALLOCATE',CLASS=A
//S1       EXEC PGM=IEFBR14
//NEW      DD   DSN=PROD.KV.EXTRACT(+1),DISP=(NEW,CATLG,DELETE)
//S2       EXEC PGM=KVLOAD01
//OUT      DD   DSN=PROD.KV.EXTRACT(+1),DISP=OLD
//S3       EXEC PGM=SORT
//SORTIN   DD   DSN=PROD.KV.EXTRACT(+1),DISP=SHR
//SORTOUT  DD   DSN=PROD.KV.SORTED,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD   *
  SORT FIELDS=(1,12,CH,A)
/*
//S4       EXEC PGM=IEFBR14
//GONE     DD   DSN=PROD.KV.EXTRACT(+1),DISP=(OLD,DELETE)
//
""")
        self.assertEqual(_dir(_dd(by["S1"], "NEW")), ("alloc", "iefbr14_disp"))
        self.assertEqual(_dir(_dd(by["S2"], "OUT")), ("output", "gdg_relative"))
        self.assertEqual(_dir(_dd(by["S3"], "SORTIN")), ("input", "gdg_same_job"))
        self.assertEqual(_dir(_dd(by["S4"], "GONE")), ("delete", "iefbr14_disp"))    # a delete, not a reader

    def test_a_utility_dd_decides_where_the_generation_says_the_other_way(self):
        # no earlier step wrote the (+1): the sort still never writes its SORTIN, nor reads its SORTOUT (0)
        _job, by = _expand("""//KVUTIL   JOB (ACCT),'UTILITY SIDES',CLASS=A
//S1       EXEC PGM=SORT
//SORTIN   DD   DSN=PROD.KV.EXTRACT(+1),DISP=SHR
//SORTOUT  DD   DSN=PROD.KV.SORTED(0),DISP=OLD
//SYSIN    DD   *
  SORT FIELDS=(1,12,CH,A)
/*
//S2       EXEC PGM=IEBGENER
//SYSUT1   DD   DSN=PROD.KV.EXTRACT(0),DISP=SHR
//SYSUT2   DD   DSN=PROD.KV.COPY(+1),DISP=(NEW,CATLG,DELETE)
//SYSIN    DD   DUMMY
//
""")
        self.assertEqual(_dir(_dd(by["S1"], "SORTIN")), ("input", "dd_convention"))
        self.assertEqual(_dir(_dd(by["S1"], "SORTOUT")), ("output", "dd_convention"))
        self.assertEqual(_dir(_dd(by["S2"], "SYSUT1")), ("input", "gdg_relative"))    # the two agree: as before
        self.assertEqual(_dir(_dd(by["S2"], "SYSUT2")), ("output", "gdg_relative"))

    def test_generations_are_counted_per_job(self):
        jobs = jcl.parse_jcl_all("""//KVJOBA   JOB (ACCT),'WRITES',CLASS=A
//S1       EXEC PGM=KVEXT01
//OUT      DD   DSN=PROD.KV.EXTRACT(+1),DISP=(NEW,CATLG,DELETE)
//KVJOBB   JOB (ACCT),'READS',CLASS=A
//S1       EXEC PGM=SORT
//SORTIN   DD   DSN=PROD.KV.EXTRACT(+1),DISP=SHR
//SORTOUT  DD   DSN=PROD.KV.SORTED,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD   *
  SORT FIELDS=(1,12,CH,A)
/*
//
""")
        self.assertEqual([j.job_name for j in jobs], ["KVJOBA", "KVJOBB"])
        eff = jcl.expand_job(jobs[1], lambda n: None)
        self.assertEqual(_dir(_dd(eff[0], "SORTIN")), ("input", "dd_convention"))  # not KVJOBA's generation

    def test_proc_steps_and_job_steps_are_one_job(self):
        proc = """//KVPROC   PROC HLQ=TEST.KV
//PS010    EXEC PGM=KVEXT01
//EXTR     DD   DSN=&HLQ..EXTRACT(+1),DISP=(NEW,CATLG,DELETE)
//PS020    EXEC PGM=SORT
//SORTIN   DD   DSN=&HLQ..EXTRACT(+1),DISP=SHR
//SORTOUT  DD   DSN=&HLQ..SORTED,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD   *
  SORT FIELDS=(1,12,CH,A)
/*
"""
        _job, by = _expand("""//KVPJOB   JOB (ACCT),'PROC THEN STEP',CLASS=A
//S1       EXEC KVPROC,HLQ=PROD.KV
//S2       EXEC PGM=KVRDR02
//KVIN2    DD   DSN=PROD.KV.EXTRACT(+1),DISP=SHR
//
""", {"KVPROC": proc})
        self.assertEqual(_dir(_dd(by["S1.PS010"], "EXTR")), ("output", "gdg_relative"))
        self.assertEqual(_dir(_dd(by["S1.PS020"], "SORTIN")), ("input", "gdg_same_job"))
        self.assertEqual(_dir(_dd(by["S2"], "KVIN2")), ("input", "gdg_same_job"))
        # the PROC's own rows (its default symbolics) read the same way
        own = {s.step_name: s for s in jcl.parse_jcl(proc).steps}
        self.assertEqual(_dir(_dd(own["PS020"], "SORTIN")), ("input", "gdg_same_job"))
        # and an instream PROC's
        job = jcl.parse_jcl("//KVIJOB   JOB (ACCT),'INSTREAM',CLASS=A\n" + proc + "//         PEND\n"
                            "//S1       EXEC KVPROC\n//\n")
        inst = {s.step_name: s for s in job.instream_procs["KVPROC"].steps}
        self.assertEqual(_dir(_dd(inst["PS020"], "SORTIN")), ("input", "gdg_same_job"))

    def test_an_override_renaming_the_writer_leaves_nothing_to_read(self):
        # the synthetic estate's NIGHT jobs: the job's //PS010.EXTR override renames the (+1) the PROC wrote, so
        # no earlier step writes PROD.KV.EXTRACT(+1) - PS020's SORTIN is read by its role, not 'gdg_same_job'
        proc = """//KVPROC   PROC HLQ=TEST.KV
//PS010    EXEC PGM=KVEXT01
//EXTR     DD   DSN=&HLQ..EXTRACT(+1),DISP=(NEW,CATLG,DELETE)
//PS020    EXEC PGM=SORT
//SORTIN   DD   DSN=&HLQ..EXTRACT(+1),DISP=SHR
//SORTOUT  DD   DSN=&HLQ..SORTED,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD   *
  SORT FIELDS=(1,12,CH,A)
/*
"""
        _job, by = _expand("""//KVOJOB   JOB (ACCT),'OVERRIDE',CLASS=A
//S1       EXEC KVPROC,HLQ=PROD.KV
//PS010.EXTR DD DSN=PROD.KV.EXTRACT.OVERRIDE(+1),DISP=(NEW,CATLG,DELETE)
//
""", {"KVPROC": proc})
        w = _dd(by["S1.PS010"], "EXTR")
        self.assertEqual((w.dsn_resolved,) + _dir(w), ("PROD.KV.EXTRACT.OVERRIDE", "output", "gdg_relative"))
        r = _dd(by["S1.PS020"], "SORTIN")
        self.assertEqual((r.dsn_resolved,) + _dir(r), ("PROD.KV.EXTRACT", "input", "dd_convention"))


# ===========================================================================
# F10 - the card member a PROC names through a symbolic is the job's
# ===========================================================================

SORT_PROC = """//KVSORT   PROC IN=PROD.CMN.SORTIN,OUT=PROD.CMN.SORTOUT,CARDS=KVDEFC
//SRT010   EXEC PGM=SORT
//SORTIN   DD   DSN=&IN,DISP=SHR
//SORTOUT  DD   DSN=&OUT,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD   DSN=PROD.CMN.PARMLIB(&CARDS),DISP=SHR
//SYSOUT   DD   SYSOUT=*
"""
NODEF_PROC = SORT_PROC.replace(",CARDS=KVDEFC", "")
SEQ_PROC = SORT_PROC.replace("DSN=PROD.CMN.PARMLIB(&CARDS)", "DSN=PROD.CMN.CARDS.&CARDS")
TSO_PROC = """//KVTSO    PROC TSO=KVDEFT
//RUN010   EXEC PGM=IKJEFT01
//SYSTSIN  DD   DSN=PROD.CMN.PARMLIB(&TSO),DISP=SHR
//SYSTSPRT DD   SYSOUT=*
"""
CARDS = {
    "KVJOBC": "  SORT FIELDS=(1,12,CH,A)\n  INCLUDE COND=(13,2,CH,EQ,C'AC')\n",
    "KVDEFC": "  SORT FIELDS=(40,8,CH,A)\n",
    "KVSETC": "  SORT FIELDS=(60,4,CH,A)\n",
    "KVOVRC": "  SORT FIELDS=(70,2,CH,A)\n",
    "KVDEFT": "  DSN SYSTEM(DB2P)\n  RUN PROGRAM(KVDEFPGM) PLAN(KVPLAN)\n  END\n",
    "KVJOBT": "  DSN SYSTEM(DB2P)\n  RUN PROGRAM(KVJOBPGM) PLAN(KVPLAN)\n  END\n",
}


def _job(exec_line, extra=""):
    return f"//KVCJOB   JOB (ACCT),'CARDS',CLASS=A\n{extra}{exec_line}\n//\n"


class ProcCardMember(unittest.TestCase):

    def test_the_exec_override_names_the_member(self):
        # Wrong answer guarded: card member KVDEFC (the PROC default), its text or none, no byte positions (F10)
        _j, by = _expand(_job("//S1       EXEC KVSORT,CARDS=KVJOBC"), {"KVSORT": SORT_PROC}, CARDS)
        d = _dd(by["S1.SRT010"], "SYSIN")
        self.assertEqual((d.dsn_resolved, d.card_member), ("PROD.CMN.PARMLIB(KVJOBC)", "KVJOBC"))
        self.assertEqual(d.sysin_text, CARDS["KVJOBC"])
        self.assertEqual(_positions(by["S1.SRT010"]), [("SORT", 1, 12), ("INCLUDE", 13, 2)])

    def test_without_an_override_the_proc_default(self):
        _j, by = _expand(_job("//S1       EXEC KVSORT"), {"KVSORT": SORT_PROC}, CARDS)
        d = _dd(by["S1.SRT010"], "SYSIN")
        self.assertEqual((d.card_member, d.sysin_text), ("KVDEFC", CARDS["KVDEFC"]))
        self.assertEqual(_positions(by["S1.SRT010"]), [("SORT", 40, 8)])

    def test_exec_override_then_proc_default_then_set(self):
        self.assertTrue(jcl.PROC_DEFAULT_BEATS_SET)
        set_line = "//         SET CARDS=KVSETC\n"
        _j, by = _expand(_job("//S1       EXEC KVSORT", set_line), {"KVSORT": SORT_PROC}, CARDS)
        self.assertEqual(_dd(by["S1.SRT010"], "SYSIN").card_member, "KVDEFC")        # the PROC default beats SET
        _j, by = _expand(_job("//S1       EXEC KVSORT", set_line), {"KVSORT": NODEF_PROC}, CARDS)
        d = _dd(by["S1.SRT010"], "SYSIN")
        self.assertEqual((d.card_member, d.sysin_text), ("KVSETC", CARDS["KVSETC"]))  # SET: no PROC default
        _j, by = _expand(_job("//S1       EXEC KVSORT,CARDS=KVJOBC", set_line), {"KVSORT": NODEF_PROC}, CARDS)
        self.assertEqual(_dd(by["S1.SRT010"], "SYSIN").card_member, "KVJOBC")        # the EXEC override beats both

    def test_a_member_not_indexed_has_no_text_and_not_the_defaults(self):
        job, by = _expand(_job("//S1       EXEC KVSORT,CARDS=KVNONE"), {"KVSORT": SORT_PROC}, CARDS)
        d = _dd(by["S1.SRT010"], "SYSIN")
        self.assertEqual((d.card_member, d.sysin_text), ("KVNONE", None))
        self.assertEqual(_positions(by["S1.SRT010"]), [])
        self.assertTrue(any(u[0] == "launcher_parm" and "S1.SRT010" in u[1] for u in job.unresolved))

    def test_the_launcher_follows_the_member(self):
        # the PROC parsed with its cards reads KVDEFPGM from its default member; the job's member decides
        own = jcl.parse_jcl(TSO_PROC, member_lookup=CARDS.get)
        self.assertEqual(own.steps[0].effective_pgm, "KVDEFPGM")
        _j, by = _expand(_job("//S1       EXEC KVTSO,TSO=KVJOBT"), {"KVTSO": TSO_PROC}, CARDS)
        self.assertEqual((by["S1.RUN010"].effective_pgm, by["S1.RUN010"].launcher), ("KVJOBPGM", "IKJEFT01"))
        job, by = _expand(_job("//S1       EXEC KVTSO,TSO=KVNONT"), {"KVTSO": TSO_PROC}, CARDS)
        self.assertIsNone(by["S1.RUN010"].effective_pgm)                           # not the default's KVDEFPGM
        self.assertTrue(any(u[0] == "launcher_parm" and "S1.RUN010" in u[1] for u in job.unresolved))

    def test_an_override_naming_a_dataset_brings_its_own_cards(self):
        override = "//SRT010.SYSIN DD DSN=PROD.CMN.PARMLIB(KVOVRC),DISP=SHR"
        _j, by = _expand(_job("//S1       EXEC KVSORT\n" + override), {"KVSORT": SORT_PROC}, CARDS)
        d = _dd(by["S1.SRT010"], "SYSIN")
        self.assertEqual((d.card_member, d.sysin_text, d.is_override), ("KVOVRC", CARDS["KVOVRC"], True))
        cards = {k: v for k, v in CARDS.items() if k != "KVOVRC"}
        _j, by = _expand(_job("//S1       EXEC KVSORT\n" + override), {"KVSORT": SORT_PROC}, cards)
        d = _dd(by["S1.SRT010"], "SYSIN")
        self.assertEqual((d.card_member, d.sysin_text), ("KVOVRC", None))           # never KVDEFC's text

    def test_instream_cards_replace_the_procs_dataset(self):
        # LESSONS 224. Wrong answer guarded: PROD.CMN.PARMLIB(KVDEFC) and card member KVDEFC beside the instream cards
        _j, by = _expand(_job("//S1       EXEC KVSORT\n//SRT010.SYSIN DD *\n  SORT FIELDS=(20,6,CH,A)\n/*"),
                         {"KVSORT": SORT_PROC}, CARDS)
        d = _dd(by["S1.SRT010"], "SYSIN")
        self.assertEqual((d.dsn, d.dsn_resolved, d.card_member, d.is_override), (None, None, None, True))
        self.assertIn("SORT FIELDS=(20,6,CH,A)", d.sysin_text)
        self.assertEqual(_dir(d), ("input", "dd_convention"))
        self.assertEqual(_positions(by["S1.SRT010"]), [("SORT", 20, 6)])

    def test_a_sequential_card_dataset_named_by_a_symbolic(self):
        job, by = _expand(_job("//S1       EXEC KVSORT,CARDS=KVJOBC"), {"KVSORT": SEQ_PROC}, CARDS)
        d = _dd(by["S1.SRT010"], "SYSIN")
        self.assertEqual((d.dsn_resolved, d.card_member, d.sysin_text),
                         ("PROD.CMN.CARDS.KVJOBC", "KVJOBC", CARDS["KVJOBC"]))
        self.assertTrue(any(u[0] == "card_seq_assumed" and "KVJOBC" in u[1] for u in job.unresolved))


# ===========================================================================
# both, in the index and the reports
# ===========================================================================

COBOL_READER = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. KVRDR01.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT KV-IN ASSIGN TO KVIN.
       DATA DIVISION.
       FILE SECTION.
       FD  KV-IN.
       01  KV-REC                 PIC X(80).
       PROCEDURE DIVISION.
       0000-MAIN.
           OPEN INPUT KV-IN.
           READ KV-IN.
           CLOSE KV-IN.
           GOBACK.
"""

INDEX_GDG_JOB = """//KVGDGJOB JOB (ACCT),'GDG SAME JOB',CLASS=A,MSGCLASS=X
//STEP010  EXEC PGM=KVEXT01
//EXTOUT   DD   DSN=PROD.KV.EXTRACT(+1),DISP=(NEW,CATLG,DELETE),
//          UNIT=SYSDA,SPACE=(CYL,(5,5))
//STEP020  EXEC PGM=SORT
//SORTIN   DD   DSN=PROD.KV.EXTRACT(+1),DISP=SHR
//SORTOUT  DD   DSN=PROD.KV.EXTRACT.SORTED,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD   *
  SORT FIELDS=(1,12,CH,A)
/*
//STEP030  EXEC PGM=KVRDR01
//KVIN     DD   DSN=PROD.KV.EXTRACT(+1),DISP=OLD
//STEP040  EXEC PGM=KVRDR02
//KVIN2    DD   DSN=PROD.KV.EXTRACT(+1),DISP=SHR
//
"""

INDEX_CARD_JOB = """//KVCARDJB JOB (ACCT),'CARDS SYMBOLIC',CLASS=A,MSGCLASS=X
//         JCLLIB ORDER=(PROD.CMN.PROCLIB)
//WEEK3    EXEC PROC=KVSORT,IN=PROD.KV.EXTRACT,OUT=PROD.KV.SORTED,
//          CARDS=KVJOBC
//WEEK4    EXEC PROC=KVSORT,CARDS=KVMISS
//
"""

LONE_PROC = """//KVLONE   PROC
//LN010    EXEC PGM=SORT
//SORTIN   DD   DSN=PROD.CMN.LONE.IN,DISP=SHR
//SORTOUT  DD   DSN=PROD.CMN.LONE.OUT,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD   DSN=PROD.CMN.PARMLIB(KVLONEC),DISP=SHR
"""


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _run_build(root, db, *extra):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = build._main([root, "--db", db, "--quiet", *extra])
    assert rc == 0, buf.getvalue()


class InTheIndex(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate")
        _write(root, "POLICY/PROD.KV.JCLLIB/KVGDGJOB.jcl", INDEX_GDG_JOB)
        _write(root, "POLICY/PROD.KV.JCLLIB/KVCARDJB.jcl", INDEX_CARD_JOB)
        _write(root, "POLICY/PROD.KV.COBOL/KVRDR01.cbl", COBOL_READER)
        _write(root, "SHARED/PROD.CMN.PROCLIB/KVSORT.prc", SORT_PROC)
        _write(root, "SHARED/PROD.CMN.PROCLIB/KVLONE.prc", LONE_PROC)
        _write(root, "SHARED/PROD.CMN.PARMLIB/KVJOBC.ctl", CARDS["KVJOBC"])
        cls.db = os.path.join(cls.td, "t.db")
        _run_build(root, cls.db, "--rebuild")
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def _row(self, job, step, dd):
        return self.conn.execute("""SELECT d.* FROM dd d JOIN step s ON s.id=d.step_id JOIN job j ON j.id=s.job_id
                                    WHERE j.job_name=? AND s.step_name=? AND d.dd_name=?""", (job, step, dd)).fetchone()

    def test_the_dd_rows(self):
        r = self._row("KVGDGJOB", "STEP020", "SORTIN")
        self.assertEqual((r["mode"], r["mode_source"], r["gdg_rel"]), ("input", "gdg_same_job", "+1"))
        r = self._row("KVGDGJOB", "STEP040", "KVIN2")
        self.assertEqual((r["mode"], r["mode_source"]), ("input", "gdg_same_job"))
        # the program's own OPEN still decides, over the same-job reading
        r = self._row("KVGDGJOB", "STEP030", "KVIN")
        self.assertEqual((r["mode"], r["mode_source"]), ("input", "open_verb"))
        r = self._row("KVGDGJOB", "STEP010", "EXTOUT")
        self.assertEqual((r["mode"], r["mode_source"]), ("output", "gdg_relative"))

    def test_dataset_lists_one_writer(self):
        out = query.cmd_dataset(self.conn, "PROD.KV.EXTRACT")
        self.assertIn("| input [gdg_same_job] | POLICY | *SORT* |", out)
        self.assertNotIn("| output [gdg_relative] | POLICY | *SORT* |", out)
        writers = [ln for ln in out.splitlines() if ln.startswith("| PROD.KV.EXTRACT |") and "| output" in ln]
        self.assertEqual(len(writers), 1, writers)
        self.assertIn("STEP010", writers[0])

    def test_the_effective_steps_cards(self):
        r = self._row("KVCARDJB", "WEEK3.SRT010", "SYSIN")
        self.assertEqual((r["dsn_resolved"], r["card_member"]), ("PROD.CMN.PARMLIB(KVJOBC)", "KVJOBC"))
        self.assertIn("SORT FIELDS=(1,12,CH,A)", r["sysin_text"])
        got = self.conn.execute("""SELECT c.card_kind, c.pos, c.length FROM card_field_ref c JOIN step s ON s.id=c.step_id
                                   WHERE s.step_name='WEEK3.SRT010' ORDER BY c.pos""").fetchall()
        self.assertEqual([tuple(x) for x in got], [("SORT", 1, 12), ("INCLUDE", 13, 2)])
        out = query.cmd_job(self.conn, "KVCARDJB")
        self.assertIn("PROD.CMN.PARMLIB(KVJOBC) -> 2 card lines loaded from member", out)
        self.assertIn("SORT 1-12", out)
        self.assertIn("PROD.CMN.PARMLIB(KVMISS) -> card member NOT indexed", out)
        self.assertNotIn("KVDEFC", out)

    def test_coverage_names_the_members_the_jobs_read(self):
        # Wrong answer guarded: KVDEFC, the PROC's default, 'referenced by JCL but NOT indexed' (F10)
        self.assertEqual(query.card_members_not_indexed(self.conn), [("KVLONEC", 1), ("KVMISS", 1)])
        out = query.cmd_coverage(self.conn)
        i = out.index("### Control-card members referenced by JCL but NOT indexed")
        section = out[i:]
        self.assertIn("| KVMISS | 1 |", section)
        self.assertIn("| KVLONEC | 1 |", section)        # a PROC no job runs: its own rows are what it reads
        self.assertNotIn("KVDEFC", section)

    def test_an_index_built_before_the_item_names_the_same_members(self):
        # LESSONS 227. The older build kept the job's dataset with the PROC default as card member - aged to that
        # shape (age_to_before_item_29, checked against a real build of that code), the table names what the jobs
        # read, as on this index. Wrong answer guarded: KVDEFC counted for WEEK3 and WEEK4, which do not read it.
        db = os.path.join(self.td, "aged.db")
        shutil.copyfile(self.db, db)
        age_to_before_item_29(db)
        aged = query.connect(db)
        try:
            row = aged.execute("""SELECT d.dsn_resolved, d.card_member, d.sysin_text FROM dd d
                                  JOIN step s ON s.id=d.step_id
                                  WHERE s.step_name='WEEK3.SRT010' AND d.dd_name='SYSIN'""").fetchone()
            self.assertEqual(tuple(row), ("PROD.CMN.PARMLIB(KVJOBC)", "KVDEFC", None))
            self.assertEqual(query.card_members_not_indexed(aged), [("KVLONEC", 1), ("KVMISS", 1)])
        finally:
            aged.close()


# ===========================================================================
# LESSONS 226 - a PROC's FTP / Connect:Direct / IMS utility step: the rows
# read from its cards or its PARM are the calling job's, once
# ===========================================================================

FTP_PROC = """//KVFTP    PROC FT=KVFTPD
//F010     EXEC PGM=FTP,PARM='kvhost.example'
//INPUT    DD   DSN=PROD.CMN.PARMLIB(&FT),DISP=SHR
//OUTPUT   DD   SYSOUT=*
"""
NDM_PROC = """//KVNDM    PROC NP=KVNDMD
//N010     EXEC PGM=DMBATCH
//SYSIN    DD   DSN=PROD.CMN.PARMLIB(&NP),DISP=SHR
"""
ULU_PROC = """//KVULL    PROC
//G        EXEC PGM=DFSRRC00,PARM=(ULU,DFSURGU0,KVDBDL)
//DFSURGU1 DD   DSN=PROD.KV.UNLOAD,DISP=(NEW,CATLG)
"""
ULU_SYM_PROC = """//KVULU    PROC DBD=KVDBDD
//G        EXEC PGM=DFSRRC00,PARM=(ULU,DFSURGU0,&DBD)
//DFSURGU1 DD   DSN=PROD.KV.UNLOAD2,DISP=(NEW,CATLG)
"""
IFACE_CARDS = {
    "KVFTPD": "put 'PROD.KV.DEFAULT.FILE' default.txt\nquit\n",
    "KVFTPJ": "put 'PROD.KV.JOB.FILE' job.txt\nquit\n",
    "KVNDMD": "  SUBMIT PROC=KVPRD1 &DSN=PROD.KV.NDM.DEFAULT\n",
    "KVNDMJ": "  SUBMIT PROC=KVPRJ1 &DSN=PROD.KV.NDM.JOBFILE\n",
}
IFACE_JOB = """//KVIFJOB  JOB (ACCT),'INTERFACES',CLASS=A
//S1       EXEC KVFTP,FT=KVFTPJ
//S2       EXEC KVNDM,NP=KVNDMJ
//S3       EXEC KVULL
//S4       EXEC KVULL,PARM.G=(ULU,DFSURRL0,KVDBDJ)
//S5       EXEC KVULU,DBD=KVDBDJ
//S6       EXEC KVFTP
//S7       EXEC KVNDM
//
"""
IFACE_PROCS = {"KVFTP": FTP_PROC, "KVNDM": NDM_PROC, "KVULL": ULU_PROC, "KVULU": ULU_SYM_PROC}


def _rows(step, name):
    return [(d.dsn_resolved, d.mode, d.mode_source) for d in step.dds if d.dd_name == name]


class ProcInterfaceRows(unittest.TestCase):
    """LESSONS 226. Wrong answer guarded: beside the job's own *FTP* / *NDM* / *DBD* row, the one the PROC's own
    cards or PARM gave (its default card member, its default database), as 'unknown [undetermined]'."""

    @classmethod
    def setUpClass(cls):
        cls.job, cls.by = _expand(IFACE_JOB, IFACE_PROCS, IFACE_CARDS)

    def test_ftp_puts_the_jobs_file_once(self):
        self.assertEqual(_rows(self.by["S1.F010"], "*FTP*"), [("PROD.KV.JOB.FILE", "input", "ftp_put")])

    def test_ftp_without_an_override_puts_the_default_once(self):
        self.assertEqual(_rows(self.by["S6.F010"], "*FTP*"), [("PROD.KV.DEFAULT.FILE", "input", "ftp_put")])

    def test_connect_direct_names_the_jobs_file_once(self):
        # the process parameter of the job's cards - the default's used to stand in its place, 'undetermined'
        self.assertEqual(_rows(self.by["S2.N010"], "*NDM*"), [("PROD.KV.NDM.JOBFILE", "unknown", "ndm_symbolic")])

    def test_connect_direct_without_an_override_names_the_default_once(self):
        # the copy of the PROC's row, 'undetermined', stood where the process parameter's row belongs
        self.assertEqual(_rows(self.by["S7.N010"], "*NDM*"), [("PROD.KV.NDM.DEFAULT", "unknown", "ndm_symbolic")])

    def test_an_ims_utility_step_names_one_database(self):
        self.assertEqual(_rows(self.by["S3.G"], "*DBD*"), [("KVDBDL", "input", "ims_utility")])
        # PARM.G= on the job's EXEC: the reload writes the job's database, and the PROC's is not there at all
        self.assertEqual(_rows(self.by["S4.G"], "*DBD*"), [("KVDBDJ", "output", "ims_utility")])
        self.assertEqual(_rows(self.by["S5.G"], "*DBD*"), [("KVDBDJ", "input", "ims_utility")])

    def test_the_procs_own_rows_keep_its_default(self):
        own = jcl.parse_jcl(FTP_PROC, member_lookup=IFACE_CARDS.get)
        self.assertEqual(_rows(own.steps[0], "*FTP*"), [("PROD.KV.DEFAULT.FILE", "input", "ftp_put")])
        own = jcl.parse_jcl(ULU_PROC)
        self.assertEqual(_rows(own.steps[0], "*DBD*"), [("KVDBDL", "input", "ims_utility")])


class ProcInterfaceRowsInTheIndex(unittest.TestCase):
    """LESSONS 226 in the reports: `dataset` of the PROC's default file named the job's step as its reader,
    'unknown [undetermined]', and said it crosses the mainframe boundary there."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate")
        _write(root, "POLICY/PROD.KV.JCLLIB/KVIFJOB.jcl", IFACE_JOB.replace("//S6       EXEC KVFTP\n//S7       EXEC KVNDM\n", ""))
        _write(root, "SHARED/PROD.CMN.PROCLIB/KVFTP.prc", FTP_PROC)
        _write(root, "SHARED/PROD.CMN.PROCLIB/KVNDM.prc", NDM_PROC)
        _write(root, "SHARED/PROD.CMN.PROCLIB/KVULL.prc", ULU_PROC)
        _write(root, "SHARED/PROD.CMN.PROCLIB/KVULU.prc", ULU_SYM_PROC)
        for name, text in IFACE_CARDS.items():
            _write(root, f"SHARED/PROD.CMN.PARMLIB/{name}.ctl", text)
        cls.db = os.path.join(cls.td, "t.db")
        _run_build(root, cls.db, "--rebuild")
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_one_row_per_file_and_database(self):
        got = self.conn.execute("""SELECT s.step_name, d.dd_name, d.dsn_resolved, d.mode, d.mode_source
                                   FROM dd d JOIN step s ON s.id=d.step_id JOIN job j ON j.id=s.job_id
                                   WHERE j.job_name='KVIFJOB' AND d.dd_name LIKE '*%' ORDER BY s.step_name""").fetchall()
        self.assertEqual([tuple(r) for r in got], [
            ("S1.F010", "*FTP*", "PROD.KV.JOB.FILE", "input", "ftp_put"),
            ("S2.N010", "*NDM*", "PROD.KV.NDM.JOBFILE", "unknown", "ndm_symbolic"),
            ("S3.G", "*DBD*", "KVDBDL", "input", "ims_utility"),
            ("S4.G", "*DBD*", "KVDBDJ", "output", "ims_utility"),
            ("S5.G", "*DBD*", "KVDBDJ", "input", "ims_utility")])

    def test_the_default_file_is_no_job_steps(self):
        out = query.cmd_dataset(self.conn, "PROD.KV.DEFAULT.FILE")
        self.assertNotIn("KVIFJOB", out)
        self.assertNotIn("undetermined]", out.split("> Direction marked")[0])

    def test_the_jobs_file_crosses_the_boundary_at_its_step(self):
        out = query.cmd_dataset(self.conn, "PROD.KV.JOB.FILE")
        self.assertIn("| PROD.KV.JOB.FILE | input [ftp_put] |", out)
        self.assertIn("ftp out (KVIFJOB S1.F010)", out)
        self.assertNotIn("ftp unknown", out)
        job = query.cmd_job(self.conn, "KVIFJOB")
        self.assertNotIn("PROD.KV.DEFAULT.FILE", job)
        self.assertNotIn("PROD.KV.NDM.DEFAULT", job)
        self.assertEqual(job.count("| *DBD* |"), 3, job)


# ===========================================================================
# LESSONS 227 - coverage and `program NAME` on an index built before the
# item: the member a row reads is the one its dataset names
# ===========================================================================

KVSORTN_PROC = SORT_PROC.replace("//KVSORT   PROC", "//KVSORTN  PROC").replace("CARDS=KVDEFC", "CARDS=KVDEFN")
OLD_JOBS = {
    "KVOJ01": "//S1       EXEC KVSORT,CARDS=KVJOBC",                 # the job's member indexed, the default too
    "KVOJ02": "//S1       EXEC KVSORT,CARDS=KVJMISS",                # the job's member missing
    "KVOJ03": "//S1       EXEC KVSORT,CARDS=KVJOBC\n"                # an override naming a member not in the estate
              "//SRT010.SYSIN DD DSN=PROD.CMN.PARMLIB(KVOMISS),DISP=SHR",
    "KVOJ04": "//S1       EXEC KVSORT",                              # reads the default, indexed
    "KVOJ05": "//S1       EXEC KVSORTN,CARDS=KVJOBC",                # the default missing, the job's member indexed
    "KVOJ06": "//S1       EXEC KVSORTN",                             # reads the default, missing
    "KVOJ07": "//S1       EXEC KVSORTN\n//SRT010.SYSIN DD *\n  SORT FIELDS=(20,6,CH,A)\n/*",   # instream cards
    "KVOJ08": "//S1       EXEC KVSORTN,CARDS=KVJMIS2",               # both missing
}
# what the jobs read and the estate does not hold, one DD each
OLD_MISSING = [("KVDEFN", 1), ("KVJMIS2", 1), ("KVJMISS", 1), ("KVOMISS", 1)]


def old_card_estate(root):
    _write(root, "SHARED/PROD.CMN.PROCLIB/KVSORT.prc", SORT_PROC)
    _write(root, "SHARED/PROD.CMN.PROCLIB/KVSORTN.prc", KVSORTN_PROC)
    _write(root, "SHARED/PROD.CMN.PARMLIB/KVJOBC.ctl", CARDS["KVJOBC"])
    _write(root, "SHARED/PROD.CMN.PARMLIB/KVDEFC.ctl", CARDS["KVDEFC"])
    for name, body in OLD_JOBS.items():
        _write(root, f"POLICY/PROD.KV.JCLLIB/{name}.jcl", f"//{name:<8} JOB (ACCT),'CARDS',CLASS=A\n{body}\n//\n")


def age_to_before_item_29(db):
    """Rewrite the card DDs of the expanded PROC steps of an index built by this code into what the build before
    ROADMAP re-parse item 29 wrote (checked against a real build of that code - f93c60c - on old_card_estate):
      - a DD the job does not override: dsn_resolved the job's (resolved with its symbols), card_member and text the
        PROC's own row's - its default member's;
      - a //PS.DD override naming a dataset: its own card_member, its own text or else the PROC default's;
      - a //PS.DD DD * override: the PROC's dataset and card member beside the instream cards (LESSONS 224) - the
        estate's one such job sets no CARDS=, so the PROC's own resolution is the job's."""
    conn = sqlite3.connect(db)
    try:
        own = {}
        for r in conn.execute("""SELECT pd.proc_name, s.step_name, d.dd_name, d.concat_seq, d.dsn, d.dsn_resolved,
                                        d.card_member, d.sysin_text
                                 FROM dd d JOIN step s ON s.id=d.step_id JOIN proc_def pd ON pd.id=s.proc_id"""):
            own[(r[0].upper(), r[1].upper(), r[2].upper(), r[3])] = r[4:]
        for r in conn.execute("""SELECT d.id, s.from_proc, s.step_name, s.parent_step, d.dd_name, d.concat_seq, d.dsn,
                                        d.is_override, d.sysin_text
                                 FROM dd d JOIN step s ON s.id=d.step_id WHERE s.from_proc IS NOT NULL""").fetchall():
            p = own.get((r[1].upper(), r[2][len(r[3]) + 1:].upper(), r[4].upper(), r[5]))
            if p is None:
                continue
            pdsn, pres, pmember, ptext = p
            if not r[7]:
                conn.execute("UPDATE dd SET card_member=?, sysin_text=? WHERE id=?", (pmember, ptext, r[0]))
            elif r[6]:
                conn.execute("UPDATE dd SET sysin_text=? WHERE id=?", (r[8] if r[8] is not None else ptext, r[0]))
            elif r[8] is not None:
                conn.execute("UPDATE dd SET dsn=?, dsn_resolved=?, card_member=? WHERE id=?",
                             (pdsn, pres, pmember, r[0]))
        conn.commit()
    finally:
        conn.close()


class AnIndexBuiltBeforeTheItem(unittest.TestCase):
    """LESSONS 227. Coverage's table of card members 'referenced by JCL but NOT indexed' and `program NAME`'s 'Used
    as control cards by', on an index built by this code and on one aged to what the build before ROADMAP re-parse
    item 29 wrote. Wrong answers guarded, on the older index: the override's missing member KVOMISS gone from the
    table (the effective row carried the default's text, the job step's own row was skipped), and the PROC default
    counted for the jobs whose dataset names their own member."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate")
        old_card_estate(root)
        cls.db = os.path.join(cls.td, "t.db")
        _run_build(root, cls.db, "--rebuild")
        cls.aged_db = os.path.join(cls.td, "aged.db")
        shutil.copyfile(cls.db, cls.aged_db)
        age_to_before_item_29(cls.aged_db)
        cls.conn = query.connect(cls.db)
        cls.aged = query.connect(cls.aged_db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.aged.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def _sysin(self, conn, job):
        return conn.execute("""SELECT d.dsn_resolved, d.card_member, d.sysin_text FROM dd d JOIN step s ON s.id=d.step_id
                               JOIN job j ON j.id=s.job_id WHERE j.job_name=? AND s.step_name='S1.SRT010'
                               AND d.dd_name='SYSIN'""", (job,)).fetchone()

    def test_the_aged_rows_are_the_older_builds(self):
        # the shape the build before the item wrote (its real build: `ZQC01 S1.SRT SYSIN PROD.ZQ.PARMLIB(ZQC01C)
        # ... ZQDEFC`) - not the default's dataset, which it never wrote
        self.assertEqual(tuple(self._sysin(self.aged, "KVOJ01")),
                         ("PROD.CMN.PARMLIB(KVJOBC)", "KVDEFC", CARDS["KVDEFC"]))
        self.assertEqual(tuple(self._sysin(self.aged, "KVOJ03")),
                         ("PROD.CMN.PARMLIB(KVOMISS)", "KVOMISS", CARDS["KVDEFC"]))
        self.assertEqual(tuple(self._sysin(self.aged, "KVOJ05")), ("PROD.CMN.PARMLIB(KVJOBC)", "KVDEFN", None))
        self.assertEqual(tuple(self._sysin(self.aged, "KVOJ07"))[:2], ("PROD.CMN.PARMLIB(KVDEFN)", "KVDEFN"))
        # and this build's
        self.assertEqual(tuple(self._sysin(self.conn, "KVOJ01")),
                         ("PROD.CMN.PARMLIB(KVJOBC)", "KVJOBC", CARDS["KVJOBC"]))
        self.assertEqual(tuple(self._sysin(self.conn, "KVOJ03")), ("PROD.CMN.PARMLIB(KVOMISS)", "KVOMISS", None))

    def test_coverage_names_what_the_jobs_read_on_both(self):
        self.assertEqual(query.card_members_not_indexed(self.conn), OLD_MISSING)
        self.assertEqual(query.card_members_not_indexed(self.aged), OLD_MISSING)
        for conn in (self.conn, self.aged):
            section = query.cmd_coverage(conn).split("### Control-card members referenced by JCL but NOT indexed")[1]
            self.assertIn("| KVOMISS | 1 |", section)
            self.assertNotIn("KVDEFC", section.split("###")[0])

    def test_program_lists_the_jobs_that_read_the_member(self):
        want = {"KVJOBC": ["KVOJ01", "KVOJ05"], "KVDEFC": ["KVOJ04"], "KVDEFN": ["KVOJ06"], "KVOMISS": ["KVOJ03"],
                "KVJMISS": ["KVOJ02"]}
        for conn in (self.conn, self.aged):
            for name, jobs in want.items():
                out = query.cmd_program(conn, name)
                section = out.split("### Used as control cards by")[1].split("###")[0]
                got = sorted({ln.split("|")[1].strip() for ln in section.splitlines() if ln.startswith("| KVOJ")})
                self.assertEqual(got, jobs, (name, section))

    def test_program_says_whether_the_text_is_loaded(self):
        # LESSONS 228. Wrong words guarded: 'Its text is loaded as that step's cards' under 'Source not indexed'
        for conn in (self.conn, self.aged):
            out = query.cmd_program(conn, "KVDEFN")
            self.assertIn("**Source not indexed**", out)
            self.assertIn("**Not in the estate as control cards**", out)
            self.assertNotIn("Its text is loaded", out)
            out = query.cmd_program(conn, "KVJOBC")
            self.assertIn("Its text is loaded as that step's cards", out)
            self.assertNotIn("Not in the estate as control cards", out)

    def test_the_card_kinds_are_the_builds(self):
        self.assertEqual(query.CARD_KINDS, build.CARD_KINDS)

    def test_the_member_a_row_reads(self):
        read = query.card_member_read
        self.assertEqual(read("SYSIN", "PROD.CMN.PARMLIB(KVJOBC)", "KVDEFC"), "KVJOBC")
        self.assertEqual(read("SRT010.SYSIN", "PROD.CMN.PARMLIB(KVJOBC)", None), "KVJOBC")
        self.assertEqual(read("SYSIN", "PROD.CMN.CARDS.KVJOBC", "KVJOBC"), "KVJOBC")        # by the last qualifier
        self.assertIsNone(read("STEPLIB", "PROD.CMN.LOADLIB(KVPGM)", None))
        self.assertIsNone(read("SYSIN", "PROD.CMN.PARMLIB(<VAR>)", None))
        self.assertIsNone(read("SYSIN", None, None))


class TheReproductions(unittest.TestCase):
    """tools/synth/repro/F09 and F10 built in process: every expect.json truth holds (verify.py says FIXED)."""

    def check(self, rid):
        src = os.path.join(REPRO, rid)
        td = tempfile.mkdtemp()
        try:
            shutil.copytree(os.path.join(src, "estate"), os.path.join(td, "estate"))
            db = os.path.join(td, "t.db")
            _run_build(os.path.join(td, "estate"), db, "--rebuild")
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
                        self.assertNotIn(ck["symptom_has"], text, (rid, ck["query"]))
            finally:
                conn.close()
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_f09_gdg_same_job(self):
        self.check("F09-gdg-same-job")

    def test_f10_proc_card_member_symbolic(self):
        self.check("F10-proc-card-member-symbolic")

    def test_the_f10_job_is_jcl_the_system_reads(self):
        # its EXEC statement ran to column 82 when it was written: columns 72-80 are not JCL, and CARDS= was lost
        path = os.path.join(REPRO, "F10-proc-card-member-symbolic", "estate", "POLICY", "PROD.POL.JCLLIB", "R10JOB.jcl")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertTrue(all(len(ln) <= 71 for ln in lines), [ln for ln in lines if len(ln) > 71])

    def test_verify_says_fixed(self):
        sys.path.insert(0, REPRO)
        import verify                                                   # noqa: E402
        out = tempfile.mkdtemp()
        try:
            got = verify.verify(os.path.join(REPRO, "F09-gdg-same-job"), out)
            got += verify.verify(os.path.join(REPRO, "F10-proc-card-member-symbolic"), out)
        finally:
            shutil.rmtree(out, ignore_errors=True)
        self.assertEqual([v for _l, v, _d in got], ["FIXED"] * 4, got)


class TheDocsSayIt(unittest.TestCase):
    """ROADMAP 29, LESSONS 222-224, README's direction and card-member paragraphs, the Field Manual's direction
    table, the two reproductions' READMEs and the findings report say what changed."""

    def read(self, *path):
        with open(os.path.join(ROOT, *path), encoding="utf-8") as fh:
            return fh.read()

    def test_lessons_rows(self):
        text = self.read("LESSONS.md")
        for n in (222, 223, 224, 225, 226, 227, 228):
            row = next((ln for ln in text.splitlines() if ln.startswith(f"| {n} |")), "")
            self.assertIn("tests/test_jcl_generations_and_cards.py", row, n)
            self.assertEqual(row.replace("\|", "").count("|") - 1, 5, n)      # number + four cells

    def test_roadmap_and_readme(self):
        self.assertIn("29. **Delivered in the batch - a PROC's card member named by a symbolic", self.read("ROADMAP.md"))
        readme = self.read("README.md")
        self.assertIn("recorded `input [gdg_same_job]` - never a second writer", readme)
        self.assertIn("the one the calling job's `EXEC PROC=...,CARDS=` names", readme)
        self.assertIn('<td class="mono">gdg_same_job</td>', self.read("docs", "FieldManual.html"))

    def test_the_older_index_sentence(self):
        # LESSONS 227. Wrong words guarded: 'an effective step still names the default and is counted as it is' -
        # the older build kept the job's dataset beside the default's card member and text
        said = "counted as it is"
        row = next(ln for ln in self.read("LESSONS.md").splitlines() if ln.startswith("| 222 |"))
        for where, text in (("row 222", row), ("ROADMAP", self.read("ROADMAP.md")),
                            ("docstring", query.card_members_not_indexed.__doc__)):
            self.assertNotIn(said, text, where)
            self.assertIn("dataset", text, where)
        item = self.read("ROADMAP.md").split("29. **Delivered in the batch")[1].split("\n\n")[0]
        self.assertIn("take the member from the dataset", item)
        self.assertIn("LESSONS 222-228", item)

    def test_card_seq_assumed_says_what_the_row_records(self):
        # LESSONS 225. Wrong words guarded: 'a card deck with no sequence field; the order on disk was assumed'
        meaning, fix = query.UNRESOLVED_MEANING["card_seq_assumed"]
        self.assertIn("named like the dataset's last qualifier", meaning)
        self.assertNotIn("order on disk", meaning + fix)
        job = jcl.parse_jcl("//KVSEQ    JOB (ACCT),'SEQ CARDS',CLASS=A\n//S1       EXEC PGM=SORT\n"
                            "//SYSIN    DD   DSN=PROD.CMN.CARDS.KVJOBC,DISP=SHR\n//\n", member_lookup=CARDS.get)
        self.assertIn(("card_seq_assumed", "SYSIN: cards taken from member KVJOBC matching the last qualifier of "
                                            "PROD.CMN.CARDS.KVJOBC", 3), job.unresolved)

    def test_the_reproductions_and_the_report(self):
        for rid in ("F09-gdg-same-job", "F10-proc-card-member-symbolic"):
            self.assertIn("Fixed by ROADMAP re-parse item 29", self.read("tools", "synth", "repro", rid, "README.md"))
        self.assertIn("(ROADMAP re-parse item 29, LESSONS 222-224)", self.read("docs", "SYNTH-findings-2026-09-25.md"))


if __name__ == "__main__":
    unittest.main()
