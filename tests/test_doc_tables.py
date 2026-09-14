"""
Spreadsheets and tables as the model reads them (LESSONS 136): a workbook's
tab is a section whose text is its rows (`row 12: TC-GEN-01 | ... | PASS`),
searched and cited section-precisely; the screenshots pasted on a tab, under
a Word heading or on a slide are anchored to that place, and the OCR section
heading says so; `doc --list` shows the indexed documents.
"""

import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, docs, ocr, query, verify_citations  # noqa: E402

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _cell(ref, text):
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _sheet(rows):
    body = "".join(f'<row r="{n}">' + "".join(_cell(f"{chr(65 + i)}{n}", v) for i, v in enumerate(vals) if v) + "</row>"
                   for n, vals in rows)
    return f'<worksheet xmlns="{S}" xmlns:r="{R}"><sheetData>{body}</sheetData><drawing r:id="rId1"/></worksheet>'


def write_workbook(path, big_rows=0):
    wb = (f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Summary" sheetId="1" r:id="rId1"/>'
          f'<sheet name="Gender Tests" sheetId="2" r:id="rId2"/></sheets></workbook>')
    wb_rels = (f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
               f'<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
    summary = [(1, ["Release", "REL12"]), (2, ["Tester", "QA team"]), (4, ["Result", "All passed"])]
    tests = [(1, ["Test ID", "Description", "Expected", "Actual", "Result"]),
             (2, ["TC-GEN-01", "Add gender N", "Accepted", "Accepted", "PASS"]),
             (3, ["TC-GEN-02", "Gender X rejected", "INVALID GENDER", "INVALID GENDER", "PASS"]),
             (5, ["TC-REL-07", "Son with gender F", "MISMATCH", "MISMATCH", "PASS"])]
    if big_rows:
        tests = [(i + 1, [f"TC-BIG-{i:05d}", "x" * 60, "PASS"]) for i in range(big_rows)]
    sheet2_rels = f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Target="../drawings/drawing1.xml"/></Relationships>'
    drawing = f'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"/>'
    drawing_rels = (f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Target="../media/image1.png"/>'
                    f'<Relationship Id="rId2" Target="../media/image2.png"/></Relationships>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", _sheet(summary))
        z.writestr("xl/worksheets/sheet2.xml", _sheet(tests))
        z.writestr("xl/worksheets/_rels/sheet2.xml.rels", sheet2_rels)
        z.writestr("xl/drawings/drawing1.xml", drawing)
        z.writestr("xl/drawings/_rels/drawing1.xml.rels", drawing_rels)
        z.writestr("xl/media/image1.png", b"\x89PNG" + b"\0" * 300)
        z.writestr("xl/media/image2.png", b"\x89PNG" + b"\0" * 300)


def write_testplan(path):
    doc = f"""<w:document xmlns:w="{W}" xmlns:a="{A}" xmlns:r="{R}"><w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Gender screen</w:t></w:r></w:p>
    <w:p><w:r><w:t>The screen accepts the new value.</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Field</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Len</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>POL-NO</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>12</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    <w:p><w:r><w:drawing><a:blip r:embed="rId7"/></w:drawing></w:r></w:p>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Relationship screen</w:t></w:r></w:p>
    <w:p><w:r><w:t>Unchanged.</w:t></w:r></w:p>
    </w:body></w:document>"""
    rels = f'<Relationships xmlns="{REL}"><Relationship Id="rId7" Target="media/image1.png"/></Relationships>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", rels)
        z.writestr("word/media/image1.png", b"\x89PNG" + b"\0" * 300)


def write_awkward_workbook(path):
    """Rows without r attributes, a formatting-only row, a shared string with
    CR LF, one screenshot pasted on two tabs."""
    wb = (f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="First" sheetId="1" r:id="rId1"/>'
          f'<sheet name="Second" sheetId="2" r:id="rId2"/></sheets></workbook>')
    wb_rels = (f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
               f'<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
    ss = f'<sst xmlns="{S}"><si><t>line one&#13;&#10;line two</t></si></sst>'
    sheet1 = (f'<worksheet xmlns="{S}" xmlns:r="{R}"><sheetData>'
              f'<row><c t="inlineStr"><is><t>first</t></is></c></row><row ht="15"/>'
              f'<row><c t="inlineStr"><is><t>third</t></is></c></row>'
              f'<row r="10"><c r="A10" t="s"><v>0</v></c><c r="B10" t="inlineStr"><is><t>PASS</t></is></c></row>'
              f'<row><c t="inlineStr"><is><t>eleven</t></is></c></row>'
              f'</sheetData><drawing r:id="rId1"/></worksheet>')
    sheet2 = f'<worksheet xmlns="{S}" xmlns:r="{R}"><sheetData/><drawing r:id="rId1"/></worksheet>'
    rels1 = f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Target="../drawings/drawing1.xml"/></Relationships>'
    rels2 = f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Target="../drawings/drawing2.xml"/></Relationships>'
    d1 = f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Target="../media/image1.png"/></Relationships>'
    d2 = f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Target="/xl/media/image1.png"/></Relationships>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/sharedStrings.xml", ss)
        z.writestr("xl/worksheets/sheet1.xml", sheet1)
        z.writestr("xl/worksheets/sheet2.xml", sheet2)
        z.writestr("xl/worksheets/_rels/sheet1.xml.rels", rels1)
        z.writestr("xl/worksheets/_rels/sheet2.xml.rels", rels2)
        z.writestr("xl/drawings/drawing1.xml", "<x/>")
        z.writestr("xl/drawings/drawing2.xml", "<x/>")
        z.writestr("xl/drawings/_rels/drawing1.xml.rels", d1)
        z.writestr("xl/drawings/_rels/drawing2.xml.rels", d2)
        z.writestr("xl/media/image1.png", b"\x89PNG" + b"\0" * 300)


def write_cover_docx(path):
    """A cover page in a content control (w:sdt), a picture in a heading
    paragraph, a picture written twice (Choice + Fallback), a picture before
    any heading."""
    MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    V = "urn:schemas-microsoft-com:vml"
    doc = f"""<w:document xmlns:w="{W}" xmlns:a="{A}" xmlns:r="{R}" xmlns:mc="{MC}" xmlns:v="{V}"><w:body>
    <w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p>
    <w:sdt><w:sdtContent>
      <w:p><w:r><w:t>Cover Title Text</w:t></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>SDT-CELL</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    </w:sdtContent></w:sdt>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Logo heading</w:t></w:r><w:r><w:drawing><a:blip r:embed="rId2"/></w:drawing></w:r></w:p>
    <w:p><w:r><w:t>Under the logo heading.</w:t></w:r></w:p>
    <w:p><w:r><mc:AlternateContent><mc:Choice><w:drawing><a:blip r:embed="rId3"/></w:drawing></mc:Choice>
      <mc:Fallback><w:pict><v:shape><v:imagedata r:id="rId3"/></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>
    </w:body></w:document>"""
    rels = (f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Target="media/image1.png"/>'
            f'<Relationship Id="rId2" Target="media/image2.png"/><Relationship Id="rId3" Target="media/image3.png"/></Relationships>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", rels)
        for k in (1, 2, 3):
            z.writestr(f"word/media/image{k}.png", b"\x89PNG" + b"\0" * 300)


def write_deck(path):
    slide = (f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
             f'<p:txBody><a:p><a:r><a:t>Walkthrough</a:t></a:r></a:p></p:txBody></p:sp><p:pic/></p:spTree></p:cSld></p:sld>')
    rels = f'<Relationships xmlns="{REL}"><Relationship Id="rId2" Target="../media/image1.png"/></Relationships>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ppt/slides/slide1.xml", slide)
        z.writestr("ppt/slides/_rels/slide1.xml.rels", rels)
        z.writestr("ppt/media/image1.png", b"\x89PNG" + b"\0" * 300)


class DocTables(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate", "SRC")
        os.makedirs(root)
        shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), root)
        cls.qa = os.path.join(cls.td, "docs", "QA")
        os.makedirs(cls.qa)
        write_workbook(os.path.join(cls.qa, "QAREL12.xlsx"))
        write_testplan(os.path.join(cls.qa, "TESTPLAN.docx"))
        write_deck(os.path.join(cls.qa, "DECK.pptx"))
        cls.db = os.path.join(cls.td, "t.db")
        build._main([os.path.join(cls.td, "estate"), "--db", cls.db, "--rebuild", "--quiet", "--also", os.path.join(cls.td, "docs")])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_a_tab_is_its_rows(self):
        outline = query.cmd_doc(self.conn, "QAREL12")
        self.assertIn("| 1 | sheet: Summary |", outline)
        self.assertIn("| 2 | sheet: Gender Tests |", outline)
        self.assertIn(r"row 1: Test ID \| Description \| Expected \| Actual \| Result", outline)   # table cells escape |
        full = query.cmd_doc(self.conn, "QAREL12", sections="2")
        self.assertIn("row 2: TC-GEN-01 | Add gender N | Accepted | Accepted | PASS", full)
        self.assertIn("row 5: TC-REL-07 | Son with gender F | MISMATCH | MISMATCH | PASS", full)   # the sheet's own row number
        self.assertNotIn("row 4:", full)                                                          # an empty row is not a row
        self.assertNotIn("[3 rows]", full)

    def test_rows_are_found_and_cited_by_section(self):
        found = query.cmd_docs(self.conn, "TC-GEN-01")
        self.assertIn("QAREL12:2", found)
        self.assertIn("section 2", found)
        self.assertIn(r"| row 2: TC-GEN-01 \| Add gender N \| Accepted \| Accepted \| PASS |", found)   # the ROW, not the tab's first line
        self.assertIn(r"row 5: TC-REL-07 \| Son with gender F", query.cmd_docs(self.conn, "TC-REL-07"))
        self.assertNotIn("| table |", found)
        res, _u = verify_citations.check_answer('[[QAREL12 2 "TC-GEN-01"]]', db_path=self.db)
        self.assertEqual([r.status for r in res], ["PASS"], res)
        res, _u = verify_citations.check_answer('[[QAREL12 1 "TC-GEN-01"]]', db_path=self.db)
        self.assertEqual([r.status for r in res], ["FAIL"], res)
        grep = query.cmd_doc(self.conn, "QAREL12", grep="INVALID GENDER")
        self.assertIn("### QAREL12 section 2 - sheet: Gender Tests", grep)

    def test_screenshots_are_anchored_to_their_tab_heading_or_slide(self):
        rows = self.conn.execute("SELECT m.name, i.name, i.anchor FROM doc_image i JOIN member m ON m.id=i.member_id "
                                 "ORDER BY m.name, i.name").fetchall()
        self.assertEqual([tuple(r) for r in rows],
                         [("DECK", "ppt/media/image1.png", "slide 1: Walkthrough"),
                          ("QAREL12", "xl/media/image1.png", "sheet: Gender Tests"),
                          ("QAREL12", "xl/media/image2.png", "sheet: Gender Tests"),
                          ("TESTPLAN", "word/media/image1.png", "Gender screen")])
        outline = query.cmd_doc(self.conn, "QAREL12")
        self.assertIn("## Pictures (screenshots) and where they sit", outline)
        self.assertIn("- **sheet: Gender Tests**: image1.png, image2.png", outline)
        self.assertEqual(ocr.image_heading("xl/media/image1.png", "sheet: Gender Tests"), "image: image1.png (sheet: Gender Tests)")
        self.assertEqual(ocr.image_heading("page-0003", "x"), "page 3 (OCR of the scanned page)")
        self.assertIn("| xl/media/image1.png | sheet: Gender Tests |", query.cmd_images(self.conn, "QAREL12"))

    def test_word_table_rows_and_picture_place(self):
        d = docs.extract(os.path.join(self.qa, "TESTPLAN.docx"))
        self.assertEqual(d.sections[0][0], "Gender screen")
        self.assertIn("[table 2x2]\nrow 1: Field | Len\nrow 2: POL-NO | 12", d.sections[0][1])
        self.assertIn("[image: image1.png]", d.sections[0][1])
        self.assertEqual(d.tables[0][1], ["POL-NO", "12"])
        self.assertIn("TESTPLAN:1", query.cmd_docs(self.conn, "POL-NO"))
        deck = docs.extract(os.path.join(self.qa, "DECK.pptx"))
        self.assertIn("[image: image1.png]", deck.sections[0][1])

    def test_doc_list(self):
        t = query.cmd_doc_list(self.conn)
        self.assertIn("| QAREL12 | QAREL12.xlsx | QA | 2 | 2 | 0 |", t)
        self.assertIn("| TESTPLAN | TESTPLAN.docx | QA | 2 | 1 | 0 |", t)
        self.assertIn("3 document(s)", t)
        self.assertIn("| QAREL12 |", query.cmd_doc_list(self.conn, "qa"))
        self.assertIn("_none_ match `nope`", query.cmd_doc_list(self.conn, "nope"))
        import contextlib
        import io
        for argv in (["doc", "--list"], ["doc", "--list", "QA"], ["doc"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                query._main(["--db", self.db, *argv])
            self.assertIn("# Documents in the index", buf.getvalue(), argv)

    def test_awkward_workbook(self):
        p = os.path.join(self.td, "AWKWARD.xlsx")
        write_awkward_workbook(p)
        d = docs.extract(p)
        first = dict(d.sections)["sheet: First"]
        # rows without r follow the previous row; a formatting-only row still counts; r=10 resets
        self.assertEqual(first.split("\n"), ["row 1: first", "row 3: third", "row 10: line one line two | PASS", "row 11: eleven"])
        self.assertEqual(d.image_anchor, {"xl/media/image1.png": "sheet: First; sheet: Second"})   # pasted on both tabs
        self.assertEqual(ocr.image_heading("xl/media/image1.png", d.image_anchor["xl/media/image1.png"]),
                         "image: image1.png (sheet: First; sheet: Second)")

    def test_cover_page_and_heading_pictures(self):
        p = os.path.join(self.td, "COVER.docx")
        write_cover_docx(p)
        d = docs.extract(p)
        heads = [h for h, _t in d.sections]
        self.assertEqual(heads, ["", "Logo heading"])
        self.assertIn("Cover Title Text", d.sections[0][1])                       # content control text is indexed
        self.assertIn("row 1: SDT-CELL", d.sections[0][1])
        self.assertIn("[image: image1.png]", d.sections[0][1])
        self.assertEqual(d.image_anchor["word/media/image1.png"], "before the first heading")
        self.assertEqual(ocr.image_heading("word/media/image1.png", "before the first heading"),
                         "image: image1.png (before the first heading)")            # no double parentheses
        self.assertEqual(d.image_anchor["word/media/image2.png"], "Logo heading")  # the heading it sits in
        self.assertIn("[image: image2.png]", d.sections[1][1])
        self.assertIn("[image: image3.png]", d.sections[1][1])                    # once, not "image3.png, image3.png"
        self.assertNotIn("image3.png, image3.png", d.sections[1][1])

    def test_gate_accepts_a_token_copied_from_a_report_table(self):
        # report tables escape | as \| ; a token copied from there must still verify
        res, _u = verify_citations.check_answer(r'[[QAREL12 2 "TC-GEN-01 \| Add gender N"]]', db_path=self.db)
        self.assertEqual([r.status for r in res], ["PASS"], res)

    def test_chunker_prefers_a_short_part_to_a_split_row(self):
        text = "row 1: " + "a" * 1900 + "\nrow 2: " + "b" * 2500 + "\nrow 3: " + "c" * 100
        parts = docs.chunk_sections([("sheet: Long", text)], max_chars=4000)
        self.assertEqual([t.split("\n")[0][:6] for _h, t in parts], ["row 1:", "row 2:"])
        self.assertTrue(all(ln.startswith("row ") for _h, t in parts for ln in t.split("\n")))
        short = "\n".join(f"row {i}: TC-{i:03d} | short | PASS" for i in range(1, 16))
        longrow = "row 16: TC-016 | " + "y" * 3680 + " | PASS"
        tail = "\n".join(f"row {i}: TC-{i:03d} | short | PASS" for i in range(17, 40))
        parts = docs.chunk_sections([("sheet: E", short + "\n" + longrow + "\n" + tail)])
        self.assertEqual(len(parts), 3)
        self.assertTrue(any(longrow in t.split("\n") for _h, t in parts), "row 16 must be whole inside one part")
        self.assertTrue(all(ln.startswith("row ") for _h, t in parts for ln in t.split("\n")))

    def test_scanned_page_is_linked_to_its_ocr_section(self):
        mid = self.conn.execute("SELECT id FROM member WHERE name='DECK'").fetchone()[0]
        self.conn.execute("INSERT INTO doc_image(member_id,name,ocr_text) VALUES(?,?,?)", (mid, "page-0002", "scanned words"))
        self.conn.execute("INSERT INTO doc_section(member_id,heading,text,ordinal) VALUES(?,?,?,?)",
                          (mid, ocr.image_heading("page-0002", None), "scanned words", 1001))
        try:
            self.assertIn("page-0002 = section 1001", query.cmd_doc(self.conn, "DECK"))
        finally:
            self.conn.execute("DELETE FROM doc_image WHERE member_id=? AND name='page-0002'", (mid,))
            self.conn.execute("DELETE FROM doc_section WHERE member_id=? AND ordinal=1001", (mid,))

    def test_older_index_without_anchor_column_does_not_crash(self):
        import sqlite3
        p = os.path.join(self.td, "old.db")
        shutil.copy(self.db, p)
        c = sqlite3.connect(p)
        c.execute("ALTER TABLE doc_image RENAME TO doc_image_old")
        c.execute("CREATE TABLE doc_image(id INTEGER PRIMARY KEY, member_id INTEGER, name TEXT, extracted_path TEXT, ocr_text TEXT)")
        c.execute("INSERT INTO doc_image(id,member_id,name,extracted_path,ocr_text) SELECT id,member_id,name,extracted_path,ocr_text FROM doc_image_old")
        c.commit()
        c.close()
        old = query.connect(p)
        try:
            self.assertIn("built by an older toolkit", query.cmd_doc(old, "QAREL12"))
            self.assertIn("built by an older toolkit", query.cmd_images(old, "QAREL12"))
        finally:
            old.close()

    def test_a_huge_single_paragraph_chunks_in_linear_time(self):
        import time
        rows = "\n".join(f"row {i}: TC-{i:07d} | {'x' * 40} | PASS" for i in range(1, 400001))    # ~24 MB, one paragraph
        t0 = time.time()
        parts = docs.chunk_sections([("sheet: Huge", rows)])
        took = time.time() - t0
        self.assertLess(took, 8, f"chunking 24 MB took {took:.1f} s")
        self.assertTrue(all(len(t) <= 4000 for _h, t in parts))
        self.assertTrue(all(ln.startswith("row ") for _h, t in parts for ln in t.split("\n")))
        self.assertEqual(sum(t.count("\n") + 1 for _h, t in parts), 400000)                   # every row, once
        self.assertEqual("\n".join(t for _h, t in parts), rows)                                # nothing lost or altered

    def test_big_sheet_is_cut_between_rows(self):
        p = os.path.join(self.td, "BIG.xlsx")
        write_workbook(p, big_rows=400)
        d = docs.extract(p)
        parts = docs.chunk_sections(d.sections)
        big = [t for h, t in parts if h.startswith("sheet: Gender Tests")]
        self.assertGreater(len(big), 3)
        for t in big:
            self.assertLessEqual(len(t), 4000)
            self.assertTrue(all(ln.startswith("row ") for ln in t.split("\n")), t[:120])   # never cut inside a row
        self.assertEqual(sum(t.count("\n") + 1 for t in big), 400)


if __name__ == "__main__":
    unittest.main()
