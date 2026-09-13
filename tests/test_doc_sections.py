"""
Documents the size of a book (LESSONS 129): a heading-less specification is
cut into sections of about 4,000 characters at paragraph boundaries, each
citable; `doc NAME` prints the outline, `--sections` / `--grep` the full
text inside a budget; `docs TERM` points at the right part; the gate is
section-precise.
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, docs, query, verify_citations  # noqa: E402

FILLER = "General text about policy administration and the handling of routine correspondence. "
RULE = "The waiver of premium applies when the insured is totally disabled for six months. "


def spec_text():
    paras = ["Premium Waiver Rules"]
    for i in range(1, 25):
        paras.append(f"Paragraph {i}. " + (RULE * 6 if i == 15 else FILLER * 5))
    return "\n\n".join(paras) + "\n"


class DocSections(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate", "SRC")
        os.makedirs(root)
        shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), root)
        cls.docdir = os.path.join(cls.td, "specs")
        os.makedirs(cls.docdir)
        with open(os.path.join(cls.docdir, "RULES.txt"), "w", encoding="utf-8") as fh:
            fh.write(spec_text())
        with open(os.path.join(cls.docdir, "SHORT.md"), "w", encoding="utf-8") as fh:
            fh.write("# Short note\n\nOne paragraph only.\n")
        cls.db = os.path.join(cls.td, "t.db")
        build._main([os.path.join(cls.td, "estate"), "--db", cls.db, "--rebuild", "--quiet", "--also", cls.docdir])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_chunker(self):
        long = "\n\n".join(f"P{i} " + "x" * 900 for i in range(10))     # ~9k chars, 10 paragraphs
        parts = docs.chunk_sections([("Spec", long), ("Tiny", "a b c")], max_chars=4000)
        self.assertEqual([h for h, _t in parts][-1], "Tiny")
        heads = [h for h, _t in parts[:-1]]
        self.assertEqual(heads, [f"Spec (part {k}/{len(heads)})" for k in range(1, len(heads) + 1)])
        self.assertTrue(all(len(t) <= 4000 for _h, t in parts))
        self.assertEqual("".join(t.replace("\n\n", "") for _h, t in parts[:-1]), long.replace("\n\n", ""))
        # one paragraph bigger than the limit is cut at a sentence end
        huge = ("Sentence one is here. " * 300).strip()
        cut = docs.chunk_sections([("", huge)], max_chars=1000)
        self.assertGreater(len(cut), 1)
        self.assertTrue(all(len(t) <= 1000 for _h, t in cut))
        self.assertTrue(cut[0][1].endswith("."))

    def test_long_document_is_several_citable_sections(self):
        n = self.conn.execute("SELECT COUNT(*) FROM doc_section WHERE member_id=(SELECT id FROM member WHERE name='RULES')").fetchone()[0]
        self.assertGreaterEqual(n, 3, "the specification must be cut into parts")
        outline = query.cmd_doc(self.conn, "RULES")
        self.assertIn(f"- {n} sections,", outline)
        self.assertIn("| 1 | part 1/", outline)
        self.assertIn("read one: `doc RULES --sections 3`", outline)

    def test_grep_and_sections_print_the_full_text(self):
        hit = query.cmd_doc(self.conn, "RULES", grep="waiver of premium")
        self.assertIn("section(s) containing `waiver of premium`", hit)
        self.assertIn(RULE.strip(), hit)
        sec = int(hit.split("### RULES section ", 1)[1].split(" ", 1)[0].rstrip("-").strip())
        self.assertGreater(sec, 1, "paragraph 15 is not in the first part")
        by_number = query.cmd_doc(self.conn, "RULES", sections=str(sec))
        self.assertIn(RULE.strip(), by_number)
        self.assertNotIn("Paragraph 1. ", by_number)                     # only that section
        # the gate is section-precise
        res, _u = verify_citations.check_answer(f'[[RULES {sec} "waiver of premium applies"]]', db_path=self.db)
        self.assertEqual([r.status for r in res], ["PASS"], res)
        res, _u = verify_citations.check_answer('[[RULES 1 "waiver of premium applies"]]', db_path=self.db)
        self.assertEqual([r.status for r in res], ["FAIL"], res)
        # `docs` points at the right part and says how to read it
        found = query.cmd_docs(self.conn, "waiver")
        self.assertIn(f"RULES:{sec}", found)
        self.assertIn("doc DOCNAME --sections n", found)

    def test_huge_pdf_goes_to_ocr_not_the_text_extractor(self):
        from unittest import mock
        p = os.path.join(self.td, "big.pdf")
        with open(p, "wb") as fh:
            fh.write(b"%PDF-1.4 tiny")
        with mock.patch("atlas.docs.PDF_TEXT_MAX_BYTES", 5):
            d = docs.extract(p)
        self.assertFalse(d.ok)
        self.assertIn("no extractable text", d.notes[0])
        self.assertIn("too large for the text extractor", d.notes[0])

    def test_heartbeat_names_a_slow_member(self):
        import time
        said = []
        with build.Heartbeat(said.append, "doc BIG.pdf", every=0.2):
            time.sleep(0.7)
        self.assertTrue(any("still parsing doc BIG.pdf" in x for x in said), said)

    def test_budget_and_not_found(self):
        n = self.conn.execute("SELECT COUNT(*) FROM doc_section WHERE member_id=(SELECT id FROM member WHERE name='RULES')").fetchone()[0]
        t = query.cmd_doc(self.conn, "RULES", sections=f"1-{n}", budget=5000)
        self.assertLessEqual(t.count("### RULES section"), n - 1)
        self.assertIn("more section(s) not shown", t)
        self.assertIn("**NOT FOUND**", query.cmd_doc(self.conn, "NOPE"))
        self.assertIn("_no section(s) containing `unicorn`_", query.cmd_doc(self.conn, "RULES", grep="unicorn"))
        short = query.cmd_doc(self.conn, "SHORT", sections="1")
        self.assertIn("One paragraph only.", short)


if __name__ == "__main__":
    unittest.main()
