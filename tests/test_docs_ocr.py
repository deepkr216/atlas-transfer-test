"""
Documents in an extra (non-mainframe) folder, the pictures inside them,
Windows OCR with zero model tokens, and document citations in the gate.
"""

import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, ocr, query, verify_citations  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(path, heading, paragraph, image_bytes=None):
    doc = (f'<w:document xmlns:w="{W}"><w:body>'
           f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{heading}</w:t></w:r></w:p>'
           f'<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>'
           f'</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)
        if image_bytes:
            z.writestr("word/media/image1.png", image_bytes)


class DocsInExtraRoot(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.estate = os.path.join(cls.td, "estate")
        shutil.copytree(FIX, cls.estate)
        cls.docs = os.path.join(cls.td, "docs", "member maintenance")
        os.makedirs(cls.docs)
        _docx(os.path.join(cls.docs, "MEMSPEC.docx"), "Member Maintenance",
              "CONDLOGX validates GENDER on the MEMMAP screen before the MEMB transaction updates the member.",
              b"\x89PNG\r\n\x1a\n" + b"\0" * 4000)
        cls.db = os.path.join(cls.td, "t.db")
        build._main([cls.estate, "--db", cls.db, "--rebuild", "--quiet", "--also", os.path.join(cls.td, "docs")])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_document_from_extra_folder_is_indexed_and_queryable(self):
        row = self.conn.execute("SELECT kind, parse_status FROM member WHERE name='MEMSPEC'").fetchone()
        self.assertEqual(tuple(row), ("doc", "ok"))
        out = query.cmd_docs(self.conn, "CONDLOGX")
        self.assertIn("MEMSPEC", out)
        self.assertIn("section 1", out)
        self.assertIn("1 image(s)", out)
        self.assertIn("MEMSPEC", query.cmd_program(self.conn, "CONDLOGX"))
        self.assertIn("Documents mentioning it", query.cmd_program(self.conn, "CONDLOGX"))
        self.assertIn("MEMSPEC", query.cmd_field(self.conn, "GENDER"))

    def test_images_are_extracted_and_listed(self):
        out_dir = os.path.join(self.td, "out", "images")
        imgs = ocr.extract_images(self.conn, out_dir)
        self.assertEqual(len(imgs), 1)
        self.assertTrue(imgs[0][3].endswith(os.path.join("MEMSPEC", "image1.png")))
        self.assertTrue(os.path.exists(imgs[0][3]))
        listing = query.cmd_images(self.conn)
        self.assertIn("MEMSPEC", listing)
        self.assertIn("not yet read", listing)
        self.assertIn("image1.png", query.cmd_images(self.conn, "MEMSPEC"))

    def test_document_citations_are_checked_by_section(self):
        res, _ = verify_citations.check_answer('The spec says so [[MEMSPEC 1 "validates GENDER"]].', db_path=self.db)
        self.assertEqual(res[0].status, "PASS")
        res, _ = verify_citations.check_answer('[[MEMSPEC 1 "nothing of the sort"]]', db_path=self.db)
        self.assertEqual(res[0].status, "FAIL")


class WindowsOcr(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        ok, why = ocr.ocr_available()
        if not ok:
            raise unittest.SkipTest(why)
        cls.td = tempfile.mkdtemp()
        cls.png = os.path.join(cls.td, "probe.png")
        if not ocr.render_text_png("POLICY STATUS CODE E123 MEANS LAPSED", cls.png):
            raise unittest.SkipTest("could not render a test image with System.Drawing")
        texts, warnings = ocr.ocr_images([cls.png])
        if not texts:
            raise unittest.SkipTest("OCR engine unavailable: " + "; ".join(warnings))
        cls.probe_text = next(iter(texts.values()))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_reads_rendered_text(self):
        self.assertIn("E123", self.probe_text)
        self.assertIn("LAPSED", self.probe_text)

    def test_ocr_text_lands_in_the_index_and_the_gate(self):
        estate = os.path.join(self.td, "estate")
        shutil.copytree(FIX, estate)
        docs = os.path.join(self.td, "docs")
        os.makedirs(docs)
        with open(self.png, "rb") as fh:
            png = fh.read()
        _docx(os.path.join(docs, "OCRDOC.docx"), "Status codes", "See the table in the picture below.", png)
        db = os.path.join(self.td, "ocr.db")
        build._main([estate, "--db", db, "--rebuild", "--quiet", "--also", docs])
        conn = query.connect(db)
        try:
            stats = ocr.run(conn, os.path.join(self.td, "out"), log=lambda s: None)
            self.assertEqual(stats["ocr_text"], 1)
            out = query.cmd_docs(conn, "E123")
            self.assertIn("OCRDOC", out)
            self.assertIn("image 1", out)
            self.assertIn("OCRDOC", query.cmd_search(conn, '"E123"', "doc", 10))
            res, _ = verify_citations.check_answer('[[OCRDOC 1001 "E123"]]', db_path=db)
            self.assertEqual(res[0].status, "PASS")
            self.assertIn("1 read by OCR", out)
            # second run reads nothing new
            self.assertEqual(ocr.run(conn, os.path.join(self.td, "out"), log=lambda s: None)["ocr_text"], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
