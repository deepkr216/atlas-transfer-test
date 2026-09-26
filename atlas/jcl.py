"""
jcl.py - extract jobs, steps, DDs, datasets and the EFFECTIVE program.

The one idea worth internalising here: `PGM=` very often does not name the
program you care about. A large share of batch steps in a shop like this run a
launcher, and the real program is buried in a control card or a PARM:

    PGM=IKJEFT01 / IKJEFT1B   -> TSO; real program in SYSTSIN "RUN PROGRAM(x)"
    PGM=DFSRRC00              -> IMS region; real program in PARM (DLI,pgm,psb)
    PGM=SORT / ICETOOL        -> DFSORT; behaviour is entirely in SYSIN
    PGM=IDCAMS                -> VSAM define/repro; objects are in SYSIN
    PGM=IEBGENER / ICEGENER   -> plain copy SYSUT1 -> SYSUT2
    PGM=DSNUTILB              -> DB2 utility; objects in SYSIN
    PGM=IEFBR14               -> does nothing; the DDs do the work (alloc/delete)

Index `PGM=` alone and every one of those steps is attributed to the wrong
program, which then corrupts the call graph, the dataset flow, and every
impact analysis that walks them. `effective_pgm` is the column analysis should
read; `pgm` is kept beside it so a human can always see what the JCL literally
said.

Symbolic parameters are the other silent corrupter. `DSN=&HLQ..EXTRACT(+1)`
is not a dataset name until &HLQ is resolved, and the resolution order is:
PROC defaults, overridden by EXEC PROC=x,SYM=value, overridden by an instream
SET. Get this wrong and producer/consumer linkage across jobs simply vanishes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field, replace
from typing import Callable, Dict, List, Optional, Tuple

from .reader import JclStatement, _split_records, keyword_operands, read_jcl, split_operands

# --------------------------------------------------------------------------
# launcher table
# --------------------------------------------------------------------------

LAUNCHERS = {
    "IKJEFT01": "tso", "IKJEFT1A": "tso", "IKJEFT1B": "tso",
    "DFSRRC00": "ims",
    "SORT": "sort", "DFSORT": "sort", "ICEMAN": "sort", "SYNCSORT": "sort",
    "ICETOOL": "sort",
    "IDCAMS": "idcams",
    "IEBGENER": "copy", "ICEGENER": "copy", "IEBCOPY": "copy",
    "IEFBR14": "noop",
    "DSNUTILB": "db2util",
    "DSNTIAUL": "db2util", "DSNTEP2": "db2util", "DSNTEP4": "db2util",
    "BPXBATCH": "usssh",
    "NDMCOPY": "ndm", "DMBATCH": "ndm",
    "FTP": "ftp",
    "EZTPA00": "easytrieve", "EZTPLUS": "easytrieve",
    "SAS": "sas", "SASHOST": "sas",
}

# IMS utilities that WRITE the database named in PARM=(ULU,util,dbd)
_IMS_DBD_WRITERS = {"DFSURGL0", "DFSURRL0", "DFSURPR0"}

_RUN_PROGRAM = re.compile(r"\bRUN\s+PROGRAM\s*\(\s*([A-Z0-9@#$]{1,8})\s*\)", re.IGNORECASE)
_RUN_PLAN = re.compile(r"\bPLAN\s*\(\s*([A-Z0-9@#$]{1,8})\s*\)", re.IGNORECASE)
_RUN_PARMS = re.compile(r"\bPARMS?\s*\(\s*'([^']*)'\s*\)", re.IGNORECASE)
_DSN_SYSTEM = re.compile(r"\bDSN\s+SYSTEM\s*\(\s*([A-Z0-9@#$]{1,4})\s*\)", re.IGNORECASE)
# TSO batch without DB2: CALL 'PROD.LOAD(CLMFIX)' 'parm'
_TSO_CALL = re.compile(r"\bCALL\s+'([A-Z0-9@#$.]+)\(([A-Z0-9@#$]{1,8})\)'", re.IGNORECASE)
# Values the scheduler or the system fills in at submit time: Control-M %%ODATE,
# CA-7 #JI, TWS/OPC &OYYMMDD (caught as a residual & symbol).
_SCHED_VAR = re.compile(r"%%[A-Z0-9$#@_]+\.?|#J[IO][A-Z0-9]*", re.IGNORECASE)
# EXEC PROC= operands that are STEP overrides, qualified (PARM.PS1=) or bare.
_STEP_KEYWORDS = {"PARM", "COND", "TIME", "REGION", "ACCT", "MEMLIMIT"}

# Symbol precedence inside a called procedure. z/OS: a value on the EXEC
# statement, else the PROC statement default, else a SET value. Flip this
# (and re-run the tests) only after checking one job on the real system:
#   //X PROC HLQ=TEST / //D DD DSN=&HLQ..A   called by   // SET HLQ=PROD / EXEC X
#   -> JESYSMSG allocates TEST.A (PROC default wins) or PROD.A (SET wins).
PROC_DEFAULT_BEATS_SET = True

# System PROCs that are rarely in the estate folder but whose EXEC says it
# all: EXEC DLIBATCH,MBR=CLMPOST,PSB=CLMPSB. (proc -> (launcher, region))
_SYSTEM_PROCS = {
    "DLIBATCH": ("DFSRRC00", "DLI"), "IMSBATCH": ("DFSRRC00", "DLI"), "DBBBATCH": ("DFSRRC00", "DBB"),
    "IMSBMP": ("DFSRRC00", "BMP"), "IMSBATCH2": ("DFSRRC00", "DLI"), "DFSMPR": ("DFSRRC00", "MPP"),
    "DSNUPROC": ("DSNUTILB", None),
}


def _system_proc_step(s: "StepFact", job: "JclFacts") -> Optional["StepFact"]:
    """An effective step for a well-known IMS/DB2 PROC that is not indexed,
    built from the EXEC's symbolic overrides. Reported as `proc_synthesised`
    so the reader knows the PROC's own DDs (IMS logs, RECON, STEPLIB) are
    not in the index."""
    name = (s.proc_called or "").upper()
    if name not in _SYSTEM_PROCS:
        return None
    launcher, region = _SYSTEM_PROCS[name]
    ov = {k.upper(): v for k, v in s.sym_overrides.items()}
    if launcher == "DFSRRC00":
        mbr, psb = ov.get("MBR") or ov.get("PGM") or "", ov.get("PSB") or ""
        if not mbr:
            return None
        parm = f"{region},{mbr}{',' + psb if psb else ''}"
    else:
        parm = f"{ov.get('SYSTEM', '')},{ov.get('UID', '')}"
    first = "G" if launcher == "DFSRRC00" else "DSNUPROC"
    eff = replace(s, step_name=f"{s.step_name}.{first}", from_proc=name, parent_step=s.step_name,
                  pgm=launcher, proc_called=None, parm=parm, dds=[], notes=[], sym_overrides={}, step_overrides={})
    for d in s.dds:                               # the job's //S1.G.DD overrides become the step's DDs
        dn = d.dd_name.upper().split(".")[-1]
        eff.dds.append(replace(d, dd_name=dn, is_override=True))
    _resolve_effective_pgm(eff, job)
    eff.notes.append(f"PROC {name} synthesised from EXEC overrides (the PROC member is not indexed)")
    job.unresolved.append(("proc_synthesised", f"{s.step_name}: system PROC {name} not indexed - step built from "
                                               f"MBR=/PSB= overrides; the PROC's own DDs are unknown", s.line))
    return eff

# DFSRRC00 PARM=(DLI,pgm,psb,...)  /  (BMP,pgm,psb,...)  /  (MPP,...)
_IMS_PARM = re.compile(
    r"^\(?\s*(DLI|BMP|MPP|IFP|ULU|DBB|IMS)\s*,\s*([A-Z0-9@#$]{1,8})\s*(?:,\s*([A-Z0-9@#$]{1,8}))?",
    re.IGNORECASE,
)

_GDG_REL = re.compile(r"\(\s*([+-]?\d+)\s*\)\s*$")
_MEMBER_REF = re.compile(r"\(\s*([A-Z0-9@#$]{1,8})\s*\)\s*$", re.IGNORECASE)
# `&&TEMP` is a temporary dataset, not a symbolic: the lookbehind keeps it intact.
_SYMBOL = re.compile(r"(?<!&)&([A-Z@#$][A-Z0-9@#$]{0,7})\.?", re.IGNORECASE)
_INCLUDE = re.compile(r"^//\S*\s+INCLUDE\s+MEMBER=([A-Z0-9@#$]{1,8})", re.IGNORECASE)

# Operands on an EXEC that are NOT symbolic overrides.
_EXEC_KEYWORDS = {"PROC", "PGM", "PARM", "COND", "REGION", "TIME", "ACCT", "ADDRSPC",
                  "DYNAMNBR", "PERFORM", "RD", "MEMLIMIT", "CCSID", "TVSMSG", "TVSAMCOM"}


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

@dataclass
class DdFact:
    dd_name: str
    concat_seq: int
    dsn: Optional[str]
    dsn_resolved: Optional[str]
    gdg_rel: Optional[str]
    disp: Optional[str]
    mode: str               # input|output|mod|sysout|dummy|unknown
    mode_source: str        # what decided `mode` - see _direction()
    sysin_text: Optional[str]
    is_override: bool
    line: int
    card_member: Optional[str] = None   # DSN=LIB(MEMBER): the member whose text became sysin_text
    is_temp: bool = False               # &&TEMP - exists only between steps of THIS job
    referback: Optional[str] = None     # DSN=*.STEP.DD as written; resolved after all steps are known


@dataclass
class StepFact:
    ordinal: int
    step_name: str
    pgm: Optional[str]
    proc_called: Optional[str]
    effective_pgm: Optional[str]
    launcher: Optional[str]
    parm: Optional[str]
    cond: Optional[str]
    line: int
    dds: List[DdFact] = dc_field(default_factory=list)
    notes: List[str] = dc_field(default_factory=list)
    from_proc: Optional[str] = None      # set on EFFECTIVE steps produced by expand_job
    parent_step: Optional[str] = None    # the job step whose EXEC PROC= produced it
    sym_overrides: Dict[str, str] = dc_field(default_factory=dict)   # EXEC PROC=X,SYM=value
    step_overrides: Dict[str, str] = dc_field(default_factory=dict)  # PARM.PS1=, COND.PS2=, bare PARM= on EXEC PROC
    guard: Optional[str] = None          # enclosing // IF (...) THEN / ELSE: the step runs only when true
    also_runs: List[str] = dc_field(default_factory=list)            # 2nd.. RUN PROGRAM() in one SYSTSIN
    submits: List[str] = dc_field(default_factory=list)              # jobs written to SYSOUT=(x,INTRDR)
    # A job step with a DD referring back into a PROC's step (DSN=*.S1.SRT010.SYSIN) is read from its cards when the
    # job is parsed, before the PROC's steps are known: the step as it was before that reading and the unresolved
    # rows the reading added, for expand_job to read it again once the referback is resolved (_read_again).
    first_reading: Optional[Tuple["StepFact", List[Tuple[str, str, int]]]] = dc_field(default=None, repr=False,
                                                                                     compare=False)


@dataclass
class JclFacts:
    job_name: Optional[str]
    job_line: int
    is_proc: bool
    proc_name: Optional[str]
    symbolics: Dict[str, str]            # everything seen: PROC defaults, SET, EXEC overrides
    steps: List[StepFact] = dc_field(default_factory=list)
    unresolved: List[Tuple[str, str, int]] = dc_field(default_factory=list)  # kind, detail, line
    set_symbols: Dict[str, str] = dc_field(default_factory=dict)     # instream SET only
    includes: List[str] = dc_field(default_factory=list)             # INCLUDE members spliced in
    instream_procs: Dict[str, "JclFacts"] = dc_field(default_factory=dict)   # // PROC ... // PEND inside a job
    job_dds: List[DdFact] = dc_field(default_factory=list)           # JOBLIB / DDs coded before the first EXEC
    jcllib: List[str] = dc_field(default_factory=list)               # // JCLLIB ORDER=(...) search order
    job_cond: Optional[str] = None                                   # COND= on the JOB card: applies to every step


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

def parse_jcl(text: str, data: bytes = b"", enc: str = "utf-8",
              extra_symbols: Optional[Dict[str, str]] = None,
              include_lookup: Optional[Callable[[str], Optional[str]]] = None,
              member_lookup: Optional[Callable[[str], Optional[str]]] = None) -> JclFacts:
    """The first (normally the only) job or PROC in the member. See parse_jcl_all."""
    return parse_jcl_all(text, data, enc, extra_symbols, include_lookup, member_lookup)[0]


def parse_jcl_all(text: str, data: bytes = b"", enc: str = "utf-8",
                  extra_symbols: Optional[Dict[str, str]] = None,
                  include_lookup: Optional[Callable[[str], Optional[str]]] = None,
                  member_lookup: Optional[Callable[[str], Optional[str]]] = None) -> List[JclFacts]:
    """Parse one JCL or PROC member; one JclFacts per JOB card.

    A member holding a job stream (JOBA then JOBB) yields two jobs - the
    second JOB card never overwrites the first job's name with all steps
    merged under it.

    include_lookup(name) -> text of an INCLUDE member (spliced in first).
    member_lookup(name)  -> text of a control-card member, for
                            `//SYSIN DD DSN=PROD.PARMLIB(SRTCLM)`: the cards a
                            step runs on are then known exactly as if instream.
    """
    included: List[str] = []
    if include_lookup is not None:
        text, included = _splice_includes(text, data, enc, include_lookup)
    stmts = read_jcl(text, data, enc)
    groups: List[List[JclStatement]] = [[]]
    for st in stmts:
        if st.op == "JOB" and any(s.op == "JOB" for s in groups[-1]):
            groups.append([])
        groups[-1].append(st)
    out: List[JclFacts] = []
    for g in groups:
        f = _parse_statements(g, extra_symbols, member_lookup)
        f.includes = list(included)
        out.append(f)
    return out


def _parse_statements(stmts: List[JclStatement], extra_symbols: Optional[Dict[str, str]],
                      member_lookup: Optional[Callable[[str], Optional[str]]]) -> JclFacts:
    job = JclFacts(job_name=None, job_line=0, is_proc=False,
                   proc_name=None, symbolics=dict(extra_symbols or {}))

    # EXEC/DD statements go to `target`: the member itself, or an instream
    # PROC (// PROC ... // PEND inside a job) while one is being collected.
    target: JclFacts = job
    inproc: Optional[JclFacts] = None
    cur: Optional[StepFact] = None
    ordinal = 0
    last_dd_name = ""
    concat_seq = 0
    guards: List[str] = []        # enclosing // IF conditions, innermost last

    for st in stmts:
        if st.op == "JOB":
            job.job_name = st.name or None
            job.job_line = st.start
            c = keyword_operands(st.operands).get("COND")
            if c:
                job.job_cond = c

        elif st.op == "JCLLIB":
            m = re.search(r"ORDER=\(?([^)]*)\)?", st.operands, re.IGNORECASE)
            if m:
                job.jcllib = [(_unquote(x.strip()) or "").upper() for x in m.group(1).split(",") if x.strip()]

        elif st.op == "IF":
            # `// IF (S1.RC > 4) THEN`: every step until ELSE/ENDIF runs only
            # when this is true - error, backout and notify steps are not the
            # normal flow of the job.
            guards.append("IF " + re.sub(r"\s+THEN\s*$", "", st.operands.strip(), flags=re.IGNORECASE))
        elif st.op == "ELSE":
            if guards:
                guards[-1] = "ELSE of " + guards[-1].replace("ELSE of ", "", 1)
        elif st.op == "ENDIF":
            if guards:
                guards.pop()

        elif st.op == "PROC":
            if job.job_name is not None or job.steps:
                # Instream PROC: its steps belong to the PROC, not to the job.
                inproc = JclFacts(job_name=None, job_line=st.start, is_proc=True,
                                  proc_name=(st.name or f"INPROC{len(job.instream_procs) + 1}").upper(),
                                  symbolics={})
                for k, v in keyword_operands(st.operands).items():
                    inproc.symbolics.setdefault(k, _unquote(v))
                job.instream_procs[inproc.proc_name] = inproc
                target, cur, ordinal = inproc, None, 0
            else:
                # A cataloged PROC's own statement carries its default symbolics.
                job.is_proc = True
                job.proc_name = st.name or job.proc_name
                for k, v in keyword_operands(st.operands).items():
                    job.symbolics.setdefault(k, _unquote(v))

        elif st.op == "PEND":
            if inproc is not None:
                # referbacks first: a card DD's DSN=*.S1.SYSIN brings the cards the step is read from
                _resolve_referbacks(inproc.steps, inproc, report=False, member_lookup=member_lookup)
                for step in inproc.steps:
                    _resolve_effective_pgm(step, inproc, member_lookup)
                inproc = None
                target, cur, ordinal = job, None, len(job.steps)

        elif st.op == "SET":
            # Instream SET wins over PROC defaults for everything after it.
            for k, v in keyword_operands(st.operands).items():
                target.symbolics[k] = _unquote(v)
                target.set_symbols[k] = _unquote(v)

        elif st.op == "EXEC":
            ordinal += 1
            cur = _build_step(st, ordinal, target)
            if guards:
                cur.guard = " AND ".join(guards)
            target.steps.append(cur)
            last_dd_name, concat_seq = "", 0

        elif st.op == "DD":
            if st.name:
                last_dd_name, concat_seq = st.name, 0
            else:
                concat_seq += 1          # unnamed DD = concatenation
            # A `&X` left in a JOB-level DSN is a run-time value (SET missing,
            # scheduler or system symbol): reported. Overrides of PROC DDs are
            # resolved again by expand_job with the PROC's symbolics, so they
            # are not reported here.
            report = target is job and not job.is_proc and not (cur is not None and cur.proc_called)
            d = _build_dd(st, last_dd_name, concat_seq, target, member_lookup, report_vars=report)
            if cur is not None:
                cur.dds.append(d)
            elif target is job:
                job.job_dds.append(d)    # JOBLIB / JOBCAT: coded before the first EXEC

        elif st.op == "INCLUDE":
            m = keyword_operands(st.operands).get("MEMBER")
            if m:
                job.unresolved.append(("include_member", _unquote(m), st.start))

    # Second pass: now that every DD is attached, resolve the referbacks that
    # point at earlier steps of this member, then unwrap the launchers - in
    # that order: `//SYSIN DD DSN=*.S1.SYSIN` reads the cards S1's SYSIN
    # names, and the step is read from them (LESSONS 229). A referback into a
    # step of a PROC the job runs is resolved by expand_job, which reads the
    # step again then (first_reading).
    _resolve_referbacks(job.steps, job, report=not any(s.proc_called for s in job.steps),
                        member_lookup=member_lookup)
    expands = not job.is_proc and any(s.proc_called for s in job.steps)
    for step in job.steps:
        if expands and any(d.referback and not d.dsn_resolved for d in step.dds):
            before = replace(step, dds=list(step.dds), notes=list(step.notes), submits=list(step.submits),
                             also_runs=list(step.also_runs))
            n = len(job.unresolved)
            _resolve_effective_pgm(step, job, member_lookup)
            step.first_reading = (before, job.unresolved[n:])
        else:
            _resolve_effective_pgm(step, job, member_lookup)
    _apply_joblib(job.steps, job.job_dds)
    # A PROC's steps run inside one job too: its own rows (default symbolics)
    # read a (+1) its earlier step wrote, as the expanded steps do.
    _same_job_generations(job.steps)
    for p in job.instream_procs.values():
        _same_job_generations(p.steps)
    return job


def _apply_joblib(steps: List[StepFact], job_dds: List[DdFact]) -> None:
    """A step without STEPLIB loads its program from JOBLIB: copy those DDs
    onto the step (mode_source 'joblib') so "which load library - which
    compiled version - ran" is answerable per step."""
    joblib = [d for d in job_dds if d.dd_name.upper() == "JOBLIB"]
    if not joblib:
        return
    for s in steps:
        if s.proc_called or any(d.dd_name.upper() == "STEPLIB" for d in s.dds):
            continue
        s.dds.extend(replace(d, mode_source="joblib", is_override=False) for d in joblib)


# --------------------------------------------------------------------------
# step / dd construction
# --------------------------------------------------------------------------

def _build_step(st: JclStatement, ordinal: int, facts: JclFacts) -> StepFact:
    ops = st.operands
    kw = keyword_operands(ops)
    positional = [t for t in split_operands(ops) if "=" not in t]

    pgm = kw.get("PGM")
    proc_called = kw.get("PROC")
    if not pgm and not proc_called:
        # `EXEC MYPROC` - bare positional operand names a PROC.
        if positional:
            proc_called = positional[0]

    # On an EXEC PROC=: PARM.PS1= / COND.PS2= / bare PARM= are STEP overrides
    # (applied to the PROC's steps by expand_job); any other KEY=VALUE is a
    # symbolic override. Neither leaks into the job's own later steps - an
    # override lives inside the PROC call only.
    overrides: Dict[str, str] = {}
    step_ov: Dict[str, str] = {}
    if proc_called:
        for k, v in kw.items():
            head = k.split(".")[0]
            if head in _STEP_KEYWORDS:
                step_ov[k] = (_unquote(v) or "") if head == "PARM" else v
            elif k not in _EXEC_KEYWORDS:
                overrides[k] = _unquote(v) or ""

    parm = _unquote(kw.get("PARM", "")) or None
    pgm_s = _unquote(pgm) if pgm else None
    if not facts.is_proc:
        # A job's SET symbols apply to its own PARM/PGM (`PARM='&RUNDT'`,
        # `PGM=DFSRRC00,PARM='DLI,&PGM,&PSB'`). PROC members keep the raw
        # text: expand_job substitutes with the calling job's values.
        if parm:
            parm = substitute_symbols(parm, facts.symbolics)
        if pgm_s:
            pgm_s = substitute_symbols(pgm_s, facts.symbolics)

    return StepFact(
        ordinal=ordinal,
        step_name=st.name or f"STEP{ordinal:03d}",
        pgm=pgm_s,
        proc_called=_unquote(proc_called) if proc_called else None,
        effective_pgm=None,
        launcher=None,
        parm=parm,
        cond=kw.get("COND"),
        line=st.start,
        sym_overrides=overrides,
        step_overrides=step_ov,
    )


# Libraries whose (MEMBER) is a load module or macro, never control cards.
_NOT_CARD_DDS = {"STEPLIB", "JOBLIB", "SYSLMOD", "SYSLIB", "DFSRESLB", "IMSACB"}
# DDs that carry control cards: a sequential dataset here is a card file.
_CARD_DDS = {"SYSIN", "SYSTSIN", "TOOLIN", "SYMNAMES", "DFSPARM", "$ORTPARM", "SORTCNTL", "INPUT", "SYSIN2", "PARMIN",
             "CARDIN", "CTLCARDS", "DFSVSAMP"}


def _build_dd(st: JclStatement, dd_name: str, concat_seq: int,
              facts: JclFacts,
              member_lookup: Optional[Callable[[str], Optional[str]]] = None,
              report_vars: bool = False) -> DdFact:
    kw = keyword_operands(st.operands)
    raw_dsn = kw.get("DSN") or kw.get("DSNAME")
    disp = kw.get("DISP")

    dsn = _unquote(raw_dsn) if raw_dsn else None
    resolved, gdg, referback, card_member, is_temp = None, None, None, None, False
    sysin = "\n".join(st.inline_data) if st.inline_data else None

    if not dsn and kw.get("PATH"):
        # A USS file (FTP landing zone, BPXBATCH input) is lineage too.
        path = _unquote(kw["PATH"]) or ""
        opts = (kw.get("PATHOPTS") or "").upper()
        mode = ("both" if "ORDWR" in opts else "input" if "ORDONLY" in opts
                else "output" if any(o in opts for o in ("OWRONLY", "OCREAT", "OAPPEND", "OTRUNC")) else "unknown")
        return DdFact(dd_name=dd_name, concat_seq=concat_seq, dsn=path, dsn_resolved=path, gdg_rel=None,
                      disp=disp, mode=mode, mode_source="pathopts" if mode != "unknown" else "undetermined",
                      sysin_text=sysin, is_override="." in dd_name, line=st.start)

    if dsn and dsn.startswith("*."):
        # DSN=*.STEP.DD reuses what an earlier step allocated: resolved once
        # every step of the job is known (_resolve_referbacks).
        referback = dsn
    elif dsn:
        resolved = substitute_symbols(dsn, facts.symbolics)
        if not facts.is_proc:
            # Job level: a symbol still unresolved here is filled in at
            # submit time (missing SET, scheduler %%ODATE / &LYYMMDD, system
            # symbol). Normalised to <VAR> so writer and reader of a
            # date-stamped extract still join; the raw text is kept in `dsn`.
            resolved, found = _variables(resolved)
            if report_vars:
                for kind, tok in found:
                    facts.unresolved.append((kind, f"{dd_name}: {tok} in {dsn} is supplied at run time "
                                                   f"(SET / scheduler / system symbol), not by this JCL", st.start))
        resolved, gdg = _strip_gdg(resolved)
        # &&TEMP lives only between the steps of THIS job. It is not a dataset
        # another job can read, so it must never join two jobs' lineage.
        is_temp = resolved.startswith("&&")
        card_member, body, by_last = _card_source(dd_name, resolved, gdg, member_lookup if sysin is None else None)
        if body is not None:
            sysin = body
        if by_last:
            facts.unresolved.append(("card_seq_assumed", f"{dd_name}: cards taken from member {card_member} "
                                                         f"matching the last qualifier of {resolved}", st.start))

    # `//STEP1.DD1 DD ...` overrides a DD inside a called PROC.
    is_override = "." in dd_name

    mode, mode_src = _direction(dd_name, gdg, disp, st.operands)

    return DdFact(
        dd_name=dd_name,
        concat_seq=concat_seq,
        dsn=dsn,
        dsn_resolved=resolved,
        gdg_rel=gdg,
        disp=disp,
        mode=mode,
        mode_source=mode_src,
        sysin_text=sysin,
        is_override=is_override,
        line=st.start,
        card_member=card_member,
        is_temp=is_temp,
        referback=referback,
    )


def _card_source(dd_name: str, resolved: str, gdg: Optional[str],
                 member_lookup: Optional[Callable[[str], Optional[str]]]) -> Tuple[Optional[str], Optional[str], bool]:
    """(card member, its text, taken by the last qualifier?) for a DD's
    resolved DSN. The text is None when the member is not indexed or no
    lookup is given (the DD's cards are instream).

    DSN=PROD.PARMLIB(SRTCLM): the control cards live in a member, not
    instream. If that member is indexed, its text becomes this DD's cards, so
    launchers, sort fields and IDCAMS ops resolve exactly as for `DD *`.
    Without this, most production steps have no cards. Cards in a SEQUENTIAL
    dataset (//SYSIN DD DSN=PROD.CLAIMS.SORTCLM) are fetched as a file named
    by the last qualifier - matched by that name, and said so by the caller,
    because it is an assumption."""
    base = dd_name.upper().split(".")[-1]
    if resolved.startswith("/"):
        return None, None, False             # a USS path (PATH=, a referback to one) is no card member
    m = _MEMBER_REF.search(resolved)
    if m and not m.group(1)[0].isdigit() and base not in _NOT_CARD_DDS:
        name = m.group(1).upper()
        return name, (member_lookup(name) if member_lookup is not None else None), False
    if member_lookup is not None and not gdg and base in _CARD_DDS:
        last = resolved.rsplit(".", 1)[-1]
        if 1 <= len(last) <= 8:
            body = member_lookup(last)
            if body is not None:
                return last, body, True
    return None, None, False


# DD names whose direction is fixed by the utility that reads them.
_INPUT_DDS = {"SORTIN", "SYSUT1", "INFILE", "SYSIN", "SYSTSIN", "STEPLIB", "JOBLIB", "TOOLIN", "SYMNAMES",
              "SYSLIB", "IMSACB", "DFSRESLB", "DFSVSAMP", "IEFRDER", "SYSLMOD"}
_OUTPUT_DDS = {"SORTOUT", "SYSUT2", "OUTFILE", "SYSPRINT", "SYSOUT", "SYSUDUMP",
               "SYSABEND", "CEEDUMP"}


def _utility_side(base: str) -> Optional[str]:
    """'input' / 'output' for the DD names whose direction the utility itself
    fixes whatever dataset they name - SORT reads SORTIN (SORTIN01...) and
    writes SORTOUT / SORTOFxx, IEBGENER reads SYSUT1 and writes SYSUT2 - else
    None. Where a relative generation number says the other way, these
    decide: a sort never writes its SORTIN, even when the DSN reads (+1), and
    never reads its SORTOUT, even when it reads (0) (LESSONS 223)."""
    if base == "SYSUT1" or base.startswith("SORTIN"):
        return "input"
    if base in ("SORTOUT", "SYSUT2") or base.startswith("SORTOF"):
        return "output"
    return None


def _direction(dd_name: str, gdg: Optional[str], disp: Optional[str],
               operands: str) -> Tuple[str, str]:
    """Decide read/write direction, and record WHAT decided it.

    DISP is NOT direction. DISP is serialisation and cataloguing. DISP=OLD is
    routinely coded on a pre-allocated output dataset, and DISP=SHR datasets
    are written all day (VSAM update-in-place is everywhere in insurance).
    Derive direction from DISP alone and the batch lineage graph comes out
    with arrows pointing the wrong way - and it looks fine.

    Signals, strongest first:
      1. GDG relative generation: (+1) is created here, (0)/(-n) is read -
         unless the DD is one whose direction the utility fixes and it says
         the other way (_utility_side: a sort never writes its SORTIN).
         A (+1) named again by a later step of the same job is the SAME new
         generation - decided over the whole job, once every step is known
         (_same_job_generations: 'gdg_same_job').
      2. Utility DD-name conventions: SORTIN/SYSUT1 read, SORTOUT/SYSUT2 write.
      3. DISP=NEW / MOD - weak corroboration only.
      4. The program's own OPEN INPUT/OUTPUT/I-O verb, joined through
         SELECT...ASSIGN to this DD name. That join is applied later, in
         build.py, and OVERRIDES everything above because it is the only
         signal that reflects what the code actually does.
    """
    up = (operands or "").upper()
    if "SYSOUT=" in up:
        if "INTRDR" in up:
            return "submit", "intrdr"        # SYSOUT=(A,INTRDR): the step SUBMITS a job
        return "sysout", "sysout"
    # DUMMY is the first positional operand, never a substring: a dataset
    # named PROD.CLM.DUMMY.FILE is real. DSN=NULLFILE is DUMMY's synonym.
    toks = split_operands(operands or "")
    if toks and toks[0].strip().upper() == "DUMMY":
        return "dummy", "dummy"
    if re.search(r"\bDSN(?:AME)?=NULLFILE\b", up):
        return "dummy", "dummy"

    base = dd_name.upper().split(".")[-1]
    if gdg is not None:
        try:
            g = int(gdg)
        except ValueError:
            g = None
        if g is not None:
            mode = "output" if g > 0 else "input"
            side = _utility_side(base)
            if side is not None and side != mode:
                return side, "dd_convention"
            return mode, "gdg_relative"

    if base in _INPUT_DDS or base.startswith("SORTIN"):
        return "input", "dd_convention"
    if base in _OUTPUT_DDS or base.startswith("SORTOF"):
        return "output", "dd_convention"

    if disp:
        status = disp.strip().lstrip("(").split(",")[0].strip().upper()
        if status == "NEW":
            return "output", "disp_new_weak"
        if status == "MOD":
            return "mod", "disp_mod_weak"
    return "unknown", "undetermined"


_GDG_ABS = re.compile(r"\.G(\d{4})V(\d{2})$", re.IGNORECASE)


def _strip_gdg(dsn: str) -> Tuple[str, Optional[str]]:
    m = _GDG_REL.search(dsn)
    if m:
        return _GDG_REL.sub("", dsn).strip(), m.group(1)
    # Absolute generation PROD.G.G0012V00 (restart/rerun JCL) is the same
    # GDG as PROD.G(0): the rerun job is a reader of the GDG.
    m = _GDG_ABS.search(dsn)
    if m:
        return dsn[:m.start()], f"G{m.group(1)}V{m.group(2)}"
    return dsn, None


def substitute_symbols(value: str, symbols: Dict[str, str], depth: int = 0) -> str:
    """Replace &SYM / &SYM. references. Unknown symbols are LEFT IN PLACE.

    Leaving them in place is deliberate: a half-resolved DSN is visibly
    half-resolved, and gets reported as unresolved rather than quietly joining
    the dataset graph under a wrong name.
    """
    if depth > 5 or "&" not in value:
        return value

    def repl(m):
        name = m.group(1).upper()
        if name in symbols:
            return symbols[name]
        return m.group(0)

    out = _SYMBOL.sub(repl, value)
    # '&HLQ..LOADLIB' -> after substitution 'PROD.POLICY..LOADLIB'; the doubled
    # dot is the JCL delimiter convention, collapse it.
    out = re.sub(r"(?<!^)\.\.", ".", out)
    if out != value:
        return substitute_symbols(out, symbols, depth + 1)
    return out


# --------------------------------------------------------------------------
# the important bit: what actually runs
# --------------------------------------------------------------------------

def _resolve_effective_pgm(step: StepFact, facts: JclFacts,
                           member_lookup: Optional[Callable[[str], Optional[str]]] = None) -> None:
    pgm = (step.pgm or "").upper()
    if not pgm:
        step.effective_pgm = None
        if step.proc_called:
            step.notes.append(f"runs PROC {step.proc_called}; steps come from the PROC member")
        return

    # SYSOUT=(x,INTRDR): whatever this step writes there is SUBMITTED as a
    # job - a scheduling edge written in JCL, not in the scheduler.
    if any(d.mode == "submit" for d in step.dds):
        for d in step.dds:
            if d.mode == "submit":
                continue
            text = d.sysin_text or ""
            for mj in re.finditer(r"^//([A-Z0-9@#$]{1,8})\s+JOB\b", text, re.IGNORECASE | re.MULTILINE):
                if mj.group(1).upper() not in step.submits:
                    step.submits.append(mj.group(1).upper())
            if d.card_member and not text and d.card_member not in step.submits:
                step.submits.append(d.card_member)          # the member name = the job name (assumption)
        if step.submits:
            step.notes.append("submits job(s) via INTRDR: " + ", ".join(step.submits))
        else:
            facts.unresolved.append(("intrdr", f"{step.step_name}: writes to INTRDR but the submitted JCL "
                                               f"is not visible (built at run time?)", step.line))

    kind = LAUNCHERS.get(pgm)
    if kind is None:
        step.effective_pgm = pgm       # an ordinary application program
        return

    step.launcher = pgm

    if kind == "tso":
        sysin = _dd_text(step, "SYSTSIN")
        if sysin:
            runs = list(_RUN_PROGRAM.finditer(sysin))
            call = _TSO_CALL.search(sysin)
            if runs:
                step.effective_pgm = runs[0].group(1).upper()
                plan = _RUN_PLAN.search(sysin)
                sub = _DSN_SYSTEM.search(sysin)
                parms = _RUN_PARMS.search(sysin)
                if plan:
                    step.notes.append(f"DB2 plan {plan.group(1).upper()}")
                if sub:
                    step.notes.append(f"DB2 subsystem {sub.group(1).upper()}")
                if parms:
                    step.notes.append(f"RUN PARMS('{parms.group(1)[:80]}')")
                # Two RUN PROGRAMs in one DSN session: both run, in order.
                step.also_runs = [r.group(1).upper() for r in runs[1:]]
                if step.also_runs:
                    step.notes.append("also runs " + ", ".join(step.also_runs) + " in the same DSN session")
                if LAUNCHERS.get(step.effective_pgm) == "db2util":
                    # RUN PROGRAM(DSNTIAUL): a utility, not an application;
                    # the SQL in SYSIN says which tables it unloads/runs.
                    step.effective_pgm = f"*{step.effective_pgm}*"
                    step.notes.append("DB2 utility: tables come from the SQL in SYSIN")
                return
            if call:
                step.effective_pgm = call.group(2).upper()
                step.notes.append(f"TSO CALL from library {call.group(1).upper()}")
                return
            step.notes.append("TSO step with no RUN PROGRAM(...) / CALL - may be TSO commands only")
        facts.unresolved.append(
            ("launcher_parm", f"{step.step_name}: IKJEFT01 program not found in SYSTSIN", step.line))

    elif kind == "ims":
        parm = step.parm or ""
        m = _IMS_PARM.search(parm)
        if m:
            region = m.group(1).upper()
            step.effective_pgm = m.group(2).upper()
            if region in ("ULU", "UDR") and m.group(3):
                # Utility regions name a DATABASE in the third position, not
                # a PSB: image copy / unload / reload jobs belong to the DBD.
                dbd = m.group(3).upper()
                step.notes.append(f"DBD {dbd} (utility region {region}: the third value is the database, not a PSB)")
                mode = "output" if step.effective_pgm in _IMS_DBD_WRITERS else "input"
                step.dds.append(DdFact(dd_name="*DBD*", concat_seq=0, dsn=dbd, dsn_resolved=dbd, gdg_rel=None,
                                       disp=None, mode=mode, mode_source="ims_utility", sysin_text=None,
                                       is_override=False, line=step.line))
            elif m.group(3):
                step.notes.append(f"PSB {m.group(3).upper()}")
            step.notes.append(f"IMS region type {region}")
            return
        facts.unresolved.append(
            ("launcher_parm", f"{step.step_name}: DFSRRC00 PARM not parseable: {parm!r}", step.line))

    elif kind == "easytrieve":
        # The program IS the SYSIN text: FILE statements name the DDs and the
        # field definitions are byte positions, like sort cards.
        member = next((d.card_member for d in step.dds if d.dd_name.upper().endswith("SYSIN") and d.card_member), None)
        step.effective_pgm = f"*EZT:{member}*" if member else "*EASYTRIEVE*"
        ctl = _dd_text(step, "SYSIN")
        if ctl:
            roles = easytrieve_dds(ctl)
            for i, d in enumerate(step.dds):
                r = roles.get(d.dd_name.upper())
                if r and d.mode not in ("sysout", "dummy"):
                    step.dds[i] = replace(d, mode=r, mode_source="easytrieve_file")
            step.notes.append("Easytrieve program in SYSIN: files and byte-position fields harvested")
        else:
            facts.unresolved.append(
                ("launcher_parm", f"{step.step_name}: Easytrieve with no SYSIN program text", step.line))

    elif kind == "sas":
        step.effective_pgm = "*SAS*"
        if _dd_text(step, "SYSIN"):
            step.notes.append("SAS program in SYSIN (not parsed: DD roles from DISP/OPEN only)")
        else:
            facts.unresolved.append(
                ("launcher_parm", f"{step.step_name}: SAS with no SYSIN program text", step.line))

    elif kind == "sort":
        step.effective_pgm = "*SORT*"
        # ICETOOL reads TOOLIN and xxxxCNTL; DFSPARM / $ORTPARM override
        # SYSIN; SYMNAMES defines symbols - all of them are the cards.
        parts = [d.sysin_text for d in step.dds if d.sysin_text and
                 (d.dd_name.upper().split(".")[-1] in ("SYSIN", "TOOLIN", "DFSPARM", "$ORTPARM", "SORTCNTL", "SYMNAMES")
                  or d.dd_name.upper().endswith("CNTL"))]
        ctl = "\n".join(parts) if parts else None
        if ctl:
            step.notes.append("sort behaviour defined by control cards")
            roles = sort_dd_roles(ctl)
            for i, d in enumerate(step.dds):
                r = roles.get(d.dd_name.upper().split(".")[-1])
                if r and d.mode not in ("sysout", "dummy") and d.mode_source != "open_verb":
                    step.dds[i] = replace(d, mode=r, mode_source="sort_card")
            if re.search(r"\bSYMNAMES\b", " ".join(d.dd_name for d in step.dds), re.IGNORECASE) and not sort_symbols(ctl):
                facts.unresolved.append(("sort_symbols", f"{step.step_name}: SYMNAMES DD present but its member is "
                                                         f"not indexed - symbolic field positions unknown", step.line))
        else:
            facts.unresolved.append(
                ("launcher_parm", f"{step.step_name}: SORT/ICETOOL with no cards in SYSIN/TOOLIN/xxxxCNTL "
                                  f"(control cards are in a dataset that is not indexed)", step.line))

    elif kind == "idcams":
        step.effective_pgm = "*IDCAMS*"
        ctl = _dd_text(step, "SYSIN")
        if ctl:
            for verb in ("DEFINE", "REPRO", "DELETE", "LISTCAT", "PRINT", "EXPORT", "IMPORT"):
                if re.search(rf"\b{verb}\b", ctl, re.IGNORECASE):
                    step.notes.append(f"IDCAMS {verb}")
        else:
            facts.unresolved.append(
                ("launcher_parm", f"{step.step_name}: IDCAMS with no inline SYSIN", step.line))

    elif kind == "copy":
        step.effective_pgm = "*COPY*"
        step.notes.append("plain copy SYSUT1 -> SYSUT2")

    elif kind == "noop":
        step.effective_pgm = "*NOOP*"
        step.notes.append("IEFBR14: the DD statements do the work (allocate/delete)")
        # The cleanup step is not a writer: DISP=(MOD,DELETE) deletes,
        # DISP=(NEW,CATLG) allocates an empty dataset, anything else does nothing.
        for i, d in enumerate(step.dds):
            if not d.dsn or d.mode in ("sysout", "dummy"):
                continue
            status, normal = _disp_parts(d.disp)
            if status != "NEW" and normal == "DELETE":
                step.dds[i] = replace(d, mode="delete", mode_source="iefbr14_disp")
            elif status == "NEW" and normal in ("CATLG", "KEEP"):
                step.dds[i] = replace(d, mode="alloc", mode_source="iefbr14_disp")
            else:
                step.dds[i] = replace(d, mode="none", mode_source="iefbr14_disp")

    elif kind == "db2util":
        step.effective_pgm = f"*{pgm}*"
        ctl = _dd_text(step, "SYSIN")
        if ctl:
            step.notes.append("DB2 utility driven by SYSIN")

    elif kind in ("ftp", "ndm", "usssh"):
        # These are the mainframe-to-non-mainframe boundary. The peer system,
        # direction and file names live in the SYSIN / process cards, so the
        # cards are the fact and the step is recorded as an interface.
        step.effective_pgm = f"*{kind.upper()}*"
        step.notes.append(f"external interface via {pgm}")
        # Batch FTP reads //INPUT by default; credentials in those cards must
        # never reach a pack.
        for i, d in enumerate(step.dds):
            if d.sysin_text and d.dd_name.upper().split(".")[-1] in ("INPUT", "SYSIN", "STDIN", "NETRC"):
                step.dds[i] = replace(d, sysin_text=_redact_credentials(d.sysin_text, kind))
        ctl = _dd_text(step, "SYSIN") or _dd_text(step, "INPUT") or _dd_text(step, "STDIN") or ""
        if kind == "ftp":
            host = (step.parm or "").strip().split()[0].strip("'(") if (step.parm or "").strip() else ""
            if host and not host.startswith("("):
                step.notes.append(f"FTP host {host}")
            for m in re.finditer(r"^[ \t]*(m?put|m?get|send|recv)[ \t]+(\S+)(?:[ \t]+(\S+))?", ctl, re.IGNORECASE | re.MULTILINE):
                verb = m.group(1).lower()
                out = verb.endswith("put") or verb == "send"
                # put 'MVS.DSN' remote  /  get remote 'MVS.DSN'
                mvs = m.group(2) if out else (m.group(3) or m.group(2))
                mvs = mvs.strip("'\"").upper()
                if "/" in mvs or "." not in mvs:          # a remote path, not an MVS name
                    step.notes.append(f"FTP {verb} {m.group(2)[:40]} ({'out' if out else 'in'})")
                    continue
                step.dds.append(DdFact(dd_name="*FTP*", concat_seq=0, dsn=mvs, dsn_resolved=mvs, gdg_rel=None,
                                       disp=None, mode="input" if out else "output",
                                       mode_source="ftp_put" if out else "ftp_get", sysin_text=None,
                                       is_override=False, line=step.line))
                step.notes.append(f"FTP {verb} {mvs} -> {host or 'peer'} ({'out' if out else 'in'})"
                                  if out else f"FTP {verb} {mvs} <- {host or 'peer'} (in)")
        elif kind == "ndm":
            # DMBATCH SUBMIT PROC=X: the process member holds the COPY FROM/TO.
            for m in re.finditer(r"\bSUBMIT\s+PROC=([A-Z0-9@#$]{1,8})", ctl, re.IGNORECASE):
                body = member_lookup(m.group(1).upper()) if member_lookup else None
                if body is not None:
                    ctl += "\n" + body
                    step.notes.append(f"Connect:Direct process {m.group(1).upper()} read from its member")
                else:
                    facts.unresolved.append(("ndm_process", f"{step.step_name}: Connect:Direct process member "
                                                            f"{m.group(1).upper()} not indexed", step.line))
            for m in re.finditer(r"\b(SNODE|PNODE)\s*=\s*([A-Z0-9@#$.-]+)", ctl, re.IGNORECASE):
                step.notes.append(f"Connect:Direct {m.group(1).upper()} {m.group(2)}")
            for m in re.finditer(r"\b(FROM|TO)\s*\(([^)]*)\)", ctl, re.IGNORECASE | re.S):
                out = m.group(1).upper() == "FROM"       # FROM here = the mainframe file is read and sent
                for dm in re.finditer(r"DSN\s*=\s*'?([A-Z0-9@#$.]+(?:\([^)]*\))?)'?", m.group(2), re.IGNORECASE):
                    dsn = dm.group(1).upper()
                    step.dds.append(DdFact(dd_name="*NDM*", concat_seq=0, dsn=dsn, dsn_resolved=dsn, gdg_rel=None,
                                           disp=None, mode="input" if out else "output",
                                           mode_source="ndm_process", sysin_text=None, is_override=False,
                                           line=step.line))
                    step.notes.append(f"Connect:Direct {'sends' if out else 'receives'} {dsn} ({'out' if out else 'in'})")
            for m in re.finditer(r"&DSN\s*=\s*'?([A-Z0-9@#$.]+)'?", ctl, re.IGNORECASE):
                dsn = m.group(1).upper()
                if not any(d.dsn_resolved == dsn for d in step.dds):
                    step.dds.append(DdFact(dd_name="*NDM*", concat_seq=0, dsn=dsn, dsn_resolved=dsn, gdg_rel=None,
                                           disp=None, mode="unknown", mode_source="ndm_symbolic", sysin_text=None,
                                           is_override=False, line=step.line))
                    step.notes.append(f"Connect:Direct process parameter &DSN={dsn} (direction in the process)")
        else:
            for m in re.finditer(r"\b(?:open|host|put|get|send|receive)\b\s*[=(]?\s*([^\s,()]+)", ctl, re.IGNORECASE):
                step.notes.append(f"interface detail: {m.group(0).strip()[:60]}")
        if not ctl:
            facts.unresolved.append(
                ("launcher_parm", f"{step.step_name}: {pgm} with no inline cards - "
                                  f"peer/direction unknown", step.line))

    else:
        step.effective_pgm = f"*{pgm}*"


def _dd_text(step: StepFact, name: str) -> Optional[str]:
    parts = [d.sysin_text for d in step.dds
             if d.dd_name.upper().endswith(name.upper()) and d.sysin_text]
    return "\n".join(parts) if parts else None


def _unquote(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _variables(resolved: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Replace run-time values in a DSN with <VAR>: scheduler tokens
    (%%ODATE, #JI) and any &SYM that survived substitution. Returns the
    normalised name and (unresolved kind, token) pairs. &&TEMP is untouched."""
    found: List[Tuple[str, str]] = []

    def sched(m: "re.Match[str]") -> str:
        found.append(("scheduler_symbol", m.group(0)))
        return "<VAR>"

    def sym(m: "re.Match[str]") -> str:
        found.append(("symbolic", m.group(0)))
        return "<VAR>"

    s = _SCHED_VAR.sub(sched, resolved)
    s = _SYMBOL.sub(sym, s)
    s = re.sub(r"(?:<VAR>)+", "<VAR>", s)
    return s, found


def _disp_parts(disp: Optional[str]) -> Tuple[str, str]:
    """(status, normal disposition) with JCL defaults: status NEW when omitted,
    normal DELETE for NEW and KEEP otherwise."""
    parts = [p.strip().upper() for p in (disp or "").strip().strip("()").split(",")]
    status = parts[0] if parts and parts[0] else "NEW"
    normal = parts[1] if len(parts) > 1 and parts[1] else ("DELETE" if status == "NEW" else "KEEP")
    return status, normal


# `[ \t]` not `\s` after `^`: with MULTILINE a `\s` runs across blank lines (quadratic - LESSONS 148)
_EZT_FILE = re.compile(r"^[ \t]*FILE[ \t]+([A-Z0-9@#$-]{1,8})\b(.*)$", re.IGNORECASE | re.MULTILINE)
_EZT_JOBIN = re.compile(r"^[ \t]*JOB[ \t]+INPUT[ \t]*\(?[ \t]*([A-Z0-9@#$-]{1,8})", re.IGNORECASE | re.MULTILINE)
_EZT_PUT = re.compile(r"^[ \t]*PUT[ \t]+([A-Z0-9@#$-]{1,8})", re.IGNORECASE | re.MULTILINE)
_EZT_GET = re.compile(r"^[ \t]*GET[ \t]+([A-Z0-9@#$-]{1,8})", re.IGNORECASE | re.MULTILINE)
# `  CLM-STAT  25  2  A` : name, start byte, length, type (A/N/P/B/W/K/U)
_EZT_FIELD = re.compile(r"^[ \t]+([A-Z0-9@#$:-]{1,40})[ \t]+(\d{1,5})[ \t]+(\d{1,5})[ \t]+([ANPBWKU])\b(.*)$",
                        re.IGNORECASE | re.MULTILINE)


def easytrieve_dds(text: str) -> Dict[str, str]:
    """DD name -> input|output from an Easytrieve program: JOB INPUT / GET
    read, PUT and FILE ... PRINTER write."""
    roles: Dict[str, str] = {}
    for m in _EZT_FILE.finditer(text):
        if re.search(r"\bPRINTER\b", m.group(2), re.IGNORECASE):
            roles[m.group(1).upper()] = "output"
        else:
            roles.setdefault(m.group(1).upper(), "unknown")
    for rx, role in ((_EZT_JOBIN, "input"), (_EZT_GET, "input"), (_EZT_PUT, "output")):
        for m in rx.finditer(text):
            name = m.group(1).upper()
            if name != "NULL":
                roles[name] = role if roles.get(name) in (None, "unknown") or roles[name] == role else "both"
    return roles


def easytrieve_fields(text: str) -> List[Tuple[str, int, int, str, str]]:
    """(card_kind, pos, length, fmt, raw) for Easytrieve field definitions -
    the same shape as sort cards, so a copybook offset change finds them."""
    out: List[Tuple[str, int, int, str, str]] = []
    fmt = {"A": "CH", "N": "ZD", "P": "PD", "B": "BI", "W": "CH", "K": "CH", "U": "CH"}
    for m in _EZT_FIELD.finditer(text):
        raw = m.group(0).strip()
        if raw.upper().startswith(("FILE ", "JOB ", "IF ", "PRINT ", "REPORT ", "LINE ", "TITLE ")):
            continue
        out.append(("EZT", int(m.group(2)), int(m.group(3)), fmt.get(m.group(4).upper(), m.group(4).upper()), raw))
    return out


_FTP_CMDS = {"ascii", "binary", "bin", "cd", "lcd", "put", "get", "mput", "mget", "quit", "bye", "close", "open",
             "dir", "ls", "delete", "rename", "site", "locsite", "sendsite", "quote", "passive", "epsv4", "pwd",
             "type", "mode", "struct", "prompt", "verbose", "sunique", "append", "mkdir", "rmdir", "cwd", "ebcdic"}


def _redact_credentials(text: str, kind: str) -> str:
    """Batch FTP cards carry the userid and password as bare lines, and
    Connect:Direct SIGNON carries them in parentheses. A pack of this step
    would otherwise paste them into a model. The shape is kept, the values
    are not."""
    out: List[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        low = s.lower()
        if kind == "ftp":
            if re.match(r"^(user|pass|password|acct|account)\b", low):
                out.append(s.split()[0] + " <redacted>")
                continue
            if s and " " not in s and low not in _FTP_CMDS and not s.startswith(("'", ";", "*")):
                out.append("<redacted>")            # bare userid / password lines
                continue
        elif kind == "ndm":
            if "SIGNON" in s.upper():
                out.append(re.sub(r"(USERID|PASS(?:WORD)?|PACCT|SACCT)\s*=\s*\([^)]*\)", r"\1=(<redacted>)",
                                  re.sub(r"(USERID|PASS(?:WORD)?)\s*=\s*[^\s,()]+", r"\1=<redacted>", s,
                                         flags=re.IGNORECASE), flags=re.IGNORECASE))
                continue
        out.append(ln)
    return "\n".join(out)


# --------------------------------------------------------------------------
# control-card extraction
# --------------------------------------------------------------------------

def control_card_params(text: str) -> Dict[str, str]:
    """Best-effort KEY=VALUE extraction from a control card deck.

    Shops invent their own control-card formats, so this is a starting point
    to be tuned to yours - but capturing the cards at all already puts you
    ahead, because a step's behaviour frequently lives here rather than in the
    COBOL. Anything not matching KEY=VALUE is preserved verbatim by the caller.
    """
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.startswith("//"):
            continue
        m = re.match(r"^([A-Z0-9_#@$-]{1,32})\s*[=:]\s*(.+)$", line, re.IGNORECASE)
        if m:
            out[m.group(1).upper()] = m.group(2).strip()
    return out


# --------------------------------------------------------------------------
# DFSORT / SYNCSORT control cards
# --------------------------------------------------------------------------

_SORT_CARD = re.compile(
    r"\b(SORT|MERGE|INCLUDE|OMIT|INREC|OUTREC|OUTFIL|JOINKEYS|SUM)\b", re.IGNORECASE)
# (pos,len[,fmt]) inside FIELDS=/COND=/BUILD=/OUTREC= lists. The format is
# optional: INREC/OUTREC/OUTFIL BUILD lists are plain (pos,len) pairs, while
# SORT FIELDS and INCLUDE/OMIT COND carry a format (or a FORMAT= default).
_CARD_TRIPLE = re.compile(
    r"(?<![\d.])(\d{1,5}),(\d{1,5})(?:,([A-Z][A-Z0-9]{0,3}))?(?=[,)\s]|$)", re.IGNORECASE)
_CARD_FORMAT = re.compile(r"\bFORMAT=([A-Z0-9]{1,4})\b", re.IGNORECASE)
_NEEDS_FMT = {"SORT", "MERGE", "INCLUDE", "OMIT", "SUM"}
# A word after (pos,len) that is no format: a relational operator (`INCLUDE COND=(13,2,NE,C'CN'),FORMAT=CH` - the
# FORMAT= gives it), AND / OR after the second field of a comparison (`(1,2,EQ,5,2,OR,...)`), and in a reformatting
# list the blank and binary-zero separators (`BUILD=(1,10,X,11,5)`) - LESSONS 244. A field an operator or a
# connective follows is still compared: it stays a field reference.
_REL_OPS = {"EQ", "NE", "GT", "GE", "LT", "LE"}
_NOT_A_FORMAT = _REL_OPS | {"AND", "OR", "X", "Z"}
# SYMNAMES: `POL-STATUS,45,1,CH` - a symbol used in the cards instead of the bytes.
_SYMNAME = re.compile(r"^([A-Z@#$_][A-Z0-9@#$_\-]*),(\d{1,5}),(\d{1,5})(?:,([A-Z][A-Z0-9]{0,3}))?\s*$", re.IGNORECASE)


def sort_symbols(text: str) -> Dict[str, str]:
    """SYMNAMES definitions in a card deck: name -> 'pos,len[,fmt]'."""
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        s = line[:71].strip()
        if not s or s.startswith("*"):
            continue
        m = _SYMNAME.match(s)
        if m:
            out[m.group(1).upper()] = ",".join(x for x in (m.group(2), m.group(3), (m.group(4) or "").upper()) if x)
    return out


def sort_card_fields(text: str) -> List[Tuple[str, int, int, str, str]]:
    """Byte positions a sort/merge step actually depends on.

    Returns (card_kind, pos_1based, length, format, raw_card). Sort cards are
    real business logic - filtering (INCLUDE/OMIT), reformatting (INREC/OUTREC/
    OUTFIL), field derivation - and they address the record BY BYTE POSITION.
    A copybook change that moves bytes silently breaks every one of these,
    which is why the impact query joins these positions against computed
    field offsets instead of hoping somebody remembers the sort step.
    SYMNAMES symbols (defined in the deck or a SYMNAMES DD/member joined into
    `text`) are substituted first; `c:p,l` column-positioned items count.
    """
    out: List[Tuple[str, int, int, str, str]] = []
    if not text:
        return out
    symbols = sort_symbols(text)
    # Join continued cards: a card continues when it ends with a comma.
    cards: List[str] = []
    buf = ""
    for line in text.splitlines():
        s = line[:71].rstrip()
        if not s.strip() or s.lstrip().startswith("*") or _SYMNAME.match(s.strip()):
            continue
        buf = (buf + " " + s.strip()) if buf else s.strip()
        if not buf.endswith(","):
            cards.append(buf)
            buf = ""
    if buf:
        cards.append(buf)

    for raw_card in cards:
        card = raw_card
        if symbols:
            def _sub(m: "re.Match[str]") -> str:
                return symbols.get(m.group(0).upper(), m.group(0))
            card = re.sub(r"(?<![A-Z0-9@#$_\-'])[A-Z@#$_][A-Z0-9@#$_\-]*(?![A-Z0-9@#$_\-'])", _sub, card, flags=re.IGNORECASE)
        card = re.sub(r"(?<![A-Z0-9])\d{1,5}:", "", card)       # OUTREC=(1:1,10,11:21,5) -> (1,10,21,5)
        mk = _SORT_CARD.search(card)
        if not mk:
            continue
        kind = mk.group(1).upper()
        fmt_default = None
        mf = _CARD_FORMAT.search(card)
        if mf:
            fmt_default = mf.group(1).upper()
        seen = set()
        for m in _CARD_TRIPLE.finditer(card):
            pos, ln = int(m.group(1)), int(m.group(2))
            fmt = (m.group(3) or "").upper()
            compared = fmt in _REL_OPS or fmt in ("AND", "OR")
            if fmt in _NOT_A_FORMAT:
                fmt = ""
            # With FORMAT=xx the triple is (pos,len,order) and 'fmt' is A/D.
            if fmt in ("A", "D", "") and fmt_default:
                fmt = fmt_default
            if not fmt and not compared and kind in _NEEDS_FMT:
                continue          # a bare pair in a SORT/INCLUDE list is not a field ref
            key = (pos, ln)
            if key in seen or pos == 0 or ln == 0:
                continue
            seen.add(key)
            out.append((kind, pos, ln, fmt or None, raw_card[:120]))
    return out


# Each lazy scan stops at the next statement of its kind: scanning to the end
# of the deck for every OUTFIL without FNAMES= was quadratic (LESSONS 148).
_ICETOOL_OPS = r"(?:SORT|COPY|MERGE|SELECT|SPLICE|COUNT|STATS|UNIQUE|OCCUR|DISPLAY|RANGE|VERIFY|RESIZE)"
_ICETOOL_OP = re.compile(r"\b" + _ICETOOL_OPS + r"\s+"
                         r"FROM\s*\(\s*([A-Z0-9@#$]+)\s*\)(?:(?:(?!\b" + _ICETOOL_OPS + r"\s+FROM\b).)*?\bTO\s*\(\s*([A-Z0-9@#$ ,]+)\s*\))?",
                         re.IGNORECASE)
_OUTFIL_NAMES = re.compile(r"\bOUTFIL\b(?:(?!\bOUTFIL\b).)*?\bFNAMES\s*=\s*\(?([A-Z0-9@#$ ,]+)\)?", re.IGNORECASE)
_JOINKEYS_F = re.compile(r"\bJOINKEYS\b(?:(?!\bJOINKEYS\b).)*?\bF[12]\s*=\s*([A-Z0-9@#$]+)", re.IGNORECASE)


def sort_dd_roles(text: str) -> Dict[str, str]:
    """DD name -> input|output from the cards themselves: OUTFIL FNAMES= (the
    split outputs of a one-pass extract), JOINKEYS F1=/F2= (the two inputs of
    a join), ICETOOL FROM()/TO()."""
    roles: Dict[str, str] = {}
    t = " ".join(ln[:71] for ln in (text or "").splitlines() if not ln.strip().startswith("*"))
    for m in _OUTFIL_NAMES.finditer(t):
        for dd in re.findall(r"[A-Z0-9@#$]+", m.group(1), re.IGNORECASE):
            roles[dd.upper()] = "output"
    for m in _JOINKEYS_F.finditer(t):
        roles[m.group(1).upper()] = "input"
    for m in _ICETOOL_OP.finditer(t):
        roles[m.group(1).upper()] = "input"
        if m.group(2):
            for dd in re.findall(r"[A-Z0-9@#$]+", m.group(2), re.IGNORECASE):
                roles[dd.upper()] = "output"
    return roles


# --------------------------------------------------------------------------
# INCLUDE members
# --------------------------------------------------------------------------

def _splice_includes(text: str, data: bytes, enc: str,
                     lookup: Callable[[str], Optional[str]], depth: int = 0) -> Tuple[str, List[str]]:
    """Replace `// INCLUDE MEMBER=X` with the member's records, as the reader
    does at submit time. Nested to depth 3."""
    out: List[str] = []
    names: List[str] = []
    for rec in _split_records(text, data, enc):
        m = _INCLUDE.match(rec[:71])
        if m and depth < 3:
            name = m.group(1).upper()
            body = lookup(name)
            if body is not None:
                names.append(name)
                out.append(f"//*      INCLUDE MEMBER={name} expanded by atlas")
                sub, subnames = _splice_includes(body, b"", enc, lookup, depth + 1)
                out.extend(sub.split("\n"))
                names.extend(subnames)
                continue
        out.append(rec)
    return "\n".join(out), names


# --------------------------------------------------------------------------
# PROC expansion: what a job ACTUALLY runs
# --------------------------------------------------------------------------

def expand_job(job: JclFacts, proc_lookup: Callable[[str], Optional["JclFacts"]],
               depth: int = 0, max_depth: int = 5,
               member_lookup: Optional[Callable[[str], Optional[str]]] = None,
               _unread: Optional[List[StepFact]] = None) -> List[StepFact]:
    """Effective steps of a job: every EXEC PROC= replaced by the PROC's steps,
    with symbolics resolved in JCL precedence (EXEC overrides > instream SET >
    PROC defaults) and //PROCSTEP.DDNAME overrides and additions applied.

    Without this, a DSN coded in a PROC as &HLQ..MASTER stays unresolved and
    the job's real datasets are invisible - and in most shops the PROC is
    where the datasets are. Unresolved symbolics are reported, not hidden.

    The effective steps are read from their cards (_resolve_effective_pgm)
    once every step of the job is known and its referbacks resolved: a card
    DD's DSN=*.SRT010.SYSIN brings the cards it names (LESSONS 229). `_unread`
    collects them through a nested PROC's expansion; callers leave it out.
    """
    top = _unread is None
    unread: List[StepFact] = [] if _unread is None else _unread
    out: List[StepFact] = []
    proc_names_seen = set()
    for s in job.steps:
        if not s.proc_called:
            out.append(s)
            continue
        # An instream PROC (// PROC ... // PEND in this job) shadows a
        # cataloged one of the same name - that is the JCL rule too.
        proc = job.instream_procs.get(s.proc_called.upper()) or proc_lookup(s.proc_called)
        if proc is None or not proc.steps:
            synth = _system_proc_step(s, job)
            if synth is not None:
                # IMS.PROCLIB / DB2 PROCs are rarely in the estate folder, but
                # `EXEC DLIBATCH,MBR=CLMPOST,PSB=CLMPSB` says everything that
                # matters: synthesised, and reported as such.
                out.append(synth)
                continue
            s.notes.append(f"PROC {s.proc_called} not in index - its steps are unknown")
            job.unresolved.append(("missing_proc", f"{s.step_name}: PROC {s.proc_called} not found", s.line))
            out.append(s)
            continue
        proc_names_seen.add(s.proc_called.upper())

        # JCL precedence inside a procedure: EXEC override > PROC statement
        # default > SET. A SET value reaches the PROC only for a symbol the
        # PROC statement does not define (z/OS MVS JCL Reference, SET
        # statement). See LESSONS.md 97 - verify once on the real system.
        symbols: Dict[str, str] = dict(job.set_symbols)
        if PROC_DEFAULT_BEATS_SET:
            symbols.update(proc.symbolics)
        else:
            symbols = dict(proc.symbolics)
            symbols.update(job.set_symbols)
        # `EXEC INNER,HLQ=&HLQ` passes the enclosing value, not the text '&HLQ'.
        ov_syms = {k: substitute_symbols(v, symbols) for k, v in s.sym_overrides.items()}
        symbols.update(ov_syms)

        # //PROCSTEP.DDNAME overrides; an unqualified DD after EXEC PROC=
        # applies to the FIRST step of the PROC (JCL rule). Keyed by
        # concatenation entry: the n-th override entry replaces the n-th
        # PROC entry, extra entries are appended, the rest stay.
        first = proc.steps[0].step_name.upper()
        ov: Dict[Tuple[str, str, int], DdFact] = {}
        for d in s.dds:
            if "." in d.dd_name:
                ps_name, dn = d.dd_name.upper().split(".", 1)
            else:
                ps_name, dn = first, d.dd_name.upper()
            ov[(ps_name, dn, d.concat_seq)] = d
        used: set = set()
        so = s.step_overrides

        for ps in proc.steps:
            psn = ps.step_name.upper()
            # effective_pgm / launcher / submits - and the *FTP* / *NDM* /
            # *DBD* rows - are re-derived below by _resolve_effective_pgm
            # from THIS job's cards: the PROC's own reading came from its
            # default card member (LESSONS 222, 226).
            eff = replace(ps,
                          step_name=f"{s.step_name}.{ps.step_name}",
                          from_proc=s.proc_called.upper(), parent_step=s.step_name,
                          dds=[], notes=[], effective_pgm=None, launcher=None, submits=[],
                          parm=substitute_symbols(ps.parm, symbols) if ps.parm else ps.parm,
                          pgm=substitute_symbols(ps.pgm, symbols) if ps.pgm else ps.pgm,
                          guard=" AND ".join(g for g in (s.guard, ps.guard) if g) or None,
                          sym_overrides=dict(ps.sym_overrides), step_overrides={}, also_runs=[])
            # PARM.PS= replaces that step's PARM; a bare PARM= replaces the
            # FIRST step's and nullifies the others' (JCL rule); COND.PS= /
            # bare COND= likewise for the condition.
            if f"PARM.{psn}" in so:
                eff.parm = so[f"PARM.{psn}"] or None
            elif "PARM" in so:
                eff.parm = (so["PARM"] or None) if psn == first else None
            if f"COND.{psn}" in so:
                eff.cond = so[f"COND.{psn}"]
            elif "COND" in so:
                eff.cond = so["COND"]

            for d in ps.dds:
                if d.dd_name.startswith("*"):
                    # *FTP* / *NDM* / *DBD*: rows _resolve_effective_pgm made
                    # from the PROC's own cards or PARM (its default card
                    # member, its default DBD). They are made again below
                    # from this job's; copied, they stood beside the job's
                    # as 'unknown [undetermined]' (LESSONS 226).
                    continue
                key = (psn, d.dd_name.upper(), d.concat_seq)
                o = ov.get(key)
                if o is not None:
                    used.add(key)
                    if o.mode == "dummy":
                        # //PS.SYSIN DD DUMMY: the PROC's dataset is NOT read.
                        nd = replace(d, dsn=None, dsn_resolved=None, gdg_rel=None, sysin_text=None,
                                     card_member=None, referback=None, is_temp=False,
                                     mode="dummy", mode_source="dummy", is_override=True, line=o.line)
                    elif not o.dsn and o.sysin_text is not None:
                        # //PS.SYSIN DD *: the instream cards REPLACE the PROC's
                        # dataset - the step reads no PARMLIB member.
                        nd = replace(d, dsn=None, dsn_resolved=None, gdg_rel=None, disp=o.disp,
                                     sysin_text=o.sysin_text, card_member=None, referback=None, is_temp=False,
                                     mode=o.mode, mode_source=o.mode_source, is_override=True, line=o.line)
                    elif o.dsn and o.dsn.startswith("/"):
                        # //PS.DD DD PATH=...: a USS file replaces the PROC's
                        # DD, read or written as the PATHOPTS it codes say
                        # (LESSONS 232).
                        nd = replace(d, dsn=o.dsn, dsn_resolved=None, gdg_rel=None, disp=o.disp,
                                     sysin_text=o.sysin_text, card_member=None, referback=None, is_temp=False,
                                     mode=o.mode, mode_source=o.mode_source, is_override=True, line=o.line)
                    else:
                        # An override naming a dataset brings its own cards (or
                        # none, when its member is not indexed), never the
                        # PROC's default member's.
                        nd = replace(d, dsn=o.dsn or d.dsn, disp=o.disp or d.disp,
                                     sysin_text=o.sysin_text if o.dsn else d.sysin_text,
                                     card_member=o.card_member if o.dsn else d.card_member,
                                     is_override=True, line=o.line)
                else:
                    nd = replace(d)
                eff.dds.append(_resolve_dd(nd, symbols, job, eff, member_lookup))
            for key, o in sorted(ov.items(), key=lambda kv: (kv[0][1], kv[0][2])):
                if key[0] == psn and key not in used:
                    used.add(key)
                    eff.dds.append(_resolve_dd(replace(o, dd_name=key[1], is_override=True), symbols, job, eff,
                                               member_lookup))
            for key in ov:
                if key[0] not in {p.step_name.upper() for p in proc.steps} and key not in used:
                    job.unresolved.append(("override_target",
                                           f"{s.step_name}: override //{key[0]}.{key[1]} names no step in PROC {s.proc_called}",
                                           s.line))
                    used.add(key)
            if not any(d.dd_name.upper() == "STEPLIB" for d in eff.dds):
                eff.dds.extend(replace(jd, mode_source="joblib", is_override=False)
                               for jd in job.job_dds if jd.dd_name.upper() == "JOBLIB")

            if eff.proc_called and depth < max_depth:
                # Nested PROC: only true SET values and what THIS EXEC codes
                # (already resolved) reach the inner PROC - its own defaults
                # are not overridden by the outer PROC's defaults.
                eff.sym_overrides = {k: substitute_symbols(v, symbols) for k, v in eff.sym_overrides.items()}
                sub = JclFacts(job_name=job.job_name, job_line=job.job_line, is_proc=False,
                               proc_name=None, symbolics=dict(symbols), set_symbols=dict(job.set_symbols),
                               instream_procs=job.instream_procs, job_dds=job.job_dds)
                sub.steps = [eff]
                out.extend(expand_job(sub, proc_lookup, depth + 1, max_depth, member_lookup, unread))
                job.unresolved.extend(sub.unresolved)
                continue
            unread.append(eff)
            out.append(eff)
    if top:
        # Every step is now known, PROC steps included: DSN=*.STEP.DD can be
        # followed to the dataset it names - and to its cards, for a card DD;
        # then each effective step is read from its cards, a job step whose
        # referback reached into a PROC's step is read again (_read_again),
        # and a (+1) named again after an earlier step wrote it is read
        # (gdg_same_job).
        _resolve_referbacks(out, job, report=True, member_lookup=member_lookup)
        for eff in unread:
            _resolve_effective_pgm(eff, job, member_lookup)
        for s in out:
            _read_again(s, job, member_lookup)
        _same_job_generations(out)
        _overrides_follow(out, job)
    return out


def _overrides_follow(steps: List[StepFact], job: JclFacts) -> None:
    """A job step's `//PS.DD` override row - the row where the job codes it,
    beside the PROC step it overrides - reads the generation the way the
    effective DD built from it does: `//PS2.KVIN DD DSN=PROD.X(+1),DISP=SHR`
    naming the (+1) an earlier step of the job wrote is input
    'gdg_same_job' in S1.PS2 (_same_job_generations), and the override row
    on S1 said output [gdg_relative] - `dataset` listed the job step as a
    second writer of the generation (LESSONS 250). Matched by the line the
    override is coded on, its concatenation entry and its DD name; a row
    already input (SORTIN, SYSUT1) keeps its own reason."""
    callers = {s.step_name.upper(): s for s in job.steps}
    for e in steps:
        caller = callers.get((e.parent_step or "").upper()) if e.from_proc else None
        if caller is None:
            continue
        for d in e.dds:
            if not d.is_override or d.mode_source != "gdg_same_job":
                continue
            for i, o in enumerate(caller.dds):
                if (o.line == d.line and o.concat_seq == d.concat_seq and o.mode != "input"
                        and o.dd_name.upper().split(".")[-1] == d.dd_name.upper().split(".")[-1]):
                    caller.dds[i] = replace(o, mode="input", mode_source="gdg_same_job")


def _read_again(step: StepFact, facts: JclFacts,
                member_lookup: Optional[Callable[[str], Optional[str]]] = None) -> None:
    """A job step with a DD referring back into a PROC's step
    (`//SYSIN DD DSN=*.S1.SRT010.SYSIN` after `//S1 EXEC KVSORT`) was read
    from its cards when the job was parsed, without that DD's dataset - the
    PROC's steps were not known yet - and expand_job has now resolved the
    referback: the step is read again from what it was before the first
    reading (step.first_reading), with the DDs expand_job resolved, and the
    rows the first reading added to the job's unresolved list go ('SORT/
    ICETOOL with no cards' among them). The first reading stands when none of
    those referbacks resolved (LESSONS 229)."""
    first = step.first_reading
    if first is None:
        return
    step.first_reading = None
    before, rows = first
    dds = list(before.dds)
    resolved = False
    for i, d in enumerate(before.dds):
        # the first reading replaces DDs in place and appends its own after them: index i is the same DD
        if d.referback and not d.dsn_resolved and step.dds[i].dsn_resolved:
            dds[i] = step.dds[i]
            resolved = True
    if not resolved:
        return
    for row in rows:
        if row in facts.unresolved:
            facts.unresolved.remove(row)
    step.dds, step.notes = dds, list(before.notes)
    step.effective_pgm, step.launcher = before.effective_pgm, before.launcher
    step.submits, step.also_runs = list(before.submits), list(before.also_runs)
    _resolve_effective_pgm(step, facts, member_lookup)
    _apply_joblib([step], facts.job_dds)


def _resolve_dd(d: DdFact, symbols: Dict[str, str], job: JclFacts, step: StepFact,
                member_lookup: Optional[Callable[[str], Optional[str]]] = None) -> DdFact:
    """A PROC step's DD with the calling job's symbols: the DSN, its
    generation and direction - and the card member it names, with that
    member's text. `//SYSIN DD DSN=PROD.PARMLIB(&CARDS)` was read with the
    PROC's default CARDS=DEFCARD when the PROC member was parsed; the job's
    EXEC PROC=...,CARDS=R10CARD makes it R10CARD, so the step's cards,
    launcher and sort byte positions are R10CARD's (LESSONS 222)."""
    if not d.dsn:
        return d
    if d.dsn.startswith("*."):
        # Followed again by _resolve_referbacks over the job's steps: the dataset, the cards and the direction the
        # PROC member's own reading gave are its defaults' (LESSONS 229).
        mode, src = _direction(d.dd_name, None, d.disp, f"DSN={d.dsn},DISP={d.disp or ''}")
        return replace(d, referback=d.dsn, dsn_resolved=None, gdg_rel=None, is_temp=False, card_member=None,
                       sysin_text=None, mode=mode, mode_source=src)
    if d.dsn.startswith("/"):
        # A USS file (PATH=, read by _build_dd): no generation and no card
        # member, and its direction is the PATHOPTS the DD codes
        # ('pathopts') - _direction reads none from a name, and every PROC's
        # PATH DD was 'unknown [undetermined]' in the jobs running it, gone
        # from `interfaces` (LESSONS 232).
        path, found = _variables(substitute_symbols(d.dsn, symbols))
        for kind, tok in found:
            job.unresolved.append((kind, f"{step.step_name} {d.dd_name}: {tok} still unresolved in {d.dsn}", d.line))
        return replace(d, dsn_resolved=path, gdg_rel=None, is_temp=False, referback=None, card_member=None)
    resolved = substitute_symbols(d.dsn, symbols)
    resolved, found = _variables(resolved)
    for kind, tok in found:
        job.unresolved.append((kind, f"{step.step_name} {d.dd_name}: {tok} still unresolved in {d.dsn}", d.line))
    resolved, gdg = _strip_gdg(resolved)
    mode, src = _direction(d.dd_name, gdg, d.disp, f"DSN={resolved},DISP={d.disp or ''}")
    member, text = d.card_member, d.sysin_text
    name, body, by_last = _card_source(d.dd_name, resolved, gdg, member_lookup)
    if name != d.card_member or text is None:
        # The member the job's symbols name. When it is the one the PROC
        # member was read with and that text is there, the text stays (the
        # PROC's own department chose among same-named members).
        member, text = name, body
    if by_last:
        # the job's row, whether its symbols name another member or the PROC's default: a job running the PROC
        # with its default sequential card dataset had none, while the same PROC run with an override had one
        # (LESSONS 250)
        entry = ("card_seq_assumed", f"{step.step_name} {d.dd_name}: cards taken from member {member} matching the "
                                     f"last qualifier of {resolved}", d.line)
        if entry not in job.unresolved:
            job.unresolved.append(entry)
    return replace(d, dsn_resolved=resolved, gdg_rel=gdg, mode=mode, mode_source=src,
                   is_temp=resolved.startswith("&&"), referback=None, card_member=member, sysin_text=text)


def _resolve_referbacks(steps: List[StepFact], facts: JclFacts, report: bool = True,
                        member_lookup: Optional[Callable[[str], Optional[str]]] = None) -> None:
    """Follow DSN=*.STEP.DD (also *.DD in the same step, *.STEP.PROCSTEP.DD)
    to the dataset the earlier DD allocated.

    This is how a &&TEMP is usually handed from one step to the next, and how
    a PROC step picks up what a previous PROC step wrote. Left as text, the
    consumer step shows no dataset at all and the lineage has a hole exactly
    where the job's own intermediate file is. The referring DD's own DISP
    decides direction (the source DD's (+1) is NOT inherited: the referback
    reads what was created).

    The referring DD reads that dataset as if its DSN were coded on it: a
    card DD (`//SYSIN DD DSN=*.S1.SYSIN`) gets the card member and the
    member's text by the rule _build_dd reads a DSN with (_card_source) - it
    got the member alone, the step had no cards, and `job` said 'card
    member NOT indexed' for the member the step above it loaded (LESSONS
    229). Run before the steps are read from their cards.
    """
    dd_index: Dict[int, Dict[str, list]] = {}
    for idx, s in enumerate(steps):
        for i, d in enumerate(s.dds):
            if not d.referback or d.dsn_resolved:
                continue
            dd_index.pop(id(s), None)                    # this step's DDs change as referbacks resolve
            parts = d.referback[2:].upper().split(".")
            ddn, path = parts[-1], parts[:-1]
            if not path:
                cands = [s]
            else:
                key = ".".join(path)
                earlier = steps[:idx + 1]
                cands = [x for x in earlier if x.step_name.upper() == key]
                if not cands:        # *.PROCSTEP.DD inside a PROC, or *.STEP.PROCSTEP.DD after expansion
                    cands = [x for x in earlier if x.step_name.upper().endswith("." + key)]
                if len(cands) > 1 and s.parent_step:
                    same = [x for x in cands if x.parent_step == s.parent_step]
                    cands = same or cands
            src = None
            for x in reversed(cands):
                by_name = dd_index.get(id(x))
                if by_name is None:                      # a step's DDs by name, built once (was a scan per referback)
                    by_name = {}
                    for y in x.dds:
                        by_name.setdefault(y.dd_name.upper().split(".")[-1], []).append(y)
                    dd_index[id(x)] = by_name
                for y in by_name.get(ddn, []):
                    if y.dsn_resolved and y is not d:
                        src = y
                        break
                if src:
                    break
            if src is None:
                entry = ("referback",
                         f"{s.step_name} {d.dd_name}: {d.referback} names no earlier DD with a dataset",
                         d.line)
                if report and entry not in facts.unresolved:     # parse and expand both get here
                    facts.unresolved.append(entry)
                continue
            mode, msrc = _direction(d.dd_name, None, d.disp, f"DSN={src.dsn_resolved},DISP={d.disp or ''}")
            member, text, by_last = _card_source(d.dd_name, src.dsn_resolved, src.gdg_rel, member_lookup)
            if by_last:
                entry = ("card_seq_assumed", f"{s.step_name} {d.dd_name}: cards taken from member {member} "
                                             f"matching the last qualifier of {src.dsn_resolved}", d.line)
                if entry not in facts.unresolved:
                    facts.unresolved.append(entry)
            s.dds[i] = replace(d, dsn_resolved=src.dsn_resolved, is_temp=src.is_temp,
                               card_member=member, sysin_text=text, mode=mode, mode_source=msrc)


def _new_generation(d: DdFact) -> Optional[int]:
    """n for a DD naming the relative generation (+n), n > 0; else None - and
    None for a DD that neither reads nor writes it: SYSOUT, DUMMY, the
    internal reader, an IEFBR14 step's allocation or delete."""
    if (d.gdg_rel is None or not d.dsn_resolved or d.is_temp
            or d.mode in ("sysout", "dummy", "submit", "alloc", "delete", "none")):
        return None
    try:
        g = int(d.gdg_rel)
    except ValueError:                           # an absolute generation G0012V00
        return None
    return g if g > 0 else None


def _reads_a_generation(d: DdFact) -> bool:
    """A DD that reads the generation it names: its role reads it (SORTIN,
    SYSUT1, an input of the sort / ICETOOL / JOINKEYS / Easytrieve cards - the
    mode already set from them), or DISP=SHR / OLD - a status that never
    creates a generation - on a DD whose direction only the generation number
    decided and whose name is not a writing one (SORTOUT, SYSUT2, ...): a
    role that writes it (an OUTFIL FNAMES= of the sort cards, an Easytrieve
    PUT) keeps it written. The program's own OPEN is applied later, in
    build.py, and decides over this."""
    base = d.dd_name.upper().split(".")[-1]
    if _utility_side(base) == "output" or base in _OUTPUT_DDS:
        return False
    if d.mode == "input":
        return True
    return (d.mode_source == "gdg_relative" and d.disp is not None
            and _disp_parts(d.disp)[0] in ("SHR", "OLD"))


def _same_job_generations(steps: List[StepFact]) -> None:
    """JES resolves relative generation numbers ONCE PER JOB: the (+1) an
    earlier step of the job created and a later step names again as (+1) is
    the same new generation, read there - not a second one, and the later
    step is not its second writer. `steps` are the job's steps in the order
    they run (PROC steps expanded in place). A generation counts as written
    by a DD of it whose direction is output / mod / both - an IEFBR14
    allocation writes nothing, so the program that writes into the
    pre-allocated generation with DISP=OLD is still its writer. A later DD of
    it that reads it (_reads_a_generation) becomes input 'gdg_same_job'; one
    that writes it stays as it is. A step's own DDs never count as an earlier
    step's."""
    written: set = set()
    for s in steps:
        mine = []
        for i, d in enumerate(s.dds):
            g = _new_generation(d)
            if g is None:
                continue
            key = (d.dsn_resolved.upper(), g)
            if key in written and _reads_a_generation(d):
                s.dds[i] = replace(d, mode="input", mode_source="gdg_same_job")
            elif d.mode in ("output", "mod", "both"):
                mine.append(key)
        written.update(mine)


# --------------------------------------------------------------------------
# IDCAMS control cards: datasets created, deleted, copied
# --------------------------------------------------------------------------

# `[^A-Z]*` then NAME: no overlap between the gap and what follows (the lazy
# gap + `\(?\s*` split every blank run - quadratic on padded card members)
_IDC_DEFINE = re.compile(r"\bDEFINE\s+(CLUSTER|AIX|ALTERNATEINDEX|GDG|PATH|NONVSAM)\b[^A-Z]*NAME\s*\(\s*([^)\s]+)\s*\)",
                         re.IGNORECASE | re.S)
_IDC_DELETE = re.compile(r"\bDELETE\s+\(?\s*([A-Z0-9$#@.]+)", re.IGNORECASE)
_IDC_REPRO = re.compile(r"\bREPRO\b(.*?)(?=\bREPRO\b|\bDEFINE\b|\bDELETE\b|\bPRINT\b|\bLISTCAT\b|$)",
                        re.IGNORECASE | re.S)
_IDC_KW = re.compile(r"\b(INFILE|OUTFILE|INDATASET|OUTDATASET|IDS|ODS|IFILE|OFILE)\s*\(\s*([^)\s]+)\s*\)",
                     re.IGNORECASE)


def idcams_ops(text: str) -> List[Tuple[str, str, str]]:
    """(operation, name, mode). DEFINE -> (create, dsn); DELETE -> (delete, dsn);
    REPRO INDATASET/OUTDATASET -> (input/output, dsn); REPRO INFILE/OUTFILE ->
    ('REPRO DD', ddname, input/output) so the step's DD gets its direction.
    An IDCAMS step is where a VSAM file is born or dies; without this the
    dataset's lineage starts at its first reader."""
    out: List[Tuple[str, str, str]] = []
    if not text:
        return out
    t = " ".join(ln[:72] for ln in text.splitlines() if not ln.strip().startswith("/*"))
    for m in _IDC_DEFINE.finditer(t):
        out.append(("DEFINE " + m.group(1).upper(), m.group(2).upper(), "create"))
    for m in _IDC_DELETE.finditer(t):
        out.append(("DELETE", m.group(1).upper(), "delete"))
    for m in _IDC_REPRO.finditer(t):
        for k in _IDC_KW.finditer(m.group(1)):
            kw, val = k.group(1).upper(), k.group(2).upper()
            if kw in ("INDATASET", "IDS"):
                out.append(("REPRO", val, "input"))
            elif kw in ("OUTDATASET", "ODS"):
                out.append(("REPRO", val, "output"))
            elif kw in ("INFILE", "IFILE"):
                out.append(("REPRO DD", val, "input"))
            else:
                out.append(("REPRO DD", val, "output"))
    return out


_IDC_BODY = re.compile(r"\bDEFINE\s+(CLUSTER|AIX|ALTERNATEINDEX|GDG|PATH)\b(.*?)(?=\bDEFINE\b|\bDELETE\b|\bREPRO\b|\bLISTCAT\b|\bPRINT\b|$)",
                       re.IGNORECASE | re.S)


def idcams_attrs(text: str) -> Dict[str, Dict[str, str]]:
    """DEFINE attributes per dataset name: vsam_type (KSDS/ESDS/RRDS/LDS/AIX/
    PATH/GDG), recordsize_max, key_len, key_off, gdg_limit, relates_to.
    The copybook's record length must agree with RECORDSIZE and the file's
    RECORD KEY with KEYS - both are checkable only when they are indexed."""
    out: Dict[str, Dict[str, str]] = {}
    if not text:
        return out
    t = " ".join(ln[:72] for ln in text.splitlines() if not ln.strip().startswith("/*"))
    for m in _IDC_BODY.finditer(t):
        what, body = m.group(1).upper(), m.group(2)
        mn = re.search(r"\bNAME\s*\(\s*([^)\s]+)\s*\)", body, re.IGNORECASE)
        if not mn:
            continue
        name = mn.group(1).upper()
        a: Dict[str, str] = {}
        if what in ("AIX", "ALTERNATEINDEX"):
            a["vsam_type"] = "AIX"
        elif what == "PATH":
            a["vsam_type"] = "PATH"
        elif what == "GDG":
            a["vsam_type"] = "GDG"
        else:
            a["vsam_type"] = ("ESDS" if re.search(r"\bNONINDEXED\b", body, re.I) else
                              "RRDS" if re.search(r"\bNUMBERED\b", body, re.I) else
                              "LDS" if re.search(r"\bLINEAR\b", body, re.I) else "KSDS")
        mr = re.search(r"\bRECORDSIZE\s*\(\s*(\d+)\s+(\d+)\s*\)", body, re.IGNORECASE)
        if mr:
            a["recordsize_max"] = mr.group(2)
        mk = re.search(r"\bKEYS\s*\(\s*(\d+)\s+(\d+)\s*\)", body, re.IGNORECASE)
        if mk:
            a["key_len"], a["key_off"] = mk.group(1), mk.group(2)
        ml = re.search(r"\bLIMIT\s*\(\s*(\d+)\s*\)", body, re.IGNORECASE)
        if ml:
            a["gdg_limit"] = ml.group(1)
        mrel = re.search(r"\b(?:RELATE|PATHENTRY)\s*\(\s*([^)\s]+)\s*\)", body, re.IGNORECASE)
        if mrel:
            a["relates_to"] = mrel.group(1).upper()
        out[name] = a
    return out


_DB2U_TABLE = re.compile(r"\b(?:INTO|FROM)\s+TABLE\s+([A-Z0-9_$#@]+(?:\.[A-Z0-9_$#@]+)?)", re.IGNORECASE)
_DB2U_OP = re.compile(r"\b(LOAD|UNLOAD|REORG|RUNSTATS|COPY|RECOVER|CHECK|REBUILD|MERGECOPY|QUIESCE)\b", re.IGNORECASE)
_DB2U_DD = re.compile(r"\b(INDDN|UNLDDN|PUNCHDDN|COPYDDN|DISCARDDN|WORKDDN)\s+\(?\s*([A-Z0-9@#$]+)", re.IGNORECASE)
_DB2U_TS = re.compile(r"\bTABLESPACE\s+([A-Z0-9_$#@]+(?:\.[A-Z0-9_$#@]+)?)", re.IGNORECASE)
_SQL_VERB_TABLE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b(?:(?!\b(?:SELECT|INSERT|UPDATE|DELETE)\b).)*?"
                             r"\b(?:FROM|INTO|UPDATE)\s+([A-Z0-9_$#@]+(?:\.[A-Z0-9_$#@]+)?)",
                             re.IGNORECASE | re.S)


def db2util_ops(text: str, is_sql: bool = False) -> List[Tuple[str, str, Optional[str], str]]:
    """(op, table, dd, direction) for DSNUTILB cards (LOAD ... INTO TABLE x
    INDDN dd; UNLOAD FROM TABLE x UNLDDN dd; REORG/RUNSTATS/COPY TABLESPACE)
    and for the SQL a DSNTIAUL / DSNTEP2 step runs. This is the DB2 <-> flat
    file lineage of every nightly feed: 'who writes CLAIM_TBL' includes the
    LOAD, 'what reads it' includes the unload."""
    out: List[Tuple[str, str, Optional[str], str]] = []
    if not text:
        return out
    t = " ".join(ln[:72] for ln in text.splitlines() if not ln.strip().startswith(("*", "--")))
    if is_sql:
        for stmt in t.split(";"):
            for m in _SQL_VERB_TABLE.finditer(stmt):
                verb = m.group(1).upper()
                out.append((verb, m.group(2).upper(), None, "write" if verb in ("INSERT", "UPDATE", "DELETE") else "read"))
        return out
    for chunk in re.split(r"(?=\b(?:LOAD|UNLOAD|REORG|RUNSTATS|COPY|RECOVER|CHECK|REBUILD|MERGECOPY|QUIESCE)\b)", t, flags=re.I):
        mo = _DB2U_OP.match(chunk.strip())
        if not mo:
            continue
        op = mo.group(1).upper()
        dds = {k.upper(): v.upper() for k, v in _DB2U_DD.findall(chunk)}
        tables = [x.upper() for x in _DB2U_TABLE.findall(chunk)] or [x.upper() for x in _DB2U_TS.findall(chunk)]
        for tbl in tables or ["?"]:
            if op == "LOAD":
                out.append((op, tbl, dds.get("INDDN", "SYSREC"), "write"))
            elif op == "UNLOAD":
                out.append((op, tbl, dds.get("UNLDDN", "SYSREC00"), "read"))
            elif op in ("REORG", "COPY", "MERGECOPY"):
                out.append((op, tbl, dds.get("COPYDDN") or dds.get("UNLDDN"), "reorg" if op == "REORG" else "read"))
            else:
                out.append((op, tbl, None, "read"))
    return out
