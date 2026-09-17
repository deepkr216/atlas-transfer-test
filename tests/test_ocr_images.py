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
