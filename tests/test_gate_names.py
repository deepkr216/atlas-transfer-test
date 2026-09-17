r"""
A document is cited by the name the reports print, and that name is its file
name without the extension: a recording's transcript is KT-SESSION.VIDEO, a
plan is CLAIMS IMPLEMENTATION PLAN V1.2. The gate's grammar allowed neither
a dot nor a space (LESSONS 153): every such citation came back 'unreadable
member reference' and the answer was rejected - and a citation the grammar
could not read AT ALL was not counted, so an answer built on one was
certified with that claim unchecked.
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

from atlas import build, query, verify_citations, video  # noqa: E402


class DocumentNames(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        estate = os.path.join(cls.td, "estate", "SRC")
        os.makedirs(estate)
        shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), estate)
        docs = os.path.join(cls.td, "docs")
        os.makedirs(docs)
        video.write_docx(os.path.join(docs, "Claims Implementation Plan v1.2.docx"), "Claims plan", ["written for a test"],
                         [("Restart procedure", ["Rerun the posting job from STEP020 after the input file is fixed."])])
        video.write_docx(os.path.join(docs, "Data Masking .docx"), "Masking", ["written for a test"],
                         [("Rules", ["Mask the member number before the extract leaves the mainframe."])])
        secs = video.sections([(1.0, "//STEP010 EXEC PGM=CLMPOST")], [(2.0, "restart from step ten")], "SAID")
        video.write_docx(os.path.join(docs, "kt-session.video.docx"), "Video kt-session.mp4", ["written by atlas.video"], secs)
        cls.db = os.path.join(cls.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([os.path.join(cls.td, "estate"), "--db", cls.db, "--rebuild", "--also", docs])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def statuses(self, text):
        res, _uncited = verify_citations.check_answer(text, db_path=self.db)
        return [(r.status, r.detail) for r in res]

    def test_a_document_with_spaces_and_a_dot_in_its_name_is_cited_as_the_reports_print_it(self):
        conn = query.connect(self.db)
        try:
            hit = query.cmd_docs(conn, "STEP020")
        finally:
            conn.close()
        self.assertIn("CLAIMS IMPLEMENTATION PLAN V1.2:2", hit, "the cite the report prints")
        st = self.statuses('The job is rerun from STEP020 [[CLAIMS IMPLEMENTATION PLAN V1.2 2 "from STEP020"]].')
        self.assertEqual([s for s, _d in st], ["PASS"], st)
        st = self.statuses('[[KT-SESSION.VIDEO 2 "PGM=CLMPOST"]] and [[KT-SESSION.VIDEO:2 "step ten"]]')
        self.assertEqual([s for s, _d in st], ["PASS", "PASS"], st)
        st = self.statuses('[[CLAIMS IMPLEMENTATION PLAN V1.2 2 "no such words"]]')
        self.assertEqual(st[0][0], "FAIL", "the token is still checked against the section")

    def test_a_document_whose_file_name_ends_with_a_space_is_cited_without_it(self):
        st = self.statuses('[[DATA MASKING 2 "member number"]]')
        self.assertEqual([s for s, _d in st], ["PASS"], st)
        conn = query.connect(self.db)
        try:
            self.assertEqual(len(query._doc_members(conn, "DATA MASKING")), 1)
        finally:
            conn.close()

    def test_a_citation_the_gate_cannot_read_fails_the_answer_instead_of_slipping_through(self):
        ans = os.path.join(self.td, "answer.md")
        with open(ans, "w", encoding="utf-8") as fh:
            fh.write('SAMPPGM calls VALIDATE [[SAMPPGM 27 "CALL \'VALIDATE\'"]].\n'
                     'The posting job restarts from STEP020 [[Claims Plan section 2]].\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = verify_citations.main([ans, "--db", self.db])
        self.assertEqual(rc, 1, buf.getvalue())
        self.assertIn("unreadable citation", buf.getvalue())
        self.assertIn("REJECT", buf.getvalue())

    def test_the_forms_the_code_reports_print_still_resolve(self):
        st = self.statuses('[[SAMPPGM 27 "CALL \'VALIDATE\'"]] [[SAMPPGM(cobol) 27 "VALIDATE"]] '
                           '[[SAMPPGM@SRC 27 "VALIDATE"]] [[SAMPPGM:27 "VALIDATE"]]')
        self.assertEqual([s for s, _d in st], ["PASS"] * 4, st)
        st = self.statuses('[[NO SUCH DOCUMENT 2 "x"]]')
        self.assertEqual(st[0][0], "FAIL", st)
        self.assertNotIn("unreadable", st[0][1], "a readable name that is not in the index says so")


if __name__ == "__main__":
    unittest.main()
