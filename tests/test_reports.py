"""
Reports and the model hand-off (coverage audit, P0 "report completeness"):

  the citation gate accepts every form the reports print (MEMBER:line,
  (via COPY X), NAME(kind), SYSTEM/NAME, NAME@LIBRARY), resolves a name
  shared by a program, its PSB and its job by kind, refuses to certify one
  department's copy against the other's, warns on weak tokens / wide ranges
  / comment-only lines and fails when the file changed since the index;
  every report row that derives from a line carries a cite; `field` never
  silently drops a program and answers an 88 name; `job PROC` says who
  executes it; `literal` shows the message text beside a code; packs have a
  budget, a token estimate, no paths, and exist for jobs and copybooks;
  `crud`, `conditions`, `paragraph`; `dead` and `coverage` say more.
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, fetch, query, verify_citations  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


class GateAndCite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        for rel, src in (("SRC/SAMPPGM.cbl", "SAMPPGM.cbl"), ("JCLLIB/SAMPJOB.jcl", "SAMPJOB.jcl"),
                         ("COPYLIB/PMASTREC.cpy", "PMASTREC.cpy")):
            p = os.path.join(cls.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            shutil.copy(os.path.join(FIX, src), p)
        # the IMS convention: a PSB and a job named after the program
        with open(os.path.join(cls.root, "JCLLIB", "SAMPPGM.jcl"), "w") as fh:
            fh.write("//SAMPPGM JOB (A)\n//* RUNS SAMPPGM\n//S1 EXEC PGM=SAMPPGM\n")
        os.makedirs(os.path.join(cls.root, "PSBSRC"))
        with open(os.path.join(cls.root, "PSBSRC", "SAMPPGM.psb"), "w") as fh:
            fh.write("         PCB   TYPE=DB,DBDNAME=POLDBD,PROCOPT=G,KEYLEN=12\n"
                     "         SENSEG NAME=POLICY,PARENT=0\n         PSBGEN LANG=COBOL,PSBNAME=SAMPPGM\n")
        # the same copybook in two departments, both authoritative
        for sysname, pic in (("CLAIMS", "X(5)"), ("POLICY", "X(9)")):
            p = os.path.join(cls.root, sysname, f"PROD.{sysname}.COPYLIB", "DUPREC.cpy")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(f"           05  DUP-FIELD PIC {pic}.\n")
        cfg = fetch.load_config(os.path.join(cls.td, "nope.json"))
        cfg["local_root"] = cls.root
        cfg["sources"] = [fetch.new_source("PROD.CLAIMS.COPYLIB", "copybook", system="CLAIMS", authoritative=True),
                          fetch.new_source("PROD.POLICY.COPYLIB", "copybook", system="POLICY", authoritative=True)]
        man = os.path.join(cls.td, "manifest.json")
        fetch.write_manifest(cfg, man)
        cls.db = os.path.join(cls.td, "t.db")
        build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet", "--manifest", man])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def _check(self, text):
        results, _unc = verify_citations.check_answer(text, db_path=self.db)
        return [(r.status, r.detail) for r in results]

    def test_forms_and_kind_resolution(self):
        r = self._check('SAMPPGM calls VALIDATE [[SAMPPGM 27 "CALL \'VALIDATE\'"]].\n'
                        'Same, colon form [[SAMPPGM:27 "CALL \'VALIDATE\'"]].\n'
                        'Via copy [[PMASTREC 11 (via COPY PMASTREC) "COMP-3"]].\n'
                        'Explicit kind [[SAMPPGM(jcl) 3 "PGM=SAMPPGM"]].\n')
        self.assertEqual([s for s, _d in r], ["PASS", "PASS", "PASS", "PASS"], r)
        self.assertIn("cobol chosen", r[0][1])

    def test_ambiguous_departments_and_qualifiers(self):
        r = self._check('[[DUPREC 1 "PIC X(9)"]]\n[[POLICY/DUPREC 1 "PIC X(9)"]]\n[[DUPREC@PROD.POLICY.COPYLIB 1 "PIC X(9)"]]\n'
                        '[[CLAIMS/DUPREC 1 "PIC X(9)"]]\n')
        self.assertEqual(r[0][0], "FAIL")
        self.assertIn("AMBIGUOUS", r[0][1])
        self.assertEqual((r[1][0], r[2][0]), ("PASS", "PASS"))
        self.assertEqual(r[3][0], "FAIL")                       # the CLAIMS copy does not say X(9)

    def test_warnings_weak_wide_comment(self):
        r = self._check('[[SAMPJOB 1-34 "DD"]]\n[[SAMPPGM(jcl) 2 "RUNS SAMPPGM"]]\n')
        self.assertEqual(r[0][0], "WARN")
        self.assertIn("weak token", r[0][1])
        self.assertIn("wide range", r[0][1])
        self.assertEqual(r[1][0], "WARN")
        self.assertIn("COMMENT", r[1][1])

    def test_changed_file_fails(self):
        p = os.path.join(self.root, "JCLLIB", "SAMPPGM.jcl")
        with open(p, "a") as fh:
            fh.write("//* CHANGED AFTER THE BUILD\n")
        try:
            r = self._check('[[SAMPPGM(jcl) 3 "PGM=SAMPPGM"]]\n')
            self.assertEqual(r[0][0], "FAIL")
            self.assertIn("changed since", r[0][1])
        finally:
            with open(p, "w") as fh:
                fh.write("//SAMPPGM JOB (A)\n//* RUNS SAMPPGM\n//S1 EXEC PGM=SAMPPGM\n")

    def test_cite_prefers_the_program_and_names_the_others(self):
        out = query.cmd_cite(self.conn, "SAMPPGM", "27", None)
        self.assertIn("SAMPPGM(cobol)", out)
        self.assertIn("CALL 'VALIDATE'", out)
        self.assertIn("jcl", out.splitlines()[1])
        self.assertIn("PGM=SAMPPGM", query.cmd_cite(self.conn, "SAMPPGM", "3", None, kind="jcl"))

    def test_ambiguous_groups_by_kind(self):
        out = query.cmd_ambiguous(self.conn)
        self.assertNotRegex(out, r"\| SAMPPGM \| \w+ \| 3")
        self.assertIn("Same name, different kinds", out)
        self.assertIn("| DUPREC | copybook | 2 | 2 |", out)


class ReportRows(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        shutil.copytree(FIX, cls.root)
        with open(os.path.join(cls.root, "LAPSED.cbl"), "w") as fh:
            fh.write("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. LAPSED.\n"
                     "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n       01  PM-REC.\n           COPY PMASTREC.\n"
                     "       PROCEDURE DIVISION.\n       0000-MAIN.\n           IF PM-LAPSED\n"
                     "              SET PM-ACTIVE TO TRUE\n           END-IF.\n           GOBACK.\n")
        with open(os.path.join(cls.root, "MSGTBL.cbl"), "w") as fh:
            fh.write("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. MSGTBL.\n"
                     "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n       01  WS-MSG-TBL.\n"
                     "           05 FILLER PIC X(4)  VALUE 'E101'.\n"
                     "           05 FILLER PIC X(30) VALUE 'GENDER MUST BE MALE FOR SON'.\n"
                     "           05 FILLER PIC X(4)  VALUE 'E102'.\n"
                     "           05 FILLER PIC X(30) VALUE 'GENDER MUST BE FEMALE'.\n"
                     "       PROCEDURE DIVISION.\n           GOBACK.\n")
        with open(os.path.join(cls.root, "CALLER2.cbl"), "w") as fh:
            fh.write("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. CALLER2.\n"
                     "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n       01  A PIC X.\n       01  B PIC X.\n"
                     "       PROCEDURE DIVISION.\n           CALL 'RATECALC' USING A B.\n           GOBACK.\n")
        with open(os.path.join(cls.root, "RATECALC.cbl"), "w") as fh:
            fh.write("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. RATECALC.\n"
                     "       DATA DIVISION.\n       LINKAGE SECTION.\n       01  L1 PIC X.\n       01  L2 PIC X.\n"
                     "       01  L3 PIC X.\n       01  L4 PIC X.\n"
                     "       PROCEDURE DIVISION USING L1 L2 L3 L4.\n           GOBACK.\n")
        cls.db = os.path.join(cls.td, "t.db")
        build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_dataset_and_copybook_rows_carry_cites(self):
        out = query.cmd_dataset(self.conn, "PROD.CLM.MASTER")
        self.assertRegex(out, r"NIGHTLY:\d+")
        cb = query.cmd_copybook(self.conn, "PMASTREC")
        self.assertRegex(cb, r"\| SAMPPGM \| SAMPPGM \|[^|]*\| SAMPPGM:\d+ \|")
        self.assertIn("| cite |", cb)

    def test_callers_cites_and_argument_check(self):
        out = query.cmd_graph(self.conn, "RATECALC", "callers", 1, args=True)
        self.assertRegex(out, r"SAMPPGM:\d+")
        self.assertIn("passes 2, callee expects 4", out)
        self.assertIn("callee LINKAGE: 4", out)

    def test_field_groups_programs_and_answers_an_88_name(self):
        out = query.cmd_field(self.conn, "WS-GENDER-CD")
        self.assertIn("one cite per program", out)
        self.assertRegex(out, r"\*\*test\*\* \(\d+ in \d+ programs\)")
        self.assertRegex(out, r"MEMBRVAL IF x\d+ @MEMBRVAL:\d+")
        allout = query.cmd_field(self.conn, "WS-GENDER-CD", show_all=True)
        self.assertIn("every site", allout)
        self.assertNotIn(" x", allout.split("References by mode")[1].split("###")[0])
        f88 = query.cmd_field(self.conn, "PM-LAPSED")
        self.assertIn("88-level condition name", f88)
        self.assertIn("PM-POLICY-STATUS", f88)
        self.assertRegex(f88, r"\| 12 \| 2 \|")
        self.assertIn("LAPSED", f88)                                 # the SET / IF references to the 88 name

    def test_job_proc_says_who_executes_it(self):
        out = query.cmd_job(self.conn, "NIGHTLY")
        self.assertRegex(out, r"Executed by \(\d+\): NIGHTJOB NIGHT @NIGHTJOB:\d+")

    def test_literal_shows_the_message_text_beside_a_code(self):
        out = query.cmd_literal(self.conn, "E101")
        self.assertIn("message text beside it", out)
        self.assertIn("next piece: GENDER MUST BE MALE FOR SON", out)

    def test_packs_budget_header_paths_kinds(self):
        p = query.cmd_pack(self.conn, "SAMPPGM", budget=1500)
        self.assertLessEqual(len(p.split("-->\n", 1)[1]), 1500)
        self.assertIn("~", p.splitlines()[0])
        self.assertIn("tokens", p.splitlines()[0])
        self.assertNotIn("C:\\", p)
        full = query.cmd_pack(self.conn, "ERRPGM")
        self.assertIn("COND  ", full)
        job = query.cmd_pack(self.conn, "NIGHTJOB")
        self.assertIn("<!-- pack job NIGHTJOB", job)
        self.assertIn("## Control cards (full text", job)
        self.assertIn("RUN PROGRAM(CLMRPT)", job)
        cb = query.cmd_pack(self.conn, "PMASTREC")
        self.assertIn("<!-- pack copybook", cb)
        self.assertIn("record length:", cb)

    def test_crud_conditions_paragraph(self):
        crud = query.cmd_crud(self.conn, ["SAMPPGM", "SQLCOLS"])
        self.assertRegex(crud, r"\| SAMPPGM \| file \| [^|]+ \| R")
        self.assertRegex(crud, r"\| SQLCOLS \| db2 \| [^|]+ \| U")
        cond = query.cmd_conditions(self.conn, "MEMBRVAL")
        self.assertIn("WHEN", cond)
        self.assertIn("Negative cases", cond)
        self.assertIn("WS-GENDER-CD", cond)
        para = query.cmd_paragraph(self.conn, "MULTILN", "1000-PROCESS")
        self.assertIn("Reached by", para)
        self.assertIn("0000-MAIN", para)
        self.assertIn("### Source", para)
        by_line = query.cmd_paragraph(self.conn, "MULTILN", "12")
        self.assertIn("# MULTILN", by_line)

    def test_dead_and_coverage_extras(self):
        dead = query.cmd_dead(self.conn)
        self.assertIn("written but never read", dead)
        self.assertIn("PROD.CLM.BACKUP", dead)
        self.assertIn("Copybooks no indexed program COPYs", dead)
        cov = query.cmd_coverage(self.conn)
        self.assertIn("What contributes facts", cov)
        self.assertRegex(cov, r"\| ctlcard \| \d+ \| NO \|")
        self.assertIn("Optional inputs", cov)
        self.assertIn("| scheduler export | NO |", cov)


if __name__ == "__main__":
    unittest.main()
