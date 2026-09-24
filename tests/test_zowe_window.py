"""
LESSONS 179 - the toolkit runs zowe exactly as the developer's window does.

His report: `zowe zos-files download all-members "ABC.XYZ.SOURCE" -d SOURCE/`
typed in his window never asks for anything; run by the toolkit it asked for
a password. Two differences the toolkit introduced: it forced
ZOWE_USE_DAEMON=no, so the plain CLI had to find the team configuration
itself; and it ran from the toolkit's folder, while Zowe looks for a
project zowe.config.json from the CURRENT folder upward - from the wrong
folder it finds no profile and asks for host / user / password.

Guarded here: the command is his, word for word; the daemon is left as the
window has it unless sources.json says "off"; every subprocess runs from
the working folder (default: the folder of sources.json); Check Zowe names
the configuration files zowe finds from there, or says in words that it
found none; --plan and the UI log show the command and the folder; an old
sources.json without the new keys loads with the new defaults; the UI's
save round-trips both settings; the first hint is the folder, never the
password. No zowe here: pure command builders and fake subprocesses only.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import fetch  # noqa: E402


def _cfg(td, **zowe):
    cfg = fetch.load_config(os.path.join(td, "sources.json"))
    cfg["local_root"] = os.path.join(td, "estate")
    cfg["zowe"].update(zowe)
    cfg["sources"] = [fetch.new_source("PROD.X.SRC", "cobol", system="CLAIMS")]
    return cfg


class HisCommand(unittest.TestCase):

    def setUp(self):
        fetch.set_session_credentials(None, None)
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        fetch.set_session_credentials(None, None)

    @mock.patch("atlas.fetch.zowe_exe", return_value="zowe")
    def test_download_is_his_command_word_for_word(self, _which):
        cfg = _cfg(self.td)
        src = cfg["sources"][0]
        self.assertEqual(fetch.download_cmd(cfg, src),
                         ["zowe", "zos-files", "download", "all-members", "PROD.X.SRC", "-d", fetch.local_path(cfg, src)])

    @mock.patch("atlas.fetch.zowe_exe", return_value="zowe")
    def test_profile_user_password_only_when_set(self, _which):
        cfg = _cfg(self.td)
        src = cfg["sources"][0]
        for flag in ("--zosmf-profile", "--user", "--password", "--encoding"):
            self.assertNotIn(flag, fetch.download_cmd(cfg, src))
            self.assertNotIn(flag, fetch.list_members_cmd(cfg, "PROD.X.SRC"))
        cfg["zowe"]["profile"] = "lpar1"
        cmd = fetch.download_cmd(cfg, src)
        self.assertEqual(cmd[cmd.index("--zosmf-profile") + 1], "lpar1")
        self.assertNotIn("--user", cmd)
        fetch.set_session_credentials("DEEPAK", "pw-for-this-test")
        cmd = fetch.download_cmd(cfg, src)
        self.assertEqual(cmd[cmd.index("--user") + 1], "DEEPAK")
        self.assertEqual(cmd[cmd.index("--password") + 1], "pw-for-this-test")
        self.assertEqual(cmd[:7], ["zowe", "zos-files", "download", "all-members", "PROD.X.SRC", "-d",
                                   fetch.local_path(cfg, src)])

    def test_daemon_left_as_the_window_has_it_unless_off(self):
        cfg = _cfg(self.td)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZOWE_USE_DAEMON", None)
            self.assertNotIn("ZOWE_USE_DAEMON", fetch.session_env(cfg))
            self.assertNotIn("ZOWE_USE_DAEMON", fetch.session_env())          # no config at all: untouched too
            os.environ["ZOWE_USE_DAEMON"] = "yes"                              # whatever the window has
            self.assertEqual(fetch.session_env(cfg)["ZOWE_USE_DAEMON"], "yes")
            cfg["zowe"]["daemon"] = "off"
            self.assertEqual(fetch.session_env(cfg)["ZOWE_USE_DAEMON"], "no")
            self.assertTrue(fetch.daemon_off(cfg))
            cfg["zowe"]["daemon"] = "window"
            self.assertFalse(fetch.daemon_off(cfg))
            self.assertEqual(fetch.session_env(cfg)["ZOWE_USE_DAEMON"], "yes")


class WorkingFolder(unittest.TestCase):

    def setUp(self):
        fetch.set_session_credentials(None, None)
        self.td = tempfile.mkdtemp()

    def test_default_is_the_folder_of_sources_json(self):
        cfg = _cfg(self.td)
        self.assertEqual(fetch.zowe_cwd(cfg), os.path.abspath(self.td))
        cfg["zowe"]["working_dir"] = "   "
        self.assertEqual(fetch.zowe_cwd(cfg), os.path.abspath(self.td))
        work = os.path.join(self.td, "where-zowe-works")
        cfg["zowe"]["working_dir"] = work
        self.assertEqual(fetch.zowe_cwd(cfg), os.path.abspath(work))

    def test_every_subprocess_runs_from_the_working_folder(self):
        """download, list, version and config list all pass cwd=<working
        folder> and the window's environment to subprocess.run."""
        work = os.path.join(self.td, "where-zowe-works")
        os.makedirs(work)
        cfg = _cfg(self.td, working_dir=work)
        seen = []

        class P:
            returncode, stdout, stderr = 0, "", ""

        def fake_run(cmd, **kw):
            seen.append((cmd, kw))
            return P()
        with mock.patch("atlas.fetch.subprocess.run", side_effect=fake_run), \
                mock.patch("atlas.fetch.zowe_exe", return_value="zowe"), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZOWE_USE_DAEMON", None)
            r = fetch.Runner(5, cfg)
            for cmd in (fetch.download_cmd(cfg, cfg["sources"][0]), fetch.list_members_cmd(cfg, "PROD.X.SRC"),
                        fetch.version_cmd(cfg), fetch.config_locations_cmd(cfg)):
                self.assertEqual(r.run(cmd)[0], 0)
        self.assertEqual(len(seen), 4)
        for cmd, kw in seen:
            self.assertEqual(kw["cwd"], os.path.abspath(work), cmd)
            self.assertNotIn("ZOWE_USE_DAEMON", kw["env"], cmd)
            self.assertIs(kw["stdin"], fetch.subprocess.DEVNULL)
        # and with the daemon off, the same subprocesses get ZOWE_USE_DAEMON=no
        cfg["zowe"]["daemon"] = "off"
        seen.clear()
        with mock.patch("atlas.fetch.subprocess.run", side_effect=fake_run):
            fetch.Runner(5, cfg).run(["zowe", "--version"])
        self.assertEqual(seen[0][1]["env"]["ZOWE_USE_DAEMON"], "no")

    def test_fetch_and_check_use_the_working_folder_by_default(self):
        """No runner given: fetch_source and check_zowe build their own, and
        it must carry the folder - not the toolkit's current directory."""
        work = os.path.join(self.td, "where-zowe-works")
        os.makedirs(work)
        cfg = _cfg(self.td, working_dir=work)
        cwds = []

        class P:
            returncode, stdout, stderr = 0, "7.18.0\n", ""

        def fake_run(cmd, **kw):
            cwds.append(kw.get("cwd"))
            return P()
        with mock.patch("atlas.fetch.subprocess.run", side_effect=fake_run), \
                mock.patch("atlas.fetch.zowe_exe", return_value="zowe"):
            fetch.fetch_source(cfg, cfg["sources"][0], log=lambda s: None)
            fetch.check_zowe(cfg)
        self.assertTrue(cwds)
        self.assertEqual(set(cwds), {os.path.abspath(work)})

    def test_a_missing_working_folder_is_named_not_mistaken_for_a_missing_zowe(self):
        cfg = _cfg(self.td, working_dir=os.path.join(self.td, "gone"))
        rc, _out, err = fetch.Runner(5, cfg).run(["zowe", "--version"])
        self.assertNotEqual(rc, 0)
        self.assertIn("working folder does not exist", err)
        self.assertIn("gone", err)
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"):
            ok, msg = fetch.check_zowe(cfg, runner=object())          # never reached
        self.assertFalse(ok)
        self.assertIn("working folder does not exist", msg)
        self.assertIn("Run zowe from folder", msg)

    def test_an_old_sources_json_without_the_new_keys_loads_with_the_defaults(self):
        p = os.path.join(self.td, "sources.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"zowe": {"executable": "zowe", "profile": "old", "encoding": "", "timeout_seconds": 60,
                                "extra_args": []},
                       "local_root": self.td, "db": "atlas.db", "sources": []}, fh)
        cfg = fetch.load_config(p)
        self.assertEqual(cfg["zowe"]["profile"], "old")
        self.assertEqual(cfg["zowe"]["working_dir"], "")
        self.assertEqual(cfg["zowe"]["daemon"], "window")
        self.assertFalse(fetch.daemon_off(cfg))
        self.assertEqual(fetch.zowe_cwd(cfg), os.path.abspath(self.td))
        # the in-memory folder marker never reaches the file
        fetch.save_config(cfg, p)
        with open(p, encoding="utf-8") as fh:
            back = json.load(fh)
        self.assertNotIn(fetch.CONFIG_DIR_KEY, back)
        self.assertEqual(back["zowe"]["working_dir"], "")
        self.assertEqual(back["zowe"]["daemon"], "window")


class CheckReportsTheConfiguration(unittest.TestCase):

    def setUp(self):
        fetch.set_session_credentials(None, None)
        self.td = tempfile.mkdtemp()

    class Fake:
        def __init__(self, config_out, config_rc=0):
            self.config_out, self.config_rc = config_out, config_rc
            self.cmds = []

        def run(self, cmd):
            self.cmds.append(cmd)
            if "--version" in cmd:
                return 0, "7.18.0\n", ""
            if "config" in cmd:
                return self.config_rc, self.config_out, "" if self.config_rc == 0 else "Command failed"
            if "list" in cmd:
                return 0, '{"data":{"items":[{"member":"A"}]}}', ""
            return 1, "", "unexpected"

    LOCATIONS = ("C:\\work\\zowe.config.json:\n"
                 "  profiles:\n    lpar1:\n      properties:\n        host: mf.example.test\n        user: DEEPAK\n"
                 "C:\\Users\\me\\.zowe\\zowe.config.user.json:\n"
                 "  defaults:\n    zosmf: lpar1\n")

    def test_paths_only_never_a_value(self):
        self.assertEqual(fetch.parse_config_locations(self.LOCATIONS),
                         ["C:\\work\\zowe.config.json", "C:\\Users\\me\\.zowe\\zowe.config.user.json"])
        self.assertEqual(fetch.parse_config_locations("/home/me/.zowe/zowe.config.json:\n - profiles\n"),
                         ["/home/me/.zowe/zowe.config.json"])
        self.assertEqual(fetch.parse_config_locations(""), [])
        self.assertEqual(fetch.parse_config_locations("profiles:\n  base:\n    host: x\n"), [])
        # a user folder with a space in it, and a bulleted list: the whole path, nothing before it
        self.assertEqual(fetch.parse_config_locations("Configuration files:\n - C:\\Users\\John Smith\\.zowe\\zowe.config.json\n"
                                                      " - C:\\My Work\\proj\\zowe.config.user.json\n"),
                         ["C:\\Users\\John Smith\\.zowe\\zowe.config.json", "C:\\My Work\\proj\\zowe.config.user.json"])

    def test_check_names_the_configuration_files_and_where_it_looked(self):
        cfg = _cfg(self.td)
        fake = self.Fake(self.LOCATIONS)
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"):
            ok, msg = fetch.check_zowe(cfg, runner=fake)
        self.assertTrue(ok, msg)
        self.assertIn(["zowe", "config", "list", "--locations", "--root"], fake.cmds)
        self.assertIn(f"3. zowe configuration found from {os.path.abspath(self.td)}", msg)
        self.assertIn("C:\\work\\zowe.config.json", msg)
        self.assertIn("C:\\Users\\me\\.zowe\\zowe.config.user.json", msg)
        self.assertNotIn("mf.example.test", msg)                     # values never reach the log
        self.assertNotIn("DEEPAK", msg)
        self.assertIn(f"run from: {os.path.abspath(self.td)}", msg)
        self.assertIn("zowe daemon as your window has it", msg)
        self.assertIn("not needed when your zowe command works without a prompt", msg)
        self.assertIn("4. host answered", msg)

    def test_check_says_in_words_when_none_is_found(self):
        cfg = _cfg(self.td)
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"):
            ok, msg = fetch.check_zowe(cfg, runner=self.Fake("profiles:\n  (none)\n"))
        self.assertIn(f"3. no zowe configuration found from {os.path.abspath(self.td)} - run the check from the "
                      "folder where your command works, or set 'Run zowe from folder'", msg)
        self.assertTrue(ok)                                            # the host stage still decides

    def test_check_goes_on_when_the_config_command_does_not_exist(self):
        cfg = _cfg(self.td, daemon="off")
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"):
            ok, msg = fetch.check_zowe(cfg, runner=self.Fake("", config_rc=1))
        self.assertTrue(ok)
        self.assertIn("3. `zowe config list --locations` failed (rc 1)", msg)
        self.assertIn("[ZOWE_USE_DAEMON=no]", msg)

    def test_check_says_when_zowe_is_not_on_path(self):
        cfg = _cfg(self.td)
        with mock.patch("atlas.fetch.zowe_exe", return_value=None):
            ok, msg = fetch.check_zowe(cfg, runner=self.Fake(""))
        self.assertFalse(ok)
        self.assertIn("is not on PATH", msg)


class PlanShowsCommandAndFolder(unittest.TestCase):

    def test_plan_prints_the_folder_then_his_command(self):
        td = tempfile.mkdtemp()
        cfgp = os.path.join(td, "sources.json")
        cfg = _cfg(td)
        fetch.save_config(cfg, cfgp)
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = fetch.main(["--config", cfgp, "--plan"])
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], f"run from: {os.path.abspath(td)}   [zowe daemon as your window has it]")
        self.assertEqual(lines[1], "zowe zos-files download all-members PROD.X.SRC -d "
                         + fetch.local_path(cfg, cfg["sources"][0]))
        # with a working folder and the daemon off, the plan says so
        cfg["zowe"]["working_dir"] = os.path.join(td, "w")
        cfg["zowe"]["daemon"] = "off"
        fetch.save_config(cfg, cfgp)
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            fetch.main(["--config", cfgp, "--plan"])
        self.assertIn(f"run from: {os.path.abspath(os.path.join(td, 'w'))}   [ZOWE_USE_DAEMON=no", out.getvalue())

    def test_the_fetch_log_shows_the_folder_and_the_command(self):
        td = tempfile.mkdtemp()
        cfg = _cfg(td)
        lines = []

        class Fake:
            def run(self, cmd):
                return 0, "", ""
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"):
            fetch.fetch_source(cfg, cfg["sources"][0], runner=Fake(), log=lines.append)
        self.assertIn(f"  run from: {os.path.abspath(td)}   [zowe daemon as your window has it]", lines)
        self.assertIn("> zowe zos-files download all-members PROD.X.SRC -d " + fetch.local_path(cfg, cfg["sources"][0]),
                      lines)


class TheFirstHintIsTheFolder(unittest.TestCase):

    def tearDown(self):
        fetch.set_session_credentials(None, None)

    def test_folder_before_daemon_before_password_in_every_hint(self):
        td = tempfile.mkdtemp()
        cfg = _cfg(td)
        cases = [(1, "Enter the host name of your service:", ""),
                 (1, "", "Enter password: \nCommand Error: no value specified for password"),
                 (124, "", "timed out after 120s")]
        for rc, out, err in cases:
            hint = fetch.password_hint(rc, out, err, cfg)
            self.assertTrue(hint, (rc, out, err))
            self.assertIn("run the toolkit from the folder where your zowe command works, or set the working folder", hint)
            self.assertIn("leave the daemon as your window has it", hint)
            self.assertIn(os.path.abspath(td), hint)                    # where this run was from
            for later in ("zowe config secure", "--ask-password", "extra_args"):
                if later in hint:
                    self.assertLess(hint.index("folder where your zowe command works"), hint.index(later), hint)
        # without a config in hand the hint still leads with the folder
        self.assertTrue(fetch.password_hint(124, "", "timed out").startswith("zowe printed nothing"))
        self.assertIn("folder where your zowe command works", fetch.password_hint(124, "", "timed out"))
        fetch.set_session_credentials("DEEPAK", "pw")
        self.assertIn("folder where your zowe command works", fetch.password_hint(124, "", "timed out", cfg))
        self.assertEqual(fetch.password_hint(0, "", "", cfg), "")


class UiRoundTrip(unittest.TestCase):
    """The UI's field and checkbox are saved into sources.json with the
    other settings, and read back. Skipped where tkinter is absent or no
    display can be opened."""

    def test_save_round_trips_working_dir_and_daemon(self):
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter not available")
        from atlas import ui
        td = tempfile.mkdtemp()
        cfgp = os.path.join(td, "sources.json")
        fetch.save_config(_cfg(td), cfgp)
        try:
            app = ui.App(cfgp)
        except Exception as e:                                        # noqa: BLE001 - no display on this CI
            self.skipTest(f"no Tk display here: {e}")
        try:
            app.withdraw()
            self.assertEqual(app.v_zowe_dir.get(), "")
            self.assertFalse(app.v_daemon_off.get())
            self.assertIn("not needed when your zowe command works without a prompt", ui.SIGN_IN_NOTE)
            self.assertIn("not needed when your zowe command works without a prompt", ui.NO_SESSION_PASSWORD)
            work = os.path.join(td, "where-zowe-works")
            app.v_zowe_dir.set(work)
            app.v_daemon_off.set(True)
            app.save()
            # Plan in the UI shows the folder and his command, redacted
            fetch.set_session_credentials("DEEPAK", "pw-for-this-test")
            try:
                app.plan()
                logged = []
                while not app.q.empty():
                    logged.append(app.q.get_nowait())
            finally:
                fetch.set_session_credentials(None, None)
            self.assertIn(f"run from: {os.path.abspath(work)}   [ZOWE_USE_DAEMON=no - 'Zowe daemon off' is set]", logged)
            self.assertTrue(any(l.startswith("zowe zos-files download all-members PROD.X.SRC -d ") for l in logged), logged)
            self.assertFalse(any("pw-for-this-test" in l for l in logged), logged)
        finally:
            app.destroy()
        back = fetch.load_config(cfgp)
        self.assertEqual(back["zowe"]["working_dir"], work)
        self.assertEqual(back["zowe"]["daemon"], "off")
        self.assertTrue(fetch.daemon_off(back))
        # and the file holds no password and no in-memory marker
        with open(cfgp, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("pw-for-this-test", text)
        self.assertNotIn(fetch.CONFIG_DIR_KEY, text)
        # unchecked again: "window"
        try:
            app = ui.App(cfgp)
        except Exception as e:                                        # noqa: BLE001
            self.skipTest(f"no Tk display here: {e}")
        try:
            app.withdraw()
            self.assertTrue(app.v_daemon_off.get())
            self.assertEqual(app.v_zowe_dir.get(), work)
            app.v_daemon_off.set(False)
            app.save()
        finally:
            app.destroy()
        self.assertEqual(fetch.load_config(cfgp)["zowe"]["daemon"], "window")


class UiSelfExplaining(unittest.TestCase):
    """His words: "the UI should be self explanatory". A senior developer
    alone on a locked laptop must read what to do from the window itself:
    a strip with the five steps in order and which one is possible now, a
    grey help line with an example under every field, a hover tip on every
    button, a status line that says what happened and the next step, and a
    Help button. Skipped where tkinter is absent or no display opens."""

    def setUp(self):
        fetch.set_session_credentials(None, None)
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        fetch.set_session_credentials(None, None)

    def _app(self):
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter not available")
        from atlas import ui
        cfgp = os.path.join(self.td, "sources.json")
        fetch.save_config(_cfg(self.td), cfgp)
        try:
            app = ui.App(cfgp)
        except Exception as e:                                        # noqa: BLE001 - no display on this CI
            self.skipTest(f"no Tk display here: {e}")
        app.withdraw()
        self.addCleanup(app.destroy)
        return ui, app

    @classmethod
    def _walk(cls, w):
        yield w
        for c in w.winfo_children():
            yield from cls._walk(c)

    def _windows(self, ui, app):
        """The main window and its three dialogs, all kept off screen."""
        wins = [ui.SourceDialog(app, modal=False), app.sign_in(), app.bulk_add()]
        for w in wins:
            w.withdraw()
            self.addCleanup(w.destroy)
        return [app, *wins]

    def _wait(self, app, marker: str) -> str:
        for _ in range(600):
            app.update()
            if marker in app.v_status.get() and not app.busy:
                return app.v_status.get()
            time.sleep(0.01)
        self.fail(f"status never said {marker!r}: {app.v_status.get()!r}")

    def test_help_line_under_every_field(self):
        ui, app = self._app()
        from tkinter import ttk
        for win in self._windows(ui, app):
            fields = [w for w in self._walk(win) if isinstance(w, (ttk.Entry, ttk.Combobox, ttk.Checkbutton))]
            self.assertTrue(fields, win.title())
            for w in fields:
                lab = getattr(w, "_atlas_help", None)
                self.assertIsNotNone(lab, f"no help line under a field of '{win.title()}'")
                self.assertTrue(lab.cget("text").strip(), win.title())
                self.assertGreater(int(lab.cget("wraplength")), 0, "a help line must wrap, never overflow")
                self.assertEqual(str(lab.cget("foreground")), ui.HELP_FG)
        self.assertEqual(set(app.help_labels), {"profile", "zowe_dir", "daemon", "sign_in", "root", "db", "extra", "filter"})
        # the words he asked for, each with an example
        for key, must in {"dataset": "e.g. PROD.CLAIMS.SRC", "local": "e.g. CLAIMS\\SRC gives estate\\CLAIMS\\SRC",
                          "zowe_dir": "the folder your command window is in when zowe works without asking anything",
                          "profile": "leave blank unless your window command uses --zosmf-profile",
                          "extra_args": "rarely needed; what you add to your own command, e.g. --encoding 1047",
                          "daemon": "tick only if zowe hangs when run from here",
                          "sign_in": "not needed when your zowe command works without a prompt"}.items():
            self.assertIn(must, ui.HELP[key], key)

    def test_strip_lists_the_five_steps_in_order(self):
        ui, app = self._app()
        self.assertEqual(len(ui.STEPS), 5)
        self.assertEqual(len(app.strip_labels), 5)
        names = ("Check Zowe", "Add the datasets to fetch", "Fetch", "Build", "Ask")
        buttons = ("Check Zowe", "Add, Bulk add", "Fetch selected / Fetch system / Fetch all", "Build index", "atlas.query")
        for i, ((head, body), name, button) in enumerate(zip(app.strip_labels, names, buttons), 1):
            self.assertTrue(head.cget("text").startswith(f"{i}  {name} - "), head.cget("text"))
            self.assertIn(button, body.cget("text"))
            self.assertGreater(int(body.cget("wraplength")), 0)
        # one row in the table, nothing fetched, no estate folder, no index:
        # step 2 is done, 1 and 3 are possible, 4 and 5 come later - and the
        # strip says so in words
        self.assertEqual(app.step_state, ["now", "done", "now", "later", "later"])
        heads = [h.cget("text") for h, _b in app.strip_labels]
        self.assertTrue(heads[0].endswith("you can do this now"), heads[0])
        self.assertTrue(heads[1].endswith("done"), heads[1])
        self.assertTrue(heads[3].endswith("after step 3"), heads[3])
        self.assertTrue(heads[4].endswith("after step 4"), heads[4])
        # the Help button and its page
        helps = [b for b in self._walk(app) if b.winfo_class() == "TButton" and b.cget("text") == "Help"]
        self.assertEqual(len(helps), 1)
        win = app.show_help()
        win.withdraw()
        self.addCleanup(win.destroy)
        for must in ("1  Check Zowe", "2  Add the datasets", "3  Fetch", "4  Build", "5  Ask", "zowe.working_dir",
                     "zowe.daemon", "local_root", "extra_roots", "sources", "atlas.db", "work\\", "out\\",
                     "manifest.json", "atlas-problems.txt"):
            self.assertIn(must, ui.HELP_TEXT, must)

    def test_step_states_follow_what_the_window_knows(self):
        from atlas import ui
        cfg = _cfg(self.td)
        self.assertEqual(ui.step_states(cfg, False, db_exists=False), ["now", "done", "now", "later", "later"])
        self.assertEqual(ui.step_states(dict(cfg, sources=[]), False, db_exists=False),
                         ["now", "now", "later", "later", "later"])
        cfg["sources"][0].update(last_fetched="2000-01-01T00:00:00", last_result="ok 3 file(s) in x")
        self.assertEqual(ui.step_states(cfg, True, db_exists=False), ["done", "done", "done", "now", "later"])
        # an estate folder alone makes Build possible
        os.makedirs(os.path.join(cfg["local_root"], "CLAIMS"))
        self.assertEqual(ui.step_states(dict(cfg, sources=[]), False, db_exists=False)[3], "now")
        # an index newer than the last fetch: Build done, Ask possible; a
        # fetch newer than the index: Build again
        db = os.path.join(self.td, "atlas.db")
        with open(db, "w", encoding="utf-8"):
            pass
        cfg["db"] = db
        self.assertEqual(ui.step_states(cfg, True), ["done", "done", "done", "done", "now"])
        cfg["sources"][0]["last_fetched"] = "2099-01-01T00:00:00"
        self.assertEqual(ui.step_states(cfg, True), ["done", "done", "done", "now", "now"])
        cfg["sources"][0]["last_result"] = "FAILED rc 1"
        self.assertEqual(ui.step_states(cfg, True)[2], "now")

    def test_every_button_has_a_tooltip(self):
        ui, app = self._app()
        seen = 0
        for win in self._windows(ui, app):
            for b in self._walk(win):
                if b.winfo_class() != "TButton":
                    continue
                tip = ui.Tooltip.of(b)
                self.assertIsNotNone(tip, f"no tip on button '{b.cget('text')}' of '{win.title()}'")
                self.assertTrue(tip.text.strip(), b.cget("text"))
                self.assertIn("<Enter>", b.bind())
                self.assertIn("<Leave>", b.bind())
                seen += 1
        self.assertGreaterEqual(seen, 24)
        # the two Browse buttons say different things
        tips = {ui.Tooltip.of(b).text for b in self._walk(app) if b.winfo_class() == "TButton" and b.cget("text") == "Browse"}
        self.assertEqual(len(tips), 2)

    def test_status_says_what_happened_and_the_next_step(self):
        ui, app = self._app()
        ok = [fetch.FetchResult("PROD.X.SRC", True, 3, 0.1, "3 file(s) in x")] * 3
        with mock.patch("atlas.fetch.fetch_all", return_value=ok):
            app.fetch_all()
            status = self._wait(app, "next:")
        self.assertEqual(status, "Fetched 3 of 3 datasets - next: Build index")
        bad = [fetch.FetchResult("PROD.X.SRC", False, 0, 0.1,
                                 "rc 1: Error: no password | second line - set 'Run zowe from folder' to the folder "
                                 "where your command works")]
        with mock.patch("atlas.fetch.fetch_all", return_value=bad):
            app.fetch_all()
            status = self._wait(app, "failed")
        self.assertEqual(status, "Fetched 0 of 1; PROD.X.SRC failed: rc 1: Error: no password - try: set 'Run zowe "
                                 "from folder' to the folder where your command works")
        # a fake build: the summary the build prints, read back into one line
        lines = ["12:00:00  parsing done: 121,000 member(s) in 2:10:00",
                 "", "== members by kind / parse status ==",
                 "  cobol      ok        80850", "  cobol      partial     150", "  copybook   ok        40000",
                 "", "== this run: new 121000  changed 0  unchanged 0  pruned 0 ==",
                 "== problems in this run: none ==", "db: atlas.db   root: C:/estate   7800.0s"]

        def fake_stream(self_, cmd):
            self.assertIn("atlas.build", cmd)
            self_.last_output = list(lines)
            return 0
        with mock.patch.object(ui.App, "_stream", fake_stream):
            app.build(False)
            status = self._wait(app, "Build done")
        self.assertTrue(status.startswith("Build done: 121,000 members, 150 parsed only in part - next: ask"), status)
        self.assertIn("python -m atlas.query --db atlas.db pack NAME", status)
        # the check: its own words, the folder, the one thing to try
        no_conf = ("   run from: C:/x\n1. zowe found: C:/z/zowe.cmd\n2. zowe --version: 7.0.0\n"
                   "3. no zowe configuration found from C:/x - run the check from the folder where your command "
                   "works, or set 'Run zowe from folder' (sources.json -> zowe.working_dir)\n"
                   "4. the host did NOT answer `zowe zos-files list all-members PROD.X.SRC` (rc 1): no password\n"
                   "   -> the folder hint")
        with mock.patch("atlas.fetch.check_zowe", return_value=(False, no_conf)):
            app.check_zowe()
            status = self._wait(app, "Zowe check failed")
        self.assertEqual(status, "Zowe check failed: zowe found no configuration from C:/x - set 'Run zowe from "
                                 "folder' to the folder where your command works")
        self.assertEqual(app.step_state[0], "now")
        with mock.patch("atlas.fetch.check_zowe", return_value=(True, "1. zowe found: z\n4. host answered: PROD.X.SRC "
                                                                     "has 12 member(s) - connection, profile and "
                                                                     "password are fine; Fetch will work")):
            app.check_zowe()
            status = self._wait(app, "Zowe OK")
        self.assertEqual(status, "Zowe OK: host answered: PROD.X.SRC has 12 member(s) - next: Fetch (Fetch all, or "
                                 "select rows and Fetch selected)")
        self.assertEqual(app.step_state[0], "done")
        self.assertTrue(app.strip_labels[0][0].cget("text").endswith("done"))

    def test_status_lines_without_a_window(self):
        from atlas import ui
        self.assertEqual(ui.status_after_fetch([]), "Nothing fetched: no enabled dataset matched - enable a row or add "
                                                    "one, then Fetch again")
        r = fetch.FetchResult("A", False, 2900, 9.0, "INCOMPLETE 2900/4100 members (1200 missing)")
        self.assertEqual(ui.status_after_fetch([r]), "Fetched 0 of 1; A failed: INCOMPLETE 2900/4100 members (1200 "
                                                     "missing) - try: Fetch it again; the log lists the missing members")
        self.assertEqual(ui.status_after_build(130, [], "atlas.db"),
                         "Build stopped - Build index again continues from where it stopped; parsed members are kept")
        crash = ["12:00  BUILD STOPPED BY AN ERROR: OSError: disk full",
                 "  The index cannot be written (disk full): free space on the drive holding C:/atlas.db", "trace"]
        self.assertEqual(ui.status_after_build(2, crash, "atlas.db"),
                         "Build stopped: OSError: disk full - try: The index cannot be written (disk full): free space "
                         "on the drive holding C:/atlas.db")
        self.assertTrue(ui.status_after_build(0, ["nothing to parse"], "x.db").startswith("Build finished - next: ask"))
        # a failing stage without an arrow hint: the text after " - " is the thing to try
        msg = "1. zowe found: z\n2. the working folder does not exist: C:/nope - set 'Run zowe from folder' to the folder"
        self.assertEqual(ui.status_after_check(False, msg, True),
                         "Zowe check failed: the working folder does not exist: C:/nope - try: set 'Run zowe from "
                         "folder' to the folder")
        self.assertTrue(ui.status_after_check(True, "4. no enabled PDS in the table yet - add one", False)
                        .endswith("next: add the datasets to fetch (Add or Bulk add)"))
        # a stop with no summary line: the exit code in plain words, never "rc"
        self.assertEqual(ui.status_after_build(3, [], "atlas.db"),
                         "Build stopped (exit code 3): exit code 3 - try: Build index again; members parsed so far are kept")
        self.assertNotIn("rc ", ui.status_after_build(3, ["   "], "atlas.db"))

    NO_CONF_NO_PDS = ("   run from: C:/x\n1. zowe found: C:/z/zowe.cmd\n2. zowe --version: 7.0.0\n"
                      "3. no zowe configuration found from C:/x - run the check from the folder where your command "
                      "works, or set 'Run zowe from folder' (sources.json -> zowe.working_dir)\n"
                      "4. no enabled PDS in the table yet - add one, then Check again to test the host connection")

    def test_no_configuration_is_not_a_pass_when_the_host_stage_was_skipped(self):
        """With no enabled PDS the host stage is skipped and check_zowe comes
        back OK - but stage 3 found no configuration, and that is the problem
        to fix first: the status says so with the fix, and step 1 is not done."""
        from atlas import ui
        self.assertFalse(ui.check_passed(True, self.NO_CONF_NO_PDS))
        status = ui.status_after_check(True, self.NO_CONF_NO_PDS, False)
        self.assertEqual(status, "Zowe check failed: zowe found no configuration from C:/x - set 'Run zowe from "
                                 "folder' to the folder where your command works")
        # a host that answered anyway (a Zowe v1 profile) is a real pass
        answered = self.NO_CONF_NO_PDS.rsplit("\n", 1)[0] + "\n4. host answered: PROD.X.SRC has 12 member(s) - fine"
        self.assertTrue(ui.check_passed(True, answered))
        self.assertTrue(ui.status_after_check(True, answered, True).startswith("Zowe OK: host answered"))
        self.assertFalse(ui.check_passed(False, "1. 'zowe' is not on PATH for this process."))
        # and in the window: step 1 stays "you can do this now", the status is red
        ui, app = self._app()
        with mock.patch("atlas.fetch.check_zowe", return_value=(True, self.NO_CONF_NO_PDS)):
            app.check_zowe()
            status = self._wait(app, "Zowe check failed")
        self.assertIn("no configuration from C:/x", status)
        self.assertIn("set 'Run zowe from folder'", status)
        self.assertFalse(app.checked_ok)
        self.assertEqual(app.step_state[0], "now")
        self.assertTrue(app.strip_labels[0][0].cget("text").endswith("you can do this now"))
        self.assertEqual(str(app.status_label.cget("foreground")), ui.BAD_FG)
        app.update()
        self.assertIn("RESULT: FAIL", app.log.get("1.0", "end"))

    def test_a_callback_that_raises_does_not_stop_the_log(self):
        """A callable on the queue that raises must not kill the poll loop:
        the error goes to the log, the status goes red, and a line queued
        after it still arrives."""
        ui, app = self._app()

        def boom():
            raise RuntimeError("the callback broke")
        app.q.put(boom)
        app.q.put("a line after the broken callback")
        for _ in range(100):
            app.update()
            if "a line after the broken callback" in app.log.get("1.0", "end"):
                break
            time.sleep(0.01)
        text = app.log.get("1.0", "end")
        self.assertIn("ERROR RuntimeError: the callback broke", text)
        self.assertIn("a line after the broken callback", text)
        self.assertLess(text.index("ERROR RuntimeError"), text.index("a line after"))
        self.assertIn("the callback broke", app.v_status.get())
        self.assertEqual(str(app.status_label.cget("foreground")), ui.BAD_FG)
        self.assertIsNotNone(app._poll_job)
        # and the loop is still alive afterwards
        app.q.put("one more line")
        for _ in range(100):
            app.update()
            if "one more line" in app.log.get("1.0", "end"):
                break
            time.sleep(0.01)
        self.assertIn("one more line", app.log.get("1.0", "end"))

    def test_plain_words_in_the_table_and_the_dialogs(self):
        ui, app = self._app()
        from tkinter import ttk
        for key, text in (("local", "folder on this laptop"), ("auth", "production copy"), ("last", "last fetch")):
            self.assertEqual(app.tree.heading(key)["text"], text)
        self.assertFalse(hasattr(ui, "ZOWE_DIR_HINT"))
        self.assertIn("e.g. the folder you cd to before typing zowe, such as C:\\Users\\you", ui.HELP["zowe_dir"])
        dlg = ui.SourceDialog(app, modal=False)
        dlg.withdraw()
        self.addCleanup(dlg.destroy)
        labels = {w.cget("text") for w in self._walk(dlg) if isinstance(w, ttk.Label)}
        self.assertIn("File extension for members, e.g. .cbl", labels)
        self.assertNotIn("File extension (pds)", labels)
        with mock.patch("tkinter.messagebox.showinfo") as info:
            app.v_filter.set("All")
            app.fetch_system()
        info.assert_called_once_with("Fetch system", "Pick a system in the System box first")

    def test_the_log_shows_eight_lines_on_a_720_screen(self):
        """At 1180x630 (the height the window picks on a 1280x720 laptop)
        and at 1280x720 the log shows at least eight full lines and the
        table four rows: the log is where Fetch and Build say how far they
        are. The window is laid out invisible and off screen, since a
        withdrawn window is not laid out at all."""
        ui, app = self._app()
        try:
            app.attributes("-alpha", 0.0)
        except Exception:                                             # noqa: BLE001 - no alpha here
            pass
        app.deiconify()
        for i in range(40):
            app._append_log(f"line {i}")
        for size in ("1180x630", "1280x720"):
            app.geometry(size + "+-3000+-3000")
            app.update()
            app.update()
            if (app.winfo_width(), app.winfo_height()) != tuple(int(x) for x in size.split("x")):
                self.skipTest(f"the window manager did not give the window {size}")
            app.log.see("end")
            app.update()
            h = app.log.winfo_height()
            full = sum(1 for i in range(1, 41)
                       if (d := app.log.dlineinfo(f"{i}.0")) is not None and d[1] + d[3] <= h)
            self.assertGreaterEqual(full, 8, f"{size}: {full} full log lines in {h}px")
            self.assertEqual(int(app.tree.cget("height")), 4)
            self.assertEqual(app.winfo_height(), int(size.split("x")[1]))
        app.withdraw()


if __name__ == "__main__":
    unittest.main(verbosity=2)
