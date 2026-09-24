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


if __name__ == "__main__":
    unittest.main(verbosity=2)
