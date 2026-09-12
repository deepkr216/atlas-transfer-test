"""
build.py - walk the folder, extract facts, write atlas.db. No model calls.

    python -m atlas.build  C:/path/to/estate  --db atlas.db
    python -m atlas.build  C:/path/to/estate  --db atlas.db --manifest manifest.json
    python -m atlas.build  C:/path/to/estate  --db atlas.db --write-expanded out/expanded

Two passes: inventory every file (hash, classify), then parse by kind. Every
COBOL program is expanded (COPY / EXEC SQL INCLUDE resolved, REPLACING
applied) before it is parsed, so facts reflect what the compiler saw. Line
numbers on program facts are therefore EXPANDED line numbers; `expand_run`
maps them back to (member, line) and query.py does the translation.

Nothing that fails is dropped: a member that will not parse is recorded with
parse_status='failed' and the error, and every unresolved reference lands in
`unresolved`. The summary at the end is the coverage report; read it before
trusting any answer built on the index.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import classify, cobol, copybook, docs, expand, ims, jcl, reader
from .reader import Line

VERSION = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))

CODE_KINDS = {"cobol", "copybook", "jcl", "proc", "ctlcard", "dbd", "psb", "bms", "mfs",
              "sql", "asm", "rexx"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".svn", "$RECYCLE.BIN"}

EXTRA_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS src_fts USING fts5(
    member_name, kind, line_no UNINDEXED, text,
    tokenize="unicode61 tokenchars '-_#@$:'");
CREATE TABLE IF NOT EXISTS doc_section(
    id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    heading TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS doc_image(
    id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    name TEXT);
CREATE TABLE IF NOT EXISTS expand_run(
    id INTEGER PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    exp_start INTEGER, exp_end INTEGER,
    src_member INTEGER REFERENCES member(id), src_start INTEGER,
    depth INTEGER, via_copy TEXT);
CREATE INDEX IF NOT EXISTS ix_exprun ON expand_run(program_id, exp_start);
"""


@dataclass
class Mem:
    id: int
    path: str
    name: str
    kind: str
    library: str
    norm_sha: str
    authoritative: int = 0


class Ctx:
    def __init__(self, conn: sqlite3.Connection, quiet: bool = False):
        self.conn = conn
        self.quiet = quiet
        self.members: List[Mem] = []
        self.by_name: Dict[str, List[Mem]] = {}
        self._lines: Dict[int, List[Line]] = {}
        self.stats: Dict[str, int] = {}
        self.write_expanded: Optional[str] = None

    def bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def lines_for(self, mem: Mem) -> List[Line]:
        if mem.id not in self._lines:
            text, data, enc = reader.load(mem.path)
            lines, _ = reader.read_cobol_lines(text, data=data, enc=enc)
            self._lines[mem.id] = lines
            if len(self._lines) > 4000:            # bounded cache
                self._lines.pop(next(iter(self._lines)))
        return self._lines[mem.id]

    def say(self, msg: str) -> None:
        if not self.quiet:
            print(msg, flush=True)


# --------------------------------------------------------------------------
# db
# --------------------------------------------------------------------------

def open_db(path: str, rebuild: bool = False) -> sqlite3.Connection:
    if rebuild and os.path.exists(path):
        os.remove(path)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with open(os.path.join(HERE, "schema.sql"), "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.executescript(EXTRA_SCHEMA)
    return conn


def _j(v) -> Optional[str]:
    return json.dumps(v) if v not in (None, [], {}) else None


# --------------------------------------------------------------------------
# pass 1 - inventory
# --------------------------------------------------------------------------

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def norm_hash(kind: str, text: str, data: bytes, enc: str) -> Tuple[str, int, int]:
    """(norm_sha, line_count, fixed_format)"""
    if kind in ("cobol", "copybook"):
        lines, fixed = reader.read_cobol_lines(text, data=data, enc=enc)
        payload = "\n".join(l.code.rstrip().upper() for l in lines if not l.is_comment)
        return sha(payload.encode("utf-8", "replace")), len(lines), int(fixed)
    recs = reader._split_records(text, data, enc)
    if kind in ("jcl", "proc", "ctlcard"):
        payload = "\n".join(r[:71].rstrip().upper() for r in recs if not r.startswith("//*"))
    else:
        payload = "\n".join(r.rstrip().upper() for r in recs)
    return sha(payload.encode("utf-8", "replace")), len(recs), 0


def inventory(ctx: Ctx, root: str, limit: Optional[int] = None) -> None:
    conn = ctx.conn
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    count = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(files):
            if fn.startswith("."):
                continue
            path = os.path.normpath(os.path.join(dirpath, fn))
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError as e:
                ctx.bump("unreadable")
                ctx.say(f"  unreadable: {path} ({e})")
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in classify.BINARY_EXTS or ext in docs.LEGACY or ext in (".pdf", ".docx", ".xlsx", ".pptx", ".vsdx"):
                kind, why = classify.classify(path, "", None)
                if kind == "binary":
                    kind = "doc"
                text, enc, norm, nlines, fixed = "", "binary", sha(data), 0, 0
            else:
                text, enc = reader.decode_bytes(data)
                kind, why = classify.classify(path, text[:8192])
                norm, nlines, fixed = norm_hash(kind, text, data, enc)

            library = os.path.basename(dirpath)
            name = os.path.splitext(fn)[0].upper()
            cur = conn.execute(
                "INSERT OR REPLACE INTO member(path,name,kind,library,ext,sha256,norm_sha,bytes,lines,"
                "fixed_format,parse_status,scanned_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (path, name, kind, library, ext, sha(data), norm, len(data), nlines, fixed, "pending", now))
            mem = Mem(cur.lastrowid, path, name, kind, library, norm)
            ctx.members.append(mem)
            ctx.by_name.setdefault(name, []).append(mem)
            ctx.bump(f"kind:{kind}")
            count += 1
            if limit and count >= limit:
                return


def load_sched(ctx: Ctx, csv_path: str) -> None:
    """Scheduler export (CA-7 / Control-M / TWS) as CSV.

    Columns (header row required, any order, extra columns ignored):
      job_name, depends_on, kind          -> one predecessor/trigger edge per row
      job_name, system, schedule, calendar -> one job definition per row
    A row with a non-empty depends_on is an edge; otherwise a definition.
    The scheduler is where the real batch flow lives; without it "is this job
    dead" and "what runs before this" cannot be answered.
    """
    import csv
    n_dep = n_job = 0
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            job = r.get("job_name") or r.get("job") or r.get("jobname")
            if not job:
                continue
            if r.get("depends_on") or r.get("predecessor"):
                ctx.conn.execute("INSERT INTO sched_dep(job_name,depends_on,kind) VALUES(?,?,?)",
                                 (job.upper(), (r.get("depends_on") or r.get("predecessor")).upper(),
                                  r.get("kind") or "predecessor"))
                n_dep += 1
            else:
                ctx.conn.execute("INSERT INTO sched_job(job_name,system,schedule,calendar,source) VALUES(?,?,?,?,?)",
                                 (job.upper(), r.get("system"), r.get("schedule"), r.get("calendar"),
                                  os.path.basename(csv_path)))
                n_job += 1
    ctx.conn.commit()
    ctx.say(f"scheduler: {n_job} job definition(s), {n_dep} dependency edge(s) loaded")


def apply_manifest(ctx: Ctx, manifest_path: str) -> None:
    """manifest.json: {"authoritative": ["PROD.SRC", "C:/estate/prod/"], "system_of": {"CLMLIB": "CLAIMS"}}"""
    with open(manifest_path, "r", encoding="utf-8") as fh:
        man = json.load(fh)
    prefixes = [p.replace("\\", "/").upper() for p in man.get("authoritative", [])]
    n = 0
    for m in ctx.members:
        p = m.path.replace("\\", "/").upper()
        if any(p.startswith(x) or f"/{x}/" in p or m.library.upper() == x for x in prefixes):
            m.authoritative = 1
            ctx.conn.execute("UPDATE member SET authoritative=1 WHERE id=?", (m.id,))
            n += 1
    ctx.say(f"manifest: {n} member(s) marked authoritative")


# --------------------------------------------------------------------------
# copybook resolution
# --------------------------------------------------------------------------

def make_resolver(ctx: Ctx, prog: Mem, notes: List[Tuple[str, str, int]]):
    def resolve(name: str, lib: Optional[str]):
        cands = [c for c in ctx.by_name.get(name.upper(), [])
                 if c.kind in ("copybook", "cobol", "sql", "unknown") and c.id != prog.id]
        if not cands:
            return None
        note = None
        pick = cands[0]
        if len(cands) > 1:
            auth = [c for c in cands if c.authoritative]
            same_lib = [c for c in cands if c.library == prog.library]
            by_lib = [c for c in cands if lib and c.library.upper() == lib.upper()]
            pick = (by_lib or auth or same_lib or cands)[0]
            if len({c.norm_sha for c in cands}) > 1:
                note = (f"{len(cands)} copies of {name} with different content; used {pick.path}")
                notes.append(("ambiguous_copybook", note, 0))
        return pick.id, ctx.lines_for(pick), note
    return resolve


# --------------------------------------------------------------------------
# pass 2 - per-kind indexing
# --------------------------------------------------------------------------

def index_cobol(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    lines = ctx.lines_for(mem)
    notes: List[Tuple[str, str, int]] = []
    exp = expand.expand(lines, mem.id, make_resolver(ctx, mem, notes))
    exp_text = expand.expanded_text(exp)
    facts = cobol.parse_program(exp_text)

    pid_name = facts.program_id or mem.name
    cur = conn.execute(
        "INSERT INTO program(member_id,program_id,uses_sql,uses_cics,uses_dli,uses_mq,linkage_using,"
        "src_lines,exp_lines) VALUES(?,?,?,?,?,?,?,?,?)",
        (mem.id, pid_name, int(facts.uses_sql), int(facts.uses_cics), int(facts.uses_dli),
         int(facts.uses_mq), _j(facts.linkage_using), len(lines), len(exp.lines)))
    pid = cur.lastrowid
    if facts.program_id is None:
        notes.append(("no_program_id", f"no PROGRAM-ID found; using member name {mem.name}", 0))

    conn.executemany(
        "INSERT INTO expand_run(program_id,exp_start,exp_end,src_member,src_start,depth,via_copy) "
        "VALUES(?,?,?,?,?,?,?)",
        [(pid, r.exp_start, r.exp_end, r.src_member, r.src_start, r.depth, r.via_copy) for r in exp.runs])

    conn.executemany(
        "INSERT INTO paragraph(program_id,section,name,start_line,end_line,ordinal) VALUES(?,?,?,?,?,?)",
        [(pid, p.section, p.name, p.start_line, p.end_line, p.ordinal) for p in facts.paragraphs])
    conn.executemany(
        "INSERT INTO perform_edge(program_id,from_para,to_para,thru_para,line) VALUES(?,?,?,?,?)",
        [(pid, a, b, c, ln) for (a, b, c, ln) in facts.performs])
    conn.executemany(
        "INSERT INTO call_edge(program_id,kind,target,via_var,resolved,resolution,using_args,line) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [(pid, c.kind, c.target, c.via_var, _j(c.resolved), c.resolution, _j(c.using_args), c.line)
         for c in facts.calls])
    for c in facts.calls:
        ctx.bump("calls:" + ("unresolved" if c.resolution == "unresolved" else c.kind))

    conn.executemany(
        "INSERT INTO copy_use(member_id,copybook,of_library,replacing,line,resolved_member_id) "
        "VALUES(?,?,?,?,?,?)",
        [(mem.id, name, lib, rep, ln, rid) for (name, lib, rep, ln, rid) in exp.copies])

    conn.executemany(
        "INSERT INTO file_decl(program_id,select_name,assign_dd,organization,access_mode,record_key,"
        "alt_keys,fd_record,line) VALUES(?,?,?,?,?,?,?,?,?)",
        [(pid, f.select_name, f.assign_dd, f.organization, f.access_mode, f.record_key,
          _j(f.alt_keys), f.fd_record, f.line) for f in facts.files])
    conn.executemany(
        "INSERT INTO io_op(program_id,target,target_kind,op,line) VALUES(?,?,?,?,?)",
        [(pid, t, k, op, ln) for (t, k, op, ln) in facts.io_ops])
    conn.executemany(
        "INSERT INTO sql_stmt(member_id,program_id,stmt_type,cursor_name,tables,columns,host_vars,"
        "is_dynamic,start_line,end_line,text) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(mem.id, pid, s.stmt_type, s.cursor_name, _j(s.tables), None, _j(s.host_vars),
          int(s.is_dynamic), s.start_line, s.end_line, s.text[:4000]) for s in facts.sql])
    conn.executemany(
        "INSERT INTO dli_call(program_id,interface,func,pcb_arg,pcb_ordinal,ssa_args,io_area,line) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [(pid, d.interface, d.func, d.pcb_arg, None, _j(d.ssa_args), d.io_area, d.line) for d in facts.dli])
    conn.executemany(
        "INSERT INTO interface_edge(member_id,kind,detail,direction,line) VALUES(?,?,?,?,?)",
        [(mem.id, "mq", call, ("out" if call in ("MQPUT", "MQPUT1") else "in" if call == "MQGET" else None), ln)
         for (call, ln) in facts.mq])
    conn.executemany(
        "INSERT INTO field_ref(program_id,name,mode,stmt,line) VALUES(?,?,?,?,?)",
        [(pid, n, mode, stmt, ln) for (n, mode, stmt, ln) in facts.field_refs])
    conn.executemany(
        "INSERT INTO literal_ref(member_id,program_id,literal,context,field,line) VALUES(?,?,?,?,?,?)",
        [(mem.id, pid, lit, ctxt, fld, ln) for (lit, ctxt, fld, ln) in facts.literal_refs])

    # Fields: keep this program's own data items, plus any copybook brought in
    # with REPLACING (its names are program-specific). Plain copybook fields
    # are reached through copy_use -> the copybook's own field rows.
    replaced = {name for (name, _l, rep, _ln, _r) in exp.copies if rep}
    logical = reader.join_cobol_continuations(exp.lines)
    roots, warns = copybook.parse_data_division(logical)
    keep: List[copybook.Field] = []
    for fld in copybook.flatten(roots):
        if fld.name == copybook.SYNTHETIC_ROOT:
            continue
        _m, _l, depth = exp.origin(fld.line)
        run = next((r for r in exp.runs if r.exp_start <= fld.line <= r.exp_end), None)
        if depth == 0 or (run and run.via_copy in replaced):
            keep.append(fld)
    _insert_fields(conn, mem.id, keep)

    for w in warns:
        if "SYNC" in w or "not compile" in w:
            notes.append(("layout_warning", w, 0))
    for (kind, detail, ln) in facts.unresolved:
        notes.append((kind, detail, ln))
    for w in exp.warnings:
        notes.append(("expand", w, 0))
    conn.executemany(
        "INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
        [(mem.id, k, d, ln or None) for (k, d, ln) in notes])
    ctx.bump("unresolved", len(notes))

    if ctx.write_expanded:
        os.makedirs(ctx.write_expanded, exist_ok=True)
        with open(os.path.join(ctx.write_expanded, f"{pid_name}.exp.cbl"), "w", encoding="utf-8") as fh:
            fh.write(exp_text)

    status = "partial" if any(k in ("expand",) for (k, _d, _l) in notes) else "ok"
    conn.execute("UPDATE member SET parse_status=? WHERE id=?", (status, mem.id))


def _insert_fields(conn: sqlite3.Connection, member_id: int, fields: List[copybook.Field]) -> None:
    ids: Dict[int, int] = {}
    for f in fields:
        parent_id = ids.get(id(f.parent)) if f.parent else None
        cur = conn.execute(
            "INSERT INTO field(member_id,parent_id,level,name,qualified,pic,usage,sign_clause,occurs,"
            "occurs_max,odo_on,redefines,value_lit,offset,length,digits,scale,is_group,line) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (member_id, parent_id, f.level, f.name, f.qualified, f.pic, f.usage, f.sign_clause,
             f.occurs, f.occurs_max, f.odo_on, f.redefines, f.value_lit, f.offset, f.length,
             f.digits, f.scale, int(f.is_group), f.line))
        ids[id(f)] = cur.lastrowid
        for (name, vals, ln) in f.conds:
            conn.execute("INSERT INTO cond88(field_id,name,values_lit,line) VALUES(?,?,?,?)",
                         (cur.lastrowid, name, _j(vals), ln))


def index_copybook(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    lines = ctx.lines_for(mem)
    logical = reader.join_cobol_continuations(lines)
    roots, warns = copybook.parse_data_division(logical)
    fields = [f for f in copybook.flatten(roots) if f.name != copybook.SYNTHETIC_ROOT]
    _insert_fields(conn, mem.id, fields)

    lits: List[Tuple[str, str, str, int]] = []
    for f in fields:
        if f.value_lit:
            lits.append((cobol._norm_lit(f.value_lit), "value", f.name, f.line))
        for (name, vals, ln) in f.conds:
            for v in vals:
                if v.upper() not in ("THRU", "THROUGH"):
                    lits.append((cobol._norm_lit(v), "cond88", f"{f.name}/{name}", ln))
    conn.executemany(
        "INSERT INTO literal_ref(member_id,program_id,literal,context,field,line) VALUES(?,NULL,?,?,?,?)",
        [(mem.id, lit, c, fld, ln) for (lit, c, fld, ln) in lits])

    # Nested COPY / procedure code inside a copybook: parse for copies only.
    text = "\n".join(f"{'':6}{l.indicator}{l.code}" for l in lines)
    facts = cobol.parse_program(text)
    conn.executemany(
        "INSERT INTO copy_use(member_id,copybook,of_library,replacing,line) VALUES(?,?,?,?,?)",
        [(mem.id, n, lib, rep, ln) for (n, lib, rep, ln) in facts.copies])
    if facts.calls or facts.performs or facts.sql:
        conn.execute("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                     (mem.id, "procedure_copybook",
                      f"copybook contains procedure code ({len(facts.calls)} CALL, "
                      f"{len(facts.performs)} PERFORM, {len(facts.sql)} SQL); its facts are attributed "
                      f"to each including program via expansion", None))
    for w in warns:
        if "SYNC" in w or "not compile" in w or "mixed" in w:
            conn.execute("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                         (mem.id, "layout_warning", w, None))
    conn.execute("UPDATE member SET parse_status='ok' WHERE id=?", (mem.id,))


def index_jcl(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    text, data, enc = reader.load(mem.path)
    facts = jcl.parse_jcl(text, data, enc)
    job_id = proc_id = None
    if facts.is_proc or mem.kind == "proc":
        cur = conn.execute(
            "INSERT INTO proc_def(member_id,proc_name,symbolics,instream,line) VALUES(?,?,?,?,?)",
            (mem.id, facts.proc_name or mem.name, _j(facts.symbolics), 0, facts.job_line or 1))
        proc_id = cur.lastrowid
    else:
        cur = conn.execute("INSERT INTO job(member_id,job_name,line) VALUES(?,?,?)",
                           (mem.id, facts.job_name or mem.name, facts.job_line or 1))
        job_id = cur.lastrowid

    for s in facts.steps:
        cur = conn.execute(
            "INSERT INTO step(job_id,proc_id,ordinal,step_name,pgm,proc_called,effective_pgm,launcher,"
            "parm,cond,line) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, proc_id, s.ordinal, s.step_name, s.pgm, s.proc_called, s.effective_pgm,
             s.launcher, s.parm, s.cond, s.line))
        sid = cur.lastrowid
        conn.executemany(
            "INSERT INTO dd(step_id,dd_name,concat_seq,dsn,dsn_resolved,gdg_rel,disp,mode,mode_source,"
            "sysin_text,is_override,line) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, d.dd_name, d.concat_seq, d.dsn, d.dsn_resolved, d.gdg_rel, d.disp, d.mode,
              d.mode_source, d.sysin_text, int(d.is_override), d.line) for d in s.dds])
        for d in s.dds:
            if d.dsn_resolved:
                base = d.dsn_resolved
                conn.execute("INSERT OR IGNORE INTO dataset(dsn,is_gdg) VALUES(?,?)",
                             (base, int(d.gdg_rel is not None)))
                if d.gdg_rel is not None:
                    conn.execute("UPDATE dataset SET is_gdg=1 WHERE dsn=?", (base,))
        if s.launcher and jcl.LAUNCHERS.get(s.launcher) == "sort":
            ctl = "\n".join(d.sysin_text for d in s.dds if d.sysin_text)
            conn.executemany(
                "INSERT INTO card_field_ref(step_id,card_kind,pos,length,fmt,raw) VALUES(?,?,?,?,?,?)",
                [(sid, k, p, ln, fmt, raw) for (k, p, ln, fmt, raw) in jcl.sort_card_fields(ctl)])
        if s.launcher and jcl.LAUNCHERS.get(s.launcher) in ("ftp", "ndm", "usssh"):
            conn.execute(
                "INSERT INTO interface_edge(member_id,kind,detail,direction,line) VALUES(?,?,?,?,?)",
                (mem.id, jcl.LAUNCHERS[s.launcher], "; ".join(s.notes)[:500], None, s.line))
        if s.notes:
            conn.execute("UPDATE step SET parm=COALESCE(parm,'') || ' /* ' || ? || ' */' WHERE id=?",
                         ("; ".join(s.notes)[:300], sid))
        if s.proc_called:
            ctx.bump("steps:proc")
        elif s.launcher:
            ctx.bump("steps:launcher")
        else:
            ctx.bump("steps:pgm")

    conn.executemany("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                     [(mem.id, k, d, ln) for (k, d, ln) in facts.unresolved])
    ctx.bump("unresolved", len(facts.unresolved))
    conn.execute("UPDATE member SET parse_status='ok' WHERE id=?", (mem.id,))


def index_dbd(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    text, _d, _e = reader.load(mem.path)
    f = ims.parse_dbd(text)
    cur = conn.execute("INSERT INTO ims_dbd(member_id,name,access,line) VALUES(?,?,?,?)",
                       (mem.id, f.name or mem.name, f.access, 1))
    did = cur.lastrowid
    conn.executemany(
        "INSERT INTO ims_segment(dbd_id,name,parent,bytes,seq_field,line) VALUES(?,?,?,?,?,?)",
        [(did, s.name, s.parent, s.bytes_max, s.seq_field, s.line) for s in f.segments])
    conn.executemany("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                     [(mem.id, "ims_dbd", w, None) for w in f.warnings])
    conn.execute("UPDATE member SET parse_status='ok' WHERE id=?", (mem.id,))


def index_psb(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    text, _d, _e = reader.load(mem.path)
    f = ims.parse_psb(text, fallback_name=mem.name)
    has_tp = any(p.pcb_type == "TP" for p in f.pcbs)
    cur = conn.execute(
        "INSERT INTO ims_psb(member_id,name,psb_type,lang,cmpat,io_pcb_first,line) VALUES(?,?,?,?,?,?,?)",
        (mem.id, f.name or mem.name, "TP" if has_tp else "DB", f.lang, f.cmpat,
         None if f.io_pcb_first is None else int(f.io_pcb_first), 1))
    psid = cur.lastrowid
    conn.executemany(
        "INSERT INTO ims_pcb(psb_id,ordinal,pcb_type,dbd_name,procopt,keylen,sensegs,line) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [(psid, p.ordinal, p.pcb_type, p.dbd_name, p.procopt, p.keylen,
          _j([s[0] for s in p.sensegs]), p.line) for p in f.pcbs])
    conn.executemany("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                     [(mem.id, "ims_psb", w, None) for w in f.warnings])
    conn.execute("UPDATE member SET parse_status='ok' WHERE id=?", (mem.id,))


def index_doc(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    d = docs.extract(mem.path)
    for i, (h, t) in enumerate(d.sections, 1):
        conn.execute("INSERT INTO doc_section(member_id,heading,text) VALUES(?,?,?)", (mem.id, h, t))
        if t:
            conn.execute("INSERT INTO src_fts(member_name,kind,line_no,text) VALUES(?,?,?,?)",
                         (mem.name, "doc", i, (h + "\n" + t)[:20000]))
    for tbl in d.tables:
        conn.execute("INSERT INTO src_fts(member_name,kind,line_no,text) VALUES(?,?,?,?)",
                     (mem.name, "doc", 0, "\n".join("\t".join(r) for r in tbl)[:20000]))
    conn.executemany("INSERT INTO doc_image(member_id,name) VALUES(?,?)",
                     [(mem.id, n) for n in d.images])
    ctx.bump("doc_images", len(d.images))
    conn.execute("UPDATE member SET parse_status=?, parse_error=? WHERE id=?",
                 ("ok" if d.ok else "partial", "; ".join(d.notes) or None, mem.id))


def index_fts_code(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    if mem.kind in ("cobol", "copybook"):
        rows = [(mem.name, mem.kind, l.no, l.code) for l in ctx.lines_for(mem) if l.code.strip()]
    else:
        text, data, enc = reader.load(mem.path)
        rows = [(mem.name, mem.kind, i, r.rstrip())
                for i, r in enumerate(reader._split_records(text, data, enc), 1) if r.strip()]
    conn.executemany("INSERT INTO src_fts(member_name,kind,line_no,text) VALUES(?,?,?,?)", rows)


HANDLERS = {
    "cobol": index_cobol, "copybook": index_copybook, "jcl": index_jcl, "proc": index_jcl,
    "dbd": index_dbd, "psb": index_psb, "doc": index_doc,
}


# --------------------------------------------------------------------------
# post passes
# --------------------------------------------------------------------------

def post_open_modes(ctx: Ctx) -> int:
    """The program's OPEN verb decides direction, overriding the JCL guess."""
    conn = ctx.conn
    n = 0
    rows = conn.execute("""
        SELECT p.id, p.program_id, f.assign_dd, f.select_name
        FROM program p JOIN file_decl f ON f.program_id = p.id
        WHERE f.assign_dd IS NOT NULL""").fetchall()
    for pid, pname, dd_name, sel in rows:
        opens = conn.execute(
            "SELECT op FROM io_op WHERE program_id=? AND target=? AND op LIKE 'OPEN%'",
            (pid, sel)).fetchall()
        if not opens:
            continue
        modes = {o[0].split()[-1] for o in opens}
        mode = ("both" if len(modes) > 1 or "I-O" in modes else
                {"INPUT": "input", "OUTPUT": "output", "EXTEND": "mod"}.get(next(iter(modes)), "unknown"))
        cur = conn.execute("""
            UPDATE dd SET mode=?, mode_source='open_verb'
            WHERE UPPER(dd_name) = ? AND step_id IN (SELECT id FROM step WHERE effective_pgm=?)""",
            (mode, dd_name.upper(), pname))
        n += cur.rowcount
    return n


def summary(ctx: Ctx, db_path: str, root: str, t0: float) -> None:
    conn = ctx.conn
    kinds = conn.execute("SELECT kind, parse_status, COUNT(*) FROM member GROUP BY 1,2 ORDER BY 1,2").fetchall()
    print("\n== members by kind / parse status ==")
    for k, s, c in kinds:
        print(f"  {k:<10} {s:<8} {c:>7}")
    n_prog = conn.execute("SELECT COUNT(*) FROM program").fetchone()[0]
    n_calls = conn.execute("SELECT resolution, COUNT(*) FROM call_edge GROUP BY 1").fetchall()
    n_jobs = conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
    n_steps = conn.execute("SELECT COUNT(*) FROM step").fetchone()[0]
    n_unres = conn.execute("SELECT kind, COUNT(*) FROM unresolved GROUP BY 1 ORDER BY 2 DESC").fetchall()
    n_amb = conn.execute("SELECT COUNT(*) FROM v_ambiguous_member WHERE distinct_content > 1").fetchone()[0]
    n_dup = conn.execute("SELECT COUNT(*) FROM v_ambiguous_member").fetchone()[0]
    print(f"\n== programs {n_prog}   jobs {n_jobs}   steps {n_steps} ==")
    print("  call edges: " + ", ".join(f"{r}={c}" for r, c in n_calls))
    print(f"\n== unresolved ({sum(c for _k, c in n_unres)}) - these are the known blind spots ==")
    for k, c in n_unres[:15]:
        print(f"  {k:<22} {c:>7}")
    print(f"\n== duplicate member names: {n_dup}  (with DIFFERENT content: {n_amb}) ==")
    if n_amb:
        print("  -> run `python -m atlas.query ambiguous` and declare production libraries in a manifest")
    print(f"\ndb: {db_path}   root: {root}   {time.time() - t0:.1f}s")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build atlas.db from a mainframe source/document folder.")
    ap.add_argument("root")
    ap.add_argument("--db", default="atlas.db")
    ap.add_argument("--manifest", help="JSON declaring authoritative (production) libraries")
    ap.add_argument("--sched", help="scheduler export CSV (job_name,depends_on,kind | job_name,system,schedule)")
    ap.add_argument("--rebuild", action="store_true", help="delete the db first")
    ap.add_argument("--write-expanded", help="directory to write expanded COBOL sources into")
    ap.add_argument("--limit", type=int, help="index only the first N files (smoke test)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    t0 = time.time()
    conn = open_db(args.db, rebuild=args.rebuild)
    ctx = Ctx(conn, quiet=args.quiet)
    ctx.write_expanded = args.write_expanded
    run = conn.execute("INSERT INTO build_run(started_at,root,tool_version) VALUES(?,?,?)",
                       (time.strftime("%Y-%m-%dT%H:%M:%S"), args.root, VERSION))
    run_id = run.lastrowid

    ctx.say(f"inventory: {args.root}")
    inventory(ctx, args.root, args.limit)
    conn.commit()
    ctx.say(f"  {len(ctx.members)} files: " + ", ".join(
        f"{k[5:]}={v}" for k, v in sorted(ctx.stats.items()) if k.startswith("kind:")))
    if args.manifest:
        apply_manifest(ctx, args.manifest)
    if args.sched:
        load_sched(ctx, args.sched)

    # Copybooks first so field rows exist; programs; then everything else.
    order = {"copybook": 0, "cobol": 1, "proc": 2, "jcl": 3, "dbd": 4, "psb": 5, "doc": 9}
    ok = partial = failed = 0
    for i, mem in enumerate(sorted(ctx.members, key=lambda m: order.get(m.kind, 6)), 1):
        handler = HANDLERS.get(mem.kind)
        try:
            if handler:
                handler(ctx, mem)
            if mem.kind in CODE_KINDS:
                index_fts_code(ctx, mem)
            if not handler:
                conn.execute("UPDATE member SET parse_status='skipped' WHERE id=?", (mem.id,))
            st = conn.execute("SELECT parse_status FROM member WHERE id=?", (mem.id,)).fetchone()[0]
            ok += st == "ok"
            partial += st == "partial"
        except Exception as e:                                          # noqa: BLE001
            failed += 1
            conn.execute("UPDATE member SET parse_status='failed', parse_error=? WHERE id=?",
                         (f"{type(e).__name__}: {e}"[:500], mem.id))
            ctx.say(f"  FAILED {mem.kind} {mem.path}: {type(e).__name__}: {e}")
        if i % 500 == 0:
            conn.commit()
            ctx.say(f"  {i}/{len(ctx.members)} ...")
    conn.commit()

    n = post_open_modes(ctx)
    ctx.say(f"post: {n} DD direction(s) set from OPEN verbs")
    conn.execute("UPDATE build_run SET finished_at=?, members=?, ok=?, partial=?, failed=? WHERE id=?",
                 (time.strftime("%Y-%m-%dT%H:%M:%S"), len(ctx.members), ok, partial, failed, run_id))
    conn.commit()
    summary(ctx, args.db, args.root, t0)
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
