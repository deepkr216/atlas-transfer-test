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


def programs_named(conn: sqlite3.Connection, name: str) -> List[sqlite3.Row]:
    return conn.execute("""
        SELECT p.*, m.name AS member_name, m.path, m.library, m.authoritative, m.parse_status, m.system
        FROM program p JOIN member m ON m.id = p.member_id
        WHERE UPPER(p.program_id) = ? OR UPPER(m.name) = ?
        ORDER BY m.authoritative DESC, m.path""", (name.upper(), name.upper())).fetchall()


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
        if not steps and not callers and not tx:
            return f"# {name}\n\n**NOT FOUND** - no member with this name, and nothing indexed runs or calls it.\n"
        out = [f"# Program {n}\n\n**Source not indexed** (no member with this PROGRAM-ID or name). "
               f"What the estate knows about it:\n"]
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
            for s in sqls:
                for t in _jl(s["tables"]):
                    agg[t].add(s["stmt_type"])
            out.append("\n### DB2\n")
            out.append(table(["table", "verbs"], [(t, ", ".join(sorted(v))) for t, v in sorted(agg.items())]))
            dyn = [s for s in sqls if s["is_dynamic"]]
            if dyn:
                out.append(f"**{len(dyn)} dynamic SQL statement(s)** - tables not statically knowable.\n")

        # IMS
        dli = conn.execute("SELECT func, pcb_arg, line FROM dli_call WHERE program_id=?", (pid,)).fetchall()
        if dli:
            out.append("\n### IMS DL/I calls\n")
            out.append(table(["func", "PCB arg (positional!)", "cite"],
                             [(d["func"], d["pcb_arg"], cite(conn, pid, d["line"])) for d in dli]))
            psbs = {s["parm"] for s in steps if s["parm"] and "PSB " in s["parm"]}
            if psbs:
                out.append("PSB from JCL: " + "; ".join(sorted(psbs))[:200] + "\n")
            out.append("> Map PCB arguments through the PSB's PCB order (`ims_pcb.ordinal`), remembering "
                       "the I/O PCB comes first in BMP/MPP or CMPAT=YES.\n")

        # interfaces
        ifs = conn.execute("SELECT kind, detail, direction, line FROM interface_edge WHERE member_id=?",
                           (p["member_id"],)).fetchall()
        if ifs:
            out.append("\n### External interfaces\n")
            out.append(table(["kind", "detail", "dir", "line"],
                             [(i["kind"], i["detail"], i["direction"], i["line"]) for i in ifs]))

        # paragraphs
        paras = conn.execute("SELECT name, start_line, end_line FROM paragraph WHERE program_id=? ORDER BY ordinal",
                             (pid,)).fetchall()
        performed = {r[0] for r in conn.execute("SELECT to_para FROM perform_edge WHERE program_id=?", (pid,))}
        thru = conn.execute("SELECT to_para, thru_para FROM perform_edge WHERE program_id=? AND thru_para IS NOT NULL",
                            (pid,)).fetchall()
        names = [q["name"] for q in paras]
        for a, b in thru:
            if a in names and b in names:
                lo, hi = sorted((names.index(a), names.index(b)))
                performed.update(names[lo:hi + 1])
        never = [q["name"] for q in paras[1:] if q["name"] not in performed]
        out.append(f"\n### Structure\n- {len(paras)} paragraphs; "
                   f"{len(never)} never PERFORMed (fall-through / GO TO not evaluated): "
                   f"{', '.join(never[:12])}{' ...' if len(never) > 12 else ''}\n")

        # literals set here
        lits = conn.execute("""
            SELECT literal, field, COUNT(*) n FROM literal_ref
            WHERE program_id=? AND context='move_to' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15""", (pid,)).fetchall()
        if lits:
            out.append("- codes/literals set here: " + ", ".join(f"'{l['literal']}'->{l['field']}" for l in lits) + "\n")

        out.append(unresolved_for(conn, [p["member_id"]]))
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
    if not jobs and not procs:
        return f"# {name}\n\n**NOT FOUND** - no job or PROC with this name is indexed.\n"
    out = [f"# Job {name.upper()}\n"]
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
        if s["parm"]:
            out.append(f"- PARM/notes: `{s['parm'][:200]}`\n")
        dds = conn.execute("SELECT * FROM dd WHERE step_id=? ORDER BY line, id", (s["id"],)).fetchall()
        drows = []
        for d in dds:
            if d["dsn_resolved"]:
                g = f"({d['gdg_rel']})" if d["gdg_rel"] else ""
                ovr = " (override)" if d["is_override"] else ""
                drows.append((d["dd_name"] or "  +concat", f"{d['dsn_resolved']}{g}{ovr}", d["disp"] or "",
                              f"{d['mode']} [{d['mode_source']}]"))
            elif d["sysin_text"]:
                first = d["sysin_text"].strip().splitlines()
                drows.append((d["dd_name"], f"inline cards ({len(first)} lines): "
                              + " / ".join(x.strip() for x in first[:3])[:90], "", "control"))
            elif d["dsn"]:
                drows.append((d["dd_name"], f"{d['dsn']} (unresolved)", d["disp"] or "", "unknown"))
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
        render_steps("job_id=? AND from_proc IS NULL", j["id"], j["member_name"], j["id"])
        mids.append(j["member_id"])
    for p in procs:
        out.append(f"\n## PROC {p['proc_name']}  `{p['path']}`  symbolics: `{p['symbolics'] or '{}'}`\n")
        render_steps("proc_id=?", p["id"], p["member_name"])
        mids.append(p["member_id"])
    out.append(unresolved_for(conn, mids))
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
    rows = conn.execute("""
        SELECT DISTINCT p.program_id, c.kind FROM call_edge c JOIN program p ON p.id=c.program_id
        WHERE UPPER(c.target)=? OR c.resolved LIKE ?""", (name.upper(), f'%"{name.upper()}"%')).fetchall()
    return [(r["program_id"], r["kind"]) for r in rows]


def cmd_graph(conn: sqlite3.Connection, name: str, direction: str, depth: int) -> str:
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
            rows.append((d + 1, cur, tgt, kind))
            if tgt not in seen:
                seen.add(tgt)
                if not programs_named(conn, tgt):
                    missing.append(tgt)
                q.append((tgt, d + 1))
    out.append(table(["depth", "from", "to", "kind"] if direction == "callees" else ["depth", "callee", "caller", "kind"], rows))
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


def cmd_field(conn: sqlite3.Connection, name: str) -> str:
    defs = _field_defs(conn, name)
    out = [f"# Field {name.upper()}\n"]
    if not defs:
        out.append("**NOT DEFINED** in any indexed copybook or program (check spelling, REPLACING renames, "
                   "or an 88-level name - try `literal`).\n")
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

    refs = conn.execute("""SELECT p.program_id, p.id AS pid, r.mode, r.stmt, r.line FROM field_ref r JOIN program p ON p.id=r.program_id
                           WHERE UPPER(r.name)=? ORDER BY r.mode, p.program_id, r.line""", (name.upper(),)).fetchall()
    out.append("\n### References by mode\n")
    by_mode: Dict[str, List[str]] = defaultdict(list)
    for r in refs:
        by_mode[r["mode"]].append(f"{r['program_id']} {r['stmt']} @{cite(conn, r['pid'], r['line'])}")
    for mode in ("write", "display", "test", "read"):
        if by_mode.get(mode):
            out.append(f"- **{mode}** ({len(by_mode[mode])}): " + "; ".join(by_mode[mode][:12])
                       + (" ..." if len(by_mode[mode]) > 12 else "") + "\n")
    lits = conn.execute("""SELECT l.literal, l.context, p.program_id, l.line, p.id AS pid FROM literal_ref l
                           LEFT JOIN program p ON p.id=l.program_id WHERE UPPER(l.field)=? OR UPPER(l.field) LIKE ?
                           ORDER BY l.context, l.literal""", (name.upper(), f"{name.upper()}/%")).fetchall()
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
        out.append(table(["literal", "how", "program", "cite"], rows[:60]))
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

    hdr = ["literal", "how", "where", "field / column", "cite"]
    out.append("\n### Defined (VALUE / 88-level)\n")
    out.append(table(hdr, rows_for(["value", "cond88"])))
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
    out.append(table(["dataset", "direction [source]", "system", "program", "job", "step", "gdg", "member"],
                     [(r["dsn"], f"{r['mode']} [{r['mode_source'] or ''}]".replace(" []", ""),
                       r["system"] or "?", r["pgm"], r["job_name"], r["step_name"], r["gdg_rel"] or "",
                       os.path.basename(r["path"] or ""))
                      for r in rows]))
    writers = {r["system"] for r in rows if r["mode"] in ("output", "mod", "both", "create") and r["system"]}
    readers = {r["system"] for r in rows if r["mode"] in ("input", "both") and r["system"]}
    if writers and readers and writers != readers:
        out.append(f"\n**Crosses departments:** written by {', '.join(sorted(writers))}; read by "
                   f"{', '.join(sorted(readers))}. A layout or value change here is an interface change "
                   f"between systems, not an internal one.\n")
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
    progs = conn.execute("""SELECT DISTINCT m.name AS member_name, p.program_id, p.id AS pid, c.replacing
                            FROM copy_use c JOIN member m ON m.id=c.member_id JOIN program p ON p.member_id=m.id
                            WHERE UPPER(c.copybook)=?""", (name.upper(),)).fetchall()
    out.append(f"\n### Programs including it ({len(progs)})\n")
    out.append(table(["program", "member", "REPLACING"], [(p["program_id"], p["member_name"], (p["replacing"] or "")[:40]) for p in progs]))
    pnames = [p["program_id"].upper() for p in progs]
    if pnames:
        q = ",".join("?" * len(pnames))
        steps = conn.execute(f"""SELECT DISTINCT j.job_name, s.step_name, s.effective_pgm FROM step s LEFT JOIN job j ON j.id=s.job_id
                                 WHERE UPPER(s.effective_pgm) IN ({q})""", pnames).fetchall()
        out.append(f"\n### Jobs/steps running those programs ({len(steps)})\n")
        out.append(table(["job", "step", "program"], [(s["job_name"], s["step_name"], s["effective_pgm"]) for s in steps]))
        outs = conn.execute(f"""SELECT DISTINCT d.dsn_resolved, s.effective_pgm, d.mode FROM dd d JOIN step s ON s.id=d.step_id
                                WHERE UPPER(s.effective_pgm) IN ({q}) AND d.dsn_resolved IS NOT NULL AND d.mode IN ('output','mod','both')""",
                            pnames).fetchall()
        out.append(f"\n### Datasets WRITTEN by those programs ({len(outs)}) - downstream consumers are one hop away\n")
        rows = []
        for o in outs:
            cons = conn.execute("""SELECT DISTINCT s.effective_pgm FROM dd d JOIN step s ON s.id=d.step_id
                                   WHERE d.dsn_resolved=? AND d.mode IN ('input','both','unknown') AND UPPER(s.effective_pgm) NOT IN (%s)""" % q,
                                (o["dsn_resolved"], *pnames)).fetchall()
            rows.append((o["dsn_resolved"], o["effective_pgm"], ", ".join(c["effective_pgm"] or "?" for c in cons) or "_none indexed_"))
        out.append(table(["dataset", "written by", "then read by"], rows))
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
    for r in conn.execute("SELECT target, resolved FROM call_edge"):
        if r["target"]:
            called.add(r["target"].upper())
        called.update(t.upper() for t in _jl(r["resolved"]))
    run = {r[0].upper() for r in conn.execute("SELECT DISTINCT effective_pgm FROM step WHERE effective_pgm IS NOT NULL")}
    tx = {r[0].upper() for r in conn.execute("SELECT DISTINCT program FROM transaction_def WHERE program IS NOT NULL")}
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
    return "".join(out)


def cmd_ambiguous(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT * FROM v_ambiguous_member ORDER BY distinct_content DESC, copies DESC").fetchall()
    out = [f"# Duplicate member names ({len(rows)})\n\n> Same name in more than one place. When contents differ, "
           "the index cannot know which is production; declare it in a manifest (`--manifest`) or every answer "
           "about these members is a coin toss.\n\n"]
    out.append(table(["member", "copies", "distinct contents", "systems", "paths"],
                     [(r["name"], r["copies"], r["distinct_content"], r["systems"] or "?", r["paths"][:160])
                      for r in rows[:300]]))
    out.append("\n> Same name in two DEPARTMENTS is normal and safe once each department's programs resolve "
               "to their own copy (sources.json `system` + copybook order). Same name with different content "
               "inside ONE department is the dangerous case.\n")
    return "".join(out)


def cmd_coverage(conn: sqlite3.Connection) -> str:
    out = ["# Coverage - what the index does and does not know\n"]
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
               f"- members with same name and DIFFERENT content: {n_amb}\n")
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


def cmd_cite(conn: sqlite3.Connection, member: str, rng: str, program: Optional[str]) -> str:
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
    row = conn.execute("SELECT path FROM member WHERE UPPER(name)=? ORDER BY authoritative DESC", (member.upper(),)).fetchone()
    if not row:
        return f"member {member} not found\n"
    text, data, enc = reader.load(row["path"])
    recs = reader._split_records(text, data, enc)
    out = [f"{member.upper()}  {row['path']}\n"]
    for i in range(max(1, s), min(len(recs), e) + 1):
        out.append(f"{i:6d} | {recs[i-1].rstrip()}\n")
    return "".join(out)


# --------------------------------------------------------------------------
# pack - the token-frugal handoff to a model
# --------------------------------------------------------------------------

def cmd_pack(conn: sqlite3.Connection, name: str, max_lines: int) -> str:
    """Dossier + the exact source lines behind every edge, so a model can
    answer from facts and cite them without reading the whole program."""
    progs = programs_named(conn, name)
    if not progs:
        return f"program {name} not found\n"
    p = progs[0]
    out = [cmd_program(conn, name)]
    out.append("\n---\n## Evidence lines (source, for citation)\n\n")
    out.append("Cite as `[[MEMBER line \"token\"]]`. Lines are from the ORIGINAL member.\n\n")
    seen = set()
    n = 0
    picks: List[Tuple[str, int]] = []
    for r in conn.execute("SELECT line FROM call_edge WHERE program_id=? ORDER BY line", (p["id"],)):
        picks.append(("CALL", r["line"]))
    for r in conn.execute("SELECT start_line FROM sql_stmt WHERE program_id=? ORDER BY start_line", (p["id"],)):
        picks.append(("SQL", r["start_line"]))
    for r in conn.execute("SELECT line FROM dli_call WHERE program_id=? ORDER BY line", (p["id"],)):
        picks.append(("DLI", r["line"]))
    for r in conn.execute("SELECT line FROM file_decl WHERE program_id=? ORDER BY line", (p["id"],)):
        picks.append(("SELECT", r["line"]))
    for r in conn.execute("SELECT line FROM io_op WHERE program_id=? AND target_kind='file' ORDER BY line", (p["id"],)):
        picks.append(("IO", r["line"]))
    for r in conn.execute("SELECT start_line FROM paragraph WHERE program_id=? ORDER BY ordinal", (p["id"],)):
        picks.append(("PARA", r["start_line"]))
    for r in conn.execute("SELECT line FROM literal_ref WHERE program_id=? AND context='move_to' ORDER BY line", (p["id"],)):
        picks.append(("SET", r["line"]))
    for tag, exp_line in picks:
        m, ln, _d, _v = origin(conn, p["id"], exp_line)
        if not m or (m, ln) in seen:
            continue
        seen.add((m, ln))
        txt = source_line(conn, m, ln)
        out.append(f"- {tag:<6} `{m}:{ln}`  {txt[:100]}\n")
        n += 1
        if n >= max_lines:
            out.append(f"_...capped at {max_lines} evidence lines; use `cite {p['member_name']} a-b` for more_\n")
            break
    return "".join(out)


# --------------------------------------------------------------------------
# DB2 columns
# --------------------------------------------------------------------------

def cmd_column(conn: sqlite3.Connection, name: str) -> str:
    """Where a DB2 column is written from, read into, and filtered by - and,
    through the host variable, where that value came from or went next."""
    n = name.upper()
    tbl, _, col = n.rpartition(".")
    q = "UPPER(c.col)=?" + (" AND UPPER(c.tbl)=?" if tbl else "")
    args = (col, tbl) if tbl else (col,)
    rows = conn.execute(f"""SELECT c.*, p.program_id AS pname FROM sql_col_ref c JOIN program p ON p.id=c.program_id
                            WHERE {q} ORDER BY c.mode, p.program_id, c.line""", args).fetchall()
    out = [f"# DB2 column {n}\n"]
    if not rows:
        return out[0] + ("\n**No static SQL references** to this column in indexed programs - dynamic SQL, "
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
                       r["group_name"] or "", (r["detail"] or "")[:80], f"{r['mem']}:{r['line']}") for r in rows]))
    for p in sorted({r["program"] for r in rows if r["program"]}):
        if programs_named(conn, p):
            out.append(f"- `{p}` is indexed - `program {p}` for its dossier\n")
        else:
            out.append(f"- **{p} is not in the index** (no source, or member / PROGRAM-ID / load name differ)\n")
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
# main
# --------------------------------------------------------------------------

def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Query atlas.db (markdown out, citations in).")
    ap.add_argument("--db", default="atlas.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("program", "job", "field", "dataset", "copybook", "values", "screen", "transaction", "column"):
        sub.add_parser(c).add_argument("name")
    s = sub.add_parser("literal")
    s.add_argument("name")
    s.add_argument("--field", help="only uses on this field (common values like 3 or 'M' appear everywhere)")
    s.add_argument("--like", action="store_true", help="substring match instead of exact")
    s = sub.add_parser("pair")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--window", type=int, default=8)
    sub.add_parser("messages").add_argument("pattern")
    for c in ("callers", "callees"):
        s = sub.add_parser(c)
        s.add_argument("name")
        s.add_argument("--depth", type=int, default=2)
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
    s = sub.add_parser("pack")
    s.add_argument("name")
    s.add_argument("--max-lines", type=int, default=120)
    a = ap.parse_args(argv)

    conn = connect(a.db)
    try:
        if a.cmd == "program":
            print(cmd_program(conn, a.name))
        elif a.cmd == "job":
            print(cmd_job(conn, a.name))
        elif a.cmd in ("callers", "callees"):
            print(cmd_graph(conn, a.name, a.cmd, a.depth))
        elif a.cmd == "field":
            print(cmd_field(conn, a.name))
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
            print(cmd_cite(conn, a.member, a.range, a.program))
        elif a.cmd == "pack":
            print(cmd_pack(conn, a.name, a.max_lines))
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
