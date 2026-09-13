"""
query.py - answer questions from atlas.db as Markdown, with citations.
No model calls. Every line of output comes from a row in the index.

    python -m atlas.query program  SAMPPGM
    python -m atlas.query job      SAMPJOB
    python -m atlas.query callers  RATECALC --depth 3
    python -m atlas.query callees  SAMPPGM --depth 2
    python -m atlas.query field    PM-POLICY-STATUS
    python -m atlas.query literal  E001
    python -m atlas.query dataset  PROD.POLICY.EXTRACT
    python -m atlas.query copybook PMASTREC
    python -m atlas.query dead
    python -m atlas.query ambiguous
    python -m atlas.query coverage
    python -m atlas.query search   "WS-PLCY-STAT-CD"
    python -m atlas.query cite     SAMPPGM 25-30
    python -m atlas.query pack     SAMPPGM          # fact pack for a model

Program facts carry EXPANDED line numbers (COPY members inlined). Citations
printed here are always translated back to  MEMBER:line  in the original
member, which is what verify_citations.py checks against.

Every report ends with the unresolved items in its scope. An answer that
omits them is incomplete by construction, not merely by accident.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
from collections import defaultdict, deque
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import re

from . import cobol, reader, screens


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def connect(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        sys.exit(f"no such db: {path}  (build it: python -m atlas.build ROOT --db {path})")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _jl(v: Optional[str]) -> list:
    try:
        return json.loads(v) if v else []
    except (TypeError, ValueError):
        return []


def origin(conn: sqlite3.Connection, program_id: int, exp_line: Optional[int]
           ) -> Tuple[Optional[str], Optional[int], int, Optional[str]]:
    """(member_name, source_line, depth, via_copy) for an expanded line."""
    if exp_line is None:
        return None, None, 0, None
    r = conn.execute("""
        SELECT m.name, r.src_start + (? - r.exp_start) AS src_line, r.depth, r.via_copy
        FROM expand_run r JOIN member m ON m.id = r.src_member
        WHERE r.program_id = ? AND ? BETWEEN r.exp_start AND r.exp_end""",
        (exp_line, program_id, exp_line)).fetchone()
    if not r:
        return None, exp_line, 0, None
    return r["name"], r["src_line"], r["depth"], r["via_copy"]


def cite(conn: sqlite3.Connection, program_id: int, exp_line: Optional[int]) -> str:
    m, ln, depth, via = origin(conn, program_id, exp_line)
    if not m:
        return f"?:{exp_line}"
    tag = f"{m}:{ln}"
    if depth:
        tag += f" (via COPY {via})"
    return tag


def source_line(conn: sqlite3.Connection, member_name: str, line: int) -> str:
    r = conn.execute("SELECT text FROM src_fts WHERE member_name=? AND line_no=? LIMIT 1",
                     (member_name, line)).fetchone()
    return r["text"].strip() if r else ""


def source_line_raw(conn: sqlite3.Connection, member_name: str, line: int) -> str:
    """The code area as written (columns 8-72), indentation kept - the IF /
    ELSE nesting an analyst reads by eye."""
    r = conn.execute("SELECT text FROM src_fts WHERE member_name=? AND line_no=? LIMIT 1",
                     (member_name, line)).fetchone()
    return r["text"].rstrip() if r else ""


def programs_named(conn: sqlite3.Connection, name: str) -> List[sqlite3.Row]:
    """Programs called `name`: by PROGRAM-ID, member name, or an ENTRY alias
    (CALL 'RATEENT' reaches RATECALC)."""
    n = name.upper()
    return conn.execute("""
        SELECT p.*, m.name AS member_name, m.path, m.library, m.authoritative, m.parse_status, m.system
        FROM program p JOIN member m ON m.id = p.member_id
        WHERE UPPER(p.program_id) = ? OR UPPER(m.name) = ?
           OR p.id IN (SELECT program_id FROM program_alias WHERE UPPER(alias) = ?)
        ORDER BY m.authoritative DESC, m.path""", (n, n, n)).fetchall()


def _names_of(conn: sqlite3.Connection, name: str) -> List[str]:
    """A program plus every ENTRY alias it answers to."""
    n = name.upper()
    out = {n}
    for p in programs_named(conn, n):
        out.add(p["program_id"].upper())
        for (a,) in conn.execute("SELECT alias FROM program_alias WHERE program_id=?", (p["id"],)):
            out.add(a.upper())
    return sorted(out)


def table(headers: Sequence[str], rows: Iterable[Sequence]) -> str:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
    return "\n".join(out) + "\n"


def unresolved_for(conn: sqlite3.Connection, member_ids: Sequence[int], limit: int = 40) -> str:
    if not member_ids:
        return ""
    q = ",".join("?" * len(member_ids))
    rows = conn.execute(f"""
        SELECT m.name, u.kind, u.detail, u.line FROM unresolved u JOIN member m ON m.id = u.member_id
        WHERE u.member_id IN ({q}) ORDER BY u.kind, m.name LIMIT ?""", (*member_ids, limit)).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM unresolved WHERE member_id IN ({q})", member_ids).fetchone()[0]
    if not total:
        return "\n### Unresolved in scope\n_none - but see `coverage` for estate-wide blind spots_\n"
    out = [f"\n### Unresolved in scope ({total}) - the answer is incomplete to this extent\n"]
    out.append(table(["member", "kind", "detail", "line"],
                     [(r["name"], r["kind"], r["detail"][:120], r["line"]) for r in rows]))
    if total > limit:
        out.append(f"_...{total - limit} more_\n")
    return "".join(out)


# --------------------------------------------------------------------------
# program
# --------------------------------------------------------------------------

def cmd_program(conn: sqlite3.Connection, name: str) -> str:
    progs = programs_named(conn, name)
    if not progs:
        # No source - but the JCL, the CSD/stage-1 and other programs may still
        # know it. A load-module-only program is normal in a 40-year estate.
        n = name.upper()
        steps = conn.execute("""
            SELECT j.job_name, s.step_name, s.launcher, s.from_proc, m.name AS jm, s.line, pd.proc_name
            FROM step s LEFT JOIN job j ON j.id=s.job_id LEFT JOIN member m ON m.id=j.member_id
            LEFT JOIN proc_def pd ON pd.id=s.proc_id
            WHERE UPPER(s.effective_pgm)=?
              AND NOT (s.proc_id IS NOT NULL AND EXISTS (SELECT 1 FROM step x WHERE x.from_proc=pd.proc_name))
            ORDER BY j.job_name, s.ordinal""", (n,)).fetchall()
        callers = _callers_of(conn, n)
        tx = conn.execute("SELECT tran_code, system FROM transaction_def WHERE UPPER(program)=?", (n,)).fetchall()
        # A control-card member (PARMLIB/CNTL) is asked about by name too:
        # "where is SRTCLM used" is a job question, not a program question.
        # Effective (expanded) steps carry the job name; a bare PROC row is
        # shown only when no job expands that PROC; a job step's own
        # //PROCSTEP.DD override is shown once, on the effective step.
        cards = conn.execute("""
            SELECT j.job_name, s.step_name, s.from_proc, d.dd_name, d.dsn_resolved, m.name AS jm, d.line, pd.proc_name
            FROM dd d JOIN step s ON s.id=d.step_id LEFT JOIN job j ON j.id=s.job_id
            LEFT JOIN member m ON m.id=j.member_id LEFT JOIN proc_def pd ON pd.id=s.proc_id
            WHERE UPPER(d.card_member)=?
              AND NOT (s.proc_id IS NOT NULL AND EXISTS (SELECT 1 FROM step x WHERE x.from_proc=pd.proc_name))
              AND NOT (s.proc_called IS NOT NULL AND EXISTS (SELECT 1 FROM step x WHERE x.job_id=s.job_id
                                                            AND x.parent_step=s.step_name))
            ORDER BY j.job_name, s.ordinal, d.line""", (n,)).fetchall()
        other = conn.execute("SELECT kind, path FROM member WHERE UPPER(name)=? ORDER BY authoritative DESC",
                             (n,)).fetchall()
        if not steps and not callers and not tx and not cards and not other:
            # Listed by the host but never downloaded is a different answer
            # from "does not exist": the fetch record knows which.
            for lib in conn.execute("SELECT dataset, fetched_at, missing FROM library WHERE complete=0"):
                if n in [x.upper() for x in _jl(lib["missing"])]:
                    return (f"# {name}\n\n**NOT FETCHED** - `{lib['dataset']}` lists a member {n} (host listing "
                            f"{(lib['fetched_at'] or '?')[:10]}) but it was not downloaded. Fetch the library again "
                            f"before concluding anything about it.\n")
            return f"# {name}\n\n**NOT FOUND** - no member with this name, and nothing indexed runs or calls it.\n"
        if other:
            out = [f"# {n}\n\n**Not a program**: indexed as a `{other[0]['kind']}` member (`{other[0]['path']}`). "
                   f"What the estate knows about it:\n"]
        else:
            out = [f"# Program {n}\n\n**Source not indexed** (no member with this PROGRAM-ID or name). "
                   f"What the estate knows about it:\n"]
        if cards:
            out.append("\n### Used as control cards by\n")
            out.append(table(["job / PROC", "step", "DD", "library(member)", "cite"],
                             [(c["job_name"] or f"(PROC {c['proc_name']})", c["step_name"], c["dd_name"],
                               c["dsn_resolved"], f"{c['from_proc'] or c['jm'] or c['proc_name']}:{c['line']}")
                              for c in cards]))
            out.append("Its text is loaded as that step's cards: `job <JOB>` shows the resolved program / sort fields.\n")
        if steps:
            out.append("\n### Runs in\n")
            out.append(table(["job", "step", "launcher", "cite"],
                             [(s["job_name"] or f"(PROC {s['proc_name']})", s["step_name"], s["launcher"],
                               f"{s['from_proc'] or s['jm'] or s['proc_name']}:{s['line']}") for s in steps]))
        if tx:
            out.append("Online: " + ", ".join(f"{t['tran_code']} ({t['system']})" for t in tx) + "\n")
        if callers:
            out.append("\n### Called by\n")
            out.append(table(["caller", "kind"], callers))
        if not other:
            out.append("\n> Obtain the source or the compile listing to go further; until then its datasets, "
                       "tables and callees are unknown, not empty.\n")
        return "".join(out)
    out = [f"# Program {name.upper()}\n"]
    if len(progs) > 1:
        out.append(f"> **{len(progs)} copies indexed.** Which one is production is not knowable from "
                   f"the folder; declare it in a manifest. Facts below are per copy.\n")
    for p in progs:
        pid = p["id"]
        out.append(f"\n## {p['member_name']}  `{p['path']}`"
                   + ("  **[authoritative]**" if p["authoritative"] else "") + "\n")
        out.append(f"- PROGRAM-ID `{p['program_id']}` - {p['src_lines']} source lines, "
                   f"{p['exp_lines']} after COPY expansion - parse: {p['parse_status']}\n")
        out.append(f"- system: {p['system'] or 'not declared (add it to sources.json / manifest systems)'}\n")
        flags = [k for k in ("sql", "cics", "dli", "mq") if p[f"uses_{k}"]]
        out.append(f"- uses: {', '.join(flags) if flags else 'files only'}\n")
        lk = _jl(p["linkage_using"])
        if lk:
            out.append(f"- called with (positional): {', '.join(f'{i+1}={a}' for i, a in enumerate(lk))}\n")

        # jobs / steps
        # Effective (expanded) steps carry the job; a PROC's own row is shown only
        # when no indexed job expands that PROC.
        steps = conn.execute("""
            SELECT j.job_name, s.step_name, s.launcher, s.parm, s.from_proc, m.name AS jm, s.line,
                   pd.proc_name
            FROM step s LEFT JOIN job j ON j.id = s.job_id LEFT JOIN member m ON m.id = j.member_id
            LEFT JOIN proc_def pd ON pd.id = s.proc_id
            WHERE UPPER(s.effective_pgm) = ?
              AND NOT (s.proc_id IS NOT NULL AND EXISTS (SELECT 1 FROM step x WHERE x.from_proc = pd.proc_name))
            ORDER BY j.job_name, s.ordinal""", (p["program_id"].upper(),)).fetchall()
        out.append("\n### Runs in\n")
        out.append(table(["job", "step", "launcher", "parm/notes", "cite"],
                         [(s["job_name"] or f"(PROC {s['proc_name']}: no indexed job expands it)",
                           s["step_name"], s["launcher"], (s["parm"] or "")[:60],
                           f"{s['from_proc'] or s['jm'] or s['proc_name']}:{s['line']}") for s in steps]))
        tx = conn.execute("SELECT tran_code, system FROM transaction_def WHERE UPPER(program)=?",
                          (p["program_id"].upper(),)).fetchall()
        if tx:
            out.append("Online: " + ", ".join(f"{t['tran_code']} ({t['system']})" for t in tx) + "\n")

        # calls out
        calls = conn.execute("SELECT * FROM call_edge WHERE program_id=? ORDER BY line", (pid,)).fetchall()
        out.append("\n### Calls\n")
        out.append(table(["kind", "target", "resolution", "using", "cite"],
                         [(c["kind"], c["target"] or f"{c['via_var']} -> {', '.join(_jl(c['resolved'])) or 'UNRESOLVED'}",
                           c["resolution"], ", ".join(_jl(c["using_args"]))[:50], cite(conn, pid, c["line"]))
                          for c in calls]))

        # callers
        callers = conn.execute("""
            SELECT q.program_id AS caller, c.kind, c.line, c.program_id AS cpid
            FROM call_edge c JOIN program q ON q.id = c.program_id
            WHERE UPPER(c.target) = ? OR c.resolved LIKE ?""",
            (p["program_id"].upper(), f'%"{p["program_id"].upper()}"%')).fetchall()
        out.append("\n### Called by\n")
        out.append(table(["caller", "kind", "cite"],
                         [(c["caller"], c["kind"], cite(conn, c["cpid"], c["line"])) for c in callers]))

        # copybooks
        cps = conn.execute("""
            SELECT c.copybook, c.replacing, c.line, m.name AS resolved
            FROM copy_use c LEFT JOIN member m ON m.id = c.resolved_member_id
            WHERE c.member_id = ? ORDER BY c.line""", (p["member_id"],)).fetchall()
        out.append("\n### Copybooks\n")
        out.append(table(["copybook", "resolved to", "REPLACING", "line"],
                         [(c["copybook"], c["resolved"] or "**NOT FOUND**",
                           (c["replacing"] or "")[:40], f"{p['member_name']}:{c['line']}") for c in cps]))

        # files
        files = conn.execute("SELECT * FROM file_decl WHERE program_id=?", (pid,)).fetchall()
        frows = []
        for f in files:
            dsns = conn.execute("""
                SELECT DISTINCT d.dsn_resolved, d.mode, d.mode_source, j.job_name
                FROM dd d JOIN step s ON s.id = d.step_id LEFT JOIN job j ON j.id = s.job_id
                WHERE UPPER(s.effective_pgm)=? AND UPPER(d.dd_name) LIKE ?""",
                (p["program_id"].upper(), f"%{(f['assign_dd'] or '').upper()}")).fetchall()
            opens = conn.execute("SELECT DISTINCT op FROM io_op WHERE program_id=? AND target=?",
                                 (pid, f["select_name"])).fetchall()
            frows.append((f["select_name"], f["assign_dd"], f["organization"] or "",
                          ", ".join(o["op"] for o in opens),
                          "; ".join(f"{d['dsn_resolved']} [{d['mode']}/{d['mode_source']}] {d['job_name'] or ''}"
                                    for d in dsns) or "_no JCL found_",
                          cite(conn, pid, f["line"])))
        out.append("\n### Files (SELECT/ASSIGN -> JCL DD -> dataset)\n")
        out.append(table(["file", "DD", "org", "ops", "datasets via JCL", "cite"], frows))

        # EXEC CICS READ FILE('X') names an FCT entry; the CSD says which dataset.
        cf = conn.execute("""
            SELECT o.target, GROUP_CONCAT(DISTINCT o.op) AS ops, f.dsname
            FROM io_op o LEFT JOIN cics_file f ON UPPER(f.name)=UPPER(o.target)
            WHERE o.program_id=? AND o.target_kind='cics' GROUP BY o.target, f.dsname""", (pid,)).fetchall()
        if cf:
            out.append("\n### CICS files (FCT -> dataset, from the CSD)\n")
            out.append(table(["file", "ops", "dataset"],
                             [(c["target"], c["ops"], c["dsname"] or "**not in any indexed CSD**") for c in cf]))

        # DB2
        sqls = conn.execute("SELECT stmt_type, tables, is_dynamic, start_line FROM sql_stmt WHERE program_id=?",
                            (pid,)).fetchall()
        if sqls:
            agg: Dict[str, set] = defaultdict(set)
            first: Dict[str, int] = {}
            for s in sqls:
                for t in _jl(s["tables"]):
                    agg[t].add(s["stmt_type"])
                    first.setdefault(t, s["start_line"])
            out.append("\n### DB2\n")
            out.append(table(["table", "verbs", "first cite"],
                             [(t, ", ".join(sorted(v)), cite(conn, pid, first[t])) for t, v in sorted(agg.items())]))
            dyn = [s for s in sqls if s["is_dynamic"]]
            if dyn:
                out.append(f"**{len(dyn)} dynamic SQL statement(s)** - tables not statically knowable.\n")

        # IMS
        dli = conn.execute("""SELECT func, pcb_arg, io_area, ssa_args, line, resolution, dest, psb_name, dbd_name,
                                     procopt, pcb_source FROM dli_call WHERE program_id=? ORDER BY line""", (pid,)).fetchall()
        if dli:
            out.append("\n### IMS DL/I calls (PCB resolved through the PSB)\n")
            out.append(table(["func", "PCB arg", "database", "PROCOPT", "I/O area", "segment/SSA", "cite"],
                             [(d["func"] + (f" (via {d['resolution']})" if d["resolution"] in ("value_clause", "parmcount") else ""),
                               d["pcb_arg"] or "", d["dbd_name"] or "?", d["procopt"] or "", d["io_area"] or "",
                               ", ".join(_jl(d["ssa_args"]))[:40] or (f"-> {d['dest']}" if d["dest"] else ""),
                               cite(conn, pid, d["line"])) for d in dli]))
            srcs = sorted({d["pcb_source"] for d in dli if d["pcb_source"]})
            for s in srcs[:4]:
                out.append(f"- PCB mapping: {s}\n")
            if any(d["dbd_name"] is None for d in dli):
                out.append("> `database ?` = position not resolved (see the mapping notes); the model must not guess it.\n")
            switches = [d for d in dli if d["dest"]]
            if switches:
                out.append("- **message switch**: CHNG to " + ", ".join(sorted({d["dest"] for d in switches}))
                           + " - the ISRT that follows reaches that transaction/LTERM\n")

        # CICS commands: maps, transaction chaining, queues (facts, not name coincidence)
        cc = conn.execute("""SELECT verb, resource_kind, resource, direction, line FROM cics_cmd
                             WHERE program_id=? ORDER BY resource_kind, resource, line""", (pid,)).fetchall()
        if cc:
            out.append("\n### CICS commands (resources named in EXEC CICS)\n")
            out.append(table(["verb", "kind", "resource", "dir", "cite"],
                             [(c["verb"], c["resource_kind"], c["resource"], c["direction"] or "",
                               cite(conn, pid, c["line"])) for c in cc]))
        aliases = conn.execute("SELECT alias, linkage_using, line FROM program_alias WHERE program_id=?", (pid,)).fetchall()
        if aliases:
            out.append("- ENTRY points: " + ", ".join(f"`{a['alias']}` USING {', '.join(_jl(a['linkage_using'])) or '-'} "
                                                     f"@{cite(conn, pid, a['line'])}" for a in aliases) + "\n")

        # interfaces
        ifs = conn.execute("SELECT kind, detail, direction, line FROM interface_edge WHERE member_id=?",
                           (p["member_id"],)).fetchall()
        if ifs:
            out.append("\n### External interfaces\n")
            out.append(table(["kind", "detail", "dir", "line"],
                             [(i["kind"], i["detail"], i["direction"], i["line"]) for i in ifs]))

        # paragraphs
        paras = conn.execute("SELECT name, section, kind, start_line, end_line FROM paragraph WHERE program_id=? ORDER BY ordinal",
                             (pid,)).fetchall()
        # Reached by ANY edge kind: PERFORM, GO TO, ALTER, fall-through, a
        # THRU range, or a PERFORMed SECTION that contains the paragraph.
        edges = conn.execute("SELECT to_para, thru_para, kind FROM perform_edge WHERE program_id=?", (pid,)).fetchall()
        reached: Dict[str, set] = defaultdict(set)
        for e in edges:
            reached[e["to_para"].split(" OF ")[0]].add(e["kind"])
        names = [q["name"] for q in paras if q["kind"] == "paragraph"]
        for e in edges:
            if e["thru_para"] and e["to_para"] in names and e["thru_para"] in names:
                lo, hi = sorted((names.index(e["to_para"]), names.index(e["thru_para"])))
                for n_ in names[lo:hi + 1]:
                    reached[n_].add("thru")
        for q in paras:
            if q["kind"] == "paragraph" and q["section"] and q["section"] in reached:
                reached[q["name"]].add("section")
        plain = [q for q in paras if q["kind"] == "paragraph"]
        never = [q["name"] for q in plain[1:] if q["name"] not in reached]
        secs = sum(1 for q in paras if q["kind"] == "section")
        by_kind = defaultdict(int)
        for q in plain:
            for k in reached.get(q["name"], ()):
                by_kind[k] += 1
        out.append(f"\n### Structure\n- {len(plain)} paragraphs" + (f" in {secs} sections" if secs else "")
                   + f"; reached by: " + ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items()))
                   + f"; **{len(never)} reached by nothing** (no PERFORM, GO TO, fall-through or THRU range): "
                   f"{', '.join(never[:12])}{' ...' if len(never) > 12 else ''}\n")

        # literals set here
        lits = conn.execute("""
            SELECT literal, field, COUNT(*) n FROM literal_ref
            WHERE program_id=? AND context='move_to' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15""", (pid,)).fetchall()
        if lits:
            out.append("- codes/literals set here: " + ", ".join(f"'{l['literal']}'->{l['field']}" for l in lits) + "\n")

        out.append(unresolved_for(conn, [p["member_id"]]))
    out.append(_docs_section(conn, name.upper()))
    return "".join(out)


# --------------------------------------------------------------------------
# job
# --------------------------------------------------------------------------

def cmd_job(conn: sqlite3.Connection, name: str) -> str:
    jobs = conn.execute("""
        SELECT j.*, m.name AS member_name, m.path, m.authoritative FROM job j JOIN member m ON m.id=j.member_id
        WHERE UPPER(j.job_name)=? OR UPPER(m.name)=? ORDER BY m.authoritative DESC""",
        (name.upper(), name.upper())).fetchall()
    procs = conn.execute("""
        SELECT p.*, m.name AS member_name, m.path FROM proc_def p JOIN member m ON m.id=p.member_id
        WHERE UPPER(p.proc_name)=? OR UPPER(m.name)=?""", (name.upper(), name.upper())).fetchall()
    incl = conn.execute("""SELECT m.name, j.job_name FROM include_use i JOIN member m ON m.id=i.member_id
                           LEFT JOIN job j ON j.member_id=m.id WHERE UPPER(i.include_member)=? ORDER BY m.name""",
                        (name.upper(),)).fetchall()
    if not jobs and not procs:
        if incl:
            return (f"# {name.upper()}\n\nAn INCLUDE member, not a job: its statements are spliced into "
                    + ", ".join(f"{r['job_name'] or r['name']}" for r in incl)
                    + f". Run `job <JOB>` for the effective steps; `cite {name.upper()} a-b` for its text.\n")
        return f"# {name}\n\n**NOT FOUND** - no job or PROC with this name is indexed.\n"
    out = [f"# Job {name.upper()}\n"]
    if incl:
        out.append("Included by (INCLUDE MEMBER=): " + ", ".join(r["job_name"] or r["name"] for r in incl) + "\n")
    for j in jobs:
        spliced = [r[0] for r in conn.execute("SELECT include_member FROM include_use WHERE member_id=? ORDER BY 1",
                                              (j["member_id"],))]
        if spliced:
            out.append(f"INCLUDE members spliced in: {', '.join(spliced)} (their DDs appear under the steps below)\n")
    sched = conn.execute("SELECT * FROM sched_dep WHERE UPPER(job_name)=? OR UPPER(depends_on)=?",
                         (name.upper(), name.upper())).fetchall()
    if sched:
        out.append("Scheduler: " + "; ".join(f"{s['job_name']} <- {s['depends_on']} ({s['kind']})" for s in sched) + "\n")
    else:
        out.append("> No scheduler export loaded: predecessor/trigger relationships are **unknown**, "
                   "not absent.\n")

    def render_step(s, member_name: str, job_for_children: Optional[int]) -> None:
        cite_member = s["from_proc"] or member_name
        tag = f"  (from PROC {s['from_proc']})" if s["from_proc"] else ""
        out.append(f"\n### {s['step_name']}{tag}  `{cite_member}:{s['line']}`\n")
        what = s["effective_pgm"] or (f"PROC {s['proc_called']}" if s["proc_called"] else "?")
        out.append(f"- runs **{what}**" + (f" (JCL says PGM={s['pgm']} - launcher)" if s["launcher"] else "")
                   + (f"; COND={s['cond']}" if s["cond"] else "") + "\n")
        if s["guard"]:
            out.append(f"- **runs only when** `{s['guard']}` - not the normal flow of the job\n")
        if s["parm"]:
            out.append(f"- PARM/notes: `{s['parm'][:200]}`\n")
        dds = conn.execute("SELECT * FROM dd WHERE step_id=? ORDER BY line, id", (s["id"],)).fetchall()
        drows = []
        for d in dds:
            if d["dsn_resolved"]:
                g = f"({d['gdg_rel']})" if d["gdg_rel"] else ""
                ovr = " (override)" if d["is_override"] else ""
                extra = ""
                if d["is_temp"]:
                    extra += " (job-local &&)"
                if d["dsn"] and d["dsn"].startswith("*."):
                    extra += f" (via {d['dsn']})"
                if d["card_member"]:
                    n = len(d["sysin_text"].strip().splitlines()) if d["sysin_text"] else 0
                    extra += (f" -> {n} card lines loaded from member" if n
                              else " -> card member NOT indexed: this step's cards are unknown")
                drows.append((d["dd_name"] or "  +concat", f"{d['dsn_resolved']}{g}{ovr}{extra}", d["disp"] or "",
                              f"{d['mode']} [{d['mode_source']}]"))
            elif d["sysin_text"]:
                first = d["sysin_text"].strip().splitlines()
                drows.append((d["dd_name"], f"inline cards ({len(first)} lines): "
                              + " / ".join(x.strip() for x in first[:3])[:90], "", "control"))
            elif d["dsn"]:
                why = "referback: target step/DD not found" if d["dsn"].startswith("*.") else "unresolved"
                drows.append((d["dd_name"], f"{d['dsn']} ({why})", d["disp"] or "", "unknown"))
        if drows:
            out.append(table(["DD", "dataset / cards", "DISP", "direction [source]"], drows))
        cards = conn.execute("SELECT card_kind, pos, length, fmt FROM card_field_ref WHERE step_id=? ORDER BY pos",
                             (s["id"],)).fetchall()
        if cards:
            out.append("- sort card byte positions: " + ", ".join(
                f"{c['card_kind']} {c['pos']}-{c['pos'] + c['length'] - 1} {c['fmt'] or ''}" for c in cards) + "\n")
        if s["proc_called"]:
            kids = conn.execute(
                "SELECT * FROM step WHERE job_id=? AND parent_step=? AND from_proc IS NOT NULL ORDER BY ordinal",
                (job_for_children, s["step_name"])).fetchall() if job_for_children else []
            if kids:
                out.append(f"- expands PROC {s['proc_called']}: effective steps below, with this job's "
                           f"symbolics and //STEP.DD overrides applied\n")
                for k in kids:
                    render_step(k, member_name, job_for_children)
            else:
                pd = conn.execute("SELECT p.id, m.name FROM proc_def p JOIN member m ON m.id=p.member_id "
                                  "WHERE UPPER(p.proc_name)=?", (s["proc_called"].upper(),)).fetchone()
                if pd:
                    out.append(f"- PROC {s['proc_called']} ({pd['name']}) is indexed but could not be expanded "
                               f"here; `job {s['proc_called']}` shows it with symbolics unresolved\n")
                else:
                    out.append(f"- **PROC {s['proc_called']} NOT FOUND** in the index - its steps are unknown\n")

    def render_steps(where: str, arg, member_name: str, job_for_children: Optional[int] = None) -> None:
        for s in conn.execute(f"SELECT * FROM step WHERE {where} ORDER BY ordinal", (arg,)).fetchall():
            render_step(s, member_name, job_for_children)

    mids = []
    for j in jobs:
        out.append(f"\n## {j['member_name']}  `{j['path']}`" + ("  **[authoritative]**" if j["authoritative"] else "") + "\n")
        if j["job_cond"]:
            out.append(f"JOB card COND={j['job_cond']}: applies to every step below.\n")
        if j["jcllib"]:
            out.append("JCLLIB ORDER: " + ", ".join(_jl(j["jcllib"])) + " (PROCs and INCLUDEs are searched there first)\n")
        if j["joblib"]:
            out.append("JOBLIB: " + ", ".join(_jl(j["joblib"])) + " - the load libraries of every step without "
                       "its own STEPLIB (shown per step as `[joblib]`)\n")
        inproc = conn.execute("SELECT proc_name FROM proc_def WHERE member_id=? AND instream=1 ORDER BY line",
                              (j["member_id"],)).fetchall()
        if inproc:
            out.append("Instream PROCs defined in this member (they shadow cataloged PROCs of the same name): "
                       + ", ".join(p["proc_name"] for p in inproc) + "\n")
        render_steps("job_id=? AND from_proc IS NULL", j["id"], j["member_name"], j["id"])
        temps = conn.execute("""
            SELECT d.dsn_resolved AS dsn, s.step_name, d.dd_name, d.mode FROM dd d JOIN step s ON s.id=d.step_id
            WHERE s.job_id=? AND d.is_temp=1 AND NOT (s.proc_called IS NOT NULL AND d.dd_name LIKE '%.%')
            ORDER BY d.dsn_resolved, s.ordinal, d.line""", (j["id"],)).fetchall()
        if temps:
            by: Dict[str, List[str]] = {}
            for t in temps:
                by.setdefault(t["dsn"], []).append(f"{t['step_name']} {t['dd_name']} ({t['mode']})")
            out.append("\n### Job-local (&&) datasets\n")
            out.append("These exist only while this job runs. No other job can read them, so they are "
                       "not dataset lineage and `dataset` hides them:\n")
            for dsn, uses in by.items():
                out.append(f"- `{dsn}`: " + " -> ".join(uses) + "\n")
        mids.append(j["member_id"])
    for p in procs:
        out.append(f"\n## PROC {p['proc_name']}  `{p['path']}`  symbolics: `{p['symbolics'] or '{}'}`\n")
        execs = conn.execute("""SELECT j.job_name, s.step_name, s.line, m.name AS mem FROM step s
                                JOIN job j ON j.id=s.job_id JOIN member m ON m.id=j.member_id
                                WHERE UPPER(s.proc_called)=? AND s.from_proc IS NULL ORDER BY j.job_name""",
                             (p["proc_name"].upper(),)).fetchall()
        if execs:
            out.append(f"Executed by ({len(execs)}): " + "; ".join(
                f"{e['job_name']} {e['step_name']} @{e['mem']}:{e['line']}" for e in execs[:40]) + "\n")
            out.append("The steps below carry the PROC's DEFAULT symbolics; `job <JOB>` shows them with the job's values.\n")
        else:
            out.append("Executed by: **no indexed job** (its default symbolics below are all the index knows).\n")
        render_steps("proc_id=?", p["id"], p["member_name"])
        mids.append(p["member_id"])
    out.append(unresolved_for(conn, mids))
    out.append(_docs_section(conn, name.upper()))
    return "".join(out)


# --------------------------------------------------------------------------
# call graph
# --------------------------------------------------------------------------

def _callees_of(conn: sqlite3.Connection, name: str) -> Tuple[List[Tuple[str, str]], int]:
    rows = conn.execute("""
        SELECT c.kind, c.target, c.resolved, c.resolution FROM call_edge c JOIN program p ON p.id=c.program_id
        WHERE UPPER(p.program_id)=?""", (name.upper(),)).fetchall()
    out, unres = [], 0
    for r in rows:
        if r["target"]:
            out.append((r["target"], r["kind"]))
        else:
            res = _jl(r["resolved"])
            if res:
                out.extend((t, "dynamic") for t in res)
            else:
                unres += 1
    return out, unres


def _callers_of(conn: sqlite3.Connection, name: str) -> List[Tuple[str, str]]:
    """Programs that reach `name`: CALL / LINK / XCTL / SQL CALL to the
    program or any ENTRY alias of it, plus START/RETURN TRANSID and IMS
    message switches to a transaction that the CSD / stage-1 routes to it."""
    names = _names_of(conn, name)
    out: List[Tuple[str, str]] = []
    seen = set()
    for n in names:
        for r in conn.execute("""
                SELECT DISTINCT p.program_id, c.kind FROM call_edge c JOIN program p ON p.id=c.program_id
                WHERE (UPPER(c.target)=? OR c.resolved LIKE ?) AND c.kind NOT IN ('cics_start','cics_return','ims_switch')""",
                              (n, f'%"{n}"%')):
            if (r["program_id"], r["kind"]) not in seen:
                seen.add((r["program_id"], r["kind"]))
                out.append((r["program_id"], r["kind"] + ("" if n == name.upper() else f" via ENTRY {n}")))
        for r in conn.execute("""
                SELECT DISTINCT p.program_id, c.kind, t.tran_code FROM call_edge c JOIN program p ON p.id=c.program_id
                JOIN transaction_def t ON (UPPER(c.target)=UPPER(t.tran_code) OR c.resolved LIKE '%"' || UPPER(t.tran_code) || '"%')
                WHERE UPPER(t.program)=? AND c.kind IN ('cics_start','cics_return','ims_switch')""", (n,)):
            key = (r["program_id"], r["kind"] + " " + r["tran_code"])
            if key not in seen:
                seen.add(key)
                out.append((r["program_id"], f"{r['kind']} (transaction {r['tran_code']})"))
    return out


def _call_site(conn: sqlite3.Connection, caller: str, callee: str) -> Tuple[str, List[str]]:
    """(cite, USING list) of one call site from caller to callee."""
    names = _names_of(conn, callee)
    for p in programs_named(conn, caller):
        for n in names:
            r = conn.execute("""SELECT line, using_args FROM call_edge WHERE program_id=? AND
                                (UPPER(target)=? OR resolved LIKE ?) ORDER BY line LIMIT 1""",
                             (p["id"], n, f'%"{n}"%')).fetchone()
            if r:
                return cite(conn, p["id"], r["line"]), _jl(r["using_args"])
    return "?", []


def cmd_graph(conn: sqlite3.Connection, name: str, direction: str, depth: int, args: bool = False) -> str:
    out = [f"# {'Callers' if direction == 'callers' else 'Callees'} of {name.upper()} (depth {depth})\n"]
    seen = {name.upper()}
    q = deque([(name.upper(), 0)])
    rows = []
    edge_seen = set()
    unresolved_total = 0
    missing = []
    while q:
        cur, d = q.popleft()
        if d >= depth:
            continue
        if direction == "callers":
            nxt = _callers_of(conn, cur)
        else:
            nxt, u = _callees_of(conn, cur)
            unresolved_total += u
        for tgt, kind in nxt:
            if (cur, tgt, kind) in edge_seen:
                continue                 # several call sites, one edge
            edge_seen.add((cur, tgt, kind))
            caller, callee = (tgt, cur) if direction == "callers" else (cur, tgt)
            ct, _u = _call_site(conn, caller, callee)
            rows.append((d + 1, cur, tgt, kind, ct))
            if tgt not in seen:
                seen.add(tgt)
                if not programs_named(conn, tgt):
                    missing.append(tgt)
                q.append((tgt, d + 1))
    out.append(table(["depth", "from", "to", "kind", "cite"] if direction == "callees"
                     else ["depth", "callee", "caller", "kind", "cite"], rows))
    if args and direction == "callers":
        # Every call site with its full USING list against the callee's
        # LINKAGE: a count mismatch is the S0C4 waiting to happen.
        callee = programs_named(conn, name)
        expect = _jl(callee[0]["linkage_using"]) if callee else []
        out.append(f"\n### Call sites and arguments (callee LINKAGE: {len(expect)} - {', '.join(expect) or 'none'})\n")
        srows = []
        for r in conn.execute("""SELECT p.program_id, p.id AS pid, c.kind, c.using_args, c.line FROM call_edge c
                                 JOIN program p ON p.id=c.program_id
                                 WHERE UPPER(c.target)=? OR c.resolved LIKE ? ORDER BY p.program_id, c.line""",
                              (name.upper(), f'%"{name.upper()}"%')):
            ua = _jl(r["using_args"])
            flag = ""
            if expect and ua and len(ua) != len(expect):
                flag = f"**passes {len(ua)}, callee expects {len(expect)}**"
            elif expect and not ua and r["kind"] not in ("cics_link", "cics_xctl"):
                flag = "no USING list seen"
            srows.append((r["program_id"], r["kind"], ", ".join(ua) or "-", cite(conn, r["pid"], r["line"]), flag))
        out.append(table(["caller", "kind", "USING (positional)", "cite", "check"], srows))
        out.append("> BY REFERENCE is the default: the callee can change every argument. COMMAREA is the "
                   "single positional argument of a LINK/XCTL.\n")
    if unresolved_total:
        out.append(f"\n**{unresolved_total} dynamic CALL(s) with no resolvable target in this graph** - "
                   f"the graph is incomplete to that extent.\n")
    if missing:
        out.append(f"\nReferenced but NOT in index (no source, or a name mismatch member/PROGRAM-ID/load): "
                   f"{', '.join(sorted(set(missing))[:30])}\n")
    return "".join(out)


# --------------------------------------------------------------------------
# field / literal
# --------------------------------------------------------------------------

def _field_defs(conn: sqlite3.Connection, name: str) -> List[sqlite3.Row]:
    return conn.execute("""
        SELECT f.*, m.name AS member_name, m.kind, m.path FROM field f JOIN member m ON m.id=f.member_id
        WHERE UPPER(f.name)=? ORDER BY m.kind, m.name""", (name.upper(),)).fetchall()


def _root_of(conn: sqlite3.Connection, field_id: int) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM field WHERE id=?", (field_id,)).fetchone()
    while r and r["parent_id"]:
        r = conn.execute("SELECT * FROM field WHERE id=?", (r["parent_id"],)).fetchone()
    return r


def flow_from(conn: sqlite3.Connection, program_id: int, program_name: str, field: str) -> List[str]:
    """Where does the VALUE in `field` go after this program? Three routes:
    positional CALL USING to a callee, positional LINKAGE back to a caller,
    and the same BYTES of a file record read by another program.
    """
    lines: List[str] = []
    prog = conn.execute("SELECT * FROM program WHERE id=?", (program_id,)).fetchone()

    # 1. passed out via CALL ... USING (position -> callee LINKAGE)
    for c in conn.execute("SELECT * FROM call_edge WHERE program_id=? AND using_args IS NOT NULL", (program_id,)):
        args = [a.upper() for a in _jl(c["using_args"])]
        if field.upper() not in args:
            continue
        pos = args.index(field.upper())
        targets = [c["target"]] if c["target"] else _jl(c["resolved"])
        if not targets:
            lines.append(f"- passed as arg {pos + 1} of an UNRESOLVED dynamic CALL {c['via_var']} "
                         f"({cite(conn, program_id, c['line'])}) - destination unknown")
        for t in targets:
            for callee in programs_named(conn, t):
                lk = _jl(callee["linkage_using"])
                if pos < len(lk):
                    cf = lk[pos]
                    refs = conn.execute("""SELECT mode, stmt, line FROM field_ref WHERE program_id=? AND name=?
                                           AND mode IN ('display','write','test') ORDER BY line""",
                                        (callee["id"], cf)).fetchall()
                    r = "; ".join(f"{x['mode']} {x['stmt']} @{cite(conn, callee['id'], x['line'])}" for x in refs[:6])
                    lines.append(f"- CALL {t} arg {pos + 1} -> **{t}.{cf}** ({cite(conn, program_id, c['line'])})"
                                 + (f": {r}" if r else ": no display/write/test refs in callee"))
                else:
                    lines.append(f"- CALL {t} arg {pos + 1}: callee has only {len(lk)} LINKAGE params - mismatch")
            if not programs_named(conn, t):
                lines.append(f"- CALL {t} arg {pos + 1}: **{t} not in index**")

    # 2. returned to callers via LINKAGE position
    lk = [a.upper() for a in _jl(prog["linkage_using"])]
    if field.upper() in lk:
        pos = lk.index(field.upper())
        for cr in conn.execute("""SELECT c.*, q.program_id AS caller FROM call_edge c JOIN program q ON q.id=c.program_id
                                  WHERE UPPER(c.target)=? OR c.resolved LIKE ?""",
                               (program_name.upper(), f'%"{program_name.upper()}"%')):
            args = _jl(cr["using_args"])
            if pos < len(args):
                caf = args[pos]
                refs = conn.execute("""SELECT mode, stmt, line FROM field_ref WHERE program_id=? AND name=?
                                       AND mode IN ('display','write','test') ORDER BY line""",
                                    (cr["program_id"], caf)).fetchall()
                r = "; ".join(f"{x['mode']} {x['stmt']} @{cite(conn, cr['program_id'], x['line'])}" for x in refs[:6])
                lines.append(f"- returned to caller **{cr['caller']}.{caf}** (LINKAGE pos {pos + 1}, "
                             f"{cite(conn, cr['program_id'], cr['line'])})" + (f": {r}" if r else ""))

    # 3. written to a file record, read by another program at the same bytes
    frow = conn.execute("SELECT * FROM field WHERE member_id=? AND UPPER(name)=?",
                        (prog["member_id"], field.upper())).fetchone()
    if frow:
        root = _root_of(conn, frow["id"])
        fd = conn.execute("SELECT * FROM file_decl WHERE program_id=? AND fd_record=?",
                          (program_id, root["name"])).fetchone() if root else None
        if fd and fd["assign_dd"]:
            lo, hi = frow["offset"] + 1, frow["offset"] + frow["length"]
            dsns = conn.execute("""SELECT DISTINCT d.dsn_resolved FROM dd d JOIN step s ON s.id=d.step_id
                                   WHERE UPPER(s.effective_pgm)=? AND UPPER(d.dd_name) LIKE ? AND d.dsn_resolved IS NOT NULL""",
                                (program_name.upper(), f"%{fd['assign_dd'].upper()}")).fetchall()
            for dsn in dsns:
                readers_ = conn.execute("""SELECT DISTINCT s.effective_pgm, d.dd_name FROM dd d JOIN step s ON s.id=d.step_id
                                           WHERE d.dsn_resolved=? AND UPPER(s.effective_pgm)<>?""",
                                        (dsn[0], program_name.upper())).fetchall()
                for rp in readers_:
                    for other in programs_named(conn, rp["effective_pgm"] or ""):
                        ofd = conn.execute("SELECT fd_record FROM file_decl WHERE program_id=? AND UPPER(assign_dd)=?",
                                           (other["id"], rp["dd_name"].upper().split(".")[-1])).fetchone()
                        if not ofd:
                            continue
                        twins = conn.execute("""SELECT f.name FROM field f JOIN field r ON r.id=(
                                                   WITH RECURSIVE up(id,pid) AS (SELECT f.id,f.parent_id UNION ALL
                                                   SELECT x.id,x.parent_id FROM field x JOIN up ON x.id=up.pid)
                                                   SELECT id FROM up WHERE pid IS NULL)
                                                WHERE f.member_id=? AND r.name=? AND f.offset+1<=? AND f.offset+f.length>=? AND f.is_group=0""",
                                             (other["member_id"], ofd["fd_record"], hi, lo)).fetchall()
                        for tw in twins:
                            refs = conn.execute("""SELECT mode, stmt, line FROM field_ref WHERE program_id=? AND name=?
                                                   AND mode IN ('display','write','test') ORDER BY line""",
                                                (other["id"], tw["name"])).fetchall()
                            r = "; ".join(f"{x['mode']} {x['stmt']} @{cite(conn, other['id'], x['line'])}" for x in refs[:6])
                            lines.append(f"- bytes {lo}-{hi} of `{dsn[0]}` read by **{other['program_id']}.{tw['name']}**"
                                         + (f": {r}" if r else ""))
                        if not twins:
                            lines.append(f"- `{dsn[0]}` is read by {other['program_id']} but no field covers bytes "
                                         f"{lo}-{hi} in its {ofd['fd_record']} (copybook missing or layout differs)")
    return lines


def cmd_field(conn: sqlite3.Connection, name: str, show_all: bool = False,
              program: Optional[str] = None) -> str:
    defs = _field_defs(conn, name)
    out = [f"# Field {name.upper()}\n"]
    names = [name.upper()]
    if not defs:
        # An 88-level name (PM-LAPSED): the question is really about its
        # parent field and the value it stands for.
        c88 = conn.execute("""SELECT c.name, c.values_lit, f.name AS parent, f.offset, f.length, f.pic, m.name AS mem, c.line
                              FROM cond88 c JOIN field f ON f.id=c.field_id JOIN member m ON m.id=f.member_id
                              WHERE UPPER(c.name)=?""", (name.upper(),)).fetchall()
        if c88:
            out.append(f"`{name.upper()}` is an **88-level condition name**, not a field:\n")
            out.append(table(["88 name", "parent field", "offset", "len", "PIC", "values", "cite"],
                             [(c["name"], c["parent"], c["offset"], c["length"], c["pic"] or "",
                               " ".join(_jl(c["values_lit"])), f"{c['mem']}:{c['line']}") for c in c88]))
            parents = sorted({c["parent"].upper() for c in c88})
            out.append(f"References below cover the 88 name itself (`SET ... TO TRUE`, `IF {name.upper()}`) - "
                       f"run `field {parents[0]}` for the parent's own references and `values {parents[0]}` "
                       f"for every value the code assumes.\n")
            defs = []
        else:
            out.append("**NOT DEFINED** in any indexed copybook or program (check spelling, REPLACING renames, "
                       "or an 88-level name - try `literal`).\n")
    # COPY ... REPLACING renamed it in some programs: their references are
    # indexed under the new name.
    for a in conn.execute("SELECT DISTINCT new_name FROM field_alias WHERE UPPER(orig_name)=?", (name.upper(),)):
        if a["new_name"].upper() not in names:
            names.append(a["new_name"].upper())
    for a in conn.execute("SELECT DISTINCT orig_name FROM field_alias WHERE UPPER(new_name)=?", (name.upper(),)):
        if a["orig_name"].upper() not in names:
            names.append(a["orig_name"].upper())
    if len(names) > 1:
        out.append(f"Also known as **{', '.join(names[1:])}** (COPY REPLACING) - references below include those names.\n")
    out.append("\n### Definitions\n")
    out.append(table(["member", "kind", "lvl", "PIC", "usage", "offset", "len", "under", "line"],
                     [(d["member_name"], d["kind"], d["level"], d["pic"] or "(group)", d["usage"] or "",
                       d["offset"], d["length"],
                       (d["qualified"].rsplit(".", 1)[0] if "." in d["qualified"] else "")
                       .replace("*COPYBOOK-FRAGMENT*", "(fragment: 01 is in the including program)"),
                       d["line"]) for d in defs]))
    layouts = {(d["offset"], d["length"]) for d in defs if d["kind"] == "copybook"}
    if len(layouts) > 1:
        out.append("> **VERSION SKEW**: this field sits at different offsets/lengths in different copybooks "
                   "- data is already being read differently by different programs.\n")
    cps = {d["member_name"] for d in defs if d["kind"] == "copybook"}
    users = []
    for cb in sorted(cps):
        rows = conn.execute("""SELECT DISTINCT m.name FROM copy_use c JOIN member m ON m.id=c.member_id
                               WHERE UPPER(c.copybook)=? AND m.kind='cobol'""", (cb,)).fetchall()
        users.append((cb, len(rows), ", ".join(r["name"] for r in rows[:25]) + (" ..." if len(rows) > 25 else "")))
    if users:
        out.append("\n### Programs including the defining copybooks\n")
        out.append(table(["copybook", "programs", "names"], users))
    c88 = conn.execute("""SELECT c.name, c.values_lit, m.name AS mem FROM cond88 c JOIN field f ON f.id=c.field_id
                          JOIN member m ON m.id=f.member_id WHERE UPPER(f.name)=?""", (name.upper(),)).fetchall()
    if c88:
        out.append("\n### 88-levels (test conditions for free)\n")
        out.append(table(["88 name", "values", "member"], [(c["name"], " ".join(_jl(c["values_lit"])), c["mem"]) for c in c88]))

    qn = ",".join("?" * len(names))
    pfilter = " AND UPPER(p.program_id)=?" if program else ""
    refs = conn.execute(f"""SELECT p.program_id, p.id AS pid, r.name, r.mode, r.stmt, r.line FROM field_ref r
                            JOIN program p ON p.id=r.program_id
                            WHERE UPPER(r.name) IN ({qn}){pfilter} ORDER BY r.mode, p.program_id, r.line""",
                        (*names, *([program.upper()] if program else []))).fetchall()
    # Every program appears, never silently dropped: one cite per (program,
    # statement kind) with the count, and `--all` for every site.
    out.append("\n### References by mode" + (" (every site)" if show_all else " (one cite per program and statement; `--all` for every site)") + "\n")
    for mode in ("write", "display", "test", "read"):
        rs = [r for r in refs if r["mode"] == mode]
        if not rs:
            continue
        if show_all:
            items = [f"{r['program_id']} {r['stmt']} @{cite(conn, r['pid'], r['line'])}"
                     + (f" as {r['name']}" if r["name"].upper() != name.upper() else "") for r in rs]
        else:
            grp: Dict[Tuple[str, str], List[sqlite3.Row]] = defaultdict(list)
            for r in rs:
                grp[(r["program_id"], r["stmt"])].append(r)
            items = [f"{pg} {stmt} x{len(v)} @{cite(conn, v[0]['pid'], v[0]['line'])}"
                     + (f" as {v[0]['name']}" if v[0]["name"].upper() != name.upper() else "")
                     for (pg, stmt), v in sorted(grp.items())]
        out.append(f"- **{mode}** ({len(rs)} in {len({r['program_id'] for r in rs})} programs): " + "; ".join(items) + "\n")
    lits = conn.execute(f"""SELECT l.literal, l.context, p.program_id, l.line, p.id AS pid FROM literal_ref l
                            LEFT JOIN program p ON p.id=l.program_id
                            WHERE UPPER(l.field) IN ({qn}) OR {' OR '.join('UPPER(l.field) LIKE ?' for _ in names)}
                            ORDER BY l.context, l.literal""", (*names, *[f"{x}/%" for x in names])).fetchall()
    if lits:
        out.append("\n### Literal values associated\n")
        rows, seen = [], set()
        for l in lits:
            ct = cite(conn, l["pid"], l["line"]) if l["pid"] else f"{defs[0]['member_name'] if defs else '?'}:{l['line']}"
            key = (l["literal"], l["context"], ct.split(" (via")[0])
            if key in seen:
                continue
            seen.add(key)
            rows.append((l["literal"], l["context"], l["program_id"] or "(copybook)", ct))
        cap = None if show_all else 60
        out.append(table(["literal", "how", "program", "cite"], rows[:cap] if cap else rows))
        if cap and len(rows) > cap:
            dropped = sorted({r[2] for r in rows[cap:]})
            out.append(f"_...{len(rows) - cap} more (programs: {', '.join(dropped[:20])}); `--all` shows them_\n")
    out.append("\n> Group-level MOVEs (MOVE REC-A TO REC-B) touch this field without naming it; check the "
               "parents listed under 'under' with `field <parent>`.\n")

    # DB2 columns this field is loaded from / stored to (column-level lineage)
    sc = conn.execute("""SELECT c.tbl, c.col, c.mode, c.stmt, c.line, p.program_id, p.id AS pid
                         FROM sql_col_ref c JOIN program p ON p.id=c.program_id
                         WHERE UPPER(c.host_var)=? ORDER BY c.mode, p.program_id, c.line""", (name.upper(),)).fetchall()
    if sc:
        out.append("\n### DB2 columns (read = column -> this field; write = this field -> column)\n")
        out.append(table(["table", "column", "mode", "stmt", "program", "cite"],
                         [(r["tbl"], r["col"], r["mode"], r["stmt"], r["program_id"], cite(conn, r["pid"], r["line"]))
                          for r in sc]))

    # IMS: DL/I calls whose I/O area (the 01 this field lives under) is read or written
    roots = set()
    for d in defs:
        r = _root_of(conn, d["id"])
        if r and r["name"] != "*COPYBOOK-FRAGMENT*":
            roots.add(r["name"].upper())
    if roots:
        q = ",".join("?" * len(roots))
        dl = conn.execute(f"""SELECT d.func, d.pcb_arg, d.io_area, d.line, p.program_id, p.id AS pid
                              FROM dli_call d JOIN program p ON p.id=d.program_id
                              WHERE UPPER(d.io_area) IN ({q}) ORDER BY p.program_id, d.line""", tuple(roots)).fetchall()
        if dl:
            out.append("\n### IMS DL/I calls whose I/O area holds this field\n")
            out.append(table(["program", "func", "PCB (positional)", "I/O area", "cite"],
                             [(r["program_id"], r["func"], r["pcb_arg"], r["io_area"], cite(conn, r["pid"], r["line"]))
                              for r in dl]))
            out.append("> GU/GN/GHU read the segment INTO the area; ISRT/REPL write it FROM the area. Map the PCB "
                       "through the PSB (`program` dossier) to name the database and segment.\n")

    out.append(_screen_section(conn, name.upper()))

    # sort cards overlapping this field's bytes (for copybook definitions)
    hits = []
    for d in defs:
        if d["kind"] != "copybook":
            continue
        lo, hi = d["offset"] + 1, d["offset"] + d["length"]
        rows = conn.execute("""SELECT DISTINCT j.job_name, s.step_name, c.card_kind, c.pos, c.length
            FROM card_field_ref c JOIN step s ON s.id=c.step_id LEFT JOIN job j ON j.id=s.job_id
            WHERE c.pos<=? AND c.pos+c.length-1>=?""", (hi, lo)).fetchall()
        for r in rows:
            hits.append((d["member_name"], f"{lo}-{hi}", r["job_name"], r["step_name"], r["card_kind"], f"{r['pos']}-{r['pos']+r['length']-1}"))
    if hits:
        out.append("\n### Sort/control cards addressing these bytes (any dataset - verify the record type matches)\n")
        out.append(table(["copybook", "field bytes", "job", "step", "card", "card bytes"], hits[:40]))
    out.append(_docs_section(conn, name.upper()))
    return "".join(out)


def cmd_literal(conn: sqlite3.Connection, value: str, field: Optional[str] = None,
                like: bool = False) -> str:
    v = value.strip().strip("'\"")
    where = "UPPER(l.literal) LIKE UPPER(?)" if like else "(l.literal=? OR UPPER(l.literal)=?)"
    args: list = [v if "%" in v else f"%{v}%"] if like else [v, v.upper()]
    if field:
        # Common values (3, 'M') appear everywhere; the field is what makes them specific.
        where += " AND (UPPER(l.field)=? OR UPPER(l.field) LIKE ?)"
        args += [field.upper(), f"{field.upper()}/%"]
    rows = conn.execute(f"""SELECT l.literal, l.context, l.field, l.line, l.member_id,
                                  p.id AS pid, p.program_id AS pname, m.name AS member_name, m.kind
                           FROM literal_ref l
                           LEFT JOIN program p ON p.id=l.program_id JOIN member m ON m.id=l.member_id
                           WHERE {where} ORDER BY l.context, m.name, l.line""", args).fetchall()
    out = [f"# Literal '{v}'" + (f" on field {field.upper()}" if field else "") + "\n"]
    if not rows:
        out.append("**not found** as a literal anywhere (VALUE, 88, MOVE, IF/WHEN, DISPLAY). "
                   "It may be built by STRING/arithmetic, read from a table, or spelled differently.\n")
        return "".join(out)
    groups: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        groups[r["context"]].append(r)

    def rows_for(ctx_names: Sequence[str]) -> List[Tuple]:
        res, seen = [], set()
        for c in ctx_names:
            for r in groups.get(c, []):
                where = r["pname"] if r["pid"] else f"{r['member_name']} ({r['kind']})"
                ct = cite(conn, r["pid"], r["line"]) if r["pid"] else f"{r['member_name']}:{r['line']}"
                key = (r["literal"], c, r["field"] or "", ct.split(" (via")[0])
                if key in seen:
                    continue              # same copybook line seen via several programs
                seen.add(key)
                res.append((r["literal"], c, where, r["field"] or "", ct))
        return res

    def message_text(r: sqlite3.Row) -> str:
        """A code kept in a message TABLE: the FILLER text beside it.
            05 FILLER PIC X(4)  VALUE 'E101'.
            05 FILLER PIC X(30) VALUE 'GENDER MUST BE MALE FOR SON'.
        The group's assembled text is indexed as value_group; the piece
        after this one is what the user sees."""
        fld = conn.execute("SELECT id, parent_id FROM field WHERE member_id=? AND line=?",
                           (r["member_id"], r["line"])).fetchone()
        if not fld or not fld["parent_id"]:
            return ""
        sibs = conn.execute("SELECT id, value_lit FROM field WHERE parent_id=? ORDER BY id", (fld["parent_id"],)).fetchall()
        ids = [s["id"] for s in sibs]
        i = ids.index(fld["id"]) if fld["id"] in ids else -1
        nxt = sibs[i + 1]["value_lit"] if 0 <= i < len(sibs) - 1 else None
        if nxt and nxt[:1] in ("'", '"'):
            return f"next piece: {cobol._norm_lit(nxt)}"
        return ""

    hdr = ["literal", "how", "where", "field / column", "cite"]
    out.append("\n### Defined (VALUE / 88-level)\n")
    defined = rows_for(["value", "cond88"])
    texts = {}
    for r in groups.get("value", []):
        t = message_text(r)
        if t:
            texts[(r["literal"], f"{r['member_name']}:{r['line']}")] = t
    if texts:
        out.append(table(hdr + ["message text beside it"],
                         [row + (texts.get((row[0], row[4].split(" (via")[0]), ""),) for row in defined]))
    else:
        out.append(table(hdr, defined))
    out.append("\n### Set (MOVE / STRING)\n")
    out.append(table(hdr, rows_for(["move_to", "string"])))
    out.append("\n### Tested (IF / WHEN)\n")
    out.append(table(hdr, rows_for(["compare", "when"])))
    out.append("\n### In SQL (predicate / SET / INSERT)\n")
    out.append(table(hdr, rows_for(["sql_predicate", "sql_set", "sql_insert"])))
    out.append("\n### Displayed as a literal\n")
    out.append(table(hdr, rows_for(["display"])))

    # Follow the value out of each program that sets it.
    out.append("\n### Where the value goes after it is set (cross-program)\n")
    setters = {(r["pname"], r["pid"], r["field"]) for r in groups.get("move_to", []) if r["pid"] and r["field"]}
    any_flow = False
    for pname, pid, fld in sorted(setters):
        disp = conn.execute("""SELECT stmt, line FROM field_ref WHERE program_id=? AND name=? AND mode='display'""",
                            (pid, fld)).fetchall()
        wr = conn.execute("""SELECT stmt, line FROM field_ref WHERE program_id=? AND name=? AND mode='write' AND stmt<>'MOVE'""",
                          (pid, fld)).fetchall()
        out.append(f"\n**{pname}.{fld}**\n")
        if disp:
            out.append("- displayed in the same program: " + "; ".join(f"{d['stmt']} @{cite(conn, pid, d['line'])}" for d in disp) + "\n")
        for line in flow_from(conn, pid, pname, fld):
            out.append(line + "\n")
            any_flow = True
        if wr:
            out.append("- also written via: " + "; ".join(f"{w['stmt']} @{cite(conn, pid, w['line'])}" for w in wr[:6]) + "\n")
    if not any_flow:
        out.append("_no CALL/LINKAGE/file route found from the setting programs - the value may leave via "
                   "DB2/IMS/MQ, or via a group-level MOVE (not name-traceable)_\n")
    mids = sorted({r["member_id"] for r in rows})
    out.append(unresolved_for(conn, mids, limit=20))
    return "".join(out)


# --------------------------------------------------------------------------
# dataset / copybook impact
# --------------------------------------------------------------------------

def cmd_dataset(conn: sqlite3.Connection, dsn: str) -> str:
    rows = conn.execute("""SELECT * FROM v_dataset_flow WHERE UPPER(dsn) LIKE ? ORDER BY dsn, mode, job_name""",
                        (f"%{dsn.upper()}%",)).fetchall()
    out = [f"# Dataset {dsn.upper()}\n"]
    for a in conn.execute("""SELECT dsn, vsam_type, recordsize_max, key_len, key_off, gdg_limit, relates_to FROM dataset
                             WHERE UPPER(dsn) LIKE ? AND (vsam_type IS NOT NULL OR gdg_limit IS NOT NULL)""",
                          (f"%{dsn.upper()}%",)):
        bits = [a["vsam_type"] or ""]
        if a["recordsize_max"]:
            bits.append(f"RECORDSIZE max {a['recordsize_max']} (the copybook length must agree)")
        if a["key_len"] is not None:
            bits.append(f"KEYS len {a['key_len']} at offset {a['key_off']} (the RECORD KEY field must sit there)")
        if a["gdg_limit"]:
            bits.append(f"GDG LIMIT {a['gdg_limit']}")
        if a["relates_to"]:
            bits.append(f"relates to {a['relates_to']}")
        out.append(f"- IDCAMS DEFINE `{a['dsn']}`: " + "; ".join(b for b in bits if b) + "\n")
    hidden = 0
    if not dsn.startswith("&&"):
        # &&TEMP names repeat across unrelated jobs; showing them here would
        # join jobs that never share a byte. Ask for the && name to see them.
        keep = [r for r in rows if not (r["dsn"] or "").startswith("&&")]
        hidden, rows = len(rows) - len(keep), keep
    if hidden:
        out.append(f"> {hidden} row(s) for job-local `&&` datasets hidden - they never leave their job. "
                   f"Query the `&&NAME` itself, or `job <JOB>`, to see them.\n")
    # A bare PROC's rows carry its DEFAULT symbolics (TEST.CLM.MASTER): shown
    # only when no indexed job expands that PROC. A job step's own
    # //PS.DD override is shown once, on the effective step, not also on the
    # EXEC PROC= step itself.
    expanded = {r[0].upper() for r in conn.execute("SELECT DISTINCT from_proc FROM step WHERE from_proc IS NOT NULL")}
    kept, proc_hidden, ov_hidden = [], 0, 0
    for r in rows:
        if r["job_name"] is None and r["proc_name"] and r["proc_name"].upper() in expanded:
            proc_hidden += 1
            continue
        if r["proc_called"] and r["job_id"] and conn.execute(
                "SELECT 1 FROM step WHERE job_id=? AND parent_step=? LIMIT 1",
                (r["job_id"], r["step_name"])).fetchone():
            ov_hidden += 1
            continue
        kept.append(r)
    rows = kept
    if proc_hidden:
        out.append(f"> {proc_hidden} row(s) from PROC members' default symbolics hidden: the jobs that "
                   f"expand those PROCs are listed with the real names.\n")
    # Every row carries the DD line it comes from: "job X writes DSN Y" is a
    # claim about one JCL/PROC line, and the gate needs that line.
    trows = [(r["dsn"], f"{r['mode']} [{r['mode_source'] or ''}]".replace(" []", ""),
              r["system"] or "?", r["pgm"],
              r["job_name"] or (f"(PROC {r['proc_name']} defaults - no indexed job runs it)"
                                if r["proc_name"] else ""),
              r["step_name"], r["gdg_rel"] or "",
              f"{r['from_proc'] or r['member_name'] or os.path.basename(r['path'] or '')}:{r['dd_line'] or '?'}")
             for r in rows]
    # ONLINE access: the CSD names the dataset (FILE ... DSNAME), the program
    # names the FCT entry (EXEC CICS READ/REWRITE FILE). A VSAM master is
    # updated all day by CICS; a batch-only answer here would be wrong.
    online = conn.execute("""
        SELECT cf.dsname, cf.name AS fct, p.program_id, p.id AS pid, m.system,
               GROUP_CONCAT(DISTINCT o.op) AS ops, MIN(o.line) AS line
        FROM cics_file cf JOIN io_op o ON UPPER(o.target)=UPPER(cf.name) AND o.target_kind='cics'
        JOIN program p ON p.id=o.program_id JOIN member m ON m.id=p.member_id
        WHERE UPPER(cf.dsname) LIKE ? GROUP BY cf.dsname, cf.name, p.program_id, p.id, m.system""",
                          (f"%{dsn.upper()}%",)).fetchall()
    online_rows = []
    for o in online:
        ops = {x.strip().upper() for x in (o["ops"] or "").split(",")}
        writes = bool(ops & {"WRITE", "REWRITE", "DELETE"})
        reads = bool(ops & {"READ", "STARTBR", "READNEXT", "READPREV"})
        mode = "both" if writes and reads else "output" if writes else "input"
        trans = [t[0] for t in conn.execute("SELECT tran_code FROM transaction_def WHERE UPPER(program)=?",
                                            (o["program_id"].upper(),))]
        online_rows.append({"dsn": o["dsname"], "mode": mode, "system": o["system"], "pgm": o["program_id"],
                            "job": f"(online) FCT {o['fct']}" + (f" tran {', '.join(trans[:4])}" if trans else ""),
                            "ops": ", ".join(sorted(ops)), "cite": cite(conn, o["pid"], o["line"])})
        trows.append((o["dsname"], f"{mode} [cics_verb]", o["system"] or "?", o["program_id"],
                      online_rows[-1]["job"], ", ".join(sorted(ops)), "", f"{cite(conn, o['pid'], o['line'])}"))
    tdq = conn.execute("SELECT detail, direction, line, member_id FROM interface_edge WHERE kind='cics_tdq' AND UPPER(detail) LIKE ?",
                       (f"%{dsn.upper()}%",)).fetchall()
    for t in tdq:
        trows.append((t["detail"].split("->")[-1].strip(), f"{'output' if t['direction'] == 'out' else 'input' if t['direction'] == 'in' else 'unknown'} [cics_tdq]",
                      "?", "(CICS)", t["detail"].split("->")[0].strip(), "", "", f"CSD:{t['line']}"))
    out.append(table(["dataset", "direction [source]", "system", "program", "job", "step", "gdg", "member/cite"], trows))
    # delete / alloc / none (IEFBR14 housekeeping) are not writers.
    writers = {r["system"] for r in rows if r["mode"] in ("output", "mod", "both", "create") and r["system"]}
    readers = {r["system"] for r in rows if r["mode"] in ("input", "both") and r["system"]}
    writers |= {o["system"] for o in online_rows if o["mode"] in ("output", "both") and o["system"]}
    readers |= {o["system"] for o in online_rows if o["mode"] in ("input", "both") and o["system"]}
    if writers and readers and writers != readers:
        out.append(f"\n**Crosses departments:** written by {', '.join(sorted(writers))}; read by "
                   f"{', '.join(sorted(readers))}. A layout or value change here is an interface change "
                   f"between systems, not an internal one.\n")
    ext = _interface_rows(conn, dsn=dsn)
    if ext:
        out.append("\n**Crosses the mainframe boundary**: " + "; ".join(
            f"{k} {d}" + (f" to/from {p}" if p else "") + f" ({w})" for (k, d, p, _what, w, _s, _c) in sorted(ext)[:6])
            + " - see `interfaces --dsn`.\n")
    out.append("\n> Direction marked `undetermined` means DISP alone was available; DISP is not direction. "
               "`open_verb` is the program's own OPEN and is authoritative. Rows from expanded PROC steps carry "
               "the calling job's name.\n")
    return "".join(out)


def cmd_copybook(conn: sqlite3.Connection, name: str) -> str:
    out = [f"# Impact of copybook {name.upper()}\n"]
    copies = conn.execute("SELECT m.* FROM member m WHERE UPPER(m.name)=? AND m.kind IN ('copybook','cobol','unknown')",
                          (name.upper(),)).fetchall()
    if not copies:
        return out[0] + "\n**NOT FOUND**\n"
    if len(copies) > 1:
        out.append(f"> **{len(copies)} copies of this member** ({len({c['norm_sha'] for c in copies})} distinct contents). "
                   f"Record lengths per copy:\n")
        for c in copies:
            r = conn.execute("SELECT MAX(offset+length) AS len, COUNT(*) AS n FROM field WHERE member_id=?", (c["id"],)).fetchone()
            out.append(f"  - `{c['path']}`: {r['len']} bytes, {r['n']} fields" + ("  [authoritative]" if c["authoritative"] else "") + "\n")
    progs = conn.execute("""SELECT DISTINCT m.name AS member_name, p.program_id, p.id AS pid, c.replacing, c.line,
                                   c.resolved_member_id, rm.path AS rpath, rm.system AS rsys, rm.norm_sha AS rsha
                            FROM copy_use c JOIN member m ON m.id=c.member_id JOIN program p ON p.member_id=m.id
                            LEFT JOIN member rm ON rm.id=c.resolved_member_id
                            WHERE UPPER(c.copybook)=? ORDER BY rm.path, p.program_id""", (name.upper(),)).fetchall()
    out.append(f"\n### Programs including it ({len(progs)})\n")
    if len({p["resolved_member_id"] for p in progs}) > 1:
        # Two copies of the copybook: say which programs compile against which
        # layout - that IS the version-skew answer.
        out.append("Grouped by the copy each program actually expanded (version skew):\n")
        by_copy: Dict[Optional[int], List[sqlite3.Row]] = defaultdict(list)
        for p in progs:
            by_copy[p["resolved_member_id"]].append(p)
        for rid, ps in by_copy.items():
            if rid:
                ln = conn.execute("SELECT MAX(offset+length) FROM field WHERE member_id=?", (rid,)).fetchone()[0]
                out.append(f"- `{ps[0]['rpath']}` ({ps[0]['rsys'] or 'no system'}, {ln} bytes): "
                           + ", ".join(f"{p['program_id']} @{p['member_name']}:{p['line']}" for p in ps) + "\n")
            else:
                out.append("- copy NOT FOUND: " + ", ".join(f"{p['program_id']} @{p['member_name']}:{p['line']}" for p in ps) + "\n")
    out.append(table(["program", "member", "REPLACING", "cite"],
                     [(p["program_id"], p["member_name"], (p["replacing"] or "")[:40], f"{p['member_name']}:{p['line']}")
                      for p in progs]))
    pnames = [p["program_id"].upper() for p in progs]
    if pnames:
        q = ",".join("?" * len(pnames))
        steps = conn.execute(f"""SELECT DISTINCT j.job_name, s.step_name, s.effective_pgm, s.line, s.from_proc, m.name AS mem
                                 FROM step s LEFT JOIN job j ON j.id=s.job_id LEFT JOIN member m ON m.id=j.member_id
                                 WHERE UPPER(s.effective_pgm) IN ({q}) AND s.job_id IS NOT NULL""", pnames).fetchall()
        out.append(f"\n### Jobs/steps running those programs ({len(steps)})\n")
        out.append(table(["job", "step", "program", "cite"],
                         [(s["job_name"], s["step_name"], s["effective_pgm"], f"{s['from_proc'] or s['mem']}:{s['line']}")
                          for s in steps]))
        outs = conn.execute(f"""SELECT DISTINCT d.dsn_resolved, s.effective_pgm, d.mode, d.line, s.from_proc, m.name AS mem
                                FROM dd d JOIN step s ON s.id=d.step_id LEFT JOIN job j ON j.id=s.job_id
                                LEFT JOIN member m ON m.id=j.member_id
                                WHERE UPPER(s.effective_pgm) IN ({q}) AND d.dsn_resolved IS NOT NULL
                                  AND d.mode IN ('output','mod','both') AND s.job_id IS NOT NULL""",
                            pnames).fetchall()
        out.append(f"\n### Datasets WRITTEN by those programs ({len(outs)}) - downstream consumers are one hop away\n")
        rows = []
        for o in outs:
            cons = conn.execute("""SELECT DISTINCT s.effective_pgm FROM dd d JOIN step s ON s.id=d.step_id
                                   WHERE d.dsn_resolved=? AND d.mode IN ('input','both','unknown') AND UPPER(s.effective_pgm) NOT IN (%s)""" % q,
                                (o["dsn_resolved"], *pnames)).fetchall()
            rows.append((o["dsn_resolved"], o["effective_pgm"], ", ".join(c["effective_pgm"] or "?" for c in cons) or "_none indexed_",
                         f"{o['from_proc'] or o['mem']}:{o['line']}"))
        out.append(table(["dataset", "written by", "then read by", "cite"], rows))
        callers = set()
        for pn in pnames:
            callers.update(c for c, _k in _callers_of(conn, pn))
        if callers:
            out.append(f"\n### Programs that CALL the including programs (LINKAGE may carry this record)\n"
                       + ", ".join(sorted(callers)) + "\n")
        dyn = conn.execute(f"""SELECT COUNT(*) FROM call_edge c JOIN program p ON p.id=c.program_id
                               WHERE UPPER(p.program_id) IN ({q}) AND c.resolution='unresolved'""", pnames).fetchone()[0]
        if dyn:
            out.append(f"\n**{dyn} unresolved dynamic CALL(s) inside these programs** - their targets may also carry the record.\n")
        cards = conn.execute(f"""SELECT COUNT(*) FROM card_field_ref c JOIN step s ON s.id=c.step_id WHERE UPPER(s.effective_pgm) IN ({q})""", pnames).fetchone()[0]
        cards2 = 0
        dsn_list = [o["dsn_resolved"] for o in outs]
        if dsn_list:
            qd = ",".join("?" * len(dsn_list))
            cards2 = conn.execute(f"""SELECT COUNT(DISTINCT c.id) FROM card_field_ref c JOIN step s ON s.id=c.step_id
                                      JOIN dd d ON d.step_id=s.id
                                      WHERE d.dsn_resolved IN ({qd}) AND d.mode IN ('input','both','unknown')""",
                                  dsn_list).fetchone()[0]
        out.append(f"\nSort-card byte references: {cards} on these steps, **{cards2} on steps that consume the "
                   f"datasets written above** (see `job <name>` for positions; any field-length change moves them).\n")
    mids = [c["id"] for c in copies] + [conn.execute("SELECT member_id FROM program WHERE id=?", (p["pid"],)).fetchone()[0] for p in progs]
    out.append(unresolved_for(conn, mids, limit=30))
    return "".join(out)


# --------------------------------------------------------------------------
# hygiene reports
# --------------------------------------------------------------------------

def cmd_dead(conn: sqlite3.Connection) -> str:
    out = ["# Dead-code candidates\n\n> Candidates only. A never-PERFORMed paragraph is still live if the "
           "paragraph above it falls through, and a never-called program is still live if the scheduler, "
           "an online transaction, or an unresolved dynamic CALL runs it. Every caveat below is real.\n"]
    called = set()
    codes = set()
    for r in conn.execute("SELECT kind, target, resolved FROM call_edge"):
        tgt = {r["target"].upper()} if r["target"] else set()
        tgt.update(t.upper() for t in _jl(r["resolved"]))
        if r["kind"] in ("cics_start", "cics_return", "ims_switch"):
            codes.update(tgt)            # transaction codes, routed to programs below
        else:
            called.update(tgt)
    # CALL 'RATEENT' reaches RATECALC through its ENTRY point.
    for r in conn.execute("SELECT p.program_id, a.alias FROM program_alias a JOIN program p ON p.id=a.program_id"):
        if r["alias"].upper() in called:
            called.add(r["program_id"].upper())
    run = {r[0].upper() for r in conn.execute("SELECT DISTINCT effective_pgm FROM step WHERE effective_pgm IS NOT NULL")}
    tx = {r[0].upper() for r in conn.execute("SELECT DISTINCT program FROM transaction_def WHERE program IS NOT NULL")}
    if codes:
        q = ",".join("?" * len(codes))
        tx.update(r[0].upper() for r in conn.execute(
            f"SELECT DISTINCT program FROM transaction_def WHERE program IS NOT NULL AND UPPER(tran_code) IN ({q})",
            sorted(codes)))
    progs = conn.execute("SELECT p.program_id, m.name, m.path FROM program p JOIN member m ON m.id=p.member_id").fetchall()
    dead = [(p["program_id"], p["name"], p["path"]) for p in progs
            if p["program_id"].upper() not in called | run | tx]
    unres = conn.execute("SELECT COUNT(*) FROM call_edge WHERE resolution='unresolved'").fetchone()[0]
    has_sched = conn.execute("SELECT COUNT(*) FROM sched_job").fetchone()[0]
    out.append(f"\n### Programs never called, never run by indexed JCL, never a transaction ({len(dead)})\n")
    out.append(f"Caveats: {unres} unresolved dynamic CALLs; scheduler loaded: {'yes' if has_sched else 'NO'}; "
               f"online definitions loaded: {'yes' if tx else 'NO'}.\n\n")
    out.append(table(["program", "member", "path"], dead[:200]))
    if len(dead) > 200:
        out.append(f"_...{len(dead) - 200} more_\n")
    out.append(_dead_extras(conn))
    return "".join(out)


def cmd_ambiguous(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT * FROM v_ambiguous_member ORDER BY distinct_content DESC, copies DESC").fetchall()
    out = [f"# Duplicate member names ({len(rows)})\n\n> Same name in more than one place. When contents differ, "
           "the index cannot know which is production; declare it in a manifest (`--manifest`) or every answer "
           "about these members is a coin toss.\n\n"]
    out.append(table(["member", "kind", "copies", "distinct contents", "systems", "paths"],
                     [(r["name"], r["kind"], r["copies"], r["distinct_content"], r["systems"] or "?", r["paths"][:160])
                      for r in rows[:300]]))
    out.append("\n> Same name in two DEPARTMENTS is normal and safe once each department's programs resolve "
               "to their own copy (sources.json `system` + copybook order). Same name with different content "
               "inside ONE department is the dangerous case.\n")
    cross = conn.execute("""SELECT name, GROUP_CONCAT(DISTINCT kind) AS kinds FROM member
                            WHERE kind IN ('cobol','copybook','jcl','proc','dbd','psb')
                            GROUP BY name HAVING COUNT(DISTINCT kind) > 1 ORDER BY name LIMIT 200""").fetchall()
    if cross:
        out.append(f"\n### Same name, different kinds ({len(cross)}) - informational\n")
        out.append("A PSB or a job named after its program is a convention, not a duplicate. Cite such a member as "
                   "`[[NAME(kind) line \"token\"]]` so the gate checks the right file.\n")
        out.append(table(["member", "kinds"], [(c["name"], c["kinds"]) for c in cross[:60]]))
    return "".join(out)


def index_header(conn: sqlite3.Connection) -> str:
    """When the index was built and from what - every answer is 'as of'
    this, and a build that never finished is said so."""
    last = conn.execute("SELECT started_at, finished_at, root, members FROM build_run WHERE finished_at IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    hanging = conn.execute("SELECT started_at FROM build_run WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    parts = []
    if last:
        parts.append(f"index built {last['finished_at']} from {os.path.basename(last['root'] or '') or last['root']} "
                     f"({last['members']} members)")
    else:
        parts.append("index never completed a build")
    if hanging and (not last or hanging["started_at"] > (last["finished_at"] or "")):
        parts.append(f"WARNING: a build started {hanging['started_at']} has not finished (running, or crashed) - "
                     f"facts may be partial")
    inc = conn.execute("SELECT COUNT(*) FROM library WHERE complete=0").fetchone()[0]
    if inc:
        parts.append(f"WARNING: {inc} fetched librar{'y is' if inc == 1 else 'ies are'} INCOMPLETE (see coverage)")
    return "; ".join(parts)


def cmd_coverage(conn: sqlite3.Connection) -> str:
    out = ["# Coverage - what the index does and does not know\n", f"\n_{index_header(conn)}_\n"]
    libs = conn.execute("SELECT dataset, fetched_at, expected, present, complete, missing, stale FROM library ORDER BY dataset").fetchall()
    if libs:
        out.append("\n### Fetched libraries (what the host listed vs what arrived)\n")
        out.append(table(["dataset", "fetched", "members", "present", "state", "missing (first few)"],
                         [(l["dataset"], (l["fetched_at"] or "")[:10], l["expected"], l["present"],
                           "complete" if l["complete"] else "**INCOMPLETE**",
                           ", ".join(_jl(l["missing"])[:6]) + (" ..." if len(_jl(l["missing"])) > 6 else "")) for l in libs]))
    out.append("\n### Members\n")
    out.append(table(["kind", "status", "count"], conn.execute(
        "SELECT kind, parse_status, COUNT(*) FROM member GROUP BY 1,2 ORDER BY 1,2").fetchall()))
    out.append("\n### Call resolution\n")
    out.append(table(["kind", "resolution", "count"], conn.execute(
        "SELECT kind, resolution, COUNT(*) FROM call_edge GROUP BY 1,2").fetchall()))
    out.append("\n### Copybooks not found\n")
    out.append(table(["copybook", "uses"], conn.execute(
        "SELECT copybook, COUNT(*) FROM copy_use WHERE resolved_member_id IS NULL AND copybook NOT IN ('SQLCA','SQLDA') "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 40").fetchall()))
    out.append("\n### Unresolved by kind\n")
    out.append(table(["kind", "count"], conn.execute(
        "SELECT kind, COUNT(*) FROM unresolved GROUP BY 1 ORDER BY 2 DESC").fetchall()))
    out.append("\n### DD direction sources\n")
    out.append(table(["source", "count"], conn.execute(
        "SELECT mode_source, COUNT(*) FROM dd WHERE dsn_resolved IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()))
    n_sched = conn.execute("SELECT COUNT(*) FROM sched_job").fetchone()[0]
    n_tx = conn.execute("SELECT COUNT(*) FROM transaction_def").fetchone()[0]
    n_amb = conn.execute("SELECT COUNT(*) FROM v_ambiguous_member WHERE distinct_content>1").fetchone()[0]
    out.append(f"\n### Not loaded\n- scheduler definitions: {n_sched} jobs\n- online transaction definitions: {n_tx}\n"
               f"- members with same name+kind and DIFFERENT content: {n_amb}\n")
    out.append(_coverage_extras(conn))
    out.append("\nAnything in this report that reads 'NO' or a large count is a class of question the index "
               "will answer WRONG, not just incompletely. Fix the input before trusting the output.\n")
    return "".join(out)


def cmd_search(conn: sqlite3.Connection, term: str, kind: Optional[str], limit: int) -> str:
    q = "SELECT member_name, kind, line_no, text FROM src_fts WHERE src_fts MATCH ?"
    args: list = [term]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    q += " LIMIT ?"
    args.append(limit)
    try:
        rows = conn.execute(q, args).fetchall()
    except sqlite3.OperationalError as e:
        return f"search error: {e}\n(quote the term: \"WS-PLCY-STAT-CD\")\n"
    out = [f"# search {term!r} ({len(rows)} hits, limit {limit})\n\n"]
    for r in rows:
        out.append(f"- `{r['member_name']}:{r['line_no']}` [{r['kind']}] {r['text'].strip()[:110]}\n")
    return "".join(out)


def cmd_cite(conn: sqlite3.Connection, member: str, rng: str, program: Optional[str],
             kind: Optional[str] = None) -> str:
    a, _, b = rng.partition("-")
    s, e = int(a), int(b or a)
    if program:
        prog = programs_named(conn, program)
        if not prog:
            return f"program {program} not found\n"
        m1, l1, _d, _v = origin(conn, prog[0]["id"], s)
        m2, l2, _d, _v = origin(conn, prog[0]["id"], e)
        if m1 != m2:
            return f"expanded lines {s}-{e} span two members ({m1} / {m2}); cite each separately\n"
        member, s, e = m1, l1, l2
    # SAMPPGM.cbl / SAMPPGM.psb / SAMPPGM.jcl share a name: `--kind` picks,
    # else the program-ish kind wins and the others are named.
    order = {"cobol": 0, "copybook": 1, "jcl": 2, "proc": 3, "psb": 4, "dbd": 5}
    rows = conn.execute("SELECT path, kind, system FROM member WHERE UPPER(name)=? ORDER BY authoritative DESC",
                        (member.upper(),)).fetchall()
    if kind:
        rows = [r for r in rows if (r["kind"] or "").lower() == kind.lower()]
    if not rows:
        return f"member {member} not found" + (f" with kind {kind}" if kind else "") + "\n"
    rows = sorted(rows, key=lambda r: order.get(r["kind"], 9))
    row = rows[0]
    text, data, enc = reader.load(row["path"])
    recs = reader._split_records(text, data, enc)
    out = [f"{member.upper()}({row['kind']})  {row['path']}\n"]
    others = sorted({r["kind"] for r in rows[1:]} - {row["kind"]})
    if others:
        out.append(f"  (other members named {member.upper()}: {', '.join(others)} - use --kind; "
                   f"cite as [[{member.upper()}({row['kind']}) line \"token\"]])\n")
    for i in range(max(1, s), min(len(recs), e) + 1):
        out.append(f"{i:6d} | {recs[i-1].rstrip()}\n")
    return "".join(out)


# --------------------------------------------------------------------------
# pack - the token-frugal handoff to a model
# --------------------------------------------------------------------------

_PATH_RX = re.compile(r"`?[A-Za-z]:\\[^`\s|]+`?|`?/[A-Za-z0-9_./\-]+/[^`\s|]+`?")


def _strip_paths(text: str) -> str:
    """Full Windows/POSIX paths carry nothing a model needs and leak the
    laptop's layout into whatever is pasted outside: keep the file name."""
    def repl(m: "re.Match[str]") -> str:
        s = m.group(0).strip("`")
        return "`" + os.path.basename(s.rstrip("/\\")) + "`" if m.group(0).startswith("`") else os.path.basename(s)
    return _PATH_RX.sub(repl, text)


def _fit_budget(sections: List[Tuple[str, str, int]], budget: Optional[int]) -> str:
    """Assemble (title, text, priority) sections inside a character budget:
    lowest priority number is kept first; a section that does not fit is
    truncated with a note rather than silently dropped."""
    if not budget:
        return "".join(t for _n, t, _p in sections)
    keep: Dict[int, str] = {}
    used = 0
    for idx, (_name, text, _prio) in sorted(enumerate(sections), key=lambda x: (x[1][2], x[0])):
        room = budget - used
        if room <= 0:
            continue
        if len(text) <= room:
            keep[idx] = text
            used += len(text)
        elif room > 120:
            keep[idx] = text[:room - 60].rstrip() + f"\n_...section truncated to fit --budget {budget}_\n"
            used = budget
    return "".join(keep[i] for i in range(len(sections)) if i in keep)


def _evidence_lines(conn: sqlite3.Connection, pid: int, member_name: str, max_lines: int,
                    want: Optional[set] = None) -> str:
    out = ["\n---\n## Evidence lines (source, for citation)\n\n",
           "Cite as `[[MEMBER line \"token\"]]` or `[[MEMBER:line \"token\"]]`. Lines are from the ORIGINAL member.\n\n"]
    picks: List[Tuple[str, int]] = []
    q = {
        "CALL": "SELECT line FROM call_edge WHERE program_id=? ORDER BY line",
        "SQL": "SELECT start_line FROM sql_stmt WHERE program_id=? ORDER BY start_line",
        "DLI": "SELECT line FROM dli_call WHERE program_id=? ORDER BY line",
        "CICS": "SELECT line FROM cics_cmd WHERE program_id=? ORDER BY line",
        "SELECT": "SELECT line FROM file_decl WHERE program_id=? ORDER BY line",
        "IO": "SELECT line FROM io_op WHERE program_id=? AND target_kind='file' ORDER BY line",
        "PARA": "SELECT start_line FROM paragraph WHERE program_id=? ORDER BY ordinal",
        "SET": "SELECT line FROM literal_ref WHERE program_id=? AND context='move_to' ORDER BY line",
        # IF/WHEN with a literal or an 88: the test conditions a tester needs.
        "COND": "SELECT line FROM literal_ref WHERE program_id=? AND context IN ('compare','when') ORDER BY line",
    }
    for tag, sql in q.items():
        if want and tag not in want:
            continue
        for r in conn.execute(sql, (pid,)):
            picks.append((tag, r[0]))
    seen = set()
    n = 0
    for tag, exp_line in picks:
        m, ln, _d, _v = origin(conn, pid, exp_line)
        if not m or (m, ln) in seen:
            continue
        seen.add((m, ln))
        txt = source_line(conn, m, ln)
        out.append(f"- {tag:<6} `{m}:{ln}`  {txt[:100]}\n")
        n += 1
        if n >= max_lines:
            out.append(f"_...capped at {max_lines} evidence lines; use `cite {member_name} a-b` for more_\n")
            break
    return "".join(out)


def cmd_pack(conn: sqlite3.Connection, name: str, max_lines: int = 120, kind: Optional[str] = None,
             budget: Optional[int] = None, sections: Optional[str] = None, keep_paths: bool = False) -> str:
    """The token-frugal hand-off: a dossier plus the exact source lines
    behind its facts, inside a character budget, for a program, a job (with
    the FULL control cards), a copybook (with its layout), a transaction or a
    field. The header states the approximate token cost."""
    kind = (kind or "").lower() or None
    if kind is None:
        if programs_named(conn, name):
            kind = "program"
        elif conn.execute("SELECT 1 FROM job j JOIN member m ON m.id=j.member_id WHERE UPPER(j.job_name)=? OR UPPER(m.name)=?",
                          (name.upper(), name.upper())).fetchone() or \
                conn.execute("SELECT 1 FROM proc_def WHERE UPPER(proc_name)=?", (name.upper(),)).fetchone():
            kind = "job"
        elif conn.execute("SELECT 1 FROM member WHERE UPPER(name)=? AND kind='copybook'", (name.upper(),)).fetchone():
            kind = "copybook"
        elif conn.execute("SELECT 1 FROM transaction_def WHERE UPPER(tran_code)=?", (name.upper(),)).fetchone():
            kind = "transaction"
        elif _field_defs(conn, name):
            kind = "field"
        else:
            return f"{name}: not a program, job/PROC, copybook, transaction or field in the index\n"
    wanted = {s.strip().upper() for s in sections.split(",")} if sections else None
    parts: List[Tuple[str, str, int]] = []           # (name, text, priority)

    def add(title: str, text: str, prio: int) -> None:
        if not text:
            return
        if wanted and title.upper() not in wanted and title.upper() != "HEADER":
            return
        parts.append((title, text, prio))

    if kind == "program":
        p = programs_named(conn, name)[0]
        doss = cmd_program(conn, name)
        # split the dossier at its section headers so the budget can rank them
        head, *rest = re.split(r"(?m)^(?=### )", doss)
        add("header", head, 0)
        prio = {"RUNS": 1, "CALLS": 1, "CALLED": 1, "FILES": 2, "COPYBOOKS": 2, "DB2": 2, "IMS": 2, "CICS": 2,
                "EXTERNAL": 2, "STRUCTURE": 5, "UNRESOLVED": 1, "DOCUMENTS": 6}
        for sec in rest:
            title = sec.split("\n", 1)[0].lstrip("# ").strip()
            key = title.split()[0].upper() if title else "OTHER"
            add(key, sec, prio.get(key, 4))
        add("evidence", _evidence_lines(conn, p["id"], p["member_name"], max_lines), 3)
    elif kind == "job":
        add("header", cmd_job(conn, name), 0)
        # the cards ARE the behaviour of a batch step: full text, cited
        cards = []
        for r in conn.execute("""SELECT j.job_name, s.step_name, s.from_proc, d.dd_name, d.sysin_text, d.card_member, d.line,
                                        m.name AS mem
                                 FROM dd d JOIN step s ON s.id=d.step_id LEFT JOIN job j ON j.id=s.job_id
                                 LEFT JOIN member m ON m.id=j.member_id LEFT JOIN proc_def pd ON pd.id=s.proc_id
                                 WHERE d.sysin_text IS NOT NULL
                                   AND (((UPPER(j.job_name)=? OR UPPER(m.name)=?) AND s.proc_called IS NULL)
                                        OR UPPER(pd.proc_name)=?)
                                 ORDER BY s.ordinal, d.line""",
                              (name.upper(), name.upper(), name.upper())):
            src = r["card_member"] or r["from_proc"] or r["mem"]
            cards.append(f"\n**{r['step_name']} {r['dd_name']}** (cards from `{src}`"
                         + (f", line {r['line']}" if not r["card_member"] else ", the whole member") + ")\n```\n")
            for i, ln in enumerate(r["sysin_text"].splitlines(), 1):
                cards.append(f"{(i if r['card_member'] else r['line'] + i):5d} | {ln.rstrip()[:100]}\n")
            cards.append("```\n")
        if cards:
            add("cards", "\n---\n## Control cards (full text; cite `MEMBER:line`)\n" + "".join(cards), 1)
    elif kind == "copybook":
        add("header", cmd_copybook(conn, name), 0)
        add("layout", "\n---\n" + cmd_layout(conn, name), 1)
    elif kind == "transaction":
        add("header", cmd_transaction(conn, name), 0)
        for (pg,) in conn.execute("SELECT DISTINCT program FROM transaction_def WHERE UPPER(tran_code)=? AND program IS NOT NULL",
                                  (name.upper(),)):
            if programs_named(conn, pg):
                add("program", "\n---\n" + cmd_program(conn, pg), 2)
    else:
        add("header", cmd_field(conn, name), 0)
    text = _fit_budget(parts, budget)
    if not keep_paths:
        text = _strip_paths(text)
    est = len(text) // 4
    return (f"<!-- pack {kind} {name.upper()}: ~{est} tokens ({len(text)} chars)"
            + (f", budget {budget}" if budget else "") + f"; {index_header(conn)} -->\n") + text


# --------------------------------------------------------------------------
# documents (prose - never facts) and the pictures inside them
# --------------------------------------------------------------------------

def _section_label(n: Optional[int]) -> str:
    if not n:
        return "table"
    return f"image {n - 1000}" if n >= 1000 else f"section {n}"


def _doc_mentions(conn: sqlite3.Connection, term: str, limit: int = 8) -> List[sqlite3.Row]:
    try:
        rows = conn.execute("""SELECT member_name, member_id, line_no, substr(text,1,200) AS snip FROM src_fts
                               WHERE src_fts MATCH ? AND kind='doc' LIMIT 80""", (f'"{term}"',)).fetchall()
    except sqlite3.OperationalError:
        return []
    seen, out = set(), []
    for r in rows:
        key = (r["member_name"], r["line_no"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _docs_section(conn: sqlite3.Connection, term: str) -> str:
    hits = _doc_mentions(conn, term)
    if not hits:
        return ""
    return ("\n### Documents mentioning it (prose: what was INTENDED, not what the code does)\n"
            + table(["document", "where", "excerpt", "cite"],
                    [(h["member_name"], _section_label(h["line_no"]), h["snip"].replace("\n", " ")[:120],
                      f"{h['member_name']}:{h['line_no'] or 0}") for h in hits]))


def cmd_docs(conn: sqlite3.Connection, term: str) -> str:
    try:
        rows = conn.execute("""SELECT f.member_name, f.member_id, f.line_no, substr(f.text,1,260) AS snip
                               FROM src_fts f WHERE src_fts MATCH ? AND kind='doc' LIMIT 400""",
                            (f'"{term}"',)).fetchall()
    except sqlite3.OperationalError as e:
        return f"search error: {e}\n"
    out = [f"# Documents mentioning `{term}`\n"]
    if not rows:
        return out[0] + ("\n_none_ - no indexed document contains it. Documents are indexed from the folders in "
                         "`extra_roots` (UI: Document folders); pictures only after `OCR images`.\n")
    by_doc: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_doc[r["member_name"]].append(r)
    for doc, hits in by_doc.items():
        mid = hits[0]["member_id"]
        m = conn.execute("SELECT path FROM member WHERE id=?", (mid,)).fetchone()
        n_img, n_ocr = conn.execute("SELECT COUNT(*), SUM(ocr_text IS NOT NULL) FROM doc_image WHERE member_id=?",
                                    (mid,)).fetchone()
        out.append(f"\n## {doc}  `{m['path'] if m else ''}`  - {n_img or 0} image(s), {n_ocr or 0} read by OCR\n")
        seen = set()
        trows = []
        for h in hits:
            if h["line_no"] in seen:
                continue
            seen.add(h["line_no"])
            head = conn.execute("SELECT heading FROM doc_section WHERE member_id=? AND ordinal=?",
                                (mid, h["line_no"])).fetchone()
            trows.append((_section_label(h["line_no"]), (head["heading"] if head and head["heading"] else "")[:40],
                          h["snip"].replace("\n", " ")[:160], f"{doc}:{h['line_no'] or 0}"))
        out.append(table(["where", "heading", "excerpt", "cite"], trows[:15]))
    out.append("\n> Full text of a section: `doc DOCNAME --sections n` (or `n-m`); every section about a term: "
               "`doc DOCNAME --grep TERM`; the outline: `doc DOCNAME`.\n"
               "> Cite a document as `[[DOCNAME <section> \"token\"]]`; the gate checks the token against that "
               "section (images are sections 1001+). Where a document and the code disagree, say so - the "
               "code is what runs.\n")
    return "".join(out)


def _doc_members(conn: sqlite3.Connection, name: str) -> List[sqlite3.Row]:
    n = name.upper()
    stem = os.path.splitext(n)[0]
    rows = conn.execute("SELECT id, name, path FROM member WHERE kind='doc' AND (UPPER(name)=? OR UPPER(name)=?) "
                        "ORDER BY path", (n, stem)).fetchall()
    if not rows:
        rows = conn.execute("SELECT id, name, path FROM member WHERE kind='doc' AND UPPER(name) LIKE ? ORDER BY path",
                            (f"%{stem}%",)).fetchall()
    return rows


def _parse_sections(spec: str) -> set:
    """'3' | '3-5' | '2,7,9-12' -> {section numbers}"""
    out: set = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        if a.strip().isdigit() and (not b or b.strip().isdigit()):
            lo, hi = int(a), int(b or a)
            out.update(range(min(lo, hi), max(lo, hi) + 1))
    return out


def cmd_doc(conn: sqlite3.Connection, name: str, sections: Optional[str] = None, grep: Optional[str] = None,
            budget: Optional[int] = None) -> str:
    """A document the model can read a section at a time: the outline
    (section numbers, headings, sizes), or the FULL text of chosen sections
    - by number or by content - each citable as [[DOC n "token"]]. Documents
    are prose: what was intended, never a fact about what runs."""
    rows = _doc_members(conn, name)
    if not rows:
        return (f"# Document {name}\n\n**NOT FOUND** - no indexed document is called that. `docs TERM` finds documents "
                "by content; documents are indexed from the folders in `extra_roots` (UI: Document folders).\n")
    m = rows[0]
    out = [f"# Document {m['name']}  ({os.path.basename(m['path'])})\n"]
    if len(rows) > 1:
        out.append(f"- {len(rows)} documents share this name; showing the first. Others: "
                   + ", ".join(os.path.basename(r["path"]) for r in rows[1:4]) + "\n")
    secs = conn.execute("SELECT ordinal, heading, text FROM doc_section WHERE member_id=? ORDER BY ordinal",
                        (m["id"],)).fetchall()
    n_img, n_ocr = conn.execute("SELECT COUNT(*), SUM(ocr_text IS NOT NULL) FROM doc_image WHERE member_id=?",
                                (m["id"],)).fetchone()
    if not sections and not grep:
        total = sum(len(s["text"] or "") for s in secs)
        out.append(f"- {len(secs)} sections, {total} characters (~{total // 4} tokens); {n_img or 0} image(s), "
                   f"{n_ocr or 0} read by OCR (their text is sections 1001+ once `OCR images` has run)\n")
        out.append(f"- read one: `doc {m['name']} --sections 3` (or `3-5`, `2,7,9-12`); by content: "
                   f"`doc {m['name']} --grep \"waiver\"`; cite as `[[{m['name']} 3 \"token\"]]`\n\n")
        trows = [(s["ordinal"], (s["heading"] or "")[:50], len(s["text"] or ""),
                  (s["text"] or "").strip().split("\n", 1)[0][:70]) for s in secs]
        out.append(table(["section", "heading", "chars", "starts with"], trows[:400]))
        if len(secs) > 400:
            out.append(f"_... {len(secs) - 400} more sections_\n")
        return "".join(out)
    if sections:
        want = _parse_sections(sections)
        chosen = [s for s in secs if s["ordinal"] in want]
        what = f"section(s) {sections}"
    else:
        g = (grep or "").upper()
        chosen = [s for s in secs if g in ((s["heading"] or "") + "\n" + (s["text"] or "")).upper()]
        what = f"section(s) containing `{grep}`"
    if not chosen:
        out.append(f"\n_no {what}_ - `doc {m['name']}` lists what there is\n")
        return "".join(out)
    out.append(f"- {len(chosen)} {what}; cite as `[[{m['name']} <section> \"token\"]]` with the token copied "
               "from the text. Prose describes intent; where it disagrees with the code, say so.\n")
    used = sum(len(x) for x in out)
    shown = 0
    for s in chosen:
        block = (f"\n### {m['name']} section {s['ordinal']}" + (f" - {s['heading']}" if s["heading"] else "")
                 + "\n```\n" + (s["text"] or "").rstrip() + "\n```\n")
        if budget and shown and used + len(block) > budget:
            rest = chosen[shown:]
            out.append(f"\n_budget {budget}: {len(rest)} more section(s) not shown - "
                       + ", ".join(str(x["ordinal"]) for x in rest[:30]) + (" ..." if len(rest) > 30 else "") + "_\n")
            break
        out.append(block)
        used += len(block)
        shown += 1
    return "".join(out)


def cmd_images(conn: sqlite3.Connection, name: Optional[str] = None) -> str:
    q = """SELECT m.name, m.path, COUNT(i.id) AS n, SUM(i.extracted_path IS NOT NULL) AS extracted,
                  SUM(i.ocr_text IS NOT NULL) AS read, SUM(i.ocr_text IS NOT NULL AND i.ocr_text<>'') AS with_text
           FROM doc_image i JOIN member m ON m.id=i.member_id"""
    args: tuple = ()
    if name:
        q += " WHERE UPPER(m.name)=?"
        args = (name.upper(),)
    q += " GROUP BY m.id ORDER BY n DESC"
    rows = conn.execute(q, args).fetchall()
    out = [f"# Pictures inside documents{' - ' + name.upper() if name else ''}\n"]
    if not rows:
        return out[0] + "\n_no images recorded_ (documents are indexed from `extra_roots`; legacy .doc/.xls must be saved as .docx/.xlsx first)\n"
    out.append(table(["document", "images", "extracted", "OCR read", "with text", "path"],
                     [(r["name"], r["n"], r["extracted"] or 0, r["read"] or 0, r["with_text"] or 0,
                       os.path.basename(r["path"])) for r in rows[:200]]))
    total = sum(r["n"] for r in rows)
    unread = sum(r["n"] - (r["read"] or 0) for r in rows)
    out.append(f"\n{total} image(s) in {len(rows)} document(s); {unread} not yet read. "
               f"`python -m atlas.ocr --db atlas.db --out out/images` reads them with the Windows OCR engine "
               f"(no tokens). Pictures that OCR cannot read (diagrams, handwriting) are the ones worth "
               f"spending a vision-model call on - they are listed with their extracted path.\n")
    if name:
        rows2 = conn.execute("""SELECT i.name, i.extracted_path, i.ocr_text FROM doc_image i JOIN member m ON m.id=i.member_id
                                WHERE UPPER(m.name)=? ORDER BY i.id""", (name.upper(),)).fetchall()
        out.append(table(["image", "extracted to", "OCR text (first 80 chars)"],
                         [(r["name"], r["extracted_path"] or "", (r["ocr_text"] or ("(not read)" if r["ocr_text"] is None else "(nothing recognised)"))[:80].replace("\n", " "))
                          for r in rows2]))
    return "".join(out)


# --------------------------------------------------------------------------
# DB2 columns
# --------------------------------------------------------------------------

def cmd_column(conn: sqlite3.Connection, name: str) -> str:
    """Where a DB2 column is written from, read into, and filtered by - and,
    through the host variable, where that value came from or went next."""
    n = name.upper()
    tbl, _, col = n.rpartition(".")
    # PRD.POLICY_TBL and POLICY_TBL are the same table written two ways: match
    # on the unqualified name unless the question itself carries a qualifier.
    base = tbl.rpartition(".")[2] if tbl else ""
    if tbl and "." in tbl:
        q, args = "UPPER(c.col)=? AND UPPER(c.tbl)=?", (col, tbl)
    elif tbl:
        q, args = "UPPER(c.col)=? AND (UPPER(c.tbl)=? OR UPPER(c.tbl) LIKE ?)", (col, base, f"%.{base}")
    else:
        q, args = "UPPER(c.col)=?", (col,)
    rows = conn.execute(f"""SELECT c.*, p.program_id AS pname FROM sql_col_ref c JOIN program p ON p.id=c.program_id
                            WHERE {q} ORDER BY c.mode, p.program_id, c.line""", args).fetchall()
    out = [f"# DB2 column {n}\n"]
    decl = conn.execute("""SELECT o.qualifier, o.name, o.source, dc.type, dc.ordinal FROM db2_column dc
                           JOIN db2_object o ON o.id=dc.object_id WHERE UPPER(dc.name)=?""" +
                        (" AND UPPER(o.name)=?" if base else ""), (col, base) if base else (col,)).fetchall()
    for d in decl:
        out.append(f"- declared: `{(d['qualifier'] + '.') if d['qualifier'] else ''}{d['name']}.{col}` "
                   f"{d['type'] or ''} (column #{d['ordinal']}, from {d['source']})\n")
    if not rows:
        return "".join(out) + ("\n**No static SQL references** to this column in indexed programs - dynamic SQL, "
                               "a view, or a different column name. Check `search \"" + col + "\"`.\n")
    for mode, title in (("write", "Written (INSERT / UPDATE from a host variable)"),
                        ("read", "Read (SELECT INTO / FETCH INTO a host variable)"),
                        ("predicate", "Used in predicates (WHERE)")):
        rs = [r for r in rows if r["mode"] == mode]
        if not rs:
            continue
        out.append(f"\n### {title}\n")
        trows = []
        for r in rs:
            hv, pid = r["host_var"], r["program_id"]
            extra = ""
            if hv and mode == "write":
                src = conn.execute("""SELECT stmt, line FROM field_ref WHERE program_id=? AND name=? AND mode='write'
                                      AND stmt<>'EXEC-SQL' ORDER BY line""", (pid, hv)).fetchall()
                lits = conn.execute("""SELECT literal, line FROM literal_ref WHERE program_id=? AND field=?
                                       AND context='move_to' ORDER BY line""", (pid, hv)).fetchall()
                extra = "; ".join([f"set by {x['stmt']} @{cite(conn, pid, x['line'])}" for x in src[:3]]
                                  + [f"'{l['literal']}' @{cite(conn, pid, l['line'])}" for l in lits[:3]]) \
                    or "host var never set by name here (group MOVE / CALL / file record?)"
            elif hv and mode == "read":
                dst = conn.execute("""SELECT mode, stmt, line FROM field_ref WHERE program_id=? AND name=?
                                      AND mode IN ('display','write','test') AND stmt<>'EXEC-SQL' ORDER BY line""",
                                   (pid, hv)).fetchall()
                extra = "; ".join(f"{x['mode']} {x['stmt']} @{cite(conn, pid, x['line'])}" for x in dst[:4]) \
                    or "value not used by name afterwards in this program"
            trows.append((r["tbl"], r["col"], r["pname"], r["stmt"], hv or "", cite(conn, pid, r["line"]), extra))
        out.append(table(["table", "column", "program", "stmt", "host variable", "cite",
                          "value from / value to"], trows))
    out.append("\n> Table `?` = the column was unqualified in a multi-table FROM. Views, dynamic SQL and "
               "stored procedures are not resolved. For the field side of any host variable, run `field <name>`.\n")
    return "".join(out)


# --------------------------------------------------------------------------
# transactions (CICS CSD / IMS stage-1)
# --------------------------------------------------------------------------

def cmd_transaction(conn: sqlite3.Connection, code: str) -> str:
    n = code.upper()
    rows = conn.execute("""SELECT t.*, m.name AS mem FROM transaction_def t LEFT JOIN member m ON m.id=t.member_id
                           WHERE UPPER(t.tran_code)=? OR UPPER(t.program)=? OR UPPER(t.psb)=?
                           ORDER BY t.system, t.tran_code""", (n, n, n)).fetchall()
    out = [f"# Transaction / program {n}\n"]
    if not rows:
        loaded = conn.execute("SELECT COUNT(*) FROM transaction_def").fetchone()[0]
        out.append("**NOT FOUND** in any indexed CSD or IMS stage-1 member. ")
        out.append("Load the DFHCSDUP extract and the stage-1 source; until then online routing is unknown.\n"
                   if not loaded else f"({loaded} transactions are indexed - this name is not one of them, "
                   f"nor a program/PSB they route to.)\n")
        return "".join(out)
    out.append(table(["code", "system", "program", "PSB", "group", "detail", "cite"],
                     [(r["tran_code"], r["system"], r["program"] or "(none)", r["psb"] or "",
                       r["group_name"] or "", (r["detail"] or "")[:120], f"{r['mem']}:{r['line']}") for r in rows]))
    for p in sorted({r["program"] for r in rows if r["program"]}):
        if programs_named(conn, p):
            out.append(f"- `{p}` is indexed - `program {p}` for its dossier\n")
        else:
            out.append(f"- **{p} is not in the index** (no source, or member / PROGRAM-ID / load name differ)\n")
    # Routing written in code: START/RETURN TRANSID, IMS CHNG message switches.
    codes = sorted({r["tran_code"].upper() for r in rows})
    q = ",".join("?" * len(codes))
    chain = conn.execute(f"""SELECT p.program_id, p.id AS pid, c.kind, c.target, c.resolved, c.line FROM call_edge c
                             JOIN program p ON p.id=c.program_id
                             WHERE c.kind IN ('cics_start','cics_return','ims_switch') AND
                                   (UPPER(c.target) IN ({q}) OR {' OR '.join('c.resolved LIKE ?' for _ in codes)})
                             ORDER BY p.program_id, c.line""", (*codes, *[f'%"{c}"%' for c in codes])).fetchall()
    if chain:
        out.append("\n### Reached from code (not from the CSD / stage-1)\n")
        out.append(table(["from program", "how", "cite"],
                         [(c["program_id"], {"cics_start": "START TRANSID", "cics_return": "RETURN TRANSID (next in the conversation)",
                                             "ims_switch": "IMS message switch (CHNG + ISRT)"}[c["kind"]],
                           cite(conn, c["pid"], c["line"])) for c in chain]))
    # Which transactions the routed programs themselves start / return to.
    progs = sorted({r["program"].upper() for r in rows if r["program"]})
    if progs:
        qp = ",".join("?" * len(progs))
        nxt = conn.execute(f"""SELECT p.program_id, c.kind, COALESCE(c.target, c.resolved) AS t FROM call_edge c
                               JOIN program p ON p.id=c.program_id WHERE UPPER(p.program_id) IN ({qp})
                               AND c.kind IN ('cics_start','cics_return','ims_switch')""", progs).fetchall()
        if nxt:
            out.append("- next in the chain: " + "; ".join(
                f"{x['program_id']} {x['kind'].replace('cics_', '').replace('ims_switch', 'switches to')} "
                f"{', '.join(_jl(x['t'])) if (x['t'] or '').startswith('[') else x['t']}" for x in nxt) + "\n")
    out.append("\n> IMS: the load module is assumed to be named as the PSB unless APPLCTN used GPSB=. "
               "CICS: a REMOTESYSTEM transaction runs in another region.\n")
    return "".join(out)


# --------------------------------------------------------------------------
# screens (BMS / MFS)
# --------------------------------------------------------------------------

def _screen_hits(conn: sqlite3.Connection, n: str) -> List[sqlite3.Row]:
    """Screen fields called n, or whose BMS symbolic name (nI / nO / nL / nF / nA) is n."""
    base = n[:-1] if len(n) > 2 and n[-1] in "IOLAF" else None
    names = [n] + ([base] if base else [])
    q = ",".join("?" * len(names))
    return conn.execute(f"""
        SELECT sf.*, s.kind, s.name AS sname, s.mode, s.parent, m.name AS mem
        FROM screen_field sf JOIN screen s ON s.id=sf.screen_id JOIN member m ON m.id=s.member_id
        WHERE UPPER(sf.name) IN ({q}) ORDER BY s.kind, s.name""", names).fetchall()


def _screen_refs(conn: sqlite3.Connection, field_name: str, kind: str) -> List[sqlite3.Row]:
    """Program references to a screen field: BMS through the generated
    symbolic names (GENDERI / GENDERO ...), MFS through the MFLD name."""
    names = screens.bms_symbolic_names(field_name) if kind == "bms_map" else [field_name.upper()]
    q = ",".join("?" * len(names))
    return conn.execute(f"""
        SELECT p.program_id, p.id AS pid, r.name, r.mode, r.stmt, r.line FROM field_ref r
        JOIN program p ON p.id=r.program_id WHERE UPPER(r.name) IN ({q})
        ORDER BY p.program_id, r.line""", names).fetchall()


def _screen_section(conn: sqlite3.Connection, n: str) -> str:
    hits = _screen_hits(conn, n)
    if not hits:
        return ""
    out = ["\n### Screen fields (BMS / MFS)\n"]
    out.append(table(["screen", "kind", "mode", "field", "row,col", "len", "offset/seg", "attrb",
                      "default", "PICIN/OUT", "cite"],
                     [(h["sname"], h["kind"], h["mode"] or "", h["name"],
                       f"{h['row']},{h['col']}" if h["row"] else "",
                       h["length"], f"{h['offset']}/{h['seg']}" if h["offset"] is not None else "",
                       h["attrb"] or "", h["initial"] or "",
                       "/".join(x for x in (h["picin"], h["picout"]) if x), f"{h['mem']}:{h['line']}")
                      for h in hits]))
    rows, seen = [], set()
    for h in hits:
        for r in _screen_refs(conn, h["name"], h["kind"]):
            key = (r["program_id"], r["name"], r["line"])
            if key in seen:
                continue
            seen.add(key)
            rows.append((r["program_id"], h["name"], r["name"], r["mode"], r["stmt"],
                         cite(conn, r["pid"], r["line"])))
    if rows:
        out.append("\n**Programs referencing these screen fields** (this is where online validation lives)\n")
        out.append(table(["program", "screen field", "as", "mode", "stmt", "cite"], rows))
    out.append("> BMS programs see a field as `<name>I` (input) / `<name>O` (output). MFS programs address "
               "the message by BYTE OFFSET in their I/O copybook: match the offset column against `field` "
               "output for that copybook.\n")
    return "".join(out)


def cmd_screen(conn: sqlite3.Connection, name: str) -> str:
    n = name.upper()
    scr = conn.execute("""SELECT s.*, m.name AS mem, m.path FROM screen s JOIN member m ON m.id=s.member_id
                          WHERE UPPER(s.name)=? OR UPPER(s.parent)=? ORDER BY s.kind, s.name""", (n, n)).fetchall()
    if not scr:
        return f"# Screen {n}\n\n**NOT FOUND** - no BMS map/mapset or MFS FMT/MSG with this name is indexed.\n"
    out = [f"# Screen {n}\n"]
    mids = set()
    for s in scr:
        mids.add(s["member_id"])
        head = f"\n## {s['kind']} {s['name']}"
        if s["parent"]:
            head += f" (in {s['parent']})"
        head += f"  mode {s['mode'] or '?'}"
        if s["next_msg"]:
            head += f"  next {s['next_msg']}"
        out.append(head + f"  `{s['mem']}:{s['line']}`\n")
        flds = conn.execute("SELECT * FROM screen_field WHERE screen_id=? ORDER BY ordinal", (s["id"],)).fetchall()
        out.append(table(["#", "field", "row,col", "len", "offset/seg", "attrb", "default", "constant",
                          "PICIN/OUT", "line"],
                         [(f["ordinal"], f["name"] or "", f"{f['row']},{f['col']}" if f["row"] else "",
                           f["length"], f"{f['offset']}/{f['seg']}" if f["offset"] is not None else "",
                           f["attrb"] or "", f["initial"] or "", f["literal"] or "",
                           "/".join(x for x in (f["picin"], f["picout"]) if x), f["line"]) for f in flds]))
        # EXEC CICS SEND/RECEIVE MAP('X') MAPSET('Y'): the program<->map edge
        # as a fact, before any field-name coincidence.
        if s["kind"] == "bms_map":
            keys = [s["name"].upper()] + ([f"{s['parent'].upper()}.{s['name'].upper()}"] if s["parent"] else [])
            q = ",".join("?" * len(keys))
            cm = conn.execute(f"""SELECT p.program_id, p.id AS pid, c.verb, c.direction, c.line FROM cics_cmd c
                                  JOIN program p ON p.id=c.program_id
                                  WHERE c.resource_kind='map' AND (UPPER(c.resource) IN ({q}) OR UPPER(c.resource) LIKE ?)
                                  ORDER BY p.program_id, c.line""", (*keys, f"%.{s['name'].upper()}")).fetchall()
            if cm:
                out.append("\n**Programs that SEND / RECEIVE this map (EXEC CICS facts)**\n")
                out.append(table(["program", "verb", "dir", "cite"],
                                 [(c["program_id"], c["verb"], c["direction"] or "", cite(conn, c["pid"], c["line"])) for c in cm]))
        rows = []
        for fld in flds:
            if not fld["name"]:
                continue
            for r in _screen_refs(conn, fld["name"], s["kind"]):
                rows.append((r["program_id"], fld["name"], r["name"], r["mode"], r["stmt"],
                             cite(conn, r["pid"], r["line"])))
        if rows:
            out.append("\n**Programs referencing its fields**\n")
            out.append(table(["program", "screen field", "as", "mode", "stmt", "cite"], rows))
        elif s["kind"] != "mfs_fmt":
            out.append("_No program references found by name. MFS programs address the message by copybook "
                       "offset; BMS programs use the generated symbolic copybook, which must be in the index._\n")
    out.append(unresolved_for(conn, sorted(mids)))
    return "".join(out)


# --------------------------------------------------------------------------
# value-domain changes: values / pair / messages
# --------------------------------------------------------------------------
#
# "Add gender N; son and daughter both become relationship 4" is a change to
# the VALUE DOMAIN of two fields. The work is not finding the fields - it is
# finding every place a specific value is assumed: 88-levels, IF/WHEN tests,
# SQL predicates, sort INCLUDE cards, derivation rules that tie one field's
# value to another's, and the messages that name the old rule. These three
# queries produce those inventories deterministically.

def _norm_val(v: str) -> str:
    s = v.strip()
    if re.fullmatch(r"[+-]?\d+", s):
        return str(int(s))
    return s


def _cond88_map(conn: sqlite3.Connection, name: str) -> Dict[str, List[Tuple[str, str]]]:
    """value -> [(88-name, member)] for every 88 under any field called `name`."""
    out: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for r in conn.execute("""SELECT c.name, c.values_lit, m.name AS mem FROM cond88 c JOIN field f ON f.id=c.field_id
                             JOIN member m ON m.id=f.member_id WHERE UPPER(f.name)=?""", (name.upper(),)):
        raw = _jl(r["values_lit"])
        vals = [cobol._norm_lit(v) for v in raw if v.upper() not in ("THRU", "THROUGH")]
        if any(x.upper() in ("THRU", "THROUGH") for x in raw) and len(vals) >= 2:
            out[f"{_norm_val(vals[0])} THRU {_norm_val(vals[1])}"].append((r["name"], r["mem"]))
        else:
            for v in vals:
                out[_norm_val(v)].append((r["name"], r["mem"]))
    return out


def _column_aliases(name: str) -> List[str]:
    """COBOL field -> plausible DB2 column names (DCLGEN style, prefixes dropped)."""
    n = name.upper().replace("-", "_")
    out = [n]
    for pre in ("WS_", "LK_", "WK_", "W_", "IN_", "OUT_"):
        if n.startswith(pre):
            out.append(n[len(pre):])
    return out


def cmd_values(conn: sqlite3.Connection, name: str) -> str:
    n = name.upper()
    cols = _column_aliases(n)
    defs = _field_defs(conn, n)
    out = [f"# Observed value domain of {n}\n"]
    if not defs:
        out.append("> Not defined in any indexed member; the rows below come from literal usage only.\n")
    documented = _cond88_map(conn, n)

    qc = ",".join("?" * len(cols))
    rows = conn.execute(f"""
        SELECT l.literal, l.context, l.line, l.member_id, p.program_id AS pname, p.id AS pid, m.name AS mem
        FROM literal_ref l LEFT JOIN program p ON p.id=l.program_id JOIN member m ON m.id=l.member_id
        WHERE (UPPER(l.field)=? AND l.context IN ('move_to','compare','when','string','value','screen_initial'))
           OR (UPPER(l.field) IN ({qc}) AND l.context LIKE 'sql_%')""", (n, *cols)).fetchall()
    uses: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        v = _norm_val(r["literal"])
        ct = cite(conn, r["pid"], r["line"]) if r["pid"] else f"{r['mem']}:{r['line']}"
        uses[v][r["context"]].append(f"{r['pname'] or r['mem']}@{ct}")

    # Tests through 88-level names never mention the literal; join them in.
    for v, lst in documented.items():
        for (c88, _mem) in lst:
            for r in conn.execute("""SELECT p.program_id, p.id AS pid, r.mode, r.line FROM field_ref r
                                     JOIN program p ON p.id=r.program_id WHERE UPPER(r.name)=?""", (c88.upper(),)):
                # Say HOW it was reached: a test through an 88 name never mentions
                # the value, which is exactly why a grep for 'M' misses it.
                uses[v][f"via 88 {c88} ({r['mode']})"].append(
                    f"via 88 {c88} {r['program_id']}@{cite(conn, r['pid'], r['line'])}")

    # Sort/INCLUDE cards comparing these bytes to constants.
    for d in defs:
        if d["kind"] != "copybook":
            continue
        lo, hi = d["offset"] + 1, d["offset"] + d["length"]
        for c in conn.execute("""SELECT c.raw, c.card_kind, j.job_name, s.step_name FROM card_field_ref c
                                 JOIN step s ON s.id=c.step_id LEFT JOIN job j ON j.id=s.job_id
                                 WHERE c.pos<=? AND c.pos+c.length-1>=? AND c.card_kind IN ('INCLUDE','OMIT')""", (hi, lo)):
            for m in re.finditer(r"C'([^']*)'|(?<=,)(\d+)(?=[,)])", c["raw"]):
                val = m.group(1) if m.group(1) is not None else m.group(2)
                uses[_norm_val(val)]["sort_card"].append(f"{c['job_name']}.{c['step_name']} {c['card_kind']}")

    all_vals = sorted(set(documented) | set(uses),
                      key=lambda v: (not v.split(" ")[0].lstrip("+-").isdigit(), v))
    trows = []
    for v in all_vals:
        u = uses.get(v, {})

        def cnt(*prefixes):
            return sum(len(l) for k, l in u.items() if k.startswith(prefixes))

        # One example per KIND of use per PROGRAM, so a test through an 88 name
        # or a sort card stays visible, and every program touching the value
        # gets at least one citation even when one program produces dozens.
        examples, seen_kp = [], set()
        for k in sorted(u):
            for w in u[k]:
                prog = w.split("@")[0]
                if (k, prog) in seen_kp:
                    continue
                seen_kp.add((k, prog))
                examples.append(w)
        total = sum(len(l) for l in u.values())
        shown = examples[:8]
        where = "; ".join(shown) + (f" (+{total - len(shown)} more)" if total > len(shown) else "")
        trows.append((v, ", ".join(sorted({c for c, _m in documented.get(v, [])})) or "(none)",
                      cnt("move_to", "string", "sql_set", "sql_insert"),
                      cnt("compare", "when", "via 88"), cnt("sql_predicate"), cnt("sort_card"), where))
    out.append(table(["value", "documented by 88", "set", "tested", "SQL", "sort cards", "where (first few)"], trows))

    undocumented = [v for v in all_vals if v not in documented and uses.get(v)
                    and any(not k.startswith("value") for k in uses[v])]
    unused = [v for v in all_vals if v in documented and not uses.get(v)]
    if undocumented:
        out.append(f"\n**USED BUT NOT DOCUMENTED BY ANY 88-LEVEL:** {', '.join(undocumented)} - undocumented "
                   f"codes are where a value-domain change breaks silently.\n")
    if unused:
        out.append(f"\n**Documented but never set, tested or queried in indexed code:** {', '.join(unused)} "
                   f"(may be reached via a group MOVE, a table, a screen, or code outside the index).\n")
    if any(k.startswith("sql") for u in uses.values() for k in u):
        out.append(f"\nSQL rows are for column name(s) {', '.join(f'`{c}`' for c in cols)} (derived from the field "
                   f"name); if the column is called something else, run `values <COLUMN>` as well.\n")
    out.append(_screen_section(conn, n))
    out.append("\n> This is the domain the CODE knows about. Values that arrive in files, DB2 rows, IMS segments or "
               "screens are data, not literals - compare against the data's actual distinct values before "
               "calling the change complete.\n")
    out.append(unresolved_for(conn, sorted({r["member_id"] for r in rows}), limit=15))
    return "".join(out)


def cmd_pair(conn: sqlite3.Connection, a: str, b: str, window: int = 8) -> str:
    """Statements where two fields (or their 88-levels) are used together -
    the validation and derivation rules that bind one value to the other."""
    a, b = a.upper(), b.upper()

    def names_for(n: str) -> List[str]:
        names = {n}
        for r in conn.execute("""SELECT c.name FROM cond88 c JOIN field f ON f.id=c.field_id
                                 WHERE UPPER(f.name)=?""", (n,)):
            names.add(r["name"].upper())
        # A BMS screen field is referenced by programs as nI / nO (symbolic map).
        if conn.execute("""SELECT 1 FROM screen_field sf JOIN screen s ON s.id=sf.screen_id
                           WHERE UPPER(sf.name)=? AND s.kind='bms_map' LIMIT 1""", (n,)).fetchone():
            names.update(screens.bms_symbolic_names(n)[:2])
        return sorted(names)

    def refs(names: List[str]):
        q = ",".join("?" * len(names))
        return conn.execute(f"SELECT program_id, name, mode, stmt, line FROM field_ref "
                            f"WHERE UPPER(name) IN ({q})", names).fetchall()

    na, nb = names_for(a), names_for(b)
    ra, rb = refs(na), refs(nb)
    by_prog: Dict[int, list] = defaultdict(list)
    for r in rb:
        by_prog[r["program_id"]].append(r)
    hits, seen = [], set()
    for x in ra:
        for y in by_prog.get(x["program_id"], []):
            if abs(x["line"] - y["line"]) > window:
                continue
            key = (x["program_id"], x["line"], y["line"])
            if key in seen:
                continue
            seen.add(key)
            lo = min(x["line"], y["line"])
            para = conn.execute("SELECT name FROM paragraph WHERE program_id=? AND ? BETWEEN start_line AND end_line",
                                (x["program_id"], lo)).fetchone()
            pname = conn.execute("SELECT program_id FROM program WHERE id=?", (x["program_id"],)).fetchone()[0]
            hits.append((pname, para["name"] if para else "", f"{x['name']} {x['mode']}",
                         f"{y['name']} {y['mode']}", cite(conn, x["program_id"], x["line"]),
                         cite(conn, x["program_id"], y["line"])))
    hits.sort()
    out = [f"# Statements relating {a} and {b}\n",
           f"Names searched: {', '.join(na)}  x  {', '.join(nb)}; same program, within {window} lines.\n\n",
           table(["program", "paragraph", a, b, f"cite {a}", f"cite {b}"], hits)]
    progs = {h[0] for h in hits}
    out.append(f"\n{len(hits)} co-reference(s) in {len(progs)} program(s). Each is a cross-field rule "
               f"(validation or derivation) to re-read; a rule such as 'SON must be MALE' is exactly what a "
               f"new value invalidates.\n")
    out.append("> Not covered: rules expressed through a group-level MOVE, a lookup table, a sort card, or a "
               "screen map.\n")
    return "".join(out)


def cmd_messages(conn: sqlite3.Connection, pattern: str) -> str:
    pat = pattern if "%" in pattern else f"%{pattern}%"
    rows = conn.execute("""
        SELECT l.literal, l.context, l.field, l.line, p.program_id AS pname, p.id AS pid, m.name AS mem
        FROM literal_ref l LEFT JOIN program p ON p.id=l.program_id JOIN member m ON m.id=l.member_id
        WHERE UPPER(l.literal) LIKE UPPER(?)
          AND l.context IN ('display','move_to','value','string','value_group','string_group',
                            'display_group','screen_initial','screen_literal')
        ORDER BY l.literal""", (pat,)).fetchall()
    groups: Dict[str, List[str]] = defaultdict(list)
    for r in rows:
        ct = cite(conn, r["pid"], r["line"]) if r["pid"] else f"{r['mem']}:{r['line']}"
        tgt = f" -> {r['field']}" if r["field"] else ""
        groups[r["literal"]].append(f"{r['context']}{tgt} @{r['pname'] or r['mem']} {ct}")
    out = [f"# Message / text literals matching `{pattern}` ({len(groups)})\n",
           table(["text", "where"], [(t, "; ".join(w[:5])) for t, w in groups.items()]),
           "\n`string_group` / `display_group` rows are messages assembled at run time: literals verbatim, "
           "fields as `<NAME>`. `value_group` rows are texts assembled from consecutive FILLER VALUEs.\n"]

    # Messages assembled by MOVEs into sibling fields of one group: show the whole set.
    assembled: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for r in rows:
        if r["context"] != "move_to" or not r["field"] or not r["pid"]:
            continue
        par = conn.execute("""SELECT f2.name FROM field f JOIN field f2 ON f2.id=f.parent_id
                              WHERE UPPER(f.name)=? LIMIT 1""", (r["field"].upper(),)).fetchone()
        if not par:
            continue
        sibs = conn.execute("""SELECT l.literal, l.field FROM literal_ref l
                               JOIN field f ON UPPER(f.name)=UPPER(l.field) JOIN field f2 ON f2.id=f.parent_id
                               WHERE l.program_id=? AND l.context='move_to' AND UPPER(f2.name)=?
                               ORDER BY l.line""", (r["pid"], par["name"].upper())).fetchall()
        if len(sibs) > 1:
            assembled[(r["pname"], par["name"])] = [(s["literal"], s["field"]) for s in sibs]
    if assembled:
        out.append("\n### Messages assembled by MOVEs into one group\n")
        out.append(table(["program", "group", "pieces in source order"],
                         [(p, g, " + ".join(f"'{l}'->{fld}" for l, fld in v)) for (p, g), v in assembled.items()]))
    docs = conn.execute("SELECT member_name, line_no, substr(text,1,120) FROM src_fts "
                        "WHERE kind='doc' AND UPPER(text) LIKE UPPER(?) LIMIT 10", (pat,)).fetchall()
    if docs:
        out.append("\n### Documents mentioning it (prose, not facts)\n")
        out.append(table(["document", "section", "excerpt"], [tuple(d) for d in docs]))
    out.append("\n> Messages held in a DB2 message table, an ISPF message member or an online message file are "
               "not literals in code; load an unload of that source to cover them.\n")
    return "".join(out)


# --------------------------------------------------------------------------
# table / dbd / segment / layout
# --------------------------------------------------------------------------

def cmd_table(conn: sqlite3.Connection, name: str) -> str:
    """Who creates / reads / updates / deletes a DB2 table, its declared
    columns, cursors, dynamic SQL and stored-procedure use - estate-wide."""
    n = name.upper()
    qual, _, base = n.rpartition(".")
    out = [f"# DB2 table {n}\n"]
    objs = conn.execute("""SELECT o.*, m.name AS mem FROM db2_object o LEFT JOIN member m ON m.id=o.member_id
                           WHERE UPPER(o.name)=? AND o.kind='table'""" + (" AND UPPER(o.qualifier)=?" if qual else ""),
                        (base, qual) if qual else (base,)).fetchall()
    for o in objs:
        cols = conn.execute("SELECT ordinal, name, type FROM db2_column WHERE object_id=? ORDER BY ordinal", (o["id"],)).fetchall()
        out.append(f"\n### Columns of {(o['qualifier'] + '.') if o['qualifier'] else ''}{o['name']} "
                   f"(from {o['source']}{(' in ' + o['mem']) if o['mem'] else ''})\n")
        out.append(table(["#", "column", "type"], [(c["ordinal"], c["name"], c["type"] or "") for c in cols]))
    if not objs:
        out.append("\n_No DCLGEN / DDL declares this table in the index: column list unknown (host structures "
                   "are still matched by SQL statement)._\n")
    # statements: sql_stmt.tables is a JSON array of names as written
    pats = [f'%"{base}"%', f'%.{base}"%']
    rows = conn.execute("""SELECT s.stmt_type, s.tables, s.cursor_name, s.is_dynamic, s.start_line, p.program_id, p.id AS pid
                           FROM sql_stmt s JOIN program p ON p.id=s.program_id
                           WHERE s.tables LIKE ? OR s.tables LIKE ? ORDER BY p.program_id, s.start_line""", pats).fetchall()
    if qual:
        rows = [r for r in rows if any(t.upper() in (n, base) for t in _jl(r["tables"]))]
    agg: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        agg[r["program_id"]][r["stmt_type"]].append(cite(conn, r["pid"], r["start_line"]))
    out.append(f"\n### Programs and verbs ({len(agg)} programs, {len(rows)} statements)\n")
    trows = []
    for pgm in sorted(agg):
        crud = "".join(c for c, verbs in (("C", ("INSERT",)), ("R", ("SELECT", "DECLARE", "FETCH", "OPEN")),
                                           ("U", ("UPDATE", "MERGE")), ("D", ("DELETE",)))
                       if any(v in agg[pgm] for v in verbs))
        trows.append((pgm, crud, ", ".join(f"{v} x{len(c)} @{c[0]}" for v, c in sorted(agg[pgm].items()))))
    out.append(table(["program", "CRUD", "statements (first cite each)"], trows))
    dyn = conn.execute("""SELECT COUNT(*) FROM sql_stmt s WHERE s.is_dynamic=1 AND (s.tables LIKE ? OR s.tables LIKE ?)""",
                       pats).fetchone()[0]
    dyn_all = conn.execute("SELECT COUNT(*) FROM sql_stmt WHERE is_dynamic=1").fetchone()[0]
    if dyn_all:
        out.append(f"\n**{dyn_all} dynamic SQL statement(s) in the estate** ({dyn} naming this table statically) - "
                   f"their tables are not knowable from source.\n")
    # column-level detail
    cols = conn.execute("""SELECT c.col, c.mode, COUNT(*) AS n, COUNT(DISTINCT c.program_id) AS pgms FROM sql_col_ref c
                           WHERE UPPER(c.tbl)=? OR UPPER(c.tbl) LIKE ? GROUP BY c.col, c.mode ORDER BY c.col, c.mode""",
                        (base, f"%.{base}")).fetchall()
    if cols:
        out.append("\n### Columns referenced (see `column TABLE.COL` for the field lineage)\n")
        out.append(table(["column", "mode", "refs", "programs"], [(c["col"], c["mode"], c["n"], c["pgms"]) for c in cols]))
    util = conn.execute("""SELECT st.op, st.tbl, st.via_dd, st.direction, j.job_name, s.step_name, s.line, s.from_proc,
                                  m.name AS mem, d.dsn_resolved
                           FROM step_table st JOIN step s ON s.id=st.step_id LEFT JOIN job j ON j.id=s.job_id
                           LEFT JOIN member m ON m.id=j.member_id
                           LEFT JOIN dd d ON d.step_id=s.id AND UPPER(d.dd_name)=UPPER(st.via_dd)
                           WHERE (UPPER(st.tbl)=? OR UPPER(st.tbl) LIKE ?) AND s.job_id IS NOT NULL
                           ORDER BY j.job_name, s.ordinal""", (base, f"%.{base}")).fetchall()
    if util:
        out.append("\n### Batch utilities (LOAD writes the table from a file; UNLOAD / DSNTIAUL read it into one)\n")
        out.append(table(["job", "step", "op", "direction", "via DD", "dataset", "cite"],
                         [(u["job_name"], u["step_name"], u["op"], u["direction"], u["via_dd"] or "",
                           u["dsn_resolved"] or "", f"{u['from_proc'] or u['mem']}:{u['line']}") for u in util]))
    plans = conn.execute("SELECT DISTINCT tran_code, detail FROM transaction_def WHERE detail LIKE '%DB2 plan%'").fetchall()
    if plans:
        out.append("\n- CICS transactions with a DB2 plan (from DB2ENTRY/DB2TRAN): "
                   + "; ".join(f"{p['tran_code']}: {re.search(r'DB2 plan ([A-Z0-9@#$]+)', p['detail']).group(1)}"
                               for p in plans if re.search(r'DB2 plan ([A-Z0-9@#$]+)', p['detail'] or ''))[:400] + "\n")
    out.append("\n> Views, aliases and BIND QUALIFIER are not resolved: a program reading a VIEW over this table is "
               "not listed. Batch LOAD/UNLOAD steps are not yet joined (see ROADMAP).\n")
    return "".join(out)


def cmd_dbd(conn: sqlite3.Connection, name: str) -> str:
    """An IMS database: segments and fields, the PSBs/PCBs that address it
    (with PROCOPT), the programs whose DL/I calls resolve to it, the utility
    jobs that copy/unload/reload it, and its online definition."""
    n = name.upper()
    dbds = conn.execute("""SELECT d.*, m.name AS mem, m.path FROM ims_dbd d JOIN member m ON m.id=d.member_id
                           WHERE UPPER(d.name)=?""", (n,)).fetchall()
    out = [f"# IMS database {n}\n"]
    if not dbds:
        out.append("**NOT FOUND** - no DBD with this name is indexed.\n")
    for d in dbds:
        out.append(f"\n## DBD {d['name']}  ACCESS={d['access'] or '?'}  `{d['mem']}:{d['line']}`"
                   + (f"  DD {d['dd1']}" + (f"/{d['dd2']}" if d['dd2'] else "") if d["dd1"] else "") + "\n")
        segs = conn.execute("SELECT * FROM ims_segment WHERE dbd_id=? ORDER BY id", (d["id"],)).fetchall()
        srows = []
        for s in segs:
            flds = conn.execute("SELECT name, start, bytes, is_seq FROM ims_field WHERE segment_id=? ORDER BY start", (s["id"],)).fetchall()
            srows.append((s["name"], s["parent"] or "(root)", s["bytes"], s["seq_field"] or "",
                          ", ".join(f"{f['name']}@{f['start']}/{f['bytes']}" + ("*" if f["is_seq"] else "") for f in flds)[:120]))
        out.append(table(["segment", "parent", "bytes", "seq field", "fields (name@start/len, *=SEQ)"], srows))
        xd = conn.execute("SELECT name, segment, srch FROM ims_xdfld WHERE dbd_id=?", (d["id"],)).fetchall()
        if xd:
            out.append("- secondary index search fields (XDFLD): " + "; ".join(
                f"{x['name']} on {x['segment']} from {', '.join(_jl(x['srch']))}" for x in xd) + "\n")
    pcbs = conn.execute("""SELECT ps.name AS psb, pc.ordinal, pc.procopt, pc.procseq, pc.list_no, pc.sensegs, m.name AS mem, pc.line
                           FROM ims_pcb pc JOIN ims_psb ps ON ps.id=pc.psb_id JOIN member m ON m.id=ps.member_id
                           WHERE UPPER(pc.dbd_name)=? ORDER BY ps.name, pc.ordinal""", (n,)).fetchall()
    if pcbs:
        out.append("\n### PSBs / PCBs addressing it\n")
        out.append(table(["PSB", "PCB #", "PROCOPT", "PROCSEQ", "LIST", "sensitive segments", "cite"],
                         [(p["psb"], p["ordinal"], p["procopt"] or "", p["procseq"] or "", "NO" if p["list_no"] else "",
                           ", ".join(_jl(p["sensegs"]))[:60], f"{p['mem']}:{p['line']}") for p in pcbs]))
    calls = conn.execute("""SELECT p.program_id, p.id AS pid, d.func, d.procopt, d.psb_name, d.io_area, d.line
                            FROM dli_call d JOIN program p ON p.id=d.program_id WHERE UPPER(d.dbd_name)=?
                            ORDER BY p.program_id, d.line""", (n,)).fetchall()
    if calls:
        out.append("\n### Programs whose DL/I calls resolve to it\n")
        agg: Dict[str, List[sqlite3.Row]] = defaultdict(list)
        for c in calls:
            agg[c["program_id"]].append(c)
        out.append(table(["program", "PSB", "functions", "PROCOPT", "updates?", "first cite"],
                         [(pg, cs[0]["psb_name"] or "", ", ".join(sorted({c["func"] or "?" for c in cs})),
                           cs[0]["procopt"] or "",
                           "YES" if any((c["func"] or "") in ("ISRT", "REPL", "DLET") for c in cs) else "no",
                           cite(conn, cs[0]["pid"], cs[0]["line"])) for pg, cs in sorted(agg.items())]))
    util = conn.execute("""SELECT j.job_name, s.step_name, s.effective_pgm, d.mode, s.line, s.from_proc, m.name AS mem
                           FROM dd d JOIN step s ON s.id=d.step_id LEFT JOIN job j ON j.id=s.job_id
                           LEFT JOIN member m ON m.id=j.member_id
                           WHERE d.dd_name='*DBD*' AND UPPER(d.dsn_resolved)=?""", (n,)).fetchall()
    if util:
        out.append("\n### Utility jobs (image copy / unload / reload)\n")
        out.append(table(["job", "step", "utility", "reads/writes", "cite"],
                         [(u["job_name"] or "(PROC)", u["step_name"], u["effective_pgm"], u["mode"],
                           f"{u['from_proc'] or u['mem']}:{u['line']}") for u in util]))
    onl = conn.execute("SELECT dbd, access, line FROM ims_online_db WHERE UPPER(dbd)=?", (n,)).fetchall()
    if onl:
        out.append("- online (stage-1 DATABASE): " + ", ".join(f"ACCESS={o['access'] or '?'}" for o in onl) + "\n")
    out.append("\n> A program is listed only when its PCB position resolved through an indexed PSB; "
               "`program X` shows the mapping notes for the rest.\n")
    return "".join(out)


def cmd_segment(conn: sqlite3.Connection, name: str) -> str:
    """Who touches an IMS segment: the DBDs holding it, the PCBs sensitive to
    it, and the programs using those PCBs (with function and PROCOPT)."""
    n = name.upper()
    segs = conn.execute("""SELECT s.*, d.name AS dbd FROM ims_segment s JOIN ims_dbd d ON d.id=s.dbd_id
                           WHERE UPPER(s.name)=?""", (n,)).fetchall()
    out = [f"# IMS segment {n}\n"]
    if not segs:
        return out[0] + "\n**NOT FOUND** in any indexed DBD.\n"
    out.append(table(["DBD", "parent", "bytes", "seq field"],
                     [(s["dbd"], s["parent"] or "(root)", s["bytes"], s["seq_field"] or "") for s in segs]))
    dbds = sorted({s["dbd"].upper() for s in segs})
    q = ",".join("?" * len(dbds))
    pcbs = conn.execute(f"""SELECT ps.name AS psb, pc.ordinal, pc.dbd_name, pc.procopt, pc.sensegs
                            FROM ims_pcb pc JOIN ims_psb ps ON ps.id=pc.psb_id
                            WHERE UPPER(pc.dbd_name) IN ({q}) AND pc.sensegs LIKE ?""", (*dbds, f'%"{n}"%')).fetchall()
    if pcbs:
        out.append("\n### PCBs sensitive to it\n")
        out.append(table(["PSB", "PCB #", "DBD", "PROCOPT"],
                         [(p["psb"], p["ordinal"], p["dbd_name"], p["procopt"] or "") for p in pcbs]))
    keys = {(p["psb"].upper(), p["ordinal"]) for p in pcbs}
    # EXEC DLI SEGMENT(X) names the segment even when the PCB position could
    # not be resolved through a PSB.
    calls = conn.execute(f"""SELECT p.program_id, p.id AS pid, d.func, d.procopt, d.psb_name, d.pcb_ordinal, d.ssa_args, d.line
                             FROM dli_call d JOIN program p ON p.id=d.program_id
                             WHERE UPPER(d.dbd_name) IN ({q}) OR d.ssa_args LIKE ? ORDER BY p.program_id, d.line""",
                         (*dbds, f'%"{n}"%')).fetchall()
    rows = []
    for c in calls:
        via_pcb = (c["psb_name"] or "").upper(), c["pcb_ordinal"]
        named = n in [x.upper() for x in _jl(c["ssa_args"])]
        if via_pcb in keys or named:
            rows.append((c["program_id"], c["func"] or "?", c["procopt"] or "", c["psb_name"] or "",
                         "SEGMENT() names it" if named else "PCB sensitive to it", cite(conn, c["pid"], c["line"])))
    if rows:
        out.append("\n### Programs (DL/I calls on a PCB sensitive to this segment)\n")
        out.append(table(["program", "func", "PROCOPT", "PSB", "how", "cite"], rows))
        out.append("> CBLTDLI calls name the segment inside the SSA data, not in the call: a call on a "
                   "multi-segment PCB may touch a sibling segment instead. EXEC DLI SEGMENT() is exact.\n")
    return "".join(out)


def cmd_layout(conn: sqlite3.Connection, name: str, program: Optional[str] = None) -> str:
    """The byte layout of a copybook / 01 record from the parser's numbers:
    offsets, lengths, PIC, usage, OCCURS/ODO/REDEFINES, 88 values, record
    length. This is what test data and interface contracts are built from -
    never from the model's own COMP-3 arithmetic."""
    n = name.upper()
    out = [f"# Layout {n}" + (f" (as seen in {program.upper()})" if program else "") + "\n"]
    def top_of(fid: int) -> sqlite3.Row:
        r = conn.execute("SELECT * FROM field WHERE id=?", (fid,)).fetchone()
        while r and r["parent_id"]:
            r = conn.execute("SELECT * FROM field WHERE id=?", (r["parent_id"],)).fetchone()
        return r

    members: List[Tuple[int, sqlite3.Row]] = []
    if program:
        progs = programs_named(conn, program)
        if not progs:
            return out[0] + f"\nprogram {program} not found\n"
        mid, pid = progs[0]["member_id"], progs[0]["id"]
        roots = conn.execute("SELECT * FROM field WHERE member_id=? AND parent_id IS NULL AND UPPER(name)=?", (mid, n)).fetchall()
        members = [(mid, r) for r in roots]
        if not members:
            # COPY X REPLACING: the renamed fields live in the PROGRAM's rows
            # under the 01 that holds the COPY - find it through the aliases.
            renamed = [r["new_name"].upper() for r in conn.execute(
                "SELECT new_name FROM field_alias WHERE program_id=? AND (UPPER(copybook)=? OR UPPER(orig_name)=?)",
                (pid, n, n))]
            seen_ids = set()
            for nm in renamed:
                fr = conn.execute("SELECT id FROM field WHERE member_id=? AND UPPER(name)=?", (mid, nm)).fetchone()
                if fr:
                    top = top_of(fr["id"])
                    if top and top["id"] not in seen_ids:
                        seen_ids.add(top["id"])
                        members.append((mid, top))
        if not members:
            # a copybook the program includes without REPLACING: its own rows
            cb = conn.execute("""SELECT resolved_member_id FROM copy_use WHERE member_id=? AND UPPER(copybook)=?
                                 AND resolved_member_id IS NOT NULL""", (mid, n)).fetchone()
            if cb:
                members = [(cb[0], r) for r in conn.execute(
                    "SELECT * FROM field WHERE member_id=? AND parent_id IS NULL ORDER BY id", (cb[0],)).fetchall()]
    else:
        for m in conn.execute("SELECT id, name, path FROM member WHERE UPPER(name)=? AND kind IN ('copybook','cobol')", (n,)):
            for r in conn.execute("SELECT * FROM field WHERE member_id=? AND parent_id IS NULL ORDER BY id", (m["id"],)):
                members.append((m["id"], r))
        if not members:
            for r in conn.execute("""SELECT f.* FROM field f WHERE UPPER(f.name)=? AND f.parent_id IS NULL""", (n,)):
                members.append((r["member_id"], r))
    if not members:
        return out[0] + "\n**NOT FOUND** - no copybook member or 01 level with this name is indexed.\n"

    # A copybook FRAGMENT (a run of 05s with the 01 in the including program)
    # is one layout, not one table per 05.
    groups: List[Tuple[int, List[sqlite3.Row]]] = []
    for mid, root in members:
        if groups and groups[-1][0] == mid and root["level"] > 1 and groups[-1][1][-1]["level"] > 1:
            groups[-1][1].append(root)
        else:
            groups.append((mid, [root]))
    for mid, roots in groups:
        mem = conn.execute("SELECT name, path, kind FROM member WHERE id=?", (mid,)).fetchone()
        root = roots[0]
        title = root["name"] if len(roots) == 1 else f"(fragment: {len(roots)} top-level items, 01 is in the including program)"
        out.append(f"\n## {title}  in `{mem['name']}` ({mem['kind']})  line {root['line']}\n")
        out.append("```\n")
        out.append(f"{'OFF':>6} {'LEN':>4} {'LVL':>3}  {'FIELD':<34} {'PIC':<16} {'USAGE':<8}\n")
        out.append("-" * 82 + "\n")
        warn = []

        def walk(f, depth):
            ind = "  " * depth
            occ = f"  OCCURS {f['occurs_max']}" if f["occurs_max"] else ""
            odo = f" DEPENDING ON {f['odo_on']}" if f["odo_on"] else ""
            red = f"  REDEFINES {f['redefines']}" if f["redefines"] else ""
            nm = f["name"]
            out.append(f"{f['offset']:>6} {f['length']:>4} {f['level']:>3}  {(ind + nm):<34} {(f['pic'] or ''):<16} "
                       f"{(f['usage'] or ''):<8}{occ}{odo}{red}\n")
            if f["odo_on"]:
                warn.append(f"{nm}: OCCURS DEPENDING ON {f['odo_on']} - offsets after it hold for the MAXIMUM "
                            f"({f['occurs_max']}) occurrences only")
            for c in conn.execute("SELECT name, values_lit FROM cond88 WHERE field_id=? ORDER BY id", (f["id"],)):
                out.append(f"{'':>6} {'':>4}  88  {(ind + '  ' + c['name']):<34} VALUE {' '.join(_jl(c['values_lit']))}\n")
            for c in conn.execute("SELECT * FROM field WHERE parent_id=? ORDER BY id", (f["id"],)):
                walk(c, depth + 1)

        for r in roots:
            walk(r, 0)
        total = max(r["offset"] + r["length"] for r in roots)
        out.append("-" * 82 + "\n")
        out.append(f"record length: {total} bytes (VB files: +4 for the RDW; the LRECL in the JCL/IDCAMS must agree)\n")
        out.append("```\n")
        for w in warn:
            out.append(f"- {w}\n")
        aliases = conn.execute("SELECT p.program_id, a.new_name, a.orig_name FROM field_alias a JOIN program p ON p.id=a.program_id "
                               "WHERE UPPER(a.copybook)=? LIMIT 12", (mem["name"].upper(),)).fetchall()
        if aliases:
            out.append("- REPLACING in: " + "; ".join(f"{a['program_id']} ({a['orig_name']} -> {a['new_name']})" for a in aliases[:6]) + "\n")
    out.append("\n> Offsets are 0-based bytes from the parser (COMP-3 packed, COMP 2/4/8, SIGN SEPARATE +1); SYNC "
               "alignment is NOT modelled. Cite as `[[MEMBER line \"05  FIELD-NAME\"]]`.\n")
    return "".join(out)


# --------------------------------------------------------------------------
# crud / conditions / paragraph - the small analysis questions
# --------------------------------------------------------------------------

def _crud_of(conn: sqlite3.Connection, pid: int) -> Dict[Tuple[str, str], Dict[str, str]]:
    """{(kind, target): {'C'|'R'|'U'|'D': first cite}} for one program from
    file OPEN modes + WRITE/REWRITE/DELETE, SQL verbs, DL/I functions (with
    the resolved database) and CICS file verbs."""
    out: Dict[Tuple[str, str], Dict[str, str]] = defaultdict(dict)

    def mark(key: Tuple[str, str], letters: str, line: int) -> None:
        for c in letters:
            out[key].setdefault(c, cite(conn, pid, line))

    # files: the SELECT -> DD -> dataset name when a job is indexed, else the DD
    dsn_by_sel: Dict[str, str] = {}
    for f in conn.execute("SELECT select_name, assign_dd FROM file_decl WHERE program_id=?", (pid,)):
        d = conn.execute("""SELECT d.dsn_resolved FROM dd d JOIN step s ON s.id=d.step_id
                            WHERE UPPER(d.dd_name)=? AND s.effective_pgm=(SELECT program_id FROM program WHERE id=?)
                              AND d.dsn_resolved IS NOT NULL AND s.job_id IS NOT NULL LIMIT 1""",
                         ((f["assign_dd"] or "").upper(), pid)).fetchone()
        dsn_by_sel[f["select_name"]] = d[0] if d else f"DD {f['assign_dd'] or '?'}"
    for o in conn.execute("SELECT target, target_kind, op, line FROM io_op WHERE program_id=? ORDER BY line", (pid,)):
        t, k, op = o["target"], o["target_kind"], o["op"].upper()
        if k == "file":
            key = ("file", dsn_by_sel.get(t, t))
            if op.startswith("OPEN INPUT") or op in ("READ", "START", "SORT READ", "RETURN"):
                mark(key, "R", o["line"])
            elif op.startswith("OPEN OUTPUT") or op in ("WRITE", "SORT WRITE", "RELEASE"):
                mark(key, "C", o["line"])
            elif op.startswith("OPEN EXTEND"):
                mark(key, "C", o["line"])
            elif op.startswith("OPEN I-O"):
                mark(key, "RU", o["line"])
            elif op == "REWRITE":
                mark(key, "U", o["line"])
            elif op == "DELETE":
                mark(key, "D", o["line"])
        elif k == "db2":
            key = ("db2", t)
            mark(key, {"SELECT": "R", "DECLARE": "R", "FETCH": "R", "OPEN": "R", "INSERT": "C", "UPDATE": "U",
                       "MERGE": "CU", "DELETE": "D", "CALL": "X"}.get(op, ""), o["line"])
        elif k == "cics":
            key = ("cics file", t)
            mark(key, {"READ": "R", "STARTBR": "R", "READNEXT": "R", "READPREV": "R", "WRITE": "C",
                       "REWRITE": "U", "DELETE": "D"}.get(op, ""), o["line"])
    for d in conn.execute("SELECT func, dbd_name, pcb_arg, line FROM dli_call WHERE program_id=?", (pid,)):
        key = ("ims", d["dbd_name"] or f"PCB {d['pcb_arg'] or '?'} (unresolved)")
        mark(key, {"GU": "R", "GHU": "R", "GN": "R", "GHN": "R", "GNP": "R", "GHNP": "R", "ISRT": "C",
                   "REPL": "U", "DLET": "D"}.get(d["func"] or "", ""), d["line"])
    return out


def cmd_crud(conn: sqlite3.Connection, programs: List[str], job: Optional[str] = None,
             copybook: Optional[str] = None, system: Optional[str] = None) -> str:
    """One matrix: programs x (datasets, DB2 tables, IMS databases, CICS
    files) with C/R/U/D and a cite per cell - the D1 design-document table."""
    names = [p.upper() for p in programs]
    if job:
        names += [r[0].upper() for r in conn.execute("""SELECT DISTINCT s.effective_pgm FROM step s JOIN job j ON j.id=s.job_id
                                                        WHERE UPPER(j.job_name)=? AND s.effective_pgm IS NOT NULL
                                                          AND s.effective_pgm NOT LIKE '*%'""", (job.upper(),))]
    if copybook:
        names += [r[0].upper() for r in conn.execute("""SELECT DISTINCT p.program_id FROM copy_use c
                                                        JOIN program p ON p.member_id=c.member_id WHERE UPPER(c.copybook)=?""",
                                                     (copybook.upper(),))]
    if system:
        names += [r[0].upper() for r in conn.execute("""SELECT p.program_id FROM program p JOIN member m ON m.id=p.member_id
                                                        WHERE UPPER(m.system)=?""", (system.upper(),))]
    names = sorted(set(names))
    out = [f"# CRUD matrix ({len(names)} programs)\n"]
    if not names:
        return out[0] + "\n_no programs selected_\n"
    rows = []
    for n in names:
        progs = programs_named(conn, n)
        if not progs:
            rows.append((n, "-", "-", "**not in index**", ""))
            continue
        for (kind, target), cells in sorted(_crud_of(conn, progs[0]["id"]).items()):
            letters = "".join(c for c in "CRUDX" if c in cells)
            rows.append((n, kind, target, letters, "; ".join(f"{c}@{cells[c]}" for c in letters)))
    out.append(table(["program", "kind", "file / table / database", "CRUD", "cites"], rows))
    out.append("\n> C = creates rows/records (INSERT, WRITE, OPEN OUTPUT/EXTEND, ISRT), R = reads, U = updates "
               "(UPDATE, REWRITE, REPL, OPEN I-O), D = deletes, X = stored-procedure call. A file shows its dataset "
               "name only when an indexed job runs the program; otherwise the DD name. IMS rows need the PSB "
               "(see `program`).\n")
    return "".join(out)


def cmd_conditions(conn: sqlite3.Connection, name: str) -> str:
    """The test-condition inventory of a program: every IF/WHEN/UNTIL with the
    field, the literal or 88 name it tests (88s expanded to their values),
    per paragraph, plus one out-of-domain negative per tested field."""
    progs = programs_named(conn, name)
    if not progs:
        return f"program {name} not found\n"
    p = progs[0]
    pid = p["id"]
    out = [f"# Test conditions for {p['program_id']}\n"]
    paras = conn.execute("SELECT name, start_line, end_line FROM paragraph WHERE program_id=? ORDER BY ordinal", (pid,)).fetchall()

    def para_of(line: int) -> str:
        for q in paras:
            if q["start_line"] <= line <= q["end_line"]:
                return q["name"]
        return "(before the first paragraph)"

    lits = conn.execute("""SELECT literal, context, field, line FROM literal_ref WHERE program_id=?
                           AND context IN ('compare','when') ORDER BY line""", (pid,)).fetchall()
    tests = conn.execute("""SELECT name, stmt, line FROM field_ref WHERE program_id=? AND mode='test' ORDER BY line""",
                         (pid,)).fetchall()
    c88 = {}
    for r in conn.execute("""SELECT c.name, c.values_lit, f.name AS parent FROM cond88 c JOIN field f ON f.id=c.field_id
                             WHERE f.member_id=? OR f.member_id IN (SELECT resolved_member_id FROM copy_use WHERE member_id=?)""",
                          (p["member_id"], p["member_id"])):
        c88[r["name"].upper()] = (r["parent"].upper(), " ".join(_jl(r["values_lit"])))
    rows = []
    by_field: Dict[str, set] = defaultdict(set)
    seen = set()
    for l in lits:
        key = (l["line"], l["field"], l["literal"])
        if key in seen:
            continue
        seen.add(key)
        fld = (l["field"] or "").split("/")[0]
        rows.append((para_of(l["line"]), l["context"].upper(), fld or "?", f"'{l['literal']}'", cite(conn, pid, l["line"])))
        if fld:
            by_field[fld].add(l["literal"])
    for t in tests:
        n = t["name"].upper()
        if n in c88:
            parent, vals = c88[n]
            rows.append((para_of(t["line"]), t["stmt"], parent, f"{n} = {vals}", cite(conn, pid, t["line"])))
            for v in vals.replace("'", "").split():
                if v not in ("THRU",):
                    by_field[parent].add(v)
    rows.sort(key=lambda r: r[4])
    out.append(table(["paragraph", "test", "field", "condition", "cite"], rows))
    other = conn.execute("SELECT COUNT(*) FROM literal_ref WHERE program_id=? AND context='when' AND literal='OTHER'", (pid,)).fetchone()[0]
    out.append(f"\n{len(rows)} conditions in {len(paras)} paragraphs; WHEN OTHER branches: {other}.\n")
    if by_field:
        out.append("\n### Negative cases (one value outside every tested value per field)\n")
        neg = []
        for fld, vals in sorted(by_field.items()):
            dom = sorted(vals)
            neg.append((fld, ", ".join(dom)[:80], "any value not listed (e.g. a blank / 'ZZ' / 99) - and one per boundary of a THRU"))
        out.append(table(["field", "values tested", "negative"], neg))
    out.append("\n> Conditions on fields tested only through group MOVEs, arithmetic or SQL predicates are not "
               "listed; `values <FIELD>` shows every value the estate assumes for a field.\n")
    return "".join(out)


def cmd_paragraph(conn: sqlite3.Connection, name: str, para: str) -> str:
    """One paragraph (or the paragraph holding a line number): who reaches
    it and how, what it performs/calls/does to which fields, with the source."""
    progs = programs_named(conn, name)
    if not progs:
        return f"program {name} not found\n"
    p = progs[0]
    pid = p["id"]
    if para.isdigit():
        row = conn.execute("""SELECT * FROM paragraph WHERE program_id=? AND ? BETWEEN start_line AND end_line
                              ORDER BY kind='section', start_line DESC LIMIT 1""", (pid, int(para))).fetchone()
    else:
        row = conn.execute("SELECT * FROM paragraph WHERE program_id=? AND UPPER(name)=?", (pid, para.upper())).fetchone()
    if not row:
        return f"# {p['program_id']} {para}\n\n**NOT FOUND** - no such paragraph/section in this program.\n"
    s, e = row["start_line"], row["end_line"]
    out = [f"# {p['program_id']} {row['name']} ({row['kind']}, expanded lines {s}-{e}, "
           f"{cite(conn, pid, s)} - {cite(conn, pid, e)})\n"]
    inbound = conn.execute("""SELECT from_para, kind, thru_para, line FROM perform_edge WHERE program_id=? AND
                              (UPPER(to_para)=? OR UPPER(thru_para)=? OR UPPER(to_para) LIKE ? OR UPPER(thru_para) LIKE ?)
                              ORDER BY line""",
                           (pid, row["name"].upper(), row["name"].upper(),
                            row["name"].upper() + " OF %", row["name"].upper() + " OF %")).fetchall()
    # a THRU range that covers this paragraph reaches it too
    names = [q["name"] for q in conn.execute("SELECT name FROM paragraph WHERE program_id=? AND kind='paragraph' ORDER BY ordinal", (pid,))]
    thru = []
    if row["name"] in names:
        i = names.index(row["name"])
        for r in conn.execute("SELECT from_para, to_para, thru_para, line FROM perform_edge WHERE program_id=? AND thru_para IS NOT NULL", (pid,)):
            if r["to_para"] in names and r["thru_para"] in names and names.index(r["to_para"]) < i < names.index(r["thru_para"]):
                thru.append(r)
    out.append("\n### Reached by\n")
    rows = [(r["from_para"] or "(top)", r["kind"] + (f" THRU {r['thru_para']}" if r["thru_para"] else ""), cite(conn, pid, r["line"]))
            for r in inbound] + [(r["from_para"], f"inside PERFORM {r['to_para']} THRU {r['thru_para']}", cite(conn, pid, r["line"])) for r in thru]
    out.append(table(["from", "how", "cite"], rows) if rows else "_never reached by PERFORM / GO TO / fall-through / THRU range_\n")
    out.append("\n### Does\n")
    perf = conn.execute("SELECT to_para, thru_para, kind, line FROM perform_edge WHERE program_id=? AND UPPER(from_para)=? ORDER BY line",
                        (pid, row["name"].upper())).fetchall()
    if perf:
        out.append("- control: " + "; ".join(f"{r['kind']} {r['to_para']}" + (f" THRU {r['thru_para']}" if r["thru_para"] else "")
                                             + f" @{cite(conn, pid, r['line'])}" for r in perf) + "\n")
    calls = conn.execute("SELECT kind, target, via_var, line FROM call_edge WHERE program_id=? AND line BETWEEN ? AND ?", (pid, s, e)).fetchall()
    if calls:
        out.append("- calls: " + "; ".join(f"{c['kind']} {c['target'] or c['via_var']} @{cite(conn, pid, c['line'])}" for c in calls) + "\n")
    for kind, sql in (("SQL", "SELECT stmt_type AS what, tables AS detail, start_line AS line FROM sql_stmt WHERE program_id=? AND start_line BETWEEN ? AND ?"),
                      ("DL/I", "SELECT func AS what, COALESCE(dbd_name, pcb_arg) AS detail, line FROM dli_call WHERE program_id=? AND line BETWEEN ? AND ?"),
                      ("CICS", "SELECT verb AS what, resource AS detail, line FROM cics_cmd WHERE program_id=? AND line BETWEEN ? AND ?"),
                      ("file", "SELECT op AS what, target AS detail, line FROM io_op WHERE program_id=? AND target_kind='file' AND line BETWEEN ? AND ?")):
        rs = conn.execute(sql, (pid, s, e)).fetchall()
        if rs:
            out.append(f"- {kind}: " + "; ".join(f"{r['what']} {r['detail']} @{cite(conn, pid, r['line'])}" for r in rs) + "\n")
    for mode in ("write", "test", "display"):
        rs = conn.execute("SELECT DISTINCT name FROM field_ref WHERE program_id=? AND mode=? AND line BETWEEN ? AND ? ORDER BY name",
                          (pid, mode, s, e)).fetchall()
        if rs:
            out.append(f"- fields {mode}: " + ", ".join(r["name"] for r in rs[:40]) + (" ..." if len(rs) > 40 else "") + "\n")
    lits = conn.execute("SELECT literal, context, field, line FROM literal_ref WHERE program_id=? AND line BETWEEN ? AND ? ORDER BY line", (pid, s, e)).fetchall()
    if lits:
        out.append("- literals: " + "; ".join(f"'{l['literal']}' {l['context']}" + (f" {l['field']}" if l["field"] else "") for l in lits[:30]) + "\n")
    out.append("\n### Source\n```\n")
    m1, l1, _d, _v = origin(conn, pid, s)
    m2, l2, _d2, _v2 = origin(conn, pid, e)
    if m1 == m2 and m1:
        for i in range(l1, l2 + 1):
            out.append(f"{i:6d} | {source_line(conn, m1, i)}\n")
        out.append(f"```\n(cite as `[[{m1} line \"token\"]]`)\n")
    else:
        out.append("(the paragraph spans an expanded copybook - use `cite --program` with the expanded lines)\n```\n")
    return "".join(out)


# --------------------------------------------------------------------------
# diff - two versions of a member: the production copy and the changed one
# --------------------------------------------------------------------------

_DIFF_KINDS = ("cobol", "copybook", "jcl", "proc", "ctlcard", "dbd", "psb", "bms", "mfs", "sql", "unknown")


class _Loose(dict):
    """A file on disk that is not in the index (a member just downloaded
    into a scratch folder): its lines can be compared, its facts cannot."""

    def __getitem__(self, k):
        return dict.get(self, k)


def _member_by_ref(conn: sqlite3.Connection, ref: str) -> list:
    """The member(s) a reference names, production copy first:
    SYSTEM/NAME, NAME@LIBRARY, NAME(kind), NAME, or a path on disk."""
    import hashlib
    r = ref.strip().strip('"').strip("'")
    if not r:
        return []
    if os.path.isfile(r):
        p = os.path.normpath(os.path.abspath(r))
        rows = conn.execute("SELECT * FROM member WHERE UPPER(path)=UPPER(?)", (p,)).fetchall()
        if rows:
            return rows
        with open(p, "rb") as fh:
            data = fh.read()
        return [_Loose(id=None, path=p, name=os.path.splitext(os.path.basename(p))[0].upper(), kind=None,
                       library=os.path.basename(os.path.dirname(p)), system=None, authoritative=0,
                       fixed_format=None, sha256=hashlib.sha256(data).hexdigest(), norm_sha=None, lines=None)]
    m = re.match(r"^(?:([A-Za-z0-9_$#@.\-]+)/)?([A-Za-z0-9_$#@\-]+?)(?:\(([a-z]+)\))?(?:@([A-Za-z0-9_$#@.\-]+))?$", r)
    if not m:
        return []
    system, name, kind, library = m.group(1), m.group(2), m.group(3), m.group(4)
    rows = _members_named(conn, system, name, kind, library)
    if not rows and library:                      # a member really named AB@CD
        rows = _members_named(conn, system, f"{name}@{library}", kind, None)
    return rows


def _members_named(conn: sqlite3.Connection, system, name, kind, library) -> list:
    q = "SELECT * FROM member WHERE UPPER(name)=?"
    args: list = [name.upper()]
    if system:
        q += " AND UPPER(COALESCE(system,''))=?"
        args.append(system.upper())
    if library:
        q += " AND UPPER(COALESCE(library,'')) LIKE ?"
        args.append("%" + library.upper() + "%")
    if kind:
        q += " AND kind=?"
        args.append(kind)
    else:
        q += " AND kind IN (%s)" % ",".join("?" * len(_DIFF_KINDS))
        args.extend(_DIFF_KINDS)
    return conn.execute(q + " ORDER BY authoritative DESC, system, path", args).fetchall()


def _ident(m) -> str:
    """How the gate names this copy: SYSTEM/NAME when the copy has a system
    (the folder under estate\\), else NAME@LIBRARY."""
    if m["system"]:
        return f"{m['system']}/{m['name']}"
    if m["library"]:
        return f"{m['name']}@{m['library']}"
    return m["name"]


def _pick_versions(rows: list, system: Optional[str] = None) -> Tuple[object, object, str]:
    """Among copies of one name: (old, new, why). Old is the declared
    production copy, else the copy whose library or system says PROD, else
    the first by path - and the report says which rule decided."""
    if system:
        news = [r for r in rows if (r["system"] or "").upper() == system.upper()]
        olds = [r for r in rows if (r["system"] or "").upper() != system.upper()]
        if news and olds:
            olds.sort(key=lambda r: (0 if r["authoritative"] else 1,
                                     0 if "PROD" in (r["library"] or "").upper() else 1, r["path"]))
            return olds[0], news[0], f"new = the copy in {system.upper()}"
    auth = [r for r in rows if r["authoritative"]]
    prod = [r for r in rows if "PROD" in ((r["library"] or "") + "/" + (r["system"] or "")).upper()]
    if len(auth) == 1:
        old, why = auth[0], "old = the copy declared production in manifest.json"
    elif not auth and len(prod) == 1:
        old, why = prod[0], "old = the copy whose library name says PROD"
    else:
        old, why = rows[0], ("old = the first copy by path - NEITHER is declared production; give both sides "
                             "(diff OLD NEW) to be sure")
    others = [r for r in rows if r is not old]
    new = next((r for r in others if r["norm_sha"] != old["norm_sha"]), others[0])
    return old, new, why


def _facts_of(conn: sqlite3.Connection, m) -> Optional[Dict[str, dict]]:
    """Comparable facts of one copy, by kind: name -> detail (fields carry
    (picture, offset, length) so a shifted field is told from a changed one).
    None for a file that is not in the index."""
    if m["id"] is None:
        return None
    out: Dict[str, dict] = {"paragraphs": {}, "calls": {}, "copybooks": {}, "tables": {}, "files": {},
                            "dli": {}, "cics": {}, "fields": {}}
    for c in conn.execute("SELECT copybook, replacing FROM copy_use WHERE member_id=?", (m["id"],)):
        out["copybooks"][c["copybook"].upper()] = "with REPLACING" if c["replacing"] else ""
    for f in conn.execute("SELECT name, pic, usage, offset, length, level, is_group FROM field WHERE member_id=? "
                          "ORDER BY id", (m["id"],)):
        pic = (f["pic"] or ("group" if f["is_group"] else "?")) + \
              (f" {f['usage']}" if f["usage"] and f["usage"] != "DISPLAY" else "")
        out["fields"][f"{f['level']:02d} {f['name']}"] = (pic, f["offset"], f["length"])
    prog = conn.execute("SELECT id FROM program WHERE member_id=?", (m["id"],)).fetchone()
    if prog:
        pid = prog["id"]
        for p in conn.execute("SELECT name, kind FROM paragraph WHERE program_id=?", (pid,)):
            out["paragraphs"][p["name"]] = p["kind"]
        for c in conn.execute("SELECT kind, target, via_var, resolved FROM call_edge WHERE program_id=?", (pid,)):
            key = c["target"] or ", ".join(_jl(c["resolved"])) or f"{c['via_var']} (unresolved)"
            out["calls"][f"{_CALL_LABEL.get(c['kind'], c['kind'])} {key}"] = ""
        tabs: Dict[str, set] = {}
        for s in conn.execute("SELECT stmt_type, tables FROM sql_stmt WHERE program_id=?", (pid,)):
            for t in _jl(s["tables"]):
                tabs.setdefault(str(t).upper(), set()).add(s["stmt_type"] or "?")
        out["tables"] = {t: "/".join(sorted(v)) for t, v in tabs.items()}
        for f in conn.execute("SELECT select_name, assign_dd FROM file_decl WHERE program_id=?", (pid,)):
            out["files"][f["select_name"]] = f"DD {f['assign_dd'] or '?'}"
        for d in conn.execute("SELECT func, dbd_name, procopt FROM dli_call WHERE program_id=?", (pid,)):
            out["dli"][f"{d['func']} {d['dbd_name'] or '?'}"] = f"PROCOPT={d['procopt']}" if d["procopt"] else ""
        for c in conn.execute("SELECT verb, resource_kind, resource FROM cics_cmd WHERE program_id=?", (pid,)):
            out["cics"][f"{c['verb']} {c['resource_kind'] or ''} {c['resource'] or ''}".strip()] = ""
    return out


def _para_spans(conn: sqlite3.Connection, m) -> List[Tuple[int, int, str]]:
    """(first source line, last source line, paragraph) for the paragraphs
    written in this member itself (a copybook's paragraphs are its own)."""
    if m["id"] is None:
        return []
    prog = conn.execute("SELECT id FROM program WHERE member_id=?", (m["id"],)).fetchone()
    if not prog:
        return []
    spans = []
    for p in conn.execute("SELECT name, start_line, end_line FROM paragraph WHERE program_id=? AND kind='paragraph'",
                          (prog["id"],)):
        m1, l1, d1, _v = origin(conn, prog["id"], p["start_line"])
        m2, l2, _d2, _v2 = origin(conn, prog["id"], p["end_line"])
        if m1 == m["name"] and l1 is not None and not d1:
            spans.append((l1, l2 if (m2 == m["name"] and l2 is not None) else l1, p["name"]))
    return spans


def _load_lines(m) -> List[str]:
    text, data, enc = reader.load(m["path"])
    return [r.rstrip("\r\n") for r in reader._split_records(text, data, enc)]


def _line_key(fixed: bool):
    # sequence numbers (1-6) and change stamps (73-80) are not changes
    return (lambda s: s[6:72].rstrip()) if fixed else (lambda s: s.rstrip())


def _fmt_field(v: tuple) -> str:
    pic, off, ln = v
    return f"{pic} @{off} len {ln}"


def cmd_diff(conn: sqlite3.Connection, old_ref: Optional[str] = None, new_ref: Optional[str] = None,
             context: int = 3, budget: Optional[int] = None, system: Optional[str] = None) -> str:
    """Two versions of a member: what changed in the facts the index tracks
    (paragraphs, calls, copybooks, tables, files, DL/I, CICS; for data, the
    fields whose picture or length changed and the fields that only shifted),
    then the changed lines of both sides with their own line numbers, each
    citable as [[SYSTEM/NAME line "token"]]. With no member: every member
    that exists in two libraries with different content (the release)."""
    import difflib
    if not old_ref:
        return cmd_diff_list(conn, system)
    olds = _member_by_ref(conn, old_ref)
    if not olds:
        return (f"# Diff\n\n**NOT FOUND**: {old_ref} - give SYSTEM/NAME (as `ambiguous` and `program` show it), "
                f"NAME@LIBRARY, or the path of the file\n")
    why = ""
    if new_ref:
        news = _member_by_ref(conn, new_ref)
        if not news:
            return f"# Diff\n\n**NOT FOUND**: new side {new_ref} - SYSTEM/NAME, NAME@LIBRARY, or a path on disk\n"
        old, new = olds[0], news[0]
    else:
        cands = [r for r in olds if r["id"] is not None]
        if len(cands) < 2:
            return (f"# Diff {old_ref}\n\n{len(olds)} copy of {old_ref} in the index - a diff needs two. Put the "
                    f"changed copy under estate\\<SYSTEM-TEST>\\<its library>\\, build again (only new members are "
                    f"parsed), then `diff {old_ref}`; or name the downloaded file: `diff {old_ref} C:\\path\\{old_ref}.cbl`\n")
        old, new, why = _pick_versions(cands, system)
    if old["id"] is not None and old["id"] == new["id"]:
        return f"# Diff\n\nboth references name the same copy ({old['path']})\n"

    a, b = _load_lines(old), _load_lines(new)
    fixed = bool(old["fixed_format"] if old["fixed_format"] is not None else new["fixed_format"]) \
        and bool(new["fixed_format"] if new["fixed_format"] is not None else old["fixed_format"])
    key = _line_key(fixed)
    sm = difflib.SequenceMatcher(None, [key(s) for s in a], [key(s) for s in b], autojunk=False)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
    added = sum(j2 - j1 for _t, _i1, _i2, j1, j2 in ops)
    removed = sum(i2 - i1 for _t, i1, i2, _j1, _j2 in ops)
    oi, ni = _ident(old), _ident(new)
    loose = new["id"] is None or old["id"] is None

    def side(tag: str, m, n: int) -> str:
        return (f"- {tag}: `{os.path.basename(m['path'])}` in {m['library'] or '?'}"
                + (f", system {m['system']}" if m["system"] else "")
                + (" **[production]**" if m["authoritative"] else "")
                + (" (not in the index: lines only)" if m["id"] is None else "")
                + f", {n} lines, sha {(m['sha256'] or '')[:12]}\n")

    out = [f"# Diff {oi} -> {ni}\n", side("old", old, len(a)), side("new", new, len(b))]
    if why:
        out.append(f"- {why}\n")
    out.append(f"- {len(ops)} changed region(s): +{added} / -{removed} lines"
               + (" (columns 1-6 and 73-80 ignored)" if fixed else "") + "\n")
    if not ops:
        out.append("\n**identical**" + (" apart from sequence numbers / change stamps" if fixed else "") + "\n")
        return "".join(out)
    if old["system"] and new["system"]:
        out.append(f"- cite the old side as `[[{oi} line \"token\"]]`, the new side as `[[{ni} line \"token\"]]`\n")
    elif loose:
        out.append(f"- the copy that is not in the index cannot be cited; cite `[[{oi if old['id'] is not None else ni} "
                   f"line \"token\"]]` on the other side, or put the folder under estate\\ and build\n")
    else:
        out.append(f"- cite as `[[{oi} line \"token\"]]` / `[[{ni} line \"token\"]]` (NAME@LIBRARY: without a system "
                   f"per environment the plain [[NAME line]] form checks the production copy only)\n")

    # ---- facts that changed
    fo, fn = _facts_of(conn, old), _facts_of(conn, new)
    out.append("\n## What changed (from the index)\n")
    if fo is None or fn is None:
        out.append("- the copy that is not in the index has no facts: lines only. Put it under estate\\ and build "
                   "for the paragraph / call / copybook / field comparison\n")
    else:
        labels = [("paragraphs", "Paragraphs"), ("calls", "Calls"), ("copybooks", "Copybooks"), ("tables", "DB2 tables"),
                  ("files", "Files"), ("dli", "DL/I"), ("cics", "CICS")]
        any_fact = False
        for k, label in labels:
            o, n = fo[k], fn[k]
            add = sorted(set(n) - set(o))
            rem = sorted(set(o) - set(n))
            chg = sorted(x for x in set(o) & set(n) if o[x] != n[x])
            if not (add or rem or chg):
                continue
            any_fact = True
            parts = []
            if add:
                parts.append("added " + ", ".join(f"`{x}`" + (f" ({n[x]})" if n[x] else "") for x in add))
            if rem:
                parts.append("removed " + ", ".join(f"`{x}`" + (f" ({o[x]})" if o[x] else "") for x in rem))
            if chg:
                parts.append("changed " + ", ".join(f"`{x}` {o[x] or '-'} -> {n[x] or '-'}" for x in chg))
            out.append(f"- **{label}**: " + "; ".join(parts) + "\n")
        o, n = fo["fields"], fn["fields"]
        add = sorted(set(n) - set(o), key=lambda x: n[x][1] if n[x][1] is not None else 1 << 30)
        rem = sorted(set(o) - set(n), key=lambda x: o[x][1] if o[x][1] is not None else 1 << 30)
        chg = [x for x in o if x in n and o[x] != n[x]]
        redef = [x for x in chg if o[x][0] != n[x][0] or o[x][2] != n[x][2]]    # picture or length
        moved = [x for x in chg if x not in redef]                              # only the offset
        if add or rem or redef or moved:
            any_fact = True
            parts = []
            if add:
                parts.append("added " + ", ".join(f"`{x}` {_fmt_field(n[x])}" for x in add))
            if rem:
                parts.append("removed " + ", ".join(f"`{x}` {_fmt_field(o[x])}" for x in rem))
            if redef:
                parts.append("changed " + ", ".join(f"`{x}` {_fmt_field(o[x])} -> {_fmt_field(n[x])}" for x in redef))
            if parts:
                out.append("- **Fields**: " + "; ".join(parts) + "\n")
            if moved:
                by_delta: Dict[int, List[str]] = {}
                for x in moved:
                    by_delta.setdefault((n[x][1] or 0) - (o[x][1] or 0), []).append(x)
                for delta, names in sorted(by_delta.items()):
                    names.sort(key=lambda x: o[x][1] or 0)
                    out.append(f"- **Fields shifted** by {delta:+d} byte(s), same picture: {len(names)} field(s), "
                               f"from `{names[0]}` to `{names[-1]}` - every program that reads this record by "
                               f"position, and every file written with the OLD layout, sees them move\n")
        # a pure insert marks no old line (the old line after the insertion point is not a change),
        # a pure delete marks no new line
        changed_old = {ln for _t, i1, i2, _j1, _j2 in ops for ln in range(i1 + 1, i2 + 1)}
        changed_new = {ln for _t, _i1, _i2, j1, j2 in ops for ln in range(j1 + 1, j2 + 1)}
        new_or_gone = set(fn["paragraphs"]) ^ set(fo["paragraphs"])          # already listed as added / removed
        touched = sorted(({nm for s, e, nm in _para_spans(conn, new) if any(s <= ln <= e for ln in changed_new)}
                          | {nm for s, e, nm in _para_spans(conn, old) if any(s <= ln <= e for ln in changed_old)})
                         - new_or_gone)
        if touched:
            any_fact = True
            out.append("- **Paragraphs with changed lines**: " + ", ".join(f"`{p}`" for p in touched)
                       + f" - `walk {ni.split('/')[-1].split('@')[0]} --from PARA` / `paragraph` for what each does now\n")
        if not any_fact:
            out.append("- no fact the index tracks changed: the edit is inside statements (a condition, a MOVE, "
                       "a literal, a comment) - see the lines\n")

    # ---- the lines, both sides, their own numbers
    out.append(f"\n## Lines (`-` old {oi}, `+` new {ni}, two spaces = unchanged context)\n```\n")
    used = sum(len(x) for x in out) + 40
    shown = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        hunk: List[str] = []
        for x in range(max(0, i1 - context), i1):
            hunk.append(f"  {x + 1:6d} | {a[x]}\n")
        for x in range(i1, i2):
            hunk.append(f"- {x + 1:6d} | {a[x]}\n")
        for y in range(j1, j2):
            hunk.append(f"+ {y + 1:6d} | {b[y]}\n")
        for y in range(j2, min(len(b), j2 + context)):
            hunk.append(f"  {y + 1:6d} | {b[y]}\n")
        hunk.append("\n")
        text = "".join(hunk)
        if budget and used + len(text) > budget and shown:
            out.append(f"... budget {budget}: {len(ops) - shown} more changed region(s) not shown - "
                       f"`cite` the member for the rest\n")
            break
        out.append(text)
        used += len(text)
        shown += 1
    out.append("```\n")
    return "".join(out)


def cmd_diff_list(conn: sqlite3.Connection, system: Optional[str] = None) -> str:
    """Every member that exists in more than one library with different
    content - the contents of a release when one folder per environment is
    indexed (estate\\GC = production, estate\\GC-TEST = the changed copies)."""
    import difflib
    system = (system or "").strip() or None
    names = conn.execute("SELECT name, kind FROM member WHERE kind IN (%s) GROUP BY name, kind "
                         "HAVING COUNT(*) > 1 AND COUNT(DISTINCT COALESCE(norm_sha, sha256)) > 1 ORDER BY kind, name"
                         % ",".join("?" * len(_DIFF_KINDS)), _DIFF_KINDS).fetchall()
    title = "# Release contents - members whose copies differ" + (f" (changed copy in {system.upper()})" if system else "")
    out = [title + "\n\n"]
    rows_out = []
    same = 0
    for nm in names:
        copies = conn.execute("SELECT * FROM member WHERE name=? AND kind=? ORDER BY authoritative DESC, path",
                              (nm["name"], nm["kind"])).fetchall()
        if system and not any((c["system"] or "").upper() == system.upper() for c in copies):
            continue
        old, new, _why = _pick_versions(copies, system)
        if old["norm_sha"] == new["norm_sha"]:
            same += 1
            continue
        a, b = _load_lines(old), _load_lines(new)
        key = _line_key(bool(old["fixed_format"]) and bool(new["fixed_format"]))
        sm = difflib.SequenceMatcher(None, [key(s) for s in a], [key(s) for s in b], autojunk=False)
        ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
        added = sum(j2 - j1 for _t, _i1, _i2, j1, j2 in ops)
        removed = sum(i2 - i1 for _t, i1, i2, _j1, _j2 in ops)
        extra = len(copies) - 2
        rows_out.append(f"| {nm['name']} | {nm['kind']} | {_ident(old)} | {_ident(new)} | +{added} / -{removed} | "
                        f"`diff {_ident(old)} {_ident(new)}`" + (f" (+{extra} more copies)" if extra > 0 else "") + " |\n")
    if rows_out:
        out.append("| member | kind | old (production) | new (changed) | lines | command |\n|---|---|---|---|---|---|\n")
        out.extend(rows_out)
    else:
        out.append("_no member has two copies with different content_" + (f" involving {system.upper()}" if system else "") + "\n")
    if system:
        fresh = conn.execute("SELECT m.name, m.kind, m.library FROM member m WHERE UPPER(COALESCE(m.system,''))=? "
                             "AND m.kind IN (%s) AND NOT EXISTS (SELECT 1 FROM member o WHERE o.name=m.name AND o.kind=m.kind "
                             "AND o.id<>m.id) ORDER BY m.kind, m.name" % ",".join("?" * len(_DIFF_KINDS)),
                             (system.upper(), *_DIFF_KINDS)).fetchall()
        if fresh:
            out.append(f"\n## New in {system.upper()} (no copy anywhere else)\n")
            out.extend(f"- {f['name']} ({f['kind']}) in {f['library']}\n" for f in fresh)
    out.append(f"\n- {len(rows_out)} member(s) differ" + (f", {same} pair(s) identical apart from sequence numbers" if same else "")
               + "; `diff OLD NEW` for each - the facts that changed, then the lines of both sides\n")
    if not system:
        out.append("- `diff --system GC-TEST` when the changed copies are in one system folder: pairs each with its "
                   "production copy and lists members new to that system\n")
    return "".join(out)


# --------------------------------------------------------------------------
# walk - a program in READING order: the entry paragraph first, then every
# paragraph the first time control reaches it (PERFORM, GO TO, fall-through,
# THRU range, performed SECTION), each with its resolved facts and its
# source; the data it touches - and only that; the paragraphs nothing
# reaches. This is the read an analyst does by hand, so a model can do it
# from a few hundred lines instead of the whole member.
# --------------------------------------------------------------------------

_HOW_LABEL = {"perform": "PERFORM", "goto": "GO TO", "goto_depending": "GO TO DEPENDING", "alter": "ALTER GO TO",
              "sort_proc": "SORT PROCEDURE", "fallthrough": "falls through", "section": "in section",
              "range": "in THRU range", "entry": "ENTRY", "start": "entry"}
_CALL_LABEL = {"static": "CALL", "dynamic": "CALL", "cics_link": "LINK", "cics_xctl": "XCTL", "cics_start": "START",
               "cics_return": "RETURN TRANSID", "ims_switch": "CHNG ->", "proc_call": "SQL CALL"}
_NOTED_REACH = ("perform", "goto", "goto_depending", "alter", "sort_proc", "fallthrough")


def _walk_facts(conn: sqlite3.Connection, pid: int, s: int, e: int) -> str:
    """One line of RESOLVED facts for the statements on expanded lines s..e:
    calls with their targets, SQL verbs and tables, DL/I with the database
    and PROCOPT the PSB gave it, CICS resources, file operations, codes set."""
    bits: List[str] = []
    for c in conn.execute("SELECT kind, target, via_var, resolved, line FROM call_edge WHERE program_id=? "
                          "AND line BETWEEN ? AND ? ORDER BY line", (pid, s, e)):
        label = _CALL_LABEL.get(c["kind"], c["kind"].upper())
        if c["target"]:
            bits.append(f"{label} {c['target']}")
        else:
            bits.append(f"{label} {c['via_var']} -> {', '.join(_jl(c['resolved'])) or 'unresolved'}")
    for r in conn.execute("SELECT stmt_type, tables, cursor_name FROM sql_stmt WHERE program_id=? "
                          "AND start_line BETWEEN ? AND ? ORDER BY start_line", (pid, s, e)):
        bits.append(f"SQL {r['stmt_type']} " + (", ".join(_jl(r["tables"])) or (r["cursor_name"] or "")).strip())
    for r in conn.execute("SELECT func, dbd_name, procopt, pcb_arg, dest FROM dli_call WHERE program_id=? "
                          "AND line BETWEEN ? AND ? ORDER BY line", (pid, s, e)):
        if r["dest"]:
            bits.append(f"DL/I {r['func']} -> {r['dest']}")
        elif r["dbd_name"]:
            bits.append(f"DL/I {r['func']} {r['dbd_name']}" + (f" (PROCOPT={r['procopt']})" if r["procopt"] else ""))
        else:
            bits.append(f"DL/I {r['func']} (PCB {r['pcb_arg'] or '?'} unresolved)")
    for r in conn.execute("SELECT verb, resource_kind, resource FROM cics_cmd WHERE program_id=? "
                          "AND line BETWEEN ? AND ? ORDER BY line", (pid, s, e)):
        bits.append(f"CICS {r['verb']} {r['resource_kind'] or ''} {r['resource'] or ''}".rstrip())
    seen_io = set()
    for r in conn.execute("SELECT op, target, target_kind FROM io_op WHERE program_id=? AND target_kind IN ('file','mq') "
                          "AND line BETWEEN ? AND ? ORDER BY line", (pid, s, e)):
        key = (r["op"], r["target"])
        if key not in seen_io:
            seen_io.add(key)
            bits.append(f"{'MQ ' if r['target_kind'] == 'mq' else ''}{r['op']} {r['target']}")
    sets = conn.execute("SELECT literal, field FROM literal_ref WHERE program_id=? AND context='move_to' "
                        "AND line BETWEEN ? AND ? ORDER BY line LIMIT 7", (pid, s, e)).fetchall()
    if sets:
        bits.append("sets " + ", ".join(f"'{r['literal']}'->{r['field']}" for r in sets[:6]) + (" ..." if len(sets) > 6 else ""))
    return "; ".join(bits)


def _walk_block(conn: sqlite3.Connection, pid: int, pname: str, row: sqlite3.Row, how: str, depth: int,
                frm: Optional[str], via: Optional[int], first_para_line: Optional[int],
                source: bool, max_lines: int) -> Tuple[str, str]:
    """(full, summary) text for one paragraph / section header. The summary
    keeps the position, the reach and the facts; only the source goes."""
    s, e = row["start_line"], row["end_line"]
    if row["kind"] == "section" and first_para_line and first_para_line > s:
        e = first_para_line - 1               # the header and any statements before its first paragraph
    # the last paragraph's end_line may run past the expanded text, and a
    # paragraph's trailing blank lines say nothing: end at the last real line
    while e > s:
        m, ln, _d, _v = origin(conn, pid, e)
        if m and ln is not None and source_line(conn, m, ln):
            break
        e -= 1
    m1, l1, d1, v1 = origin(conn, pid, s)
    m2, l2, _d2, _v2 = origin(conn, pid, e)
    span = f"{m1}:{l1}-{l2}" if m1 == m2 else f"{m1}:{l1} .. {m2}:{l2}"
    if d1 and v1:
        span += f" (via COPY {v1})"
    ind = "  " * min(depth, 8)
    if how == "start":
        reach = "- entry"
    elif how == "entry":
        reach = "- ENTRY point"
    else:
        reach = f"<- {_HOW_LABEL.get(how, how)}" + (f" from {frm}" if frm else "") + (f" @{cite(conn, pid, via)}" if via else "")
    title = row["name"] + (" SECTION" if row["kind"] == "section" else "")
    head = f"#### {ind}{title}  {reach}  [depth {depth}]  {span}\n"
    facts = _walk_facts(conn, pid, s, e)
    fl = f"{ind}- facts: {facts}\n" if facts else ""
    first_txt = source_line(conn, m1, l1) if m1 and l1 is not None else ""
    if d1 and first_txt and not first_txt.upper().startswith(row["name"].upper()):
        fl += (f"{ind}- note: the copybook line reads `{first_txt}` - COPY REPLACING renames it to {row['name']} "
               f"in this program; cite the copybook's own text\n")
    summ = head + fl                          # the span in the header is the `cite` range when source is omitted
    if not source:
        return summ, summ
    body: List[str] = []
    cur: Optional[str] = None
    shown = 0
    for x in range(s, e + 1):
        m, ln, d, v = origin(conn, pid, x)
        if not m or ln is None:
            continue
        if m != cur:
            if cur is not None or d:
                body.append(f"------ {'from COPY ' + v + ' - ' if d and v else ''}cite as [[{m} line \"token\"]] ------\n")
            cur = m
        txt = source_line_raw(conn, m, ln)
        if not txt:
            continue                                    # a blank line: nothing to read, nothing to cite
        body.append(f"{ln:6d} | {txt}\n")
        shown += 1
        if shown >= max_lines and x < e:
            body.append(f"   ... {e - x} more lines: `cite {m} {ln + 1}-{l2}`\n")
            break
    full = head + fl + "```\n" + "".join(body) + "```\n"
    return full, summ


def _walk_data(conn: sqlite3.Connection, pid: int, mid: int, limit: int = 200) -> Tuple[str, int]:
    """The fields the PROCEDURE DIVISION names - and only those - grouped
    under their 01 with offset, length, PIC and how they are used. Program
    rows (WORKING-STORAGE, COPY ... REPLACING) cite through the line map;
    copybook rows cite the copybook. Returns (text, rows)."""
    refs = conn.execute("""SELECT UPPER(name) AS name, GROUP_CONCAT(DISTINCT mode) AS modes, MIN(line) AS first_line
                           FROM field_ref WHERE program_id=? GROUP BY UPPER(name) ORDER BY MIN(line)""", (pid,)).fetchall()
    # OPEN / READ / CLOSE name files, not fields
    files = {r[0].upper() for r in conn.execute("SELECT select_name FROM file_decl WHERE program_id=?", (pid,))}
    refs = [r for r in refs if r["name"] not in files]
    if not refs:
        return "", 0, []

    def root_of(fid: int) -> sqlite3.Row:
        r = conn.execute("SELECT * FROM field WHERE id=?", (fid,)).fetchone()
        while r and r["parent_id"]:
            r = conn.execute("SELECT * FROM field WHERE id=?", (r["parent_id"],)).fetchone()
        return r

    def def_cite(f: sqlite3.Row, member_name: str) -> str:
        return cite(conn, pid, f["line"]) if f["member_id"] == mid else f"{member_name}:{f['line']}"

    groups: Dict[int, Tuple[sqlite3.Row, str, List[Tuple[sqlite3.Row, sqlite3.Row, Optional[str]]]]] = {}
    order: List[int] = []
    unknown: List[str] = []
    for ref in refs:
        defs = conn.execute("""SELECT f.*, m.name AS member_name FROM field f JOIN member m ON m.id=f.member_id
                               WHERE f.member_id=? AND UPPER(f.name)=?""", (mid, ref["name"])).fetchall()
        if not defs:
            defs = conn.execute("""SELECT f.*, m.name AS member_name FROM copy_use c
                                   JOIN field f ON f.member_id=c.resolved_member_id JOIN member m ON m.id=f.member_id
                                   WHERE c.member_id=? AND UPPER(f.name)=?""", (mid, ref["name"])).fetchall()
        c88: List[sqlite3.Row] = []
        if not defs:
            c88 = conn.execute("""SELECT c.name AS c88_name, c.values_lit, f.*, m.name AS member_name FROM cond88 c
                                  JOIN field f ON f.id=c.field_id JOIN member m ON m.id=f.member_id
                                  WHERE UPPER(c.name)=? AND (f.member_id=? OR f.member_id IN
                                        (SELECT resolved_member_id FROM copy_use WHERE member_id=? AND resolved_member_id IS NOT NULL))""",
                               (ref["name"], mid, mid)).fetchall()
        if not defs and not c88:
            unknown.append(ref["name"])
            continue
        tagged = [(d, None) for d in defs[:3]] + \
                 [(c, f"88 {c['c88_name']} = {', '.join(_jl(c['values_lit'])) or '?'}") for c in c88[:3]]
        for d, tag in tagged:
            root = root_of(d["id"])
            if root is None:
                continue
            # a copybook FRAGMENT (05s whose 01 sits in the program) groups under the copybook itself
            gkey = -d["member_id"] if (root["level"] > 1 and d["member_id"] != mid) else root["id"]
            if gkey not in groups:
                groups[gkey] = (None if gkey < 0 else root, d["member_name"], [])
                order.append(gkey)
            groups[gkey][2].append((d, ref, tag))

    out = ["\n### Data it touches (only the fields the PROCEDURE DIVISION names; offsets and lengths are the parser's)\n"]
    total = 0
    heads: List[str] = []                     # one short label per group, for a budget-cut data section
    for rid in order:
        root, member_name, items = groups[rid]
        if root is None:
            n_all = conn.execute("SELECT COUNT(*) FROM field WHERE member_id=?", (-rid,)).fetchone()[0]
            out.append(f"\n**COPY {member_name}** (fragment - its 01 is in the program) - {len(items)} of {n_all} fields used\n")
            heads.append(f"COPY {member_name} {len(items)}/{n_all}")
        elif root["is_group"]:
            # the 01 itself named (CALL USING, MOVE, INITIALIZE) is said on its header, not counted as a field
            n_all = conn.execute("""WITH RECURSIVE sub(id) AS (SELECT id FROM field WHERE id=? UNION ALL
                                    SELECT f.id FROM field f JOIN sub ON f.parent_id=sub.id) SELECT COUNT(*)-1 FROM sub""",
                                 (rid,)).fetchone()[0]
            own = [(f, ref, tag) for f, ref, tag in items if f["id"] == rid and tag is None]
            items = [t for t in items if t not in own]
            used_as = ""
            if own:
                modes = "".join(k[0] for k in ("write", "read", "test", "display") if k in (own[0][1]["modes"] or ""))
                used_as = f", the group itself used as {modes} @{cite(conn, pid, own[0][1]['first_line'])}"
            out.append(f"\n**{root['level']:02d} {root['name']}** ({def_cite(root, member_name)}"
                       + (f", {root['length']} bytes" if root["length"] else "") + f"{used_as}) - {len(items)} of {n_all} fields used\n")
            heads.append(f"{root['level']:02d} {root['name']} {len(items)}/{n_all}")
        else:                                 # an elementary 01 / 77: the item is its own table
            out.append(f"\n**{root['level']:02d} {root['name']}** ({def_cite(root, member_name)}"
                       + (f", {root['length']} bytes" if root["length"] else "") + ")\n")
            heads.append(f"{root['level']:02d} {root['name']}")
        rows = []
        for f, ref, tag in sorted(items, key=lambda t: (t[0]["offset"] if t[0]["offset"] is not None else 10 ** 9, t[0]["line"] or 0)):
            modes = "".join(k[0] for k in ("write", "read", "test", "display") if k in (ref["modes"] or ""))
            pic = (f["pic"] or ("group" if f["is_group"] else "")) + (f" {f['usage']}" if f["usage"] and f["usage"] != "DISPLAY" else "")
            name = f"{f['level']:02d} {f['name']}" + (f" ({tag})" if tag else "")
            rows.append((name, pic, "" if f["offset"] is None else f["offset"], f["length"] or "",
                         modes, def_cite(f, member_name), cite(conn, pid, ref["first_line"])))
            total += 1
        out.append(table(["field", "PIC / usage", "offset", "len", "w/r/t/d", "defined", "first use"], rows))
        if total >= limit:
            out.append(f"_... capped at {limit} fields; `field NAME --program` for the rest_\n")
            break
    if unknown:
        out.append(f"\n- referenced but not defined in this program or its copybooks ({len(unknown)}): "
                   + ", ".join(unknown[:20]) + (" ..." if len(unknown) > 20 else "")
                   + " - LINKAGE items are here too when the caller's layout was not copied\n")
    return "".join(out), total, heads


def _walk_touches(conn: sqlite3.Connection, pid: int) -> str:
    out: List[str] = []
    files = conn.execute("SELECT select_name, assign_dd, organization FROM file_decl WHERE program_id=? ORDER BY line", (pid,)).fetchall()
    if files:
        parts = []
        for f in files:
            ops = [r[0] for r in conn.execute("SELECT DISTINCT op FROM io_op WHERE program_id=? AND target_kind='file' AND UPPER(target)=? ORDER BY op",
                                              (pid, f["select_name"].upper()))]
            parts.append(f"{f['select_name']} (DD {f['assign_dd'] or '?'}" + (f", {f['organization']}" if f["organization"] else "") + ")"
                         + (f" {'/'.join(ops)}" if ops else ""))
        out.append("- files: " + "; ".join(parts[:12]) + (" ..." if len(parts) > 12 else "") + "\n")
    tabs: Dict[str, set] = defaultdict(set)
    for r in conn.execute("SELECT stmt_type, tables FROM sql_stmt WHERE program_id=?", (pid,)):
        for t in _jl(r["tables"]):
            tabs[t].add(r["stmt_type"] or "?")
    if tabs:
        out.append("- DB2: " + "; ".join(f"{t} {'/'.join(sorted(v))}" for t, v in list(tabs.items())[:12]) + "\n")
    dbs: Dict[str, set] = defaultdict(set)
    for r in conn.execute("SELECT func, dbd_name, procopt, pcb_arg FROM dli_call WHERE program_id=?", (pid,)):
        key = (r["dbd_name"] + (f" (PROCOPT={r['procopt']})" if r["procopt"] else "")) if r["dbd_name"] else f"PCB {r['pcb_arg'] or '?'} unresolved"
        dbs[key].add(r["func"] or "?")
    if dbs:
        out.append("- IMS: " + "; ".join(f"{k} {'/'.join(sorted(v))}" for k, v in list(dbs.items())[:12]) + "\n")
    cics = conn.execute("SELECT DISTINCT verb, resource_kind, resource FROM cics_cmd WHERE program_id=? ORDER BY resource_kind, resource", (pid,)).fetchall()
    if cics:
        out.append("- CICS: " + "; ".join(f"{c['verb']} {c['resource_kind'] or ''} {c['resource'] or ''}".strip() for c in cics[:14])
                   + (" ..." if len(cics) > 14 else "") + "\n")
    calls = []
    for c in conn.execute("SELECT kind, target, via_var, resolved FROM call_edge WHERE program_id=? ORDER BY line", (pid,)):
        t = c["target"] or (", ".join(_jl(c["resolved"])) or f"{c['via_var']} (unresolved)")
        item = f"{_CALL_LABEL.get(c['kind'], c['kind'].upper())} {t}"
        if item not in calls:
            calls.append(item)
    if calls:
        out.append("- calls: " + "; ".join(calls[:14]) + (" ..." if len(calls) > 14 else "") + "\n")
    return "".join(out)


def cmd_walk(conn: sqlite3.Connection, name: str, start: Optional[str] = None, budget: Optional[int] = None,
             max_depth: Optional[int] = None, source: bool = True, data: bool = True, max_lines: int = 400) -> str:
    """The program in reading order. PERFORM returns at the end of its
    target (or THRU range), so the paragraph after a performed one is NOT
    reached by falling out of it; GO TO does not return; a performed
    SECTION runs its paragraphs in order; ENTRY points are extra roots;
    DECLARATIVES run on their USE condition and are listed, not walked."""
    progs = programs_named(conn, name)
    if not progs:
        return f"program {name} not found\n"
    p = progs[0]
    pid, mid, pname, mname = p["id"], p["member_id"], p["program_id"], p["member_name"]
    paras = conn.execute("SELECT id, name, section, kind, start_line, end_line, ordinal FROM paragraph "
                         "WHERE program_id=? ORDER BY start_line, kind='paragraph'", (pid,)).fetchall()
    if not paras:
        return (f"# Walk {pname}\n\nThe PROCEDURE DIVISION has no paragraphs or sections in the index (parse status: "
                f"{p['parse_status']}). Read it with `cite {mname} <first>-<last>`; `program {pname}` lists its facts.\n")
    by_name: Dict[str, List[sqlite3.Row]] = defaultdict(list)   # a name can live in several sections
    for r in paras:
        by_name[r["name"].upper()].append(r)
    by_id: Dict[int, sqlite3.Row] = {r["id"]: r for r in paras}
    plain = [r for r in paras if r["kind"] == "paragraph"]
    ordinal = {r["id"]: i for i, r in enumerate(plain)}
    in_section: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in plain:
        if r["section"]:
            in_section[r["section"].upper()].append(r)

    def resolve(key: Optional[str], from_section: Optional[str] = None) -> Optional[sqlite3.Row]:
        """A target as the parser wrote it: NAME, or NAME OF SECTION. A bare
        name that exists in several sections prefers the caller's own
        section, then a paragraph over a section, then the first in source."""
        if not key:
            return None
        name, _sep, qual = key.upper().partition(" OF ")
        cands = by_name.get(name.strip(), [])
        if qual:
            cands = [c for c in cands if (c["section"] or "").upper() == qual.strip()]
        if not cands:
            return None
        if len(cands) > 1 and from_section:
            same = [c for c in cands if (c["section"] or "").upper() == from_section.upper()]
            cands = same or cands
        return sorted(cands, key=lambda c: (c["kind"] != "paragraph", c["start_line"]))[0]

    edges: Dict[str, List[sqlite3.Row]] = defaultdict(list)     # keyed by the parser's from_para name
    for e in conn.execute("SELECT from_para, to_para, thru_para, line, kind FROM perform_edge WHERE program_id=? "
                          "ORDER BY line, id", (pid,)):
        edges[(e["from_para"] or "").upper()].append(e)

    # DECLARATIVES sections run on a USE condition, never from the entry
    decl_lo = decl_hi = None
    for r in conn.execute("SELECT line_no, text FROM src_fts WHERE member_name=? AND UPPER(text) LIKE '%DECLARATIVES%' "
                          "ORDER BY line_no", (mname,)):
        t = r["text"].upper()
        if re.search(r"\bEND\s+DECLARATIVES\b", t):
            decl_hi = r["line_no"]
        elif re.search(r"\bDECLARATIVES\s*\.", t) and decl_lo is None:
            decl_lo = r["line_no"]

    def in_declaratives(row: sqlite3.Row) -> bool:
        if decl_lo is None or decl_hi is None:
            return False
        m, ln, depth, _v = origin(conn, pid, row["start_line"])
        return m == mname and not depth and ln is not None and decl_lo <= ln <= decl_hi

    if start:
        entry = resolve(start)
        if entry is None:
            return f"# Walk {pname}\n\n**NOT FOUND** - no paragraph or section named {start.upper()} in {pname}.\n"
    else:
        entry = next((r for r in paras if not in_declaratives(r)), paras[0])

    def thru_range(a: sqlite3.Row, b: sqlite3.Row) -> List[sqlite3.Row]:
        ia, ib = ordinal.get(a["id"]), ordinal.get(b["id"])
        if ia is not None and ib is not None and ia < ib:
            return plain[ia + 1: ib + 1]
        return []

    def first_para_line(sec: sqlite3.Row) -> Optional[int]:
        ps = in_section.get(sec["name"].upper(), [])
        return ps[0]["start_line"] if ps else None

    visited: Dict[int, int] = {}                          # paragraph id -> order shown
    blocks: List[Tuple[str, str, int]] = []               # (full, summary, depth)
    repeats: Dict[Tuple[str, str], int] = defaultdict(int)
    noted: Dict[int, int] = defaultdict(int)
    missing: Dict[Tuple[str, str], List[str]] = defaultdict(list)

    # a frame: (target key, how, depth, from name, via line, stop paragraph id, caller's section, explicit row id)
    def children_of(row: sqlite3.Row, depth: int, stop: Optional[int], how: str) -> List[tuple]:
        out: List[tuple] = []
        key = row["name"].upper()
        from_sec = row["name"] if row["kind"] == "section" else row["section"]
        if row["kind"] == "section":
            secp = in_section.get(key, [])
            # a PERFORMed section returns after its last paragraph; a section
            # entered by the entry, a GO TO or fall-through keeps flowing
            last = secp[-1]["id"] if (secp and how in ("perform", "sort_proc", "range")) else stop
            out += [(q["name"].upper(), "section", depth + 1, row["name"], None, last, from_sec, q["id"]) for q in secp]
        for e in edges.get(key, []):
            kind, tgt = e["kind"], e["to_para"].upper()
            thru = (e["thru_para"] or "").upper() or None
            if kind == "fallthrough":
                if stop is not None and row["id"] == stop:
                    continue                              # the PERFORM returns here
                out.append((tgt, "fallthrough", depth, row["name"], e["line"], stop, from_sec, None))
            elif kind in ("perform", "sort_proc"):
                t_row = resolve(tgt, from_sec)
                th_row = resolve(thru, from_sec) if thru else None
                end = th_row or t_row
                out.append((tgt, kind, depth + 1, row["name"], e["line"], end["id"] if end else None, from_sec, None))
                if t_row and th_row:
                    out += [(q["name"].upper(), "range", depth + 1, row["name"], e["line"], end["id"], from_sec, q["id"])
                            for q in thru_range(t_row, th_row)]
            else:                                         # goto / goto_depending / alter: no return, same return point
                out.append((tgt, kind, depth + 1, row["name"], e["line"], stop, from_sec, None))
        return out

    def run(root: sqlite3.Row, how0: str) -> None:
        stack: List[tuple] = [(root["name"].upper(), how0, 0, None, None, None, None, root["id"])]
        while stack:
            key, how, depth, frm, via, stop, from_sec, rid = stack.pop()
            row = by_id.get(rid) if rid is not None else resolve(key, from_sec)
            if row is None:
                missing[(key, how)].append(f"{frm or '?'} @{cite(conn, pid, via)}" if via else (frm or "?"))
                continue
            if row["id"] in visited:
                if how in _NOTED_REACH:
                    repeats[(row["name"], how)] += 1
                    if noted[row["id"]] < 2:
                        noted[row["id"]] += 1
                        line = (f"{'  ' * min(depth, 8)}> {row['name']} <- {_HOW_LABEL[how]}" + (f" from {frm}" if frm else "")
                                + (f" @{cite(conn, pid, via)}" if via else "") + " - shown above\n")
                        blocks.append((line, line, depth))
                continue
            visited[row["id"]] = len(visited) + 1
            full, summ = _walk_block(conn, pid, pname, row, how, depth, frm, via,
                                     first_para_line(row) if row["kind"] == "section" else None, source, max_lines)
            blocks.append((full, summ, depth))
            for c in reversed(children_of(row, depth, stop, how)):
                stack.append(c)

    run(entry, "start")
    for a in conn.execute("SELECT alias, line FROM program_alias WHERE program_id=? ORDER BY line", (pid,)):
        r = conn.execute("""SELECT id FROM paragraph WHERE program_id=? AND kind='paragraph' AND ? BETWEEN start_line AND end_line
                            ORDER BY start_line DESC LIMIT 1""", (pid, a["line"])).fetchone()
        if r and r["id"] not in visited:
            note = f"\n### Entry point `{a['alias']}` (ENTRY @{cite(conn, pid, a['line'])}) - callers of that name start here\n"
            blocks.append((note, note, 0))
            run(by_id[r["id"]], "entry")

    # ---- assemble, then fit the budget by dropping SOURCE from the end (order and facts stay)
    # `DECLARATIVES.` itself parses like a paragraph header; it is a keyword, not code
    never = [r for r in paras if r["id"] not in visited and r["name"].upper() != "DECLARATIVES"
             and not (r["kind"] == "section" and any(q["id"] in visited for q in in_section.get(r["name"].upper(), [])))]
    secs = [r for r in paras if r["kind"] == "section"]
    names = _names_of(conn, pname)
    ph = ",".join("?" for _ in names)
    runs = conn.execute(f"""SELECT DISTINCT j.job_name || '.' || COALESCE(s.step_name, '?') AS r FROM step s
                            JOIN job j ON j.id=s.job_id WHERE UPPER(s.effective_pgm) IN ({ph}) ORDER BY 1 LIMIT 9""",
                        [n.upper() for n in names]).fetchall()
    trans = conn.execute(f"SELECT DISTINCT tran_code || ' (' || COALESCE(system, '?') || ')' AS t FROM transaction_def "
                         f"WHERE UPPER(program) IN ({ph}) ORDER BY 1 LIMIT 9", [n.upper() for n in names]).fetchall()
    head = [f"# Walk {pname}  (member {mname}, {len(plain)} paragraphs"
            + (f" in {len(secs)} section{'s' if len(secs) != 1 else ''}" if secs else "")
            + f"; entry {entry['name']}; {len(visited)} reached, {len(never)} not reached)\n"]
    if runs or trans:
        head.append("- runs in: " + ", ".join([r["r"] for r in runs] + [t["t"] for t in trans]) + "\n")
    else:
        head.append("- runs in: no job step or transaction of the index names it (callers: `callers " + pname + "`)\n")
    head.append("- order: the entry first, then each paragraph the first time control reaches it - PERFORM (returns at the end "
                "of its target or THRU range), GO TO (no return), fall-through, THRU range, performed SECTION. `>` marks a "
                "reach of a paragraph already shown. [depth] is PERFORM nesting. Lines are ORIGINAL member lines: cite as "
                "`[[MEMBER line \"token\"]]`.\n")
    touch = _walk_touches(conn, pid)
    if touch:
        head.append("\n### Touches\n" + touch)
    data_txt, data_rows, data_heads = _walk_data(conn, pid, mid) if data else ("", 0, [])
    tail: List[str] = []
    if repeats:
        tail.append("\n### Repeated reaches (already shown above)\n")
        tail.append(table(["paragraph", "how", "times"], [(k[0], _HOW_LABEL[k[1]], v) for k, v in sorted(repeats.items(), key=lambda kv: -kv[1])[:40]]))
    if never:
        tail.append(f"\n### Not reached from the entry ({len(never)})\n")
        rows = []
        for r in never:
            if in_declaratives(r):
                why = "DECLARATIVES - runs on its USE condition, not from the entry"
            elif r["kind"] == "section":
                why = "section: never PERFORMed and never entered"
            else:
                why = "no PERFORM, GO TO, fall-through, THRU range or performed SECTION reaches it"
            rows.append((r["name"], r["kind"], cite(conn, pid, r["start_line"]), why))
        tail.append(table(["paragraph", "kind", "starts", "why"], rows[:80]))
        if len(rows) > 80:
            tail.append(f"_... {len(rows) - 80} more_\n")
    if missing:
        tail.append("\n### PERFORM / GO TO targets not found in this program\n")
        tail.append(table(["target", "how", "from"], [(k[0], _HOW_LABEL.get(k[1], k[1]), ", ".join(v[:5])) for k, v in missing.items()]))
    tail.append(unresolved_for(conn, [mid]))

    chosen: List[str] = []
    for full, summ, d in blocks:
        chosen.append(summ if (max_depth is not None and d > max_depth) else full)
    ih = index_header(conn)
    # the leading comment (token count, budget note, index header) is part of the budget too
    fixed = sum(len(x) for x in head) + sum(len(x) for x in tail) + len("\n## Walk\n\n") + len(ih) + 260
    total = fixed + len(data_txt) + sum(len(c) for c in chosen)
    omitted = 0
    note = ""
    if budget and total > budget:
        for i in range(len(blocks) - 1, -1, -1):
            if total <= budget:
                break
            full, summ, _d = blocks[i]
            if chosen[i] is full and full is not summ:
                total -= len(full) - len(summ)
                chosen[i] = summ
                omitted += 1
        note = (f", budget {budget}: source omitted for {omitted} of {len(visited)} paragraphs (from the end; the first "
                f"reached keep theirs; each header's span is the `cite` range)")
        if total > budget and data_txt:
            # sources gone and still over: keep the data section's shape, drop its tables
            keep = (f"\n### Data it touches - cut for budget ({data_rows} fields named; `field NAME --program {pname}` "
                    f"for offsets): " + "; ".join(data_heads) + "\n")
            total -= len(data_txt) - len(keep)
            data_txt = keep
            note += "; data tables cut"
        if total > budget:
            note += f"; still ~{total - budget} chars over - the skeleton alone exceeds the budget"
    text = "".join(head) + data_txt + "\n## Walk\n\n" + "".join(chosen) + "".join(tail)
    est = len(text) // 4
    return f"<!-- walk {pname}: ~{est} tokens ({len(text)} chars){note}; {index_header(conn)} -->\n" + text


def _dead_extras(conn: sqlite3.Connection) -> str:
    """Decommissioning candidates beyond programs: datasets written but never
    read (or read but never written) IN THE INDEX, copybooks nobody COPYs,
    jobs absent from the scheduler when one is loaded."""
    out = []
    rows = conn.execute("""SELECT dsn,
                             SUM(CASE WHEN mode IN ('output','mod','both','create') THEN 1 ELSE 0 END) AS w,
                             SUM(CASE WHEN mode IN ('input','both') THEN 1 ELSE 0 END) AS r
                           FROM v_dataset_flow WHERE job_name IS NOT NULL AND is_temp=0 AND dsn NOT LIKE '&&%'
                           GROUP BY dsn""").fetchall()
    wnr = [r["dsn"] for r in rows if r["w"] and not r["r"]]
    rnw = [r["dsn"] for r in rows if r["r"] and not r["w"]]
    out.append(f"\n### Datasets written but never read in the index ({len(wnr)})\n"
               "Caveat: online CICS readers, unindexed departments, FTP/NDM and reports are not counted.\n")
    out.append(table(["dataset"], [(d,) for d in wnr[:100]]))
    out.append(f"\n### Datasets read but never written in the index ({len(rnw)}) - they come from somewhere not indexed\n")
    out.append(table(["dataset"], [(d,) for d in rnw[:100]]))
    cps = conn.execute("""SELECT m.name, m.path FROM member m WHERE m.kind='copybook'
                          AND NOT EXISTS (SELECT 1 FROM copy_use c WHERE UPPER(c.copybook)=UPPER(m.name))
                          ORDER BY m.name""").fetchall()
    out.append(f"\n### Copybooks no indexed program COPYs ({len(cps)})\n")
    out.append(table(["copybook", "path"], [(c["name"], os.path.basename(c["path"])) for c in cps[:100]]))
    if conn.execute("SELECT COUNT(*) FROM sched_job").fetchone()[0]:
        jobs = conn.execute("""SELECT j.job_name FROM job j WHERE NOT EXISTS
                               (SELECT 1 FROM sched_job s WHERE UPPER(s.job_name)=UPPER(j.job_name))
                               AND NOT EXISTS (SELECT 1 FROM sched_dep d WHERE UPPER(d.job_name)=UPPER(j.job_name))
                               ORDER BY j.job_name""").fetchall()
        out.append(f"\n### Jobs not in the scheduler export ({len(jobs)}) - on-request, obsolete, or a different scheduler\n")
        out.append(table(["job"], [(j["job_name"],) for j in jobs[:100]]))
    return "".join(out)


def _coverage_extras(conn: sqlite3.Connection) -> str:
    """What contributes FACTS and what is only searchable text - stated, so
    that 'ctlcard skipped 3100' reads as the gap it is."""
    from . import build as _build          # local import: build imports nothing from query
    out = ["\n### What contributes facts\n"]
    kinds = conn.execute("SELECT kind, COUNT(*) AS n FROM member GROUP BY kind ORDER BY kind").fetchall()
    lost = {"ctlcard": "cards are read only when a job's DD names the member",
            "asm": "no program rows, calls or DSECT layouts", "rexx": "no calls / SUBMITs / ALLOCs",
            "sql": "DDL not parsed (columns, views, triggers unknown)", "unknown": "nothing - misfiled library?",
            "doc": "prose only, never facts", "listing": "not parsed (offsets unknown)", "other": "nothing",
            "empty": "no code lines"}
    out.append(table(["kind", "members", "handler", "what is lost without one"],
                     [(k["kind"], k["n"], "yes" if k["kind"] in _build.HANDLERS else "NO",
                       "" if k["kind"] in _build.HANDLERS else lost.get(k["kind"], "not parsed")) for k in kinds]))
    unk = conn.execute("""SELECT library, ext, COUNT(*) AS n FROM member WHERE kind IN ('unknown','other')
                          GROUP BY library, ext ORDER BY n DESC LIMIT 20""").fetchall()
    if unk:
        out.append("\n### Unrecognised members by folder and extension (misfiled libraries show up here)\n")
        out.append(table(["folder", "ext", "members"], [(u["library"], u["ext"] or "(none)", u["n"]) for u in unk]))
    checks = [
        ("scheduler export", conn.execute("SELECT COUNT(*) FROM sched_job").fetchone()[0]),
        ("CICS CSD", conn.execute("SELECT COUNT(*) FROM transaction_def WHERE system='cics'").fetchone()[0]),
        ("IMS stage-1", conn.execute("SELECT COUNT(*) FROM transaction_def WHERE system='ims_dc'").fetchone()[0]),
        ("PSB source", conn.execute("SELECT COUNT(*) FROM ims_psb").fetchone()[0]),
        ("DBD source", conn.execute("SELECT COUNT(*) FROM ims_dbd").fetchone()[0]),
        ("DCLGEN / DDL columns", conn.execute("SELECT COUNT(*) FROM db2_column").fetchone()[0]),
        ("BMS / MFS screens", conn.execute("SELECT COUNT(*) FROM screen").fetchone()[0]),
        ("control-card members", conn.execute("SELECT COUNT(*) FROM member WHERE kind='ctlcard'").fetchone()[0]),
        ("documents", conn.execute("SELECT COUNT(*) FROM member WHERE kind='doc'").fetchone()[0]),
    ]
    out.append("\n### Optional inputs\n")
    out.append(table(["input", "loaded"], [(n, str(v) if v else "NO") for n, v in checks]))
    miss = conn.execute("""SELECT card_member, COUNT(*) AS n FROM dd WHERE card_member IS NOT NULL AND sysin_text IS NULL
                           GROUP BY card_member ORDER BY n DESC LIMIT 30""").fetchall()
    if miss:
        out.append(f"\n### Control-card members referenced by JCL but NOT indexed ({len(miss)} shown)\n"
                   "Fetch the PARMLIB/CNTL library: until then these steps' cards, programs and sort fields are unknown.\n")
        out.append(table(["member", "DDs"], [(m["card_member"], m["n"]) for m in miss]))
    unres = conn.execute("SELECT COUNT(*) FROM unresolved WHERE kind='dli_function'").fetchone()[0]
    return "".join(out)


# --------------------------------------------------------------------------
# interfaces - the mainframe / non-mainframe boundary
# --------------------------------------------------------------------------

def _interface_rows(conn: sqlite3.Connection, system: Optional[str] = None,
                    dsn: Optional[str] = None) -> List[Tuple]:
    """(kind, direction, peer, what, where, system, cite) from every source:
    FTP/NDM/BPXBATCH steps, MQ calls, IMS message switches, CICS TD queues,
    URIMAP web entry points, REMOTESYSTEM transactions, and the manifest's
    declared external_interfaces."""
    rows: List[Tuple] = []
    q = """SELECT i.kind, i.detail, i.direction, i.line, m.name AS mem, m.kind AS mkind, m.system,
                  (SELECT program_id FROM program p WHERE p.member_id=m.id LIMIT 1) AS pgm,
                  (SELECT job_name FROM job j WHERE j.member_id=m.id LIMIT 1) AS job
           FROM interface_edge i JOIN member m ON m.id=i.member_id"""
    for r in conn.execute(q):
        if system and (r["system"] or "").upper() != system.upper():
            continue
        where = r["pgm"] or r["job"] or r["mem"]
        rows.append((r["kind"], r["direction"] or "?", "", r["detail"][:90], where, r["system"] or "?",
                     f"{r['mem']}:{r['line']}"))
    for r in conn.execute("""SELECT d.dd_name, d.dsn_resolved, d.mode, d.mode_source, d.line, s.step_name, s.from_proc,
                                    j.job_name, m.name AS mem, m.system
                             FROM dd d JOIN step s ON s.id=d.step_id JOIN job j ON j.id=s.job_id JOIN member m ON m.id=j.member_id
                             WHERE d.dd_name IN ('*FTP*','*NDM*') OR d.mode_source IN ('pathopts')"""):
        if system and (r["system"] or "").upper() != system.upper():
            continue
        if dsn and dsn.upper() not in (r["dsn_resolved"] or "").upper():
            continue
        kind = {"*FTP*": "ftp", "*NDM*": "ndm"}.get(r["dd_name"], "uss")
        direction = "out" if r["mode"] == "input" and kind != "uss" else "in" if r["mode"] == "output" and kind != "uss" else r["mode"]
        rows.append((kind, direction, "", r["dsn_resolved"], f"{r['job_name']} {r['step_name']}", r["system"] or "?",
                     f"{r['from_proc'] or r['mem']}:{r['line']}"))
    for r in conn.execute("SELECT tran_code, program, detail, member_id, line FROM transaction_def WHERE system='cics_web' "
                          "OR detail LIKE 'REMOTESYSTEM%'"):
        rows.append(("web" if r["detail"] and r["detail"].startswith("URIMAP") else "remote-region", "in", "",
                     f"{r['tran_code']} -> {r['program'] or '?'} ({(r['detail'] or '')[:40]})", r["program"] or "", "?",
                     f"CSD:{r['line']}"))
    for r in conn.execute("SELECT kind, peer, direction, target_kind, target, note FROM external_interface"):
        if dsn and r["target_kind"] == "dataset" and dsn.upper() not in r["target"].upper():
            continue
        rows.append((r["kind"], r["direction"] or "?", r["peer"] or "?", f"{r['target_kind']} {r['target']}",
                     "(declared in the manifest)", "-", r["note"] or "manifest"))
    if dsn:
        rows = [x for x in rows if dsn.upper() in x[3].upper() or dsn.upper() in x[4].upper()]
    return rows


def cmd_interfaces(conn: sqlite3.Connection, system: Optional[str] = None, dsn: Optional[str] = None) -> str:
    """What leaves and enters the mainframe: files sent by FTP / Connect:Direct,
    MQ queues with their message layout, IMS message switches, CICS TD queues
    and web entry points, plus the peers declared in the manifest."""
    rows = _interface_rows(conn, system, dsn)
    out = [f"# Interfaces" + (f" of {system.upper()}" if system else "") + (f" touching {dsn.upper()}" if dsn else "") + "\n"]
    if not rows:
        out.append("\n_none found_ - no FTP/NDM/MQ/TDQ/web facts in scope and no `external_interfaces` in the manifest.\n")
    else:
        out.append(table(["kind", "dir", "peer", "what", "where", "system", "cite"], sorted(rows)))
    out.append("\n> A peer system that no source names (the reinsurer, the warehouse, the portal) comes only from the "
               "manifest's `external_interfaces`; MQ queue names come from the MQOD literal; a `(queue not "
               "resolvable)` row means the name is set at run time. Message layouts: `layout <01-NAME>`.\n")
    return "".join(out)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Query atlas.db (markdown out, citations in).")
    ap.add_argument("--db", default="atlas.db")
    ap.add_argument("--out", help="write the report to this file (UTF-8, LF, folders created) instead of "
                                  "the terminal - use this rather than the shell's '>' (PowerShell 5.1 "
                                  "writes UTF-16)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("program", "job", "dataset", "copybook", "values", "screen", "transaction", "column",
              "table", "dbd", "segment"):
        sub.add_parser(c).add_argument("name")
    s = sub.add_parser("field")
    s.add_argument("name")
    s.add_argument("--all", action="store_true", help="every reference site (default: one cite per program/statement)")
    s.add_argument("--program", help="only this program's references")
    s = sub.add_parser("layout")
    s.add_argument("name")
    s.add_argument("--program", help="the 01 as this program sees it (REPLACING applied)")
    s = sub.add_parser("crud")
    s.add_argument("programs", nargs="*")
    s.add_argument("--job", help="every program the job runs")
    s.add_argument("--copybook", help="every program including the copybook")
    s.add_argument("--system", help="every program of a department")
    sub.add_parser("conditions").add_argument("name")
    s = sub.add_parser("interfaces")
    s.add_argument("--system", help="one department")
    s.add_argument("--dsn", help="interfaces touching this dataset")
    s = sub.add_parser("paragraph")
    s.add_argument("name")
    s.add_argument("para", help="paragraph / section name, or a line number")
    s = sub.add_parser("doc", help="a document's outline, or the full text of its sections by number / by content")
    s.add_argument("name")
    s.add_argument("--sections", help="which sections to print in full: 3, 3-5, or 2,7,9-12")
    s.add_argument("--grep", help="print every section containing this text (case-insensitive)")
    s.add_argument("--budget", type=int, help="character budget for the printed sections")
    s = sub.add_parser("diff", help="two versions of a member (production vs changed): changed facts, then the lines "
                                    "of both sides; with no member, every member whose copies differ (the release)")
    s.add_argument("old", nargs="?", help="SYSTEM/NAME, NAME@LIBRARY, NAME(kind), NAME, or a path - the production copy")
    s.add_argument("new", nargs="?", help="the changed copy; omitted = the other copy of the same name in the index")
    s.add_argument("--system", help="the system folder holding the changed copies (e.g. GC-TEST)")
    s.add_argument("--context", type=int, default=3, help="unchanged lines shown around each change")
    s.add_argument("--budget", type=int, help="character budget for the lines")
    s = sub.add_parser("walk", help="the program in reading order: entry first, each paragraph as control reaches it")
    s.add_argument("name")
    s.add_argument("--from", dest="start", help="start at this paragraph / section instead of the entry")
    s.add_argument("--budget", type=int, help="character budget (about 4 per token): later paragraphs keep their "
                                              "position and facts, lose their source")
    s.add_argument("--depth", type=int, help="print source only down to this PERFORM depth; deeper ones are summarised")
    s.add_argument("--no-source", action="store_true", help="skeleton only: order, reach, facts")
    s.add_argument("--no-data", action="store_true", help="skip the data section")
    s.add_argument("--max-lines", type=int, default=400, help="source lines per paragraph before truncation")
    s = sub.add_parser("literal")
    s.add_argument("name")
    s.add_argument("--field", help="only uses on this field (common values like 3 or 'M' appear everywhere)")
    s.add_argument("--like", action="store_true", help="substring match instead of exact")
    s = sub.add_parser("pair")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--window", type=int, default=8)
    sub.add_parser("messages").add_argument("pattern")
    sub.add_parser("docs").add_argument("term")
    sub.add_parser("images").add_argument("name", nargs="?")
    for c in ("callers", "callees"):
        s = sub.add_parser(c)
        s.add_argument("name")
        s.add_argument("--depth", type=int, default=2)
        s.add_argument("--args", action="store_true", help="every call site with its USING list vs the callee's LINKAGE")
    sub.add_parser("dead")
    sub.add_parser("ambiguous")
    sub.add_parser("coverage")
    s = sub.add_parser("search")
    s.add_argument("term")
    s.add_argument("--kind")
    s.add_argument("--limit", type=int, default=50)
    s = sub.add_parser("cite")
    s.add_argument("member")
    s.add_argument("range", help="e.g. 120 or 120-135")
    s.add_argument("--program", help="treat lines as EXPANDED lines of this program and translate")
    s.add_argument("--kind", help="cobol|copybook|jcl|proc|psb|... when several members share the name")
    s = sub.add_parser("pack")
    s.add_argument("name")
    s.add_argument("--max-lines", type=int, default=120)
    s.add_argument("--kind", help="program|job|copybook|transaction|field (default: detected)")
    s.add_argument("--budget", type=int, help="maximum characters (about 4 per token)")
    s.add_argument("--sections", help="comma-separated section names to keep, e.g. runs,calls,files,evidence")
    s.add_argument("--keep-paths", action="store_true", help="keep full file paths (default: file names only)")
    a = ap.parse_args(argv)
    if a.out:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _run(a)
        write_out(a.out, buf.getvalue())
        return rc
    return _run(a)


def write_out(path: str, text: str) -> None:
    """Write a report as UTF-8 with LF, creating the folder. The shell's own
    redirection is not used because its encoding is the shell's choice
    (PowerShell 5.1 `>` produces UTF-16), which the chat model and the gate
    then misread."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"written {path}  ({len(text)} chars, ~{len(text) // 4} tokens)")


def _run(a: argparse.Namespace) -> int:
    conn = connect(a.db)
    try:
        if a.cmd == "program":
            print(cmd_program(conn, a.name))
        elif a.cmd == "job":
            print(cmd_job(conn, a.name))
        elif a.cmd in ("callers", "callees"):
            print(cmd_graph(conn, a.name, a.cmd, a.depth, a.args))
        elif a.cmd == "field":
            print(cmd_field(conn, a.name, a.all, a.program))
        elif a.cmd == "crud":
            print(cmd_crud(conn, a.programs, a.job, a.copybook, a.system))
        elif a.cmd == "conditions":
            print(cmd_conditions(conn, a.name))
        elif a.cmd == "interfaces":
            print(cmd_interfaces(conn, a.system, a.dsn))
        elif a.cmd == "paragraph":
            print(cmd_paragraph(conn, a.name, a.para))
        elif a.cmd == "walk":
            print(cmd_walk(conn, a.name, a.start, a.budget, a.depth, not a.no_source, not a.no_data, a.max_lines))
        elif a.cmd == "doc":
            print(cmd_doc(conn, a.name, a.sections, a.grep, a.budget))
        elif a.cmd == "diff":
            print(cmd_diff(conn, a.old, a.new, a.context, a.budget, a.system))
        elif a.cmd == "literal":
            print(cmd_literal(conn, a.name, a.field, a.like))
        elif a.cmd == "values":
            print(cmd_values(conn, a.name))
        elif a.cmd == "pair":
            print(cmd_pair(conn, a.a, a.b, a.window))
        elif a.cmd == "messages":
            print(cmd_messages(conn, a.pattern))
        elif a.cmd == "screen":
            print(cmd_screen(conn, a.name))
        elif a.cmd == "transaction":
            print(cmd_transaction(conn, a.name))
        elif a.cmd == "column":
            print(cmd_column(conn, a.name))
        elif a.cmd == "table":
            print(cmd_table(conn, a.name))
        elif a.cmd == "dbd":
            print(cmd_dbd(conn, a.name))
        elif a.cmd == "segment":
            print(cmd_segment(conn, a.name))
        elif a.cmd == "layout":
            print(cmd_layout(conn, a.name, a.program))
        elif a.cmd == "docs":
            print(cmd_docs(conn, a.term))
        elif a.cmd == "images":
            print(cmd_images(conn, a.name))
        elif a.cmd == "dataset":
            print(cmd_dataset(conn, a.name))
        elif a.cmd == "copybook":
            print(cmd_copybook(conn, a.name))
        elif a.cmd == "dead":
            print(cmd_dead(conn))
        elif a.cmd == "ambiguous":
            print(cmd_ambiguous(conn))
        elif a.cmd == "coverage":
            print(cmd_coverage(conn))
        elif a.cmd == "search":
            print(cmd_search(conn, a.term, a.kind, a.limit))
        elif a.cmd == "cite":
            print(cmd_cite(conn, a.member, a.range, a.program, a.kind))
        elif a.cmd == "pack":
            print(cmd_pack(conn, a.name, a.max_lines, a.kind, a.budget, a.sections, a.keep_paths))
    finally:
        conn.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Any crash becomes a short, redacted atlas-crash.txt you can share."""
    try:
        return _main(argv)
    except Exception as e:                                              # noqa: BLE001
        from .diag import write_crash
        text = write_crash(e, sys.argv)
        print("\n" + text, file=sys.stderr)
        print("\nSaved to atlas-crash.txt - paste it to get a fix.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
