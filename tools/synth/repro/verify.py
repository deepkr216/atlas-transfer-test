"""
verify.py - build every reproduction under tools/synth/repro/<ID>/ into a
scratch index and check that the symptom is still there.

    python tools/synth/repro/verify.py [--out FOLDER] [ID ...]

Each folder holds `estate/` (the smallest members that show the finding)
and `expect.json`:

    {"title": "...", "checks": [
        {"sql": "SELECT ...", "truth": <value>, "symptom": <value>},          # a scalar the index holds
        {"query": ["layout", "OUTER"], "truth_has": "...", "symptom_has": "..."},   # a report's text
        {"gate": "answer.md", "truth_rc": 0, "symptom_rc": 1}                  # the gate's verdict
    ]}

A check prints REPRODUCED when the index gives the symptom, FIXED when it
gives the truth, and OTHER with what it gave. Standard library only; the
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
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def verify(folder: str, out: str) -> list:
    spec = json.load(open(os.path.join(folder, "expect.json"), encoding="utf-8"))
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
        run([PY, "-m", "atlas.recover", "--db", db])
        run([PY, "-m", "atlas.build", os.path.join(work, "estate"), "--db", db, "--quiet", "--no-current-file"])
    conn = sqlite3.connect(db)
    for k, ck in enumerate(spec["checks"], 1):
        label = f"{rid} check {k}"
        if "sql" in ck:
            got = conn.execute(ck["sql"]).fetchall()
            got = [list(r) if len(r) > 1 else r[0] for r in got]
            got = got[0] if len(got) == 1 and ck.get("scalar", True) else got
            if got == ck["symptom"]:
                results.append((label, "REPRODUCED", f"{got}"))
            elif got == ck["truth"]:
                results.append((label, "FIXED", f"{got}"))
            else:
                results.append((label, "OTHER", f"{got} (truth {ck['truth']}, symptom {ck['symptom']})"))
        elif "query" in ck:
            path = os.path.join(work, f"q{k}.md")
            run([PY, "-m", "atlas.query", "--db", db, "--out", path, *ck["query"]])
            text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            if "count" in ck:
                n = text.count(ck["count"])
                verdict = "REPRODUCED" if n == ck["symptom"] else "FIXED" if n == ck["truth"] else "OTHER"
                results.append((label, verdict, f"'{ck['count']}' x{n} (truth {ck['truth']}, symptom {ck['symptom']})"))
                continue
            if ck.get("symptom_has") and ck["symptom_has"] in text and not (ck.get("truth_has") and ck["truth_has"] in text):
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
