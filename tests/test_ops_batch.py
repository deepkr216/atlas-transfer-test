"""
Classification and operations (coverage audit): CNTL folders are cards not
jobs, listings are not programs, an unrecognised member of a dataset-named
folder is not a document, empty / comment-only members are not programs, a
card deck in the JCL library is re-typed, the toolkit's own outputs are
never re-indexed, a changed parser or manifest re-parses everything, a
changed PROC re-parses every job, the fetch reconciles the folder with the
host's member list (INCOMPLETE libraries, NOT FETCHED members), a hand-kept
manifest is never overwritten, and passwords never reach a log line.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, classify, fetch, query  # noqa: E402


class Classification(unittest.TestCase):

    def test_cntl_listing_unknown_and_jcl_by_content(self):
        self.assertEqual(classify.classify("x/PROD.CLAIMS.CNTL/SRTCLM01.ctl", "  SORT FIELDS=(1,10,CH,A)\n")[0], "ctlcard")
        self.assertEqual(classify.classify("x/PROD.CLAIMS.CNTL/CLMJOB", "//CLMJOB JOB A\n//S1 EXEC PGM=SORT\n")[0], "jcl")
        lst = ("PP 5655-S71 IBM Enterprise COBOL for z/OS  6.3.0   CLMPOST   Date 09/12/2026  Page 1\n"
               "   LineID  PL SL  ----+-*A-1-B--+----2\n   000002         PROGRAM-ID. CLMPOST.\n")
        self.assertEqual(classify.classify("x/PROD.CLAIMS.LISTING/CLMPOST.lst", lst)[0], "listing")
        self.assertEqual(classify.classify("x/PROD.CLAIMS.BIND/CLMPOST.txt", " DSN SYSTEM(DB2P)\n BIND PACKAGE(X)\n")[0], "unknown")
        self.assertEqual(classify.classify("x/DOCS/notes.txt", "meeting notes about premiums\n")[0], "doc")

    def test_redact_cmd(self):
        cmd = ["zowe", "zos-files", "download", "all-members", "X", "--user", "me", "--password", "s3cret",
               "--token-value=abc"]
        s = fetch.redact_cmd(cmd)
        self.assertNotIn("s3cret", s)
        self.assertNotIn("abc", s)
        self.assertIn("--password <redacted>", s)
        self.assertIn("--token-value=<redacted>", s)


class BuildLevel(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        self.files = {
            "PROD.CLAIMS.SRC/CLMPOST.cbl": ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. CLMPOST.\n"
                                            "       PROCEDURE DIVISION.\n           GOBACK.\n"),
            "PROD.CLAIMS.SRC/EMPTYMEM.cbl": "",
            "PROD.CLAIMS.SRC/OLDPGM.cbl": "000100*  RETIRED 2019 - SEE CLMPOST2\n000200*  NOTHING HERE\n",
            "PROD.CLAIMS.SRC/JUSTCOPY.cbl": "           05  WS-X PIC X(5).\n           05  WS-Y PIC X(5).\n",
            "PROD.CLAIMS.SRC/JUSTCARD.cbl": "  SORT FIELDS=(1,10,CH,A)\n  INCLUDE COND=(25,2,CH,EQ,C'AC')\n",
            "PROD.CLAIMS.JCLLIB/SRTCARDS.jcl": "  SORT FIELDS=(1,10,CH,A)\n",
            "PROD.CLAIMS.JCLLIB/CLMNIGHT.jcl": "//CLMNIGHT JOB (A)\n//S1 EXEC NIGHTLY\n",
            "PROD.CLAIMS.PROCLIB/NIGHTLY.prc": "//NIGHTLY PROC\n//PS1 EXEC PGM=CLMPOST\n",
            "out/expanded/CLMPOST.exp.cbl": "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. CLMPOST.\n",
            "out/expanded/.atlas-output": "x\n",
        }
        for rel, text in self.files.items():
            p = os.path.join(self.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        self.db = os.path.join(self.td, "t.db")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _build(self, *extra):
        build._main([self.root, "--db", self.db, "--quiet", *extra])
        return query.connect(self.db)

    def test_kinds_and_no_phantom_rows(self):
        c = self._build("--rebuild")
        kinds = {r[0]: (r[1], r[2]) for r in c.execute("SELECT name, kind, parse_status FROM member")}
        self.assertEqual(kinds["EMPTYMEM"][0], "empty")
        self.assertEqual(kinds["OLDPGM"][0], "empty")
        self.assertEqual(kinds["SRTCARDS"][0], "ctlcard")               # re-typed by index_jcl
        self.assertEqual(kinds["JUSTCOPY"], ("copybook", "ok"))         # level numbers: a copybook, wherever it sits
        self.assertEqual(kinds["JUSTCARD"], ("cobol", "skipped"))       # no PROGRAM-ID / DIVISION: no program row
        self.assertEqual([r[0] for r in c.execute("SELECT program_id FROM program ORDER BY 1")], ["CLMPOST"])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM job").fetchone()[0], 1)
        self.assertNotIn("CLMPOST.exp", "".join(kinds))
        self.assertIn("| empty |", query.cmd_coverage(c))
        c.close()

    def test_proc_change_forces_jobs_and_fingerprint_forces_all(self):
        c = self._build("--rebuild")
        self.assertEqual(c.execute("SELECT effective_pgm FROM step WHERE step_name='S1.PS1'").fetchone()[0], "CLMPOST")
        c.close()
        with open(os.path.join(self.root, "PROD.CLAIMS.PROCLIB", "NIGHTLY.prc"), "w") as fh:
            fh.write("//NIGHTLY PROC\n//PS1 EXEC PGM=CLMPOST2\n")
        c = self._build()
        self.assertEqual(c.execute("SELECT effective_pgm FROM step WHERE step_name='S1.PS1'").fetchone()[0], "CLMPOST2")
        c.close()
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            c = self._build()                              # nothing changed: every member row is kept
        self.assertRegex(buf.getvalue(), r"changed 0 +unchanged [1-9]")
        # a different toolkit fingerprint recorded on the last run => everything re-parsed
        c.execute("UPDATE build_run SET fingerprint='0000000000000000' WHERE id=(SELECT MAX(id) FROM build_run)")
        c.commit()
        c.close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            c = self._build()
        self.assertRegex(buf.getvalue(), r"changed [1-9]\d* +unchanged 0")
        c.close()

    def test_library_marker_incomplete_and_not_fetched(self):
        with open(os.path.join(self.root, "PROD.CLAIMS.SRC", ".atlas-library.json"), "w") as fh:
            json.dump({"dataset": "PROD.CLAIMS.SRC", "fetched_at": "2026-09-10T01:00:00", "rc": 8,
                       "expected": 5, "present": 4, "complete": False, "missing": ["CLMRATE"], "stale": []}, fh)
        c = self._build("--rebuild")
        cov = query.cmd_coverage(c)
        self.assertIn("**INCOMPLETE**", cov)
        self.assertIn("CLMRATE", cov)
        self.assertIn("INCOMPLETE", query.index_header(c))
        self.assertIn("NOT FETCHED", query.cmd_program(c, "CLMRATE"))
        self.assertIn("NOT FOUND", query.cmd_program(c, "NOSUCH"))
        self.assertIn("index built", query.cmd_pack(c, "CLMPOST").splitlines()[0])
        c.close()


class FetchReconcile(unittest.TestCase):

    def test_reconcile_writes_marker_and_moves_stale(self):
        td = tempfile.mkdtemp()
        try:
            cfg = fetch.load_config(os.path.join(td, "nope.json"))
            cfg["local_root"] = td
            src = fetch.new_source("PROD.CLAIMS.SRC", "cobol", system="CLAIMS")
            cfg["sources"] = [src]

            class FakeRunner:
                def run(self, cmd):
                    if "--directory" in cmd:
                        d = cmd[cmd.index("--directory") + 1]
                        os.makedirs(d, exist_ok=True)
                        for m in ("clmpost", "clmupdt", "clmold"):
                            with open(os.path.join(d, m + ".cbl"), "w") as fh:
                                fh.write("       IDENTIFICATION DIVISION.\n")
                        return 8, "", "member CLMRATE: conversion error"
                    if "all-members" in cmd:
                        return 0, json.dumps({"data": {"items": [{"member": "CLMPOST"}, {"member": "CLMUPDT"}, {"member": "CLMRATE"}]}}), ""
                    return 0, "", ""

            logs = []
            from unittest import mock
            with mock.patch.object(fetch, "zowe_exe", return_value="zowe"):
                res = fetch.fetch_source(cfg, src, runner=FakeRunner(), log=logs.append)
            self.assertFalse(res.ok)
            self.assertIn("INCOMPLETE 2/3", res.message)
            dest = fetch.local_path(cfg, src)
            rec = json.load(open(os.path.join(dest, ".atlas-library.json")))
            self.assertEqual((rec["missing"], rec["stale"], rec["complete"]), (["CLMRATE"], ["CLMOLD"], False))
            self.assertTrue(os.path.isfile(os.path.join(dest, ".stale", "clmold.cbl")))
            self.assertFalse(os.path.isfile(os.path.join(dest, "clmold.cbl")))
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_hand_written_manifest_is_kept(self):
        td = tempfile.mkdtemp()
        try:
            cfg = fetch.load_config(os.path.join(td, "nope.json"))
            cfg["local_root"] = td
            cfg["sources"] = [fetch.new_source("PROD.CLAIMS.SRC", "cobol", system="CLAIMS", authoritative=True)]
            man = os.path.join(td, "manifest.json")
            with open(man, "w") as fh:
                json.dump({"authoritative": ["by-hand"]}, fh)
            fetch.write_manifest(cfg, man)
            self.assertEqual(json.load(open(man))["authoritative"], ["by-hand"])
            gen = json.load(open(os.path.join(td, "manifest.generated.json")))
            self.assertEqual(gen["_generated_by"], "atlas.fetch")
            # a generated manifest IS overwritten next time
            fetch.write_manifest(cfg, os.path.join(td, "manifest.generated.json"))
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
