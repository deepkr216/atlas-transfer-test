"""
JCL leftovers from the audit: system PROCs synthesised from their EXEC
overrides, INTRDR submissions as scheduling edges, USS PATH= files, sort
card DD roles (OUTFIL FNAMES / JOINKEYS / ICETOOL), SYMNAMES symbols and
column-positioned items, DB2 utility lineage (LOAD / UNLOAD / DSNTIAUL),
and IDCAMS DEFINE attributes.
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, jcl, query  # noqa: E402


def _dd(step, name):
    return next(d for d in step.dds if d.dd_name.upper() == name.upper())


class ParseLevel(unittest.TestCase):

    def test_system_proc_synthesised(self):
        j = jcl.parse_jcl("//IMSJOB JOB (A)\n//S1 EXEC DLIBATCH,MBR=CLMPOST,PSB=CLMPSB,DBRC=Y\n"
                          "//G.CLMIN DD DSN=PROD.CLM.IN,DISP=SHR\n"
                          "//S2 EXEC IMSBMP,MBR=CLMBMP01,PSB=CLMBMP01\n//S3 EXEC DSNUPROC,SYSTEM=DB2P,UID=CLMLOAD\n")
        eff = {s.step_name: s for s in jcl.expand_job(j, lambda n: None)}
        self.assertEqual(eff["S1.G"].effective_pgm, "CLMPOST")
        self.assertIn("PSB CLMPSB", eff["S1.G"].notes)
        self.assertIn("IMS region type DLI", eff["S1.G"].notes)
        self.assertEqual(_dd(eff["S1.G"], "CLMIN").dsn_resolved, "PROD.CLM.IN")
        self.assertEqual(eff["S2.G"].effective_pgm, "CLMBMP01")
        self.assertIn("IMS region type BMP", eff["S2.G"].notes)
        self.assertEqual(eff["S3.DSNUPROC"].effective_pgm, "*DSNUTILB*")
        self.assertEqual(sum(1 for u in j.unresolved if u[0] == "proc_synthesised"), 3)
        self.assertFalse(any(u[0] == "missing_proc" for u in j.unresolved))

    def test_intrdr_submit_edge(self):
        j = jcl.parse_jcl("//SUBJOB JOB (A)\n//S2 EXEC PGM=IEBGENER\n//SYSUT1 DD DATA,DLM=@@\n//NEXTJOB JOB (A)\n"
                          "//S1 EXEC PGM=X\n@@\n//SYSUT2 DD SYSOUT=(A,INTRDR)\n//SYSIN DD DUMMY\n"
                          "//S3 EXEC PGM=IEBGENER\n//SYSUT1 DD DSN=PROD.JCLLIB(OTHERJOB),DISP=SHR\n"
                          "//SYSUT2 DD SYSOUT=(A,INTRDR)\n")
        self.assertEqual(_dd(j.steps[0], "SYSUT2").mode, "submit")
        self.assertEqual(j.steps[0].submits, ["NEXTJOB"])
        self.assertEqual(j.steps[1].submits, ["OTHERJOB"])       # member name = job name (assumption)

    def test_uss_path_dd(self):
        j = jcl.parse_jcl("//J JOB (A)\n//S3 EXEC PGM=BPXBATCH,PARM='SH /u/scripts/load.sh'\n"
                          "//STDIN DD PATH='/u/feeds/claims.txt',PATHOPTS=(ORDONLY)\n"
                          "//STDOUT DD PATH='/u/logs/load.log',PATHOPTS=(OWRONLY,OCREAT,OTRUNC),PATHMODE=SIRWXU\n")
        s = j.steps[0]
        self.assertEqual((_dd(s, "STDIN").dsn_resolved, _dd(s, "STDIN").mode), ("/u/feeds/claims.txt", "input"))
        self.assertEqual((_dd(s, "STDOUT").dsn_resolved, _dd(s, "STDOUT").mode), ("/u/logs/load.log", "output"))

    def test_sort_cards_roles_symbols_positions(self):
        j = jcl.parse_jcl("//J JOB (A)\n//S1 EXEC PGM=SORT\n//SORTIN DD DSN=PROD.MASTER,DISP=SHR\n"
                          "//ACT DD DSN=PROD.ACTIVE(+1),DISP=(NEW,CATLG)\n//LAP DD DSN=PROD.LAPSED(+1),DISP=(NEW,CATLG)\n"
                          "//SYMNAMES DD *\nPOL-NO,1,10,CH\nPOL-STATUS,45,1,CH\n/*\n"
                          "//SYSIN DD *\n  SORT FIELDS=(POL-NO,A)\n  OUTFIL FNAMES=ACT,INCLUDE=(POL-STATUS,EQ,C'AC'),\n"
                          "    OUTREC=(1:1,10,11:21,5)\n  OUTFIL FNAMES=LAP,INCLUDE=(POL-STATUS,EQ,C'LP')\n/*\n"
                          "//S2 EXEC PGM=ICETOOL\n//IN1 DD DSN=PROD.A,DISP=SHR\n//OUT1 DD DSN=PROD.B,DISP=(NEW,CATLG)\n"
                          "//TOOLIN DD *\n  SORT FROM(IN1) TO(OUT1) USING(CTL1)\n/*\n//CTL1CNTL DD *\n  SORT FIELDS=(1,10,CH,A)\n/*\n"
                          "//S3 EXEC PGM=SORT\n//SORTJNF1 DD DSN=PROD.F1,DISP=SHR\n//SORTJNF2 DD DSN=PROD.F2,DISP=SHR\n"
                          "//SYSIN DD *\n  JOINKEYS F1=SORTJNF1,FIELDS=(1,10,A)\n  JOINKEYS F2=SORTJNF2,FIELDS=(1,10,A)\n/*\n")
        s1, s2, s3 = j.steps
        self.assertEqual((_dd(s1, "ACT").mode, _dd(s1, "ACT").mode_source), ("output", "sort_card"))
        self.assertEqual(_dd(s1, "LAP").mode, "output")
        ctl = "\n".join(d.sysin_text for d in s1.dds if d.sysin_text)
        fields = {(k, p, ln, f) for (k, p, ln, f, _r) in jcl.sort_card_fields(ctl)}
        self.assertIn(("SORT", 1, 10, "CH"), fields)            # POL-NO via SYMNAMES
        self.assertIn(("OUTFIL", 45, 1, "CH"), fields)          # POL-STATUS via SYMNAMES
        self.assertIn(("OUTFIL", 21, 5, None), fields)          # 11:21,5 column-positioned item
        self.assertFalse(any(u[0] == "launcher_parm" for u in j.unresolved))   # ICETOOL TOOLIN counts as cards
        self.assertEqual((_dd(s2, "IN1").mode, _dd(s2, "OUT1").mode), ("input", "output"))
        self.assertEqual((_dd(s3, "SORTJNF1").mode, _dd(s3, "SORTJNF2").mode), ("input", "input"))

    def test_db2_utility_ops_and_idcams_attrs(self):
        ops = jcl.db2util_ops("  LOAD DATA INDDN SYSREC LOG NO RESUME YES\n    INTO TABLE PRODCLM.CLAIM_TBL\n"
                              "    (CLM_NO POSITION(1:10) CHAR)\n  UNLOAD TABLESPACE DBCLM.TSCLM FROM TABLE PRODCLM.POLICY_TBL\n"
                              "    UNLDDN SYSREC01\n")
        self.assertIn(("LOAD", "PRODCLM.CLAIM_TBL", "SYSREC", "write"), ops)
        self.assertIn(("UNLOAD", "PRODCLM.POLICY_TBL", "SYSREC01", "read"), ops)
        sql = jcl.db2util_ops("  SELECT * FROM PROD.CLM_MASTER WHERE STATUS = 'A';\n", is_sql=True)
        self.assertEqual(sql, [("SELECT", "PROD.CLM_MASTER", None, "read")])
        attrs = jcl.idcams_attrs("  DEFINE CLUSTER (NAME(PROD.POLICY.MASTER.KSDS) INDEXED -\n"
                                 "          RECORDSIZE(200 200) KEYS(10 0) SHAREOPTIONS(2 3)) -\n"
                                 "         DATA (NAME(PROD.POLICY.MASTER.KSDS.DATA))\n"
                                 "  DEFINE AIX (NAME(PROD.POLICY.MASTER.AIX) -\n"
                                 "          RELATE(PROD.POLICY.MASTER.KSDS) KEYS(5 45))\n"
                                 "  DEFINE GDG (NAME(PROD.CLM.EXTRACT) LIMIT(7) SCRATCH)\n")
        self.assertEqual(attrs["PROD.POLICY.MASTER.KSDS"], {"vsam_type": "KSDS", "recordsize_max": "200", "key_len": "10", "key_off": "0"})
        self.assertEqual(attrs["PROD.POLICY.MASTER.AIX"]["relates_to"], "PROD.POLICY.MASTER.KSDS")
        self.assertEqual(attrs["PROD.CLM.EXTRACT"]["gdg_limit"], "7")


class BuildLevel(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        files = {
            "JCL/CLMLOAD.jcl": ("//CLMLOAD JOB (A)\n//S1 EXEC PGM=IKJEFT01\n//SYSTSIN DD *\n  DSN SYSTEM(DB2P)\n"
                                "  RUN PROGRAM(DSNTIAUL) PLAN(DSNTIB12) PARMS('SQL')\n/*\n//SYSIN DD *\n"
                                "  SELECT * FROM PROD.CLM_MASTER WHERE STATUS = 'A';\n/*\n"
                                "//SYSREC00 DD DSN=PROD.CLM.UNLOAD,DISP=(NEW,CATLG)\n"
                                "//S2 EXEC PGM=DSNUTILB,PARM='DB2P,LOADCLM'\n//SYSREC DD DSN=PROD.CLM.UNLOAD,DISP=SHR\n"
                                "//SYSIN DD *\n  LOAD DATA INDDN SYSREC LOG NO REPLACE\n    INTO TABLE PRODCLM.CLM_MASTER_HIST\n/*\n"
                                "//S3 EXEC PGM=IDCAMS\n//SYSPRINT DD SYSOUT=*\n//SYSIN DD *\n"
                                "  DEFINE CLUSTER (NAME(PROD.POLICY.MASTER.KSDS) INDEXED -\n"
                                "          RECORDSIZE(200 200) KEYS(10 0))\n/*\n"
                                "//S4 EXEC PGM=IEBGENER\n//SYSUT1 DD DSN=PROD.JCLLIB(NEXTJOB),DISP=SHR\n"
                                "//SYSUT2 DD SYSOUT=(A,INTRDR)\n//SYSIN DD DUMMY\n"),
            "JCL/IMSJOB.jcl": "//IMSJOB JOB (A)\n//S1 EXEC DLIBATCH,MBR=CLMPOST,PSB=CLMPSB,DBRC=Y\n",
            "SRC/CLMPOST.cbl": "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. CLMPOST.\n       PROCEDURE DIVISION.\n           GOBACK.\n",
        }
        for rel, text in files.items():
            p = os.path.join(cls.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_db2_utility_lineage(self):
        rows = [tuple(r) for r in self.conn.execute("SELECT op, tbl, via_dd, direction FROM step_table ORDER BY id")]
        self.assertIn(("SELECT", "PROD.CLM_MASTER", None, "read"), rows)
        self.assertIn(("LOAD", "PRODCLM.CLM_MASTER_HIST", "SYSREC", "write"), rows)
        out = query.cmd_table(self.conn, "CLM_MASTER_HIST")
        self.assertIn("Batch utilities", out)
        self.assertIn("| CLMLOAD | S2 | LOAD | write | SYSREC | PROD.CLM.UNLOAD |", out)
        ds = query.cmd_dataset(self.conn, "PROD.CLM.UNLOAD")
        self.assertIn("output [db2util_card]", ds)                 # written by DSNTIAUL
        self.assertIn("input [db2util_card]", ds)                  # read by the LOAD

    def test_idcams_attributes(self):
        row = self.conn.execute("SELECT vsam_type, recordsize_max, key_len, key_off FROM dataset WHERE dsn='PROD.POLICY.MASTER.KSDS'").fetchone()
        self.assertEqual(tuple(row), ("KSDS", 200, 10, 0))
        self.assertIn("RECORDSIZE max 200", query.cmd_dataset(self.conn, "PROD.POLICY.MASTER.KSDS"))

    def test_intrdr_and_system_proc_in_the_index(self):
        dep = self.conn.execute("SELECT job_name, depends_on, kind FROM sched_dep").fetchall()
        self.assertIn(("NEXTJOB", "CLMLOAD", "intrdr"), [tuple(d) for d in dep])
        step = self.conn.execute("SELECT effective_pgm, from_proc FROM step WHERE step_name='S1.G'").fetchone()
        self.assertEqual(tuple(step), ("CLMPOST", "DLIBATCH"))
        self.assertNotRegex(query.cmd_dead(self.conn), r"\| CLMPOST \|")
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM unresolved WHERE kind='proc_synthesised'").fetchone())


if __name__ == "__main__":
    unittest.main()
