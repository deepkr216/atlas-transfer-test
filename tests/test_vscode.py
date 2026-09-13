"""
The VS Code loop (tasks -> work/*.md -> /atlas-* prompts -> gate):

  `--out FILE` on atlas.query and atlas.verify_citations writes UTF-8 with LF
  and creates the folder, independent of the shell (PowerShell 5.1 `>`
  writes UTF-16 - LESSONS 118); the gate still prints its report when it
  also writes it; every task in .vscode/tasks.json is well-formed, names a
  real query, declares every ${input:} it uses, and actually runs against
  the fixture index; every file a prompt attaches is one a task writes (or
  the user writes: abend.txt, answer.md); the Copilot contract names the
  gate and --out.
"""

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from atlas import build, query, verify_citations  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
TASKS = os.path.join(ROOT, ".vscode", "tasks.json")
PROMPTS = os.path.join(ROOT, ".github", "prompts")
CONTRACT = os.path.join(ROOT, ".github", "copilot-instructions.md")

# what the user types when a task prompts; fixture names where they exist,
# harmless names elsewhere (a report for an unknown name is still a report)
INPUTS = {"name": "SAMPPGM", "kind": "program", "budget": "12000", "field": "PM-POLICY-STATUS",
          "field2": "PM-POLICY-STATUS", "pattern": "ERROR", "literal": "E001", "job": "SAMPJOB",
          "program": "SAMPPGM", "copybook": "PMASTREC", "member": "SAMPPGM", "range": "1-9999",
          "paragraph": "100-MAIN", "document": "CLAIMSPEC"}
USER_WRITTEN = {"abend.txt", "answer.md"}


def _run_capture(fn, argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(argv)
    return rc, buf.getvalue()


class OutOption(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        for rel, src in (("SRC/SAMPPGM.cbl", "SAMPPGM.cbl"), ("JCLLIB/SAMPJOB.jcl", "SAMPJOB.jcl"),
                         ("COPYLIB/PMASTREC.cpy", "PMASTREC.cpy")):
            p = os.path.join(cls.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            shutil.copy(os.path.join(FIX, src), p)
        cls.db = os.path.join(cls.td, "t.db")
        build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_query_out_writes_utf8(self):
        out = os.path.join(self.td, "work", "nested", "pack.md")       # folder does not exist yet
        rc, shown = _run_capture(query._main, ["--db", self.db, "--out", out, "pack", "SAMPPGM"])
        self.assertEqual(rc, 0)
        self.assertIn("written", shown)
        self.assertNotIn("<!-- pack", shown, "the report went to the terminal instead of the file")
        raw = open(out, "rb").read()
        self.assertFalse(raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"), "UTF-16 BOM")
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM would confuse the gate")
        self.assertNotIn(b"\r\n", raw)
        text = raw.decode("utf-8")
        _rc, direct = _run_capture(query._main, ["--db", self.db, "pack", "SAMPPGM"])
        self.assertEqual(text, direct, "--out must write exactly what the terminal would show")
        self.assertTrue(text.startswith("<!-- pack program SAMPPGM"))

    def test_verify_out_writes_and_still_prints(self):
        ans = os.path.join(self.td, "work", "answer.md")
        os.makedirs(os.path.dirname(ans), exist_ok=True)
        with open(ans, "w", encoding="utf-8") as fh:
            fh.write('SAMPPGM calls VALIDATE [[SAMPPGM 27 "CALL \'VALIDATE\'"]].\n')
        gate = os.path.join(self.td, "work", "gate.txt")
        rc, shown = _run_capture(verify_citations.main, [ans, "--db", self.db, "--out", gate])
        self.assertEqual(rc, 0)
        self.assertIn("RESULT: citations verified", shown)
        written = open(gate, encoding="utf-8").read()
        self.assertEqual(written, shown)

    # ---- the tasks really run --------------------------------------------

    def _substitute(self, command):
        def sub(m):
            self.assertIn(m.group(1), INPUTS, f"task uses an input the test does not know: {m.group(1)}")
            return INPUTS[m.group(1)]
        c = re.sub(r"\$\{input:(\w+)\}", sub, command)
        c = c.replace("--db atlas.db", "--db " + shlex.quote(self.db))
        c = c.replace("work/", self.td.replace("\\", "/") + "/work/")
        return c

    def test_every_query_task_runs_against_the_fixture(self):
        tasks = json.load(open(TASKS, encoding="utf-8"))["tasks"]
        ran = 0
        for t in tasks:
            for piece in t["command"].split(";"):
                piece = piece.strip()
                if not piece.startswith("python -m atlas.query"):
                    continue
                argv = shlex.split(self._substitute(piece))[3:]           # drop python -m atlas.query
                rc, shown = _run_capture(query._main, argv)
                self.assertEqual(rc, 0, (t["label"], piece, shown))
                self.assertIn("written", shown, (t["label"], "task does not use --out"))
                out = argv[argv.index("--out") + 1]
                self.assertTrue(os.path.getsize(out) > 0, (t["label"], "empty report"))
                ran += 1
        self.assertGreaterEqual(ran, 14, "expected every query task to be exercised")

    def test_verify_task_runs_against_the_fixture(self):
        tasks = json.load(open(TASKS, encoding="utf-8"))["tasks"]
        t = next(t for t in tasks if "verify work/answer.md" in t["label"])
        ans = os.path.join(self.td, "work", "answer.md")
        os.makedirs(os.path.dirname(ans), exist_ok=True)
        with open(ans, "w", encoding="utf-8") as fh:
            fh.write('SAMPPGM calls VALIDATE [[SAMPPGM 27 "CALL \'VALIDATE\'"]].\n')
        argv = shlex.split(self._substitute(t["command"]))[3:]
        rc, shown = _run_capture(verify_citations.main, argv)
        self.assertEqual(rc, 0, shown)
        self.assertTrue(os.path.exists(os.path.join(self.td, "work", "gate.txt")))


class TasksAndPrompts(unittest.TestCase):

    def setUp(self):
        self.tasks = json.load(open(TASKS, encoding="utf-8"))
        self.outputs = set()
        for t in self.tasks["tasks"]:
            for m in re.finditer(r"--out (\S+)", t["command"]):
                self.outputs.add(m.group(1).split("/")[-1])

    def test_tasks_are_well_formed(self):
        inputs = {i["id"] for i in self.tasks["inputs"]}
        for t in self.tasks["tasks"]:
            self.assertTrue(t["label"].startswith("Atlas: "), t["label"])
            self.assertEqual(t["type"], "shell", t["label"])
            self.assertIn("detail", t, t["label"])
            for used in re.findall(r"\$\{input:(\w+)\}", t["command"]):
                self.assertIn(used, inputs, (t["label"], used))
            for piece in t["command"].split(";"):
                piece = piece.strip()
                m = re.match(r"python -m atlas\.(\w+)", piece)
                if m and m.group(1) == "query":
                    toks = shlex.split(piece.replace("${", "X").replace("}", ""))[3:]
                    i = 0
                    while i < len(toks) and toks[i] in ("--db", "--out"):   # global options first
                        i += 2
                    sub = toks[i]
                    self.assertTrue(hasattr(query, "cmd_" + sub) or sub in ("callers", "callees"),
                                    (t["label"], "unknown query", sub))
                    self.assertIn("--out", piece, (t["label"], "a report task must use --out"))
                    self.assertNotIn(">", piece, (t["label"], "never the shell's redirection"))
                elif m:
                    self.assertIn(m.group(1), ("verify_citations", "ui"), (t["label"], m.group(1)))
                else:
                    self.assertEqual(piece, "python selfcheck.py", (t["label"], piece))
            if ";" in t["command"]:
                self.assertEqual(t["options"]["shell"]["executable"], "powershell.exe",
                                 (t["label"], "a chained command must pin the shell"))
        self.assertIn("gate.txt", self.outputs)

    def test_prompts_attach_only_files_a_task_writes(self):
        files = sorted(f for f in os.listdir(PROMPTS) if f.endswith(".prompt.md"))
        self.assertGreaterEqual(len(files), 10)
        for f in files:
            text = open(os.path.join(PROMPTS, f), encoding="utf-8").read()
            self.assertTrue(text.startswith("---\n"), f)
            head = text.split("---", 2)[1]
            self.assertIn("mode:", head, f)
            self.assertIn("description:", head, f)
            links = re.findall(r"\]\(\.\./\.\./work/([^)]+)\)", text)
            if "mode: 'agent'" not in head:                     # an agent prompt writes its own reports
                self.assertTrue(links, (f, "an ask-mode prompt must attach at least one work/ file"))
            for name in links:
                self.assertTrue(name in self.outputs or name in USER_WRITTEN,
                                (f, name, "no task writes this file"))
            # the contract travels with every request: cite verbatim, keep the gaps visible
            self.assertIn("token", text, f)
            self.assertIn("verbatim", text, f)
            self.assertIn("UNVERIFIED", text, f)

    def test_contract_names_the_gate_and_out(self):
        text = open(CONTRACT, encoding="utf-8").read()
        for needle in ("verify_citations", "--out work/", "## FACTS", "## UNRESOLVED IN SCOPE",
                       "INSUFFICIENT EVIDENCE", "UNVERIFIED"):
            self.assertIn(needle, text, needle)


if __name__ == "__main__":
    unittest.main()
