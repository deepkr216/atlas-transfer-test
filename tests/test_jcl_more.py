"""
Four JCL shapes the first fixtures did not have, each common in production:

  1. control cards kept in a PARMLIB member (`//SYSIN DD DSN=LIB(MEMBER)`),
     not instream - without the member's text the step has no cards, so no
     effective program, no sort fields, no IDCAMS ops;
  2. an instream PROC (`// PROC ... // PEND`) executed by a job step - it
     shadows a cataloged PROC of the same name;
  3. a &&TEMP dataset - it exists only between steps of THIS job and must
     never link two jobs' lineage;
  4. DSN=*.STEP.DD referbacks, inside a PROC and across the PROC boundary.

Each test names the wrong answer it guards against.
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, jcl, query, reader  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def _text(name):
    return reader.load(os.path.join(FIX, name))[0]


CARDS = {"SRTCLM": _text("SRTCLM.ctl"), "RUNCLM": _text("RUNCLM.ctl")}


class ParseLevel(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.job = jcl.parse_jcl(_text("REFERJOB.jcl"), member_lookup=CARDS.get)
        cls.eff = jcl.expand_job(cls.job, lambda n: None)     # no cataloged PROCs at all
        cls.by = {s.step_name: s for s in cls.eff}

    def _dd(self, step, name):
        return next(d for d in self.by[step].dds if d.dd_name.upper() == name)

    # --- instream PROC -----------------------------------------------------
    def test_instream_proc_is_collected_not_flattened_into_the_job(self):
        # Wrong answer guarded: SORT1/RPT1 counted as job steps, run twice.
        self.assertEqual(list(self.job.instream_procs), ["SRTPROC"])
        self.assertEqual([s.step_name for s in self.job.steps], ["STEP010", "STEP020"])
        self.assertEqual([s.step_name for s in self.job.instream_procs["SRTPROC"].steps], ["SORT1", "RPT1"])
        self.assertEqual(self.job.instream_procs["SRTPROC"].symbolics, {"HLQ": "PROD"})

    def test_instream_proc_expands_with_exec_override(self):
        # Wrong answer guarded: "PROC SRTPROC not found" because it is not cataloged.
        self.assertEqual([s.step_name for s in self.eff], ["STEP010.SORT1", "STEP010.RPT1", "STEP020"])
        self.assertEqual(self._dd("STEP010.SORT1", "SORTIN").dsn_resolved, "TEST.CLM.EXTRACT")
        self.assertFalse(any(u[0] == "missing_proc" for u in self.job.unresolved))

    # --- control cards in a PARMLIB member --------------------------------
    def test_card_member_text_is_attached_to_the_dd(self):
        d = self._dd("STEP010.SORT1", "SYSIN")
        self.assertEqual(d.card_member, "SRTCLM")
        self.assertEqual(d.dsn_resolved, "TEST.PARMLIB(SRTCLM)")
        self.assertIn("SORT FIELDS=(1,12,CH,A)", d.sysin_text)
        self.assertIn(("SORT", 1, 12, "CH", "SORT FIELDS=(1,12,CH,A)"),
                      [(k, p, ln, f, r.strip()) for (k, p, ln, f, r) in jcl.sort_card_fields(d.sysin_text)])

    def test_launcher_resolves_through_the_card_member(self):
        # Wrong answer guarded: RPT1 "runs IKJEFT01" - the program is in RUNCLM.
        s = self.by["STEP010.RPT1"]
        self.assertEqual(s.launcher, "IKJEFT01")
        self.assertEqual(s.effective_pgm, "CLMRPT2")

    def test_missing_card_member_still_records_the_name(self):
        j = jcl.parse_jcl(_text("REFERJOB.jcl"))                # no lookup at all
        d = next(d for d in j.instream_procs["SRTPROC"].steps[0].dds if d.dd_name == "SYSIN")
        self.assertEqual(d.card_member, "SRTCLM")
        self.assertIsNone(d.sysin_text)

    def test_steplib_member_is_a_load_module_not_cards(self):
        j = jcl.parse_jcl("//J JOB A\n//S1 EXEC PGM=X\n//STEPLIB DD DSN=P.LOADLIB(X),DISP=SHR\n",
                          member_lookup=lambda n: "BOOM")
        self.assertIsNone(j.steps[0].dds[0].card_member)
        self.assertIsNone(j.steps[0].dds[0].sysin_text)

    def test_gdg_relative_is_not_a_member(self):
        j = jcl.parse_jcl("//J JOB A\n//S1 EXEC PGM=X\n//IN DD DSN=P.CLM.EXTRACT(0),DISP=SHR\n",
                          member_lookup=lambda n: "BOOM")
        self.assertIsNone(j.steps[0].dds[0].card_member)
        self.assertEqual(j.steps[0].dds[0].gdg_rel, "0")

    # --- &&TEMP ------------------------------------------------------------
    def test_temp_is_job_local(self):
        d = self._dd("STEP010.SORT1", "SORTOUT")
        self.assertTrue(d.is_temp)
        self.assertEqual(d.dsn_resolved, "&&SORTED")
        self.assertFalse(any(u[0] == "symbolic" for u in self.job.unresolved))

    # --- referbacks --------------------------------------------------------
    def test_referback_inside_the_proc_follows_the_temp(self):
        # Wrong answer guarded: CLMIN "reads *.SORT1.SORTOUT" - no dataset, hole in lineage.
        d = self._dd("STEP010.RPT1", "CLMIN")
        self.assertEqual(d.dsn_resolved, "&&SORTED")
        self.assertTrue(d.is_temp)
        self.assertEqual(d.dsn, "*.SORT1.SORTOUT")
        self.assertNotEqual(d.mode, "output")

    def test_referback_across_the_proc_boundary_reads_the_created_generation(self):
        # Wrong answer guarded: SYSUT1 inherits (+1) and looks like a second writer.
        d = self._dd("STEP020", "SYSUT1")
        self.assertEqual(d.dsn_resolved, "TEST.CLM.REPORT")
        self.assertIsNone(d.gdg_rel)
        self.assertEqual((d.mode, d.mode_source), ("input", "dd_convention"))
        self.assertFalse(d.is_temp)
        self.assertFalse(any(u[0] == "referback" for u in self.job.unresolved))

    def test_bad_referback_is_reported_not_dropped(self):
        j = jcl.parse_jcl("//J JOB A\n//S1 EXEC PGM=X\n//A DD DSN=*.S0.B,DISP=SHR\n")
        self.assertTrue(any(u[0] == "referback" and "S1 A" in u[1] for u in j.unresolved))
        jcl.expand_job(j, lambda n: None)
        self.assertEqual(sum(1 for u in j.unresolved if u[0] == "referback"), 1)   # not duplicated

    def test_same_step_referback(self):
        j = jcl.parse_jcl("//J JOB A\n//S1 EXEC PGM=X\n//A DD DSN=P.Q.R,DISP=SHR\n//B DD DSN=*.A,DISP=SHR\n")
        self.assertEqual(j.steps[0].dds[1].dsn_resolved, "P.Q.R")

    def test_instream_proc_shadows_cataloged_proc(self):
        cataloged = {"SRTPROC": jcl.parse_jcl("//SRTPROC PROC\n//OTHER EXEC PGM=WRONG\n")}
        eff = jcl.expand_job(jcl.parse_jcl(_text("REFERJOB.jcl"), member_lookup=CARDS.get), cataloged.get)
        self.assertNotIn("STEP010.OTHER", [s.step_name for s in eff])
        self.assertIn("STEP010.SORT1", [s.step_name for s in eff])


class BuildLevel(unittest.TestCase):

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

    def test_job_dossier(self):
        out = query.cmd_job(self.conn, "REFERJOB")
        self.assertIn("Instream PROCs defined in this member", out)
        self.assertIn("STEP010.SORT1", out)
        self.assertIn("runs **CLMRPT2**", out)
        self.assertIn("TEST.PARMLIB(SRTCLM)", out)
        self.assertIn("3 card lines loaded from member", out)
        self.assertIn("sort card byte positions: SORT 1-12 CH, INCLUDE 25-26 CH", out)
        self.assertIn("&&SORTED (job-local &&) (via *.SORT1.SORTOUT)", out)
        self.assertIn("TEST.CLM.REPORT (via *.STEP010.RPT1.CLMRPT)", out)
        self.assertIn("### Job-local (&&) datasets", out)
        self.assertNotIn("not found", out.lower())

    def test_temp_never_becomes_a_dataset_row(self):
        self.assertIsNone(self.conn.execute("SELECT 1 FROM dataset WHERE dsn LIKE '&&%'").fetchone())
        out = query.cmd_dataset(self.conn, "SORTED")
        self.assertIn("hidden", out)
        self.assertNotIn("| &&SORTED", out)
        self.assertIn("REFERJOB", query.cmd_dataset(self.conn, "&&SORTED"))

    def test_referback_dataset_has_lineage_across_the_proc(self):
        out = query.cmd_dataset(self.conn, "TEST.CLM.REPORT")
        self.assertIn("STEP010.RPT1", out)                 # created (+1) inside the PROC
        self.assertIn("STEP020", out)                      # read by the plain job step via referback
        self.assertIn("CLMRPT2", out)

    def test_card_member_is_findable_by_name(self):
        out = query.cmd_program(self.conn, "SRTCLM")
        self.assertIn("Not a program", out)
        self.assertIn("Used as control cards by", out)
        self.assertIn("REFERJOB", out)
        self.assertIn("TEST.PARMLIB(SRTCLM)", out)

    def test_program_known_only_through_a_card_member(self):
        out = query.cmd_program(self.conn, "CLMRPT2")
        self.assertIn("Source not indexed", out)
        self.assertIn("REFERJOB", out)
        self.assertIn("STEP010.RPT1", out)


if __name__ == "__main__":
    unittest.main()
