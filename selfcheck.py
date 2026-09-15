"""
selfcheck.py - prove the toolkit works before trusting it or copying it.

    python selfcheck.py

Runs: the regression suite; a smoke build over tests/fixtures; a handful of
queries with expected content; and the citation gate on a known-good and a
known-bad answer. Exit 0 only if every stage passes. No model calls.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run(args, **kw):
    return subprocess.run([PY, *args], cwd=HERE, capture_output=True, text=True, **kw)


def stage(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""))
    return ok


def failure_summary(text: str, limit: int = 12) -> list:
    """`FAIL: test (module.Class.test)` and the first exception line under it."""
    import re
    exc = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Failure|Warning)\b")
    out = []
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if not ln.startswith(("FAIL: ", "ERROR: ")):
            continue
        out.append(ln.strip())
        for nxt in lines[i + 1:i + 80]:
            if nxt.startswith(("FAIL: ", "ERROR: ")):
                break
            if exc.match(nxt.strip()):
                out.append("  " + nxt.strip()[:300])
                break
        if len(out) >= limit * 2:
            break
    return out


def main() -> int:
    all_ok = True

    r = run(["-m", "unittest", "discover", "-s", "tests"])
    tail = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else ""
    all_ok &= stage("regression suite", r.returncode == 0, tail)
    if r.returncode != 0:
        # the name of each failing test and the line that says why - test names and toolkit
        # messages only, nothing from an estate: safe to paste
        for line in failure_summary((r.stderr or "") + "\n" + (r.stdout or "")):
            print("      " + line)

    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "smoke.db")
        r = run(["-m", "atlas.build", os.path.join(HERE, "tests", "fixtures"), "--db", db, "--rebuild", "--quiet"])
        all_ok &= stage("smoke build", r.returncode == 0 and "programs 7" in r.stdout,
                        "" if r.returncode == 0 else (r.stderr.strip().splitlines() or ["?"])[-1])

        checks = [
            (["program", "SAMPPGM"], ["PROD.POLICY.MASTER.KSDS", "open_verb", "ADJUSTER, RATECALC", "SAMPPGM:27"]),
            (["job", "SAMPJOB"], ["runs **PREMCALC**", "runs **CLMPOST**", "gdg_relative", "INCLUDE 25-26"]),
            (["literal", "E001"], ["move_to", "ERRPGM:30", "compare", "ERRLOG"]),
            (["field", "PM-POLICY-STATUS"], ["| 12 | 2 |", "PM-ACTIVE", "SAMPPGM"]),
            (["copybook", "PMASTREC"], ["SAMPPGM", "PROD.POLICY.EXTRACT", "*SORT*"]),
            (["callees", "SAMPPGM"], ["VALIDATE", "RATECALC", "NOT in index"]),
            (["coverage"], ["POLDCL", "scheduler definitions: 0"]),
            (["values", "WS-REL-CD"], ["NOT DOCUMENTED BY ANY 88-LEVEL", "WS-REL-SON", "MEMBRVAL:33"]),
            (["values", "WS-GENDER-CD"], ["via 88 WS-GENDER-MALE", "GENDER_CD"]),
            (["pair", "WS-REL-CD", "WS-GENDER-CD"], ["MEMBRVAL:22", "MEMBRVAL:37"]),
            (["messages", "GENDER"], ["GENDER MUST BE MALE FOR SON", "MEMBRVAL:29", "RELATIONSHIP/GENDER MISMATCH"]),
            (["screen", "MEMMAP"], ["| GENDER |", "GENDERI", "CONDLOGX:40"]),
            (["screen", "MEMFIP"], ["next MEMFOP", "| 5/1 |"]),
            (["values", "GENDER"], ["Screen fields", "| U |"]),
            (["transaction", "MEMB"], ["CONDLOGX", "MEMBRVAL", "ims_dc"]),
            (["program", "CONDLOGX"], ["Online: MEMB (cics)"]),
            (["job", "NIGHTJOB"], ["NIGHT.PS010", "PROD.CLM.MASTER", "runs **CLMRPT**", "PROD.CLM.NEW.KSDS", "CLMSUB"]),
            (["column", "MEMBER_TBL.GENDER_CD"], ["SQLCOLS", "WS-GENDER-CD", "Written"]),
            (["messages", "INVALID GENDER"], ["INVALID GENDER <WS-GENDER-CD> FOR MEMBER <WS-MEMBER-ID>"]),
            (["walk", "WALKPGM"], ["0000-MAIN  - entry", "<- PERFORM from 0000-MAIN", "| 1500-UNUSED | paragraph |",
                                   "WALKPROC:1-2 (via COPY WALKPROC)", "Entry point `WALKENT`"]),
            (["diff", "WALKPGM"], ["a diff needs two"]),                 # one copy in the fixtures: says so, no crash
            (["diff"], ["no member has two copies"]),
        ]
        for args, needles in checks:
            r = run(["-m", "atlas.query", "--db", db, *args])
            missing = [n for n in needles if n not in r.stdout]
            all_ok &= stage(f"query {' '.join(args)}", r.returncode == 0 and not missing,
                            f"missing {missing}" if missing else (r.stderr.strip()[-200:] if r.returncode else ""))

        good = os.path.join(td, "good.md")
        bad = os.path.join(td, "bad.md")
        with open(good, "w", encoding="utf-8") as fh:
            fh.write("SAMPPGM calls VALIDATE [[SAMPPGM 27 \"CALL 'VALIDATE'\"]] and runs in "
                     "STEP010 [[SAMPJOB 6 \"PGM=SAMPPGM\"]].\n")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("SAMPPGM calls FOO [[SAMPPGM 27 \"CALL 'FOO'\"]].\n")
        r = run(["-m", "atlas.verify_citations", good, "--db", db])
        all_ok &= stage("citation gate accepts a true answer", r.returncode == 0 and "verified" in r.stdout)
        r = run(["-m", "atlas.verify_citations", bad, "--db", db])
        all_ok &= stage("citation gate rejects a false answer", r.returncode == 1 and "REJECT" in r.stdout)

    print("\nSELFCHECK " + ("PASSED" if all_ok else "FAILED"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
