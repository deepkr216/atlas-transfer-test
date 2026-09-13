"""
Last audit items: the `interfaces` report (FTP/NDM steps, MQ, TD queues,
URIMAP, manifest-declared peers), an SQL `--` comment ending in a period
inside EXEC SQL, a DCLGEN copybook that is NOT "procedure code", INCLUDE
members answering "included by", control cards in a SEQUENTIAL dataset,
and diag redaction of two-qualifier dataset names / job names.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, cobol, diag, fetch, jcl, query  # noqa: E402


class ParseLevel(unittest.TestCase):

    def test_sql_comment_with_period_does_not_end_the_statement(self):
        f = cobol.parse_program("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. SQLC.\n"
                                "       PROCEDURE DIVISION.\n"
                                "           EXEC SQL\n             -- get the policy.\n"
                                "             SELECT STATUS_CD INTO :WS-ST FROM POLICY_TBL\n"
                                "             WHERE POL_NO = :WS-KEY\n           END-EXEC.\n"
                                "           MOVE 'X' TO WS-A.\n           GOBACK.\n")
        self.assertEqual([s.stmt_type for s in f.sql], ["SELECT"])
        self.assertIn("POLICY_TBL", f.sql[0].tables)
        self.assertIn(("WS-ST", "write"), [(n, m) for (n, m, _s, _l) in f.field_refs])

    def test_sequential_card_dataset_matched_by_last_qualifier(self):
        cards = {"SRTCLM": "  SORT FIELDS=(1,12,CH,A)\n"}
        j = jcl.parse_jcl("//J JOB (A)\n//S1 EXEC PGM=SORT\n//SYSIN DD DSN=PROD.CLAIMS.SRTCLM,DISP=SHR\n",
                          member_lookup=cards.get)
        d = j.steps[0].dds[0]
        self.assertEqual((d.card_member, d.dsn_resolved), ("SRTCLM", "PROD.CLAIMS.SRTCLM"))
        self.assertIn("SORT FIELDS", d.sysin_text)
        self.assertTrue(any(u[0] == "card_seq_assumed" for u in j.unresolved))
        self.assertEqual(j.steps[0].effective_pgm, "*SORT*")

    def test_diag_redacts_two_qualifier_dsns(self):
        self.assertEqual(diag.redact("failed on PROD.MASTER and PROD.CLM.EXTRACT(+1)"), "failed on <dsn> and <dsn>")


class BuildLevel(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        files = {
            "COPYLIB/DCLPOL.cpy": ("           EXEC SQL DECLARE PRD.POLICY_TBL TABLE\n"
                                   "           ( POL_NO CHAR(12) NOT NULL, STATUS_CD CHAR(2) ) END-EXEC.\n"
                                   "       01  DCLPOLICY.\n           10 POL-NO PIC X(12).\n           10 STATUS-CD PIC X(02).\n"),
            "JCL/CLMINC.jcl": "//SORTIN DD DSN=PROD.CLM.EXTRACT(0),DISP=SHR\n",
            "JCL/CLMJOB.jcl": ("//CLMJOB JOB (A)\n//S1 EXEC PGM=SORT\n// INCLUDE MEMBER=CLMINC\n"
                               "//SORTOUT DD DSN=PROD.CLM.SORTED,DISP=(NEW,CATLG)\n//SYSIN DD *\n  SORT FIELDS=COPY\n/*\n"
                               "//S2 EXEC PGM=FTP,PARM='ftp.vendor.com (EXIT'\n//INPUT DD *\nuserid\nsecretpw\n"
                               "put 'PROD.CLM.SORTED' claims.txt\nquit\n/*\n"),
            "SRC/MQPGM.cbl": ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. MQPGM.\n"
                              "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
                              "       01  WS-MQOD.\n           05  MQOD-OBJECTNAME PIC X(48) VALUE 'CLAIMS.OUT.QUEUE'.\n"
                              "       01  HCONN PIC S9(9) COMP.\n       01  HOBJ PIC S9(9) COMP.\n       01  OPTS PIC S9(9) COMP.\n"
                              "       01  CC PIC S9(9) COMP.\n       01  RC PIC S9(9) COMP.\n       01  MQMD PIC X(324).\n"
                              "       01  PMO PIC X(128).\n       01  BUFLEN PIC S9(9) COMP.\n       01  WS-MSG PIC X(100).\n"
                              "       PROCEDURE DIVISION.\n"
                              "           CALL 'MQOPEN' USING HCONN WS-MQOD OPTS HOBJ CC RC.\n"
                              "           CALL 'MQPUT' USING HCONN HOBJ MQMD PMO BUFLEN WS-MSG CC RC.\n           GOBACK.\n"),
        }
        for rel, text in files.items():
            p = os.path.join(cls.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        man = os.path.join(cls.td, "manifest.json")
        with open(man, "w") as fh:
            json.dump({"authoritative": [], "external_interfaces": [
                {"kind": "ndm", "peer": "REINSURER-X", "direction": "out", "dataset": "PROD.CLM.SORTED"},
                {"kind": "ddf", "peer": "CLAIMS-WEB", "direction": "in", "table": "PRD.POLICY_TBL"}]}, fh)
        cls.db = os.path.join(cls.td, "t.db")
        build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet", "--manifest", man])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_dclgen_is_not_procedure_code_and_declares_columns(self):
        self.assertIsNone(self.conn.execute("SELECT 1 FROM unresolved WHERE kind='procedure_copybook'").fetchone())
        cols = [r[0] for r in self.conn.execute("""SELECT c.name FROM db2_column c JOIN db2_object o ON o.id=c.object_id
                                                    WHERE o.name='POLICY_TBL' ORDER BY c.ordinal""")]
        self.assertEqual(cols, ["POL_NO", "STATUS_CD"])
        self.assertIn("POL_NO", query.cmd_table(self.conn, "POLICY_TBL"))

    def test_include_member_says_who_includes_it(self):
        out = query.cmd_job(self.conn, "CLMINC")
        self.assertIn("INCLUDE member", out)
        self.assertIn("CLMJOB", out)
        self.assertIn("INCLUDE members spliced in: CLMINC", query.cmd_job(self.conn, "CLMJOB"))

    def test_interfaces_report_and_dataset_boundary_line(self):
        out = query.cmd_interfaces(self.conn)
        self.assertIn("| ftp | out |", out)
        self.assertIn("PROD.CLM.SORTED", out)
        self.assertIn("| mq | out |", out)
        self.assertIn("CLAIMS.OUT.QUEUE", out)
        self.assertIn("REINSURER-X", out)
        self.assertIn("CLAIMS-WEB", out)
        self.assertNotIn("secretpw", out)
        ds = query.cmd_dataset(self.conn, "PROD.CLM.SORTED")
        self.assertIn("Crosses the mainframe boundary", ds)
        self.assertIn("REINSURER-X", ds)
        only = query.cmd_interfaces(self.conn, dsn="PROD.CLM.SORTED")
        self.assertNotIn("CLAIMS.OUT.QUEUE", only)


if __name__ == "__main__":
    unittest.main()
