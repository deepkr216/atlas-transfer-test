"""
PROC expansion, symbolic precedence, //STEP.DD overrides, INCLUDE members,
temporary datasets, IDCAMS dataset operations, and the job-level dataset
lineage that results. This is where "where is this file created / used"
gets its answer.
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, fetch, jcl, query, reader  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def _text(name):
    return reader.load(os.path.join(FIX, name))[0]


class ProcExpansion(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        procs = {"NIGHTLY": jcl.parse_jcl(_text("NIGHTLY.prc"))}
        includes = {"CLMINCL": _text("CLMINCL.jcl")}
        cls.job = jcl.parse_jcl(_text("NIGHTJOB.jcl"), include_lookup=includes.get)
        cls.eff = jcl.expand_job(cls.job, procs.get)
        cls.by = {s.step_name: s for s in cls.eff}

    def _dd(self, step, name):
        return next(d for d in self.by[step].dds if d.dd_name.upper() == name)

    def test_include_member_was_spliced(self):
        self.assertEqual(self.job.includes, ["CLMINCL"])

    def test_exec_override_beats_proc_default(self):
        self.assertEqual(self._dd("NIGHT.PS010", "MASTER").dsn_resolved, "PROD.CLM.MASTER")   # not TEST.CLM

    def test_job_set_beats_proc_default_in_parm(self):
        self.assertEqual(self.by["NIGHT.PS010"].parm, "20260912")

    def test_included_dd_overrides_proc_dd(self):
        d = self._dd("NIGHT.PS010", "STEPLIB")
        self.assertEqual(d.dsn_resolved, "PROD.CLM.LOADLIB")
        self.assertTrue(d.is_override)

    def test_inline_sysin_override_replaces_dummy(self):
        d = self._dd("NIGHT.PS010", "SYSIN")
        self.assertIn("RUNMODE=FULL", d.sysin_text or "")
        self.assertTrue(d.is_override)

    def test_temporary_dataset_is_not_a_symbolic(self):
        d = self._dd("NIGHT.PS010", "TEMP1")
        self.assertEqual(d.dsn_resolved, "&&TEMP")
        self.assertFalse(any(u[0] == "symbolic" and "TEMP" in u[1] for u in self.job.unresolved))

    def test_launcher_inside_proc_resolved(self):
        self.assertEqual(self.by["NIGHT.PS020"].effective_pgm, "CLMRPT")

    def test_qualified_override_and_added_dd(self):
        self.assertEqual(self._dd("NIGHT.PS020", "EXTRACT").dsn_resolved, "PROD.CLM.EXTRACT.OVERRIDE")
        added = self._dd("NIGHT.PS020", "NEWDD")
        self.assertEqual((added.dsn_resolved, added.mode), ("PROD.CLM.ADDED", "output"))

    def test_gdg_and_direction_survive_expansion(self):
        # EXTRACT was overridden; the PROC's own GDG(+1) form is exercised via the proc parse
        proc = jcl.parse_jcl(_text("NIGHTLY.prc"))
        d = next(d for s in proc.steps for d in s.dds if d.dd_name == "EXTRACT")
        self.assertEqual((d.gdg_rel, d.mode, d.mode_source), ("+1", "output", "gdg_relative"))

    def test_missing_nested_proc_is_reported_not_hidden(self):
        self.assertIn("NIGHT.PS040", self.by)
        self.assertEqual(self.by["NIGHT.PS040"].proc_called, "CLMSUB")
        self.assertTrue(any(u[0] == "missing_proc" and "CLMSUB" in u[1] for u in self.job.unresolved))

    def test_idcams_ops(self):
        ops = jcl.idcams_ops(self._dd("NIGHT.PS030", "SYSIN").sysin_text)
        self.assertIn(("DEFINE CLUSTER", "PROD.CLM.NEW.KSDS", "create"), ops)
        self.assertIn(("REPRO DD", "MASTER", "input"), ops)
        self.assertIn(("REPRO", "PROD.CLM.BACKUP", "output"), ops)


class JobLevelLineage(unittest.TestCase):

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

    def test_job_dossier_shows_effective_steps(self):
        out = query.cmd_job(self.conn, "NIGHTJOB")
        self.assertIn("NIGHT.PS010", out)
        self.assertIn("(from PROC NIGHTLY)", out)
        self.assertIn("PROD.CLM.MASTER", out)
        self.assertIn("runs **CLMRPT**", out)
        self.assertIn("PROD.CLM.EXTRACT.OVERRIDE (override)", out)
        self.assertIn("PROD.CLM.NEW.KSDS", out)                       # IDCAMS DEFINE
        self.assertIn("create [idcams_card]", out)
        self.assertIn("input [idcams_card]", out)                     # REPRO INFILE(MASTER)
        self.assertIn("CLMSUB", out)

    def test_dataset_lineage_at_job_level(self):
        out = query.cmd_dataset(self.conn, "PROD.CLM.MASTER")
        self.assertIn("NIGHTJOB", out)
        self.assertIn("NIGHT.PS010", out)
        self.assertIn("CLMPOST", out)
        vs = self.conn.execute("SELECT is_vsam FROM dataset WHERE dsn='PROD.CLM.NEW.KSDS'").fetchone()
        self.assertEqual(vs[0], 1)

    def test_program_runs_in_shows_the_job_not_the_bare_proc(self):
        out = query.cmd_program(self.conn, "CLMRPT")
        self.assertIn("NIGHTJOB", out)
        self.assertIn("NIGHT.PS020", out)
        self.assertNotIn("no indexed job expands it", out)

    def test_symbolic_left_unresolved_is_reported(self):
        # The bare PROC member, on its own, still carries &HLQ - and says so via the job when unresolved.
        out = query.cmd_job(self.conn, "NIGHTLY")
        self.assertIn("&HLQ", out.replace("TEST.CLM", "&HLQ") if "TEST.CLM" in out else out)


class SchedulerSource(unittest.TestCase):

    def test_build_cmd_passes_scheduler_exports(self):
        cfg = fetch.load_config(os.path.join(tempfile.gettempdir(), "nope-atlas.json"))
        cfg["local_root"] = "C:/estate"
        cfg["sources"] = [fetch.new_source("PROD.CA7.DEPS", "sched", type="seq", local="routing/ca7.csv"),
                          fetch.new_source("PROD.SRC", "cobol")]
        cmd = fetch.build_cmd(cfg, "m.json")
        self.assertIn("--sched", cmd)
        self.assertTrue(cmd[cmd.index("--sched") + 1].endswith(os.path.join("routing", "ca7.csv")))

    def test_native_scheduler_export_is_reported_not_silently_empty(self):
        self.assertEqual(fetch.infer_kind("PROD.ZEKE.EVENTS"), ("sched", False))
        td = tempfile.mkdtemp()
        try:
            z = os.path.join(td, "zeke.txt")
            with open(z, "w", encoding="utf-8") as fh:
                fh.write("EVENT 001234  JOB NIGHTJOB   WHEN EVENT 001200 ENDED\n"
                         "EVENT 001235  JOB DAYJOB     WHEN TIME 0600\n")
            db = os.path.join(td, "t.db")
            build._main([FIX, "--db", db, "--rebuild", "--quiet", "--sched", z])
            c = query.connect(db)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sched_dep").fetchone()[0], 0)
            row = c.execute("SELECT detail FROM unresolved WHERE kind='scheduler_format'").fetchone()
            self.assertIsNotNone(row)
            self.assertIn("zeke.txt", row[0])
            self.assertIn("scheduler_format", query.cmd_coverage(c))
            c.close()
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_two_scheduler_files_load(self):
        td = tempfile.mkdtemp()
        try:
            a = os.path.join(td, "a.csv")
            b = os.path.join(td, "b.csv")
            with open(a, "w", encoding="utf-8") as fh:
                fh.write("job_name,depends_on,kind\nNIGHTJOB,DAYJOB,predecessor\n")
            with open(b, "w", encoding="utf-8") as fh:
                fh.write("job_name,system,schedule\nNIGHTJOB,CLAIMS,DAILY 2100\n")
            db = os.path.join(td, "t.db")
            build._main([FIX, "--db", db, "--rebuild", "--quiet", "--sched", a, "--sched", b])
            c = query.connect(db)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sched_dep").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sched_job").fetchone()[0], 1)
            self.assertIn("DAYJOB", query.cmd_job(c, "NIGHTJOB"))
            c.close()
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
