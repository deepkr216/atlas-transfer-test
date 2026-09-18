r"""
Screens read off a recording, checked against the estate (LESSONS 166).
The lines below are what the Windows OCR engine really returned for a 3270
screen shared inside a 720p and a 1080p recording: `000100` as `eeeløø`,
`WS-RESTART-FLAG` as `KS-RESTART-FLAG`. The index tells the invented from
the real.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import screentext  # noqa: E402

NAMES = ["CLMPOST", "WS-RESTART-FLAG", "WS-STEP-COUNT", "1000-OPEN-FILES", "2000-POST-CLAIMS", "CLAIM-FILE", "CLMIN",
         "0000-MAIN", "PROD.CLAIMS.SRC", "CLMNIGHT", "STEP010", "PROD.CLAIMS.DAILY.INPUT", "CLMEDIT"]

SHARED_720 = """Menu
EDIT
Utilities Compilers Help
PROD. CLAIMS . (CLmposT)
- el.e3
columns eeeel øøe72
Scroll
CSR
IDENTIFICATION DIVISION.
PRCQAM-ID. CLmposT.
ENVIRONMENT DIVISION.
SELECT CLAIM-FILE ASSIGN TO
CLMIN.
WORKING-STORAGE SECTION.
01 KS-RESTART-FLAG
01 KS-STEP-CWNT
aøea-MAIN.
PIC 9(4) core.
PERFORM leee-0PEN-FILES
PERFORM 2eee-POST-CLA1ms UNTIL
END-OF-FILE"""

SHARED_1080 = """Columns oeeel eøø72
eeeløø
eee2øø
PROGRAM-ID. CLMPOST .
01 WS-RESTART-FLAG
PIC X VALUE 'N' .
eeee-MAIN .
IOOO-OPEN-FILES
2000-POST-CLAIMS"""


class Checked(unittest.TestCase):

    def setUp(self):
        self.v = screentext.Vocabulary(NAMES, from_index=True)

    def test_a_misread_name_is_snapped_to_the_index_and_an_invented_one_marked(self):
        st = screentext.Stats()
        out = screentext.clean_screen(SHARED_720, self.v, st)
        self.assertIn("01 WS-RESTART-FLAG", out, out)
        self.assertNotIn("KS-RESTART-FLAG", out)
        self.assertIn("(CLMPOST)", out, "capitals and small letters mixed: the index name it is")
        self.assertIn("PRCQAM-ID?", out, "shaped like a name, in no index: marked, not passed as fact")
        for gone in ("eeeel", "el.e3", "aøea", "KS-STEP-CWNT", "CLA1ms"):
            self.assertNotIn(gone, out, gone)
        self.assertIn("PERFORM ? UNTIL", out, "an unreadable piece is a ?, the line stays because its keywords are real")
        self.assertGreaterEqual(st.snapped, 1)
        self.assertGreater(st.lines_in - st.lines_kept, 5, "the invented lines are gone")
        self.assertIn("dropped as unreadable", str(st))

    def test_the_engines_o_for_0_and_i_for_1_still_snap(self):
        st = screentext.Stats()
        out = screentext.clean_screen(SHARED_1080, self.v, st)
        self.assertIn("1000-OPEN-FILES", out, out)
        self.assertNotIn("IOOO", out)
        self.assertIn("PROGRAM-ID. CLMPOST", out)
        self.assertNotIn("eeel", out)
        self.assertNotIn("eeee-MAIN", out)

    def test_jcl_and_prose_on_a_slide_are_read_without_an_index(self):
        plain = screentext.Vocabulary()                                   # keywords and words only
        self.assertEqual(screentext.clean_line("//CLMNIGHT JOB (ACCT),'CLAIMS'", plain), "//CLMNIGHT JOB (ACCT),'CLAIMS'")
        self.assertEqual(screentext.clean_line("//STEP010 EXEC PGM=CLMPOST", plain), "//STEP010 EXEC PGM=CLMPOST")
        self.assertEqual(screentext.clean_line("Restart procedure for the nightly claims job", plain),
                         "Restart procedure for the nightly claims job")
        self.assertIsNone(screentext.clean_line("eeeløø eee2øø", plain))
        self.assertIsNone(screentext.clean_line("xq zv", plain), "nothing recognisable: dropped")
        self.assertIsNone(screentext.clean_line("", plain))

    def test_no_guess_between_two_names_equally_close(self):
        v = screentext.Vocabulary(["WS-FLAG-A", "WS-FLAG-B"], from_index=True)
        self.assertIsNone(v.snap("WS-FLAG-X"))
        self.assertEqual(v.snap("WS-FLAG-A"), "WS-FLAG-A")
        self.assertIsNone(v.snap("ABC"), "too short to guess")

    def test_names_come_from_the_index(self):
        import contextlib
        import io
        import shutil
        import tempfile
        from atlas import build
        td = tempfile.mkdtemp()
        try:
            src = os.path.join(td, "estate", "SRC")
            os.makedirs(src)
            for fn in ("SAMPPGM.cbl", "PMASTREC.cpy", "SAMPJOB.jcl"):
                shutil.copy(os.path.join(HERE, "fixtures", fn), src)
            db = os.path.join(td, "t.db")
            with contextlib.redirect_stdout(io.StringIO()):
                build._main([os.path.join(td, "estate"), "--db", db, "--rebuild", "--quiet"])
            said = []
            v = screentext.Vocabulary.from_db(db, log=said.append)
            self.assertTrue(v.from_index)
            for n in ("SAMPPGM", "PMASTREC", "SAMPJOB", "PM-POLICY-STATUS", "PROD.POLICY.MASTER.KSDS", "PROD"):
                self.assertIn(n, v.names, n)
            self.assertTrue(any("names from the index" in s for s in said), said)
            self.assertEqual(screentext.clean_line("MOVE PM-POLlCY-STATUS TO WS-X", v), "MOVE PM-POLICY-STATUS TO WS-X?")
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
