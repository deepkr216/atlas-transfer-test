"""
Transaction routing (CICS CSD / IMS stage-1), incremental re-indexing, and
the Zowe fetch layer (tested with a fake runner - no Zowe needed).
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, classify, fetch, query, reader, txn  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def _load(name):
    return reader.load(os.path.join(FIX, name))


class RoutingParsing(unittest.TestCase):

    def test_csd_define_form(self):
        text, _, _ = _load("MEMCSD.csd")
        f = txn.parse_csd(text)
        tx = {t.tran_code: t for t in f.transactions}
        self.assertEqual((tx["MEMB"].program, tx["MEMB"].group, tx["MEMB"].system), ("CONDLOGX", "MEMGRP", "cics"))
        self.assertIsNone(tx["MEMR"].program)
        self.assertIn("REMOTESYSTEM CIC2", tx["MEMR"].detail)
        self.assertTrue(any("MEMR" in w for w in f.warnings))
        self.assertEqual([(p.name, p.language) for p in f.programs], [("CONDLOGX", "COBOL")])
        self.assertEqual([(x.name, x.dsname) for x in f.files], [("POLMAST", "PROD.POLICY.MASTER.KSDS")])

    def test_csd_list_report_form(self):
        rep = "\n".join([
            "TRANSACTION(CLMU)              GROUP(CLMGRP)",
            "   DESCRIPTION : CLAIM UPDATE",
            "   PROGRAM     : CLMUPDT",
            "   TWASIZE     : 00000",
            "PROGRAM(CLMUPDT)               GROUP(CLMGRP)",
            "   LANGUAGE    : COBOL",
        ])
        f = txn.parse_csd(rep)
        self.assertEqual([(t.tran_code, t.program) for t in f.transactions], [("CLMU", "CLMUPDT")])
        self.assertEqual(f.programs[0].language, "COBOL")

    def test_ims_stage1(self):
        text, _, _ = _load("MEMSTG1.imsgen")
        f = txn.parse_imsgen(text)
        tx = {t.tran_code: t for t in f.transactions}
        self.assertEqual((tx["MEMB"].program, tx["MEMB"].psb, tx["MEMB"].system), ("MEMBRVAL", "MEMBRVAL", "ims_dc"))
        self.assertIn("assumed = PSB name", tx["MEMB"].detail)
        self.assertEqual((tx["GENA"].program, tx["GENA"].psb), ("GENPGM", None))   # GPSB names the program
        self.assertIn("GENB", tx)
        self.assertEqual([d[0] for d in f.databases], ["POLDBD", "CLMDBD"])

    def test_classify_routing_members(self):
        for name, kind in (("MEMCSD.csd", "csd"), ("MEMSTG1.imsgen", "imsgen")):
            text, _, _ = _load(name)
            self.assertEqual(classify.classify("x/" + name.split(".")[0], text)[0], kind)


class RoutingEndToEnd(unittest.TestCase):

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

    def test_transaction_rows_and_program_dossier(self):
        rows = {(r[0], r[1], r[2]) for r in self.conn.execute("SELECT tran_code, system, program FROM transaction_def")}
        self.assertIn(("MEMB", "cics", "CONDLOGX"), rows)
        self.assertIn(("MEMB", "ims_dc", "MEMBRVAL"), rows)
        self.assertIn("Online: MEMB (cics)", query.cmd_program(self.conn, "CONDLOGX"))
        out = query.cmd_transaction(self.conn, "MEMB")
        self.assertIn("CONDLOGX", out)
        self.assertIn("MEMBRVAL", out)
        self.assertIn("`CONDLOGX` is indexed", out)
        self.assertIn("GENPGM is not in the index", query.cmd_transaction(self.conn, "GENA"))

    def test_online_programs_are_no_longer_dead_candidates(self):
        out = query.cmd_dead(self.conn)
        self.assertIn("MULTILN", out)
        self.assertNotIn("| CONDLOGX |", out)
        self.assertNotIn("| MEMBRVAL |", out)

    def test_cics_file_maps_to_dataset(self):
        r = self.conn.execute("SELECT dsname FROM cics_file WHERE name='POLMAST'").fetchone()
        self.assertEqual(r[0], "PROD.POLICY.MASTER.KSDS")
        self.assertEqual(self.conn.execute("SELECT is_vsam FROM dataset WHERE dsn='PROD.POLICY.MASTER.KSDS'").fetchone()[0], 1)


class IncrementalBuild(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        shutil.copytree(FIX, self.root)
        self.db = os.path.join(self.td, "t.db")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _counts(self):
        c = query.connect(self.db)
        try:
            return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("member", "program", "call_edge", "field", "literal_ref", "src_fts", "transaction_def")}
        finally:
            c.close()

    def test_rerun_is_idempotent_and_changes_propagate(self):
        build._main([self.root, "--db", self.db, "--rebuild", "--quiet"])
        first = self._counts()
        build._main([self.root, "--db", self.db, "--quiet"])          # nothing changed
        self.assertEqual(self._counts(), first)                        # no duplicated facts

        c = query.connect(self.db)
        exp_before = c.execute("SELECT exp_lines FROM program WHERE program_id='SAMPPGM'").fetchone()[0]
        c.close()
        with open(os.path.join(self.root, "PMASTREC.cpy"), "a", encoding="utf-8") as fh:
            fh.write("000310*  a comment appended to the copybook\n")
        build._main([self.root, "--db", self.db, "--quiet"])
        after = self._counts()
        self.assertEqual(after["program"], first["program"])
        self.assertEqual(after["call_edge"], first["call_edge"])
        c = query.connect(self.db)
        exp_after = c.execute("SELECT exp_lines FROM program WHERE program_id='SAMPPGM'").fetchone()[0]
        c.close()
        self.assertEqual(exp_after, exp_before + 1)                     # SAMPPGM was re-expanded

        os.remove(os.path.join(self.root, "MULTILN.cbl"))
        build._main([self.root, "--db", self.db, "--quiet"])
        c = query.connect(self.db)
        self.assertIsNone(c.execute("SELECT id FROM program WHERE program_id='MULTILN'").fetchone())
        self.assertEqual(c.execute("SELECT COUNT(*) FROM src_fts WHERE member_name='MULTILN'").fetchone()[0], 0)
        c.close()


class FetchLayer(unittest.TestCase):

    def _cfg(self, **zowe):
        cfg = fetch.load_config(os.path.join(self.td, "nope.json"))
        cfg["local_root"] = os.path.join(self.td, "estate")
        cfg["zowe"].update(zowe)
        cfg["sources"] = [
            fetch.new_source("PROD.CLAIMS.SRC", "cobol", system="CLAIMS", authoritative=True),
            fetch.new_source("PROD.CICS.CSD", "csd", type="seq", local="routing/csd.txt"),
        ]
        return cfg

    def setUp(self):
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_config_round_trip(self):
        p = os.path.join(self.td, "sources.json")
        cfg = self._cfg(profile="prod")
        fetch.save_config(cfg, p)
        back = fetch.load_config(p)
        self.assertEqual(back["zowe"]["profile"], "prod")
        self.assertEqual(back["sources"][0]["ext"], "cbl")
        self.assertTrue(back["sources"][0]["authoritative"])
        self.assertEqual(back["sources"][1]["type"], "seq")

    @mock.patch("atlas.fetch.zowe_exe", return_value="zowe")
    def test_commands(self, _which):
        cfg = self._cfg(profile="prod", encoding="1047")
        pds = fetch.download_cmd(cfg, cfg["sources"][0])
        self.assertEqual(pds[:4], ["zowe", "zos-files", "download", "all-members"])
        self.assertIn("PROD.CLAIMS.SRC", pds)
        self.assertIn("--directory", pds)
        self.assertIn(os.path.normpath(os.path.join(cfg["local_root"], "PROD.CLAIMS.SRC")), pds)
        self.assertEqual(pds[pds.index("--extension") + 1], "cbl")
        self.assertEqual(pds[pds.index("--zosmf-profile") + 1], "prod")
        self.assertEqual(pds[pds.index("--encoding") + 1], "1047")
        seq = fetch.download_cmd(cfg, cfg["sources"][1])
        self.assertEqual(seq[:4], ["zowe", "zos-files", "download", "data-set"])
        self.assertIn("--file", seq)
        self.assertTrue(seq[seq.index("--file") + 1].endswith(os.path.join("routing", "csd.txt")))
        self.assertIn("--response-format-json", fetch.list_members_cmd(cfg, "PROD.X"))

    def test_member_list_parsing(self):
        js = '{"success":true,"data":{"apiResponse":{"items":[{"member":"CLMPOST"},{"member":"CLMUPDT"}]}}}'
        self.assertEqual(fetch.parse_member_list(js), ["CLMPOST", "CLMUPDT"])
        self.assertEqual(fetch.parse_member_list("CLMPOST\nCLMUPDT\n"), ["CLMPOST", "CLMUPDT"])

    @mock.patch("atlas.fetch.zowe_exe", return_value=None)
    def test_check_reports_missing_zowe(self, _which):
        ok, msg = fetch.check_zowe(self._cfg())
        self.assertFalse(ok)
        self.assertIn("not on PATH", msg)

    @mock.patch("atlas.fetch.zowe_exe", return_value="zowe")
    def test_fetch_all_with_fake_runner_and_manifest(self, _which):
        cfg = self._cfg()
        seen = []

        class FakeRunner:
            def run(self, cmd):
                seen.append(cmd)
                # simulate zowe writing one member into the target directory
                if "--directory" in cmd:
                    d = cmd[cmd.index("--directory") + 1]
                    os.makedirs(d, exist_ok=True)
                    with open(os.path.join(d, "clmpost.cbl"), "w") as fh:
                        fh.write("       IDENTIFICATION DIVISION.\n")
                    return 0, "1 member(s) downloaded", ""
                return 8, "", "z/OSMF error: dataset not found"

        logs = []
        res = fetch.fetch_all(cfg, runner=FakeRunner(), log=logs.append)
        self.assertEqual([r.ok for r in res], [True, False])
        self.assertEqual(res[0].files, 1)
        self.assertTrue(cfg["sources"][0]["last_result"].startswith("ok"))
        self.assertIn("not found", cfg["sources"][1]["last_result"])
        self.assertEqual(len(seen), 2)
        self.assertTrue(any(l.startswith("> ") for l in logs))

        man = fetch.write_manifest(cfg, os.path.join(self.td, "manifest.json"))
        self.assertEqual(len(man["authoritative"]), 1)
        self.assertTrue(man["authoritative"][0].endswith("PROD.CLAIMS.SRC"))
        self.assertEqual(man["system_of"]["PROD.CLAIMS.SRC"], "CLAIMS")
        cmd = fetch.build_cmd(cfg, os.path.join(self.td, "manifest.json"), rebuild=True)
        self.assertIn("atlas.build", cmd)
        self.assertIn("--rebuild", cmd)

    def test_ui_module_imports(self):
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter not available")
        from atlas import ui
        self.assertTrue(hasattr(ui, "App"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
