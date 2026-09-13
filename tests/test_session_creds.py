"""
The mainframe password (LESSONS 128): run by hand, zowe prompts for it and
works; run by the toolkit it cannot prompt, so a fetch used to create the
folders and download nothing - silently, or after waiting for the timeout.

  Runner closes stdin (a prompt fails at once) and passes the session
  credentials to the subprocess through ZOWE_OPT_USER / ZOWE_OPT_PASSWORD;
  a failed download whose output mentions a password says what zowe wanted
  and what to do; the password never reaches the log, the result message
  or sources.json; --ask-password sets it for one run.
"""

import io
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import fetch  # noqa: E402

SECRET = "S3cret-Pw!"


class SessionCredentials(unittest.TestCase):

    def tearDown(self):
        fetch.set_session_credentials(None, None)

    def test_env_only_while_set(self):
        self.assertFalse(fetch.has_session_credentials())
        self.assertNotIn("ZOWE_OPT_PASSWORD", fetch.session_env())
        fetch.set_session_credentials("DEEPAK", SECRET)
        env = fetch.session_env()
        self.assertEqual(env["ZOWE_OPT_USER"], "DEEPAK")
        self.assertEqual(env["ZOWE_OPT_PASSWORD"], SECRET)
        self.assertNotIn("ZOWE_OPT_PASSWORD", os.environ)          # the toolkit's own process is untouched
        fetch.set_session_credentials(None, None)
        self.assertNotIn("ZOWE_OPT_PASSWORD", fetch.session_env())

    def test_credentials_travel_as_options_too_and_the_daemon_is_off(self):
        """Explicit --user/--password reach every Zowe version and mode; the
        log never shows the value; the daemon is bypassed for our subprocess."""
        fetch.set_session_credentials("DEEPAK", SECRET)
        cfg = fetch.load_config(os.path.join(tempfile.mkdtemp(), "nope.json"))
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"):
            cmd = fetch.list_members_cmd(cfg, "PROD.X.SRC")
            dl = fetch.download_cmd(cfg, fetch.new_source("PROD.X.SRC", "cobol"))
        for c in (cmd, dl):
            self.assertEqual(c[c.index("--user") + 1], "DEEPAK")
            self.assertEqual(c[c.index("--password") + 1], SECRET)
            self.assertNotIn(SECRET, fetch.redact_cmd(c))
            self.assertIn("--password <redacted>", fetch.redact_cmd(c))
        self.assertEqual(fetch.session_env()["ZOWE_USE_DAEMON"], "no")
        fetch.set_session_credentials(None, None)
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"):
            self.assertNotIn("--password", fetch.list_members_cmd(cfg, "PROD.X.SRC"))
        self.assertEqual(fetch.session_env()["ZOWE_USE_DAEMON"], "no")

    def test_runner_passes_the_env_and_closes_stdin(self):
        fetch.set_session_credentials("DEEPAK", SECRET)
        rc, out, _err = fetch.Runner(30).run([sys.executable, "-c",
                                              "import os,sys;print(os.environ.get('ZOWE_OPT_PASSWORD','-'));"
                                              "print('stdin=%d' % len(sys.stdin.read()))"])
        self.assertEqual(rc, 0)
        self.assertIn(SECRET, out)
        self.assertIn("stdin=0", out)                                # a prompt cannot wait for a human

    def test_failed_download_says_zowe_wanted_a_password_and_never_leaks_it(self):
        fetch.set_session_credentials("DEEPAK", SECRET)
        td = tempfile.mkdtemp()
        cfg = fetch.load_config(os.path.join(td, "nope.json"))
        cfg["local_root"] = td
        src = fetch.new_source("PROD.X.SRC", "cobol")
        lines = []

        class Prompting:
            def run(self, cmd):
                return 1, "", "Enter password: \nCommand Error: no value specified for password"
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"):
            res = fetch.fetch_source(cfg, src, runner=Prompting(), log=lines.append)
        self.assertFalse(res.ok)
        self.assertIn("zowe wanted a password", res.message)
        self.assertIn("zowe config secure", res.message)
        self.assertIn("--ask-password", res.message)
        joined = "\n".join(lines) + res.message + " ".join(res.command) + (src.get("last_result") or "")
        self.assertNotIn(SECRET, joined)
        self.assertEqual(fetch.password_hint(0, "password ok", ""), "")   # only on failure

    def test_ask_password_on_the_command_line(self):
        td = tempfile.mkdtemp()
        cfgp = os.path.join(td, "sources.json")
        cfg = fetch.load_config(cfgp)
        cfg["sources"] = [fetch.new_source("PROD.X.SRC", "cobol")]
        fetch.save_config(cfg, cfgp)
        with mock.patch("getpass.getpass", return_value=SECRET), \
                mock.patch("atlas.fetch.zowe_exe", return_value="zowe"), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = fetch.main(["--config", cfgp, "--ask-password", "--user", "DEEPAK", "--plan"])
        self.assertEqual(rc, 0)
        self.assertTrue(fetch.has_session_credentials())
        self.assertEqual(fetch.session_env()["ZOWE_OPT_USER"], "DEEPAK")
        self.assertNotIn(SECRET, out.getvalue())
        with open(cfgp, encoding="utf-8") as fh:
            self.assertNotIn(SECRET, fh.read())                      # never written to sources.json


class StagedCheck(unittest.TestCase):
    """Check Zowe says which of three stages fails: found, runs, host answers."""

    def _cfg(self):
        cfg = fetch.load_config(os.path.join(tempfile.mkdtemp(), "nope.json"))
        cfg["sources"] = [fetch.new_source("PROD.X.SRC", "cobol")]
        return cfg

    class Fake:
        def __init__(self, list_rc=0, list_err=""):
            self.list_rc, self.list_err = list_rc, list_err

        def run(self, cmd):
            if "--version" in cmd:
                return 0, "7.18.0\n", ""
            if "list" in cmd:
                return self.list_rc, ('{"success":true,"data":{"apiResponse":{"items":[{"member":"A"},{"member":"B"}]}}}'
                                      if self.list_rc == 0 else ""), self.list_err
            return 1, "", "unexpected"

    def test_all_three_stages_pass(self):
        with mock.patch("atlas.fetch.zowe_exe", return_value=r"C:\\npm\\zowe.cmd"):
            ok, msg = fetch.check_zowe(self._cfg(), runner=self.Fake())
        self.assertTrue(ok, msg)
        self.assertIn("1. zowe found: C:\\\\npm\\\\zowe.cmd", msg)
        self.assertIn("2. zowe --version: 7.18.0", msg)
        self.assertIn("3. host answered: PROD.X.SRC has 2 member(s)", msg)

    def test_host_refuses_with_password_prompt(self):
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"):
            ok, msg = fetch.check_zowe(self._cfg(), runner=self.Fake(1, "Enter password: \nCommand Error: password required"))
        self.assertFalse(ok)
        self.assertIn("3. the host did NOT answer", msg)
        self.assertIn("zowe wanted a password", msg)

    def test_host_command_hangs_on_the_password_prompt(self):
        """The real symptom: stages 1-2 pass, the first command that logs on
        never comes back (zowe put up its password prompt). The check must
        name it, not sit there."""
        class Hanging(self.Fake):
            def run(self, cmd):
                if "list" in cmd:
                    return 124, "", "timed out after 120s"
                return super().run(cmd)
        fetch.set_session_credentials(None, None)
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"):
            ok, msg = fetch.check_zowe(self._cfg(), runner=Hanging())
        self.assertFalse(ok)
        self.assertIn("did NOT answer", msg)
        self.assertIn("waiting for the mainframe password", msg)
        self.assertIn("--ask-password", msg)
        self.assertIn("zowe config secure", msg)
        # with a session password the same silence is the HOST prompt (the plain CLI
        # not seeing the profile) - the confirmed case on the work laptop
        fetch.set_session_credentials("DEEPAK", "pw")
        try:
            self.assertIn("HOST NAME", fetch.password_hint(124, "", "timed out"))
            self.assertIn("extra_args", fetch.password_hint(124, "", "timed out"))
        finally:
            fetch.set_session_credentials(None, None)
        # and when zowe manages to print the prompt, it is named directly
        self.assertIn("HOST NAME", fetch.password_hint(1, "Enter the host name of your service:", ""))

    def test_host_refuses_for_another_reason(self):
        with mock.patch("atlas.fetch.zowe_exe", return_value="zowe"):
            ok, msg = fetch.check_zowe(self._cfg(), runner=self.Fake(1, "Error: self signed certificate in certificate chain"))
        self.assertFalse(ok)
        self.assertIn("self signed certificate", msg)
        self.assertIn("run it by hand in your window and compare", msg)


if __name__ == "__main__":
    unittest.main()
