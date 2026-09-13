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


if __name__ == "__main__":
    unittest.main()
