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
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

from . import reader

CITATION = re.compile(
    r"\[\[\s*([A-Za-z0-9_$#@.\\/:\-]+?)\s+(\d+)(?:\s*-\s*(\d+))?\s+\"((?:[^\"\\]|\\.)*)\"\s*\]\]")

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

def _resolve(ref: str, root: Optional[str], db: Optional[sqlite3.Connection]
             ) -> Tuple[Optional[str], str, Optional[str]]:
    """-> (path, detail, kind)"""
    if "/" in ref or "\\" in ref:
        p = ref if os.path.isabs(ref) else os.path.join(root or ".", ref)
        if os.path.isfile(p):
            return p, "path", None
        return None, f"path not found: {p}", None

    name = ref.upper()
    if db is not None:
        rows = db.execute(
            "SELECT path, kind, authoritative FROM member WHERE UPPER(name)=? ORDER BY authoritative DESC",
            (name,)).fetchall()
        if not rows:
            return None, f"member {name} not in index", None
        if len(rows) > 1 and not rows[0][2]:
            distinct = {r[0] for r in rows}
            if len(distinct) > 1:
                return None, (f"member {name} is AMBIGUOUS ({len(rows)} copies, none marked "
                              f"authoritative): " + " | ".join(sorted(distinct)[:4])), None
        return rows[0][0], "index", rows[0][1]

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


def check_answer(text: str, root: Optional[str] = None,
                 db_path: Optional[str] = None) -> Tuple[List[Result], List[str]]:
    db = sqlite3.connect(db_path) if db_path else None
    results: List[Result] = []
    cache: Dict[str, Tuple[List[str], Optional[List[reader.Line]]]] = {}

    for m in CITATION.finditer(text):
        ref, s, e, quote = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        end = int(e) if e else s
        quote = quote.replace('\\"', '"')
        path, how, kind = _resolve(ref, root, db)
        if not path:
            results.append(Result(m.group(0), ref, s, end, quote, "FAIL", how))
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
        results.append(Result(m.group(0), ref, s, end, quote, "PASS", how, path))

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
    args = ap.parse_args(argv)

    with open(args.answer, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    results, uncited = check_answer(text, args.root, args.db)

    n_pass = sum(1 for r in results if r.status == "PASS")
    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_warn = sum(1 for r in results if r.status == "WARN")

    if args.json:
        print(json.dumps({"citations": [asdict(r) for r in results], "uncited": uncited,
                          "pass": n_pass, "fail": n_fail, "warn": n_warn}, indent=1))
    else:
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
    return 1 if n_fail or not results else 0


if __name__ == "__main__":
    sys.exit(main())
