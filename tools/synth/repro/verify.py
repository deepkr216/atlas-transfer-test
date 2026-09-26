"""
verify.py - build every reproduction under tools/synth/repro/<ID>/ into a
scratch index and check that the symptom is still there.

    python tools/synth/repro/verify.py [--out FOLDER] [ID ...]

Each folder holds `estate/` (the smallest members that show the finding)
and `expect.json`:

    {"title": "...", "steps": [...], "checks": [
        {"sql": "SELECT ...", "truth": <value>, "symptom": <value>},          # a scalar the index holds
        {"query": ["layout", "OUTER"], "truth_has": "...", "symptom_has": "..."},   # a report's text
        {"gate": "answer.md", "truth_rc": 0, "symptom_rc": 1}                  # the gate's verdict
    ]}

The index is built with --rebuild; `"recover": true` runs atlas.recover
and an incremental build after it. `steps`, when given, run after the first
build instead, in order - for a finding that shows only after the estate
changes between builds (a compiler listing that arrives, moves or goes):

    {"put": ["later/CLAIMS/X.lst", "estate/CLAIMS/X.lst"]}   # a file of the folder dropped into the estate
    {"move": ["estate/KVA/A.lst", "estate/SHARED/A.lst"]}    # within the estate
    {"remove": "estate/KVA/A.lst"}
    {"recover": true}                                        # atlas.recover over the index
    {"build": true}                                          # an incremental build

atlas.recover runs in the scratch folder, so its report (work\recover.md)
is written there, never into the repository.

A check prints REPRODUCED when the index gives the symptom, FIXED when it
gives the truth, and OTHER with what it gave. A check whose truth and
symptom are the same is a control - a fact the tool gave right before the
fix too (the program's own view beside the copybook's) - and prints FIXED
when the index still gives it: read first as the symptom, every control
stayed REPRODUCED after its finding was fixed. Standard library only; the
toolkit is run from the repository root as the owner runs it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PY = sys.executable


def run(cmd, cwd=REPO):
    env = dict(os.environ, PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""))
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def step(folder: str, work: str, db: str, st: dict) -> None:
    """One of expect.json's `steps` (see the module's notes)."""
    estate = os.path.join(work, "estate")
    if "put" in st:
        src, dst = (os.path.join(folder, *st["put"][0].split("/")), os.path.join(work, *st["put"][1].split("/")))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(src, dst)
    elif "move" in st:
        src, dst = (os.path.join(work, *p.split("/")) for p in st["move"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
    elif "remove" in st:
        os.remove(os.path.join(work, *st["remove"].split("/")))
    elif st.get("recover"):
        run([PY, "-m", "atlas.recover", "--db", db], cwd=work)
    elif st.get("build"):
        run([PY, "-m", "atlas.build", estate, "--db", db, "--quiet", "--no-current-file"])


def verify(folder: str, out: str) -> list:
    with open(os.path.join(folder, "expect.json"), encoding="utf-8") as fh:
        spec = json.load(fh)
    rid = os.path.basename(folder)
    work = os.path.join(out, rid)
    if os.path.isdir(work):
        shutil.rmtree(work)
    shutil.copytree(os.path.join(folder, "estate"), os.path.join(work, "estate"))
    db = os.path.join(work, "atlas.db")
    rc, outp = run([PY, "-m", "atlas.build", os.path.join(work, "estate"), "--db", db, "--rebuild", "--quiet", "--no-current-file"])
    results = []
    if rc:
        results.append((rid, "BUILD FAILED", outp[-300:]))
        return results
    if spec.get("recover"):
        run([PY, "-m", "atlas.recover", "--db", db], cwd=work)
        run([PY, "-m", "atlas.build", os.path.join(work, "estate"), "--db", db, "--quiet", "--no-current-file"])
    for st in spec.get("steps", ()):
        step(folder, work, db, st)
    conn = sqlite3.connect(db)
    for k, ck in enumerate(spec["checks"], 1):
        label = f"{rid} check {k}"
        if "sql" in ck:
            got = conn.execute(ck["sql"]).fetchall()
            got = [list(r) if len(r) > 1 else r[0] for r in got]
            got = got[0] if len(got) == 1 and ck.get("scalar", True) else got
            if got == ck["truth"] == ck["symptom"]:
                results.append((label, "FIXED", f"{got} (a control: right before the fix too)"))
            elif got == ck["symptom"]:
                results.append((label, "REPRODUCED", f"{got}"))
            elif got == ck["truth"]:
                results.append((label, "FIXED", f"{got}"))
            else:
                results.append((label, "OTHER", f"{got} (truth {ck['truth']}, symptom {ck['symptom']})"))
        elif "query" in ck:
            path = os.path.join(work, f"q{k}.md")
            run([PY, "-m", "atlas.query", "--db", db, "--out", path, *ck["query"]])
            text = ""
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            if "count" in ck:
                n = text.count(ck["count"])
                verdict = "REPRODUCED" if n == ck["symptom"] else "FIXED" if n == ck["truth"] else "OTHER"
                results.append((label, verdict, f"'{ck['count']}' x{n} (truth {ck['truth']}, symptom {ck['symptom']})"))
                continue
            if ck.get("truth_has") and ck["truth_has"] == ck.get("symptom_has") and ck["truth_has"] in text:
                results.append((label, "FIXED", f"{ck['truth_has']} (a control: right before the fix too)"))
            elif ck.get("symptom_has") and ck["symptom_has"] in text and not (ck.get("truth_has") and ck["truth_has"] in text):
                results.append((label, "REPRODUCED", ck["symptom_has"]))
            elif ck.get("truth_has") and ck["truth_has"] in text:
                results.append((label, "FIXED", ck["truth_has"]))
            elif ck.get("symptom_lacks") and ck["symptom_lacks"] not in text:
                results.append((label, "REPRODUCED", f"'{ck['symptom_lacks']}' absent"))
            else:
                results.append((label, "OTHER", text[:200].replace("\n", " / ")))
        elif "gate" in ck:
            ap = os.path.join(folder, ck["gate"])
            rc2, outp2 = run([PY, "-m", "atlas.verify_citations", ap, "--db", db])
            if rc2 == ck["symptom_rc"]:
                results.append((label, "REPRODUCED", outp2.strip().splitlines()[-1][:120]))
            elif rc2 == ck["truth_rc"]:
                results.append((label, "FIXED", outp2.strip().splitlines()[-1][:120]))
            else:
                results.append((label, "OTHER", f"rc {rc2}"))
    conn.close()
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "atlas-synth-repro"))
    a = ap.parse_args(argv)
    folders = sorted(d for d in os.listdir(HERE) if os.path.isdir(os.path.join(HERE, d)) and os.path.exists(os.path.join(HERE, d, "expect.json")))
    if a.ids:
        folders = [f for f in folders if f in a.ids]
    os.makedirs(a.out, exist_ok=True)
    n_rep = 0
    for f in folders:
        for label, verdict, detail in verify(os.path.join(HERE, f), a.out):
            print(f"{verdict:<11} {label}: {detail}")
            n_rep += verdict == "REPRODUCED"
    print(f"\n{len(folders)} reproduction folder(s); {n_rep} check(s) still show the symptom")
    return 0


if __name__ == "__main__":
    sys.exit(main())
