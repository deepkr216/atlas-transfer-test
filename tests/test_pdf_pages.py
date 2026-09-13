"""
Scanned or undecodable PDFs: their pages are rendered with the Windows PDF
renderer and read by the Windows OCR engine, becoming citable sections
1001+ of the document. The end-to-end part runs only where the engine and
the renderer exist (Windows 10/11); the decision logic runs everywhere.
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

from atlas import build, ocr, query  # noqa: E402


def tiny_pdf(text: str) -> bytes:
    """A one-page PDF with `text` in 30-point Helvetica, valid xref included."""
    content = f"BT /F1 30 Tf 60 700 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


class PdfPages(unittest.TestCase):

    def test_which_pdfs_get_their_pages_rendered(self):
        self.assertTrue(ocr._pdf_needs_pages("no extractable text; 3 embedded image(s) - likely a scan, needs OCR"))
        self.assertTrue(ocr._pdf_needs_pages("PDF text recovered from content streams without a font decoder: text using CID/Identity-H fonts may be garbled - verify before quoting"))
        self.assertFalse(ocr._pdf_needs_pages(None))
        self.assertFalse(ocr._pdf_needs_pages(""))

    def test_scanned_pdf_pages_are_read_end_to_end(self):
        ok, why = ocr.ocr_available()
        if not ok:
            self.skipTest(why)
        td = tempfile.mkdtemp()
        try:
            # probe the two Windows engines first: where either is missing (no OCR
            # language pack, a locked-down WinRT) the toolkit degrades and says so,
            # and this test has nothing to prove - skip, do not fail
            probe = os.path.join(td, "probe.png")
            if not ocr.render_text_png("WAIVER PROBE", probe):
                self.skipTest("could not render a probe image with System.Drawing")
            texts, warnings = ocr.ocr_images([probe])
            if not texts or "WAIVER" not in next(iter(texts.values())).upper():
                self.skipTest("OCR engine unavailable or unreadable here: " + "; ".join(warnings))
            docs = os.path.join(td, "specs")
            os.makedirs(docs)
            with open(os.path.join(docs, "SCANSPEC.pdf"), "wb") as fh:
                fh.write(tiny_pdf("WAIVER OF PREMIUM APPLIES AFTER SIX MONTHS"))
            total, pages, warns = ocr.render_pdf_pages(os.path.join(docs, "SCANSPEC.pdf"), os.path.join(td, "probe-pdf"))
            if not pages:
                self.skipTest("Windows PDF renderer unavailable here: " + "; ".join(warns))
            est = os.path.join(td, "estate", "SRC")
            os.makedirs(est)
            shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), est)
            db = os.path.join(td, "t.db")
            with contextlib.redirect_stdout(io.StringIO()):
                build._main([os.path.join(td, "estate"), "--db", db, "--rebuild", "--quiet", "--also", docs])
            conn = query.connect(db)
            log = []
            # this PDF has extractable text, so it would not be rendered by default; force it
            stats = ocr.run(conn, os.path.join(td, "out", "images"), member="SCANSPEC", log=log.append, pdf_pages="all")
            if any("renderer unavailable" in l for l in log):
                self.skipTest("Windows PDF renderer not available here")
            self.assertGreaterEqual(stats["images"], 1, log)
            rows = conn.execute("SELECT name, extracted_path, ocr_text FROM doc_image WHERE name LIKE 'page-%'").fetchall()
            self.assertEqual([r["name"] for r in rows], ["page-0001"], log)
            self.assertTrue(os.path.exists(rows[0]["extracted_path"]))
            self.assertIn("WAIVER", (rows[0]["ocr_text"] or "").upper(), log)
            sec = conn.execute("SELECT ordinal, heading, text FROM doc_section WHERE ordinal>=1001").fetchone()
            self.assertEqual(sec["ordinal"], 1001)
            self.assertEqual(sec["heading"], "page 1 (OCR of the scanned page)")
            # reachable through the document queries, and citable
            self.assertIn("SCANSPEC:1001", query.cmd_docs(conn, "waiver"))
            self.assertIn("section 1001 - page 1", query.cmd_doc(conn, "SCANSPEC", grep="premium"))
            # a second run does not render again
            stats2 = ocr.run(conn, os.path.join(td, "out", "images"), member="SCANSPEC", log=log.append, pdf_pages="all")
            self.assertEqual(stats2["images"], 0)
            # the default leaves a PDF with readable text alone
            conn.execute("DELETE FROM doc_image")
            conn.execute("DELETE FROM doc_section WHERE ordinal>=1001")
            conn.commit()
            stats3 = ocr.run(conn, os.path.join(td, "out", "images"), member="SCANSPEC", log=log.append)
            self.assertEqual(stats3["images"], 0)
            conn.close()
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
