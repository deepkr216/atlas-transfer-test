"""
Several departments, each with its own libraries: department folders,
kind inference from dataset names, load libraries kept out, per-department
copybook resolution in declared SYSLIB order, and cross-department dataset
flow.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, fetch, query  # noqa: E402


class SourceHelpers(unittest.TestCase):

    def test_kind_inferred_from_dataset_name(self):
        cases = {
            "PROD.CLAIMS.SRC": ("cobol", False), "PROD.CLAIMS.COPYLIB": ("copybook", False),
            "PROD.CLAIMS.JCLLIB": ("jcl", False), "PROD.CLAIMS.PROCLIB": ("proc", False),
            "PROD.CLAIMS.PARMLIB": ("ctlcard", False), "PROD.IMS.PSBSOURCE": ("psb", False),
            "PROD.IMS.DBDSRC": ("dbd", False), "PROD.CICS.BMS": ("bms", False),
            "PROD.IMS.STAGE1": ("imsgen", False), "PROD.CA7.SCHED": ("sched", False),
            "PROD.IMS.PSBLIB": ("other", True), "PROD.CLAIMS.LOADLIB": ("other", True),
            "PROD.CLAIMS.WHATEVER": ("other", False),
        }
        for ds, expect in cases.items():
            self.assertEqual(fetch.infer_kind(ds), expect, ds)

    def test_department_folder_and_bulk_add(self):
        cfg = fetch.load_config(os.path.join(tempfile.gettempdir(), "nope-atlas.json"))
        cfg["local_root"] = "C:/estate"
        s = fetch.new_source("PROD.CLAIMS.SRC", "cobol", system="claims")
        self.assertEqual((s["system"], s["local"]), ("CLAIMS", "CLAIMS/PROD.CLAIMS.SRC"))
        added, warnings = fetch.bulk_add(cfg, "CLAIMS", "\n".join([
            "PROD.CLAIMS.SRC", "PROD.CLAIMS.COPYLIB", "PROD.COMMON.COPYLIB", "PROD.CLAIMS.JCLLIB",
            "PROD.IMS.PSBLIB", "* a comment", "PROD.CLAIMS.SRC", "PROD.CLAIMS.ODDNAME"]), authoritative=True)
        self.assertEqual([a["dataset"] for a in added],
                         ["PROD.CLAIMS.SRC", "PROD.CLAIMS.COPYLIB", "PROD.COMMON.COPYLIB", "PROD.CLAIMS.JCLLIB",
                          "PROD.IMS.PSBLIB", "PROD.CLAIMS.ODDNAME"])
        psblib = next(a for a in added if a["dataset"] == "PROD.IMS.PSBLIB")
        self.assertFalse(psblib["enabled"])
        self.assertTrue(any("LOAD library" in w for w in warnings))
        self.assertTrue(any("already listed" in w for w in warnings))
        self.assertTrue(any("ODDNAME" in w for w in warnings))
        self.assertTrue(all(a["authoritative"] for a in added))
        self.assertEqual(fetch.systems_in(cfg), ["CLAIMS"])
        self.assertEqual(len(fetch.filter_sources(cfg, "CLAIMS")), 6)
        self.assertEqual(len(fetch.filter_sources(cfg, "All")), 6)
        man = fetch.write_manifest(cfg, os.path.join(tempfile.gettempdir(), "nope-manifest.json"))
        self.assertEqual([os.path.basename(p) for p in man["copylib_order"]["CLAIMS"]],
                         ["PROD.CLAIMS.COPYLIB", "PROD.COMMON.COPYLIB"])
        self.assertIn("CLAIMS", man["systems"])
        self.assertNotIn("PROD.IMS.PSBLIB", json.dumps(man))     # disabled sources stay out


PROGRAM = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. {name}.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-REC.
           COPY DUPREC.
       PROCEDURE DIVISION.
           GOBACK.
"""


class TwoDepartments(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        self.files = {
            "CLAIMS/PROD.CLAIMS.SRC/CLMPGM.cbl": PROGRAM.format(name="CLMPGM"),
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": "           05  DUP-FIELD-A   PIC X(5).\n",
            "CLAIMS/PROD.COMMON.COPYLIB/DUPREC.cpy": "           05  DUP-FIELD-C   PIC X(7).\n",
            "CLAIMS/PROD.CLAIMS.JCLLIB/CLMEXTR.jcl": "//CLMEXTR JOB (A)\n//S1 EXEC PGM=CLMPGM\n"
                                                    "//OUT DD DSN=PROD.CLM.EXTRACT(+1),DISP=(NEW,CATLG)\n//\n",
            "POLICY/PROD.POLICY.SRC/POLPGM.cbl": PROGRAM.format(name="POLPGM"),
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": "           05  DUP-FIELD-B   PIC X(9).\n",
            "POLICY/PROD.POLICY.JCLLIB/POLLOAD.jcl": "//POLLOAD JOB (A)\n//S1 EXEC PGM=POLPGM\n"
                                                    "//IN DD DSN=PROD.CLM.EXTRACT(0),DISP=SHR\n//\n",
        }
        for rel, text in self.files.items():
            p = os.path.join(self.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        self.db = os.path.join(self.td, "t.db")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _cfg(self, copylib_order):
        cfg = fetch.load_config(os.path.join(self.td, "nope.json"))
        cfg["local_root"] = self.root
        cfg["sources"] = [fetch.new_source("PROD.CLAIMS.SRC", "cobol", system="CLAIMS", authoritative=True)]
        for ds in copylib_order:
            cfg["sources"].append(fetch.new_source(ds, "copybook", system="CLAIMS", authoritative=True))
        cfg["sources"] += [fetch.new_source("PROD.CLAIMS.JCLLIB", "jcl", system="CLAIMS", authoritative=True),
                           fetch.new_source("PROD.POLICY.SRC", "cobol", system="POLICY", authoritative=True),
                           fetch.new_source("PROD.POLICY.COPYLIB", "copybook", system="POLICY", authoritative=True),
                           fetch.new_source("PROD.POLICY.JCLLIB", "jcl", system="POLICY", authoritative=True)]
        return cfg

    def _build(self, cfg=None):
        args = [self.root, "--db", self.db, "--rebuild", "--quiet"]
        if cfg is not None:
            man = os.path.join(self.td, "manifest.json")
            fetch.write_manifest(cfg, man)
            args += ["--manifest", man]
        build._main(args)
        return query.connect(self.db)

    def _copy_of(self, conn, program):
        return conn.execute("""SELECT m2.path FROM copy_use c JOIN member m ON m.id=c.member_id
                               JOIN member m2 ON m2.id=c.resolved_member_id
                               WHERE UPPER(m.name)=? AND c.copybook='DUPREC'""", (program,)).fetchone()[0]

    def test_each_department_resolves_its_own_copybook(self):
        conn = self._build(self._cfg(["PROD.CLAIMS.COPYLIB", "PROD.COMMON.COPYLIB"]))
        try:
            self.assertIn("PROD.CLAIMS.COPYLIB", self._copy_of(conn, "CLMPGM"))
            self.assertIn("PROD.POLICY.COPYLIB", self._copy_of(conn, "POLPGM"))
            systems = dict(conn.execute("SELECT name, system FROM member WHERE kind='cobol'").fetchall())
            self.assertEqual((systems["CLMPGM"], systems["POLPGM"]), ("CLAIMS", "POLICY"))
            self.assertIn("system: CLAIMS", query.cmd_program(conn, "CLMPGM"))
            amb = query.cmd_ambiguous(conn)
            self.assertIn("DUPREC", amb)
            self.assertIn("CLAIMS", amb)
            self.assertIn("POLICY", amb)
        finally:
            conn.close()

    def test_declared_copylib_order_decides_within_a_department(self):
        conn = self._build(self._cfg(["PROD.COMMON.COPYLIB", "PROD.CLAIMS.COPYLIB"]))
        try:
            self.assertIn("PROD.COMMON.COPYLIB", self._copy_of(conn, "CLMPGM"))
            note = conn.execute("SELECT detail FROM unresolved WHERE kind='ambiguous_copybook' AND detail LIKE '%CLMPGM%' "
                                "OR kind='ambiguous_copybook'").fetchone()[0]
            self.assertIn("same system + declared order", note)
        finally:
            conn.close()

    def test_without_manifest_the_folder_names_the_department(self):
        # estate\CLAIMS\PROD.CLAIMS.SRC: the folder between the root and the
        # library is the department even with no manifest, so each program
        # still gets its own department's copy - and the choice is still noted
        conn = self._build(None)
        try:
            notes = [r[0] for r in conn.execute("SELECT detail FROM unresolved WHERE kind='ambiguous_copybook'")]
            self.assertEqual(len(notes), 2)
            self.assertTrue(all("same system" in n for n in notes), notes)
            self.assertIn("PROD.CLAIMS.COPYLIB", self._copy_of(conn, "CLMPGM"))
            self.assertIn("PROD.POLICY.COPYLIB", self._copy_of(conn, "POLPGM"))
            self.assertEqual(conn.execute("SELECT system FROM member WHERE name='CLMPGM'").fetchone()[0], "CLAIMS")
        finally:
            conn.close()

    def test_dataset_flow_crossing_departments_is_called_out(self):
        conn = self._build(self._cfg(["PROD.CLAIMS.COPYLIB"]))
        try:
            out = query.cmd_dataset(conn, "PROD.CLM.EXTRACT")
            self.assertIn("Crosses departments", out)
            self.assertIn("written by CLAIMS", out)
            self.assertIn("read by POLICY", out)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
