"""
Control cards have no content signature, so their library's kind must come
from somewhere: the folder name (CNTL / PARM / CARD / CTL - and now UTL, a
common name for a utility control-card library), or the kind the user
declared for that library in the sources table, which the manifest carries
as `kinds` and the build reads BEFORE typing members.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, classify, fetch, query  # noqa: E402

CARD = " SORT FIELDS=(1,10,CH,A)\n INCLUDE COND=(11,2,CH,EQ,C'AC')\n"


class DeclaredKinds(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        for folder, name in (("SHARED/PROD.SHARED.UTL", "SRTCARD"), ("SHARED/PROD.SHARED.MISC", "SRTCARD2"),
                             ("GC/PROD.GC.SRC", "SAMPPGM")):
            d = os.path.join(self.root, *folder.split("/"))
            os.makedirs(d, exist_ok=True)
            if name == "SAMPPGM":
                shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), d)
            else:
                with open(os.path.join(d, name + ".txt"), "w") as fh:
                    fh.write(CARD)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def kinds(self, db):
        conn = query.connect(db)
        rows = {r[0]: r[1] for r in conn.execute("SELECT name, kind FROM member")}
        conn.close()
        return rows

    def test_utl_folder_name_means_control_cards(self):
        self.assertEqual(classify.classify(os.path.join(self.root, "SHARED", "PROD.SHARED.UTL", "SRTCARD.txt"), CARD)[0],
                         "ctlcard")

    def test_declared_kind_types_a_library_the_name_cannot(self):
        db = os.path.join(self.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([self.root, "--db", db, "--rebuild", "--quiet"])
        k = self.kinds(db)
        self.assertEqual(k["SRTCARD"], "ctlcard")            # ...UTL: the folder name hint
        self.assertEqual(k["SRTCARD2"], "unknown")           # ...MISC: nothing says what it is
        # declare it in the sources table -> manifest "kinds" -> build types it
        cfg = fetch.load_config(os.path.join(self.td, "nope.json"))
        cfg["local_root"] = self.root
        cfg["sources"] = [fetch.new_source("PROD.SHARED.MISC", "ctlcard", system="SHARED"),
                          fetch.new_source("PROD.GC.SRC", "cobol", system="GC")]
        man = os.path.join(self.td, "manifest.json")
        fetch.write_manifest(cfg, man)
        with open(man, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["kinds"], {"PROD.SHARED.MISC": "ctlcard", "PROD.GC.SRC": "cobol"})
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([self.root, "--db", db, "--rebuild", "--quiet", "--manifest", man])
        k = self.kinds(db)
        self.assertEqual(k["SRTCARD2"], "ctlcard")
        self.assertEqual(k["SAMPPGM"], "cobol")              # content still wins over any declaration
        conn = query.connect(db)
        systems = {r[0]: r[1] for r in conn.execute("SELECT name, system FROM member")}
        conn.close()
        self.assertEqual(systems["SRTCARD2"], "SHARED")
        self.assertEqual(systems["SAMPPGM"], "GC")


class MfsRecognition(unittest.TestCase):
    """Every MFS statement can carry a label in column 1; a format library
    can be called FORMAT rather than MFS."""

    def test_labelled_statements_are_mfs(self):
        from atlas import classify
        head = ("MEMMSG   MSG   TYPE=INPUT,SOR=(MEMFMT,IGNORE),NXT=MEMOUT\n"
                "         SEG\n"
                "         MFLD  'MEMB'\n"
                "GENDER   MFLD  (GENDER,'N'),LTH=1\n"
                "         MSGEND\n")
        self.assertEqual(classify.classify("PROD.GC.WHATEVER/MEMMSG.txt", head)[0], "mfs")
        fmt = "MEMFMT   FMT\n         DEV   TYPE=3270-A2,FEAT=IGNORE\n"
        self.assertEqual(classify.classify("X/MEMFMT.txt", fmt)[0], "mfs")
        # a plain assembler member is not mistaken for MFS
        asm = "MYPGM    CSECT\n         STM   14,12,12(13)\nMSG      DC    C'HELLO'\n"
        self.assertNotEqual(classify.classify("X/MYPGM.txt", asm)[0], "mfs")

    def test_format_library_names(self):
        from atlas import classify
        for lib in ("PROD.GC.MFS", "PROD.GC.MFSSRC", "PROD.GC.FORMAT", "PROD.GC.FORMATS", "PROD.GC.FMTLIB", "PROD.GC.MSGLIB"):
            self.assertEqual(classify.classify(f"C:/estate/GC/{lib}/ANY.txt", "nothing here")[0], "mfs", lib)
        self.assertNotEqual(classify.classify("C:/estate/GC/PROD.GC.REFORMAT/ANY.txt", "nothing here")[0], "mfs")
        # the specific libraries win over the generic SRC / SOURCE rule
        for lib, kind in (("PROD.GC.PSBSOURCE", "psb"), ("PROD.GC.PSBSRC", "psb"), ("PROD.GC.DBDSRC", "dbd"),
                          ("PROD.GC.BMSSRC", "bms"), ("PROD.GC.MFSSRC", "mfs"), ("PROD.GC.SRC", "cobol"),
                          ("PROD.GC.SOURCE", "cobol")):
            self.assertEqual(classify.classify(f"C:/estate/GC/{lib}/ANY.txt", "nothing here")[0], kind, lib)


if __name__ == "__main__":
    unittest.main()
