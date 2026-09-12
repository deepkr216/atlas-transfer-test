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
}

_RUN_PROGRAM = re.compile(r"\bRUN\s+PROGRAM\s*\(\s*([A-Z0-9@#$]{1,8})\s*\)", re.IGNORECASE)
_RUN_PLAN = re.compile(r"\bPLAN\s*\(\s*([A-Z0-9@#$]{1,8})\s*\)", re.IGNORECASE)
_DSN_SYSTEM = re.compile(r"\bDSN\s+SYSTEM\s*\(\s*([A-Z0-9@#$]{1,4})\s*\)", re.IGNORECASE)

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


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

def parse_jcl(text: str, data: bytes = b"", enc: str = "utf-8",
              extra_symbols: Optional[Dict[str, str]] = None,
              include_lookup: Optional[Callable[[str], Optional[str]]] = None) -> JclFacts:
    included: List[str] = []
    if include_lookup is not None:
        text, included = _splice_includes(text, data, enc, include_lookup)
    stmts = read_jcl(text, data, enc)
    facts = JclFacts(job_name=None, job_line=0, is_proc=False,
                     proc_name=None, symbolics=dict(extra_symbols or {}))
    facts.includes = included

    cur: Optional[StepFact] = None
    ordinal = 0
    last_dd_name = ""
    concat_seq = 0

    for st in stmts:
        if st.op == "JOB":
            facts.job_name = st.name or None
            facts.job_line = st.start

        elif st.op == "PROC":
            # A cataloged PROC's own statement carries its default symbolics.
            facts.is_proc = True
            facts.proc_name = st.name or facts.proc_name
            for k, v in keyword_operands(st.operands).items():
                facts.symbolics.setdefault(k, _unquote(v))

        elif st.op == "SET":
            # Instream SET wins over PROC defaults for everything after it.
            for k, v in keyword_operands(st.operands).items():
                facts.symbolics[k] = _unquote(v)
                facts.set_symbols[k] = _unquote(v)

        elif st.op == "EXEC":
            ordinal += 1
            cur = _build_step(st, ordinal, facts)
            facts.steps.append(cur)
            last_dd_name, concat_seq = "", 0

        elif st.op == "DD" and cur is not None:
            if st.name:
                last_dd_name, concat_seq = st.name, 0
            else:
                concat_seq += 1          # unnamed DD = concatenation
            cur.dds.append(_build_dd(st, last_dd_name, concat_seq, facts))

        elif st.op == "INCLUDE":
            m = keyword_operands(st.operands).get("MEMBER")
            if m:
                facts.unresolved.append(("include_member", _unquote(m), st.start))

    # Second pass: now that every DD is attached, unwrap the launchers.
    for step in facts.steps:
        _resolve_effective_pgm(step, facts)

    return facts


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

    # Any other KEY=VALUE on an EXEC PROC= is a symbolic override for that step.
    overrides: Dict[str, str] = {}
    if proc_called:
        for k, v in kw.items():
            if k not in _EXEC_KEYWORDS:
                overrides[k] = _unquote(v) or ""
                facts.symbolics[k] = _unquote(v) or ""

    return StepFact(
        ordinal=ordinal,
        step_name=st.name or f"STEP{ordinal:03d}",
        pgm=_unquote(pgm) if pgm else None,
        proc_called=_unquote(proc_called) if proc_called else None,
        effective_pgm=None,
        launcher=None,
        parm=_unquote(kw.get("PARM", "")) or None,
        cond=kw.get("COND"),
        line=st.start,
        sym_overrides=overrides,
    )


def _build_dd(st: JclStatement, dd_name: str, concat_seq: int,
              facts: JclFacts) -> DdFact:
    kw = keyword_operands(st.operands)
    raw_dsn = kw.get("DSN") or kw.get("DSNAME")
    disp = kw.get("DISP")

    dsn = _unquote(raw_dsn) if raw_dsn else None
    resolved, gdg = (None, None)
    if dsn:
        resolved = substitute_symbols(dsn, facts.symbolics)
        resolved, gdg = _strip_gdg(resolved)

    sysin = "\n".join(st.inline_data) if st.inline_data else None

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
    )


# DD names whose direction is fixed by the utility that reads them.
_INPUT_DDS = {"SORTIN", "SYSUT1", "INFILE", "SYSIN", "SYSTSIN", "STEPLIB", "JOBLIB",
              "SYSLIB", "IMSACB", "DFSRESLB", "DFSVSAMP", "IEFRDER", "SYSLMOD"}
_OUTPUT_DDS = {"SORTOUT", "SYSUT2", "OUTFILE", "SYSPRINT", "SYSOUT", "SYSUDUMP",
               "SYSABEND", "CEEDUMP"}


def _direction(dd_name: str, gdg: Optional[str], disp: Optional[str],
               operands: str) -> Tuple[str, str]:
    """Decide read/write direction, and record WHAT decided it.

    DISP is NOT direction. DISP is serialisation and cataloguing. DISP=OLD is
    routinely coded on a pre-allocated output dataset, and DISP=SHR datasets
    are written all day (VSAM update-in-place is everywhere in insurance).
    Derive direction from DISP alone and the batch lineage graph comes out
    with arrows pointing the wrong way - and it looks fine.

    Signals, strongest first:
      1. GDG relative generation: (+1) is created here, (0)/(-n) is read.
      2. Utility DD-name conventions: SORTIN/SYSUT1 read, SORTOUT/SYSUT2 write.
      3. DISP=NEW / MOD - weak corroboration only.
      4. The program's own OPEN INPUT/OUTPUT/I-O verb, joined through
         SELECT...ASSIGN to this DD name. That join is applied later, in
         build.py, and OVERRIDES everything above because it is the only
         signal that reflects what the code actually does.
    """
    up = (operands or "").upper()
    if "SYSOUT=" in up:
        return "sysout", "sysout"
    if "DUMMY" in up:
        return "dummy", "dummy"

    if gdg is not None:
        try:
            g = int(gdg)
        except ValueError:
            g = None
        if g is not None:
            return ("output" if g > 0 else "input"), "gdg_relative"

    base = dd_name.upper().split(".")[-1]
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


def _strip_gdg(dsn: str) -> Tuple[str, Optional[str]]:
    m = _GDG_REL.search(dsn)
    if m:
        return _GDG_REL.sub("", dsn).strip(), m.group(1)
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

def _resolve_effective_pgm(step: StepFact, facts: JclFacts) -> None:
    pgm = (step.pgm or "").upper()
    if not pgm:
        step.effective_pgm = None
        if step.proc_called:
            step.notes.append(f"runs PROC {step.proc_called}; steps come from the PROC member")
        return

    kind = LAUNCHERS.get(pgm)
    if kind is None:
        step.effective_pgm = pgm       # an ordinary application program
        return

    step.launcher = pgm

    if kind == "tso":
        sysin = _dd_text(step, "SYSTSIN")
        if sysin:
            m = _RUN_PROGRAM.search(sysin)
            if m:
                step.effective_pgm = m.group(1).upper()
                plan = _RUN_PLAN.search(sysin)
                sub = _DSN_SYSTEM.search(sysin)
                if plan:
                    step.notes.append(f"DB2 plan {plan.group(1).upper()}")
                if sub:
                    step.notes.append(f"DB2 subsystem {sub.group(1).upper()}")
                return
            step.notes.append("TSO step with no RUN PROGRAM(...) - may be TSO commands only")
        facts.unresolved.append(
            ("launcher_parm", f"{step.step_name}: IKJEFT01 program not found in SYSTSIN", step.line))

    elif kind == "ims":
        parm = step.parm or ""
        m = _IMS_PARM.search(parm)
        if m:
            step.effective_pgm = m.group(2).upper()
            if m.group(3):
                step.notes.append(f"PSB {m.group(3).upper()}")
            step.notes.append(f"IMS region type {m.group(1).upper()}")
            return
        facts.unresolved.append(
            ("launcher_parm", f"{step.step_name}: DFSRRC00 PARM not parseable: {parm!r}", step.line))

    elif kind == "sort":
        step.effective_pgm = "*SORT*"
        ctl = _dd_text(step, "SYSIN")
        if ctl:
            step.notes.append("sort behaviour defined by SYSIN control cards")
        else:
            facts.unresolved.append(
                ("launcher_parm", f"{step.step_name}: SORT with no inline SYSIN "
                                  f"(control cards are in a dataset)", step.line))

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
        ctl = _dd_text(step, "SYSIN") or _dd_text(step, "STDIN") or ""
        step.notes.append(f"external interface via {pgm}")
        for m in re.finditer(r"\b(?:open|SNODE=|PNODE=|host|put|get|send|receive)\b\s*[=(]?\s*([^\s,()]+)",
                             ctl, re.IGNORECASE):
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
    r"(?<![\d:.])(\d{1,5}),(\d{1,5})(?:,([A-Z][A-Z0-9]{0,3}))?(?=[,)\s]|$)", re.IGNORECASE)
_CARD_FORMAT = re.compile(r"\bFORMAT=([A-Z0-9]{1,4})\b", re.IGNORECASE)
_NEEDS_FMT = {"SORT", "MERGE", "INCLUDE", "OMIT", "SUM"}


def sort_card_fields(text: str) -> List[Tuple[str, int, int, str, str]]:
    """Byte positions a sort/merge step actually depends on.

    Returns (card_kind, pos_1based, length, format, raw_card). Sort cards are
    real business logic - filtering (INCLUDE/OMIT), reformatting (INREC/OUTREC/
    OUTFIL), field derivation - and they address the record BY BYTE POSITION.
    A copybook change that moves bytes silently breaks every one of these,
    which is why the impact query joins these positions against computed
    field offsets instead of hoping somebody remembers the sort step.
    """
    out: List[Tuple[str, int, int, str, str]] = []
    if not text:
        return out
    # Join continued cards: a card continues when it ends with a comma.
    cards: List[str] = []
    buf = ""
    for line in text.splitlines():
        s = line[:71].rstrip()
        if not s.strip() or s.lstrip().startswith("*"):
            continue
        buf = (buf + " " + s.strip()) if buf else s.strip()
        if not buf.endswith(","):
            cards.append(buf)
            buf = ""
    if buf:
        cards.append(buf)

    for card in cards:
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
            # With FORMAT=xx the triple is (pos,len,order) and 'fmt' is A/D.
            if fmt in ("A", "D", "") and fmt_default:
                fmt = fmt_default
            if not fmt and kind in _NEEDS_FMT:
                continue          # a bare pair in a SORT/INCLUDE list is not a field ref
            key = (pos, ln)
            if key in seen or pos == 0 or ln == 0:
                continue
            seen.add(key)
            out.append((kind, pos, ln, fmt or None, card[:120]))
    return out


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
               depth: int = 0, max_depth: int = 5) -> List[StepFact]:
    """Effective steps of a job: every EXEC PROC= replaced by the PROC's steps,
    with symbolics resolved in JCL precedence (EXEC overrides > instream SET >
    PROC defaults) and //PROCSTEP.DDNAME overrides and additions applied.

    Without this, a DSN coded in a PROC as &HLQ..MASTER stays unresolved and
    the job's real datasets are invisible - and in most shops the PROC is
    where the datasets are. Unresolved symbolics are reported, not hidden.
    """
    out: List[StepFact] = []
    for s in job.steps:
        if not s.proc_called:
            out.append(s)
            continue
        proc = proc_lookup(s.proc_called)
        if proc is None or not proc.steps:
            s.notes.append(f"PROC {s.proc_called} not in index - its steps are unknown")
            job.unresolved.append(("missing_proc", f"{s.step_name}: PROC {s.proc_called} not found", s.line))
            out.append(s)
            continue

        symbols: Dict[str, str] = dict(proc.symbolics)
        symbols.update(job.set_symbols)
        symbols.update(s.sym_overrides)

        # //PROCSTEP.DDNAME overrides; an unqualified DD after EXEC PROC=
        # applies to the FIRST step of the PROC (JCL rule).
        first = proc.steps[0].step_name.upper()
        ov: Dict[Tuple[str, str], DdFact] = {}
        for d in s.dds:
            if "." in d.dd_name:
                ps_name, dn = d.dd_name.upper().split(".", 1)
            else:
                ps_name, dn = first, d.dd_name.upper()
            ov[(ps_name, dn)] = d
        used: set = set()

        for ps in proc.steps:
            eff = replace(ps,
                          step_name=f"{s.step_name}.{ps.step_name}",
                          from_proc=s.proc_called.upper(), parent_step=s.step_name,
                          dds=[], notes=[],            # re-derived below by _resolve_effective_pgm
                          parm=substitute_symbols(ps.parm, symbols) if ps.parm else ps.parm,
                          pgm=substitute_symbols(ps.pgm, symbols) if ps.pgm else ps.pgm,
                          sym_overrides=dict(ps.sym_overrides))
            for d in ps.dds:
                key = (ps.step_name.upper(), d.dd_name.upper())
                o = ov.get(key)
                if o is not None:
                    used.add(key)
                    nd = replace(d, dsn=o.dsn or d.dsn, disp=o.disp or d.disp,
                                 sysin_text=o.sysin_text or d.sysin_text,
                                 is_override=True, line=o.line)
                else:
                    nd = replace(d)
                eff.dds.append(_resolve_dd(nd, symbols, job, eff))
            for key, o in ov.items():
                if key[0] == ps.step_name.upper() and key not in used:
                    eff.dds.append(_resolve_dd(replace(o, dd_name=key[1], is_override=True), symbols, job, eff))
            for key in ov:
                if key[0] not in {p.step_name.upper() for p in proc.steps} and key not in used:
                    job.unresolved.append(("override_target",
                                           f"{s.step_name}: override //{key[0]}.{key[1]} names no step in PROC {s.proc_called}",
                                           s.line))
                    used.add(key)

            if eff.proc_called and depth < max_depth:
                sub = JclFacts(job_name=job.job_name, job_line=job.job_line, is_proc=False,
                               proc_name=None, symbolics=dict(symbols), set_symbols=dict(symbols))
                sub.steps = [eff]
                out.extend(expand_job(sub, proc_lookup, depth + 1, max_depth))
                job.unresolved.extend(sub.unresolved)
                continue
            _resolve_effective_pgm(eff, job)
            out.append(eff)
    return out


def _resolve_dd(d: DdFact, symbols: Dict[str, str], job: JclFacts, step: StepFact) -> DdFact:
    if not d.dsn:
        return d
    resolved, gdg = _strip_gdg(substitute_symbols(d.dsn, symbols))
    if "&" in resolved.replace("&&", ""):
        job.unresolved.append(("symbolic",
                               f"{step.step_name} {d.dd_name}: symbolic still unresolved in {resolved}",
                               d.line))
    mode, src = _direction(d.dd_name, gdg, d.disp, f"DSN={resolved},DISP={d.disp or ''}")
    return replace(d, dsn_resolved=resolved, gdg_rel=gdg, mode=mode, mode_source=src)


# --------------------------------------------------------------------------
# IDCAMS control cards: datasets created, deleted, copied
# --------------------------------------------------------------------------

_IDC_DEFINE = re.compile(r"\bDEFINE\s+(CLUSTER|AIX|ALTERNATEINDEX|GDG|PATH|NONVSAM)\b[^A-Z]*?\(?\s*NAME\s*\(\s*([^)\s]+)\s*\)",
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
