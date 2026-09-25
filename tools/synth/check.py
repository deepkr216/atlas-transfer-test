"""
check.py - the comparison: generate the synthetic estate, build it, change a
copybook and build again, drop in the copybook that "arrives", run
atlas.recover over the listings, build once more, then run EVERY query
command over the samples and compare each answer with the ground truth,
at two levels:

  * the fact tables (sqlite) - every truth fact must be a row, every row
    must be a truth fact (missed / invented), offsets and lines exact;
  * the reports (markdown) - every truth fact the report should print is
    printed, every name the report prints is a name the estate has, every
    cite points at a line holding the token, the deliberate defects are
    reported in the words LESSONS 181-192 promised, coverage's counts add up,
    the gate passes a pack's citations and fails a wrong one.

Findings are written as docs/SYNTH-findings-<date>.md, one row per finding,
grouped by module and ranked by facts affected, with a minimal reproduction
under tools/synth/repro/. Run:

    python tools/synth/check.py --out <scratch folder> [--scale 12] [--seed N]

Standard library only. The toolkit is run as subprocesses (python -m
atlas.build / recover / query / verify_citations) from the repository root,
exactly as the owner runs it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PY = sys.executable

# words the reports print in capitals that are not estate names: never "invented"
VOCAB = set("""
A ABEND ACCEPT ACCESS ACTIVE ADD ADDRESS AFTER ALL ALTER AND ANY ARE AS ASC ASKIP AT ATTRB BEFORE BINARY BLKSIZE BMP BMS BOTH BRT BSAM BY
C CALL CALL-USING CATLG CBLTDLI CH CHAR CHKP CHNG CICS CLASS CLOSE CLUSTER COBOL COMMAREA COMP COMP-3 COMP-5 COMPUTE COND CONTINUE COPY COPYLIB
CORRESPONDING CR CROSS CRUD CSD CURRENT CURSOR D DATA DATABASE DATE DB DB2 DB2P DCB DCLGEN DD DDL DEBUG DECIMAL DECLARE DEFINE DELETE DEPENDING
DFHAID DFHCOMMAREA DFHMDF DFHMDI DFHMSD DFHRESP DFSRRC00 DISP DISPLAY DIVISION DLET DLI DLIBATCH DO DSN DSNAME DSNTIAR DSNTIAUL DSNUTILB DUMMY
DYNAMIC E EJECT ELSE END END-EVALUATE END-EXEC END-IF END-READ END-REWRITE END-WRITE ENTRY EQ ERASE EVALUATE EXEC EXIT EXP EXTEND EXTRA
F FALSE FB FD FETCH FIELDS FILE FILLER FINAL FIRST FMT FOR FROM FSET FTP FULL FUNCTION G GB GDG GE GIVING GN GNP GO GOBACK GSAM GT GU
HDAM HIDAM HLQ I I-O IDCAMS IEBGENER IEFBR14 IF IKJEFT01 ILBOABN0 IMS IN INCLUDE INDEX INDEXED INFILE INITIAL INITIALIZE INPUT INSERT INSPECT
INTDR INTO INVALID IO IS ISRT JCL JCLLIB JOB JOBLIB KEY KEYLEN KEYS KSDS LABEL LANG LANGUAGE LE LEADING LENGTH LESS LIB LIMIT LINE LINK LINKAGE
LIST LOAD LOG LOW-VALUES LRECL LS LT MAIN MAINONLY MAP MAPONLY MAPSET MERGE MOD MODE MOVE MPP MSGCLASS N NE NEW NEXT NO NOT NOT-FOUND
NULL NUM NUMERIC OCCURS OF OK OLD OMIT ON OPEN OR ORDER OTHER OUT OUTDATASET OUTPUT OUTREC OVERRIDE PARM PASS PCB PD PEND PERFORM PGM PGMTYPE
PIC PICIN PICOUT PLAN POS PROC PROCEDURE PROCOPT PROCSEQ PROGRAM PROGRAM-ID PROV PSB PSBGEN PUT QUEUE RC READ READQ RECEIVE RECORD RECORDSIZE
REDEFINES REGION RELATE REMOTESYSTEM REPL REPLACING REPRO RESP RETURN RETURNING REWRITE RIDFLD RLSE RO RUN S SECTION SEGM SEGMENT SELECT SEND
SENSEG SEPARATE SEQ SEQUENTIAL SET SHR SIGN SIZE SORT SORTIN SORTOUT SPACE SPACES SQL SQLCA SQLCODE SQLDA START STATUS STEP STEPLIB STOP STRING
SUB SUM SYSDA SYSIN SYSOUT SYSPRINT SYSPUNCH SYSREC SYSREC00 SYSTSIN SYSTSPRT SYSUT1 SYSUT2 TD TDQ TDQUEUE THEN THRU TIMES TIMESTAMP TO TP TRANSACT
TRANSACTION TRANSID TRK TRUE TS TSQ TWASIZE U UNIT UNPROT UNRESOLVED UNTIL UP UPDATE USAGE USING V VALUE VALUES VARCHAR VARYING VB VERIFY
WHEN WHERE WITH WORKING-STORAGE WRITE WRITEQ X XCTL XREF Y YES ZERO ZEROS ZEROES ZZ ZD BI FI CSECT DSECT ASM MFS REXX
POLICY CLAIMS BILLING SHARED PROD STG TEST DOCS RECOVERED-COPYBOOKS EBCDIC ASCII UTF-8 CRLF LF README ROADMAP LESSONS FACTS INFERENCE
INTRDR CNTL PARMLIB PROCLIB LOADLIB JCLLIB COPYBOOK COPYBOOKS PLUS MINUS MAX MIN ODO CICSCSD1 IMSGEN1 EIBCALEN EIBAID DFHCICST ENABLED
NORMAL LTERM MOD MID FOP FIP OSVS INTEGER-OF-DATE SMALLINT INTEGER CMPAT IOPCB ALT ULU DBB IFP JBP JMP GHU GHN GHNP ROLB XRST
""".split())


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def run(cmd: Sequence[str], cwd: Optional[str] = None, timeout: int = 1800) -> Tuple[int, str, float]:
    t0 = time.time()
    p = subprocess.run(list(cmd), cwd=cwd or REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or ""), time.time() - t0


def read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def md_tables(text: str) -> List[Tuple[str, List[str], List[List[str]]]]:
    """[(nearest heading above, header cells, rows)] for every markdown table."""
    out = []
    heading = ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#"):
            heading = ln.lstrip("#").strip()
        if ln.startswith("|") and i + 1 < len(lines) and re.match(r"^\|\s*-+", lines[i + 1]):
            header = [c.strip() for c in ln.strip().strip("|").split("|")]
            rows = []
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append((heading, header, rows))
            continue
        i += 1
    return out


def section(text: str, title_start: str) -> str:
    """The text under the first heading starting with title_start, up to the next heading of the same or higher level."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^(#+)\s*(.*)$", ln)
        if m and m.group(2).startswith(title_start):
            level = len(m.group(1))
            j = i + 1
            while j < len(lines):
                m2 = re.match(r"^(#+)\s", lines[j])
                if m2 and len(m2.group(1)) <= level:
                    break
                j += 1
            return "\n".join(lines[i:j])
    return ""


CITE = re.compile(r"(?:(?P<sys>[A-Z0-9-]+)/)?(?P<mem>[A-Z0-9@#$.-]+?)(?:\((?P<kind>[a-z]+)\))?:(?P<line>\d+)")


def cites_in(text: str) -> List[Tuple[str, int]]:
    return [(m.group("mem"), int(m.group("line"))) for m in CITE.finditer(text)]


class Finding:
    def __init__(self, module: str, command: str, subject: str, truth: str, tool: str, facts: int = 1,
                 severity: str = "wrong", note: str = "", fid: str = "") -> None:
        self.module, self.command, self.subject, self.truth, self.tool = module, command, subject, truth, tool
        self.facts, self.severity, self.note, self.fid = facts, severity, note, fid
        self.repro = ""

    def key(self) -> str:
        return f"{self.module}|{self.command}|{self.subject}|{self.truth[:60]}"


# --------------------------------------------------------------------------
# the checker
# --------------------------------------------------------------------------

class Checker:
    def __init__(self, out: str, scale: int, seed: int, report: str, repro_dir: str) -> None:
        self.out = os.path.abspath(out)
        self.scale, self.seed = scale, seed
        self.report_path = report
        self.repro_dir = repro_dir
        self.db = os.path.join(self.out, "atlas.db")
        self.qdir = os.path.join(self.out, "queries")
        self.findings: List[Finding] = []
        self.matched: Dict[str, int] = Counter()
        self.timing: Dict[str, object] = {}
        self.cmd_times: List[Tuple[str, float]] = []
        self.log_lines: List[str] = []
        self.truth: dict = {}
        self.unknown_names: Counter = Counter()
        self.unknown_where: Dict[str, str] = {}
        self.documented: List[Finding] = []

    def log(self, msg: str) -> None:
        print(msg, flush=True)
        self.log_lines.append(msg)

    def find(self, module: str, command: str, subject: str, truth: str, tool: str, facts: int = 1,
             severity: str = "wrong", note: str = "", fid: str = "") -> Finding:
        f = Finding(module, command, subject, truth, tool, facts, severity, note, fid)
        self.findings.append(f)
        return f

    def ok(self, what: str, n: int = 1) -> None:
        self.matched[what] += n

    # ------------------------------------------------------------ driving
    def generate(self) -> None:
        # the scratch folder is cleared only when it is a folder THIS tool made (it holds truth.json):
        # never the repository, never an unrelated folder
        if os.path.isdir(self.out):
            marker = os.path.join(self.out, "truth.json")
            inside_repo = os.path.normcase(self.out).startswith(os.path.normcase(REPO)) or os.path.normcase(REPO).startswith(os.path.normcase(self.out))
            if not os.path.isfile(marker) or inside_repo:
                raise SystemExit(f"refusing to clear {self.out}: not a synthetic-estate folder made by generate.py (no truth.json), "
                                 "or inside the repository - give an empty scratch folder")
            shutil.rmtree(self.out)
        rc, outp, secs = run([PY, os.path.join(HERE, "generate.py"), "--out", self.out, "--seed", str(self.seed), "--scale", str(self.scale)])
        if rc:
            raise SystemExit("generator failed:\n" + outp)
        self.log(outp.strip())
        self.truth = json.load(open(os.path.join(self.out, "truth.json"), encoding="utf-8"))
        self.timing["generate_s"] = round(secs, 2)

    def build(self, label: str, rebuild: bool = False, db: Optional[str] = None) -> str:
        cmd = [PY, "-m", "atlas.build", self.truth["root"], "--db", db or self.db, "--manifest", self.truth["manifest"],
               "--no-current-file"]
        if rebuild:
            cmd.append("--rebuild")
        rc, outp, secs = run(cmd)
        with open(os.path.join(self.out, f"build-{label}.log"), "w", encoding="utf-8") as fh:
            fh.write(outp)
        if rc:
            self.find("build", f"build {label}", "the build", "exit 0", f"exit {rc}: " + outp[-500:], facts=999, severity="crash")
        members = len(self.truth["members"])
        m = re.search(r"parsing done: ([\d,]+) member\(s\) in (\d+):(\d+):(\d+)", outp)
        parse_s = None
        if m:
            parse_s = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
        m2 = re.search(r"== this run: new (\d+)\s+changed (\d+)\s+unchanged (\d+)\s+pruned (\d+)", outp)
        self.timing[f"build_{label}"] = {"wall_s": round(secs, 2), "members": members,
                                         "members_per_s": round(members / secs, 1) if secs else None,
                                         "parse_phase_s": parse_s,
                                         "run": {"new": int(m2.group(1)), "changed": int(m2.group(2)), "unchanged": int(m2.group(3)),
                                                 "pruned": int(m2.group(4))} if m2 else None}
        self.log(f"build {label}: {secs:.1f}s for {members} members = {members / secs:.0f} members/s"
                 + (f"; this run {m2.group(0)}" if m2 else ""))
        return outp

    def query(self, *args: str, label: Optional[str] = None) -> str:
        os.makedirs(self.qdir, exist_ok=True)
        name = label or re.sub(r"[^A-Za-z0-9._-]+", "_", " ".join(args))[:120]
        path = os.path.join(self.qdir, name + ".md")
        rc, outp, secs = run([PY, "-m", "atlas.query", "--db", self.db, "--out", path, *args])
        self.cmd_times.append((" ".join(args), secs))
        if rc or not os.path.exists(path):
            self.find("query", args[0], " ".join(args), "a report", f"exit {rc}: {outp[-400:]}", facts=50, severity="crash")
            return ""
        return read(path)

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    # ------------------------------------------------------------ phase 1: builds
    def phase_builds(self) -> None:
        self.build("rebuild", rebuild=True)
        self.snapshot_before = {"coverage": self.query("coverage", "--all", label="coverage-before-recover"),
                                "program_POLUPD01": self.query("program", "POLUPD01", label="program-POLUPD01-before")}
        # the incremental build: one copybook changed (a field added before its FILLER), every program that
        # expands it must be parsed again and nothing else
        t = self.truth
        cb = t["copybooks"]["CLMTRANR"]
        path = cb["path"]
        text = read(path)
        lines = text.splitlines()
        idx = next(i for i, ln in enumerate(lines) if "CT-FILLER" in ln)
        lines.insert(idx, lines[idx][:7] + "    05  CT-NEW-CHANNEL-CD       PIC X(03).")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        c = self.conn()
        before = {r["name"]: (r["scanned_at"], r["parse_status"]) for r in c.execute("SELECT name, scanned_at, parse_status FROM member WHERE kind='cobol'")}
        exp_before = {r["program_id"]: r["exp_lines"] for r in c.execute("SELECT program_id, exp_lines FROM program")}
        c.close()
        time.sleep(1.1)
        outp = self.build("incremental")
        copiers = sorted({p["name"] for p in t["programs"].values() if any(cp["copybook"] == "CLMTRANR" for cp in p["copies"])})
        c = self.conn()
        after = {r["name"]: (r["scanned_at"], r["parse_status"]) for r in c.execute("SELECT name, scanned_at, parse_status FROM member WHERE kind='cobol'")}
        exp_after = {r["program_id"]: r["exp_lines"] for r in c.execute("SELECT program_id, exp_lines FROM program")}
        newf = c.execute("SELECT COUNT(*) FROM field f JOIN member m ON m.id=f.member_id WHERE m.name='CLMTRANR' AND f.name='CT-NEW-CHANNEL-CD'").fetchone()[0]
        pf = {r[0] for r in c.execute("SELECT DISTINCT p.program_id FROM pfield f JOIN program p ON p.id=f.program_id WHERE f.name='CT-NEW-CHANNEL-CD'")}
        c.close()
        if newf != 1:
            self.find("build", "build (incremental)", "CLMTRANR", "the new field CT-NEW-CHANNEL-CD in the copybook's rows", f"{newf} rows", facts=1)
        else:
            self.ok("incremental: changed copybook re-parsed")
        for pgm in copiers:
            if pgm not in pf:
                self.find("build", "build (incremental)", pgm, f"re-parsed with the new field (copies CLMTRANR)", "the program's fields do not hold CT-NEW-CHANNEL-CD", facts=1)
            else:
                self.ok("incremental: copier re-parsed")
        untouched = [n for n in before if n not in copiers and n in after and after[n][0] != before[n][0]]
        if untouched:
            self.find("build", "build (incremental)", ", ".join(untouched[:8]), "not parsed again (they do not copy CLMTRANR)", "parsed again", facts=len(untouched), severity="waste")
        else:
            self.ok("incremental: untouched programs kept", len(before) - len(copiers))
        self.incremental = {"copybook": "CLMTRANR", "copiers": copiers}
        # the copybook that arrives after the build
        arr = t["arrived"]
        os.makedirs(os.path.dirname(arr["path"]), exist_ok=True)
        with open(arr["path"], "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(arr["lines"]) + "\n")

    # ------------------------------------------------------------ phase 2: recover
    def phase_recover(self) -> None:
        t = self.truth
        rc, outp, secs = run([PY, "-m", "atlas.recover", "--db", self.db, "--from", t["listings_dir"]], cwd=REPO)
        self.timing["recover_s"] = round(secs, 2)
        with open(os.path.join(self.out, "recover-console.txt"), "w", encoding="utf-8") as fh:
            fh.write(outp)
        self.recover_console = outp
        rep_path = os.path.join(REPO, "work", "recover.md")
        if os.path.exists(rep_path):
            shutil.copy(rep_path, os.path.join(self.out, "recover-report.md"))
        self.recover_report = read(rep_path) if os.path.exists(rep_path) else ""
        if rc:
            self.find("recover", "recover", "the run", "exit 0", f"exit {rc}: {outp[-600:]}", facts=999, severity="crash")
        # what it wrote: only POLMISSB
        rec_dir = os.path.join(t["root"], "SHARED", "RECOVERED-COPYBOOKS")
        written = sorted(f for f in os.listdir(rec_dir) if f.lower().endswith(".cpy")) if os.path.isdir(rec_dir) else []
        if written != ["POLMISSB.cpy"]:
            self.find("recover", "recover", "written copybooks", "exactly POLMISSB.cpy (the only missing copybook the listings hold)",
                      ", ".join(written) or "nothing", facts=1 + len([w for w in written if w != "POLMISSB.cpy"]))
        else:
            self.ok("recover: wrote only the missing copybook")
            # its text must be the copybook's own (the listing holds it verbatim)
            got = [ln.rstrip() for ln in read(os.path.join(rec_dir, "POLMISSB.cpy")).splitlines()]
            want = [ln.rstrip() for ln in t["copybooks"]["POLMISSB"]["fields"] and self._missing_text()]
            body_got = [ln[6:72].rstrip() for ln in got if not ln.lstrip().startswith("*") and ln[6:7] != "*"]
            body_want = [ln[6:72].rstrip() for ln in want if ln[6:7] != "*"]
            if [x for x in body_got if x.strip()] != [x for x in body_want if x.strip()]:
                self.find("recover", "recover", "POLMISSB", "the copybook's text as the listings print it",
                          "a different text: " + " / ".join(body_got[:3]), facts=len(body_want))
            else:
                self.ok("recover: recovered text exact", len(body_want))
        # re-filed: nothing on an index this toolkit built - D3 and D4 are copybooks by their own lines since ROADMAP
        # re-parse items 20 and 22 (phase_aged checks the re-file on an index built before them)
        c = self.conn()
        refiled = sorted(r[0] for r in c.execute("SELECT name FROM member WHERE parse_error LIKE 're-filed as copybook by atlas.recover%'"))
        pending = sorted(r[0] for r in c.execute("SELECT name FROM member WHERE parse_status='pending'"))
        c.close()
        if refiled:
            self.find("recover", "recover", "re-filed members", "none: D3 and D4 are copybooks by their own lines (ROADMAP re-parse "
                      "items 20 and 22)", ", ".join(refiled), facts=len(refiled))
        else:
            self.ok("recover: nothing re-filed on an index this toolkit built")
        want_pending = sorted({p["name"] for p in t["programs"].values()
                               if any(cp["copybook"] in refiled for cp in p["copies"])})
        missing_mark = [p for p in want_pending if p not in pending]
        extra_mark = [p for p in pending if p not in want_pending]
        if missing_mark:
            self.find("recover", "recover", ", ".join(missing_mark[:10]), "marked pending for the next build (they copy a re-filed, recovered or arrived copybook)",
                      "not marked", facts=len(missing_mark))
        if extra_mark:
            self.find("recover", "recover", ", ".join(extra_mark[:10]), "left alone (they copy nothing that changed)", "marked pending", facts=len(extra_mark))
        if not missing_mark and not extra_mark:
            self.ok("recover: marked exactly the programs that need the build", len(want_pending))
        # the words
        d = t["defects"]
        for w in d["D2"]["recover_words"] + d["D3"]["recover_words"] + d["D4"]["recover_words"] + d["D5"]["recover_words"] + d["D6"]["recover_words"] + d["ARRIVED"]["recover_words"]:
            where = self.recover_console + "\n" + self.recover_report
            if w in where:
                self.ok("recover: sentence present")
            else:
                self.find("recover", "recover", "console / work\\recover.md", f"the sentence `{w}`", "absent", facts=1, severity="words")
        for w in ("re-filed as copybook", "## Re-filed as copybook", "filed as asm", "filed as proc"):
            if w in self.recover_console + "\n" + self.recover_report:
                self.find("recover", "recover", "console / work\\recover.md", f"no `{w}` - nothing is misfiled on an index this "
                          "toolkit built (ROADMAP re-parse items 20 and 22)", "present", facts=1, severity="words")
            else:
                self.ok("recover: no re-file sentence on an index this toolkit built")
        m = re.search(r"copybook choices checked against the listings: (\d+) confirmed, (\d+) contradicted, (\d+) unknown", outp)
        if m:
            a, b, u = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a < 1 or b < 1:
                self.find("recover", "recover", "STDHDR choices", "1 confirmed (POLRPT01's listing) and 1 contradicted (CLMRPT01's listing)",
                          m.group(0), facts=2)
            else:
                self.ok("recover: listing check counts", 2)
        else:
            self.find("recover", "recover", "STDHDR choices", "the sentence 'copybook choices checked against the listings: ...'", "absent", facts=2, severity="words")
        # the final build
        self.build("after-recover")
        rc, outp2, _s = run([PY, "-m", "atlas.recover", "--db", self.db, "--from", t["listings_dir"]], cwd=REPO)
        with open(os.path.join(self.out, "recover-console-2.txt"), "w", encoding="utf-8") as fh:
            fh.write(outp2)
        self.recover_console2 = outp2
        if "1 program(s) copy a copybook that has arrived" in outp2 or "re-files" in outp2 and "would" not in outp2:
            pass
        c = self.conn()
        still = sorted(r[0] for r in c.execute("SELECT name FROM member WHERE parse_status='pending'"))
        c.close()
        if still:
            self.find("recover", "recover (second run)", ", ".join(still[:10]), "nothing left pending after the build", "still pending", facts=len(still))

    def _patch_incremental_truth(self, cb: dict) -> None:
        if cb.get("_patched"):
            return
        cb["_patched"] = True
        rows = cb["fields"]
        fil = next((r for r in rows if r[1] == "CT-FILLER"), None)
        if fil is None:
            return
        idx = rows.index(fil)
        new = list(fil)
        new[1], new[3], new[4], new[13] = "CT-NEW-CHANNEL-CD", 3, "X(03)", fil[13]
        fil[2] += 3
        fil[13] += 1
        rows.insert(idx, new)
        cb["record_length"] += 3

    def _missing_text(self) -> List[str]:
        # the generator kept the missing copybook's text only in the listings: rebuild it from the truth rows
        from synth_estate import Estate  # noqa
        import synth_domain as dom
        from synth_layout import Emitter, Item, layout, render_items
        root = Item(1, "PMSS-AUDIT-REC", children=[dom.x(5, "PMSS-AUDIT-KEY", 12), dom.x(5, "PMSS-AUDIT-USER", 8),
                                                   dom.n9(5, "PMSS-AUDIT-DT", 8), dom.packed(5, "PMSS-AUDIT-AMT", 9),
                                                   dom.x(5, "PMSS-AUDIT-TEXT", 40)])
        layout(root)
        em = Emitter()
        em.comment(" POLICY AUDIT RECORD - THE COPYBOOK MISSING FROM THE ESTATE")
        render_items(em, root)
        return list(em.lines)

    # ------------------------------------------------------------ phase 5: an index built before the batch
    def phase_aged(self) -> None:
        """The stand-in on an index built before ROADMAP re-parse items 20 and 22: a copy of the index with POLPROCB
        (D3) filed proc and CMNDATEA / CMNCUSTP (D4) filed asm, as the classifier before the batch filed them, their
        programs parsed again without them (COPY NOT FOUND) and the last build recorded as an older toolkit's (no
        declared kinds). recover must re-file exactly those three with the promised words and mark their programs;
        the next build re-parses every member and makes them whole; recover then finds nothing."""
        t = self.truth
        d = t["defects"]
        aged = os.path.join(self.out, "aged.db")
        shutil.copy(self.db, aged)
        books = {d["D3"]["copybook"]: d["D3"]["aged_kind"], **{b: d["D4"]["aged_kind"] for b in d["D4"]["copybooks"]}}
        copiers = sorted({p["name"] for p in t["programs"].values() if any(cp["copybook"] in books for cp in p["copies"])})
        c = sqlite3.connect(aged)
        try:
            tables = [n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for book, kind in books.items():
                row = c.execute("SELECT id FROM member WHERE name=? AND kind='copybook'", (book,)).fetchone()
                if row is None:
                    self.find("recover", "aged index", book, "a copybook member to age", "none", facts=1, severity="crash")
                    return
                for table in tables:
                    cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
                    if "member_id" in cols and table not in ("member", "fts_span") and not table.startswith("src_fts"):
                        c.execute(f"DELETE FROM {table} WHERE member_id=?", (row[0],))
                c.execute("UPDATE member SET kind=?, parse_status='skipped', parse_error=NULL WHERE id=?", (kind, row[0]))
            q = ",".join("?" * len(books))
            c.execute(f"UPDATE member SET parse_status='pending' WHERE kind='cobol' AND id IN "
                      f"(SELECT member_id FROM copy_use WHERE UPPER(copybook) IN ({q}))", list(books))
            c.commit()
        finally:
            c.close()
        self.build("aged-incremental", db=aged)
        c = sqlite3.connect(aged)
        try:
            c.execute("UPDATE build_run SET fingerprint='0000prebatch0000', declared_kinds=NULL WHERE id=(SELECT MAX(id) FROM build_run)")
            c.commit()
            partial = sorted(r[0] for r in c.execute("SELECT name FROM member WHERE kind='cobol' AND parse_status='partial'"))
        finally:
            c.close()
        not_partial = [p for p in copiers if p not in partial]
        if not_partial:
            self.find("recover", "aged index", ", ".join(not_partial[:8]), "parsed only in part on the aged copy (COPY NOT FOUND)",
                      "whole", facts=len(not_partial), severity="crash")
            return
        rc, outp, _secs = run([PY, "-m", "atlas.recover", "--db", aged], cwd=REPO)
        with open(os.path.join(self.out, "recover-console-aged.txt"), "w", encoding="utf-8") as fh:
            fh.write(outp)
        rep_path = os.path.join(REPO, "work", "recover.md")
        report = read(rep_path) if os.path.exists(rep_path) else ""
        if report:
            shutil.copy(rep_path, os.path.join(self.out, "recover-report-aged.md"))
        c = sqlite3.connect(aged)
        try:
            refiled = sorted(r[0] for r in c.execute("SELECT name FROM member WHERE parse_error LIKE 're-filed as copybook by atlas.recover%'"))
            pending = sorted(r[0] for r in c.execute("SELECT name FROM member WHERE parse_status='pending'"))
        finally:
            c.close()
        if refiled != sorted(books):
            self.find("recover", "recover (aged index)", "re-filed members", f"exactly {', '.join(sorted(books))} (filed asm / proc by "
                      "the classifier before the batch)", ", ".join(refiled) or "none", facts=len(books))
        else:
            self.ok("aged: re-filed exactly D3 and D4", len(books))
        unmarked = [p for p in copiers if p not in pending]
        if unmarked:
            self.find("recover", "recover (aged index)", ", ".join(unmarked[:10]), "marked pending for the next build (they copy a "
                      "re-filed copybook)", "not marked", facts=len(unmarked))
        else:
            self.ok("aged: the copiers marked", len(copiers))
        self.must("recover", "recover (aged index)", "console", outp, d["D4"]["aged_recover_words"], "the re-file sentence")
        self.must("recover", "recover (aged index)", "work\\recover.md", report, d["D4"]["aged_report_words"], "the re-filed section")
        text = run([PY, "-m", "atlas.query", "--db", aged, "copybook", "CMNDATEA"], cwd=REPO)[1]
        self.must("recover", "copybook (aged index)", "CMNDATEA", text, d["D4"]["aged_copybook_words"], "the re-filed sentence")
        self.build("aged-after-recover", db=aged)
        c = sqlite3.connect(aged)
        try:
            kinds = {r[0]: (r[1], r[2]) for r in c.execute(f"SELECT name, kind, parse_status FROM member WHERE name IN ({q})", list(books))}
            status = {r[0]: r[1] for r in c.execute("SELECT name, parse_status FROM member WHERE kind='cobol'")}
        finally:
            c.close()
        wrong = [f"{b} {kinds.get(b)}" for b in books if kinds.get(b) != ("copybook", "ok")]
        if wrong:
            self.find("build", "build (aged index)", ", ".join(wrong), "copybook, ok (the first build after the toolkit changed "
                      "re-parses every member)", "otherwise", facts=len(wrong))
        else:
            self.ok("aged: the build files them copybooks", len(books))
        c = sqlite3.connect(self.db)
        try:
            want = {r[0]: r[1] for r in c.execute("SELECT name, parse_status FROM member WHERE kind='cobol'")}
        finally:
            c.close()
        off = [p for p in copiers if status.get(p) != want.get(p)]
        if off:
            self.find("build", "build (aged index)", ", ".join(off[:8]), "the status the index this toolkit built gives them, after "
                      "recover and the build", ", ".join(f"{p} {status.get(p)} (not {want.get(p)})" for p in off[:8]), facts=len(off))
        else:
            self.ok("aged: the programs whole", len(copiers))
        outp = run([PY, "-m", "atlas.recover", "--db", aged, "--dry-run"], cwd=REPO)[1]
        if "re-filed" in outp:
            self.find("recover", "recover (aged index)", "the run after the build", "nothing to re-file", outp[:200], facts=1,
                      severity="words")
        else:
            self.ok("aged: nothing left to re-file")

    # ------------------------------------------------------------ phase 3: the fact tables
    def phase_facts(self) -> None:
        t = self.truth
        c = self.conn()
        # members: kind and final status
        rows = {r["path"].replace("\\", "/").lower(): r for r in c.execute("SELECT path, name, kind, parse_status, parse_error, system FROM member")}
        for m in t["members"]:
            r = rows.get(m["path"].replace("\\", "/").lower())
            if r is None:
                self.find("build", "build", m["name"], f"a member row ({m['kind']})", "no row", facts=1)
                continue
            self.ok("member indexed")
            exp_kind = m["kind"]
            exp_status = m["status"]
            if r["kind"] != exp_kind:
                self.find("classify", "build", m["name"], f"kind {exp_kind}", f"kind {r['kind']} ({r['parse_error'] or ''})"[:200], facts=1)
            else:
                self.ok("member kind")
            if exp_status in ("ok", "partial", "skipped", "empty") and r["parse_status"] != exp_status:
                self.find("build", "build", m["name"], f"parse_status {exp_status} ({m['why'] or m['role']})", f"parse_status {r['parse_status']}", facts=1,
                          severity="status")
            elif exp_status in ("ok", "partial", "skipped", "empty"):
                self.ok("member status")
            if r["system"] != m["system"]:
                self.find("build", "build", m["name"], f"system {m['system']} (from the folder layout)", f"system {r['system']}", facts=1)
        # copybooks: every field row
        for key, cb in t["copybooks"].items():
            if cb["kind"] in ("procedure", "stub", "missing") or not cb["fields"]:
                continue
            mid = c.execute("SELECT id FROM member WHERE path=?", (cb["path"],)).fetchone()
            if cb["kind"] == "data" and cb["status"] == "skipped":
                # re-filed: documented - its own layout rows wait for the next full re-parse
                self.documented.append(Finding("copybook", "layout", cb["name"], "field rows", "none until the next full re-parse (documented, LESSONS 186)", len(cb["fields"])))
                continue
            if not mid:
                continue
            got_rows = [r for r in c.execute("SELECT name, level, offset, length, pic, usage, occurs_max, odo_on, redefines, line FROM field WHERE member_id=?", (mid[0],)) if r["name"]]
            got = {}
            for r in got_rows:
                got.setdefault(r["name"], []).append(r)
            if cb["name"] == "CLMTRANR":
                self._patch_incremental_truth(cb)
            for row in cb["fields"]:
                level, name, off, ln, pic, usage, occ, odo, redef, after_odo, root, sec, src, line, orig, fd = row
                if not name or name == "FILLER":
                    continue
                cands = got.get(name) or []
                g = next((x for x in cands if x["offset"] == off), cands[0] if cands else None)
                if g is None:
                    self.find("copybook", "layout / field", f"{cb['name']} {name}", f"a field row at offset {off} len {ln}", "no row", facts=1)
                    continue
                self.ok("copybook field present")
                if g["offset"] != off or g["length"] != ln:
                    self.find("copybook", "layout / field", f"{cb['name']} {name}", f"offset {off} length {ln}", f"offset {g['offset']} length {g['length']}",
                              facts=1, note=cb.get("note", ""))
                else:
                    self.ok("copybook offset/length exact")
                if (g["occurs_max"] or 0) != (occ or 0):
                    self.find("copybook", "layout", f"{cb['name']} {name}", f"OCCURS {occ}", f"OCCURS {g['occurs_max']}", facts=1)
                if (g["odo_on"] or "") != (odo or ""):
                    self.find("copybook", "layout", f"{cb['name']} {name}", f"DEPENDING ON {odo}", f"{g['odo_on']}", facts=1)
                if (g["redefines"] or "") != (redef or ""):
                    self.find("copybook", "layout", f"{cb['name']} {name}", f"REDEFINES {redef}", f"{g['redefines']}", facts=1)
                if line and g["line"] != line:
                    self.find("copybook", "layout", f"{cb['name']} {name}", f"line {line}", f"line {g['line']}", facts=1, severity="cite")
                else:
                    self.ok("copybook field line exact")
            extra = [n for n in got if n not in {r[1] for r in cb["fields"]} and n != "FILLER"]
            if extra:
                self.find("copybook", "layout", cb["name"], "no other fields", "invented: " + ", ".join(extra[:6]), facts=len(extra))
            # 88s
            got88 = {(r["name"]): (r["values_lit"], r["line"]) for r in c.execute(
                "SELECT c.name, c.values_lit, c.line FROM cond88 c JOIN field f ON f.id=c.field_id WHERE f.member_id=?", (mid[0],))}
            for cd in cb["conds"]:
                g = got88.get(cd["name"])
                if g is None:
                    self.find("copybook", "field", f"{cb['name']} 88 {cd['name']}", "an 88-level row", "no row", facts=1)
                    continue
                vals = json.loads(g[0]) if g[0] else []
                want = [v for v in cd["values"]]
                if [v.replace(" ", "") for v in vals] != [v.replace(" ", "") for v in want] and " ".join(vals).replace(" ", "") != " ".join(want).replace(" ", ""):
                    self.find("copybook", "field", f"{cb['name']} 88 {cd['name']}", f"values {want}", f"values {vals}", facts=1)
                else:
                    self.ok("88 values exact")
                if cd["line"] and g[1] != cd["line"]:
                    self.find("copybook", "field", f"{cb['name']} 88 {cd['name']}", f"line {cd['line']}", f"line {g[1]}", facts=1, severity="cite")
        # DB2 tables and columns
        for q, tb in t["db2"].items():
            qual, _, base = q.rpartition(".")
            o = c.execute("SELECT id, source FROM db2_object WHERE kind='table' AND name=? AND qualifier=?", (base, qual)).fetchone()
            if not o:
                self.find("db2", "table", q, "a db2_object row from the DCLGEN", "no row", facts=len(tb["columns"]) + 1)
                continue
            cols = [(r["name"], r["type"]) for r in c.execute("SELECT name, type FROM db2_column WHERE object_id=? ORDER BY ordinal", (o[0],))]
            if [x[0] for x in cols] != [x[0] for x in tb["columns"]]:
                self.find("db2", "table", q, f"columns {[x[0] for x in tb['columns']]}", f"{[x[0] for x in cols]}", facts=len(tb["columns"]))
            else:
                self.ok("db2 columns exact", len(cols))
        # IMS
        for name, d in t["ims"]["dbds"].items():
            r = c.execute("SELECT id, access, dd1, dd2 FROM ims_dbd WHERE name=?", (name,)).fetchone()
            if not r:
                self.find("ims", "dbd", name, "a DBD row", "none", facts=5)
                continue
            self.ok("dbd present")
            if (r["access"] or "") != d["access"]:
                self.find("ims", "dbd", name, f"ACCESS {d['access']}", f"{r['access']}", facts=1)
            if (r["dd1"] or "") != (d.get("dd1") or ""):
                self.find("ims", "dbd", name, f"DD1 {d.get('dd1')}", f"{r['dd1']}", facts=1)
            segs = {s["name"]: s for s in c.execute("SELECT id, name, parent, bytes, seq_field FROM ims_segment WHERE dbd_id=?", (r[0],))}
            for s in d["segments"]:
                g = segs.get(s["name"])
                if not g:
                    self.find("ims", "dbd", f"{name} {s['name']}", "a segment row", "none", facts=1 + len(s["fields"]))
                    continue
                if (g["parent"] or None) != s["parent"] or g["bytes"] != s["bytes"] or (g["seq_field"] or "") != s["seq"]:
                    self.find("ims", "dbd", f"{name} {s['name']}", f"parent {s['parent']} bytes {s['bytes']} seq {s['seq']}",
                              f"parent {g['parent']} bytes {g['bytes']} seq {g['seq_field']}", facts=1)
                else:
                    self.ok("segment exact")
                flds = {f["name"]: f for f in c.execute("SELECT name, start, bytes, is_seq FROM ims_field WHERE segment_id=?", (g[0],))}
                for fn, st, by, sq in s["fields"]:
                    f = flds.get(fn)
                    if not f or f["start"] != st or f["bytes"] != by or bool(f["is_seq"]) != sq:
                        self.find("ims", "dbd", f"{name} {s['name']} {fn}", f"start {st} bytes {by} seq {sq}",
                                  f"{dict(f) if f else 'no row'}", facts=1)
                    else:
                        self.ok("ims field exact")
            for x in d["xdfld"]:
                if not c.execute("SELECT 1 FROM ims_xdfld WHERE dbd_id=? AND name=?", (r[0], x["name"])).fetchone():
                    self.find("ims", "dbd", f"{name} XDFLD {x['name']}", "an XDFLD row", "none", facts=1)
                else:
                    self.ok("xdfld present")
        for name, p in t["ims"]["psbs"].items():
            r = c.execute("SELECT id, cmpat, io_pcb_first FROM ims_psb WHERE name=?", (name,)).fetchone()
            if not r:
                self.find("ims", "dbd", name, "a PSB row", "none", facts=len(p["pcbs"]))
                continue
            self.ok("psb present")
            pcbs = {x["ordinal"]: x for x in c.execute("SELECT ordinal, pcb_type, dbd_name, procopt, keylen, list_no, procseq, sensegs FROM ims_pcb WHERE psb_id=?", (r[0],))}
            for pc in p["pcbs"]:
                g = pcbs.get(pc["ordinal"])
                if not g:
                    self.find("ims", "dbd", f"{name} PCB {pc['ordinal']}", "a PCB row at that position", "none", facts=1)
                    continue
                want = (pc["type"], pc.get("dbd"), pc.get("procopt"), bool(pc.get("list_no")), pc.get("procseq"))
                got = (g["pcb_type"], g["dbd_name"], g["procopt"], bool(g["list_no"]), g["procseq"])
                if want != got:
                    self.find("ims", "dbd", f"{name} PCB {pc['ordinal']}", f"{want}", f"{got}", facts=1)
                else:
                    self.ok("pcb exact")
                segs = [(x[0] if isinstance(x, list) else x) for x in json.loads(g["sensegs"] or "[]")] if g["sensegs"] else []
                if pc["type"] == "DB" and segs != [s[0] for s in pc.get("sensegs", [])]:
                    self.find("ims", "dbd", f"{name} PCB {pc['ordinal']}", f"SENSEGs {[s[0] for s in pc.get('sensegs', [])]}", f"{segs}", facts=1)
        # transactions
        got_tx = {(r["tran_code"], r["system"]): r for r in c.execute("SELECT tran_code, system, program, psb, group_name FROM transaction_def")}
        for x in t["transactions"]:
            g = got_tx.get((x["code"], x["system"]))
            if not g:
                self.find("online", "transaction", x["code"], f"a {x['system']} transaction row", "none", facts=1)
                continue
            if (g["program"] or None) != x.get("program"):
                self.find("online", "transaction", x["code"], f"program {x.get('program')}", f"program {g['program']}", facts=1)
            else:
                self.ok("transaction routing exact")
        # screens
        for ms, sc in t["screens"].items():
            s = c.execute("SELECT id FROM screen WHERE name=? AND kind='bms_map'", (sc["map"],)).fetchone()
            if not s:
                self.find("screens", "screen", sc["map"], "a bms_map row", "none", facts=len(sc["fields"]))
                continue
            got = {r["name"]: r for r in c.execute("SELECT name, row, col, length, initial, attrb, picin, picout FROM screen_field WHERE screen_id=?", (s[0],)) if r["name"]}
            consts = [r for r in c.execute("SELECT initial FROM screen_field WHERE screen_id=? AND name IS NULL", (s[0],))]
            for f in sc["fields"]:
                g = got.get(f["name"])
                if not g:
                    self.find("screens", "screen", f"{sc['map']} {f['name']}", "a field row", "none", facts=1)
                    continue
                want = (f["row"], f["col"], f["length"], f["initial"], f["picin"], f["picout"])
                gt = (g["row"], g["col"], g["length"], g["initial"], g["picin"], g["picout"])
                if want != gt:
                    self.find("screens", "screen", f"{sc['map']} {f['name']}", f"{want}", f"{gt}", facts=1)
                else:
                    self.ok("screen field exact")
            got_consts = [x["initial"] or "" for x in consts]
            for k in sc["constants"]:
                if not any(k == g for g in got_consts):
                    self.find("screens", "screen", f"{sc['map']} constant '{k}'", f"an unnamed field with INITIAL '{k}'",
                              f"{got_consts}", facts=1, note="an INITIAL literal holding a blank")
                else:
                    self.ok("screen constant exact")
        c.close()

    # ------------------------------------------------------------ programs (facts)
    def check_program_facts(self, key: str, pt: dict) -> None:
        c = self.conn()
        name = pt["name"]
        r = c.execute("SELECT p.*, m.parse_status FROM program p JOIN member m ON m.id=p.member_id WHERE m.path=?", (pt["path"],)).fetchone()
        if not r:
            self.find("cobol", "program", name, "a program row", "none", facts=50)
            c.close()
            return
        pid, mid = r["id"], r["member_id"]
        self.ok("program row")
        for k in ("sql", "cics", "dli"):
            if bool(r[f"uses_{k}"]) != bool(pt["uses"][k]):
                self.find("cobol", "program", name, f"uses {k} = {pt['uses'][k]}", f"{bool(r[f'uses_{k}'])}", facts=1)
        lk = json.loads(r["linkage_using"] or "[]")
        if lk != pt["linkage_using"]:
            self.find("cobol", "program", name, f"PROCEDURE DIVISION USING {pt['linkage_using']}", f"{lk}", facts=max(1, len(pt["linkage_using"])))
        else:
            self.ok("linkage USING exact")
        origin = self._origin_map(c, pid, mid)
        facts = pt["facts"]
        # calls
        got_calls = [dict(x) for x in c.execute("SELECT kind, target, via_var, resolved, resolution, using_args, line, returning_item FROM call_edge WHERE program_id=?", (pid,))]
        for g in got_calls:
            g["mline"] = origin.get(g["line"], (None, None))[1]
        want_calls = [f for f in facts if f["fk"] == "call"]
        used = set()
        for w in want_calls:
            match = None
            for i, g in enumerate(got_calls):
                if i in used:
                    continue
                if w["kind"] == "static" and g["kind"] == "static" and g["target"] == w["target"] and g["mline"] == w["line"]:
                    match = i
                elif w["kind"] == "dynamic" and g["kind"] == "dynamic" and g["via_var"] == w["via_var"] and g["mline"] == w["line"]:
                    match = i
                elif w["kind"] in ("cics_link", "cics_xctl", "cics_start", "cics_return") and g["kind"] == w["kind"] and g["mline"] == w["line"]:
                    match = i
                if match is not None:
                    break
            if match is None:
                near = [g for g in got_calls if (g["target"] == w.get("target") or g["via_var"] == w.get("via_var")) and g["kind"] == w["kind"]]
                tool = f"a {w['kind']} edge at member line {near[0]['mline']}" if near else "no edge"
                self.find("cobol", "program (calls)", f"{name} line {w['line']}", f"{w['kind']} CALL {w.get('target') or w.get('via_var')} USING {w.get('using')}",
                          tool, facts=1, note="a statement in the same sentence as an EXEC SQL?" if near == [] else "")
                continue
            used.add(match)
            g = got_calls[match]
            self.ok("call edge exact (kind, target, line)")
            gu = json.loads(g["using_args"] or "[]")
            if [u.split("(")[0].strip() for u in w.get("using", [])] != gu and w["kind"] in ("static", "dynamic"):
                self.find("cobol", "program (calls)", f"{name} line {w['line']}", f"USING {w['using']}", f"USING {gu}", facts=1)
            if w["kind"] == "dynamic":
                res = json.loads(g["resolved"] or "[]")
                if sorted(res) != sorted(w["resolved"]):
                    self.find("cobol", "program (calls)", f"{name} line {w['line']}", f"dynamic CALL {w['via_var']} resolves to {w['resolved']}", f"{res} ({g['resolution']})", facts=1)
                else:
                    self.ok("dynamic call resolved exact")
            if w.get("returning") and (g["returning_item"] or "") != w["returning"]:
                self.find("cobol", "program (calls)", f"{name} line {w['line']}", f"RETURNING {w['returning']}", f"{g['returning_item']}", facts=1)
        for i, g in enumerate(got_calls):
            if i not in used and g["kind"] in ("static", "dynamic"):
                self.find("cobol", "program (calls)", f"{name} line {g['mline']}", "no CALL there", f"invented {g['kind']} call {g['target'] or g['via_var']}", facts=1)
        # copies
        got_cp = {(x["copybook"], origin.get(x["line"], (None, x["line"]))[1] if False else x["line"]): x for x in c.execute("SELECT copybook, replacing, line, resolved_member_id FROM copy_use WHERE member_id=?", (mid,))}
        for w in pt["copies"]:
            if w["expect"] == "system":
                continue
            g = got_cp.get((w["copybook"], w["line"]))
            if g is None:
                # the line stored is the member line of the COPY statement
                cands = [x for (b, l), x in got_cp.items() if b == w["copybook"]]
                if cands:
                    self.find("cobol", "program (copybooks)", f"{name} COPY {w['copybook']}", f"line {w['line']}", f"line {cands[0]['line']}", facts=1, severity="cite")
                    g = cands[0]
                else:
                    self.find("cobol", "program (copybooks)", f"{name} line {w['line']}", f"COPY {w['copybook']}" + (" (EXEC SQL INCLUDE)" if w["sql_include"] else ""), "no copy_use row", facts=1)
                    continue
            else:
                self.ok("copy_use row exact")
            exp = w["expect"]
            resolved = g["resolved_member_id"] is not None
            if exp in ("resolved", "chosen", "not_found_then_ok", "resolved_empty") and not resolved:
                self.find("cobol", "program (copybooks)", f"{name} COPY {w['copybook']}", "resolved to the copybook member", "NOT FOUND", facts=1)
            elif exp == "not_found" and resolved:
                self.find("cobol", "program (copybooks)", f"{name} COPY {w['copybook']}", "NOT FOUND (the member is not a copybook the build expands)", "resolved", facts=1)
            elif exp == "system" and resolved:
                pass
            else:
                self.ok("copy resolution as expected")
            if w["replacing"] and not (g["replacing"] or "").strip():
                self.find("cobol", "program (copybooks)", f"{name} COPY {w['copybook']}", f"REPLACING {w['replacing']}", "no REPLACING recorded", facts=1)
        # field aliases from REPLACING
        got_al = {(a["orig_name"], a["new_name"]) for a in c.execute("SELECT orig_name, new_name FROM field_alias WHERE program_id=?", (pid,))}
        for cb, o, n in pt["field_aliases"]:
            if (o, n) not in got_al:
                self.find("expand", "field (REPLACING)", f"{name} {cb}", f"alias {o} -> {n}", "no field_alias row", facts=1)
            else:
                self.ok("field alias exact")
        # selects / files
        got_sel = {x["select_name"]: x for x in c.execute("SELECT select_name, assign_dd, organization, record_key, fd_record, line FROM file_decl WHERE program_id=?", (pid,))}
        for s in pt["selects"]:
            g = got_sel.get(s["name"])
            if not g:
                self.find("cobol", "program (files)", f"{name} SELECT {s['name']}", "a file_decl row", "none", facts=1)
                continue
            if g["assign_dd"] != s["dd"] or (g["organization"] or "SEQUENTIAL") != s["org"] or (s.get("key") and g["record_key"] != s["key"]):
                self.find("cobol", "program (files)", f"{name} SELECT {s['name']}", f"DD {s['dd']} {s['org']} key {s.get('key')}",
                          f"DD {g['assign_dd']} {g['organization']} key {g['record_key']}", facts=1)
            else:
                self.ok("file_decl exact")
            if s.get("fd_record") and g["fd_record"] != s["fd_record"]:
                self.find("cobol", "program (files)", f"{name} FD {s['name']}", f"record {s['fd_record']}", f"{g['fd_record']}", facts=1)
        # io ops
        got_io = defaultdict(set)
        for x in c.execute("SELECT target, target_kind, op, line FROM io_op WHERE program_id=?", (pid,)):
            got_io[(x["target"], x["target_kind"])].add(x["op"].split()[0] if x["op"] != "OPEN" else x["op"])
            got_io[(x["target"], x["target_kind"])].add(x["op"])
        for f in facts:
            if f["fk"] == "io":
                ops = got_io.get((f["target"], "file"), set())
                op = f["op"] if f["op"] != "OPEN" else f"OPEN {f['mode']}"
                if op in ops or f["op"] in ops:
                    self.ok("io_op present")
                else:
                    self.find("cobol", "program (files)", f"{name} {f['target']}", f"{op} at line {f['line']}", f"ops recorded: {sorted(ops)}", facts=1)
        # paragraphs / sections
        got_par = {(x["name"], x["kind"]): x for x in c.execute("SELECT name, kind, section, start_line FROM paragraph WHERE program_id=?", (pid,))}
        for p in pt["paragraphs"]:
            g = got_par.get((p["name"], "paragraph"))
            if not g:
                self.find("cobol", "paragraph", f"{name} {p['name']}", "a paragraph row", "none", facts=1)
                continue
            self.ok("paragraph present")
            ml = origin.get(g["start_line"], (None, None))[1]
            if ml != p["line"]:
                self.find("cobol", "paragraph", f"{name} {p['name']}", f"starts at member line {p['line']}", f"line {ml}", facts=1, severity="cite")
            if (g["section"] or None) != p["section"]:
                self.find("cobol", "paragraph", f"{name} {p['name']}", f"in section {p['section']}", f"section {g['section']}", facts=1)
        for s in pt["sections"]:
            if (s["name"], "section") not in got_par:
                self.find("cobol", "paragraph", f"{name} {s['name']}", "a section row", "none", facts=1)
            else:
                self.ok("section present")
        for (n, k), g in got_par.items():
            names = {p["name"] for p in pt["paragraphs"]} | {s["name"] for s in pt["sections"]}
            cbparas = set()
            for w in pt["copies"]:
                cbt = self.truth["copybooks"].get(w["copybook"])
                if cbt:
                    cbparas |= set(cbt.get("paragraphs", []))
            if n not in names and n not in cbparas:
                self.find("cobol", "paragraph", f"{name} {n}", "no such paragraph", f"invented {k} {n}", facts=1)
        # perform edges
        got_pe = [dict(x) for x in c.execute("SELECT from_para, to_para, thru_para, kind, line FROM perform_edge WHERE program_id=?", (pid,))]
        for f in facts:
            if f["fk"] != "perform":
                continue
            ok = any(g["to_para"] == f["to"] and (g["thru_para"] or None) == f.get("thru") and g["kind"] == f["kind"]
                     and origin.get(g["line"], (None, None))[1] == f["line"] for g in got_pe)
            if ok:
                self.ok("perform edge exact")
            else:
                near = [g for g in got_pe if g["to_para"] == f["to"]]
                self.find("cobol", "paragraph / walk", f"{name} line {f['line']}", f"{f['kind']} {f['to']}" + (f" THRU {f['thru']}" if f.get("thru") else ""),
                          (f"an edge at member line {origin.get(near[0]['line'], (None, None))[1]} kind {near[0]['kind']}" if near else "no edge"), facts=1)
        # field references
        got_ref = defaultdict(set)
        got_ref_lines = defaultdict(set)
        for x in c.execute("SELECT name, mode, stmt, line FROM field_ref WHERE program_id=?", (pid,)):
            got_ref[(x["name"], x["mode"])].add(x["stmt"])
            got_ref_lines[(x["name"], x["mode"])].add(origin.get(x["line"], (None, None))[1])
        n_ref_ok = 0
        for f in facts:
            if f["fk"] != "ref":
                continue
            key = (f["name"], f["mode"])
            if f["line"] in got_ref_lines.get(key, set()):
                n_ref_ok += 1
            elif key in got_ref:
                self.find("cobol", "field (references)", f"{name} {f['name']} {f['mode']}", f"a {f['mode']} reference at member line {f['line']} ({f['stmt']})",
                          f"{f['mode']} references only at lines {sorted(x for x in got_ref_lines[key] if x)[:6]}", facts=1, severity="cite")
            else:
                other = {m for (n, m) in got_ref if n == f["name"]}
                self.find("cobol", "field (references)", f"{name} {f['name']}", f"a {f['mode']} reference at member line {f['line']} ({f['stmt']})",
                          f"no {f['mode']} reference (modes seen: {sorted(other)})", facts=1)
        self.ok("field reference exact (name, mode, line)", n_ref_ok)
        declared = set(pt["declared"]) | {"SQLCODE", "SQLCA", "SQLSTATE", "EIBCALEN", "EIBAID", "DFHCOMMAREA", "DFHRESP", "TRUE", "OTHER", "SPACES", "ZERO", "ZEROS"}
        cb_names = set()
        for w in pt["copies"]:
            cbt = self.truth["copybooks"].get(w["copybook"])
            if cbt:
                cb_names |= {r[1] for r in cbt["fields"]} | {cd["name"] for cd in cbt["conds"]}
                for nr in cbt.get("nested_rows", []):
                    cb_names.add(nr[1])
        files = {s["name"] for s in pt["selects"]}
        bad = sorted({n for (n, m) in got_ref if n not in declared and n not in cb_names and n not in files and not n.endswith(("I", "O", "L", "F", "A")) and not n.startswith("DFH")})
        if bad:
            self.find("cobol", "field (references)", name, "references only to declared data names", f"references to undeclared names: {bad[:8]}", facts=len(bad))
        fref = sorted({n for (n, m) in got_ref if n in files})
        if fref:
            self.find("cobol", "field (references)", "file names", "field references to data items only", f"a SELECT file name recorded as a field reference ({fref[0]} in {name}, and others)", facts=1, severity="noise")
        # literals
        got_lit = defaultdict(set)
        for x in c.execute("SELECT literal, context, field, line FROM literal_ref WHERE program_id=?", (pid,)):
            got_lit[(x["literal"], x["context"], x["field"])].add(origin.get(x["line"], (None, None))[1])
        for f in facts:
            if f["fk"] != "literal":
                continue
            key = (f["literal"], f["context"], f["field"])
            if f["line"] in got_lit.get(key, set()):
                self.ok("literal reference exact")
            else:
                cands = {k: v for k, v in got_lit.items() if k[0] == f["literal"]}
                self.find("cobol", "literal / values / conditions", f"{name} '{f['literal']}' {f['context']} {f['field']} line {f['line']}",
                          "a literal_ref row", f"rows for that literal: {[(k[1], k[2], sorted(x for x in v if x)) for k, v in cands.items()][:4]}", facts=1)
        # SQL
        got_sql = [dict(x) for x in c.execute("SELECT stmt_type, tables, cursor_name, start_line FROM sql_stmt WHERE program_id=?", (pid,))]
        for g in got_sql:
            g["mline"] = origin.get(g["start_line"], (None, None))[1]
        want_sql = [f for f in facts if f["fk"] == "sql"]
        for w in want_sql:
            m = [g for g in got_sql if g["stmt_type"] == w["stmt_type"] and g["mline"] == w["line"]]
            if not m:
                near = [g for g in got_sql if g["stmt_type"] == w["stmt_type"]]
                self.find("db2", "table / column / crud", f"{name} line {w['line']}", f"EXEC SQL {w['stmt_type']} on {w['tables']} cited at its own line",
                          f"a {w['stmt_type']} row cited at member line {near[0]['mline']}" if near else "no row", facts=1, severity="cite" if near else "wrong")
                continue
            self.ok("sql statement at its line")
            tb = json.loads(m[0]["tables"] or "[]")
            if w["stmt_type"] in ("SELECT", "INSERT", "UPDATE", "DELETE", "DECLARE") and sorted(tb) != sorted(w["tables"]):
                self.find("db2", "table", f"{name} line {w['line']}", f"{w['stmt_type']} tables {w['tables']}", f"{tb}", facts=len(w["tables"]))
            if w.get("cursor") and w["stmt_type"] in ("DECLARE",) and m[0]["cursor_name"] != w["cursor"]:
                self.find("db2", "table", f"{name} line {w['line']}", f"cursor {w['cursor']}", f"{m[0]['cursor_name']}", facts=1)
        got_cols = {(x["tbl"], x["col"], x["host_var"], x["mode"]) for x in c.execute("SELECT tbl, col, host_var, mode FROM sql_col_ref WHERE program_id=?", (pid,))}
        for w in want_sql:
            for col in w.get("cols", []):
                key = (col["tbl"], col["col"], col["host"], col["mode"])
                if key in got_cols or any(k[1:] == key[1:] and (k[0] or "").endswith(key[0]) for k in got_cols):
                    self.ok("sql column lineage exact")
                else:
                    self.find("db2", "column", f"{name} {col['tbl']}.{col['col']}", f"{col['mode']} via host {col['host']} (line {w['line']})",
                              f"rows for the column: {[k for k in got_cols if k[1] == col['col']][:4]}", facts=1)
        # DL/I
        got_dli = [dict(x) for x in c.execute("SELECT func, pcb_arg, io_area, line, dbd_name, procopt FROM dli_call WHERE program_id=?", (pid,))]
        for g in got_dli:
            g["mline"] = origin.get(g["line"], (None, None))[1]
        for f in facts:
            if f["fk"] != "dli":
                continue
            m = [g for g in got_dli if g["mline"] == f["line"]]
            if not m:
                self.find("ims", "program (DL/I)", f"{name} line {f['line']}", f"{f['func']} on {f['pcb_arg']}", "no dli_call row", facts=1)
                continue
            g = m[0]
            if g["func"] != f["func"] or g["pcb_arg"] != f["pcb_arg"] or (g["io_area"] or None) != f.get("io_area"):
                self.find("ims", "program (DL/I)", f"{name} line {f['line']}", f"{f['func']} {f['pcb_arg']} {f.get('io_area')}", f"{g['func']} {g['pcb_arg']} {g['io_area']}", facts=1)
            else:
                self.ok("dli call exact")
            exp_dbd = self._expected_dbd(pt, f)
            if exp_dbd and g["dbd_name"] != exp_dbd:
                self.find("ims", "program / dbd (PCB position)", f"{name} line {f['line']}", f"{f['func']} on {f['pcb_arg']} = database {exp_dbd}", f"{g['dbd_name']}", facts=1)
            elif exp_dbd:
                self.ok("dli database resolved exact")
        # CICS
        got_cics = [dict(x) for x in c.execute("SELECT verb, resource_kind, resource, direction, line FROM cics_cmd WHERE program_id=?", (pid,))]
        for g in got_cics:
            g["mline"] = origin.get(g["line"], (None, None))[1]
        for f in facts:
            if f["fk"] != "cics":
                continue
            m = [g for g in got_cics if g["mline"] == f["line"] and g["verb"] == f["verb"]]
            if not m:
                self.find("cics", "program (CICS)", f"{name} line {f['line']}", f"EXEC CICS {f['verb']} {f['resource_kind']} {f['resource']}", "no cics_cmd row", facts=1)
                continue
            g = m[0]
            res_ok = g["resource"] == f["resource"] or (f["resource_kind"] == "map" and g["resource"].endswith(f["resource"].split(".")[-1]))
            if g["resource_kind"] != f["resource_kind"] or not res_ok:
                self.find("cics", "program (CICS)", f"{name} line {f['line']}", f"{f['verb']} {f['resource_kind']} {f['resource']}", f"{g['verb']} {g['resource_kind']} {g['resource']}", facts=1)
            else:
                self.ok("cics command exact")
        # ENTRY aliases
        got_ent = {x["alias"] for x in c.execute("SELECT alias FROM program_alias WHERE program_id=?", (pid,))}
        for a in pt["aliases"]:
            if a["alias"] not in got_ent:
                self.find("cobol", "program / callers", f"{name} ENTRY {a['alias']}", "a program_alias row", "none", facts=1)
            else:
                self.ok("entry alias present")
        # jobs running it
        got_jobs = {(x["job_name"], x["step_name"]) for x in c.execute(
            "SELECT j.job_name, s.step_name FROM step s JOIN job j ON j.id=s.job_id WHERE UPPER(s.effective_pgm)=?", (name,))}
        for j in pt["jobs"]:
            stepname = f"{j['step']}" if not j["from_proc"] else None
            hit = any(g[0] == j["job"] and (g[1] == j["step"] or g[1].endswith("." + j["step"])) for g in got_jobs)
            if hit:
                self.ok("job/step running the program exact")
            else:
                self.find("jcl", "program (runs in)", f"{name}", f"runs in {j['job']} {j['step']}" + (f" (from PROC {j['from_proc']})" if j["from_proc"] else ""),
                          f"steps found: {sorted(got_jobs)[:6]}", facts=1)
        extra_jobs = [g for g in got_jobs if not any(g[0] == j["job"] and (g[1] == j["step"] or g[1].endswith("." + j["step"])) for j in pt["jobs"])]
        if extra_jobs:
            self.find("jcl", "program (runs in)", name, "no other job runs it", f"invented: {sorted(extra_jobs)[:6]}", facts=len(extra_jobs))
        c.close()

    def _expected_dbd(self, pt: dict, f: dict) -> Optional[str]:
        """The database a DL/I call addresses, by the PSB's PCB order and the program's USING list."""
        psb = self.truth["ims"]["psbs"].get(pt["name"])
        if not psb:
            return None
        using = pt["linkage_using"]
        if f["pcb_arg"] not in using:
            return None
        pos = using.index(f["pcb_arg"]) + 1
        if psb["io_first"] or True:
            if pos == 1:
                return "*IO-PCB*"
            pos -= 1
        listed = [pc for pc in psb["pcbs"] if not pc.get("list_no")]
        if pos - 1 < len(listed):
            pc = listed[pos - 1]
            return pc.get("dbd") if pc["type"] != "TP" else None
        return None

    def _origin_map(self, c: sqlite3.Connection, pid: int, mid: int) -> Dict[int, Tuple[int, int]]:
        """expanded line -> (member id, member line) through expand_run, the generator's own reading of the table."""
        runs = c.execute("SELECT exp_start, exp_end, src_member, src_start FROM expand_run WHERE program_id=? ORDER BY exp_start", (pid,)).fetchall()
        out: Dict[int, Tuple[int, int]] = {}
        for r in runs:
            for k in range(r["exp_start"], r["exp_end"] + 1):
                out[k] = (r["src_member"], r["src_start"] + (k - r["exp_start"]))
        if not runs:
            n = c.execute("SELECT exp_lines FROM program WHERE id=?", (pid,)).fetchone()[0] or 0
            for k in range(1, n + 1):
                out[k] = (mid, k)
        # only the program's own lines carry member lines the truth knows
        return {k: v for k, v in out.items() if v[0] == mid}

    # ------------------------------------------------------------ jobs (facts)
    def check_job_facts(self, name: str, jt: dict) -> None:
        c = self.conn()
        j = c.execute("SELECT j.*, m.name AS mname FROM job j JOIN member m ON m.id=j.member_id WHERE j.job_name=?", (name,)).fetchone()
        if not j:
            self.find("jcl", "job", name, "a job row", "none", facts=20)
            c.close()
            return
        self.ok("job row")
        if jt["jcllib"] and json.loads(j["jcllib"] or "[]") != jt["jcllib"]:
            self.find("jcl", "job", name, f"JCLLIB {jt['jcllib']}", f"{j['jcllib']}", facts=1)
        if jt["joblib"] and json.loads(j["joblib"] or "[]") != jt["joblib"]:
            self.find("jcl", "job", name, f"JOBLIB {jt['joblib']}", f"{j['joblib']}", facts=1)
        if (j["job_cond"] or None) != jt["cond"]:
            self.find("jcl", "job", name, f"JOB COND {jt['cond']}", f"{j['job_cond']}", facts=1)
        steps = [dict(x) for x in c.execute("SELECT * FROM step WHERE job_id=? ORDER BY ordinal", (j["id"],))]
        by_name: Dict[str, dict] = {}
        for s in steps:
            by_name[s["step_name"]] = s
        for w in jt["steps"]:
            key = w["name"] if not w["parent_step"] else f"{w['parent_step']}.{w['name']}"
            g = by_name.get(key)
            if not g:
                self.find("jcl", "job", f"{name} {key}", f"an effective step running {w['effective_pgm'] or ('PROC ' + str(w['proc_called']))}",
                          f"steps: {sorted(by_name)[:8]}", facts=1 + len(w["dds"]))
                continue
            self.ok("effective step present")
            if w["effective_pgm"] and (g["effective_pgm"] or "") != w["effective_pgm"]:
                self.find("jcl", "job", f"{name} {key}", f"runs {w['effective_pgm']}", f"runs {g['effective_pgm']}", facts=1)
            elif w["effective_pgm"]:
                self.ok("effective program exact")
            if w["launcher"] and (g["launcher"] or "") != w["launcher"]:
                self.find("jcl", "job", f"{name} {key}", f"launcher {w['launcher']}", f"{g['launcher']}", facts=1)
            if w["parm"] and w["effective_pgm"] and w["launcher"] is None and (g["parm"] or "").strip("'") != w["parm"].strip("'"):
                self.find("jcl", "job", f"{name} {key}", f"PARM {w['parm']}", f"PARM {g['parm']}", facts=1)
            if (w["cond"] or None) != (g["cond"] or None):
                self.find("jcl", "job", f"{name} {key}", f"COND {w['cond']}", f"{g['cond']}", facts=1)
            if w["guard"] and not g["guard"]:
                self.find("jcl", "job", f"{name} {key}", f"runs only when {w['guard']}", "no guard", facts=1)
            elif w["guard"]:
                self.ok("IF guard present")
            if w["proc_called"] and not w["from_proc"]:
                continue
            dds = [dict(x) for x in c.execute("SELECT * FROM dd WHERE step_id=?", (g["id"],))]
            for wd in w["dds"]:
                if wd["dummy"] or wd["dsn"] is None and not wd["inline"]:
                    continue
                if wd["inline"] and not wd["card_member"]:
                    gd = [d for d in dds if (d["dd_name"] or "") == wd["name"] and d["sysin_text"]]
                    if not gd:
                        self.find("jcl", "job", f"{name} {key} {wd['name']}", f"inline cards ({len(wd['inline'])} lines)", "no cards", facts=1)
                    else:
                        self.ok("inline cards kept")
                        got_lines = [x.strip() for x in gd[0]["sysin_text"].splitlines() if x.strip()]
                        if got_lines != [x.strip() for x in wd["inline"] if x.strip()]:
                            self.find("jcl", "job / pack", f"{name} {key} {wd['name']}", f"cards {wd['inline'][:2]}", f"{got_lines[:2]}", facts=1)
                    continue
                gd = [d for d in dds if (d["dd_name"] or "") == wd["name"]]
                if not gd:
                    self.find("jcl", "job", f"{name} {key} {wd['name']}", f"DD {wd['dsn_resolved']}", "no DD row", facts=1)
                    continue
                d = gd[0]
                if wd["referback"] and wd["dsn_resolved"]:
                    if d["dsn_resolved"] != wd["dsn_resolved"]:
                        self.find("jcl", "job / dataset", f"{name} {key} {wd['name']}", f"referback {wd['dsn']} = {wd['dsn_resolved']}", f"{d['dsn_resolved']}", facts=1)
                    else:
                        self.ok("referback resolved exact")
                elif wd["dsn_resolved"] and not wd["is_temp"]:
                    exp = wd["dsn_resolved"] + (f"({wd['card_member']})" if wd["card_member"] and "(" not in wd["dsn_resolved"] else "")
                    if (d["dsn_resolved"] or "") not in (wd["dsn_resolved"], exp):
                        self.find("jcl", "job / dataset", f"{name} {key} {wd['name']}", f"DSN {exp}", f"{d['dsn_resolved']}", facts=1)
                    else:
                        self.ok("DSN resolved exact")
                    if (d["gdg_rel"] or None) != (wd["gdg_rel"] or None):
                        self.find("jcl", "job / dataset", f"{name} {key} {wd['name']}", f"GDG {wd['gdg_rel']}", f"{d['gdg_rel']}", facts=1)
                    if wd["card_member"]:
                        if (d["card_member"] or "") != wd["card_member"]:
                            self.find("jcl", "job", f"{name} {key} {wd['name']}", f"cards from member {wd['card_member']}", f"{d['card_member']}", facts=1)
                        elif not d["sysin_text"]:
                            self.find("jcl", "job", f"{name} {key} {wd['name']}", f"the text of member {wd['card_member']} loaded", "card member NOT indexed", facts=1)
                        else:
                            self.ok("card member loaded")
                    if wd["mode_source"] in ("open_verb", "gdg_relative", "dd_convention", "gdg_same_job"):
                        if d["mode"] != wd["mode"]:
                            self.find("jcl", "job / dataset", f"{name} {key} {wd['name']} ({wd['dsn_resolved']})",
                                      f"direction {wd['mode']} [{wd['mode_source']}]", f"{d['mode']} [{d['mode_source']}]", facts=1,
                                      note="a (+1) generation created by an earlier step of the same job is read here" if wd["mode_source"] == "gdg_same_job" else "")
                        else:
                            self.ok("DD direction exact")
                if wd["is_override"] and not d["is_override"]:
                    self.find("jcl", "job", f"{name} {key} {wd['name']}", "marked as a //STEP.DD override", "not marked", facts=1)
            # sort cards
            got_cards = {(x["card_kind"], x["pos"], x["length"]) for x in c.execute("SELECT card_kind, pos, length FROM card_field_ref WHERE step_id=?", (g["id"],))}
            for kind, pos, ln, fmt in w["cards"]:
                if (kind, pos, ln) in got_cards:
                    self.ok("sort card position exact")
                else:
                    self.find("jcl", "job / field (sort cards)", f"{name} {key}", f"{kind} bytes {pos}-{pos + ln - 1}", f"{sorted(got_cards)}", facts=1)
            got_tb = {(x["op"], x["tbl"]) for x in c.execute("SELECT op, tbl FROM step_table WHERE step_id=?", (g["id"],))}
            for op, tbl, direction in w["tables"]:
                if (op, tbl) in got_tb:
                    self.ok("db2 utility table exact")
                else:
                    self.find("jcl", "table (utilities)", f"{name} {key}", f"{op} {tbl}", f"{sorted(got_tb)}", facts=1)
        extra = [s["step_name"] for s in steps if s["step_name"] not in {(w["name"] if not w["parent_step"] else f"{w['parent_step']}.{w['name']}") for w in jt["steps"]}
                 and not (s["proc_called"] and s["from_proc"] is None)]
        if extra:
            self.find("jcl", "job", name, "no other steps", f"invented steps {extra[:6]}", facts=len(extra))
        c.close()

    # ------------------------------------------------------------ phase 4: the reports
    def names_in(self, text: str) -> Set[str]:
        return {m for m in re.findall(r"(?<![A-Za-z0-9/_.\-])([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+|[A-Z][A-Z0-9]{3,})(?![A-Za-z0-9\-])", text)}

    def universe(self) -> Set[str]:
        if getattr(self, "_uni", None):
            return self._uni
        u = self.truth["universe"]
        s: Set[str] = set()
        for k, v in u.items():
            s |= set(v)
        for d in u["datasets"]:
            s |= set(d.split("."))
        s |= {c for tb in self.truth["db2"].values() for c, _t in tb["columns"]}
        s |= {tb["struct"] for tb in self.truth["db2"].values()}
        s |= {f["name"] for sc in self.truth["screens"].values() for f in sc["fields"]}
        s |= {f["name"] + x for sc in self.truth["screens"].values() for f in sc["fields"] for x in "IOLFA"}
        s |= {x for m in self.truth["members"] for x in m["library"].split(".")}
        s |= {m["name"] for m in self.truth["members"]}
        s |= {fn for d in self.truth["ims"]["dbds"].values() for sg in d["segments"] for fn, *_r in sg["fields"]}
        s |= {x["name"] for d in self.truth["ims"]["dbds"].values() for x in d["xdfld"]}
        s |= {s2["name"] for p in self.truth["programs"].values() for s2 in p["selects"]} | {s2["dd"] for p in self.truth["programs"].values() for s2 in p["selects"]}
        s |= {a["alias"] for p in self.truth["programs"].values() for a in p["aliases"]}
        s |= {d["name"] for j in self.truth["jobs"].values() for st in j["steps"] for d in st["dds"]}
        s |= {st["name"] for j in self.truth["jobs"].values() for st in j["steps"]}
        s |= {f"{st['parent_step']}.{st['name']}" for j in self.truth["jobs"].values() for st in j["steps"] if st["parent_step"]}
        s |= {"POLCUR", "CLMCUR", "BILCUR", "ITEMCUR", "CUSTFILE", "CUST-RECORD", "DLIBATCH", "CMNSRT1", "PEERHOST", "CIC2", "CMNGENP"}
        s |= {m["name"] for m in self.truth["members"]}
        s |= set(self.truth["docs"])
        self._uni = s
        return s

    def check_names(self, command: str, text: str) -> None:
        uni = self.universe()
        for n in self.names_in(text):
            if n in uni or n in VOCAB or n.startswith(("WS-", "WD-", "WE-", "WH-", "WA-", "LK-", "UR-", "IN-", "OUT-", "SSA-", "ALT-", "ERR-", "HDR-", "CF-", "IO-", "SQL-", "ABEND-", "DATE-", "DI-", "DAYS-", "START-", "END-", "PARR-", "PMSS-", "RPT-", "AGENT-", "BRANCH-", "ITEM-", "STATUS-", "POLICY-", "CLAIM-", "ACCOUNT-", "REGION-", "PREMIUM-", "COMMISSION-", "RESERVE-", "PAID-", "AMOUNT-", "LAST-", "INSURED-", "EFF-", "EXP-", "SEQ-", "CHG-", "OLD-", "NEW-", "COMM-", "SINCE-", "RATE-", "MIN-", "MAX-", "DCL")):
                continue
            if re.fullmatch(r"[A-Z]{1,2}\d[A-Z0-9]*", n) or n.startswith(("PM-", "PT-", "PX-", "PH-", "PC-", "PW-", "PA-", "PR-", "PS-", "PSC-", "PG1-", "PG2-",
                                                                          "CM-", "CT-", "CX-", "CH-", "CC-", "CW-", "CA-", "CR-", "CS-", "CSC-", "CG1-", "CG2-",
                                                                          "BM-", "BT-", "BX-", "BH-", "BC-", "BW-", "BA-", "BR-", "BS-", "BSC-", "BG1-", "BG2-",
                                                                          "POL-", "CLM-", "BIL-", "POLGS-", "CLMU-", "CMN-")):
                continue
            self.unknown_names[n] += 1
            self.unknown_where.setdefault(n, command)

    def must(self, module: str, command: str, subject: str, text: str, tokens: Sequence[str], what: str, facts_each: int = 1) -> int:
        missing = [tok for tok in tokens if tok not in text]
        if missing:
            self.find(module, command, subject, f"{what}: {', '.join(str(m) for m in missing[:8])}" + (" ..." if len(missing) > 8 else ""),
                      "absent from the report", facts=facts_each * len(missing))
        self.ok(f"{command}: {what} printed", len(tokens) - len(missing))
        return len(missing)

    def check_cites(self, command: str, text: str, member_hint: Optional[str] = None) -> None:
        """Every MEMBER:line cite must point at a line of that member (a line that exists)."""
        paths: Dict[str, List[str]] = {}
        for m in self.truth["members"]:
            paths.setdefault(m["name"], []).append(m["path"])
        bad = 0
        for mem, line in set(cites_in(text)):
            ps = paths.get(mem)
            if not ps or mem == "manifest":
                continue
            n = max(len(read(p).splitlines()) for p in ps)
            if line < 1 or line > n:
                bad += 1
                self.find("query", command, f"{mem}:{line}", f"a line of {mem} (it has {n})", "a cite past the end of the member", facts=1, severity="cite")
        self.ok(f"{command}: cites inside the member", len(set(cites_in(text))) - bad)

    def phase_reports(self) -> None:
        t = self.truth
        progs = t["programs"]
        # samples
        names = sorted({p["name"] for p in progs.values()})
        core = [n for n in names if not re.search(r"GEN\d\d$", n)]
        gens = [n for n in names if re.search(r"GEN\d\d$", n)]
        sample_progs = core + gens[:max(0, 40 - len(core))]
        jobs = sorted(t["jobs"])
        core_jobs = [j for j in jobs if not re.search(r"GJ\d\d$", j)]
        sample_jobs = core_jobs + [j for j in jobs if re.search(r"GJ\d\d$", j)][:max(0, 20 - len(core_jobs))]
        cbs = [k for k, v in t["copybooks"].items() if v["kind"] in ("data", "dclgen") and "@" not in k]
        sample_cbs = sorted(cbs)[:26] + ["STDHDR", "POLMISSB", "POLSTUBB", "POLPROCB", "CMNDATEA", "CMNCUSTP", "POLARRVB", "POLSTUBC"]
        fields = self._sample_fields(34)
        datasets = ["PROD.POL.MASTER.KSDS", "PROD.POL.EXTRACT", "PROD.POL.EXTRACT.SORTED", "PROD.CLM.TRANS", "PROD.BIL.HISTORY", "PROD.POL.UNLOAD",
                    "PROD.CMN.CUSTOMER.KSDS", "PROD.CLM.MASTER.KSDS", "STG.POL.EXTRACT", "PROD.BIL.REPORT.DAILY", "PROD.POL.GSAM.OUT", "PROD.CLM.EXTRACT"]
        tables = ["PRD.POLICY_TBL", "PRD.CLAIM_TBL", "PRD.ACCOUNT_TBL", "PRD.POL_HISTORY_TBL", "PRD.CLM_ITEM_TBL", "PRD.BIL_AGENT_TBL"]
        self.samples = {"programs": sample_progs, "jobs": sample_jobs, "copybooks": sample_cbs, "fields": fields, "datasets": datasets, "tables": tables}
        # facts per program / job
        for n in sample_progs:
            for key, pt in progs.items():
                if pt["name"] == n:
                    self.check_program_facts(key, pt)
        for j in sample_jobs:
            self.check_job_facts(j, t["jobs"][j])
        # reports
        for n in sample_progs:
            self.report_program(n)
        for j in sample_jobs:
            self.report_job(j)
        for cb in sample_cbs:
            self.report_copybook(cb)
        for f in fields:
            self.report_field(f)
        for d in datasets:
            self.report_dataset(d)
        for tb in tables:
            self.report_table(tb)
        for d in t["ims"]["dbds"]:
            self.report_dbd(d)
        for x in t["transactions"]:
            self.report_transaction(x)
        self.report_coverage()
        self.report_misc()
        self.report_gate()

    def _sample_fields(self, n: int) -> List[str]:
        t = self.truth
        picks = ["PM-STATUS", "PM-POLICY-NO", "PM-INSURED-NAME", "PM-ITEM-TBL", "PM-FILLER", "PM-CITY", "CM-CLAIM-NO", "BM-ACCOUNT-NO", "PT-TRAN-AMT", "PT-TRAN-TYPE",
                 "CT-NEW-CHANNEL-CD", "PX-NAME-TEXT", "PH-CHG-TYPE", "PC-RUN-DATE", "PW-EOF-SW", "PW-READ-CNT", "PA-RETURN-CODE", "ERR-CODE", "START-DATE", "DATE-RC",
                 "CF-CUST-KEY", "PS-STATUS", "PSC-ITEM-PREM", "STATUS-CD", "PREMIUM-AMT", "HDR-PROGRAM", "PR-AMT-1", "POLICY-NO", "PMSS-AUDIT-KEY", "PARR-RESTART-SW",
                 "WS-RTN-NAME", "SSA-ROOT-KEY", "CS-INSURED-NAME", "BW-TOTAL-AMT", "PM-ACTIVE", "PW-MSG-CODE"]
        return picks[:n]

    # ---- program report
    def report_program(self, name: str) -> None:
        t = self.truth
        pts = [p for p in t["programs"].values() if p["name"] == name]
        text = self.query("program", name)
        if not text:
            return
        self.check_names("program", text)
        self.check_cites("program", text)
        pt = pts[0]
        targets = sorted({f.get("target") or f.get("via_var") for f in pt["facts"] if f["fk"] == "call"})
        self.must("cobol", "program", name, section(text, "Calls"), targets, "call targets in the Calls table")
        self.must("cobol", "program", name, section(text, "Copybooks"), sorted({c["copybook"] for c in pt["copies"] if c["expect"] != "system"}), "copybooks in the Copybooks table")
        self.must("cobol", "program", name, section(text, "Files"), sorted({s["name"] for s in pt["selects"]}), "SELECT names in the Files table")
        self.must("jcl", "program", name, section(text, "Runs in"), sorted({j["job"] for j in pt["jobs"]}), "jobs in Runs in")
        if pt["transactions"]:
            self.must("online", "program", name, text, pt["transactions"], "transactions on the Online line")
        # parse label
        final = pt["status"]
        role = pt["role"]
        if role in ("D1 chosen", "ambiguous") or role.startswith("D1"):
            if "parse: complete (copybook chosen among several - see notes)" not in text and "parse: partial" not in text:
                self.find("query", "program", name, "parse: complete (copybook chosen among several - see notes)", re.search(r"parse: [^\n]*", text).group(0) if re.search(r"parse: [^\n]*", text) else "no parse line", facts=1, severity="words")
            elif "chosen among several" in text:
                self.ok("program: chosen-copybook wording")
        # cite lines of the Calls table
        for h, hdr, rows in md_tables(section(text, "Calls")):
            for row in rows:
                if len(row) < 5:
                    continue
                m = re.search(r":(\d+)", row[4])
                if not m:
                    continue
                line = int(m.group(1))
                src = read(pt["path"]).splitlines()
                tgt = row[1].split(" ->")[0].strip()
                if 1 <= line <= len(src) and ("CALL" in src[line - 1].upper() or "EXEC CICS" in src[line - 1].upper()):
                    self.ok("program: call cite points at the CALL line")
                else:
                    self.find("query", "program", f"{name} call {tgt}", f"cited at a line holding CALL", f"cited {row[4]}: `{src[line - 1].strip()[:60] if 1 <= line <= len(src) else '?'}`", facts=1, severity="cite")
        # PROC default datasets shown beside the job's
        if "TEST." in section(text, "Files") and pt["jobs"]:
            self.find("query", "program", name, "datasets via JCL from the jobs that run it (PROD.*)", "the PROC's default symbolic datasets (TEST.*) listed beside them",
                      facts=1, severity="noise", note="README: the bare PROC only when no indexed job expands it")
        # defects' words
        d = t["defects"]
        if name in d["D2"]["program_words"]:
            w = d["D2"]["program_words"][name]
            if w in text:
                self.ok("program: listing-says sentence (D2)")
            else:
                self.find("recover", "program", name, f"`{w}`", "absent", facts=1, severity="words")
        if name == d["D3"]["program"]:
            row = next((r for h, hdr, rows_ in md_tables(section(text, "Copybooks")) for r in rows_ if r and r[0] == d["D3"]["copybook"]), None)
            bad = [w for w in d["D3"]["not_words"] if w in text]
            if row and len(row) > 1 and row[1] == d["D3"]["copybook"] and not bad:
                self.ok("program: D3 resolved")
            else:
                self.find("classify", "program", name, "COPY POLPROCB resolved to POLPROCB (a copybook by its statements, ROADMAP "
                          "re-parse item 20)", "; ".join(bad) or (" | ".join(row) if row else "no row"), facts=1)
        if name == d["D6"]["program"]:
            cell = section(text, "Copybooks")
            for book in ("POLSTUBB", "POLSTUBC"):
                row = cell.split(f"| {book} |")[1].split("\n")[0] if f"| {book} |" in cell else ""
                if "a stub - not expanded" in row:
                    self.ok("program: D6 stub wording")
                else:
                    self.find("recover", "program", name, f"the {book} cell says a stub, not expanded (ROADMAP re-parse item 23)",
                              row[:120] or "no row", facts=1, severity="words")

    # ---- job report
    def report_job(self, name: str) -> None:
        t = self.truth
        jt = t["jobs"][name]
        text = self.query("job", name)
        if not text:
            return
        self.check_names("job", text)
        self.check_cites("job", text)
        heads = [f"### {s['name'] if not s['parent_step'] else s['parent_step'] + '.' + s['name']}" for s in jt["steps"]]
        self.must("jcl", "job", name, text, heads, "step headings")
        dsns = sorted({d["dsn_resolved"] for s in jt["steps"] for d in s["dds"] if d["dsn_resolved"] and not d["is_temp"] and not d["referback"]})
        self.must("jcl", "job", name, text, dsns, "resolved dataset names")
        pgms = sorted({s["effective_pgm"] for s in jt["steps"] if s["effective_pgm"] and not s["effective_pgm"].startswith("*")})
        self.must("jcl", "job", name, text, [f"runs **{p}**" for p in pgms], "effective programs")
        for s in jt["steps"]:
            if s["guard"]:
                self.must("jcl", "job", f"{name} {s['name']}", text, ["runs only when"], "the IF guard")
            for d in s["dds"]:
                if d["card_member"]:
                    self.must("jcl", "job", f"{name} {s['name']} {d['name']}", text, ["card lines loaded from member"], "the card member's text loaded")
            for kind, pos, ln, fmt in s["cards"]:
                self.must("jcl", "job", f"{name} {s['name']}", text, [f"{kind} {pos}-{pos + ln - 1}"], "sort card byte positions")
        # the effective step cites: MEMBER:line must name the member whose line it is
        for m in re.finditer(r"### (\S+)\s+\(from PROC (\S+)\)\s+`([^`]+)`", text):
            step, proc, cite = m.group(1), m.group(2), m.group(3)
            mem, _, line = cite.partition(":")
            if mem != proc:
                self.find("jcl", "job", f"{name} {step}", f"cited to the PROC member {proc}", f"cited `{cite}`", facts=1, severity="cite")
            else:
                self.ok("job: effective step cited to its PROC member")

    # ---- copybook report
    def report_copybook(self, name: str) -> None:
        t = self.truth
        cb = t["copybooks"].get(name)
        text = self.query("copybook", name)
        lay = self.query("layout", name)
        if not text or cb is None:
            return
        self.check_names("copybook", text)
        self.check_names("layout", lay)
        self.check_cites("copybook", text)
        nesting = {k: v.get("nested", []) for k, v in t["copybooks"].items()}
        copiers = sorted({p["name"] for p in t["programs"].values()
                          if any(c["copybook"] == name or name in nesting.get(c["copybook"], []) for c in p["copies"])})
        if cb["kind"] in ("data", "dclgen"):
            self.must("cobol", "copybook", name, section(text, "Programs including it"), copiers, "programs including it")
            m = re.search(r"### Programs including it \((\d+)\)", text)
            if m and int(m.group(1)) != len(copiers) and name != "STDHDR":
                self.find("query", "copybook", name, f"{len(copiers)} programs including it", f"({m.group(1)})", facts=abs(int(m.group(1)) - len(copiers)))
            jobs = sorted({j["job"] for p in t["programs"].values() if p["name"] in copiers for j in p["jobs"]})
            self.must("jcl", "copybook", name, section(text, "Jobs/steps running those programs"), jobs, "jobs running the including programs")
        d = t["defects"]
        if name == d["D3"]["copybook"] or name in d["D4"]["copybooks"]:
            key = "D3" if name == d["D3"]["copybook"] else "D4"
            bad = [w for w in d[key]["not_words"] if w in text]
            if bad:
                self.find("classify", "copybook", name, "a copybook with its programs, nothing misfiled (ROADMAP re-parse items 20 and "
                          "22)", "; ".join(bad), facts=len(bad), severity="words")
            else:
                self.ok(f"copybook: {key} filed as a copybook")
        if name in ("POLSTUBB", "POLSTUBC"):
            self.must("recover", "copybook", name, text, ["a stub"], "the stub verdict")
        if name == "POLMISSB":
            if "recovered" not in text.lower():
                self.find("recover", "copybook", name, "says the index holds it as a recovered copy", "does not", facts=1, severity="words")
            else:
                self.ok("copybook: recovered wording")
        # layout: offsets
        if cb["fields"] and cb["status"] != "skipped":
            rows: Dict[str, List[Tuple[int, int]]] = {}
            for ln in lay.splitlines():
                m = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+([A-Z0-9-]+)", ln)
                if m:
                    rows.setdefault(m.group(4), []).append((int(m.group(1)), int(m.group(2))))
            bad = 0
            # a name the copybook itself holds twice (DI-YY under DATE-IN and under DATE-OUT) is matched by its offset;
            # any other name by the first row printed, as before
            names = [r[1] for r in cb["fields"]]
            twice = {n for n in names if names.count(n) > 1}
            for r in cb["fields"]:
                nm, off, ln_ = r[1], r[2], r[3]
                if not nm or nm == "FILLER":
                    continue
                same = rows.get(nm) or []
                g = (off, ln_) if nm in twice and (off, ln_) in same else (same[0] if same else None)
                if g is None:
                    bad += 1
                    self.find("copybook", "layout", f"{name} {nm}", f"a row at offset {off}", "no row in the layout", facts=1)
                elif g != (off, ln_):
                    bad += 1
                    self.find("copybook", "layout", f"{name} {nm}", f"offset {off} len {ln_}", f"offset {g[0]} len {g[1]}", facts=1, note=cb.get("note", ""))
            self.ok("layout offsets exact", len([r for r in cb["fields"] if r[1] and r[1] != "FILLER"]) - bad)
            m = re.search(r"record length: (\d+) bytes", lay)
            if m and int(m.group(1)) != cb["record_length"]:
                self.find("copybook", "layout", name, f"record length {cb['record_length']}", f"record length {m.group(1)}", facts=1, note=cb.get("note", ""))
            elif m:
                self.ok("record length exact")
            for cd in cb["conds"]:
                if f"88  {cd['name']}" in lay or f"88    {cd['name']}" in lay or cd["name"] in lay:
                    self.ok("layout 88 printed")
                else:
                    self.find("copybook", "layout", f"{name} 88 {cd['name']}", "printed under its field", "absent", facts=1)
        # a program's view after REPLACING
        if name in ("POLHISTR", "POLCOMMA"):
            lp = self.query("layout", name, "--program", "POLUPD01")
            pfx = "WH-" if name == "POLHISTR" else "WE-"
            if pfx not in lp:
                self.find("expand", "layout --program", f"{name} in POLUPD01", f"the renamed fields ({pfx}...) as the program sees them", "not shown", facts=len(cb["fields"]))
            else:
                self.ok("layout --program shows the REPLACING names")
        if name == "POLMASTR":
            lp = self.query("layout", "PM-MASTER-RECORD", "--program", "POLUPD01")
            m = re.search(r"^\s*(\d+)\s+(\d+)\s+\d+\s+PM-ITEM-TBL", lp, re.M)
            want = next(r[2] for r in cb["fields"] if r[1] == "PM-ITEM-TBL")
            if m and int(m.group(1)) == want:
                self.ok("layout --program: nested COPY bytes in place")
            else:
                self.find("copybook", "layout --program", "PM-MASTER-RECORD in POLUPD01", f"PM-ITEM-TBL at offset {want}", f"{m.group(1) if m else 'no row'}", facts=1)

    # ---- field report
    def report_field(self, name: str) -> None:
        t = self.truth
        text = self.query("field", name)
        if not text:
            return
        self.check_names("field", text)
        self.check_cites("field", text)
        defs = [(k, r) for k, cb in t["copybooks"].items() for r in cb["fields"] if r[1] == name and cb["status"] != "skipped" and cb["kind"] != "missing"]
        conds = [(k, cd) for k, cb in t["copybooks"].items() for cd in cb["conds"] if cd["name"] == name]
        if defs:
            for k, r in defs:
                cbname = k.split("@")[0]
                rows = [row for h, hdr, rows_ in md_tables(section(text, "Definitions")) for row in rows_ if row[0] == cbname]
                if not rows:
                    self.find("copybook", "field", f"{name} in {cbname}", "a Definitions row", "none", facts=1)
                    continue
                self.ok("field definition present")
                off = next((int(row[5]) for row in rows if row[5].isdigit()), None)
                if off is not None and off != r[2]:
                    self.find("copybook", "field", f"{name} in {cbname}", f"offset {r[2]}", f"offset {off}", facts=1, note=t["copybooks"][k].get("note", ""))
                else:
                    self.ok("field offset exact")
        elif conds:
            if "88-level condition name" in text:
                self.ok("field: 88 name answered")
            else:
                self.find("query", "field", name, "recognised as an 88-level condition name", "not", facts=1)
        else:
            # a program's own field or a re-filed copybook's: the definition is in the program (pfield) or waits for the re-parse
            pass
        # references: every truth ref for this field appears as a program in the References section
        refs = defaultdict(set)
        for p in t["programs"].values():
            for f in p["facts"]:
                if f["fk"] == "ref" and f["name"] == name:
                    refs[f["mode"]].add(p["name"])
        sec = section(text, "References by mode")
        for mode, pgms in refs.items():
            line = next((ln for ln in sec.splitlines() if ln.startswith(f"- **{mode}**")), "")
            missing = [pg for pg in sorted(pgms) if pg not in line]
            if missing:
                self.find("cobol", "field", f"{name} {mode}", f"programs with a {mode} reference: {sorted(pgms)}", f"missing {missing}", facts=len(missing))
            self.ok("field references by program", len(pgms) - len(missing))

    # ---- dataset report
    def report_dataset(self, dsn: str) -> None:
        t = self.truth
        text = self.query("dataset", dsn)
        if not text:
            return
        self.check_names("dataset", text)
        self.check_cites("dataset", text)
        ent = t["datasets"].get(dsn)
        if not ent:
            return
        rows = [row for h, hdr, rows_ in md_tables(text) if hdr and hdr[0] == "dataset" for row in rows_ if row[0] == dsn]
        got = {(row[3], row[4], row[5], row[1].split(" ")[0]) for row in rows if len(row) > 5}
        for w in ent["writers"] + ent["readers"]:
            step = w["step"]
            hit = [g for g in got if g[1] == w["job"] and (g[2] == step or g[2].endswith("." + step))]
            if not hit:
                self.find("jcl", "dataset", f"{dsn} {w['job']} {w['step']}", f"a row: {w['mode']} by {w['pgm']}", "no row", facts=1)
                continue
            self.ok("dataset row present")
            if hit[0][3] != w["mode"] and w["source"] in ("open_verb", "gdg_relative", "dd_convention", "gdg_same_job"):
                self.find("jcl", "dataset", f"{dsn} {w['job']} {w['step']}", f"direction {w['mode']} [{w['source']}]", f"{hit[0][3]}", facts=1,
                          note="a (+1) generation created by an earlier step of the same job is read here" if w["source"] == "gdg_same_job" else "")
            else:
                self.ok("dataset direction exact")
        sw, sr = set(ent["systems_w"]), set(ent["systems_r"])
        if sw and sr and sw != sr:
            if "Crosses departments" in text:
                self.ok("dataset: crosses departments said")
            else:
                self.find("query", "dataset", dsn, f"Crosses departments (written by {sorted(sw)}, read by {sorted(sr)})", "not said", facts=1, severity="words")

    # ---- table report
    def report_table(self, q: str) -> None:
        t = self.truth
        text = self.query("table", q)
        if not text:
            return
        self.check_names("table", text)
        self.check_cites("table", text)
        tb = t["db2"][q]
        self.must("db2", "table", q, section(text, "Columns of"), [c for c, _t in tb["columns"]], "declared columns")
        rows = {row[0]: row[1] for h, hdr, rows_ in md_tables(section(text, "Programs and verbs")) for row in rows_ if len(row) > 1}
        for pgm, crud in tb["programs"].items():
            g = rows.get(pgm)
            if g is None:
                self.find("db2", "table", f"{q} {pgm}", f"CRUD {crud}", "program absent", facts=len(crud))
            elif g != crud:
                self.find("db2", "table", f"{q} {pgm}", f"CRUD {crud}", f"CRUD {g}", facts=len(set(crud) ^ set(g)))
            else:
                self.ok("table CRUD exact", len(crud))
        for pgm in rows:
            if pgm not in tb["programs"]:
                self.find("db2", "table", f"{q} {pgm}", "no SQL on this table", f"listed with {rows[pgm]}", facts=1)

    def report_dbd(self, name: str) -> None:
        t = self.truth
        text = self.query("dbd", name)
        if not text:
            return
        self.check_names("dbd", text)
        d = t["ims"]["dbds"][name]
        self.must("ims", "dbd", name, text, [s["name"] for s in d["segments"]], "segments")
        self.must("ims", "dbd", name, text, [f"{fn}@{st}/{by}" for s in d["segments"] for fn, st, by, _sq in s["fields"]], "fields at their bytes")
        psbs = [pn for pn, p in t["ims"]["psbs"].items() if any(pc.get("dbd") == name for pc in p["pcbs"])]
        self.must("ims", "dbd", name, section(text, "PSBs / PCBs"), psbs, "PSBs addressing it")
        pgms = sorted({p["name"] for p in t["programs"].values() for f in p["facts"] if f["fk"] == "dli" and self._expected_dbd(p, f) == name})
        self.must("ims", "dbd", name, section(text, "Programs whose DL/I calls"), pgms, "programs whose calls resolve to it")
        seg = self.query("segment", d["segments"][0]["name"], "--dbd", name) if d["segments"] else ""
        if seg:
            self.check_names("segment", seg)
            self.must("ims", "segment", d["segments"][0]["name"], seg, pgms, "programs on a sensitive PCB")

    def report_transaction(self, x: dict) -> None:
        text = self.query("transaction", x["code"])
        if not text:
            return
        self.check_names("transaction", text)
        if x.get("program"):
            self.must("online", "transaction", x["code"], text, [x["program"]], "the routed program")
            if x["in_index"]:
                self.must("online", "transaction", x["code"], text, [f"`{x['program']}` is indexed"], "the program is indexed")
            else:
                self.must("online", "transaction", x["code"], text, ["is not in the index"], "the program is not in the index")
        if x.get("remote"):
            self.must("online", "transaction", x["code"], text, ["(none)"], "no program (REMOTESYSTEM)")

    # ---- coverage
    def report_coverage(self) -> None:
        text = self.query("coverage", "--all", label="coverage-final")
        self.coverage_final = text
        self.check_names("coverage", text)
        c = self.conn()
        counts = {(r[0], r[1]): r[2] for r in c.execute("SELECT kind, parse_status, COUNT(*) FROM member GROUP BY 1,2")}
        rows = {(row[0], row[1]): int(row[2]) for h, hdr, rows_ in md_tables(section(text, "Members")) for row in rows_ if len(row) == 3 and row[2].isdigit()}
        if rows != counts:
            self.find("query", "coverage", "Members table", f"the index's counts {sorted(counts.items())}", f"{sorted(rows.items())}", facts=len(set(rows.items()) ^ set(counts.items())))
        else:
            self.ok("coverage member counts exact", len(counts))
        n_part = c.execute("SELECT COUNT(*) FROM member WHERE parse_status='partial'").fetchone()[0]
        m = re.search(r"of the (\d+) members marked `partial`, \*\*(\d+) (?:is|are) parsed only in part\*\* and \*\*(\d+) (?:is|are) complete", text)
        if m:
            a, b, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a != n_part or b + d != a:
                self.find("query", "coverage", "partial split", f"{n_part} partial = truly partial + chosen", m.group(0), facts=1)
            else:
                self.ok("coverage partial split adds up")
        nf = {row[0]: int(row[1]) for h, hdr, rows_ in md_tables(section(text, "Copybooks not found")) for row in rows_ if len(row) >= 2 and row[1].isdigit()}
        want_nf = {"POLSTUBB": 1, "POLSTUBC": 1, "DFHAID": 3}
        for k, v in want_nf.items():
            if nf.get(k) != v:
                self.find("query", "coverage", f"Copybooks not found {k}", f"{v} use(s)", f"{nf.get(k)}", facts=1)
            else:
                self.ok("coverage not-found row exact")
        for k in ("CMNDATEA", "CMNCUSTP", "POLPROCB", "POLMISSB", "POLARRVB"):
            if k in nf:
                self.find("recover", "coverage", f"Copybooks not found {k}", "not listed (a copybook in the index, or healed by recover "
                          "and the build)", f"listed with {nf[k]} use(s)", facts=nf[k])
            else:
                self.ok("coverage: healed copybook gone from not-found")
        call_rows = {(row[0], row[1]): int(row[2]) for h, hdr, rows_ in md_tables(section(text, "Call resolution")) for row in rows_ if len(row) == 3 and row[2].isdigit()}
        got_calls = {(r[0], r[1]): r[2] for r in c.execute("SELECT kind, resolution, COUNT(*) FROM call_edge GROUP BY 1,2")}
        if call_rows != got_calls:
            self.find("query", "coverage", "Call resolution", f"{sorted(got_calls.items())}", f"{sorted(call_rows.items())}", facts=1)
        else:
            self.ok("coverage call resolution counts exact", len(got_calls))
        d = self.truth["defects"]
        self.must("recover", "coverage", "D1", text, d["D1"]["coverage_words"], "the chosen-copybook sentences")
        self.must("recover", "coverage", "D2", text, d["D2"]["coverage_words"], "the listing-check sentence")
        self.must("recover", "coverage", "D6", text, ["POLSTUBB"], "the stub in the not-found table")
        for book in ("POLSTUBB", "POLSTUBC"):
            stub_row = next((row for h, hdr, rows_ in md_tables(section(text, "Copybooks not found")) for row in rows_ if row and row[0] == book), None)
            if stub_row and "a stub" not in " ".join(stub_row):
                self.find("recover", "coverage", book, "the cell says a stub - only numbers, never expanded (ROADMAP re-parse item 23)", " | ".join(stub_row)[:200], facts=1, severity="words")
            elif stub_row:
                self.ok("coverage: stub verdict")
        # the D1 programs are in the chosen table, not the partial one
        chosen = section(text, "Complete, with a copybook chosen among several")
        for pg in d["D1"]["programs"]:
            if pg in chosen:
                self.ok("coverage: chosen program in its table")
            else:
                self.find("recover", "coverage", pg, "listed under 'Complete, with a copybook chosen among several'", "not listed", facts=1)
        c.close()

    # ---- the rest of the commands
    def report_misc(self) -> None:
        t = self.truth
        # callers / callees
        text = self.query("callers", "CMNDATE", "--depth", "2", "--args")
        self.check_names("callers", text)
        callers = sorted({p["name"] for p in t["programs"].values() for f in p["facts"] if f["fk"] == "call" and f.get("target") == "CMNDATE"})
        self.must("cobol", "callers", "CMNDATE", text, callers, "direct callers")
        text = self.query("callees", "POLUPD01", "--depth", "2")
        self.check_names("callees", text)
        pt = next(p for p in t["programs"].values() if p["name"] == "POLUPD01")
        self.must("cobol", "callees", "POLUPD01", text, sorted({f.get("target") for f in pt["facts"] if f["fk"] == "call" and f.get("target")}), "direct callees")
        self.must("cobol", "callees", "POLUPD01", text, ["POLRATE1", "POLRATE2"], "both dynamic targets")
        text = self.query("callers", "POLEDIT2")
        if "POLEDIT" not in text:
            self.find("cobol", "callers", "POLEDIT2 (an ENTRY alias)", "reaches POLEDIT", "no caller", facts=1)
        # crud
        text = self.query("crud", "--job", "POLNIGHT")
        self.check_names("crud", text)
        self.must("jcl", "crud --job", "POLNIGHT", text, ["POLUPD01", "POLEXT01", "POLRPT01", "PROD.POL.MASTER.KSDS", "PROD.POL.TRANS", "PROD.POL.EXTRACT.SORTED"], "programs and files of the job")
        text = self.query("crud", "POLDB201", "POLIMS01", "POLONL01")
        self.check_names("crud", text)
        self.must("db2", "crud", "POLDB201", text, ["PRD.POLICY_TBL", "PRD.POL_HISTORY_TBL"], "DB2 tables")
        rows = {(row[0], row[2]): row[3] for h, hdr, rows_ in md_tables(text) for row in rows_ if len(row) > 3}
        if rows.get(("POLDB201", "PRD.POLICY_TBL")) != "CRUD":
            self.find("db2", "crud", "POLDB201 PRD.POLICY_TBL", "CRUD", f"{rows.get(('POLDB201', 'PRD.POLICY_TBL'))}", facts=1)
        else:
            self.ok("crud DB2 letters exact")
        if not any(k[0] == "POLIMS01" and "POLDBD" in k[1] for k in rows):
            self.find("ims", "crud", "POLIMS01", "an IMS row for POLDBD (GU/GN/REPL = R U)", f"rows {sorted(k for k in rows if k[0] == 'POLIMS01')}", facts=1)
        else:
            self.ok("crud IMS row present")
        if not any(k[0] == "POLONL01" and "POLMAST" in k[1] or "PROD.POL.MASTER.KSDS" in k[1] for k in rows if k[0] == "POLONL01"):
            self.find("cics", "crud", "POLONL01", "a CICS file row for POLMAST (READ)", f"rows {sorted(k for k in rows if k[0] == 'POLONL01')}", facts=1)
        else:
            self.ok("crud CICS row present")
        # conditions
        text = self.query("conditions", "POLUPD01")
        self.check_names("conditions", text)
        lits = [(f["literal"], f["field"]) for f in pt["facts"] if f["fk"] == "literal" and f["context"] in ("compare", "when")]
        for lit, fld in lits:
            if f"'{lit}'" in text or f"= '{lit}'" in text or lit in text:
                self.ok("condition literal printed")
            else:
                self.find("cobol", "conditions", f"POLUPD01 {fld} '{lit}'", "a condition row", "absent", facts=1)
        if "DATE-OK" not in text:
            self.find("cobol", "conditions", "POLUPD01 IF NOT DATE-OK", "the 88 DATE-OK expanded to its value (CMNDATEA, a copybook with "
                      "its own rows since ROADMAP re-parse item 22)", "absent", facts=1)
        else:
            self.ok("conditions: the copybook's 88 expanded")
        # values / pair / literal / messages
        text = self.query("values", "PM-STATUS")
        self.check_names("values", text)
        self.must("cobol", "values", "PM-STATUS", text, ["AC", "LP", "CN", "PN", "PM-ACTIVE", "PM-CANCELLED"], "the documented values and their 88s")
        row2 = next((row for h, hdr, rows_ in md_tables(text) for row in rows_ if row and row[0] == "2"), None)
        if row2:
            self.find("cobol", "values", "PM-STATUS", "only values the code holds (the INCLUDE card compares bytes 13-14 with C'AC')",
                      f"a value `2` from the sort card's LENGTH token, flagged USED BUT NOT DOCUMENTED", facts=1, severity="invented")
        text = self.query("pair", "PM-STATUS", "WE-RETURN-CODE")
        self.check_names("pair", text)
        self.must("cobol", "pair", "PM-STATUS WE-RETURN-CODE", text, ["POLUPD01"], "the co-referencing program")
        text = self.query("literal", "E003")
        self.check_names("literal", text)
        self.must("cobol", "literal", "E003", section(text, "Set"), ["POLUPD01", "CLMUPD01", "BILUPD01"], "the programs that MOVE it")
        defined = section(text, "Defined")
        if "POLMSGT" in defined and "BILMSGT" in defined:
            self.ok("literal: message-table definition found (split FILLERs)")
        else:
            self.find("cobol", "literal", "E003", "defined in the message tables POLMSGT and BILMSGT (a 4-byte FILLER VALUE 'E003' beside its text)", f"Defined: {defined.strip()[:160]}", facts=2)
        if "CLMMSGT" not in defined:
            self.documented.append(Finding("cobol", "literal", "E003 in CLMMSGT", "the code inside a 50-byte FILLER literal", "not found as a code (documented: `literal` finds codes kept in their own FILLER)", 1))
        text = self.query("messages", "NOT FOUND")
        self.check_names("messages", text)
        self.must("cobol", "messages", "NOT FOUND", text, ["POLMSGT", "CLMMSGT", "BILMSGT"], "the message tables holding such texts")
        text = self.query("literal", "POLRATE2")
        self.must("cobol", "literal", "POLRATE2", text, ["POLUPD01"], "the MOVE that names the dynamic target")
        # paragraph / walk / flow
        text = self.query("paragraph", "POLUPD01", "2300-CHANGE-MASTER")
        self.check_names("paragraph", text)
        self.must("cobol", "paragraph", "POLUPD01 2300-CHANGE-MASTER", text, ["2000-PROCESS-TRAN", "perform THRU 2300-CHANGE-EXIT", "9000-ERROR", "2500-WRITE-HISTORY", "POLEDIT", "WS-RTN-NAME"], "who reaches it and what it does")
        text = self.query("walk", "POLUPD01", "--no-source")
        self.check_names("walk", text)
        self.must("cobol", "walk", "POLUPD01", text, ["9900-NEVER-REACHED", "1 not reached"], "the paragraph nothing reaches")
        self.must("cobol", "walk", "POLUPD01", text, ["CMN-DATE-AREA", "START-DATE"], "the D4 copybook's fields in the data section")
        text = self.query("walk", "CMNCUST1")
        self.must("cobol", "walk", "CMNCUST1", text, ["A-110-POSITION", "A-120-READ", "CMNCUSTP"], "the procedure copybook's paragraphs, cited to the copybook")
        text = self.query("flow", "PT-TRAN-AMT", "--program", "POLUPD01")
        self.check_names("flow", text)
        self.must("flow", "flow", "PT-TRAN-AMT in POLUPD01", text, ["PM-PREMIUM-AMT", "WH-NEW-AMT", "POLEDIT", "POLRATE1"], "the MOVE targets and the CALL USING hop")
        text = self.query("flow", "PX-STATUS", "--program", "POLEXT01")
        self.must("flow", "flow", "PX-STATUS in POLEXT01", text, ["PROD.POL.EXTRACT", "POLRPT01", "PR-STATUS"], "the file, the reader and its field at the same bytes")
        text = self.query("flow", "STATUS-CD", "--program", "POLDB201", "--up")
        self.must("flow", "flow --up", "STATUS-CD in POLDB201", text, ["POLICY_TBL", "STATUS_CD"], "the DB2 column it is read from")
        # screen / column / segment / interfaces / dead / ambiguous / diff / cite
        text = self.query("screen", "POLMAP1")
        self.check_names("screen", text)
        self.must("screens", "screen", "POLMAP1", text, ["KEYIN", "STATUS", "AMOUNT", "NAME", "ERRMSG", "POLONL01", "RECEIVE", "SEND"], "the fields and the program")
        self.must("screens", "screen", "POLMAP1", text, ["POLICY-NO:", "POLICY MAINTENANCE"], "the two constant labels")
        text = self.query("column", "POLICY_TBL.PREMIUM_AMT")
        self.check_names("column", text)
        self.must("db2", "column", "POLICY_TBL.PREMIUM_AMT", text, ["POLDB201", "UPDATE", "INSERT", "FETCH", "SELECT", "PT-TRAN-AMT", "WS-OLD-AMT", "DECIMAL(11,2)"], "writers, readers and the declared type")
        text = self.query("interfaces")
        self.check_names("interfaces", text)
        self.must("jcl", "interfaces", "estate", text, ["STG.POL.EXTRACT", "REINSURER-X", "CLAIMS-WEB", "POLFTP", "POLQ", "PROD.POL.TDQ.LOG"], "FTP, manifest peers and TD queues")
        n_ftp = text.count("| ftp |")
        if n_ftp > 3:
            self.find("query", "interfaces", "FTP steps", "one row per FTP put (3)", f"{n_ftp} ftp rows (each put listed twice)", facts=n_ftp - 3, severity="noise")
        text = self.query("dead")
        self.check_names("dead", text)
        dead_pgms = section(text, "Programs never called")
        for pg in ("POLOLD01", "CLMOLD01", "BILOLD01"):
            self.must("cobol", "dead", pg, dead_pgms, [pg], "the retired program")
        for pg in ("POLRATE2", "POLONL02", "CMNSQLER", "POLEDIT", "CMNCUST1", "POLSUB01"):
            if re.search(rf"\| {pg} \|", dead_pgms):
                self.find("cobol", "dead", pg, "not a dead-code candidate (called, linked or a resolved dynamic target)", "listed as never called", facts=1)
            else:
                self.ok("dead: live program not listed")
        text = self.query("ambiguous")
        self.check_names("ambiguous", text)
        self.must("build", "ambiguous", "estate", text, ["STDHDR", "CMNUTIL"], "the two duplicate names with different content")
        text = self.query("diff", "POLICY/CMNUTIL", "CLAIMS/CMNUTIL")
        self.check_names("diff", text)
        self.must("query", "diff", "CMNUTIL", text, ["WS-CLAIMS-ONLY"], "the field only the CLAIMS copy has")
        text = self.query("cite", "POLUPD01", "91-93")
        if "CALL 'CMNDATE'" not in text:
            self.find("query", "cite", "POLUPD01 91-93", "the CALL 'CMNDATE' line", text[:200], facts=1)
        else:
            self.ok("cite exact")
        text = self.query("cite", "POLNIGHT", "3", "--kind", "proc")
        if "PGM=POLUPD01" not in text:
            self.find("query", "cite", "POLNIGHT(proc) 3", "the EXEC PGM=POLUPD01 line of the PROC", text[:200], facts=1)
        text = self.query("search", '"START-DATE"')
        if "CMNDATEA" not in text:
            self.find("query", "search", "START-DATE", "found in CMNDATEA (a copybook with its own lines indexed since ROADMAP re-parse "
                      "item 22)", "absent", facts=1)
        else:
            self.ok("search: the D4 copybook's own line found")
        text = self.query("program", "POLZIP01")
        self.must("query", "program", "POLZIP01 (load module only)", text, ["Source not indexed", "POLRERUN"], "the job that runs a program with no source")
        text = self.query("program", "CMNUTIL")
        self.must("query", "program", "CMNUTIL", text, ["2 copies indexed"], "the two copies")
        text = self.query("job", "CMNSORT")
        self.must("jcl", "job", "CMNSORT (PROC)", text, ["Executed by (3)"], "the three jobs that execute the shared PROC")
        text = self.query("job", "POLWEEK")
        self.must("jcl", "job", "POLWEEK", text, ["WEEK3.SRT010", "PROD.CMN.PARMLIB(POLSORT1)", "PROD.POL.EXTRACT(-1)"], "the shared sort PROC with the job's symbolics (CARDS=POLSORT1)")
        text = self.query("copybook", "STDHDR")
        self.must("recover", "copybook", "STDHDR", text, ["The programs' compiler listings say this copybook came from"], "the listings' libraries")

    # ---- the gate
    def report_gate(self) -> None:
        pack = self.query("pack", "POLUPD01", "--budget", "16000")
        self.check_names("pack", pack)
        # an answer with every cite the pack prints, each with the token the line really holds
        paths = {m["name"]: m["path"] for m in self.truth["members"]}
        answer = ["## FACTS", ""]
        n = 0
        for mem, line in sorted(set(cites_in(pack))):
            if mem not in paths or mem == "POLNIGHT":
                continue
            src = read(paths[mem]).splitlines()
            if 1 <= line <= len(src):
                tok = src[line - 1][7:72].strip()
                tok = tok[:40].rsplit(" ", 1)[0] if len(tok) > 40 else tok
                if len(tok) >= 6 and "'" not in tok and '"' not in tok:
                    answer.append(f"- fact {n}: [[{mem} {line} \"{tok}\"]]")
                    n += 1
        answer += ["", "## INFERENCE", "none", "", "## UNRESOLVED IN SCOPE", "none", "", "## NOT SEARCHED", "none"]
        ap = os.path.join(self.out, "answer-good.md")
        with open(ap, "w", encoding="utf-8") as fh:
            fh.write("\n".join(answer) + "\n")
        rc, outp, secs = run([PY, "-m", "atlas.verify_citations", ap, "--db", self.db])
        self.cmd_times.append(("verify_citations (good answer)", secs))
        if rc != 0 or "RESULT: citations verified" not in outp:
            self.find("gate", "verify_citations", f"{n} cites copied from the pack", "RESULT: citations verified", outp[-600:], facts=n)
        else:
            self.ok("gate: pack cites verified", n)
        # a wrong one must fail, and a PROC-named-like-the-job cite is checked as the gate resolves it
        bad = ["## FACTS", "- [[POLUPD01 91 \"CALL 'RATECALC'\"]]", "- [[POLNIGHT 3 \"PGM=POLUPD01\"]]", "- [[POLNIGHT(proc) 3 \"PGM=POLUPD01\"]]"]
        bp = os.path.join(self.out, "answer-bad.md")
        with open(bp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(bad) + "\n")
        rc, outp, secs = run([PY, "-m", "atlas.verify_citations", bp, "--db", self.db])
        self.cmd_times.append(("verify_citations (bad answer)", secs))
        if rc == 0:
            self.find("gate", "verify_citations", "a cite with a token the line does not hold", "FAIL", "verified", facts=1)
        else:
            self.ok("gate: wrong cite rejected")
        # a name shared by the job and its PROC: the gate must pick the member whose line holds the token
        for line_ in outp.splitlines():
            if "POLNIGHT 3-3" in line_ and '"PGM=POLUPD01"' in line_:
                if line_.startswith("PASS"):
                    self.ok("gate: a job/PROC name resolved by the token")
                else:
                    self.find("gate", "verify_citations", "[[POLNIGHT 3 \"PGM=POLUPD01\"]]", "PASS (the PROC's line 3 holds the token)", line_[:160], facts=1, severity="cite")
        self.gate_out = outp

    # ------------------------------------------------------------ report
    def write_report(self) -> None:
        t = self.truth
        today = dt.date.today().isoformat()
        seen = {}
        for f in self.findings:
            k = f.key()
            if k in seen:
                seen[k].facts += 0
            else:
                seen[k] = f
        # group identical shapes (same module+command+truth pattern) into one row with a count
        grouped: Dict[str, List[Finding]] = defaultdict(list)

        def norm(x: str) -> str:
            x = re.sub(r"'[^']*'", "'L'", x)
            x = re.sub(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+|[A-Z][A-Z0-9]{3,}(?:\.[A-Z0-9_]+)*", "NAME", x)
            return re.sub(r"\d+", "N", x)[:80]
        for f in seen.values():
            grouped[f"{f.module}|{f.command}|{f.severity}|{norm(f.truth)}|{norm(f.tool)[:40]}"].append(f)
        rows = []
        for key, fs in grouped.items():
            facts = sum(x.facts for x in fs)
            first = fs[0]
            subjects = ", ".join(x.subject for x in fs[:4]) + (f" (+{len(fs) - 4} more)" if len(fs) > 4 else "")
            rows.append((first.module, facts, first, subjects, len(fs)))
        rows.sort(key=lambda r: (r[0], -r[1]))
        by_module: Dict[str, list] = defaultdict(list)
        for r in rows:
            by_module[r[0]].append(r)
        lines = [f"# Synthetic-estate comparison - findings {today}", "",
                 f"Estate: seed {t['seed']}, scale {self.scale}: {len(t['members'])} members, {len(t['programs'])} programs, {len(t['jobs'])} jobs, "
                 f"{len(t['copybooks'])} copybooks, {len(t['db2'])} DB2 tables, {len(t['ims']['dbds'])} DBDs, {len(t['ims']['psbs'])} PSBs, "
                 f"{len(t['transactions'])} transactions. Generated by `tools/synth/generate.py`, compared by `tools/synth/check.py` (this report is its output; "
                 "rerun both to reproduce).", "",
                 "The flow: `build --rebuild`, a copybook changed and an incremental build, the arrived copybook dropped in, `recover --from listings`, "
                 "an incremental build, then every query command over the samples, and the gate over a pack.", "",
                 "## Summary", "",
                 f"- findings: **{len(seen)}** distinct ({sum(f.facts for f in seen.values())} facts affected), in {len(by_module)} modules; "
                 f"documented limitations met: {len(self.documented)}",
                 f"- matched: **{sum(self.matched.values())}** facts / sentences (the table at the end)",
                 f"- samples: {len(self.samples['programs'])} programs, {len(self.samples['jobs'])} jobs, {len(self.samples['copybooks'])} copybooks, "
                 f"{len(self.samples['fields'])} fields, {len(self.samples['datasets'])} datasets, {len(self.samples['tables'])} tables, "
                 f"{len(t['ims']['dbds'])} DBDs, {len(t['transactions'])} transactions", "",
                 "## Timing", ""]
        for k in ("build_rebuild", "build_incremental", "build_after-recover"):
            b = self.timing.get(k)
            if b:
                lines.append(f"- {k}: {b['wall_s']} s wall for {b['members']} members = **{b['members_per_s']} members/s** "
                             f"(parse phase {b['parse_phase_s']} s; this run {b['run']})")
        lines.append(f"- recover --from listings: {self.timing.get('recover_s')} s")
        lines.append(f"- generate: {self.timing.get('generate_s')} s")
        lines.append("")
        lines.append("| command | runs | mean s | max s |")
        lines.append("|---|---|---|---|")
        agg: Dict[str, List[float]] = defaultdict(list)
        for cmd, secs in self.cmd_times:
            agg[cmd.split()[0]].append(secs)
        for cmd, v in sorted(agg.items()):
            lines.append(f"| {cmd} | {len(v)} | {sum(v) / len(v):.2f} | {max(v):.2f} |")
        lines.append("")
        lines.append("## Findings, by module, ranked by facts affected")
        lines.append("")
        lines.append("Severity: `wrong` a wrong or missing fact; `cite` a right fact cited to the wrong line; `words` a promised sentence absent; "
                     "`invented` a fact the estate does not hold; `silent` a gap the tool does not name; `noise` a right fact shown beside a misleading one; `status` a parse status.")
        lines.append("")
        fid = 0
        for module, rs in sorted(by_module.items(), key=lambda kv: -sum(r[1] for r in kv[1])):
            lines.append(f"### {module} ({sum(r[1] for r in rs)} facts, {len(rs)} rows)")
            lines.append("")
            lines.append("| id | command | member(s) | truth says | tool says | facts | severity | repro |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for mod, facts, f, subjects, n in rs:
                fid += 1
                f.fid = f"F{fid:02d}"
                repro = f.repro or ""
                truth = f.truth.replace("|", "\\|")
                tool = f.tool.replace("|", "\\|")
                if f.note:
                    truth += f" _({f.note.replace('|', '/')})_"
                lines.append(f"| {f.fid} | `{f.command}` | {subjects.replace('|', '/')} | {truth[:300]} | {tool[:300]} | {facts} | {f.severity} | {repro} |")
            lines.append("")
        if self.unknown_names:
            lines.append("### Names printed that the estate does not hold (candidates for invented facts - triaged below)")
            lines.append("")
            lines.append("| name | times | first seen in |")
            lines.append("|---|---|---|")
            for n_, c_ in self.unknown_names.most_common(60):
                lines.append(f"| {n_} | {c_} | {self.unknown_where[n_]} |")
            lines.append("")
        if self.documented:
            lines.append("### Documented limitations met (not findings)")
            lines.append("")
            lines.append("| command | member | truth says | tool says |")
            lines.append("|---|---|---|---|")
            for f in self.documented:
                lines.append(f"| `{f.command}` | {f.subject} | {f.truth} | {f.tool} |")
            lines.append("")
        lines.append("## What matched")
        lines.append("")
        lines.append("| check | count |")
        lines.append("|---|---|")
        for k, v in sorted(self.matched.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {k} | {v} |")
        lines.append("")
        lines.append("## Samples")
        lines.append("")
        for k, v in self.samples.items():
            lines.append(f"- {k} ({len(v)}): {', '.join(v)}")
        lines.append("")
        os.makedirs(os.path.dirname(self.report_path), exist_ok=True)
        with open(self.report_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        with open(os.path.join(self.out, "findings.json"), "w", encoding="utf-8") as fh:
            json.dump([{"id": f.fid, "module": f.module, "command": f.command, "subject": f.subject, "truth": f.truth, "tool": f.tool,
                        "facts": f.facts, "severity": f.severity, "note": f.note} for f in seen.values()], fh, indent=1)
        self.log(f"report written: {self.report_path} ({len(seen)} findings, {sum(self.matched.values())} matched)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare the toolkit's answers over the synthetic estate with its ground truth.")
    ap.add_argument("--out", required=True, help="scratch folder for the estate, the index and the query outputs")
    ap.add_argument("--scale", type=int, default=12)
    ap.add_argument("--seed", type=int, default=20260925)
    ap.add_argument("--report", default=os.path.join(REPO, "docs", f"SYNTH-findings-{dt.date.today().isoformat()}.md"))
    ap.add_argument("--repro", default=os.path.join(HERE, "repro"))
    a = ap.parse_args(argv)
    if not a.out.strip():
        raise SystemExit("--out must name a scratch folder")
    ck = Checker(a.out, a.scale, a.seed, a.report, a.repro)
    ck.generate()
    ck.phase_builds()
    ck.phase_recover()
    ck.phase_facts()
    ck.phase_reports()
    ck.phase_aged()
    ck.write_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
