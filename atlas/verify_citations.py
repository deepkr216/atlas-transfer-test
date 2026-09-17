"""
verify_citations.py - mechanically check every citation in an answer against
the real source. Zero model tokens.

This is the gate that turns "the model says" into "the source says". The
model is instructed (see templates/CLAUDE.md) to write every factual claim
with a citation in this exact form:

    [[MEMBER 120-124 "CALL 'RATECALC'"]]
    [[SAMPJOB 17 "PGM=IKJEFT01"]]
    [[libs/copy/PMASTREC.cpy 11 "COMP-3"]]

i.e.  [[ <member-or-path> <line>[-<line>] "<token that must appear there>" ]]

This script opens the cited member, takes exactly those lines, and checks the
quoted token really is there (case-insensitive, whitespace collapsed). A
citation that fails means the claim is unsupported and the answer must not be
used until it is fixed. Lines that are COBOL comments are flagged, because a
claim about live behaviour cited to a commented-out line is wrong.

Usage:
    python -m atlas.verify_citations ANSWER.md --db atlas.db
    python -m atlas.verify_citations ANSWER.md --root C:/path/to/source
    python -m atlas.verify_citations ANSWER.md --db atlas.db --json

Exit status 0 = every citation verified; 1 = at least one failed.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

from . import reader

# [[MEMBER 27 "token"]]  [[MEMBER:27 "token"]]  [[MEMBER 27-31 (via COPY X) "token"]]
# [[POLICY/DUPREC 1 "..."]] (department)  [[SAMPPGM(cobol) 27 "..."]] (kind)
# [[DUPREC@PROD.POLICY.COPYLIB 1 "..."]] (library) - the forms the reports
# print and the forms a model writes back; all of them must be checkable.
# A document is named after its file: KT-SESSION.VIDEO, CLAIMS PLAN V1.2 -
# dots and spaces are part of the name (LESSONS 153).
CITATION = re.compile(
    r"\[\[\s*([A-Za-z0-9_$#@.\\/:\-() ]+?)(?:\s+|:)(\d+)(?:\s*-\s*(\d+))?\s*(?:\(via\s+COPY\s+[^)]*\)\s*)?"
    r"\"((?:[^\"\\]|\\.)*)\"\s*\]\]")
_REF = re.compile(r"^(?:(?P<system>[A-Za-z0-9_$#@\-]+)/)?(?P<name>[A-Za-z0-9_$#@.\- ]+?)"
                  r"(?:\((?P<kind>[a-z]+)\))?(?:@(?P<library>[A-Za-z0-9.$#@\-]+))?$", re.I)
# the name is non-greedy: `@` is a legal character in a member name AND the
# NAME@LIBRARY separator; greedy, the name swallowed the library every time
_ANY_BRACKETS = re.compile(r"\[\[[^\]]*\]\]")
# A quoted token this short matches almost anywhere; the gate says so.
WEAK_TOKEN_CHARS = 6
WIDE_RANGE_LINES = 20

# Lines that assert something about the code but carry no citation.
_ASSERTIVE = re.compile(
    r"\b(calls?|performs?|reads?|writes?|updates?|deletes?|inserts?|sets?|moves?|"
    r"populates?|displays?|opens?|closes?|runs?|executes?|triggers?|feeds?|produces?|"
    r"consumes?|references?|uses?|defines?|contains?|returns?|abends?)\b", re.I)


@dataclass
class Result:
    raw: str
    member: str
    start: int
    end: int
    quote: str
    status: str            # PASS | FAIL | WARN
    detail: str
    path: Optional[str] = None


# --------------------------------------------------------------------------
# member resolution
# --------------------------------------------------------------------------

def _resolve(ref: str, root: Optional[str], db: Optional[sqlite3.Connection],
             token: str = "") -> Tuple[Optional[str], str, Optional[str]]:
    """-> (path, detail, kind)

    A name is resolved by (name, kind): SAMPPGM.cbl, SAMPPGM.psb and
    SAMPPGM.jcl are three members with one name (the IMS PSB=program and the
    one-job-per-program conventions). The kind is taken from the citation
    (`SAMPPGM(cobol)`), else chosen as the ONLY kind whose text contains the
    quoted token. Two DIFFERENT copies of the same (name, kind) - one per
    department - stay AMBIGUOUS unless the citation names the department
    (`POLICY/DUPREC`) or the library (`DUPREC@PROD.POLICY.COPYLIB`); the
    gate must not certify a claim about one copy against the other.
    """
    if ("/" in ref or "\\" in ref) and not _REF.match(ref):
        p = ref if os.path.isabs(ref) else os.path.join(root or ".", ref)
        if os.path.isfile(p):
            return p, "path", None
        return None, f"path not found: {p}", None
    if "/" in ref:
        p = ref if os.path.isabs(ref) else os.path.join(root or ".", ref)
        if os.path.isfile(p):
            return p, "path", None

    m = _REF.match(ref)
    if not m:
        return None, f"unreadable member reference {ref!r}", None
    name = m.group("name").upper()
    want_kind = (m.group("kind") or "").lower() or None
    want_sys = (m.group("system") or "").upper() or None
    want_lib = (m.group("library") or "").upper() or None

    if db is not None:
        rows = db.execute(
            "SELECT path, kind, authoritative, system, library, norm_sha FROM member WHERE UPPER(name)=? "
            "ORDER BY authoritative DESC, path", (name,)).fetchall()
        if not rows and want_lib:
            # a member really named AB@CD: the split was wrong, not the cite
            whole = f"{name}@{want_lib}"
            rows = db.execute(
                "SELECT path, kind, authoritative, system, library, norm_sha FROM member WHERE UPPER(name)=? "
                "ORDER BY authoritative DESC, path", (whole,)).fetchall()
            if rows:
                name, want_lib = whole, None
        if not rows:
            # a document named `PLAN .docx` is member `PLAN ` - cited, rightly, without the space
            rows = db.execute(
                "SELECT path, kind, authoritative, system, library, norm_sha FROM member WHERE UPPER(TRIM(name))=? "
                "ORDER BY authoritative DESC, path", (name.strip(),)).fetchall()
        if not rows:
            return None, f"member {name} not in index", None
        cands = list(rows)
        if want_kind:
            cands = [r for r in cands if (r[1] or "").lower() == want_kind]
            if not cands:
                return None, f"member {name} has no copy of kind {want_kind} (kinds: {sorted({r[1] for r in rows})})", None
        if want_sys:
            cands = [r for r in cands if (r[3] or "").upper() == want_sys]
            if not cands:
                return None, f"member {name}: no copy in department {want_sys}", None
        if want_lib:
            cands = [r for r in cands if (r[4] or "").upper() == want_lib]
            if not cands:
                return None, f"member {name}: no copy in library {want_lib}", None
        kinds = sorted({r[1] for r in cands})
        how = "index"
        if len(kinds) > 1 and token:
            # Several kinds share the name: the one whose text holds the token.
            hits = []
            for r in cands:
                try:
                    txt, data, enc = reader.load(r[0])
                except OSError:
                    continue
                if _norm(token) in _norm(txt):
                    hits.append(r)
            hit_kinds = sorted({r[1] for r in hits})
            if len(hit_kinds) == 1:
                cands = [r for r in cands if r[1] == hit_kinds[0]]
                how = f"index ({hit_kinds[0]} chosen: the only kind containing the token)"
            elif not hits:
                return None, (f"member {name} exists as {', '.join(kinds)} and none contains the token - "
                              f"cite as {name}(kind)"), None
            else:
                return None, (f"member {name} is AMBIGUOUS across kinds {', '.join(hit_kinds)} - cite as "
                              f"{name}({hit_kinds[0]})"), None
        distinct = {r[0] for r in cands}
        if len(distinct) > 1:
            same = len({r[5] for r in cands}) == 1
            auth = [r for r in cands if r[2]]
            if same:
                pass                                   # identical copies: any will do
            elif len(auth) == 1:
                cands = auth
            else:
                by_sys = sorted({(r[3] or "?") for r in cands})
                return None, (f"member {name} is AMBIGUOUS ({len(cands)} different copies"
                              + (f" in departments {', '.join(by_sys)}" if len(by_sys) > 1 else "")
                              + f") - cite as SYSTEM/{name} or {name}@LIBRARY: " + " | ".join(sorted(distinct)[:4])), None
        return cands[0][0], how, cands[0][1]

    if root:
        hits = []
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if os.path.splitext(fn)[0].upper() == name:
                    hits.append(os.path.join(dirpath, fn))
        if not hits:
            return None, f"no file named {name}.* under {root}", None
        if len(hits) > 1:
            return None, f"member {name} is AMBIGUOUS under {root}: " + " | ".join(hits[:4]), None
        return hits[0], "walk", None
    return None, "no --db or --root given", None


# --------------------------------------------------------------------------
# checking
# --------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().upper()


def _is_comment_record(rec: str, kind: Optional[str], path: str) -> bool:
    """`//*` in JCL/PROC, `*` in column 1 of control cards / HLASM-style
    members: a citation to those proves nothing about what runs."""
    k = (kind or "").lower()
    low = path.lower()
    if k in ("jcl", "proc") or low.endswith((".jcl", ".prc", ".proc")):
        return rec.startswith("//*")
    if k in ("ctlcard", "dbd", "psb", "mfs", "imsgen", "csd") or low.endswith((".ctl", ".dbd", ".psb", ".mfs")):
        return rec.startswith("*")
    return False


def check_answer(text: str, root: Optional[str] = None,
                 db_path: Optional[str] = None) -> Tuple[List[Result], List[str]]:
    db = sqlite3.connect(db_path) if db_path else None
    results: List[Result] = []
    cache: Dict[str, Tuple[List[str], Optional[List[reader.Line]]]] = {}

    for m in CITATION.finditer(text):
        ref, s, e, quote = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        end = int(e) if e else s
        quote = quote.replace('\\"', '"').replace("\\|", "|")   # a token copied from a report table (| is escaped there)
        path, how, kind = _resolve(ref, root, db, quote)
        if not path:
            results.append(Result(m.group(0), ref, s, end, quote, "FAIL", how))
            continue
        # The file checked must be the file that was indexed: a re-fetch after
        # the pack was made shifts every line and every citation with it.
        if db is not None and path not in cache:
            row = db.execute("SELECT sha256 FROM member WHERE path=?", (path,)).fetchone()
            try:
                with open(path, "rb") as fh:
                    now = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                now = None
            if row and now and row[0] != now:
                results.append(Result(m.group(0), ref, s, end, quote, "FAIL",
                                      "member changed since the index was built (sha mismatch) - rebuild, then re-cite", path))
                continue

        if kind == "doc" and db is not None:
            # A document has sections, not lines: [[DOCNAME 3 "token"]] cites
            # section 3 (1001+ = OCR'd images). The token must be in that
            # section's heading or text.
            secs = db.execute("""SELECT ordinal, heading, text FROM doc_section
                                 WHERE member_id=(SELECT id FROM member WHERE path=?) ORDER BY ordinal""",
                              (path,)).fetchall()
            target = [x for x in secs if x[0] is not None and s <= x[0] <= end]
            scoped = bool(target)
            if not target:
                target = secs
            hay = " ".join(f"{h or ''} {t or ''}" for _o, h, t in target)
            if _norm(quote) in _norm(hay):
                results.append(Result(m.group(0), ref, s, end, quote, "PASS",
                                      "document section" if scoped else "document (section number not found; matched elsewhere)",
                                      path))
            else:
                results.append(Result(m.group(0), ref, s, end, quote, "FAIL",
                                      f"quoted text not in document section(s) {s}-{end}", path))
            continue

        if path not in cache:
            txt, data, enc = reader.load(path)
            recs = reader._split_records(txt, data, enc)
            cob = None
            if (kind in ("cobol", "copybook")) or path.lower().endswith((".cbl", ".cob", ".cpy", ".copy")):
                cob, _ = reader.read_cobol_lines(txt, data=data, enc=enc)
            cache[path] = (recs, cob)
        recs, cob = cache[path]

        if s < 1 or end > len(recs) or end < s:
            results.append(Result(m.group(0), ref, s, end, quote, "FAIL",
                                  f"line range {s}-{end} outside member ({len(recs)} lines)", path))
            continue

        window = " ".join(recs[s - 1:end])
        if _norm(quote) not in _norm(window):
            # Try a little harder: the model may have quoted across a
            # continuation - check the joined code area too.
            joined = ""
            if cob:
                joined = " ".join(ln.code for ln in cob[s - 1:end])
            if _norm(quote) in _norm(joined):
                results.append(Result(m.group(0), ref, s, end, quote, "PASS",
                                      "matched in code area (continuation joined)", path))
                continue
            results.append(Result(m.group(0), ref, s, end, quote, "FAIL",
                                  f"quoted text not found in lines {s}-{end}", path))
            continue

        if cob and all(cob[i - 1].is_comment for i in range(s, end + 1)):
            results.append(Result(m.group(0), ref, s, end, quote, "WARN",
                                  "every cited line is a COMMENT - not live code", path))
            continue
        if not cob and all(_is_comment_record(recs[i - 1], kind, path) for i in range(s, end + 1)):
            results.append(Result(m.group(0), ref, s, end, quote, "WARN",
                                  "every cited line is a COMMENT (//* or * card) - not live code", path))
            continue
        warn = []
        if len(_norm(quote)) < WEAK_TOKEN_CHARS:
            n_lines = sum(1 for r in recs if _norm(quote) in _norm(r))
            warn.append(f"weak token ({len(_norm(quote))} chars, on {n_lines} lines of the member)")
        if end - s + 1 > WIDE_RANGE_LINES:
            warn.append(f"wide range ({end - s + 1} lines) - cite the line that holds the fact")
        if warn:
            results.append(Result(m.group(0), ref, s, end, quote, "WARN", "; ".join(warn), path))
            continue
        results.append(Result(m.group(0), ref, s, end, quote, "PASS", how, path))

    # A [[...]] the grammar cannot read is a claim that was NOT checked: it
    # counts as a failure, or an answer built on it would be certified.
    readable = {m.start() for m in CITATION.finditer(text)}
    for m in _ANY_BRACKETS.finditer(text):
        if m.start() not in readable:
            results.append(Result(m.group(0), m.group(0)[2:-2].strip()[:60], 0, 0, "", "FAIL",
                                  'unreadable citation - write [[NAME section "token"]] with the name exactly as '
                                  'the report prints it'))

    uncited: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        looks_claim = (re.match(r"^([-*+]|\d+[.)])\s+", stripped) or _ASSERTIVE.search(stripped))
        if looks_claim and "[[" not in stripped and len(stripped) > 25:
            if re.search(r"\b(unknown|not found|insufficient evidence|unverified|assumption|could not)\b",
                         stripped, re.I):
                continue                       # honest hedges are fine uncited
            uncited.append(stripped[:140])
    if db is not None:
        db.close()
    return results, uncited


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Verify [[MEMBER line \"token\"]] citations against source.")
    ap.add_argument("answer", help="answer file (markdown/text) containing citations")
    ap.add_argument("--db", help="atlas.db built by atlas.build (preferred)")
    ap.add_argument("--root", help="source folder to search when no --db")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--out", help="also write the report to this file (UTF-8, folders created) - the "
                                  "fix-up prompt reads it; the terminal still shows it")
    args = ap.parse_args(argv)

    with open(args.answer, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    results, uncited = check_answer(text, args.root, args.db)

    n_pass = sum(1 for r in results if r.status == "PASS")
    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_warn = sum(1 for r in results if r.status == "WARN")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _report(args.json, results, uncited, n_pass, n_fail, n_warn)
    report = buf.getvalue()
    sys.stdout.write(report)
    if args.out:
        d = os.path.dirname(args.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(report)
    return 1 if n_fail or not results else 0


def _report(as_json: bool, results, uncited, n_pass: int, n_fail: int, n_warn: int) -> None:
    if as_json:
        print(json.dumps({"citations": [asdict(r) for r in results], "uncited": uncited,
                          "pass": n_pass, "fail": n_fail, "warn": n_warn}, indent=1))
        return
    for r in results:
        print(f"{r.status:<4} {r.member} {r.start}-{r.end} \"{r.quote[:40]}\"  {r.detail}")
    print("-" * 78)
    print(f"{len(results)} citation(s): {n_pass} pass, {n_fail} FAIL, {n_warn} warn")
    if uncited:
        print(f"{len(uncited)} assertive line(s) with NO citation - treat as unverified:")
        for u in uncited[:20]:
            print(f"   ? {u}")
    if n_fail:
        print("RESULT: REJECT - unsupported claims present")
    elif not results:
        print("RESULT: NO CITATIONS - nothing here is verifiable")
    else:
        print("RESULT: citations verified" + (" (with warnings)" if n_warn else ""))


if __name__ == "__main__":
    sys.exit(main())
