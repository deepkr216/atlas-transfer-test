"""
synth_jcl.py - jobs, PROCs and control cards written statement by statement,
with the ground truth of what a job really runs: its EFFECTIVE steps after
PROC expansion (symbolics resolved in JCL precedence: EXEC override > PROC
statement default > instream SET for a symbol the PROC does not define),
//STEP.DD overrides applied, the effective program unwrapped from the
launchers (IKJEFT01 -> RUN PROGRAM, DFSRRC00 -> PARM, SORT / IDCAMS /
IEBGENER / IEFBR14 / DSNUTILB -> a utility), GDG relative generations split
from the base, and the direction of every DD with the signal the generator
expects to decide it (the program's OPEN verb, the GDG generation, the
utility DD-name convention, DISP=NEW as a weak hint, else undetermined).

The symbolic rules here are the generator's own reading of the JCL
reference, never atlas/jcl.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Tuple

_SYM = re.compile(r"&([A-Z0-9@#$]+)\.?")
GDG_REL = re.compile(r"^(.*?)\(([+-]?\d+)\)$")

INPUT_DDS = {"SORTIN", "SYSUT1", "INFILE", "SYSREC", "INDD"}
IMS_SYSTEM_PROCS = {"DLIBATCH": "DLI", "IMSBATCH": "DLI", "DBBBATCH": "DBB", "IMSBMP": "BMP", "DFSMPR": "MPP"}
OUTPUT_DDS = {"SORTOUT", "SYSUT2", "OUTFILE", "OUTDD"}


@dataclass
class Dd:
    name: str
    dsn: Optional[str] = None
    disp: Optional[str] = None
    operands: str = ""
    inline: Optional[List[str]] = None       # DD * cards
    member: Optional[str] = None             # DSN=LIB(MEMBER): the card member
    line: int = 0
    is_override: bool = False
    concat: List[Tuple[str, str]] = dc_field(default_factory=list)   # (dsn, disp) continuation lines
    dummy: bool = False
    sysout: bool = False
    referback: Optional[str] = None
    path: Optional[str] = None


@dataclass
class Step:
    name: str
    pgm: Optional[str] = None
    proc: Optional[str] = None
    parm: Optional[str] = None
    cond: Optional[str] = None
    overrides: Dict[str, str] = dc_field(default_factory=dict)     # EXEC PROC=X,SYM=val
    dds: List[Dd] = dc_field(default_factory=list)
    line: int = 0
    guard: Optional[str] = None
    proc_dd_overrides: List[Tuple[str, Dd]] = dc_field(default_factory=list)   # (procstep, dd)
    parm_for: Dict[str, str] = dc_field(default_factory=dict)      # PARM.PS010=
    cond_for: Dict[str, str] = dc_field(default_factory=dict)      # COND.PS010=


class JclBuilder:
    """Writes a job or a PROC member and records its truth."""

    def __init__(self, name: str, kind: str = "jcl") -> None:
        self.name = name
        self.kind = kind
        self.lines: List[str] = []
        self.job_name: Optional[str] = None
        self.proc_name: Optional[str] = None
        self.symbolics: Dict[str, str] = {}      # PROC statement defaults
        self.sets: Dict[str, str] = {}           # instream SET, in order of appearance (last wins)
        self.steps: List[Step] = []
        self.jcllib: List[str] = []
        self.joblib: List[str] = []
        self.job_cond: Optional[str] = None
        self.includes: List[str] = []
        self.instream_procs: Dict[str, "JclBuilder"] = {}
        self._guard: Optional[str] = None
        self._cur: Optional[Step] = None
        self._in_instream: Optional["JclBuilder"] = None

    # ------------------------------------------------------------------ emit
    @property
    def n(self) -> int:
        return len(self.lines)

    def raw(self, text: str) -> int:
        self.lines.append(text)
        return self.n

    def stmt(self, label: str, op: str, operands: str = "", cont_indent: int = 12) -> int:
        """`//LABEL   OP  operands`, continued after a comma at column 72 as JCL is."""
        head = f"//{label:<8} {op:<4} " if label else f"//{'':<8} {op:<4} "
        first = None
        if not operands:
            return self.raw(head.rstrip())
        if "'" in operands and len(head + operands) > 71:
            # a quoted PARM broken at column 71 exactly, the rest from column 16 (the JCL rule for literals)
            pieces = []
            text = head + operands
            first = None
            while len(text) > 71:
                pieces.append(text[:71])
                text = "//" + " " * 13 + text[71:]
            pieces.append(text)
            for p in pieces:
                n = self.raw(p)
                first = first if first is not None else n
            return first if first is not None else self.n
        parts = operands.split(",")
        cur = head
        line = cur
        pieces: List[str] = []
        for k, p in enumerate(parts):
            tail = "," if k < len(parts) - 1 else ""
            cand = line + p + tail
            if len(cand) > 71 and line.strip() not in (head.strip(),):
                pieces.append(line.rstrip())
                line = "//" + " " * (cont_indent - 2) + p + tail
            else:
                line = cand
        pieces.append(line.rstrip())
        for p in pieces:
            n = self.raw(p)
            first = first if first is not None else n
        return first if first is not None else self.n

    def comment(self, text: str = "") -> int:
        return self.raw(("//*" + text)[:72])

    # -------------------------------------------------------------- job/proc
    def job(self, name: str, acct: str = "ACCT", desc: str = "SYNTH", cls: str = "A", cond: Optional[str] = None,
            extra: str = "") -> int:
        self.job_name = name
        ops = f"({acct}),'{desc}',CLASS={cls},MSGCLASS=X"
        if cond:
            ops += f",COND={cond}"
            self.job_cond = cond
        if extra:
            ops += "," + extra
        return self.stmt(name, "JOB", ops)

    def jcllib_order(self, libs: Sequence[str]) -> int:
        self.jcllib = list(libs)
        return self.stmt("", "JCLLIB", f"ORDER=({','.join(libs)})")

    def joblib_dd(self, dsns: Sequence[str]) -> int:
        self.joblib = list(dsns)
        n = self.stmt("JOBLIB", "DD", f"DSN={dsns[0]},DISP=SHR")
        for d in dsns[1:]:
            self.stmt("", "DD", f"DSN={d},DISP=SHR")
        return n

    def set_(self, **syms: str) -> int:
        n = None
        for k, v in syms.items():
            self.sets[k] = v
            m = self.stmt("", "SET", f"{k}={v}")
            n = n if n is not None else m
        return n or self.n

    def proc(self, name: str, **defaults: str) -> int:
        self.proc_name = name
        self.symbolics = dict(defaults)
        ops = ",".join(f"{k}={v}" for k, v in defaults.items())
        return self.stmt(name, "PROC", ops)

    def pend(self) -> int:
        return self.stmt("", "PEND")

    def instream_proc_begin(self, name: str, **defaults: str) -> "JclBuilder":
        """`// PROC ... // PEND` inside a job: the steps until pend() belong to it."""
        pb = JclBuilder(name, "proc")
        pb.proc_name = name
        pb.symbolics = dict(defaults)
        self.stmt(name, "PROC", ",".join(f"{k}={v}" for k, v in defaults.items()))
        self._in_instream = pb
        self.instream_procs[name] = pb
        return pb

    def instream_proc_end(self) -> None:
        self.stmt("", "PEND")
        self._in_instream = None

    def include(self, member: str) -> int:
        self.includes.append(member)
        return self.stmt("", "INCLUDE", f"MEMBER={member}")

    def if_(self, cond: str) -> int:
        self._guard = cond
        return self.stmt("", "IF", f"({cond}) THEN")

    def else_(self) -> int:
        self._guard = f"NOT ({self._guard})"
        return self.stmt("", "ELSE")

    def endif(self) -> int:
        self._guard = None
        return self.stmt("", "ENDIF")

    def step(self, name: str, pgm: Optional[str] = None, proc: Optional[str] = None, parm: Optional[str] = None,
             cond: Optional[str] = None, region: Optional[str] = None, bare_proc: bool = False, **overrides: str) -> Step:
        """EXEC PGM= or EXEC PROC= (bare_proc: `EXEC NAME` without the PROC= keyword)."""
        ops = []
        if pgm:
            ops.append(f"PGM={pgm}")
        elif proc:
            ops.append(proc if bare_proc else f"PROC={proc}")
        if parm is not None:
            ops.append(f"PARM={parm}")
        if cond:
            ops.append(f"COND={cond}")
        if region:
            ops.append(f"REGION={region}")
        for k, v in overrides.items():
            ops.append(f"{k}={v}")
        line = self.stmt(name, "EXEC", ",".join(ops))
        st = Step(name=name, pgm=pgm, proc=proc, parm=parm, cond=cond, line=line, guard=self._guard,
                  overrides={k: v for k, v in overrides.items() if not k.startswith(("PARM.", "COND."))})
        for k, v in overrides.items():
            if k.startswith("PARM."):
                st.parm_for[k[5:]] = v
            elif k.startswith("COND."):
                st.cond_for[k[5:]] = v
        target = self._in_instream if self._in_instream is not None else self
        target.steps.append(st)
        self._cur = st
        return st

    def dd(self, name: str, dsn: Optional[str] = None, disp: Optional[str] = None, extra: str = "",
           dummy: bool = False, sysout: Optional[str] = None, member: Optional[str] = None,
           path: Optional[str] = None, trailing_comment: str = "") -> Dd:
        ops = []
        if dummy:
            ops.append("DUMMY")
        elif sysout is not None:
            ops.append(f"SYSOUT={sysout}")
        elif path:
            ops.append(f"PATH='{path}'")
        else:
            if member:
                ops.append(f"DSN={dsn}({member})")
            else:
                ops.append(f"DSN={dsn}")
            if disp:
                ops.append(f"DISP={disp}")
        if extra:
            ops.append(extra)
        text = ",".join(ops)
        if trailing_comment:
            text = text + " " + trailing_comment
        is_override = "." in name
        line = self.stmt(name, "DD", text)
        d = Dd(name=name.split(".")[-1], dsn=dsn, disp=disp, operands=text, member=member, line=line,
               is_override=is_override, dummy=dummy, sysout=sysout is not None, path=path,
               referback=dsn if dsn and dsn.startswith("*.") else None)
        if is_override:
            procstep = name.split(".")[0]
            self._cur.proc_dd_overrides.append((procstep, d))
        else:
            self._cur.dds.append(d)
        return d

    def dd_concat(self, dsn: str, disp: str = "SHR") -> int:
        n = self.stmt("", "DD", f"DSN={dsn},DISP={disp}")
        self._cur.dds[-1].concat.append((dsn, disp))
        return n

    def dd_inline(self, name: str, cards: Sequence[str], delim: str = "*") -> Dd:
        line = self.stmt(name, "DD", delim)
        for c in cards:
            self.raw(c)
        self.raw("/*")
        d = Dd(name=name, inline=list(cards), line=line, operands="*")
        self._cur.dds.append(d)
        return d

    def end(self) -> int:
        return self.raw("//")

    # ----------------------------------------------------------------- truth
    def records(self) -> List[str]:
        return list(self.lines)


# --------------------------------------------------------------------------
# the generator's own expansion: what the index must say the job runs
# --------------------------------------------------------------------------

def substitute(text: Optional[str], syms: Dict[str, str]) -> Optional[str]:
    if text is None:
        return None

    def rep(m: re.Match) -> str:
        k = m.group(1)
        if k in syms:
            return syms[k]
        return m.group(0)
    out = _SYM.sub(rep, text)
    return out.replace("..", ".")


def split_gdg(dsn: str) -> Tuple[str, Optional[str]]:
    m = GDG_REL.match(dsn)
    if m and re.fullmatch(r"[+-]?\d+", m.group(2)):
        return m.group(1), m.group(2)
    return dsn, None


def utility_of(pgm: str, parm: Optional[str], sysin: Optional[List[str]], systs: Optional[List[str]]) -> Tuple[str, Optional[str], Optional[str]]:
    """(effective program, launcher, note) as the generator expects the index to record."""
    p = (pgm or "").upper()
    if p == "IKJEFT01":
        run = None
        for ln in systs or []:
            m = re.search(r"RUN\s+PROGRAM\((\w+)\)", ln, re.I)
            if m:
                run = m.group(1).upper()
                break
        if run in ("DSNTIAUL", "DSNTEP2", "DSNTIAD"):
            return f"*{run}*", "IKJEFT01", None
        return run or "?", "IKJEFT01", None
    if p == "DFSRRC00":
        m = re.match(r"^\(?\s*(DLI|BMP|MPP|ULU|DBB)\s*,\s*(\w+)\s*(?:,\s*(\w+))?", (parm or "").strip("'"), re.I)
        if m:
            return m.group(2).upper(), "DFSRRC00", f"region {m.group(1).upper()} psb {(m.group(3) or '').upper()}"
        return "?", "DFSRRC00", "parm not parseable"
    if p in ("SORT", "ICEMAN", "DFSORT", "SYNCSORT"):
        return "*SORT*", "SORT", None
    if p == "IDCAMS":
        return "*IDCAMS*", "IDCAMS", None
    if p == "IEBGENER":
        return "*COPY*", "IEBGENER", None
    if p == "IEFBR14":
        return "*NOOP*", "IEFBR14", None
    if p == "DSNUTILB":
        return "*DSNUTILB*", "DSNUTILB", None
    if p == "FTP":
        return "*FTP*", "FTP", None
    return p, None, None


@dataclass
class EffDd:
    name: str
    dsn: Optional[str]
    dsn_resolved: Optional[str]
    gdg_rel: Optional[str]
    disp: Optional[str]
    mode: str
    mode_source: str
    line: int
    is_temp: bool
    card_member: Optional[str]
    inline: Optional[List[str]]
    is_override: bool = False
    referback: Optional[str] = None
    dummy: bool = False


@dataclass
class EffStep:
    name: str
    pgm: Optional[str]
    effective_pgm: Optional[str]
    launcher: Optional[str]
    from_proc: Optional[str]
    parent_step: Optional[str]
    parm: Optional[str]
    cond: Optional[str]
    guard: Optional[str]
    line: int
    dds: List[EffDd]
    proc_called: Optional[str] = None
    note: Optional[str] = None
    cards: List[Tuple[str, int, int, str]] = dc_field(default_factory=list)   # (kind, pos, len, fmt)
    tables: List[Tuple[str, str, str]] = dc_field(default_factory=list)       # (op, table, direction)
    idcams: List[Tuple[str, str, str]] = dc_field(default_factory=list)       # (op, dsn, mode)


def direction(dd: Dd, dsn_resolved: Optional[str], gdg: Optional[str], opens: Dict[str, str]) -> Tuple[str, str]:
    """The generator's expectation, strongest signal first, as README states it."""
    if dd.sysout:
        return "sysout", "sysout"
    if dd.dummy:
        return "dummy", "dummy"
    if dd.name.upper() in opens:
        return opens[dd.name.upper()], "open_verb"
    if gdg is not None:
        return ("output" if int(gdg) > 0 else "input"), "gdg_relative"
    base = dd.name.upper()
    if base in INPUT_DDS or base.startswith("SORTIN"):
        return "input", "dd_convention"
    if base in OUTPUT_DDS or base.startswith("SORTOF"):
        return "output", "dd_convention"
    disp = (dd.disp or "").upper()
    if disp.startswith("(NEW") or disp == "NEW":
        return "output", "disp_new_weak"
    if disp.startswith("(MOD") or disp == "MOD":
        return "mod", "disp_mod_weak"
    return "undetermined", "undetermined"


def sort_cards(cards: Sequence[str]) -> List[Tuple[str, int, int, str]]:
    """(kind, pos, len, fmt) the generator wrote into SORT/INCLUDE/OMIT/OUTREC cards."""
    out: List[Tuple[str, int, int, str]] = []
    text = " ".join(c.strip() for c in cards)
    for m in re.finditer(r"\b(SORT|MERGE|INCLUDE|OMIT|INREC|OUTREC|SUM)\b\s+(?:FIELDS|COND)=\((.*?)\)(?=\s|$|,)", text, re.I):
        kind = m.group(1).upper()
        body = m.group(2)
        if kind in ("SORT", "MERGE", "SUM"):
            toks = [t.strip() for t in body.split(",")]
            i = 0
            while i + 2 < len(toks) + 1 and i + 1 < len(toks):
                try:
                    pos, ln = int(toks[i]), int(toks[i + 1])
                except ValueError:
                    break
                fmt = toks[i + 2] if i + 2 < len(toks) and not toks[i + 2].isdigit() else ""
                out.append((kind, pos, ln, fmt.upper()))
                i += 4 if fmt else 2
        elif kind in ("INCLUDE", "OMIT"):
            for mm in re.finditer(r"(\d+),(\d+),([A-Z]{2}),", body):
                out.append((kind, int(mm.group(1)), int(mm.group(2)), mm.group(3).upper()))
        else:
            for mm in re.finditer(r"(\d+),(\d+)", body):
                out.append((kind, int(mm.group(1)), int(mm.group(2)), ""))
    return out


def expand(job: JclBuilder, procs: Dict[str, JclBuilder], opens_of: Dict[str, Dict[str, str]],
           card_members: Dict[str, List[str]], includes: Dict[str, List[Dd]]) -> List[EffStep]:
    """The job's effective steps, the generator's way. opens_of: program ->
    {DD: mode from its OPEN verbs}; card_members: PARMLIB member -> cards."""
    out: List[EffStep] = []
    referbacks: Dict[str, str] = {}      # STEP.DD -> resolved dsn
    created_gens: set = set()            # (base, +n) generations created earlier in this job

    def eff_dd(dd: Dd, syms: Dict[str, str], pgm: Optional[str], step_name: str, override: bool = False) -> EffDd:
        dsn = dd.dsn
        resolved = substitute(dsn, syms) if dsn else None
        gdg = None
        if resolved and not resolved.startswith("*."):
            resolved, gdg = split_gdg(resolved)
        if resolved and resolved.startswith("*."):
            key = resolved[2:]
            parts = key.split(".")
            tgt = referbacks.get(key) or referbacks.get(f"{parts[-2]}.{parts[-1]}" if len(parts) >= 2 else key)
            resolved = tgt
        is_temp = bool(resolved and resolved.startswith("&&"))
        cards = dd.inline
        member = substitute(dd.member, syms) if dd.member else None
        if member and resolved and "(" in resolved:
            resolved = resolved.split("(")[0]
        if member and member in card_members:
            cards = card_members[member]
        mode, src = direction(dd, resolved, gdg, opens_of.get((pgm or "").upper(), {}))
        if gdg is not None and int(gdg) > 0 and (resolved, gdg) in created_gens:
            # the generation created by an earlier step of THIS job, named again with the same (+n):
            # JES resolves relative numbers once per job, so this is the same new generation, read back
            mode, src = "input", "gdg_same_job"
        if gdg is not None and int(gdg) > 0 and mode == "output":
            created_gens.add((resolved, gdg))
        if resolved and not is_temp and not dd.referback:
            referbacks[f"{step_name}.{dd.name}"] = resolved + (f"({gdg})" if gdg else "")
        return EffDd(name=dd.name, dsn=dsn, dsn_resolved=resolved, gdg_rel=gdg, disp=dd.disp, mode=mode,
                     mode_source=src, line=dd.line, is_temp=is_temp, card_member=member, inline=cards,
                     is_override=override, referback=dd.referback, dummy=dd.dummy)

    def eff_step(st: Step, syms: Dict[str, str], from_proc: Optional[str], parent: Optional[Step],
                 proc_line_member: str) -> EffStep:
        dds = [eff_dd(d, syms, None, st.name) for d in st.dds]
        parm = substitute(st.parm, syms)
        if parent is not None:
            if st.name in parent.parm_for:
                parm = parent.parm_for[st.name]
            elif parent.parm is not None and st is parent_first.get(id(parent)):
                parm = parent.parm
        cond = st.cond
        if parent is not None and st.name in parent.cond_for:
            cond = parent.cond_for[st.name]
        if parent is not None:
            # //PROCSTEP.DD overrides replace the PROC's DD of that name, or add one
            for ps, od in parent.proc_dd_overrides:
                if ps == st.name:
                    new = eff_dd(od, syms, None, st.name, override=True)
                    for k, d in enumerate(dds):
                        if d.name == od.name:
                            dds[k] = new
                            break
                    else:
                        dds.append(new)
        sysin = next((d.inline for d in dds if d.name.upper() == "SYSIN"), None)
        systs = next((d.inline for d in dds if d.name.upper() == "SYSTSIN"), None)
        eff, launcher, note = utility_of(st.pgm or "", parm, sysin, systs)
        # directions from the effective program's OPEN verbs
        opens = opens_of.get((eff or "").upper(), {})
        for d in dds:
            if d.name.upper() in opens:
                d.mode, d.mode_source = opens[d.name.upper()], "open_verb"
        es = EffStep(name=st.name, pgm=st.pgm, effective_pgm=eff if st.pgm else None, launcher=launcher,
                     from_proc=from_proc, parent_step=parent.name if parent else None, parm=parm, cond=cond,
                     guard=st.guard if parent is None else parent.guard, line=st.line, dds=dds, note=note)
        if launcher in ("SORT",) and sysin:
            es.cards = sort_cards(sysin)
        if launcher == "DSNUTILB" and sysin:
            text = " ".join(sysin)
            m = re.search(r"LOAD\s+DATA.*?INTO\s+TABLE\s+([A-Z0-9_.]+)", text, re.I | re.S)
            if m:
                es.tables.append(("LOAD", m.group(1).upper(), "write"))
        if launcher == "IDCAMS" and sysin:
            text = " ".join(x.strip() for x in sysin)
            for m in re.finditer(r"DEFINE\s+(CLUSTER|GDG)\s*\(\s*NAME\s*\(\s*([^)\s]+)\s*\)", text, re.I):
                es.idcams.append(("DEFINE " + m.group(1).upper(), m.group(2).upper(), "create"))
            for m in re.finditer(r"DELETE\s+\(?([A-Z0-9.]+)\)?", text, re.I):
                es.idcams.append(("DELETE", m.group(1).upper(), "delete"))
            for m in re.finditer(r"REPRO\s+INFILE\s*\(\s*(\w+)\s*\)\s*OUTDATASET\s*\(\s*([A-Z0-9.]+)\s*\)", text, re.I):
                es.idcams.append(("REPRO DD", m.group(1).upper(), "input"))
                es.idcams.append(("REPRO", m.group(2).upper(), "output"))
        return es

    parent_first: Dict[int, Step] = {}
    for st in job.steps:
        if st.pgm:
            es = eff_step(st, dict(job.sets), None, None, job.name)
            out.append(es)
            continue
        pb = job.instream_procs.get(st.proc or "") or procs.get(st.proc or "")
        parent_es = EffStep(name=st.name, pgm=None, effective_pgm=None, launcher=None, from_proc=None,
                            parent_step=None, parm=st.parm, cond=st.cond, guard=st.guard, line=st.line,
                            dds=[], proc_called=st.proc)
        out.append(parent_es)
        if pb is None and (st.proc or "") in IMS_SYSTEM_PROCS:
            # an IBM-supplied IMS PROC the estate never holds: the index synthesises its step from the
            # MBR= / PSB= overrides (README: proc_synthesised)
            region = IMS_SYSTEM_PROCS[st.proc]
            out.append(EffStep(name="G", pgm="DFSRRC00", effective_pgm=(st.overrides.get("MBR") or "?").upper(),
                               launcher="DFSRRC00", from_proc=st.proc, parent_step=st.name, parm=None, cond=None,
                               guard=st.guard, line=st.line, dds=[], note=f"synthesised {region} region"))
            continue
        if pb is None:
            parent_es.note = "PROC NOT FOUND"
            continue
        # symbolics: EXEC override > PROC default > SET (only for a symbol the PROC statement does not define)
        syms: Dict[str, str] = {}
        for k, v in job.sets.items():
            if k not in pb.symbolics:
                syms[k] = v
        syms.update(pb.symbolics)
        syms.update(st.overrides)
        if pb.steps:
            parent_first[id(st)] = pb.steps[0]
        for ps in pb.steps:
            es = eff_step(ps, syms, pb.proc_name or st.proc, st, pb.name)
            out.append(es)
    return out
