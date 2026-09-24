"""
`segment SEG [--dbd DBD]` - each DBD field at its bytes in every program's
I/O area, whatever that program calls it.

  A field NAME is the wrong handle for "where is the gender field of this
  segment populated": every database has a gender field and every program's
  copybook names the byte its own way. The right handle is the DBD's FIELD
  (START/BYTES) -> the segment -> each program's DL/I I/O area -> the item at
  that byte range. The report says, per DBD field and per program, the item
  there (with the copybook line to cite), a FILLER as a FILLER, a shorter
  area as a miss, a run of items as "spans", a different copybook as "layout
  differs", and "stored by this program" on the writers. `dbd` counts the
  programs storing and reading each segment; `--dbd` keeps one DBD when two
  use the segment name. On an index built before the value-flow tables the
  copybook's own rows answer, with the caveat said.
"""

import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query, verify_citations  # noqa: E402

H = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. {p}.\n       ENVIRONMENT DIVISION.\n"
     "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n")

FILES = {
    "DEPDBD.dbd": ("         DBD   NAME=DEPDBD,ACCESS=HDAM\n"
                   "         SEGM  NAME=DEPSEG,PARENT=0,BYTES=50\n"
                   "         FIELD NAME=(DEPNO,SEQ,U),BYTES=8,START=1\n"
                   "         FIELD NAME=DEPNAME,BYTES=30,START=9\n"
                   "         FIELD NAME=BIRTHDT,BYTES=6,START=39\n"
                   "         FIELD NAME=GENDER,BYTES=1,START=45\n"
                   "         DBDGEN\n         FINISH\n         END\n"),
    # the same segment name in another database: --dbd keeps one
    "OTHDBD.dbd": ("         DBD   NAME=OTHDBD,ACCESS=HIDAM\n"
                   "         SEGM  NAME=DEPSEG,PARENT=0,BYTES=20\n"
                   "         FIELD NAME=(OKEY,SEQ,U),BYTES=4,START=1\n"
                   "         DBDGEN\n         FINISH\n         END\n"),
    "DEPSEG.cpy": ("           05  DEP-NO           PIC X(08).\n"
                   "           05  DEP-NAME         PIC X(30).\n"
                   "           05  DEP-BIRTH        PIC X(06).\n"
                   "           05  DEP-GENDER       PIC X(01).\n"
                   "           05  FILLER           PIC X(05).\n"),
    "DEPREC2.cpy": ("           05  WS-DEP-KEY       PIC X(08).\n"
                    "           05  WS-DEP-FIRST     PIC X(15).\n"
                    "           05  WS-DEP-LAST      PIC X(15).\n"
                    "           05  WS-DEP-DOB       PIC X(06).\n"
                    "           05  WS-DEP-SEX       PIC X(01).\n"
                    "           05  WS-DEP-SPARE     PIC X(05).\n"),
    "DEPREC3.cpy": ("           05  D3-NO            PIC X(08).\n"
                    "           05  D3-NAME          PIC X(30).\n"
                    "           05  FILLER           PIC X(06).\n"),
    "PGMA.psb": ("         PCB   TYPE=DB,DBDNAME=DEPDBD,PROCOPT=A,KEYLEN=8\n"
                 "         SENSEG NAME=DEPSEG,PARENT=0\n         PSBGEN LANG=COBOL,PSBNAME=PGMA\n         END\n"),
    "PGMB.psb": ("         PCB   TYPE=DB,DBDNAME=DEPDBD,PROCOPT=G,KEYLEN=8\n"
                 "         SENSEG NAME=DEPSEG,PARENT=0\n         PSBGEN LANG=COBOL,PSBNAME=PGMB\n         END\n"),
    "PGMC.psb": ("         PCB   TYPE=DB,DBDNAME=DEPDBD,PROCOPT=G,KEYLEN=8\n"
                 "         SENSEG NAME=DEPSEG,PARENT=0\n         PSBGEN LANG=COBOL,PSBNAME=PGMC\n         END\n"),
    "PGMO.psb": ("         PCB   TYPE=DB,DBDNAME=OTHDBD,PROCOPT=A,KEYLEN=4\n"
                 "         SENSEG NAME=DEPSEG,PARENT=0\n         PSBGEN LANG=COBOL,PSBNAME=PGMO\n         END\n"),
    # stores the segment from an area laid out by DEPSEG
    "PGMA.cbl": H.format(p="PGMA") + (
        "       01  WS-ISRT          PIC X(4) VALUE 'ISRT'.\n"
        "       01  DEP-IO-AREA.\n"
        "           COPY DEPSEG.\n"
        "       LINKAGE SECTION.\n       01  DEP-PCB PIC X(40).\n"
        "       PROCEDURE DIVISION USING DEP-PCB.\n"
        "           MOVE 'F' TO DEP-GENDER.\n"
        "           CALL 'CBLTDLI' USING WS-ISRT DEP-PCB DEP-IO-AREA.\n           GOBACK.\n"),
    # reads it into an area laid out by DEPREC2: the name split in two, the gender byte under another name
    "PGMB.cbl": H.format(p="PGMB") + (
        "       01  WS-GU            PIC X(4) VALUE 'GU  '.\n"
        "       01  WS-DEP-AREA.\n"
        "           COPY DEPREC2.\n"
        "       LINKAGE SECTION.\n       01  DEP-PCB PIC X(40).\n"
        "       PROCEDURE DIVISION USING DEP-PCB.\n"
        "           CALL 'CBLTDLI' USING WS-GU DEP-PCB WS-DEP-AREA.\n"
        "           DISPLAY WS-DEP-SEX.\n           GOBACK.\n"),
    # reads it into a shorter area: a FILLER over the birth date, nothing at the gender byte
    "PGMC.cbl": H.format(p="PGMC") + (
        "       01  WS-GN            PIC X(4) VALUE 'GN  '.\n"
        "       01  D3-AREA.\n"
        "           COPY DEPREC3.\n"
        "       LINKAGE SECTION.\n       01  DEP-PCB PIC X(40).\n"
        "       PROCEDURE DIVISION USING DEP-PCB.\n"
        "           CALL 'CBLTDLI' USING WS-GN DEP-PCB D3-AREA.\n"
        "           DISPLAY D3-NAME.\n           GOBACK.\n"),
    # the other database's DEPSEG, stored from an area declared in the program itself
    "PGMO.cbl": H.format(p="PGMO") + (
        "       01  WS-REPL          PIC X(4) VALUE 'REPL'.\n"
        "       01  OTH-AREA.\n"
        "           05  OTH-KEY          PIC X(04).\n"
        "           05  OTH-REST         PIC X(16).\n"
        "       LINKAGE SECTION.\n       01  OTH-PCB PIC X(40).\n"
        "       PROCEDURE DIVISION USING OTH-PCB.\n"
        "           CALL 'CBLTDLI' USING WS-REPL OTH-PCB OTH-AREA.\n           GOBACK.\n"),
}

GENDER = ("- GENDER (bytes 45-45): PGMA DEP-GENDER via copybook DEPSEG (X(01)) `DEPSEG:4` - stored by this program; "
          "PGMB WS-DEP-SEX via copybook DEPREC2 (X(01)) `DEPREC2:5` - layout differs from PGMA's; "
          "PGMC: no field at that offset (area shorter: 44 bytes)\n")
DEPNAME = ("- DEPNAME (bytes 9-38): PGMA DEP-NAME via copybook DEPSEG (X(30)) `DEPSEG:2` - stored by this program; "
           "PGMB spans WS-DEP-FIRST..WS-DEP-LAST via copybook DEPREC2 (X(15)..X(15)) `DEPREC2:2` `DEPREC2:3` "
           "- layout differs from PGMA's; "
           "PGMC D3-NAME via copybook DEPREC3 (X(30)) `DEPREC3:2` - layout differs from PGMA's\n")
BIRTHDT = ("- BIRTHDT (bytes 39-44): PGMA DEP-BIRTH via copybook DEPSEG (X(06)) `DEPSEG:3` - stored by this program; "
           "PGMB WS-DEP-DOB via copybook DEPREC2 (X(06)) `DEPREC2:4` - layout differs from PGMA's; "
           "PGMC: FILLER at that offset via copybook DEPREC3 (X(06), bytes 39-44) `DEPREC3:3`\n")
DEPNO = ("- DEPNO (SEQ) (bytes 1-8): PGMA DEP-NO via copybook DEPSEG (X(08)) `DEPSEG:1` - stored by this program; "
         "PGMB WS-DEP-KEY via copybook DEPREC2 (X(08)) `DEPREC2:1` - layout differs from PGMA's; "
         "PGMC D3-NO via copybook DEPREC3 (X(08)) `DEPREC3:1` - layout differs from PGMA's\n")
OKEY = "- OKEY (SEQ) (bytes 1-4): PGMO OTH-KEY in the program (X(04)) `PGMO:8` - stored by this program\n"


class SegmentOffsets(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        src = os.path.join(cls.td, "estate")
        os.makedirs(src)
        for name, text in FILES.items():
            with open(os.path.join(src, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([src, "--db", cls.db, "--rebuild", "--quiet"])
        cls.conn = query.connect(cls.db)
        cls.text = query.cmd_segment(cls.conn, "DEPSEG", "DEPDBD")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    # ---- the lines ----------------------------------------------------------

    def test_gender_byte_under_each_programs_own_name(self):
        self.assertIn(GENDER, self.text)

    def test_a_field_split_in_two_says_spans(self):
        self.assertIn(DEPNAME, self.text)

    def test_filler_is_said_as_filler_not_the_nearest_name(self):
        self.assertIn(BIRTHDT, self.text)
        self.assertNotIn("D3-NAME via copybook DEPREC3 (X(30)) `DEPREC3:2` - stored", self.text)

    def test_seq_field_and_no_note_for_the_first_program(self):
        self.assertIn(DEPNO, self.text)

    def test_io_area_table_and_sizes(self):
        self.assertIn("| PGMA | DEP-IO-AREA | copybook DEPSEG | 50 | ISRT | stores | PGMA:13 |", self.text)
        self.assertIn("| PGMB | WS-DEP-AREA | copybook DEPREC2 | 50 | GU | reads | PGMB:12 |", self.text)
        self.assertIn("| PGMC | D3-AREA | copybook DEPREC3 | 44 | GN | reads | PGMC:12 |", self.text)
        self.assertIn("the DBD says the segment is 50 bytes", self.text)

    # ---- --dbd and dbd ----------------------------------------------------------

    def test_dbd_filter_keeps_one_database(self):
        self.assertIn("# IMS segment DEPSEG in DBD DEPDBD\n", self.text)
        self.assertNotIn("OTHDBD", self.text)
        both = query.cmd_segment(self.conn, "DEPSEG")
        self.assertIn("2 DBDs hold a segment of this name; `segment DEPSEG --dbd NAME` keeps one", both)
        self.assertIn(GENDER, both)
        self.assertIn(OKEY, both)
        self.assertNotIn("PGMO", both.split("### DBD OTHDBD")[0].split("### DBD DEPDBD")[1])
        self.assertIn("**NOT FOUND** in any indexed DBD named NOPE.", query.cmd_segment(self.conn, "DEPSEG", "NOPE"))

    def test_dbd_counts_the_storers_and_readers(self):
        out = query.cmd_dbd(self.conn, "DEPDBD")
        self.assertIn("| DEPSEG | (root) | 50 | DEPNO | DEPNO@1/8*, DEPNAME@9/30, BIRTHDT@39/6, GENDER@45/1 | "
                      "1 program stores it, 2 programs read it |", out)
        self.assertIn("`segment NAME --dbd DEPDBD` puts each DBD field at its bytes", out)
        self.assertIn("| 1 program stores it, 0 programs read it |", query.cmd_dbd(self.conn, "OTHDBD"))

    # ---- cites and the CLI ----------------------------------------------------------

    def test_copybook_cites_pass_the_gate(self):
        answer = ('[[DEPSEG 4 "DEP-GENDER"]] [[DEPREC2 5 "WS-DEP-SEX"]] [[DEPREC3 3 "FILLER"]] '
                  '[[DEPREC2 2 "WS-DEP-FIRST"]] [[DEPREC2 3 "WS-DEP-LAST"]] [[PGMO 8 "OTH-KEY"]] [[PGMA 13 "CBLTDLI"]]')
        res, _u = verify_citations.check_answer(answer, db_path=self.db)
        self.assertEqual([r.status for r in res], ["PASS"] * 7, [(r.status, r.detail) for r in res])

    def test_cli_with_dbd(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = query._main(["--db", self.db, "segment", "DEPSEG", "--dbd", "depdbd"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn(GENDER, out.getvalue())

    # ---- an index built before the value-flow tables ------------------------------

    def test_old_index_answers_from_the_copybooks_own_rows_and_says_so(self):
        old = os.path.join(self.td, "old.db")
        shutil.copyfile(self.db, old)
        c = sqlite3.connect(old)
        c.executescript("DROP TABLE pfield;")
        c.close()
        conn = query.connect(old)
        try:
            text = query.cmd_segment(conn, "DEPSEG")
        finally:
            conn.close()
        self.assertIn(GENDER, text)
        self.assertIn(BIRTHDT, text)
        self.assertIn(OKEY, text)
        self.assertIn("| PGMA | DEP-IO-AREA | copybook DEPSEG (this index was built before the value-flow tables: "
                      "rebuild it for this program's own offsets) | 50 | ISRT | stores | PGMA:13 |", text)
        self.assertIn("| PGMO | OTH-AREA | in the program (this index was built before", text)


if __name__ == "__main__":
    unittest.main()
