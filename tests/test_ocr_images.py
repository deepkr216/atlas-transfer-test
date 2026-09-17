r"""
Pictures the OCR engine cannot decode (LESSONS 150): a diagram pasted into
Word or Visio is stored as a metafile (EMF/WMF), and the engine fails on it
outright - so such a diagram was listed as a picture and never read. It is
now drawn onto a bitmap first, and a multi-page TIFF gives one image per
page. The end-to-end part runs only where Windows imaging and the OCR
engine exist; the wiring runs everywhere.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, ocr, query  # noqa: E402

EMF_SCRIPT = r"""
param([string]$Out)
Add-Type -AssemblyName System.Drawing
$ref = New-Object System.Drawing.Bitmap 1, 1
$g0 = [System.Drawing.Graphics]::FromImage($ref)
$hdc = $g0.GetHdc()
$mf = New-Object System.Drawing.Imaging.Metafile($Out, $hdc)
$g0.ReleaseHdc($hdc)
$g = [System.Drawing.Graphics]::FromImage($mf)
$g.Clear([System.Drawing.Color]::White)
$font = New-Object System.Drawing.Font("Arial", 18)
$g.DrawString("CLAIM POSTING FLOW", $font, [System.Drawing.Brushes]::Black, 10, 10)
$g.DrawString("STEP 010 CLMPOST", $font, [System.Drawing.Brushes]::Black, 10, 50)
$g.Dispose(); $mf.Dispose(); $g0.Dispose(); $ref.Dispose()
"""


def make_emf(path: str) -> bool:
    """A metafile with two lines of text, drawn the way Word stores a pasted
    diagram. False where Windows imaging is not available."""
    if sys.platform != "win32":
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as fh:
        fh.write(EMF_SCRIPT)
        script = fh.name
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Out", path],
                       capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            os.remove(script)
        except OSError:
            pass
    return os.path.isfile(path) and os.path.getsize(path) > 200


def docx_with_image(path: str, media_name: str, media_bytes: bytes) -> None:
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    REL = "http://schemas.openxmlformats.org/package/2006/relationships"
    doc = (f'<w:document xmlns:w="{W}" xmlns:a="{A}" xmlns:r="{R}"><w:body>'
           f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Claim posting</w:t></w:r></w:p>'
           f'<w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p></w:body></w:document>')
    rels = f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Target="media/{media_name}"/></Relationships>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", rels)
        z.writestr(f"word/media/{media_name}", media_bytes)


class Wiring(unittest.TestCase):

    def test_metafiles_and_multi_page_scans_are_on_the_list_to_convert(self):
        for ext in (".emf", ".wmf", ".tif", ".tiff"):
            self.assertIn(ext, ocr.CONVERT_EXT, ext)
            self.assertIn(ext, ocr.IMAGE_EXT, "and they must be extracted in the first place")
        for ext in (".png", ".jpg"):
            self.assertNotIn(ext, ocr.CONVERT_EXT, "what the engine reads already is not converted")
        self.assertEqual(ocr.to_readable_images([]), {})
        self.assertEqual(ocr.to_readable_images(["a.png", "b.jpg"]), {}, "nothing to do, no PowerShell started")


class RunPowerShell(unittest.TestCase):
    """The runner behind OCR, PDF pages, metafiles and recordings."""

    def setUp(self):
        ok, why = ocr.ocr_available()
        if not ok:
            self.skipTest(why)

    def test_a_silent_script_is_killed_when_its_time_is_up(self):
        import time
        t0 = time.time()
        rc, out, err = ocr._run_ps("[Console]::Out.WriteLine('{\"a\":1}'); Start-Sleep -Seconds 60", [], timeout=3)
        self.assertEqual(rc, 124, (out, err))
        self.assertIn("timed out after 3s", err)
        self.assertIn('{"a":1}', out, "what it printed before the limit is kept")
        self.assertLess(time.time() - t0, 30, "killed at the limit, not after the sleep")

    def test_a_script_that_floods_the_error_stream_still_finishes(self):
        script = ("foreach ($i in 1..3000) { [Console]::Error.WriteLine('error record number ' + $i + ' ' + ('x' * 60)) }\n"
                  "[Console]::Out.WriteLine('{\"done\":true}')")
        rc, out, err = ocr._run_ps(script, [], timeout=120)
        self.assertEqual(rc, 0, err[:300])
        self.assertIn('{"done":true}', out)
        self.assertIn("error record number 3000", err, "the errors are returned as the 'noise' string")

    def test_a_script_that_dies_before_its_first_result_is_reported(self):
        rc, out, err = ocr._run_ps("[Console]::Error.WriteLine('policy says no'); exit 7", [], timeout=60)
        self.assertEqual(rc, 7)
        self.assertIn("policy says no", err)
        with mock.patch.object(ocr, "_run_ps", return_value=(7, "policy says no\n", "policy says no")):
            texts, warnings = ocr.ocr_images([os.path.join(HERE, "fixtures", "SAMPPGM.cbl")])
        self.assertEqual(texts, {})
        self.assertEqual(warnings, ["powershell rc 7: policy says no"], "the reason reaches the caller")


BIG_PAGES = r"""
param([string]$Dir)
Add-Type -AssemblyName System.Drawing
function Page($name, $w, $h) {
  $bmp = New-Object System.Drawing.Bitmap $w, $h
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.Clear([System.Drawing.Color]::White)
  $font = New-Object System.Drawing.Font("Arial", 28)
  $g.DrawString("TOP LINE ALPHA CLMPOST", $font, [System.Drawing.Brushes]::Black, 40, 40)
  $g.DrawString("MIDDLE LINE BRAVO", $font, [System.Drawing.Brushes]::Black, 40, [int]($h / 2))
  $g.DrawString("BOTTOM LINE OMEGA STEP020", $font, [System.Drawing.Brushes]::Black, 40, $h - 90)
  $g.Dispose(); $bmp.Save((Join-Path $Dir $name), [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
}
Page "page-tall.png" 1200 3000
Page "page-huge.png" 4000 6000
"""


def two_page_pdf(text1: str, text2: str) -> bytes:
    """A two-page PDF, each page one line of 30-point Helvetica, valid xref included."""
    c1 = f"BT /F1 30 Tf 60 700 Td ({text1}) Tj ET".encode("latin-1")
    c2 = f"BT /F1 30 Tf 60 700 Td ({text2}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(c1)).encode() + b" >>\nstream\n" + c1 + b"\nendstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 7 0 R >>",
        b"<< /Length " + str(len(c2)).encode() + b" >>\nstream\n" + c2 + b"\nendstream",
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


def zip_with_entries(path: str, entries) -> None:
    """A zip written by hand, so the central directory can declare a size for
    an entry that stores no data - what a broken export does."""
    import struct
    out = bytearray()
    central = bytearray()
    for name, data, declared in entries:
        crc = zipfile.crc32(data) & 0xFFFFFFFF
        nb = name.encode()
        off = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0, crc, len(data), declared, len(nb), 0) + nb + data
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 0, 0, 0, crc, len(data), declared,
                               len(nb), 0, 0, 0, 0, 0, off) + nb
    cd_off = len(out)
    out += central
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(central), cd_off, 0)
    with open(path, "wb") as fh:
        fh.write(out)


class BrokenPictures(unittest.TestCase):
    """At work: 5 of 2,057 pictures 'empty file (0 bytes)' - the document
    declared a size for each and stored nothing (LESSONS 155). The zip reader
    hands back zero bytes without a word, so the toolkit must notice."""

    def setUp(self):
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_a_picture_the_document_holds_no_data_for_is_said_once_and_never_retried(self):
        docs_dir = os.path.join(self.td, "docs")
        os.makedirs(docs_dir)
        W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        doc = (f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>Scanned claims procedure</w:t></w:r></w:p>'
               f'</w:body></w:document>').encode()
        good = b"\x89PNG\r\n\x1a\n" + bytes(4000)
        zip_with_entries(os.path.join(docs_dir, "SCANPROC.docx"),
                         [("word/document.xml", doc, len(doc)),
                          ("word/media/page-001-0001.png", b"", 5000),          # declared, not stored
                          ("word/media/page-001-0002.png", good, len(good))])
        estate = os.path.join(self.td, "estate", "SRC")
        os.makedirs(estate)
        shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), estate)
        db = os.path.join(self.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([os.path.join(self.td, "estate"), "--db", db, "--rebuild", "--quiet", "--also", docs_dir])
        conn = query.connect(db)
        try:
            said = []
            key = os.path.normcase(os.path.abspath(os.path.join(self.td, "out", "SCANPROC", "page-001-0002.png")))
            with mock.patch.object(ocr, "ocr_available", return_value=(True, "test")), \
                 mock.patch.object(ocr, "ocr_images", return_value=({key: "STEP020 RESTART"}, [])) as engine:
                stats = ocr.run(conn, os.path.join(self.td, "out"), log=said.append)
            self.assertEqual((stats["images"], stats["broken"], stats["ocr_text"], stats["ocr_failed"]), (2, 1, 1, 0), said)
            self.assertEqual([os.path.basename(p) for p in engine.call_args[0][0]], ["page-001-0002.png"],
                             "the empty one never reaches the engine")
            line = [s for s in said if "holds no data inside the document" in s]
            self.assertEqual(len(line), 1, said)
            self.assertIn("page-001-0001.png", line[0])
            self.assertIn("5,000 bytes declared, none stored", line[0])
            self.assertIn("red X", line[0])
            self.assertTrue(any("(1 broken inside their documents: nothing to read)" in s for s in said), said)
            rows = dict(conn.execute("SELECT name, ocr_text FROM doc_image").fetchall())
            self.assertEqual(rows["word/media/page-001-0001.png"], "", "recorded as read and empty")
            self.assertEqual(rows["word/media/page-001-0002.png"], "STEP020 RESTART")
            # the next run: the broken one is not mentioned again, not retried, not a failure
            said = []
            with mock.patch.object(ocr, "ocr_available", return_value=(True, "test")), \
                 mock.patch.object(ocr, "ocr_images", return_value=({}, [])) as engine:
                stats = ocr.run(conn, os.path.join(self.td, "out"), log=said.append)
            self.assertEqual((stats["broken"], stats["ocr_failed"]), (0, 0), said)
            self.assertFalse(any("holds no data" in s for s in said), said)
            self.assertFalse(engine.called and engine.call_args[0][0], "nothing left for the engine")
        finally:
            conn.close()


class PagesTheEngineRefused(unittest.TestCase):
    """At work: 'page-0001.png: Exception calling "Wait" ... One or more errors
    occurred' - the wrapper hid the reason, a big page lost its small print,
    and a page that failed once was never tried again."""

    def setUp(self):
        ok, why = ocr.ocr_available()
        if not ok:
            self.skipTest(why)
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_an_empty_page_file_says_so_instead_of_one_or_more_errors_occurred(self):
        empty = os.path.join(self.td, "page-0001.png")
        open(empty, "wb").close()
        texts, warnings = ocr.ocr_images([empty])
        self.assertEqual(texts, {})
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("empty file (0 bytes)", warnings[0])
        self.assertNotIn("One or more errors", warnings[0])

    def test_a_tall_or_huge_page_is_read_whole_in_tiles(self):
        script = os.path.join(self.td, "make.ps1")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(BIG_PAGES)
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Dir", self.td],
                       capture_output=True, timeout=300)
        tall, huge = os.path.join(self.td, "page-tall.png"), os.path.join(self.td, "page-huge.png")
        if not (os.path.isfile(tall) and os.path.isfile(huge)):
            self.skipTest("System.Drawing could not draw the pages here")
        texts, warnings = ocr.ocr_images([tall, huge])
        for p in (tall, huge):
            got = (texts.get(os.path.normcase(os.path.abspath(p))) or "").upper()
            if "ALPHA" not in got and p == tall:
                self.skipTest("OCR engine unreadable here: " + "; ".join(warnings))
            for token in ("TOP LINE ALPHA CLMPOST", "MIDDLE LINE BRAVO", "BOTTOM LINE OMEGA STEP020"):
                self.assertIn(token, got, (os.path.basename(p), got, warnings))

    def test_a_page_that_failed_on_an_earlier_run_is_tried_again_and_an_empty_one_rendered_again(self):
        probe = os.path.join(self.td, "probe.png")
        if not ocr.render_text_png("WAIVER PROBE", probe):
            self.skipTest("could not render a probe image")
        texts, _w = ocr.ocr_images([probe])
        if not texts or "WAIVER" not in next(iter(texts.values())).upper():
            self.skipTest("OCR engine unreadable here")
        docs_dir = os.path.join(self.td, "specs")
        os.makedirs(docs_dir)
        with open(os.path.join(docs_dir, "TWOPAGE.pdf"), "wb") as fh:
            fh.write(two_page_pdf("WAIVER OF PREMIUM ON PAGE ONE", "RESTART FROM STEP020 ON PAGE TWO"))
        estate = os.path.join(self.td, "estate", "SRC")
        os.makedirs(estate)
        shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), estate)
        db = os.path.join(self.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([os.path.join(self.td, "estate"), "--db", db, "--rebuild", "--quiet", "--also", docs_dir])
        conn = query.connect(db)
        try:
            log = []
            out_dir = os.path.join(self.td, "out", "images")
            stats = ocr.run(conn, out_dir, member="TWOPAGE", log=log.append, pdf_pages="all")
            if any("renderer unavailable" in ln for ln in log) or stats["images"] < 2:
                self.skipTest("Windows PDF renderer not available here: " + " | ".join(log))
            self.assertEqual(stats["ocr_text"], 2, log)
            rows = conn.execute("SELECT name, extracted_path FROM doc_image WHERE name LIKE 'page-%' ORDER BY name").fetchall()
            self.assertEqual([r["name"] for r in rows], ["page-0001", "page-0002"])
            # 1. the engine refused page 2 on an earlier run: the file is fine, the text is missing
            conn.execute("UPDATE doc_image SET ocr_text=NULL WHERE name='page-0002'")
            conn.commit()
            stats = ocr.run(conn, out_dir, member="TWOPAGE", log=log.append, pdf_pages="all")
            self.assertEqual((stats["images"], stats["ocr_text"]), (1, 1), log)
            got = conn.execute("SELECT ocr_text FROM doc_image WHERE name='page-0002'").fetchone()[0]
            self.assertIn("STEP020", (got or "").upper())
            # 2. its render came out empty: rendered again, then read
            conn.execute("UPDATE doc_image SET ocr_text=NULL WHERE name='page-0002'")
            conn.commit()
            open(rows[1]["extracted_path"], "wb").close()
            stats = ocr.run(conn, out_dir, member="TWOPAGE", log=log.append, pdf_pages="all")
            self.assertTrue(any("rendering page(s) 2 again" in ln for ln in log), log)
            self.assertEqual((stats["images"], stats["ocr_text"], stats["ocr_failed"]), (1, 1, 0), log)
            self.assertGreater(os.path.getsize(rows[1]["extracted_path"]), 0)
            # 3. nothing left to do
            stats = ocr.run(conn, out_dir, member="TWOPAGE", log=log.append, pdf_pages="all")
            self.assertEqual(stats["images"], 0, log)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM doc_section WHERE ordinal>=1001").fetchone()[0], 4,
                             "an OCR section per read (the retried page is read twice in this test: twice recorded)")
        finally:
            conn.close()


class Metafiles(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.emf = os.path.join(self.td, "diagram.emf")
        if not make_emf(self.emf):
            self.skipTest("Windows imaging (System.Drawing) not available here")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_a_metafile_becomes_a_png_the_engine_can_read(self):
        made = ocr.to_readable_images([self.emf])
        key = os.path.normcase(os.path.abspath(self.emf))
        self.assertIn(key, made, "the metafile must be converted")
        png = made[key][0]
        self.assertTrue(os.path.isfile(png) and png.lower().endswith(".ocr.png"))
        self.assertGreater(os.path.getsize(png), os.path.getsize(self.emf))
        ok, why = ocr.ocr_available()
        if not ok:
            self.skipTest(why)
        texts, warnings = ocr.ocr_images([self.emf, png])
        self.assertEqual(texts.get(os.path.normcase(os.path.abspath(self.emf))), None,
                         "the engine cannot read a metafile - that is why this conversion exists")
        read = (texts.get(os.path.normcase(os.path.abspath(png))) or "").upper()
        if "CLAIM" not in read:
            self.skipTest("OCR engine unreadable here: " + "; ".join(warnings))
        self.assertIn("CLMPOST", read)

    def test_a_metafile_filed_on_its_own_is_converted_under_the_out_folder_not_beside_itself(self):
        docs_dir = os.path.join(self.td, "docs")
        os.makedirs(docs_dir)
        shutil.copy(self.emf, os.path.join(docs_dir, "flow.emf"))
        estate = os.path.join(self.td, "estate", "SRC")
        os.makedirs(estate)
        shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), estate)
        db = os.path.join(self.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([os.path.join(self.td, "estate"), "--db", db, "--rebuild", "--also", docs_dir])
        conn = query.connect(db)
        try:
            ocr.run(conn, os.path.join(self.td, "out"), log=lambda s: None)
        finally:
            conn.close()
        self.assertEqual(sorted(os.listdir(docs_dir)), ["flow.emf"], "nothing is written into his documents folder")
        made = [f for _d, _s, fs in os.walk(os.path.join(self.td, "out")) for f in fs if f.endswith(".ocr.png")]
        self.assertEqual(made, ["flow.ocr.png"], "the converted copy lives under --out, which the build never indexes")

    def test_a_diagram_in_a_word_file_is_read_end_to_end(self):
        ok, _why = ocr.ocr_available()
        probe = os.path.join(self.td, "probe.png")
        if not ok or not ocr.render_text_png("WAIVER PROBE", probe):
            self.skipTest("OCR engine or renderer not available here")
        texts, _w = ocr.ocr_images([probe])
        if not texts or "WAIVER" not in next(iter(texts.values())).upper():
            self.skipTest("OCR engine unreadable here")
        estate = os.path.join(self.td, "estate", "SRC")
        os.makedirs(estate)
        shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), estate)
        docs = os.path.join(self.td, "docs")
        os.makedirs(docs)
        with open(self.emf, "rb") as fh:
            docx_with_image(os.path.join(docs, "FLOWDOC.docx"), "image1.emf", fh.read())
        db = os.path.join(self.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([os.path.join(self.td, "estate"), "--db", db, "--rebuild", "--also", docs])
        conn = query.connect(db)
        try:
            said = []
            stats = ocr.run(conn, os.path.join(self.td, "out"), log=said.append)
            self.assertGreaterEqual(stats["images"], 1, said)
            self.assertEqual(stats["ocr_failed"], 0, said)
            row = conn.execute("SELECT i.name, i.ocr_text FROM doc_image i JOIN member m ON m.id=i.member_id "
                               "WHERE m.name='FLOWDOC'").fetchone()
            self.assertEqual(row[0], "word/media/image1.emf")
            self.assertIn("CLAIM", (row[1] or "").upper())
            sec = conn.execute("SELECT heading, text FROM doc_section WHERE ordinal>=1000").fetchone()
            self.assertIn("image: image1.emf", sec[0])
            self.assertIn("CLMPOST", sec[1].upper())
            # and it is searchable and citable like any other section
            self.assertIn("FLOWDOC", query.cmd_docs(conn, "CLMPOST"))
        finally:
            conn.close()
        self.assertTrue(any("converting 1 diagram" in s for s in said), said)


if __name__ == "__main__":
    unittest.main()
