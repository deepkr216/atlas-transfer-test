"""
Legacy Office files (.doc / .xls / .ppt): `atlas.convert` plans the whole
tree (subfolders included), skips files that already have a modern copy,
parses the PowerShell results, never deletes anything; the build indexes
the converted copy and skips the legacy original beside it.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, convert, query  # noqa: E402


class Convert(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.docs = os.path.join(self.td, "specs")
        os.makedirs(os.path.join(self.docs, "sub", "deeper"))
        for rel in ("Claims Manual.doc", "rates.xls", "sub/Flow.ppt", "sub/deeper/OLD.DOC", "sub/notes.md",
                    "~$Claims Manual.doc"):
            with open(os.path.join(self.docs, rel), "w") as fh:
                fh.write("x")
        # one already converted: a (minimal but whole) modern copy beside the legacy file
        import zipfile
        with zipfile.ZipFile(os.path.join(self.docs, "rates.xlsx"), "w") as z:
            z.writestr("xl/workbook.xml", "<workbook/>")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_plan_walks_subfolders_and_skips_done_and_temp_files(self):
        items = convert.plan(self.docs)
        by_src = {os.path.relpath(s, self.docs).replace("\\", "/"): (os.path.basename(d), st) for s, d, st in items}
        self.assertEqual(by_src, {
            "Claims Manual.doc": ("Claims Manual.docx", "convert"),
            "rates.xls": ("rates.xlsx", "exists"),
            "sub/Flow.ppt": ("Flow.pptx", "convert"),
            "sub/deeper/OLD.DOC": ("OLD.docx", "convert"),
        })

    def test_parse_output(self):
        rows = convert.parse_output("noise\nOK\tC:\\a\\b.doc\tC:\\a\\b.docx\r\nFAIL\tC:\\a\\c.xls\tpassword protected\n")
        self.assertEqual(rows, [("OK", "C:\\a\\b.doc", "C:\\a\\b.docx"), ("FAIL", "C:\\a\\c.xls", "password protected")])

    def test_convert_tree_reports_and_never_deletes(self):
        lines = []
        with mock.patch("atlas.convert._run_powershell") as ps:
            def fake(pairs, timeout, log=None):
                out = []
                for src, dst in pairs:
                    if src.endswith("OLD.DOC"):
                        out.append(f"FAIL\t{src}\tThe file is locked for editing")
                    else:
                        with open(dst, "w") as fh:
                            fh.write("converted")
                        out.append(f"OK\t{src}\t{dst}")
                return 0, "\n".join(out) + "\n", ""
            ps.side_effect = fake
            ok, have, fail = convert.convert_tree(self.docs, dry_run=False, log=lines.append)
        self.assertEqual((ok, have, fail), (2, 1, 1))
        self.assertTrue(os.path.exists(os.path.join(self.docs, "Claims Manual.docx")))
        self.assertTrue(os.path.exists(os.path.join(self.docs, "Claims Manual.doc")))       # original kept
        self.assertTrue(os.path.exists(os.path.join(self.docs, "sub", "Flow.pptx")))
        self.assertIn("FAIL", "\n".join(lines))
        self.assertIn("locked for editing", "\n".join(lines))
        # dry run: plan only, nothing written
        lines.clear()
        with mock.patch("atlas.convert._run_powershell") as ps:
            convert.convert_tree(self.docs, dry_run=True, log=lines.append)
            ps.assert_not_called()
        self.assertIn("todo", "\n".join(lines))

    def test_office_missing_is_said_plainly(self):
        lines = []
        with mock.patch("atlas.convert._run_powershell", return_value=(1, "", "New-Object : Retrieving the COM class factory ... 80040154")), \
                mock.patch("shutil.which", return_value=None):
            ok, have, fail = convert.convert_tree(self.docs, log=lines.append)
        self.assertEqual(ok, 0)
        self.assertEqual(fail, 3)
        self.assertIn("Is Microsoft Office installed", "\n".join(lines))

    def test_build_indexes_the_converted_copy_and_skips_the_legacy_original(self):
        est = os.path.join(self.td, "estate", "SRC")
        os.makedirs(est)
        shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), est)
        db = os.path.join(self.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([os.path.join(self.td, "estate"), "--db", db, "--rebuild", "--quiet", "--also", self.docs])
        conn = query.connect(db)
        names = {os.path.basename(r[0]).lower() for r in conn.execute("SELECT path FROM member")}
        conn.close()
        self.assertIn("rates.xlsx", names)
        self.assertNotIn("rates.xls", names)                 # the legacy original beside its copy is skipped
        self.assertIn("claims manual.doc", names)            # no copy yet: still listed (as legacy, unreadable)

    def test_both_versions_present(self):
        """A .doc beside its .docx: the copy is used and the legacy file left
        alone - unless the .doc changed after the copy was made (STALE):
        then it is said, and --refresh remakes the copy."""
        doc = os.path.join(self.docs, "rates.xls")
        docx = os.path.join(self.docs, "rates.xlsx")
        old, new = 1_600_000_000, 1_700_000_000
        os.utime(doc, (old, old))
        os.utime(docx, (new, new))                         # copy made after the legacy file: current
        self.assertEqual([st for s, _d, st in convert.plan(self.docs) if s == doc], ["exists"])
        os.utime(doc, (new + 3600, new + 3600))            # the legacy file edited later: copy is stale
        self.assertEqual([st for s, _d, st in convert.plan(self.docs) if s == doc], ["stale"])
        lines = []
        with mock.patch("atlas.convert._run_powershell", return_value=(0, "", "")) as ps:
            convert.convert_tree(self.docs, dry_run=True, log=lines.append)
            ps.assert_not_called()
        self.assertIn("1 copies STALE", "\n".join(lines))
        self.assertIn("STALE " + doc, "\n".join(lines))
        with mock.patch("atlas.convert._run_powershell") as ps:
            ps.side_effect = lambda pairs, timeout, log=None: (0, "".join(f"OK\t{s}\t{d}\n" for s, d in pairs), "")
            convert.convert_tree(self.docs, log=lines.append, refresh=True)
            remade = [d for s, d in ps.call_args[0][0]]
        self.assertIn(docx, remade)                          # --refresh remakes the stale copy
        # the build indexes the copy, and says it is stale
        est = os.path.join(self.td, "estate", "SRC")
        os.makedirs(est)
        shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), est)
        db = os.path.join(self.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([os.path.join(self.td, "estate"), "--db", db, "--rebuild", "--quiet", "--also", self.docs])
        conn = query.connect(db)
        row = conn.execute("SELECT parse_status, parse_error FROM member WHERE path LIKE '%rates.xlsx'").fetchone()
        conn.close()
        self.assertEqual(row[0], "partial")
        self.assertIn("STALE copy: rates.xls changed after this conversion", row[1])

    def test_zip_archives_are_extracted_beside_them_nested_and_safely(self):
        import zipfile
        inner = os.path.join(self.td, "inner.zip")
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("deep/Policy Rules.md", "# Rules\n\nThe waiver applies.\n")
            z.writestr("deep/old.doc", "x")
        outer = os.path.join(self.docs, "sub", "Archive 2019.zip")
        with zipfile.ZipFile(outer, "w") as z:
            z.write(inner, "inner.zip")
            z.writestr("readme.txt", "hello")
            z.writestr("../escape.txt", "must not be written")          # zip-slip entry
        lines = []
        done, have, failed = convert.unzip_tree(self.docs, log=lines.append)
        self.assertEqual((done, have, failed), (2, 0, 0))                # outer, then the zip inside it
        top = os.path.join(self.docs, "sub", "Archive 2019.unzipped")
        self.assertTrue(os.path.exists(os.path.join(top, "readme.txt")))
        self.assertTrue(os.path.exists(os.path.join(top, "inner.unzipped", "deep", "Policy Rules.md")))
        self.assertFalse(os.path.exists(os.path.join(self.docs, "sub", "escape.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.docs, "escape.txt")))
        self.assertIn("entry outside the folder ignored", "\n".join(lines))
        self.assertTrue(os.path.exists(outer))                             # nothing deleted
        # second run: up to date, nothing re-extracted
        done, have, failed = convert.unzip_tree(self.docs, log=lines.append)
        self.assertEqual((done, have, failed), (0, 2, 0))
        # the archive changed: extracted again
        with zipfile.ZipFile(outer, "a") as z:
            z.writestr("added.txt", "new")
        done, have, failed = convert.unzip_tree(self.docs, log=lines.append)
        self.assertEqual(done, 1)
        self.assertTrue(os.path.exists(os.path.join(top, "added.txt")))
        # the legacy file inside the archive is now on the conversion plan
        self.assertIn("old.doc", {os.path.basename(s) for s, _d, _st in convert.plan(self.docs)})
        # the build indexes the extracted documents and not the archives
        est = os.path.join(self.td, "estate", "SRC")
        os.makedirs(est)
        shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), est)
        db = os.path.join(self.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([os.path.join(self.td, "estate"), "--db", db, "--rebuild", "--quiet", "--also", self.docs])
        conn = query.connect(db)
        names = {os.path.basename(r[0]).lower() for r in conn.execute("SELECT path FROM member")}
        conn.close()
        self.assertIn("policy rules.md", names)
        self.assertIn("readme.txt", names)
        self.assertNotIn("archive 2019.zip", names)
        self.assertNotIn("inner.zip", names)
        # dry run touches nothing
        os.remove(os.path.join(top, "added.txt"))
        with zipfile.ZipFile(outer, "a") as z:
            z.writestr("again.txt", "x")
        convert.unzip_tree(self.docs, log=lines.append, dry_run=True)
        self.assertFalse(os.path.exists(os.path.join(top, "again.txt")))

    def test_a_half_written_copy_is_made_again(self):
        """Ctrl+C during a save leaves a truncated .docx beside the .doc: the
        next run must treat it as not converted, not as done."""
        import zipfile
        doc = os.path.join(self.docs, "Claims Manual.doc")
        docx = os.path.join(self.docs, "Claims Manual.docx")
        with open(docx, "wb") as fh:
            fh.write(b"PK\x03\x04 truncated in the middle of a save")
        self.assertFalse(convert.modern_copy_is_whole(docx))
        self.assertEqual([st for s, _d, st in convert.plan(self.docs) if s == doc], ["convert"])
        # a whole one is left alone
        with zipfile.ZipFile(docx, "w") as z:
            z.writestr("word/document.xml", "<w:document/>")
        self.assertTrue(convert.modern_copy_is_whole(docx))
        os.utime(doc, (1_600_000_000, 1_600_000_000))
        self.assertEqual([st for s, _d, st in convert.plan(self.docs) if s == doc], ["exists"])

    def test_cli(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = convert.main([self.docs, "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("3 to convert, 1 already converted earlier", buf.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(convert.main([os.path.join(self.td, "nope")]), 2)


if __name__ == "__main__":
    unittest.main()
