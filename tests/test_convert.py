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
            def fake(pairs, timeout, log=None, *rest):
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
        # the build indexes the (stale) copy, and says so
        est = os.path.join(self.td, "estate", "SRC")
        os.makedirs(est)
        shutil.copy(os.path.join(HERE, "fixtures", "SAMPPGM.cbl"), est)
        db = os.path.join(self.td, "t.db")

        def status():
            with contextlib.redirect_stdout(io.StringIO()):
                build._main([os.path.join(self.td, "estate"), "--db", db, "--rebuild", "--quiet", "--also", self.docs])
            conn = query.connect(db)
            row = conn.execute("SELECT parse_status, parse_error FROM member WHERE path LIKE '%rates.xlsx'").fetchone()
            conn.close()
            return row
        row = status()
        self.assertEqual(row[0], "partial")
        self.assertIn("STALE copy: rates.xls changed after this conversion", row[1])
        # --refresh remakes the stale copy (a whole new file lands beside the original) - and it is stale no more
        with mock.patch("atlas.convert._run_powershell") as ps:
            def remake(pairs, timeout, log=None, *rest):
                for s, d in pairs:
                    import zipfile as zf
                    with zf.ZipFile(d, "w") as z:
                        z.writestr("xl/workbook.xml", "<workbook/>")
                return 0, "".join(f"OK\t{s}\t{d}\n" for s, d in pairs), ""
            ps.side_effect = remake
            convert.convert_tree(self.docs, log=lines.append, refresh=True)
            remade = [d for s, d in ps.call_args[0][0]]
        self.assertIn(os.path.basename(docx), [os.path.basename(d) for d in remade])
        self.assertEqual([st for s, _d, st in convert.plan(self.docs) if s == doc], ["exists"])
        self.assertEqual(status()[0], "ok")

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

    def test_interrupt_closes_only_the_office_this_run_started(self):
        """PID lines from the script name the Office processes it started;
        on Ctrl+C the runner ends exactly those (never the user's own Word)
        and says how to continue."""
        killed = []
        with mock.patch("atlas.convert.subprocess.run", side_effect=lambda cmd, **kw: killed.append(cmd)):
            convert._close_office([("WINWORD", 4321), ("EXCEL", 8765)], log=lambda l: None)
        self.assertEqual(killed, [["taskkill", "/PID", "4321", "/F"], ["taskkill", "/PID", "8765", "/F"]])
        # PID lines are consumed, not mistaken for results
        self.assertEqual(convert.parse_output("PID\tWINWORD\t4321\nOK\tC:\\a\\b.doc\tC:\\a\\b.docx\n"),
                         [("OK", "C:\\a\\b.doc", "C:\\a\\b.docx")])
        # the CLI turns an interrupt into a clean exit
        with mock.patch("atlas.convert._run_folders", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(convert.main([self.docs]), 130)
        self.assertIn("interrupted", out.getvalue())

    def test_office_getters_return_one_object(self):
        """LESSONS 132: nothing inside the Get-Word / Get-Excel / Get-PPT
        functions may write to the pipeline, or the COM object comes back as
        a list and every .Open fails with 'cannot call a method on a
        null-valued expression'. Checked statically, and - where PowerShell
        exists - by running the script's own Report-NewPid inside a function."""
        import re
        script = convert._PS_SCRIPT
        for name in ("Get-Word", "Get-Excel", "Get-PPT", "Report-NewPid"):
            body = re.search(r"function " + re.escape(name) + r".*?\n}", script, re.S).group(0)
            code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))   # comments may say the word
            self.assertNotIn("Write-Output", code, name)
        if shutil.which("powershell") is None:
            self.skipTest("no powershell here")
        fn = re.search(r"function Report-NewPid.*?\n}", script, re.S).group(0)
        harness = fn + "\nfunction Get-X { Report-NewPid 'NOSUCHPROCESSNAME' @(); return 42 }\n$x = Get-X\n" \
                       "Write-Output ('array=' + ($x -is [array]) + ' value=' + $x)\n"
        path = os.path.join(self.td, "probe.ps1")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(harness)
        import subprocess
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path],
                           capture_output=True, text=True, timeout=120)
        self.assertIn("array=False value=42", p.stdout, p.stdout + p.stderr)

    def test_watchdog_skips_a_file_office_never_finishes(self):
        """Office silent on one file (an invisible dialog) must not hang the
        run: that file is failed, Office closed, the rest converted."""
        import subprocess
        fake = os.path.join(self.td, "fakeps.py")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write(
                "import sys, time\n"
                "listing = sys.argv[1]\n"
                "for line in open(listing, encoding='utf-8'):\n"
                "    src, dst = line.rstrip('\\n').split('\\t', 1)\n"
                "    print('START\\t' + src, flush=True)\n"
                "    if 'STALL' in src:\n"
                "        time.sleep(30)\n"
                "    open(dst, 'w').write('ok')\n"
                "    print('OK\\t' + src + '\\t' + dst, flush=True)\n")
        for n in ("A.doc", "M-STALL.doc", "Z.doc"):        # the stalling file in the middle of the run
            with open(os.path.join(self.docs, n), "w") as fh:
                fh.write("x")
        for n in ("Claims Manual.doc", "sub/Flow.ppt", "sub/deeper/OLD.DOC"):
            os.remove(os.path.join(self.docs, n))
        lines = []
        with mock.patch("atlas.convert._ps_command", side_effect=lambda script, listing, visible: [sys.executable, fake, listing]), \
                mock.patch("atlas.convert._close_office") as closer:
            ok, have, fail = convert.convert_tree(self.docs, log=lines.append, stall_seconds=2)
        self.assertEqual((ok, fail), (2, 1), "\n".join(lines))
        self.assertTrue(os.path.exists(os.path.join(self.docs, "A.docx")))
        self.assertTrue(os.path.exists(os.path.join(self.docs, "Z.docx")))
        self.assertFalse(os.path.exists(os.path.join(self.docs, "M-STALL.docx")))
        text = "\n".join(lines)
        self.assertIn("M-STALL.doc: Office did not finish this file in 2 s", text)
        self.assertIn("--visible", text)
        self.assertIn("restarting Office for the remaining 1 file(s)", text)
        closer.assert_called()

    def test_office_is_told_read_only_and_no_passwords_up_front(self):
        """A 'password to modify' / 'read-only recommended' file must open
        without the dialog the user answers with Read Only; a 'password to
        open' file must fail with Office's message, not prompt."""
        script = convert._PS_SCRIPT
        self.assertIn('$w.Documents.Open($p, $false, $true, $false, "", "", $false, "", ""', script)   # ReadOnly, empty passwords
        self.assertIn('$x.Workbooks.Open($p, 0, $true, $miss, "", "", $true', script)                # ReadOnly, empty passwords, IgnoreReadOnlyRecommended
        self.assertIn("$miss, $true) }", script)                                                     # NoEncodingDialog on Word
        self.assertIn("[System.Reflection.Missing]::Value", script)

    def test_a_dead_office_is_replaced_for_the_next_file(self):
        """'The object invoked has disconnected from its clients' means the
        Word/Excel the script held was killed: the script must drop it and
        start a fresh one for the next file, not fail the rest of the list."""
        script = convert._PS_SCRIPT
        self.assertIn("disconnected from its clients|RPC server is unavailable", script)
        self.assertIn("{ $_ -in '.doc', '.dot', '.rtf' } { $script:word = $null }", script)
        self.assertIn("a new one is started for the next file", script)

    def test_office_works_on_a_local_copy_and_the_result_lands_beside_the_original(self):
        """OneDrive: Office must never open the file where it lives - it is
        copied to a scratch folder, converted there, and the result moved
        beside the original; the log names the original."""
        seen = []
        lines = []
        with mock.patch("atlas.convert._run_powershell") as ps:
            def fake(pairs, timeout, log=None, *rest):
                for src, dst in pairs:
                    seen.append(src)
                    with open(dst, "w") as fh:
                        fh.write("converted")
                    if log:
                        log(f"  OK   {src}")
                return 0, "".join(f"OK\t{s}\t{d}\n" for s, d in pairs), ""
            ps.side_effect = fake
            ok, have, fail = convert.convert_tree(self.docs, log=lines.append)
        self.assertEqual((ok, fail), (3, 0), "\n".join(lines))
        self.assertTrue(all(self.docs not in s for s in seen), seen)               # Office saw scratch copies only
        self.assertTrue(all("atlas-stage-" in s for s in seen), seen)
        self.assertTrue(os.path.exists(os.path.join(self.docs, "Claims Manual.docx")))
        self.assertTrue(os.path.exists(os.path.join(self.docs, "sub", "Flow.pptx")))
        self.assertTrue(os.path.exists(os.path.join(self.docs, "sub", "deeper", "OLD.docx")))
        # the scratch folders are gone afterwards
        self.assertFalse(any("atlas-stage-" in d for d in os.listdir(tempfile.gettempdir())
                             if os.path.isdir(os.path.join(tempfile.gettempdir(), d)) and d.startswith("atlas-stage-")))
        # a file that cannot be read (a OneDrive placeholder that is not downloaded) is reported, the rest converted
        with mock.patch("atlas.convert.shutil.copyfile", side_effect=lambda s, d: (_ for _ in ()).throw(OSError("cloud file not available")) if "Flow" in s else open(d, "w").close()), \
                mock.patch("atlas.convert._run_powershell") as ps2:
            ps2.side_effect = fake
            for n in ("Claims Manual.docx", "sub/Flow.pptx", "sub/deeper/OLD.docx"):
                os.remove(os.path.join(self.docs, *n.split("/")))
            lines.clear()
            ok, have, fail = convert.convert_tree(self.docs, log=lines.append)
        self.assertEqual((ok, fail), (2, 1), "\n".join(lines))
        self.assertIn("Flow.ppt: could not read the file (cloud file not available) - a OneDrive file not downloaded", "\n".join(lines))

    def test_an_existing_copy_survives_a_failed_remake(self):
        """--refresh must never delete the old copy before a whole new one exists."""
        doc = os.path.join(self.docs, "rates.xls")
        docx = os.path.join(self.docs, "rates.xlsx")
        os.utime(doc, (1_800_000_000, 1_800_000_000))            # stale copy
        before = open(docx, "rb").read()
        with mock.patch("atlas.convert._run_powershell",
                        side_effect=lambda pairs, timeout, log=None, *rest: (0, "".join(f"OK\t{s}\t{d}\n" for s, d in pairs), "")):
            lines = []
            ok, have, fail = convert.convert_tree(self.docs, log=lines.append, refresh=True)   # "OK" but no file written
        self.assertEqual(open(docx, "rb").read(), before)
        self.assertIn("Office reported success but wrote no file", "\n".join(lines))

    def test_a_failure_carries_the_circumstances(self):
        """'Command failed' says nothing; the FAIL line adds where the file
        lives, whether it is a cloud placeholder, its size and the app."""
        od = os.path.join(self.td, "OneDrive - Company", "specs")
        os.makedirs(od)
        with open(os.path.join(od, "Rules.doc"), "w") as fh:
            fh.write("x" * 3000)
        c = convert.circumstances(os.path.join(od, "Rules.doc"))
        self.assertIn("path is under OneDrive/SharePoint", c)
        self.assertIn("3 KB", c)
        self.assertIn("Word", c)
        self.assertNotIn("cloud placeholder", c)
        lines = []
        with mock.patch("atlas.convert._run_powershell",
                        side_effect=lambda pairs, timeout, log=None, *rest: (0, "".join(f"FAIL\t{s}\tCommand failed\n" for s, d in pairs), "")):
            convert.convert_tree(od, log=lines.append)
        self.assertIn("Rules.doc: Command failed [path is under OneDrive/SharePoint; 3 KB; Word]", "\n".join(lines))

    def test_powershell_script_parses(self):
        """No Office here, but PowerShell is: the conversion script must be
        syntactically valid - a typo in it fails every file on the laptop."""
        import subprocess
        if shutil.which("powershell") is None:
            self.skipTest("no powershell here")
        path = os.path.join(self.td, "convert.ps1")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(convert._PS_SCRIPT)
        probe = ("$errs = $null; $null = [System.Management.Automation.Language.Parser]::ParseFile('" + path.replace("'", "''")
                 + "', [ref]$null, [ref]$errs); if ($errs.Count -gt 0) { $errs | ForEach-Object { Write-Output ('ERR ' + $_.Message) } } else { Write-Output 'PARSE OK' }")
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", probe],
                           capture_output=True, text=True, timeout=120)
        self.assertIn("PARSE OK", p.stdout, p.stdout + p.stderr)
        for fn in ("Convert-Doc", "Convert-Xls", "Describe-Doc", "Report-NewPid"):
            self.assertIn("function " + fn, convert._PS_SCRIPT)

    def test_robustness_pass(self):
        """Templates and RTF convert too; a name with a control character is
        refused with a reason; Office's known messages get a hint; a result
        held by OneDrive for a moment is retried; a long path gets the prefix."""
        from atlas import docs
        for n in ("old.dot", "spec.rtf", "sheet.xlt", "deck.pot"):
            with open(os.path.join(self.docs, n), "w") as fh:
                fh.write("x")
        by_src = {os.path.basename(s): os.path.basename(d) for s, d, _st in convert.plan(self.docs)}
        self.assertEqual(by_src["old.dot"], "old.docx")
        self.assertEqual(by_src["spec.rtf"], "spec.docx")
        self.assertEqual(by_src["sheet.xlt"], "sheet.xlsx")
        self.assertEqual(by_src["deck.pot"], "deck.pptx")
        self.assertIn("File Block Settings", convert.hint_for("You are attempting to open a file type that is blocked"))
        self.assertIn("password to OPEN", convert.hint_for("The password is incorrect. Word cannot open the document."))
        self.assertIn("damaged", convert.hint_for("Word found unreadable content in x.doc"))
        self.assertIn("open elsewhere", convert.hint_for("The file is locked for editing by another user"))
        self.assertIn("generic refusal", convert.hint_for("readonly-open, docx: Command failed hr=0x800a1066 [ReadOnly=True]"))
        self.assertEqual(convert.hint_for("something new"), "")
        # a replace that fails twice then succeeds (OneDrive sync holding the new file)
        calls = {"n": 0}
        real_replace = os.replace

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("being used by another process")
            return real_replace(a, b)
        stage = tempfile.mkdtemp()
        produced = os.path.join(stage, "0", "Claims Manual.docx")
        os.makedirs(os.path.dirname(produced))
        with open(produced, "w") as fh:
            fh.write("new")
        ssrc = os.path.join(stage, "0", "Claims Manual.doc")
        back = {ssrc: (os.path.join(self.docs, "Claims Manual.doc"), os.path.join(self.docs, "Claims Manual.docx"))}
        with mock.patch("atlas.convert.os.replace", side_effect=flaky), mock.patch("atlas.convert.time.sleep"):
            rows = convert._unstage([("OK", ssrc, produced)], back, lambda l: None)
        self.assertEqual(rows[0][0], "OK")
        self.assertEqual(calls["n"], 3)
        self.assertTrue(os.path.exists(os.path.join(self.docs, "Claims Manual.docx")))
        # long paths
        deep = "C:\\" + "\\".join(["folder-with-a-long-name"] * 12) + "\\file.doc"
        lp = docs.long_path(deep)
        if os.name == "nt":
            self.assertTrue(lp.startswith("\\\\?\\"), lp)
            self.assertEqual(docs.long_path("C:\\short.doc"), "C:\\short.doc")
        for fn in ("Convert-Ppt", "Tick-App"):
            self.assertIn("function " + fn, convert._PS_SCRIPT)
        self.assertIn("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8", convert._PS_SCRIPT)

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
