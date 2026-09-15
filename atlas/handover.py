"""
atlas.handover - every report a transition document needs, gathered into
ONE file with no model tokens, so the model is called once.

    python -m atlas.handover --db atlas.db --system GC-DEV --docs PLAN12,STORIES12,QAREL12 --out work/handover.md

What goes in, in this order:
  1. the release: every member whose copy in --system differs from its
     production copy, and members new to that system (`diff --system`)
  2. one `diff` per changed member - the facts that changed, then the lines
  3. one `walk` per changed program, of the NEW copy (SYSTEM/NAME)
  4. per changed copybook: every other program that expands it
     (`copybook`) and its layout as the changed program sees it (`layout`)
  5. `crud` for the changed programs and `program` for each (the jobs
     that run it)
  6. the documents named (--docs) or found by folder (--docs-folder):
     outline, pictures, and the sections in full inside --doc-budget
A hand-written `work/release-facts.md` is copied from the template when
missing. The first lines give the size in characters and tokens.

Without a second system (the changes are already in the production copy
and no previous version was indexed) give --members: the diffs are
skipped and said so, the rest comes from the copies in the index.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import List, Optional

from . import query

DEFAULT_BUDGET = 8000          # per walk / diff: about 2,000 tokens each
DEFAULT_DOC_BUDGET = 20000     # per document: about 5,000 tokens each


def _split(csv: Optional[str]) -> List[str]:
    return [x.strip().upper() for x in (csv or "").replace(";", ",").split(",") if x.strip()]


def docs_in_folder(conn, pattern: str) -> List[str]:
    pat = pattern.strip().upper()
    if not pat:
        return []
    return [r[0] for r in conn.execute("SELECT name FROM member WHERE kind='doc' ORDER BY path")
            if pat in r[0].upper() or pat in (conn.execute("SELECT path FROM member WHERE name=? AND kind='doc'",
                                                            (r[0],)).fetchone()[0] or "").upper()]


def build_pack(conn, system: Optional[str] = None, members: Optional[List[str]] = None,
               doc_names: Optional[List[str]] = None, budget: int = DEFAULT_BUDGET,
               doc_budget: int = DEFAULT_DOC_BUDGET, say=print) -> str:
    system = (system or "").strip() or None
    members = [m.upper() for m in (members or [])]
    doc_names = [d.upper() for d in (doc_names or [])]
    parts: List[str] = []

    # ---- 1. the release
    pairs, fresh, _same = query.release_pairs(conn, system) if system else ([], [], 0)
    changed = [(p["name"], p["kind"], p["old"], p["new"]) for p in pairs]
    changed += [(f["name"], f["kind"], None, f"{system}/{f['name']}") for f in fresh]
    known = {c[0] for c in changed}
    for m in members:                                   # named by hand: the copy in --system, else the production copy
        if m not in known:
            row = conn.execute("SELECT name, kind, system FROM member WHERE UPPER(name)=? AND kind IN ('cobol','copybook',"
                               "'jcl','proc','ctlcard','psb','dbd','mfs','bms') ORDER BY "
                               "CASE WHEN UPPER(COALESCE(system,''))=? THEN 0 ELSE 1 END, authoritative DESC, path",
                               (m, (system or "").upper())).fetchone()
            if row:
                ident = f"{row['system']}/{row['name']}" if row["system"] else row["name"]
                changed.append((row["name"], row["kind"], None, ident))
            else:
                changed.append((m, "unknown", None, m))
    programs = [c for c in changed if c[1] == "cobol"]
    copybooks = [c for c in changed if c[1] == "copybook"]
    others = [c for c in changed if c[1] not in ("cobol", "copybook")]

    head = ["# Hand-over pack\n",
            f"- changed copies in system **{system}**\n" if system else
            "- no second system given: no before/after diff; the reports describe the copies in the index as they are\n",
            f"- {len(programs)} program(s), {len(copybooks)} copybook(s), {len(others)} other member(s)"
            + (f", {len(doc_names)} document(s)" if doc_names else "") + "\n",
            "- every fact below is citable as it stands: `[[SYSTEM/MEMBER line \"token\"]]` for code, "
            "`[[DOCNAME n \"token\"]]` for a document section; the model must not read anything else\n"]
    say(f"release: {len(programs)} program(s), {len(copybooks)} copybook(s), {len(others)} other member(s)")
    if system:
        parts.append(query.cmd_diff(conn, None, None, system=system))

    # ---- 2. diffs
    for name, kind, old, new in changed:
        if old:
            say(f"diff {old} {new}")
            parts.append(query.cmd_diff(conn, old, new, budget=budget))
        else:
            parts.append(f"# Diff {new}\n\n_no previous copy of {name} ({kind}) in the index - nothing to compare; "
                         "the reports below describe the copy as it is now_\n")

    # ---- 3. walks of the new copies
    for name, _kind, _old, new in programs:
        say(f"walk {new}")
        parts.append(query.cmd_walk(conn, new, budget=budget))

    # ---- 4. copybooks: who else expands them, layout as the changed program sees it
    for name, _kind, _old, new in copybooks:
        say(f"copybook {name}")
        parts.append(query.cmd_copybook(conn, name))
        user = None
        for pname, _k, _o, pnew in programs:
            if conn.execute("SELECT 1 FROM copy_use c JOIN member m ON m.id=c.member_id WHERE UPPER(c.copybook)=? "
                            "AND UPPER(m.name)=?", (name.upper(), pname.upper())).fetchone():
                user = pnew
                break
        parts.append(query.cmd_layout(conn, name, program=user))

    # ---- 5. jobs and the CRUD matrix
    if programs:
        say("crud " + " ".join(p[0] for p in programs))
        parts.append(query.cmd_crud(conn, [p[0] for p in programs]))
        for name, _kind, _old, new in programs:
            parts.append(query.cmd_program(conn, new))

    # ---- 6. documents
    for dname in doc_names:
        say(f"doc {dname}")
        parts.append(query.cmd_doc(conn, dname))
        parts.append(query.cmd_doc(conn, dname, sections="1-9999", budget=doc_budget))

    body = "\n\n---\n\n".join(parts)
    size = len(body) + sum(len(h) for h in head)
    head.append(f"- size: {size:,} characters, about {size // 4:,} tokens - `--budget` (per walk / diff, now {budget}) "
                f"and `--doc-budget` (per document, now {doc_budget}) shrink it\n\n---\n\n")
    return "".join(head) + body


def ensure_release_facts(work_dir: str, say=print) -> str:
    """Copy the template beside the pack when the hand-written file is missing."""
    dest = os.path.join(work_dir, "release-facts.md")
    if os.path.exists(dest):
        return dest
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(here, "templates", "release-facts.md")
    if os.path.isfile(src):
        os.makedirs(work_dir, exist_ok=True)
        shutil.copy(src, dest)
        say(f"release facts: {dest} created from the template - fill it in (dates, contacts, what agents may say)")
    return dest


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Gather every report a transition document needs into one file (no model tokens).")
    ap.add_argument("--db", default="atlas.db")
    ap.add_argument("--system", nargs="?", const="", default="", help="the system folder holding the changed copies (GC-DEV, GC-TEST)")
    ap.add_argument("--members", nargs="?", const="", default="", help="changed members, comma-separated - when no second system exists")
    ap.add_argument("--docs", nargs="?", const="", default="", help="documents to include, comma-separated (names as `doc --list` shows them)")
    ap.add_argument("--docs-folder", nargs="?", const="", default="", help="include every document whose path contains this (e.g. QA)")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help=f"characters per walk / diff (default {DEFAULT_BUDGET})")
    ap.add_argument("--doc-budget", type=int, default=DEFAULT_DOC_BUDGET, help=f"characters per document (default {DEFAULT_DOC_BUDGET})")
    ap.add_argument("--out", default=os.path.join("work", "handover.md"))
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    say = (lambda s: None) if a.quiet else (lambda s: print(s, flush=True))
    conn = query.connect(a.db)
    try:
        docs = _split(a.docs)
        if a.docs_folder.strip():
            docs += [d for d in docs_in_folder(conn, a.docs_folder) if d.upper() not in docs]
        if not a.system.strip() and not _split(a.members):
            print("give --system (the folder with the changed copies) or --members (the changed members)", file=sys.stderr)
            return 2
        text = build_pack(conn, a.system, _split(a.members), docs, a.budget, a.doc_budget, say)
    finally:
        conn.close()
    query.write_out(a.out, text)
    ensure_release_facts(os.path.dirname(os.path.abspath(a.out)), say)
    say(f"pack: {a.out} ({len(text):,} characters, about {len(text) // 4:,} tokens). Next: fill in release-facts.md, "
        "then /atlas-handover in the chat (ask mode) - one request.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
