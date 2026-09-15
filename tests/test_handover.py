r"""
`python -m atlas.handover`: every report a transition document needs, in
one file, with no model tokens - the release from `diff --system`, a diff
per changed member, the walk of each changed program's NEW copy, the
copybooks' users and layouts, crud and program for the jobs, the documents
named; the release-facts template copied beside it.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from atlas import build, handover, query  # noqa: E402
from test_doc_tables import write_workbook  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def _read(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def _write(folder, name, text):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class Handover(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        estate = os.path.join(cls.td, "estate")
        pgm, rec, proc = _read("WALKPGM.cbl"), _read("WALKREC.cpy"), _read("WALKPROC.cpy")
        _write(os.path.join(estate, "GC", "PROD.GC.SRC"), "WALKPGM.cbl", pgm)
        _write(os.path.join(estate, "GC", "PROD.GC.CPY"), "WALKREC.cpy", rec)
        _write(os.path.join(estate, "GC", "PROD.GC.CPY"), "WALKPROC.cpy", proc)
        new_pgm = (pgm.replace("           MOVE 0 TO WS-READ-COUNT.", "           MOVE 1 TO WS-READ-COUNT.")
                      .replace("           ADD 1 TO WS-READ-COUNT.\n",
                               "           CALL 'NEWRATE' USING WS-RATE-PARM.\n           ADD 1 TO WS-READ-COUNT.\n"
                               "       2200-AUDIT.\n           DISPLAY 'AUDIT ' WR-POLICY-NO.\n"))
        _write(os.path.join(estate, "GC-DEV", "DEV.GC.SRC"), "WALKPGM.cbl", new_pgm)
        _write(os.path.join(estate, "GC-DEV", "DEV.GC.CPY"), "WALKREC.cpy",
               rec.replace("           05  WR-CLAIM-AMT", "           05  WR-GENDER            PIC X(01).\n           05  WR-CLAIM-AMT"))
        _write(os.path.join(estate, "GC-DEV", "DEV.GC.CPY"), "WALKPROC.cpy", proc)
        _write(os.path.join(estate, "GC-DEV", "DEV.GC.SRC"), "NEWRATE.cbl",
               "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. NEWRATE.\n       PROCEDURE DIVISION.\n"
               "       0000-MAIN.\n           GOBACK.\n")
        docdir = os.path.join(cls.td, "docs", "QA")
        os.makedirs(docdir)
        write_workbook(os.path.join(docdir, "QAREL12.xlsx"))
        cls.db = os.path.join(cls.td, "t.db")
        build._main([estate, "--db", cls.db, "--rebuild", "--quiet", "--also", os.path.join(cls.td, "docs")])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_system_prefix_picks_the_copy(self):
        self.assertEqual(query.programs_named(self.conn, "GC-DEV/WALKPGM")[0]["system"], "GC-DEV")
        self.assertEqual(query.programs_named(self.conn, "GC/WALKPGM")[0]["system"], "GC")
        self.assertEqual(query.programs_named(self.conn, "NOPE/WALKPGM"), [])
        walk = query.cmd_walk(self.conn, "GC-DEV/WALKPGM", budget=8000)
        self.assertIn("2200-AUDIT", walk)                                 # the NEW copy
        self.assertNotIn("2200-AUDIT", query.cmd_walk(self.conn, "GC/WALKPGM", budget=8000))
        # the source lines are the NEW copy's own, and the cites name that copy
        self.assertIn("MOVE 1 TO WS-READ-COUNT", walk)
        self.assertNotIn("MOVE 0 TO WS-READ-COUNT", walk)
        self.assertIn("GC-DEV/WALKPGM:26-31", walk)
        self.assertIn('cite as `[[GC-DEV/WALKPGM line "token"]]` (this copy, not another system\'s)', walk)
        self.assertIn('from COPY WALKPROC - cite as [[GC-DEV/WALKPROC line "token"]]', walk)   # the copybook's own copy
        old = query.cmd_walk(self.conn, "GC/WALKPGM", budget=8000)
        self.assertIn("MOVE 0 TO WS-READ-COUNT", old)
        self.assertIn("GC/WALKPGM:26-31", old)
        # cite reaches the named copy
        self.assertIn("MOVE 1 TO WS-READ-COUNT", query.cmd_cite(self.conn, "GC-DEV/WALKPGM", "34-34", None))
        self.assertIn("MOVE 0 TO WS-READ-COUNT", query.cmd_cite(self.conn, "GC/WALKPGM", "34-34", None))
        self.assertIn("no copy in system NOPE", query.cmd_cite(self.conn, "NOPE/WALKPGM", "1-5", None))
        # paragraph: the new copy's lines
        para = query.cmd_paragraph(self.conn, "GC-DEV/WALKPGM", "1000-INIT")
        self.assertIn("MOVE 1 TO WS-READ-COUNT", para)
        self.assertIn('[[GC-DEV/WALKPGM line "token"]]', para)

    def test_pack_holds_every_report_once(self):
        said = []
        pack = handover.build_pack(self.conn, "GC-DEV", doc_names=["QAREL12"], budget=6000, say=said.append)
        self.assertIn("# Hand-over pack", pack)
        self.assertIn("changed copies in system **GC-DEV**", pack)
        self.assertIn("- 2 program(s), 1 copybook(s), 0 other member(s), 1 document(s)", pack)   # NEWRATE is new in DEV
        self.assertIn("# Release contents", pack)
        self.assertIn("| WALKPGM | cobol | GC/WALKPGM | GC-DEV/WALKPGM |", pack)
        self.assertIn("# Diff GC/WALKPGM -> GC-DEV/WALKPGM", pack)
        self.assertIn("# Diff GC/WALKREC -> GC-DEV/WALKREC", pack)
        self.assertIn("**Fields shifted** by +1 byte(s)", pack)
        self.assertIn("## New in GC-DEV", pack)                          # NEWRATE exists only in DEV
        self.assertIn("_no previous copy of NEWRATE (cobol) in the index", pack)
        self.assertEqual(pack.count("# Walk "), 2, "one walk per changed program: WALKPGM and NEWRATE")
        self.assertIn("2200-AUDIT", pack)
        self.assertIn("# Impact of copybook WALKREC", pack)
        self.assertIn("# Layout WALKREC (as seen in GC-DEV/WALKPGM)", pack)
        self.assertIn("WR-GENDER", pack)
        self.assertIn("# CRUD", pack)
        self.assertIn("# Document QAREL12", pack)
        self.assertIn("row 2: TC-GEN-01 | Add gender N | Accepted | Accepted | PASS", pack)
        self.assertRegex(pack, r"- size: [\d,]+ characters, about [\d,]+ tokens")
        self.assertTrue(any(s.startswith("walk GC-DEV/WALKPGM") for s in said), said)

    def test_pack_without_a_second_system(self):
        pack = handover.build_pack(self.conn, None, members=["WALKPGM", "WALKREC", "NOSUCH"], doc_names=[])
        self.assertIn("no second system given", pack)
        self.assertIn("_no previous copy of WALKPGM (cobol)", pack)
        self.assertIn("# Walk ", pack)
        self.assertIn("# Impact of copybook WALKREC", pack)
        self.assertIn("NOSUCH", pack)

    def test_cli_writes_the_pack_and_the_release_facts(self):
        out = os.path.join(self.td, "work", "handover.md")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = handover.main(["--db", self.db, "--system", "GC-DEV", "--docs-folder", "QA", "--out", out])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(out))
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("# Document QAREL12", text)                         # found by folder
        self.assertTrue(os.path.isfile(os.path.join(self.td, "work", "release-facts.md")))
        self.assertIn("release facts:", buf.getvalue())
        self.assertIn("about", buf.getvalue())
        # an empty --system and an empty --members (what a task passes when the box is left blank) is refused, not a crash
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = handover.main(["--db", self.db, "--system", "", "--members", "", "--out", out])
        self.assertEqual(rc, 2)
        self.assertIn("give --system", err.getvalue())
        # PowerShell drops an empty "" argument: --system with nothing after it must still parse
        with contextlib.redirect_stderr(io.StringIO()):
            rc = handover.main(["--db", self.db, "--system", "--members", "WALKREC", "--out", out])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
