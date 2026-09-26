"""
The JCL shapes a production estate has that a tidy fixture does not - each
one found by a coverage audit before it produced a wrong answer at work:

  trailing comments on operands, quoted PARMs continued at column 71,
  several JOB cards in one member, JOBLIB, IF/THEN/ELSE guards and JOB COND,
  PARM.PS= / COND.PS= on EXEC PROC, concatenation and DUMMY overrides,
  override values leaking into later steps, job-level PARM/PGM symbols,
  run-time symbols in DSNs, IEFBR14 housekeeping counted as writers,
  DUMMY as a substring, IKJEFT01 CALL and double RUN, IMS utility regions,
  Easytrieve steps, batch FTP / Connect:Direct (credentials redacted),
  nested PROC pass-through, absolute GDG generations, department-aware PROC
  resolution (JCLLIB ORDER), and PROC-default phantom datasets.
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, fetch, jcl, query, reader  # noqa: E402


def _job(text, **kw):
    return jcl.parse_jcl(text, **kw)


def _dd(step, name):
    return next(d for d in step.dds if d.dd_name.upper() == name.upper())


class Reader(unittest.TestCase):

    def test_operand_field_ends_at_the_first_blank(self):
        j = _job("//J JOB (A)\n"
                 "//S1 EXEC PGM=PGMA,PARM='X Y'   RUN THE EXTRACT\n"
                 "//DD1 DD DISP=SHR,DSN=PROD.X   INPUT MASTER\n"
                 "//S2 EXEC PGM=PGMB,   RUN STEP TWO\n"
                 "//   PARM='Y'\n")
        self.assertEqual(_dd(j.steps[0], "DD1").dsn_resolved, "PROD.X")
        self.assertEqual(j.steps[0].parm, "X Y")
        self.assertEqual(j.steps[1].parm, "Y")                     # comma, blank, comment = continued
        self.assertEqual(len(j.steps), 2)                          # no bogus 'PARM' statement
        st = reader.read_jcl("//DD1 DD DISP=SHR,DSN=PROD.X   INPUT MASTER\n")[0]
        self.assertEqual(st.comment, "INPUT MASTER")

    def test_quoted_parm_continued_at_column_71(self):
        line1 = "//S1 EXEC PGM=PGMA,PARM='" + "A" * 45 + "X"     # ends exactly at col 71
        self.assertEqual(len(line1), 71)
        j = _job("//J JOB (A)\n" + line1 + "\n" + "//" + " " * 13 + "MNOP'\n")
        self.assertTrue(j.steps[0].parm.endswith("XMNOP"))
        self.assertEqual(len(j.steps[0].parm), 50)

    def test_if_operands_keep_their_blanks(self):
        st = reader.read_jcl("//    IF (S1.RC > 4) THEN\n")[0]
        self.assertEqual((st.op, st.operands), ("IF", "(S1.RC > 4) THEN"))


class JobShapes(unittest.TestCase):

    def test_several_job_cards_in_one_member(self):
        jobs = jcl.parse_jcl_all("//JOBA JOB (A),'X'\n//S1 EXEC PGM=PGMA\n//DD1 DD DSN=A.B,DISP=SHR\n//\n"
                                 "//JOBB JOB (A),'Y'\n//S1 EXEC PGM=PGMB\n//DD1 DD DSN=C.D,DISP=(NEW,CATLG)\n")
        self.assertEqual([j.job_name for j in jobs], ["JOBA", "JOBB"])
        self.assertEqual(_dd(jobs[0].steps[0], "DD1").dsn_resolved, "A.B")
        self.assertEqual(_dd(jobs[1].steps[0], "DD1").dsn_resolved, "C.D")

    def test_joblib_reaches_every_step_without_steplib(self):
        j = _job("//J JOB (A)\n//JOBLIB DD DSN=PROD.LOAD,DISP=SHR\n//       DD DSN=PROD.LOAD2,DISP=SHR\n"
                 "//S1 EXEC PGM=PGMA\n//S2 EXEC PGM=PGMB\n//STEPLIB DD DSN=TEST.LOAD,DISP=SHR\n")
        self.assertEqual([d.dsn_resolved for d in j.job_dds], ["PROD.LOAD", "PROD.LOAD2"])
        s1 = [d for d in j.steps[0].dds if d.dd_name == "JOBLIB"]
        self.assertEqual([(d.dsn_resolved, d.mode_source) for d in s1], [("PROD.LOAD", "joblib"), ("PROD.LOAD2", "joblib")])
        self.assertEqual([d.dsn_resolved for d in j.steps[1].dds], ["TEST.LOAD"])

    def test_if_then_else_guards_and_job_cond(self):
        j = _job("//J JOB (A),COND=(4,LT)\n//S1 EXEC PGM=PGMA\n//    IF (S1.RC > 4) THEN\n//S2 EXEC PGM=PGMFAIL\n"
                 "//    ELSE\n//S3 EXEC PGM=PGMOK\n//    ENDIF\n//S4 EXEC PGM=PGMLAST\n")
        self.assertEqual(j.job_cond, "(4,LT)")
        self.assertEqual([s.guard for s in j.steps], [None, "IF (S1.RC > 4)", "ELSE of IF (S1.RC > 4)", None])

    def test_override_does_not_leak_into_later_job_steps(self):
        j = _job("//J JOB (A)\n// SET HLQ=SETVAL\n//S1 EXEC NIGHTLY,HLQ=OVR\n//S2 EXEC PGM=PGMB\n"
                 "//DD1 DD DSN=&HLQ..FILE,DISP=SHR\n")
        self.assertEqual(_dd(j.steps[1], "DD1").dsn_resolved, "SETVAL.FILE")
        self.assertEqual(j.steps[0].sym_overrides, {"HLQ": "OVR"})

    def test_job_level_parm_pgm_symbols_and_runtime_symbols(self):
        j = _job("//J JOB (A)\n// SET DT=20260912,PGM=CLMPOST,PSB=CLMPOSTP\n//S1 EXEC PGM=PGMA,PARM='&DT'\n"
                 "//S2 EXEC PGM=DFSRRC00,PARM='DLI,&PGM,&PSB'\n//S3 EXEC PGM=PGMB\n"
                 "//DD1 DD DSN=PROD.CLM.D&LYYMMDD,DISP=SHR\n//DD2 DD DSN=PROD.CLM.EXTRACT.D%%ODATE,DISP=SHR\n")
        self.assertEqual(j.steps[0].parm, "20260912")
        self.assertEqual(j.steps[1].effective_pgm, "CLMPOST")
        self.assertIn("PSB CLMPOSTP", j.steps[1].notes)
        self.assertEqual(_dd(j.steps[2], "DD1").dsn_resolved, "PROD.CLM.D<VAR>")
        self.assertEqual(_dd(j.steps[2], "DD2").dsn_resolved, "PROD.CLM.EXTRACT.D<VAR>")
        kinds = sorted(u[0] for u in j.unresolved)
        self.assertEqual(kinds, ["scheduler_symbol", "symbolic"])

    def test_iefbr14_is_not_a_writer(self):
        j = _job("//J JOB (A)\n//DEL EXEC PGM=IEFBR14\n//D1 DD DSN=PROD.CLM.EXTRACT,DISP=(MOD,DELETE,DELETE),SPACE=(TRK,1)\n"
                 "//D2 DD DSN=PROD.CLM.WORK,DISP=(NEW,CATLG,DELETE),SPACE=(TRK,1)\n//D3 DD DSN=PROD.CLM.KEEP,DISP=OLD\n")
        s = j.steps[0]
        self.assertEqual([(d.mode, d.mode_source) for d in s.dds],
                         [("delete", "iefbr14_disp"), ("alloc", "iefbr14_disp"), ("none", "iefbr14_disp")])

    def test_dummy_is_the_first_operand_not_a_substring(self):
        j = _job("//J JOB (A)\n//S1 EXEC PGM=PGMA\n//IN1 DD DUMMY\n//IN2 DD DSN=NULLFILE\n"
                 "//IN3 DD DSN=PROD.CLM.DUMMY.FILE,DISP=SHR\n")
        self.assertEqual([d.mode for d in j.steps[0].dds], ["dummy", "dummy", "unknown"])
        self.assertEqual(_dd(j.steps[0], "IN3").dsn_resolved, "PROD.CLM.DUMMY.FILE")

    def test_absolute_gdg_generation_joins_the_gdg(self):
        j = _job("//J JOB (A)\n//S1 EXEC PGM=PGMA\n//IN DD DSN=PROD.CLM.EXTRACT.G0012V00,DISP=SHR\n")
        d = _dd(j.steps[0], "IN")
        self.assertEqual((d.dsn_resolved, d.gdg_rel), ("PROD.CLM.EXTRACT", "G0012V00"))


class Launchers(unittest.TestCase):

    def test_tso_call_and_two_runs(self):
        j = _job("//J JOB (A)\n//S3 EXEC PGM=IKJEFT01\n//SYSTSIN DD *\n  CALL 'PROD.LOAD(CLMFIX)' 'PARM1'\n/*\n"
                 "//S4 EXEC PGM=IKJEFT01\n//SYSTSIN DD *\n  DSN SYSTEM(DB2P)\n  RUN PROGRAM(PGM1) PLAN(P1) PARMS('A')\n"
                 "  RUN PROGRAM(PGM2) PLAN(P2)\n  END\n/*\n")
        self.assertEqual(j.steps[0].effective_pgm, "CLMFIX")
        self.assertEqual((j.steps[1].effective_pgm, j.steps[1].also_runs), ("PGM1", ["PGM2"]))
        self.assertTrue(any("PARMS('A')" in n for n in j.steps[1].notes))

    def test_db2_utility_run_under_tso_is_not_a_program(self):
        j = _job("//J JOB (A)\n//S1 EXEC PGM=IKJEFT01\n//SYSTSIN DD *\n  DSN SYSTEM(DB2P)\n"
                 "  RUN PROGRAM(DSNTIAUL) PLAN(DSNTIB12) PARMS('SQL')\n/*\n")
        self.assertEqual(j.steps[0].effective_pgm, "*DSNTIAUL*")

    def test_ims_utility_region_names_a_dbd_not_a_psb(self):
        j = _job("//J JOB (A)\n//S1 EXEC PGM=DFSRRC00,PARM='ULU,DFSUDMP0,CLMDBD'\n"
                 "//S2 EXEC PGM=DFSRRC00,PARM='ULU,DFSURGL0,CLMDBD'\n//S3 EXEC PGM=DFSRRC00,PARM='DLI,CLMPOST,CLMPSB'\n")
        self.assertTrue(any(n.startswith("DBD CLMDBD") for n in j.steps[0].notes))
        self.assertFalse(any(n.startswith("PSB") for n in j.steps[0].notes))
        self.assertEqual((_dd(j.steps[0], "*DBD*").mode, _dd(j.steps[1], "*DBD*").mode), ("input", "output"))
        self.assertIn("PSB CLMPSB", j.steps[2].notes)

    def test_easytrieve_step(self):
        j = _job("//J JOB (A)\n//S1 EXEC PGM=EZTPA00\n//CLMFILE DD DSN=PROD.CLM.MASTER,DISP=SHR\n//RPTOUT DD SYSOUT=*\n"
                 "//SYSIN DD *\nFILE CLMFILE\n  CLM-NO  1 10 A\n  CLM-STAT 25 2 A\nJOB INPUT CLMFILE\n"
                 "  IF CLM-STAT = 'AC'\n    PRINT RPT1\n  END-IF\n/*\n")
        s = j.steps[0]
        self.assertEqual(s.effective_pgm, "*EASYTRIEVE*")
        self.assertEqual((_dd(s, "CLMFILE").mode, _dd(s, "CLMFILE").mode_source), ("input", "easytrieve_file"))
        fields = jcl.easytrieve_fields(_dd(s, "SYSIN").sysin_text)
        self.assertEqual([(f[1], f[2], f[3]) for f in fields], [(1, 10, "CH"), (25, 2, "CH")])

    def test_ftp_reads_input_dd_and_never_keeps_the_password(self):
        j = _job("//J JOB (A)\n//S2 EXEC PGM=FTP,PARM='ftp.vendor.com (EXIT'\n//INPUT DD *\nuserid\nsecretpw\n"
                 "put 'PROD.CLM.VENDOR.FEED' claims.txt\nget ack.txt 'PROD.CLM.VENDOR.ACK'\nquit\n/*\n")
        s = j.steps[0]
        self.assertIn("FTP host ftp.vendor.com", s.notes)
        self.assertNotIn("secretpw", _dd(s, "INPUT").sysin_text)
        self.assertNotIn("userid", _dd(s, "INPUT").sysin_text)
        self.assertIn("put 'PROD.CLM.VENDOR.FEED'", _dd(s, "INPUT").sysin_text)
        ftp = [(d.dsn_resolved, d.mode) for d in s.dds if d.dd_name == "*FTP*"]
        self.assertEqual(ftp, [("PROD.CLM.VENDOR.FEED", "input"), ("PROD.CLM.VENDOR.ACK", "output")])
        self.assertFalse(any(u[0] == "launcher_parm" for u in j.unresolved))

    def test_connect_direct_process_member_is_followed(self):
        process = {"SENDCLM": "SENDCLM PROCESS SNODE=VENDORND\n STEP1 COPY FROM (DSN=&DSN PNODE) -\n"
                              "       TO (DSN=VENDOR.IN.FILE SNODE DISP=RPL)\n"}
        j = _job("//J JOB (A)\n//S3 EXEC PGM=DMBATCH,PARM=(YYSLYNN)\n//SYSIN DD *\n SIGNON USERID=(me,secret)\n"
                 " SUBMIT PROC=SENDCLM SNODE=VENDORND -\n   &DSN=PROD.CLM.VENDOR.FEED\n SIGNOFF\n/*\n",
                 member_lookup=process.get)
        s = j.steps[0]
        self.assertNotIn("secret", _dd(s, "SYSIN").sysin_text)
        ndm = {d.dsn_resolved: d.mode for d in s.dds if d.dd_name == "*NDM*"}
        self.assertEqual(ndm.get("PROD.CLM.VENDOR.FEED"), "unknown")       # passed as &DSN: direction is in the process
        self.assertEqual(ndm.get("VENDOR.IN.FILE"), "output")
        self.assertTrue(any("SNODE VENDORND" in n for n in s.notes))


PROC_NIGHTLY = ("//NIGHTLY PROC HLQ=TEST\n//PS010 EXEC PGM=CLMPOST,PARM='DEFAULTPARM'\n"
                "//STEPLIB DD DSN=PROD.LOAD1,DISP=SHR\n//        DD DSN=PROD.LOAD2,DISP=SHR\n"
                "//SYSIN DD DSN=PROD.PARMLIB(X),DISP=SHR\n//M DD DSN=&HLQ..CLM.MASTER,DISP=SHR\n"
                "//PS020 EXEC PGM=CLMRPT,PARM='RPTPARM'\n//    IF (PS010.RC = 0) THEN\n//PS030 EXEC PGM=CLMOK\n//    ENDIF\n")


class ProcExpansion(unittest.TestCase):

    def _expand(self, job_text, procs):
        parsed = {k: jcl.parse_jcl(v) for k, v in procs.items()}
        job = jcl.parse_jcl(job_text)
        eff = jcl.expand_job(job, parsed.get)
        return job, {s.step_name: s for s in eff}

    def test_parm_and_cond_qualified_overrides(self):
        _job_, by = self._expand("//J JOB (A)\n//S1 EXEC NIGHTLY,PARM.PS010='JOBPARM',COND.PS020=(0,NE)\n",
                                 {"NIGHTLY": PROC_NIGHTLY})
        self.assertEqual(by["S1.PS010"].parm, "JOBPARM")
        self.assertEqual(by["S1.PS020"].parm, "RPTPARM")
        self.assertEqual(by["S1.PS020"].cond, "(0,NE)")
        self.assertEqual(by["S1.PS030"].guard, "IF (PS010.RC = 0)")

    def test_bare_parm_on_exec_proc_goes_to_the_first_step_only(self):
        _job_, by = self._expand("//J JOB (A)\n//S1 EXEC NIGHTLY,PARM='ONLYFIRST'\n", {"NIGHTLY": PROC_NIGHTLY})
        self.assertEqual(by["S1.PS010"].parm, "ONLYFIRST")
        self.assertIsNone(by["S1.PS020"].parm)                     # JCL nullifies the others

    def test_concatenation_override_entry_by_entry_and_dummy(self):
        _job_, by = self._expand("//J JOB (A)\n//S1 EXEC NIGHTLY\n//PS010.STEPLIB DD DSN=TEST.LOAD,DISP=SHR\n"
                                 "//              DD DSN=PROD.LOAD1,DISP=SHR\n//PS010.SYSIN DD DUMMY\n",
                                 {"NIGHTLY": PROC_NIGHTLY})
        s = by["S1.PS010"]
        self.assertEqual([d.dsn_resolved for d in s.dds if d.dd_name == "STEPLIB"], ["TEST.LOAD", "PROD.LOAD1"])
        sysin = _dd(s, "SYSIN")
        self.assertEqual((sysin.mode, sysin.dsn_resolved, sysin.card_member), ("dummy", None, None))

    def test_joblib_reaches_expanded_steps_without_steplib(self):
        _job_, by = self._expand("//J JOB (A)\n//JOBLIB DD DSN=PROD.JOBLOAD,DISP=SHR\n//S1 EXEC NIGHTLY\n",
                                 {"NIGHTLY": PROC_NIGHTLY})
        self.assertEqual([d.dsn_resolved for d in by["S1.PS010"].dds if d.dd_name == "STEPLIB"],
                         ["PROD.LOAD1", "PROD.LOAD2"])                 # has its own STEPLIB
        self.assertEqual([(d.dsn_resolved, d.mode_source) for d in by["S1.PS020"].dds if d.dd_name == "JOBLIB"],
                         [("PROD.JOBLOAD", "joblib")])

    def test_nested_proc_passes_the_value_not_the_symbol(self):
        job, by = self._expand("//J JOB (A)\n//S1 EXEC OUTER,HLQ=JOB\n",
                               {"OUTER": "//OUTER PROC HLQ=OUT\n//O1 EXEC INNER,HLQ=&HLQ\n",
                                "INNER": "//INNER PROC HLQ=INN,Y=9\n//I1 EXEC PGM=PGMI,PARM='&Y'\n//D DD DSN=&HLQ..I,DISP=SHR\n"})
        s = by["S1.O1.I1"]
        self.assertEqual(_dd(s, "D").dsn_resolved, "JOB.I")
        self.assertEqual(s.parm, "9")                                # INNER's own default, not OUTER's
        self.assertFalse(any(u[0] == "symbolic" for u in job.unresolved))

    def test_override_does_not_leak_into_the_job_after_expansion(self):
        job, by = self._expand("//J JOB (A)\n// SET HLQ=SETVAL\n//S1 EXEC NIGHTLY,HLQ=OVR\n//S2 EXEC PGM=PGMB\n"
                               "//DD1 DD DSN=&HLQ..FILE,DISP=SHR\n", {"NIGHTLY": PROC_NIGHTLY})
        self.assertEqual(_dd(by["S1.PS010"], "M").dsn_resolved, "OVR.CLM.MASTER")
        self.assertEqual(_dd(by["S2"], "DD1").dsn_resolved, "SETVAL.FILE")


class BuildLevel(unittest.TestCase):
    """Two departments each own a PROC called NIGHTLY; a JOBLIB job; an
    IEFBR14 cleanup; a job stream member; PROC defaults."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        files = {
            "CLAIMS/PROD.CLAIMS.PROCLIB/NIGHTLY.prc": "//NIGHTLY PROC HLQ=T\n//PS1 EXEC PGM=CLMPOST\n"
                                                     "//M DD DSN=&HLQ..CLM.MASTER,DISP=SHR\n",
            "POLICY/PROD.POLICY.PROCLIB/NIGHTLY.prc": "//NIGHTLY PROC HLQ=T\n//PS1 EXEC PGM=POLPOST\n"
                                                     "//M DD DSN=&HLQ..POL.MASTER,DISP=SHR\n",
            "POLICY/PROD.POLICY.JCLLIB/POLNIGHT.jcl": "//POLNIGHT JOB (A)\n//  JCLLIB ORDER=(PROD.POLICY.PROCLIB)\n"
                                                     "//S1 EXEC NIGHTLY,HLQ=PROD\n",
            "CLAIMS/PROD.CLAIMS.JCLLIB/CLMNIGHT.jcl": "//CLMNIGHT JOB (A),COND=(4,LT)\n"
                                                     "//JOBLIB DD DSN=PROD.CLM.LOAD,DISP=SHR\n"
                                                     "//S1 EXEC NIGHTLY,HLQ=PROD\n"
                                                     "//    IF (S1.PS1.RC > 4) THEN\n//S2 EXEC PGM=CLMFAIL\n//    ENDIF\n"
                                                     "//DEL EXEC PGM=IEFBR14\n"
                                                     "//D1 DD DSN=PROD.CLM.MASTER,DISP=(MOD,DELETE,DELETE)\n",
            "CLAIMS/PROD.CLAIMS.JCLLIB/STREAM.jcl": "//JOBA JOB (A)\n//S1 EXEC PGM=PGMA\n//DD1 DD DSN=A.B,DISP=SHR\n//\n"
                                                   "//JOBB JOB (A)\n//S1 EXEC PGM=PGMB\n//DD1 DD DSN=C.D,DISP=(NEW,CATLG)\n",
            # a POLICY job without JCLLIB: its own department's PROC must still win
            "POLICY/PROD.POLICY.JCLLIB/POLDAY.jcl": "//POLDAY JOB (A)\n//S1 EXEC NIGHTLY,HLQ=PROD\n",
        }
        for rel, text in files.items():
            p = os.path.join(cls.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        cfg = fetch.load_config(os.path.join(cls.td, "nope.json"))
        cfg["local_root"] = cls.root
        cfg["sources"] = [fetch.new_source("PROD.CLAIMS.PROCLIB", "proc", system="CLAIMS"),
                          fetch.new_source("PROD.CLAIMS.JCLLIB", "jcl", system="CLAIMS"),
                          fetch.new_source("PROD.POLICY.PROCLIB", "proc", system="POLICY"),
                          fetch.new_source("PROD.POLICY.JCLLIB", "jcl", system="POLICY")]
        man = os.path.join(cls.td, "manifest.json")
        fetch.write_manifest(cfg, man)
        cls.db = os.path.join(cls.td, "t.db")
        build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet", "--manifest", man])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def _eff(self, job, step):
        return self.conn.execute("SELECT * FROM step s JOIN job j ON j.id=s.job_id WHERE j.job_name=? AND s.step_name=?",
                                 (job, step)).fetchone()

    def test_jcllib_order_picks_the_departments_proc(self):
        self.assertEqual(self._eff("POLNIGHT", "S1.PS1")["effective_pgm"], "POLPOST")
        self.assertEqual(self._eff("CLMNIGHT", "S1.PS1")["effective_pgm"], "CLMPOST")
        self.assertIn("PROD.POL.MASTER", query.cmd_job(self.conn, "POLNIGHT"))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM unresolved WHERE kind='ambiguous_proc'").fetchone())

    def test_same_department_wins_without_jcllib(self):
        self.assertEqual(self._eff("POLDAY", "S1.PS1")["effective_pgm"], "POLPOST")

    def test_dataset_lineage_credits_the_right_job(self):
        out = query.cmd_dataset(self.conn, "POL.MASTER")
        self.assertIn("POLNIGHT", out)
        self.assertNotIn("CLMNIGHT", out)
        self.assertNotIn("T.POL.MASTER", out)                       # PROC default hidden
        self.assertIn("row(s) of PROC members' own steps (read with the PROC's defaults) hidden", out)

    def test_job_dossier_shows_joblib_guard_and_cond(self):
        out = query.cmd_job(self.conn, "CLMNIGHT")
        self.assertIn("JOB card COND=(4,LT)", out)
        self.assertIn("JOBLIB: PROD.CLM.LOAD", out)
        self.assertIn("runs only when** `IF (S1.PS1.RC > 4)`", out)
        self.assertIn("[joblib]", out)
        self.assertIn("delete [iefbr14_disp]", out)

    def test_iefbr14_delete_is_not_a_writer_in_dataset(self):
        out = query.cmd_dataset(self.conn, "PROD.CLM.MASTER")
        self.assertIn("delete [iefbr14_disp]", out)
        self.assertNotIn("Crosses departments", out)

    def test_job_stream_member_yields_two_jobs(self):
        self.assertEqual([r[0] for r in self.conn.execute(
            "SELECT job_name FROM job WHERE job_name IN ('JOBA','JOBB') ORDER BY job_name")], ["JOBA", "JOBB"])
        self.assertIn("JOBA", query.cmd_dataset(self.conn, "A.B"))
        self.assertNotIn("JOBB", query.cmd_dataset(self.conn, "A.B"))

    def test_ambiguous_proc_is_reported_when_nothing_decides(self):
        td = tempfile.mkdtemp()
        try:
            root = os.path.join(td, "estate")
            for rel, text in {"LIB1/NIGHTLY.prc": "//NIGHTLY PROC\n//PS1 EXEC PGM=ONE\n",
                              "LIB2/NIGHTLY.prc": "//NIGHTLY PROC\n//PS1 EXEC PGM=TWO\n",
                              "JCL/J1.jcl": "//J1 JOB (A)\n//S1 EXEC NIGHTLY\n"}.items():
                p = os.path.join(root, *rel.split("/"))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(text)
            db = os.path.join(td, "t.db")
            build._main([root, "--db", db, "--rebuild", "--quiet"])
            c = query.connect(db)
            row = c.execute("SELECT detail FROM unresolved WHERE kind='ambiguous_proc'").fetchone()
            self.assertIsNotNone(row)
            self.assertIn("NIGHTLY", row[0])
            c.close()
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
