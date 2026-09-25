"""
synth_estate.py - the synthetic estate: three insurance systems (POLICY,
CLAIMS, BILLING) and a SHARED department, laid out as
estate\\SYSTEM\\LIBRARY\\member the way the owner's fetches land, with the
ground truth of every member written beside it (truth.json).

Deliberate defects - the shapes LESSONS 181-192 were written for:
  D1  STDHDR exists in three COPYLIBs with different content: the build
      CHOOSES a copy for every program that copies it (chosen, not partial).
  D2  a compiler listing whose copybook-source table CONTRADICTS the copy
      the build chose for one program, and confirms it for another.
  D3  POLPROCB, a procedure copybook (no level numbers), in a PROCS folder:
      filed `proc` by the folder name, never expanded.
  D4  CMNDATEA (a data copybook with `05 START-DATE`) and CMNCUSTP (a
      procedure copybook with the COBOL START verb) in COPYLIB folders:
      the classifier reads a line as Assembler and files them `asm`;
      atlas.recover must re-file them.
  D5  POLMISSB is copied by two programs (one with a REPLACING clause over
      two lines, the period inside the pseudo-text) and is nowhere in the
      estate: their listings hold its text, atlas.recover must write it.
  D6  POLSTUBB is a stub: 8-digit numbers in columns 1-8 and nothing else,
      so the build files it `empty`; the disk check must say so.
  +   POLARRVB arrives in the COPYLIB after the first build (the checker
      drops it in): recover must mark its program, the next build resolves it.
"""

from __future__ import annotations

import json
import os
import random
import re
from typing import Dict, List, Optional, Sequence, Tuple

import synth_domain as dom
from synth_cobol import CopybookDef, ProgramBuilder
from synth_jcl import JclBuilder, EffStep, expand
from synth_layout import Emitter, Item, Replacing, flatten, layout, render_items, truth_rows

SYSTEMS = ("POLICY", "CLAIMS", "BILLING")
S3 = dom.SYS3
PFX = {"POLICY": {"master": "PM", "trans": "PT", "extract": "PX", "history": "PH", "control": "PC", "work": "PW",
                  "comm": "PA", "report": "PR", "seg": "PS", "seg2": "PSC", "gen1": "PG1", "gen2": "PG2", "pcb": "POL"},
       "CLAIMS": {"master": "CM", "trans": "CT", "extract": "CX", "history": "CH", "control": "CC", "work": "CW",
                  "comm": "CA", "report": "CR", "seg": "CS", "seg2": "CSC", "gen1": "CG1", "gen2": "CG2", "pcb": "CLM"},
       "BILLING": {"master": "BM", "trans": "BT", "extract": "BX", "history": "BH", "control": "BC", "work": "BW",
                   "comm": "BA", "report": "BR", "seg": "BS", "seg2": "BSC", "gen1": "BG1", "gen2": "BG2", "pcb": "BIL"}}
HLQ = dom.HLQ


def lib(system: str, kind: str) -> str:
    """Library (dataset) name for a kind of member in a system."""
    s3 = "CMN" if system == "SHARED" else S3[system]
    return {"src": f"PROD.{s3}.SRC", "copy": f"PROD.{s3}.COPYLIB", "jcl": f"PROD.{s3}.JCLLIB",
            "proc": f"PROD.{s3}.PROCLIB", "parm": f"PROD.{s3}.PARMLIB", "dbd": f"PROD.{s3}.DBDSRC",
            "psb": f"PROD.{s3}.PSBSRC", "bms": f"PROD.{s3}.BMS", "dclgen": f"PROD.{s3}.DCLGEN",
            "procs": f"PROD.{s3}.PROCS", "csd": "PROD.CICS.CSD", "stage1": "PROD.IMS.STAGE1",
            "docs": "DOCS"}[kind]


EXT = {"src": ".cbl", "copy": ".cpy", "jcl": ".jcl", "proc": ".prc", "parm": ".ctl", "dbd": ".dbd", "psb": ".psb",
       "bms": ".bms", "dclgen": ".cpy", "procs": ".txt", "csd": ".csd", "stage1": ".imsgen", "docs": ".txt"}


def _split_operands(text: str) -> List[str]:
    out, cur, depth, q = [], "", 0, False
    for ch in text:
        if ch == "'":
            q = not q
        elif not q:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                out.append(cur)
                cur = ""
                continue
        cur += ch
    out.append(cur)
    return out


def hlasm(label: str, op: str, operands: str) -> List[str]:
    """One HLASM macro statement as records: the label in column 1, the operation, the operands
    broken at commas so no record passes column 71, a continuation character in column 72 and
    the next record starting in column 16."""
    head = f"{label:<8} {op:<6} " if label else f"{'':<8} {op:<6} "
    parts = _split_operands(operands)
    lines: List[str] = []
    cur = head
    for k, p in enumerate(parts):
        tail = "," if k < len(parts) - 1 else ""
        cand = cur + p + tail
        if len(cand) > 71 and cur.strip() != head.strip():
            lines.append(cur.ljust(71) + "X")
            cur = " " * 15 + p + tail
        else:
            cur = cand
    lines.append(cur)
    return lines


def message_table(system: str, pfx: str, split: bool = True) -> Tuple[List[Item], List[str]]:
    """`01 xxx-MSG-TABLE.` of FILLER VALUEs and its REDEFINES. split: the code and the text as two
    FILLERs (the shape README documents for `literal`); else one 50-byte literal holding both."""
    msgs = dom.MESSAGES[system]
    kids: List[Item] = []
    codes: List[str] = []
    for k, m in enumerate(msgs, 1):
        code = f"E{k:03d}"
        codes.append(code)
        if split:
            kids.append(Item(5, "FILLER", pic="X(04)", value=dom.lit(code)))
            kids.append(Item(5, "FILLER", pic="X(46)", value=dom.lit((" " + m).ljust(46)[:46])))
        else:
            kids.append(Item(5, "FILLER", pic="X(50)", value=dom.lit(f"{code} {m}".ljust(50)[:50])))
    tbl = Item(1, f"{pfx}-MSG-TABLE", children=kids)
    red = Item(1, f"{pfx}-MSG-TBL", redefines=f"{pfx}-MSG-TABLE",
               children=[dom.group(5, f"{pfx}-MSG-ENTRY", [dom.x(10, f"{pfx}-MSG-CODE", 4), dom.x(10, "FILLER", 1),
                                                           dom.x(10, f"{pfx}-MSG-TEXT", 45)], occurs=len(msgs))])
    return [tbl, red], codes


class Estate:
    def __init__(self, out_dir: str, seed: int = 20260925, scale: int = 12) -> None:
        self.scale = scale
        self.out = os.path.abspath(out_dir)
        self.root = os.path.join(self.out, "estate")
        self.listings_dir = os.path.join(self.out, "listings")
        self.seed = seed
        self.rng = random.Random(seed)
        self.files: Dict[str, List[str]] = {}          # path -> records
        self.members: List[dict] = []
        self.books: Dict[str, CopybookDef] = {}       # by name (the resolvable copy for the program's own system)
        self.book_copies: Dict[str, List[CopybookDef]] = {}   # every copy by name
        self.copy_truth: Dict[str, dict] = {}         # "NAME" or "NAME@LIBRARY" -> truth
        self.programs: Dict[str, dict] = {}
        self.prog_builders: Dict[str, ProgramBuilder] = {}
        self.jobs: Dict[str, dict] = {}
        self.procs: Dict[str, JclBuilder] = {}
        self.proc_truth: Dict[str, dict] = {}
        self.cards: Dict[str, List[str]] = {}
        self.ims: Dict[str, dict] = {"dbds": {}, "psbs": {}}
        self.transactions: List[dict] = []
        self.screens: Dict[str, dict] = {}
        self.db2: Dict[str, dict] = {}
        self.tables: Dict[str, List[dom.Table]] = {}
        self.defects: Dict[str, dict] = {}
        self.listings: Dict[str, dict] = {}
        self.record_meta: Dict[str, dom.RecordMeta] = {}
        self.opens_of: Dict[str, Dict[str, str]] = {}    # program -> {DD: mode}
        self.interfaces: List[dict] = []
        self.docs: Dict[str, dict] = {}
        self.arrived: dict = {}
        self.incremental: dict = {}
        self.notes: List[str] = []

    # ------------------------------------------------------------- helpers
    def path(self, system: str, kind: str, member: str, ext: Optional[str] = None) -> str:
        return os.path.join(self.root, system, lib(system, kind), member + (ext if ext is not None else EXT[kind]))

    def add_file(self, path: str, records: Sequence[str], name: str, kind: str, system: str, library: str,
                 status: str = "ok", why: str = "", role: str = "") -> None:
        self.files[path] = list(records)
        self.members.append({"name": name, "kind": kind, "system": system, "library": library, "path": path,
                             "status": status, "why": why, "role": role})

    def register_book(self, cb: CopybookDef, primary: bool = True) -> None:
        self.book_copies.setdefault(cb.name, []).append(cb)
        if primary or cb.name not in self.books:
            self.books[cb.name] = cb

    # ----------------------------------------------------------- copybooks
    def write_copybook(self, system: str, name: str, roots: Sequence[Item], kind: str = "copy",
                       comments: Sequence[str] = (), fragment: bool = False, style: str = "std",
                       seq: bool = False, tag: str = "", nested: Sequence[str] = (), ck: str = "data",
                       status: str = "ok", why: str = "", role: str = "", primary: bool = True,
                       page_headers: bool = False, extra_lines_before: Sequence[str] = ()) -> CopybookDef:
        em = Emitter()
        for c in comments:
            em.comment(c)
        for ln in extra_lines_before:
            em.raw(ln)
        rows: List[list] = []
        if fragment:
            virtual = Item(1, "", children=list(roots))
            layout(virtual)
        else:
            for r in roots:
                layout(r)
        for k, r in enumerate(roots):
            if page_headers and k:
                em.raw("      /")                                     # a page eject comment between entries
            render_items(em, r, style=style)
        for r in roots:
            rows.extend(truth_rows(r, "COPY", member=name))
        # the 88 lines, read back from what was rendered
        conds: List[dict] = []
        for r in roots:
            for it in flatten(r):
                for cname, vals in it.conds:
                    ln = next((i for i, rec in enumerate(em.lines, 1) if re.match(rf"\s+88\s+{re.escape(cname)}\b", rec)), 0)
                    conds.append({"name": cname, "parent": it.name, "values": vals, "line": ln})
        recs = []
        for k, ln in enumerate(em.lines, 1):
            rec = ln.ljust(72) if (seq or tag) else ln
            if seq:
                rec = f"{k * 100:06d}" + rec[6:]
            if tag:
                rec = rec + (tag + f"{k:04d}")[:8]
            recs.append(rec.rstrip() if not tag else rec)
        path = self.path(system, kind, name)
        cb = CopybookDef(name=name, roots=list(roots), kind=ck, nested=list(nested), text_lines=recs,
                         system=system, library=lib(system, kind), path=path)
        self.register_book(cb, primary)
        self.add_file(path, recs, name, "copybook", system, lib(system, kind), status, why, role)
        key = name if name not in self.copy_truth else f"{name}@{lib(system, kind)}"
        if name in self.copy_truth and "@" not in key:
            pass
        self.copy_truth[key] = {"name": name, "system": system, "library": lib(system, kind), "path": path,
                                "kind": ck, "fields": rows, "conds": conds, "nested": list(nested),
                                "fragment": fragment, "lines": len(recs), "status": status, "why": why,
                                "record_length": (virtual.length if fragment else max((r.offset + r.length * (r.occurs or 1)) for r in roots)) if roots else 0}
        return cb

    def write_text_copybook(self, system: str, name: str, lines: Sequence[str], kind: str = "copy",
                            ck: str = "procedure", paragraphs: Sequence[str] = (), status: str = "ok", why: str = "",
                            role: str = "", ext: Optional[str] = None, member_kind: str = "copybook",
                            names: Sequence[str] = ()) -> CopybookDef:
        path = self.path(system, kind, name, ext)
        cb = CopybookDef(name=name, roots=[], kind=ck, paragraphs=list(paragraphs), text_lines=list(lines),
                         system=system, library=lib(system, kind), path=path)
        self.register_book(cb)
        self.add_file(path, lines, name, member_kind, system, lib(system, kind), status, why, role)
        self.copy_truth[name] = {"name": name, "system": system, "library": lib(system, kind), "path": path,
                                 "kind": ck, "fields": [], "conds": [], "nested": [], "fragment": False,
                                 "lines": len(lines), "status": status, "why": why, "paragraphs": list(paragraphs),
                                 "record_length": 0}
        return cb

    def build_copybooks(self) -> None:
        rng = self.rng
        for sys_ in SYSTEMS:
            p = PFX[sys_]
            s3 = S3[sys_]
            variant = SYSTEMS.index(sys_)
            # the address sub-layout, copied from inside the master record (LESSONS 184: a nested COPY)
            addr = Item(1, "", children=[dom.x(10, f"{p['master']}-ADDR-LINE", 30, occurs=3), dom.x(10, f"{p['master']}-CITY", 20),
                                         dom.flag(10, f"{p['master']}-PROV", [(f"{p['master']}-VALID-PROV", dom.PROVINCES)], n=2, style="VALUES ARE"),
                                         dom.x(10, f"{p['master']}-POSTAL-CD", 9)])
            addr_items = addr.children
            # master record: a fragment of 05s, the ADDRESS group replaced by a nested COPY
            items, meta = dom.master_record(rng, sys_, p["master"], variant)
            self.record_meta[f"{s3}MASTR"] = meta
            addr_group = next(it for it in items if it.name == f"{p['master']}-ADDRESS")
            # write the address copybook first (its own rows start at 0: it is a fragment of its own)
            self.write_copybook(sys_, f"{s3}ADDRA", [Item(5, f"{p['master']}-ADDRESS", children=[c.clone() for c in addr_group.children])],
                                comments=[f" {sys_} ADDRESS SUB-LAYOUT - COPIED INSIDE {s3}MASTR"], fragment=True,
                                seq=(sys_ == "POLICY"), tag=(s3 + "A0") if sys_ == "CLAIMS" else "")
            # the master record with the COPY inside it: the ADDRESS group's bytes are IN the record
            # (the compiler puts them there), so every item after the COPY sits 121 bytes further on
            em = Emitter()
            em.comment("================================================================")
            em.comment(f" {sys_} MASTER RECORD LAYOUT ({s3}MASTR)")
            em.comment(f" RECORD KEY: {p['master']}-{dom.KEY_FIELD[sys_][0]}")
            em.comment("================================================================")
            root = Item(1, "", children=items)
            layout(root)
            copy_line = 0
            for k, it in enumerate(items):
                if it is addr_group:
                    copy_line = em.b(f"COPY {s3}ADDRA.") if sys_ != "BILLING" else em.b(f"COPY '{s3}ADDRA'.")
                    for sub in flatten(it):
                        sub.src = f"{s3}ADDRA"
                        sub.line = copy_line
                    continue
                render_items(em, it)
            recs = [ln for ln in em.lines]
            if sys_ == "POLICY":
                recs = [f"{k * 100:06d}" + ln.ljust(72)[6:] for k, ln in enumerate(recs, 1)]
            path = self.path(sys_, "copy", f"{s3}MASTR")
            all_rows = truth_rows(root, "COPY", member=f"{s3}MASTR")[1:]
            rows = [r for r in all_rows if r[12] == f"{s3}MASTR"]
            nested_rows = [r for r in all_rows if r[12] != f"{s3}MASTR"]
            conds = []
            for it in flatten(root):
                if it.src == f"{s3}ADDRA":
                    continue
                for cname, vals in it.conds:
                    ln = next((i for i, rec in enumerate(recs, 1) if re.match(rf".{{0,6}}\s+88\s+{re.escape(cname)}\b", rec)), 0)
                    conds.append({"name": cname, "parent": it.name, "values": vals, "line": ln})
            cb = CopybookDef(name=f"{s3}MASTR", roots=[root], kind="data", nested=[f"{s3}ADDRA"], text_lines=recs,
                             system=sys_, library=lib(sys_, "copy"), path=path)
            self.register_book(cb)
            self.add_file(path, recs, f"{s3}MASTR", "copybook", sys_, lib(sys_, "copy"), role="master record")
            self.copy_truth[f"{s3}MASTR"] = {"name": f"{s3}MASTR", "system": sys_, "library": lib(sys_, "copy"), "path": path,
                                             "kind": "data", "fields": rows, "conds": conds, "nested": [f"{s3}ADDRA"],
                                             "fragment": True, "lines": len(recs), "status": "ok", "why": "",
                                             "copy_line": copy_line, "record_length": root.length, "nested_rows": nested_rows,
                                             "note": "the ADDRESS group is a nested COPY: its 121 bytes sit inside the record, so every item after it is 121 bytes further on than the copybook's own text suggests"}
            # the other records
            for key, fn, book in (("trans", dom.trans_record, f"{s3}TRANR"), ("extract", dom.extract_record, f"{s3}EXTR"),
                                  ("history", dom.history_record, f"{s3}HISTR"), ("control", dom.control_record, f"{s3}CTLR")):
                items, meta = fn(rng, sys_, p[key], variant)
                self.record_meta[book] = meta
                if key in ("trans", "history"):
                    root = Item(1, f"{p[key]}-{ 'TRAN' if key == 'trans' else 'HIST'}-RECORD", children=items)
                    self.write_copybook(sys_, book, [root], comments=[f" {sys_} {key.upper()} RECORD"],
                                        seq=(sys_ == "CLAIMS"), tag=(s3 + "T0") if key == "trans" else "")
                else:
                    self.write_copybook(sys_, book, items, comments=[f" {sys_} {key.upper()} RECORD (FRAGMENT)"], fragment=True)
            for key, book in (("gen1", f"{s3}GEN1R"), ("gen2", f"{s3}GEN2R")):
                items, meta = dom.generic_record(rng, sys_, p[key], variant)
                self.record_meta[book] = meta
                self.write_copybook(sys_, book, [Item(1, f"{p[key]}-RECORD", children=items)],
                                    comments=[f" {sys_} GENERIC RECORD {book}"], style="osvs" if key == "gen2" else "std")
            roots, meta = dom.work_area(rng, sys_, p["work"], variant)
            self.record_meta[f"{s3}WORKA"] = meta
            self.write_copybook(sys_, f"{s3}WORKA", roots, comments=[f" {sys_} WORK AREA"] + [" " * 3 + f"PAGE HEADER {k}" for k in range(3)])
            roots, meta = dom.comm_area(rng, sys_, p["comm"], variant)
            self.record_meta[f"{s3}COMMA"] = meta
            self.write_copybook(sys_, f"{s3}COMMA", roots, comments=[f" {sys_} COMMUNICATION AREA"])
            roots, meta = dom.report_line(rng, sys_, p["report"], variant)
            self.record_meta[f"{s3}RPTL"] = meta
            heads = dom.report_headings(sys_, p["report"], "MASTER UPDATE")
            self.write_copybook(sys_, f"{s3}RPTL", [Item(1, f"{p['report']}-DETAIL-LINE", children=roots)] + heads,
                                comments=[f" {sys_} REPORT LINES"], page_headers=True)
            tbl, codes = message_table(sys_, p["work"], split=(sys_ != "CLAIMS"))
            self.write_copybook(sys_, f"{s3}MSGT", tbl, comments=[f" {sys_} MESSAGE TABLE" + (" (CODE AND TEXT IN ONE LITERAL)" if sys_ == "CLAIMS" else "")])
            self.record_meta[f"{s3}MSGT"] = dom.RecordMeta()
            self.notes.append(f"{s3}MSGT codes: {', '.join(codes)}")
            # IMS segment layouts: match the DBD's FIELD positions (built in build_ims)
            self.write_copybook(sys_, f"{s3}SEGA", [self._root_segment_layout(sys_)], comments=[f" {sys_} ROOT SEGMENT I/O AREA"])
            self.write_copybook(sys_, f"{s3}SEGB", [self._child_segment_layout(sys_)], comments=[f" {sys_} CHILD SEGMENT I/O AREA"])
            # the standard header: same name, three different contents (D1)
            roots, _m = dom.std_header(sys_)
            self.write_copybook(sys_, "STDHDR", roots, comments=[f" STANDARD HEADER - {sys_} VERSION"], role="D1 chosen",
                                primary=(sys_ == "POLICY"))
            # DCLGENs
            self.tables[sys_] = dom.tables_for(sys_)
            for t in self.tables[sys_]:
                L, root = dom.dclgen_lines(t, lib(sys_, "dclgen"))
                layout(root)
                em = Emitter()
                for ln in L:
                    em.raw(ln)
                render_items(em, root)
                recs = list(em.lines)
                path = self.path(sys_, "dclgen", t.dclgen)
                cb = CopybookDef(name=t.dclgen, roots=[root], kind="dclgen", text_lines=recs, system=sys_,
                                 library=lib(sys_, "dclgen"), path=path)
                self.register_book(cb)
                self.add_file(path, recs, t.dclgen, "copybook", sys_, lib(sys_, "dclgen"), role="dclgen")
                self.copy_truth[t.dclgen] = {"name": t.dclgen, "system": sys_, "library": lib(sys_, "dclgen"), "path": path,
                                             "kind": "dclgen", "fields": truth_rows(root, "COPY", member=t.dclgen), "conds": [],
                                             "nested": [], "fragment": False, "lines": len(recs), "status": "ok", "why": "",
                                             "table": t.qualified, "record_length": root.length}
                self.db2[t.qualified] = {"dclgen": t.dclgen, "system": sys_, "columns": [(c.name, c.sqltype) for c in t.columns],
                                         "programs": {}, "struct": t.struct}
        # ---- SHARED copybooks
        roots, meta = dom.date_area_with_start_date()
        self.record_meta["CMNDATEA"] = meta
        self.write_copybook("SHARED", "CMNDATEA", roots, comments=[" COMMON DATE WORK AREA (CALL 'CMNDATE' USING CMN-DATE-AREA)"],
                            status="skipped", why="filed asm by the classifier (START-DATE), re-filed by recover", role="D4 asm by content")
        roots, meta = dom.error_area()
        self.record_meta["CMNERRA"] = meta
        self.write_copybook("SHARED", "CMNERRA", roots, comments=[" COMMON ERROR AREA"])
        roots, meta = dom.cust_record()
        self.record_meta["CMNCUSTR"] = meta
        self.write_copybook("SHARED", "CMNCUSTR", roots, comments=[" SHARED CUSTOMER KSDS RECORD"], seq=True)
        roots, _m = dom.pcb_mask()
        self.write_copybook("SHARED", "CMNPCBM", roots, comments=[" DB PCB MASK - COPY WITH REPLACING ==:PCB:== BY ==XXX=="])
        roots, _m = dom.io_pcb_mask()
        self.write_copybook("SHARED", "CMNIOPCB", roots, comments=[" I/O PCB MASK"])
        roots, _m = dom.sql_work_area()
        self.write_copybook("SHARED", "CMNSQLW", roots, comments=[" SQL WORK AREA"])
        roots, _m = dom.abend_area()
        self.write_copybook("SHARED", "CMNABNDA", roots, comments=[" ABEND AREA"])
        # the procedure copybook with the COBOL START verb (LESSONS 189): asm by a line of its text
        self.write_text_copybook("SHARED", "CMNCUSTP", [
            "      *  CUSTOMER LOOKUP - PARAGRAPHS COPIED INTO THE CALLER'S SECTION",
            "       A-110-POSITION.",
            "           MOVE WS-CUST-KEY TO CF-CUST-KEY.",
            "           START CUSTFILE",
            "               KEY IS >= CF-CUST-KEY",
            "               INVALID KEY MOVE 'N' TO WS-CUST-FOUND-SW",
            "           END-START.",
            "       A-120-READ.",
            "           READ CUSTFILE NEXT",
            "               AT END MOVE 'N' TO WS-CUST-FOUND-SW",
            "           END-READ.",
            "           IF CF-CUST-NO = WS-CUST-NO",
            "               MOVE 'Y' TO WS-CUST-FOUND-SW",
            "           END-IF.",
        ], paragraphs=["A-110-POSITION", "A-120-READ"], status="skipped",
            why="filed asm by the classifier (START verb), re-filed by recover", role="D4 asm by content (procedure)")
        # D3: a procedure copybook in a PROCS folder - filed proc by the folder, no signature of its own
        self.write_text_copybook("POLICY", "POLPROCB", [
            "      *  POLICY EDIT PARAGRAPHS - PROCEDURE COPYBOOK",
            "       B-100-EDIT-KEY.",
            "           IF PT-POLICY-NO = SPACES",
            "               MOVE 'E001' TO PW-RETURN-CD",
            "               ADD 1 TO PW-ERROR-CNT",
            "           END-IF.",
            "       B-200-EDIT-DATE.",
            "           IF PT-TRAN-DT = ZEROS",
            "               MOVE 'E003' TO PW-RETURN-CD",
            "           END-IF.",
        ], kind="procs", paragraphs=["B-100-EDIT-KEY", "B-200-EDIT-DATE"], status="ok",
            why="filed proc by the folder name (PROCS)", role="D3 misfiled by folder", member_kind="proc")
        # D6: a stub - 8-digit numbers in columns 1-8, nothing else
        stub = [f"{k * 100:07d}" for k in range(1, 7)]
        self.write_text_copybook("POLICY", "POLSTUBB", stub, ck="stub", status="skipped",
                                 why="text sits in columns 1-7: filed empty", role="D6 stub", member_kind="empty")
        # the same stub with EIGHT digits: the eighth sits in column 8, so the reader sees one character of 'code'
        stub8 = [f"{k * 100:08d}" for k in range(1, 7)]
        self.write_text_copybook("POLICY", "POLSTUBC", stub8, ck="stub", status="ok",
                                 why="an 8-digit stub: column 8 holds a digit the reader takes for code", role="D6 stub (8 digits)",
                                 member_kind="copybook")
        # D5: the missing copybook's text (only in listings) - a full 01 the two programs rename
        miss_root = Item(1, "PMSS-AUDIT-REC", children=[dom.x(5, "PMSS-AUDIT-KEY", 12), dom.x(5, "PMSS-AUDIT-USER", 8),
                                                        dom.n9(5, "PMSS-AUDIT-DT", 8), dom.packed(5, "PMSS-AUDIT-AMT", 9),
                                                        dom.x(5, "PMSS-AUDIT-TEXT", 40)])
        layout(miss_root)
        em = Emitter()
        em.comment(" POLICY AUDIT RECORD - THE COPYBOOK MISSING FROM THE ESTATE")
        render_items(em, miss_root)
        self.missing_book = CopybookDef(name="POLMISSB", roots=[miss_root], kind="missing", text_lines=list(em.lines))
        self.books["POLMISSB"] = self.missing_book
        self.copy_truth["POLMISSB"] = {"name": "POLMISSB", "system": "POLICY", "library": "RECOVERED-COPYBOOKS", "path": "",
                                       "kind": "missing", "fields": truth_rows(miss_root, "COPY", member="POLMISSB"), "conds": [],
                                       "nested": [], "fragment": False, "lines": len(em.lines), "status": "recovered",
                                       "why": "not in the estate; recovered from the listings", "record_length": miss_root.length}
        # the copybook that ARRIVES after the first build
        arr_root = Item(1, "PARR-RESTART-AREA", children=[dom.x(5, "PARR-RESTART-KEY", 12), dom.n9(5, "PARR-RESTART-CNT", 7),
                                                          dom.flag(5, "PARR-RESTART-SW", [("PARR-RESTARTING", ["Y"]), ("PARR-FRESH", ["N"])], value="'N'")])
        layout(arr_root)
        em = Emitter()
        em.comment(" POLICY RESTART AREA - ARRIVES AFTER THE FIRST BUILD")
        render_items(em, arr_root)
        self.arrived_book = CopybookDef(name="POLARRVB", roots=[arr_root], kind="data", text_lines=list(em.lines),
                                        system="POLICY", library=lib("POLICY", "copy"), path=self.path("POLICY", "copy", "POLARRVB"))
        self.books["POLARRVB"] = self.arrived_book
        self.arrived = {"name": "POLARRVB", "path": self.arrived_book.path, "lines": list(em.lines),
                        "fields": truth_rows(arr_root, "COPY", member="POLARRVB")}
        # the symbolic maps (BMS-generated copybooks) are written by build_cics

    def _root_segment_layout(self, sys_: str) -> Item:
        p = PFX[sys_]["seg"]
        key, klen = dom.KEY_FIELD[sys_]
        kids = [dom.x(5, f"{p}-{key}", klen), dom.x(5, f"{p}-STATUS", 2), dom.x(5, f"{p}-INSURED-NAME", 30),
                dom.n9(5, f"{p}-EFFECTIVE-DT", 8), dom.x(5, f"{p}-PROV", 2), dom.x(5, f"{p}-AGENT-ID", 6),
                dom.x(5, f"{p}-FILLER", 120 - (klen + 2 + 30 + 8 + 2 + 6))]
        return Item(1, f"{p}-ROOT-SEG", children=kids)

    def _child_segment_layout(self, sys_: str) -> Item:
        p = PFX[sys_]["seg2"]
        kids = [dom.x(5, f"{p}-ITEM-SEQ", 4), dom.x(5, f"{p}-ITEM-CODE", 4), dom.packed(5, f"{p}-ITEM-LIMIT", 11),
                dom.packed(5, f"{p}-ITEM-PREM", 7), dom.n9(5, f"{p}-ITEM-DT", 8), dom.x(5, f"{p}-FILLER", 60 - (4 + 4 + 7 + 5 + 8))]
        return Item(1, f"{p}-CHILD-SEG", children=kids)

    # ------------------------------------------------------------------ IMS
    def build_ims(self) -> None:
        for sys_ in SYSTEMS:
            s3 = S3[sys_]
            key, klen = dom.KEY_FIELD[sys_]
            dbd = f"{s3}DBD"
            root = f"{s3}ROOT"
            child = f"{s3}CHLD"
            access = {"POLICY": "HDAM", "CLAIMS": "HIDAM", "BILLING": "HDAM"}[sys_]
            kf = key.replace("-", "")[:8]
            L = [f"{dbd:<8} DBD   NAME={dbd},ACCESS=({access},OSAM)",
                 f"         DATASET DD1={s3}DBD1,DEVICE=3390,SIZE=4096",
                 f"         SEGM  NAME={root},PARENT=0,BYTES=120,RULES=(LLL,LAST)",
                 f"         FIELD NAME=({kf},SEQ,U),BYTES={klen},START=1,TYPE=C",
                 f"         FIELD NAME=STATUS,BYTES=2,START={klen + 1},TYPE=C",
                 f"         FIELD NAME=INSNAME,BYTES=30,START={klen + 3},TYPE=C",
                 f"         FIELD NAME=EFFDT,BYTES=8,START={klen + 33},TYPE=C",
                 f"         FIELD NAME=PROV,BYTES=2,START={klen + 41},TYPE=C"]
            xd = []
            if sys_ == "CLAIMS":
                L.append(f"         LCHILD NAME=(CLMXSEG,CLMXDBD),PTR=INDX")
                L.append(f"         XDFLD NAME=CLMXNAME,SRCH=INSNAME,SEGMENT={root}")
                xd.append({"name": "CLMXNAME", "segment": root, "srch": ["INSNAME"]})
            L += [f"         SEGM  NAME={child},PARENT={root},BYTES=60",
                  f"         FIELD NAME=(ITEMSEQ,SEQ,U),BYTES=4,START=1,TYPE=C",
                  f"         FIELD NAME=ITEMCODE,BYTES=4,START=5,TYPE=C",
                  f"         FIELD NAME=ITEMDT,BYTES=8,START=17,TYPE=C",
                  "         DBDGEN", "         FINISH", "         END"]
            self.add_file(self.path(sys_, "dbd", dbd), L, dbd, "dbd", sys_, lib(sys_, "dbd"))
            self.ims["dbds"][dbd] = {
                "system": sys_, "access": access, "dd1": f"{s3}DBD1", "path": self.path(sys_, "dbd", dbd),
                "segments": [{"name": root, "parent": None, "bytes": 120, "seq": kf,
                              "fields": [(kf, 1, klen, True), ("STATUS", klen + 1, 2, False), ("INSNAME", klen + 3, 30, False),
                                         ("EFFDT", klen + 33, 8, False), ("PROV", klen + 41, 2, False)]},
                             {"name": child, "parent": root, "bytes": 60, "seq": "ITEMSEQ",
                              "fields": [("ITEMSEQ", 1, 4, True), ("ITEMCODE", 5, 4, False), ("ITEMDT", 17, 8, False)]}],
                "xdfld": xd}
            if sys_ == "CLAIMS":
                X = ["CLMXDBD  DBD   NAME=CLMXDBD,ACCESS=INDEX",
                     "         DATASET DD1=CLMXDD1,DEVICE=3390",
                     "         SEGM  NAME=CLMXSEG,PARENT=0,BYTES=30",
                     "         FIELD NAME=(CLMXKEY,SEQ,U),BYTES=30,START=1",
                     "         LCHILD NAME=(CLMROOT,CLMDBD),INDEX=CLMXNAME",
                     "         DBDGEN", "         FINISH", "         END"]
                self.add_file(self.path(sys_, "dbd", "CLMXDBD"), X, "CLMXDBD", "dbd", sys_, lib(sys_, "dbd"))
                self.ims["dbds"]["CLMXDBD"] = {"system": sys_, "access": "INDEX", "dd1": "CLMXDD1", "path": self.path(sys_, "dbd", "CLMXDBD"),
                                               "segments": [{"name": "CLMXSEG", "parent": None, "bytes": 30, "seq": "CLMXKEY",
                                                             "fields": [("CLMXKEY", 1, 30, True)]}], "xdfld": []}
            if sys_ == "POLICY":
                G = ["POLGSAM  DBD   NAME=POLGSAM,ACCESS=(GSAM,BSAM)",
                     "         DATASET DD1=POLGSOUT,DD2=POLGSIN,RECFM=F,RECORD=200",
                     "         DBDGEN", "         FINISH", "         END"]
                self.add_file(self.path(sys_, "dbd", "POLGSAM"), G, "POLGSAM", "dbd", sys_, lib(sys_, "dbd"))
                self.ims["dbds"]["POLGSAM"] = {"system": sys_, "access": "GSAM", "dd1": "POLGSOUT", "dd2": "POLGSIN",
                                               "path": self.path(sys_, "dbd", "POLGSAM"), "segments": [], "xdfld": []}
            # PSBs
            def psb(name: str, pcbs: List[dict], cmpat: Optional[str] = None, lang: str = "COBOL") -> None:
                L2 = []
                for pc in pcbs:
                    if pc["type"] == "TP":
                        L2 += hlasm(pc.get("label", ""), "PCB", "TYPE=TP" + (",MODIFY=YES" if pc.get("modify") else "")
                                    + (",EXPRESS=YES" if pc.get("express") else ""))
                    elif pc["type"] == "GSAM":
                        L2 += hlasm(pc.get("label", ""), "PCB", f"TYPE=GSAM,DBDNAME={pc['dbd']},PROCOPT={pc['procopt']}")
                    else:
                        ops = f"TYPE=DB,DBDNAME={pc['dbd']},PROCOPT={pc['procopt']},KEYLEN={pc['keylen']}"
                        if pc.get("list_no"):
                            ops += ",LIST=NO"
                        if pc.get("procseq"):
                            ops += f",PROCSEQ={pc['procseq']}"
                        L2 += hlasm(pc.get("label", ""), "PCB", ops)
                        for sg in pc.get("sensegs", []):
                            L2 += hlasm("", "SENSEG", f"NAME={sg[0]},PARENT={sg[1] or 0},PROCOPT={pc['procopt']}")
                L2 += hlasm("", "PSBGEN", f"LANG={lang},PSBNAME={name}" + (f",CMPAT={cmpat}" if cmpat else ""))
                L2.append("         END")
                self.add_file(self.path(sys_, "psb", name), L2, name, "psb", sys_, lib(sys_, "psb"))
                self.ims["psbs"][name] = {"system": sys_, "cmpat": cmpat, "path": self.path(sys_, "psb", name),
                                          "pcbs": [{"ordinal": k, **pc} for k, pc in enumerate(pcbs, 1)],
                                          "io_first": bool(cmpat == "YES" or any(pc["type"] == "TP" for pc in pcbs))}
            sens = [(root, None), (child, root)]
            psb(f"{s3}IMS01", [{"type": "DB", "dbd": dbd, "procopt": "A", "keylen": klen + 4, "sensegs": sens, "label": f"{s3}PCB1"}], cmpat="YES")
            if sys_ == "POLICY":
                psb("POLIMS02", [{"type": "GSAM", "dbd": "POLGSAM", "procopt": "LS", "label": "POLGSPCB"},
                                 {"type": "DB", "dbd": dbd, "procopt": "G", "keylen": klen + 4, "sensegs": sens},
                                 {"type": "DB", "dbd": dbd, "procopt": "GO", "keylen": klen, "sensegs": sens[:1], "list_no": True}])
            elif sys_ == "CLAIMS":
                psb("CLMIMS02", [{"type": "DB", "dbd": dbd, "procopt": "G", "keylen": 30, "sensegs": sens, "procseq": "CLMXNAME"},
                                 {"type": "DB", "dbd": dbd, "procopt": "A", "keylen": klen + 4, "sensegs": sens}])
            else:
                psb("BILIMS02", [{"type": "DB", "dbd": dbd, "procopt": "G", "keylen": klen, "sensegs": sens[:1]},
                                 {"type": "DB", "dbd": dbd, "procopt": "A", "keylen": klen + 4, "sensegs": sens}])
            psb(f"{s3}ONL03", [{"type": "TP", "modify": True, "label": f"{s3}ALTPCB"},
                               {"type": "DB", "dbd": dbd, "procopt": "GO", "keylen": klen, "sensegs": sens[:1]}])
        # stage-1
        L = ["*        IMS STAGE-1 SYSTEM DEFINITION - SYNTHETIC ESTATE"]
        for sys_ in SYSTEMS:
            s3 = S3[sys_]
            L += [f"         APPLCTN PSB={s3}ONL03,PGMTYPE=TP,SCHDTYP=PARALLEL",
                  f"         TRANSACT CODE={s3}3,PRTY=(7,10,2),INQUIRY=NO,MODE=SNGL,",
                  "               MSGTYPE=(SNGLSEG,RESPONSE,1)",
                  f"         APPLCTN PSB={s3}ONL04,PGMTYPE=TP",
                  f"         TRANSACT CODE={s3}4,PRTY=(1,1,1),INQUIRY=YES",
                  f"         APPLCTN PSB={s3}IMS02,PGMTYPE=BATCH",
                  f"         DATABASE DBD={s3}DBD,ACCESS=UP"]
            self.transactions += [{"code": f"{s3}3", "system": "ims_dc", "program": f"{s3}ONL03", "psb": f"{s3}ONL03", "in_index": True},
                                  {"code": f"{s3}4", "system": "ims_dc", "program": f"{s3}ONL04", "psb": f"{s3}ONL04", "in_index": False}]
        L += ["         APPLCTN GPSB=CMNGENP,PGMTYPE=TP,LANG=COBOL",
              "         TRANSACT CODE=(GENA,GENB),PRTY=(1,1,1)",
              "         DATABASE DBD=CLMXDBD,ACCESS=RO"]
        self.transactions += [{"code": "GENA", "system": "ims_dc", "program": "CMNGENP", "psb": None, "in_index": False},
                              {"code": "GENB", "system": "ims_dc", "program": "CMNGENP", "psb": None, "in_index": False}]
        self.add_file(self.path("SHARED", "stage1", "IMSGEN1"), L, "IMSGEN1", "imsgen", "SHARED", lib("SHARED", "stage1"))

    # ----------------------------------------------------------------- CICS
    def build_cics(self) -> None:
        csd = ["* DFHCSDUP EXTRACT - SYNTHETIC ESTATE ONLINE DEFINITIONS"]
        for sys_ in SYSTEMS:
            s3 = S3[sys_]
            mapset, map1 = f"{s3}MAPS", f"{s3}MAP1"
            key, klen = dom.KEY_FIELD[sys_]
            fields = [("KEYIN", 3, 12, klen, "(UNPROT,FSET)", None, "X", "X"), ("STATUS", 4, 12, 2, "(UNPROT)", "AC", "X", "X"),
                      ("AMOUNT", 5, 12, 12, "(UNPROT,NUM)", None, "9(10).99", "Z(9)9.99"), ("NAME", 6, 12, 30, "(ASKIP,BRT)", None, None, None),
                      ("ERRMSG", 23, 1, 60, "(ASKIP,BRT)", None, None, None)]
            B = [f"{mapset:<8} DFHMSD TYPE=&SYSPARM,MODE=INOUT,LANG=COBOL,STORAGE=AUTO,",
                 "               TIOAPFX=YES",
                 f"{map1:<8} DFHMDI SIZE=(24,80),LINE=1,COLUMN=1",
                 f"         DFHMDF POS=(1,30),LENGTH=20,ATTRB=(ASKIP,BRT),INITIAL='{sys_} MAINTENANCE'",
                 f"         DFHMDF POS=(3,1),LENGTH=10,ATTRB=ASKIP,INITIAL='{key[:10]}:'"]
            truth_fields = []
            B = [f"{mapset:<8} DFHMSD TYPE=&SYSPARM,MODE=INOUT,LANG=COBOL,STORAGE=AUTO,",
                 "               TIOAPFX=YES",
                 f"{map1:<8} DFHMDI SIZE=(24,80),LINE=1,COLUMN=1"]
            B += hlasm("", "DFHMDF", f"POS=(1,30),LENGTH=20,ATTRB=(ASKIP,BRT),INITIAL='{sys_} MAINTENANCE'")
            B += hlasm("", "DFHMDF", f"POS=(3,1),LENGTH=10,ATTRB=ASKIP,INITIAL='{key[:10]}:'")
            for k, (nm, row, col, ln, attrb, init, picin, picout) in enumerate(fields, 1):
                ops = f"POS=({row},{col}),LENGTH={ln},ATTRB={attrb}"
                if init:
                    ops += f",INITIAL='{init}'"
                if picin:
                    ops += f",PICIN='{picin}',PICOUT='{picout}'"
                B += hlasm(nm, "DFHMDF", ops)
                truth_fields.append({"name": nm, "row": row, "col": col, "length": ln, "initial": init, "attrb": attrb,
                                     "picin": picin, "picout": picout})
            B += ["         DFHMSD TYPE=FINAL", "         END"]
            self.add_file(self.path(sys_, "bms", mapset), B, mapset, "bms", sys_, lib(sys_, "bms"))
            self.screens[mapset] = {"system": sys_, "path": self.path(sys_, "bms", mapset), "map": map1, "fields": truth_fields,
                                    "constants": [f"{sys_} MAINTENANCE", f"{key[:10]}:"]}
            # the symbolic map copybook, as the BMS assembler generates it
            kids = [Item(5, "FILLER", pic="X(12)")]
            for f in truth_fields:
                kids.append(Item(5, f"{f['name']}L", pic="S9(4)", usage="COMP"))
                kids.append(Item(5, f"{f['name']}F", pic="X"))
                kids.append(Item(5, f"{f['name']}I", pic=f"X({f['length']})"))
            in_map = Item(1, f"{map1}I", children=kids)
            out_map = Item(1, f"{map1}O", redefines=f"{map1}I", children=[Item(5, "FILLER", pic="X(12)")] + [
                it for f in truth_fields for it in (Item(5, "FILLER", pic="X(3)"), Item(5, f"{f['name']}O", pic=f"X({f['length']})"))])
            self.write_copybook(sys_, mapset, [in_map, out_map], comments=[f" SYMBOLIC MAP FOR MAPSET {mapset} (GENERATED)"], role="symbolic map")
            csd += [f"DEFINE TRANSACTION({s3}1) GROUP({s3}GRP)",
                    f"       DESCRIPTION({sys_} MAINTENANCE)",
                    f"       PROGRAM({s3}ONL01) TWASIZE(0) PROFILE(DFHCICST)",
                    "       STATUS(ENABLED) TASKDATALOC(ANY)",
                    f"DEFINE TRANSACTION({s3}2) GROUP({s3}GRP) PROGRAM({s3}ONL02)",
                    f"DEFINE PROGRAM({s3}ONL01) GROUP({s3}GRP) LANGUAGE(COBOL) RELOAD(NO) STATUS(ENABLED)",
                    f"DEFINE PROGRAM({s3}ONL02) GROUP({s3}GRP) LANGUAGE(COBOL)",
                    f"DEFINE MAPSET({mapset}) GROUP({s3}GRP) STATUS(ENABLED)",
                    f"DEFINE FILE({s3}MAST) GROUP({s3}GRP)",
                    f"       DSNAME({HLQ[sys_]}.MASTER.KSDS) LSRPOOLID(1)",
                    "       ADD(YES) READ(YES) UPDATE(YES) DELETE(YES)",
                    f"DEFINE TDQUEUE({s3}Q) GROUP({s3}GRP) TYPE(EXTRA) DDNAME({s3}QLOG)",
                    f"       DSNAME({HLQ[sys_]}.TDQ.LOG) DISPOSITION(SHR)",
                    f"DEFINE TRANSACTION({s3}R) GROUP({s3}GRP) REMOTESYSTEM(CIC2)"]
            self.transactions += [{"code": f"{s3}1", "system": "cics", "program": f"{s3}ONL01", "group": f"{s3}GRP", "in_index": True},
                                  {"code": f"{s3}2", "system": "cics", "program": f"{s3}ONL02", "group": f"{s3}GRP", "in_index": True},
                                  {"code": f"{s3}R", "system": "cics", "program": None, "group": f"{s3}GRP", "in_index": False, "remote": "CIC2"}]
            self.interfaces.append({"kind": "cics_tdq", "detail": f"{s3}Q -> {HLQ[sys_]}.TDQ.LOG", "system": sys_})
        self.add_file(self.path("SHARED", "csd", "CICSCSD1"), csd, "CICSCSD1", "csd", "SHARED", lib("SHARED", "csd"))
        self.cics_files = {f"{S3[s]}MAST": f"{HLQ[s]}.MASTER.KSDS" for s in SYSTEMS}

    # ---------------------------------------------------------------- docs
    def build_docs(self) -> None:
        for sys_ in SYSTEMS:
            s3 = S3[sys_]
            L = [f"{sys_} NIGHTLY RUNBOOK", "", f"Job {s3}NIGHT runs the master update ({s3}UPD01) and the extract ({s3}EXT01).",
                 f"If {s3}UPD01 abends with U100 check the control file record and rerun {s3}RERUN.",
                 f"The extract feeds {s3}RPT01 after the sort step; message E003 means the effective date is wrong.", ""]
            name = f"{s3}-NIGHTLY-RUNBOOK"
            path = os.path.join(self.root, "SHARED", "DOCS", name + ".txt")
            self.add_file(path, L, name.upper(), "doc", "SHARED", "DOCS", role="doc")
            self.docs[name.upper()] = {"mentions": [f"{s3}NIGHT", f"{s3}UPD01", f"{s3}EXT01", f"{s3}RPT01", "E003"]}

    # ----------------------------------------------------------------- JCL
    def build_jcl(self) -> None:
        from synth_programs import OPENS
        # control cards
        for sys_ in SYSTEMS:
            s3 = S3[sys_]
            h = HLQ[sys_]
            key, klen = dom.KEY_FIELD[sys_]
            self.cards[f"{s3}SORT1"] = [f"* {sys_} EXTRACT SORT - KEY, ACTIVE ONLY",
                                        f"  SORT FIELDS=(1,{klen},CH,A,{klen + 1},2,CH,A)",
                                        f"  INCLUDE COND=({klen + 1},2,CH,EQ,C'AC')",
                                        "  OUTREC FIELDS=(1,200)"]
            self.cards[f"RUN{s3}"] = ["DSN SYSTEM(DB2P)", f"RUN PROGRAM({s3}DB201) PLAN({s3}DB201) -", f"    LIB('{h}.LOADLIB')", "END"]
            self.cards[f"{s3}TIAUL"] = [f"SELECT * FROM PRD.{self.tables[sys_][0].name}", "  WHERE STATUS_CD = 'AC';"]
            for nm, lines in list(self.cards.items()):
                if nm.startswith(s3) or nm.endswith(s3):
                    self.add_file(self.path(sys_, "parm", nm), lines, nm, "ctlcard", sys_, lib(sys_, "parm"), status="skipped")
        # PROCs
        for sys_ in SYSTEMS:
            s3 = S3[sys_]
            h = HLQ[sys_]
            pb = JclBuilder(f"{s3}NIGHT", "proc")
            pb.proc(f"{s3}NIGHT", HLQ=f"TEST.{s3}", RPTDT="00000000", CYCLE="01")
            pb.comment(f" {sys_} NIGHTLY - UPDATE, EXTRACT, REPORT")
            st = pb.step("PS010", pgm=f"{s3}UPD01", parm="'&RPTDT'")
            pb.dd("STEPLIB", "&HLQ..LOADLIB", "SHR")
            pb.dd(f"{s3}MAST", "&HLQ..MASTER.KSDS", "OLD")
            pb.dd(f"{s3}TRAN", "&HLQ..TRANS", "SHR")
            pb.dd(f"{s3}ERR", "&HLQ..ERRORS", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(TRK,(5,5)),DCB=(RECFM=FB,LRECL=133)")
            pb.dd(f"{s3}CTL", "&HLQ..CONTROL", "SHR")
            pb.dd("CUSTFILE", "PROD.CMN.CUSTOMER.KSDS", "SHR")
            pb.dd("SYSIN", dummy=True)
            pb.dd("SYSOUT", sysout="*")
            pb.step("PS020", pgm=f"{s3}EXT01", cond="(4,LT,PS010)")
            pb.dd("STEPLIB", "&HLQ..LOADLIB", "SHR")
            pb.dd(f"{s3}MAST", "&HLQ..MASTER.KSDS", "SHR")
            pb.dd(f"{s3}EXTR", "&HLQ..EXTRACT(+1)", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(50,10),RLSE),DCB=(RECFM=FB,LRECL=200,BLKSIZE=0)")
            pb.dd(f"{s3}CTL", "&HLQ..CONTROL", "SHR")
            pb.dd("SYSOUT", sysout="*")
            pb.step("PS030", pgm="SORT", cond="(0,NE)")
            pb.dd("SORTIN", "&HLQ..EXTRACT(+1)", "SHR")
            pb.dd("SORTOUT", "&HLQ..EXTRACT.SORTED", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(50,10),RLSE)")
            pb.dd("SYSIN", f"{h}.PARMLIB", "SHR", member=f"{s3}SORT1")
            pb.dd("SYSOUT", sysout="*")
            pb.step("PS040", pgm=f"{s3}RPT01")
            pb.dd("STEPLIB", "&HLQ..LOADLIB", "SHR")
            pb.dd(f"{s3}SRTD", "&HLQ..EXTRACT.SORTED", "SHR")
            pb.dd(f"{s3}RPT", "&HLQ..REPORT.G&CYCLE", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(5,5))")
            pb.dd("SYSOUT", sysout="*")
            self._add_proc(sys_, pb)
            pb = JclBuilder(f"{s3}BKUP", "proc")
            pb.proc(f"{s3}BKUP", HLQ=h)
            pb.step("BK010", pgm="IDCAMS")
            pb.dd("MASTER", "&HLQ..MASTER.KSDS", "SHR")
            pb.dd_inline("SYSIN", [f"  REPRO INFILE(MASTER) OUTDATASET({h}.BACKUP)"])
            pb.dd("SYSPRINT", sysout="*")
            self._add_proc(sys_, pb)
        pb = JclBuilder("CMNSORT", "proc")
        pb.proc("CMNSORT", IN="PROD.CMN.SORTIN", OUT="PROD.CMN.SORTOUT", CARDS="CMNSRT1")
        pb.step("SRT010", pgm="SORT")
        pb.dd("SORTIN", "&IN", "SHR")
        pb.dd("SORTOUT", "&OUT", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(10,5))")
        pb.dd("SYSIN", "PROD.CMN.PARMLIB", "SHR", member="&CARDS")
        pb.dd("SYSOUT", sysout="*")
        self._add_proc("SHARED", pb)
        # jobs
        for sys_ in SYSTEMS:
            s3 = S3[sys_]
            h = HLQ[sys_]
            key, klen = dom.KEY_FIELD[sys_]
            main_tbl = f"PRD.{self.tables[sys_][0].name}"
            # NIGHT: EXEC PROC with overrides, INCLUDE, //STEP.DD overrides
            jb = JclBuilder(f"{s3}NIGHT")
            jb.job(f"{s3}NIGHT", desc=f"{sys_} NIGHTLY")
            jb.jcllib_order([f"{h}.PROCLIB", "PROD.CMN.PROCLIB"])
            jb.joblib_dd([f"{h}.LOADLIB", "PROD.CMN.LOADLIB"])
            jb.set_(RPTDT="20260924", CYCLE="07")
            jb.comment(f" {sys_} NIGHTLY BATCH - UPDATE, EXTRACT, SORT, REPORT")
            jb.step("NIGHT", proc=f"{s3}NIGHT", HLQ=h, CYCLE="09")
            jb.dd("PS010.SYSIN", None, extra="", dummy=True) if False else None
            jb.dd_inline("PS010.SYSIN", ["RUNMODE=FULL", "REGION=ALL"])
            jb.dd(f"PS020.{s3}EXTR", f"{h}.EXTRACT.OVERRIDE(+1)", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(50,10),RLSE)")
            jb.dd("PS040.NEWDD", f"{h}.ADDED.FILE", "(NEW,CATLG)", extra="UNIT=SYSDA,SPACE=(TRK,(1,1))")
            jb.end()
            self._add_job(sys_, jb)
            # EXTRT: plain PGM steps, GDG, SORT with inline cards, report, IEBGENER copy, referback
            jb = JclBuilder(f"{s3}EXTRT")
            jb.job(f"{s3}EXTRT", desc=f"{sys_} EXTRACT", cond="(8,LT)")
            jb.set_(HLQ=h)
            jb.step("STEP010", pgm=f"{s3}EXT01", parm="'RUNMODE=DELTA'")
            jb.dd("STEPLIB", "&HLQ..LOADLIB", "SHR")
            jb.dd(f"{s3}MAST", "&HLQ..MASTER.KSDS", "SHR", trailing_comment="MASTER FILE")
            jb.dd(f"{s3}EXTR", "&HLQ..EXTRACT(+1)", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(50,10),RLSE),DCB=(RECFM=FB,LRECL=200,BLKSIZE=0)")
            jb.dd(f"{s3}CTL", "&HLQ..CONTROL", "SHR")
            jb.dd("SYSOUT", sysout="*")
            jb.step("STEP020", pgm="SORT", cond="(4,LT)")
            jb.dd("SORTIN", "&HLQ..EXTRACT(0)", "SHR")
            jb.dd("SORTOUT", "&&SORTED", "(NEW,PASS)", extra="UNIT=SYSDA,SPACE=(CYL,(50,10))")
            jb.dd_inline("SYSIN", [f"  SORT FIELDS=(1,{klen},CH,A)", f"  INCLUDE COND=({klen + 1},2,CH,EQ,C'AC')"])
            jb.dd("SYSOUT", sysout="*")
            jb.step("STEP030", pgm=f"{s3}RPT01")
            jb.dd("STEPLIB", "&HLQ..LOADLIB", "SHR")
            jb.dd(f"{s3}SRTD", "*.STEP020.SORTOUT", "(OLD,DELETE)")
            jb.dd(f"{s3}RPT", "&HLQ..REPORT.DAILY", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(5,5))")
            jb.dd("SYSOUT", sysout="*")
            jb.step("STEP040", pgm="IEBGENER")
            jb.dd("SYSUT1", "&HLQ..EXTRACT(0)", "SHR")
            jb.dd("SYSUT2", f"STG.{s3}.EXTRACT", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(50,10),RLSE)")
            jb.dd("SYSIN", dummy=True)
            jb.dd("SYSPRINT", sysout="*")
            jb.end()
            self._add_job(sys_, jb)
            # DB2LD: IKJEFT01 with SYSTSIN in a member and inline, DSNUTILB LOAD, DSNTIAUL
            jb = JclBuilder(f"{s3}DB2LD")
            jb.job(f"{s3}DB2LD", desc=f"{sys_} DB2 LOAD")
            jb.step("STEP010", pgm="IKJEFT01", parm=None)
            jb.dd("STEPLIB", "DB2P.SDSNLOAD", "SHR")
            jb.dd("SYSTSPRT", sysout="*")
            jb.dd("SYSTSIN", f"{h}.PARMLIB", "SHR", member=f"RUN{s3}")
            jb.dd(f"{s3}TRAN", f"{h}.TRANS", "SHR")
            jb.dd(f"{s3}HIST", f"{h}.HISTORY", "MOD")
            jb.step("STEP020", pgm="IKJEFT01", cond="(4,LT)")
            jb.dd("STEPLIB", "DB2P.SDSNLOAD", "SHR")
            jb.dd("SYSTSPRT", sysout="*")
            jb.dd_inline("SYSTSIN", ["  DSN SYSTEM(DB2P)", f"  RUN PROGRAM({s3}DB202) PLAN({s3}DB202) -", f"      LIB('{h}.LOADLIB')", "  END"])
            jb.dd(f"{s3}UNLD", f"{h}.UNLOAD", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(20,5)),DCB=(RECFM=FB,LRECL=200)")
            jb.dd(f"{s3}SRTD", "PROD.POL.EXTRACT.SORTED", "SHR")
            jb.step("STEP030", pgm="DSNUTILB", parm=f"'DB2P,LOAD{s3}'")
            jb.dd("STEPLIB", "DB2P.SDSNLOAD", "SHR")
            jb.dd("SYSREC", f"{h}.UNLOAD", "SHR")
            jb.dd_inline("SYSIN", ["  LOAD DATA INDDN SYSREC LOG NO RESUME YES", f"    INTO TABLE {main_tbl}"])
            jb.dd("SYSPRINT", sysout="*")
            jb.step("STEP040", pgm="IKJEFT01")
            jb.dd("STEPLIB", "DB2P.SDSNLOAD", "SHR")
            jb.dd("SYSTSPRT", sysout="*")
            jb.dd_inline("SYSTSIN", ["  DSN SYSTEM(DB2P)", "  RUN PROGRAM(DSNTIAUL) PLAN(DSNTIAUL) PARMS('SQL')", "  END"])
            jb.dd("SYSIN", f"{h}.PARMLIB", "SHR", member=f"{s3}TIAUL")
            jb.dd("SYSREC00", f"{h}.TIAUL.OUT", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(20,5))")
            jb.dd("SYSPUNCH", sysout="*")
            jb.end()
            self._add_job(sys_, jb)
            # IMSBT: DFSRRC00 DLI and BMP, DLIBATCH system PROC (synthesised)
            jb = JclBuilder(f"{s3}IMSBT")
            jb.job(f"{s3}IMSBT", desc=f"{sys_} IMS BATCH")
            jb.step("STEP010", pgm="DFSRRC00", parm=f"(DLI,{s3}IMS01,{s3}IMS01,,,,,,,,,,,N)", region="4M")
            jb.dd("STEPLIB", "IMS.PROD.RESLIB", "SHR")
            jb.dd_concat(f"{h}.LOADLIB")
            jb.dd("IMS", "IMS.PROD.PSBLIB", "SHR")
            jb.dd_concat("IMS.PROD.DBDLIB")
            jb.dd(f"{s3}DBD1", f"{h}.IMS.{s3}DBD", "SHR")
            jb.dd(f"{s3}CHKP", f"{h}.CHKPT.FILE", "SHR")
            jb.dd("IEFRDER", "&&LOG", "(NEW,PASS)", extra="UNIT=SYSDA,SPACE=(CYL,(5,5))")
            jb.dd("SYSOUT", sysout="*")
            jb.step("STEP020", pgm="DFSRRC00", parm=f"(BMP,{s3}IMS02,{s3}IMS02)", cond="(4,LT)")
            jb.dd("STEPLIB", "IMS.PROD.RESLIB", "SHR")
            jb.dd_concat(f"{h}.LOADLIB")
            if sys_ == "POLICY":
                jb.dd("POLGSOUT", f"{h}.GSAM.OUT(+1)", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(10,5)),DCB=(RECFM=FB,LRECL=200)")
            jb.dd(f"{s3}RUNP", f"{h}.RUN.PARMS", "SHR")
            jb.dd("SYSOUT", sysout="*")
            jb.step("STEP030", proc="DLIBATCH", bare_proc=False, MBR=f"{s3}IMS01", PSB=f"{s3}IMS01")
            jb.end()
            self._add_job(sys_, jb)
            # PURGE: IEFBR14, IDCAMS DEFINE/DELETE, IF/THEN/ELSE, JOB-card cond
            jb = JclBuilder(f"{s3}PURGE")
            jb.job(f"{s3}PURGE", desc=f"{sys_} PURGE", cond="(12,LT)")
            jb.step("DEL010", pgm="IEFBR14")
            jb.dd("DEL1", f"{h}.WORK.PURGE", "(MOD,DELETE,DELETE)", extra="UNIT=SYSDA,SPACE=(TRK,(1,1))")
            jb.step("DEF020", pgm="IDCAMS")
            jb.dd("SYSPRINT", sysout="*")
            jb.dd_inline("SYSIN", [f"  DELETE {h}.MASTER.NEW CLUSTER PURGE",
                                   f"  DEFINE CLUSTER (NAME({h}.MASTER.NEW) -",
                                   f"         KEYS({klen} 0) RECORDSIZE(300 300) -",
                                   "         INDEXED SHAREOPTIONS(2 3))",
                                   f"  DEFINE GDG (NAME({h}.EXTRACT.ARCH) LIMIT(30) SCRATCH)"])
            jb.if_("DEF020.RC > 4")
            jb.step("ERR030", pgm=f"{s3}HIS01")
            jb.dd("STEPLIB", f"{h}.LOADLIB", "SHR")
            jb.dd(f"{s3}HIST", f"{h}.HISTORY", "SHR")
            jb.dd(f"{s3}RPT", f"{h}.PURGE.REPORT", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(TRK,(5,5))")
            jb.dd("SYSOUT", sysout="*")
            jb.else_()
            jb.step("OK040", pgm="IEFBR14")
            jb.dd("ALLOC", f"{h}.PURGE.OK", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(TRK,(1,1))")
            jb.endif()
            jb.end()
            self._add_job(sys_, jb)
            # WEEK: instream PROC, referback, &&TEMP, GDG (-1), EXEC of the shared sort PROC, quoted PARM continued
            jb = JclBuilder(f"{s3}WEEK")
            jb.job(f"{s3}WEEK", desc=f"{sys_} WEEKLY")
            jb.jcllib_order(["PROD.CMN.PROCLIB"])
            pb = jb.instream_proc_begin(f"{s3}WKLY", HLQ=h)
            st = jb.step("WK010", pgm=f"{s3}HIS01")
            jb.dd("STEPLIB", "&HLQ..LOADLIB", "SHR")
            jb.dd(f"{s3}HIST", "&HLQ..HISTORY", "SHR")
            jb.dd(f"{s3}RPT", "&&WKRPT", "(NEW,PASS)", extra="UNIT=SYSDA,SPACE=(TRK,(5,5))")
            jb.dd("SYSOUT", sysout="*")
            jb.instream_proc_end()
            jb.step("WEEK1", proc=f"{s3}WKLY")
            jb.step("WEEK2", pgm="IEBGENER")
            jb.dd("SYSUT1", "*.WEEK1.WK010." + f"{s3}RPT", "(OLD,DELETE)")
            jb.dd("SYSUT2", f"{h}.WEEKLY.REPORT(+1)", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(TRK,(5,5))")
            jb.dd("SYSIN", dummy=True)
            jb.dd("SYSPRINT", sysout="*")
            jb.step("WEEK3", proc="CMNSORT", IN=f"{h}.EXTRACT(-1)", OUT=f"{h}.EXTRACT.PREV.SORTED", CARDS=f"{s3}SORT1")
            jb.step("WEEK4", pgm=f"{s3}DB202", parm="'MODE=WEEKLY,REGION=ALL,CYCLE=52,USER=BATCH,RESTART=NO,LIMIT=000000999'")
            jb.dd("STEPLIB", f"{h}.LOADLIB", "SHR")
            jb.dd(f"{s3}UNLD", f"{h}.UNLOAD.WEEKLY", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(20,5))")
            jb.dd(f"{s3}SRTD", f"{h}.EXTRACT.PREV.SORTED", "SHR")
            jb.dd("SYSOUT", sysout="*")
            jb.end()
            self._add_job(sys_, jb)
            # FTP: the extract leaves the mainframe
            jb = JclBuilder(f"{s3}FTP")
            jb.job(f"{s3}FTP", desc=f"{sys_} FTP")
            jb.step("FTP010", pgm="FTP", parm="'PEERHOST (EXIT'")
            jb.dd("SYSPRINT", sysout="*")
            jb.dd_inline("INPUT", ["put 'STG." + s3 + ".EXTRACT' extract.txt", "quit"])
            jb.end()
            self._add_job(sys_, jb)
            self.interfaces.append({"kind": "ftp", "detail": f"STG.{s3}.EXTRACT", "direction": "out", "job": f"{s3}FTP"})
            # RERUN: a program not in the index, JOBLIB, PGM=ZIP
            jb = JclBuilder(f"{s3}RERUN")
            jb.job(f"{s3}RERUN", desc=f"{sys_} RERUN")
            jb.joblib_dd([f"{h}.LOADLIB"])
            jb.step("RR010", pgm=f"{s3}ZIP01", parm="'RESTART'")
            jb.dd(f"{s3}CTL", f"{h}.CONTROL", "OLD")
            jb.dd("SYSOUT", sysout="*")
            jb.step("RR020", pgm=f"{s3}UPD01", cond="(0,NE,RR010)")
            jb.dd(f"{s3}MAST", f"{h}.MASTER.KSDS", "OLD")
            jb.dd(f"{s3}TRAN", f"{h}.TRANS.RERUN", "SHR")
            jb.dd(f"{s3}ERR", f"{h}.ERRORS.RERUN", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(TRK,(5,5))")
            jb.dd(f"{s3}CTL", f"{h}.CONTROL", "SHR")
            jb.dd("CUSTFILE", "PROD.CMN.CUSTOMER.KSDS", "SHR")
            jb.dd("SYSIN", dummy=True)
            jb.dd("SYSOUT", sysout="*")
            jb.end()
            self._add_job(sys_, jb)
        # a shared job that runs the ambiguous CMNUTIL and the CICS TDQ log copy
        jb = JclBuilder("CMNHOUSE")
        jb.job("CMNHOUSE", desc="SHARED HOUSEKEEPING")
        jb.step("HK010", pgm="CMNUTIL")
        jb.dd("STEPLIB", "PROD.CMN.LOADLIB", "SHR")
        jb.dd("CUSTFILE", "PROD.CMN.CUSTOMER.KSDS", "SHR")
        jb.dd("CUSTRPT", "PROD.CMN.CUSTOMER.REPORT", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(TRK,(5,5))")
        jb.dd("SYSOUT", sysout="*")
        jb.end()
        self._add_job("SHARED", jb)
        # the scale programs: one plain job each, a two-step chain (extract then copy)
        for sys_ in SYSTEMS:
            s3 = S3[sys_]
            h = HLQ[sys_]
            for k in range(1, self.scale + 1):
                jb = JclBuilder(f"{s3}GJ{k:02d}")
                jb.job(f"{s3}GJ{k:02d}", desc=f"{sys_} GENERIC {k}")
                jb.step("GS010", pgm=f"{s3}GEN{k:02d}", parm=f"'CYCLE={k:02d}'")
                jb.dd("STEPLIB", f"{h}.LOADLIB", "SHR")
                jb.dd(f"{s3}GIN", f"{h}.GEN{k:02d}.INPUT", "SHR")
                jb.dd(f"{s3}GOUT", f"{h}.GEN{k:02d}.OUTPUT(+1)", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(5,5))")
                jb.dd("SYSOUT", sysout="*")
                jb.step("GS020", pgm="IEBGENER", cond="(4,LT)")
                jb.dd("SYSUT1", f"{h}.GEN{k:02d}.OUTPUT(+1)", "SHR")
                jb.dd("SYSUT2", f"STG.{s3}.GEN{k:02d}.COPY", "(NEW,CATLG,DELETE)", extra="UNIT=SYSDA,SPACE=(CYL,(5,5))")
                jb.dd("SYSIN", dummy=True)
                jb.dd("SYSPRINT", sysout="*")
                jb.end()
                self._add_job(sys_, jb)
        # effective steps: the generator's own expansion
        includes: Dict[str, List] = {}
        for name, j in self.jobs.items():
            eff = expand(j["builder"], self.procs, self.opens_of, self.cards, includes)
            j["steps"] = [self._eff_dict(e) for e in eff]

    def _eff_dict(self, e: EffStep) -> dict:
        return {"name": e.name, "pgm": e.pgm, "effective_pgm": e.effective_pgm, "launcher": e.launcher,
                "from_proc": e.from_proc, "parent_step": e.parent_step, "parm": e.parm, "cond": e.cond,
                "guard": e.guard, "line": e.line, "proc_called": e.proc_called, "note": e.note,
                "cards": [list(c) for c in e.cards], "tables": [list(t) for t in e.tables], "idcams": [list(i) for i in e.idcams],
                "dds": [{"name": d.name, "dsn": d.dsn, "dsn_resolved": d.dsn_resolved, "gdg_rel": d.gdg_rel, "disp": d.disp,
                         "mode": d.mode, "mode_source": d.mode_source, "line": d.line, "is_temp": d.is_temp,
                         "card_member": d.card_member, "inline": d.inline, "is_override": d.is_override,
                         "referback": d.referback, "dummy": d.dummy} for d in e.dds]}

    def _add_proc(self, sys_: str, pb: JclBuilder) -> None:
        self.procs[pb.proc_name] = pb
        path = self.path(sys_, "proc", pb.name)
        self.add_file(path, pb.records(), pb.name, "proc", sys_, lib(sys_, "proc"))
        self.proc_truth[pb.proc_name] = {"path": path, "system": sys_, "symbolics": dict(pb.symbolics),
                                         "steps": [{"name": s.name, "pgm": s.pgm, "line": s.line,
                                                    "dds": [{"name": d.name, "dsn": d.dsn, "line": d.line} for d in s.dds]} for s in pb.steps]}

    def _add_job(self, sys_: str, jb: JclBuilder) -> None:
        path = self.path(sys_, "jcl", jb.name)
        self.add_file(path, jb.records(), jb.name, "jcl", sys_, lib(sys_, "jcl"))
        self.jobs[jb.job_name] = {"path": path, "system": sys_, "builder": jb, "jcllib": list(jb.jcllib),
                                  "joblib": list(jb.joblib), "cond": jb.job_cond, "includes": list(jb.includes),
                                  "instream_procs": list(jb.instream_procs)}

    # ------------------------------------------------------------ programs
    def build_programs(self) -> None:
        import synth_programs as sp
        sp.build_all(self)

    def add_program(self, pb: ProgramBuilder, status: str = "ok", why: str = "", role: str = "", kind: str = "src",
                    system: Optional[str] = None) -> None:
        sys_ = system or pb.system
        path = self.path(sys_, kind, pb.name)
        recs = pb.records()
        self.add_file(path, recs, pb.name, "cobol", sys_, lib(sys_, kind), status, why, role)
        t = pb.truth()
        t["path"] = path
        t["status"] = status
        t["why"] = why
        t["role"] = role
        self.programs.setdefault(pb.name, []).append(t)
        self.prog_builders[f"{sys_}/{pb.name}"] = pb
        opens: Dict[str, str] = {}
        for f in pb.facts:
            if f.kind == "io" and f.data["op"] == "OPEN":
                sel = f.data["target"]
                dd = next((s["dd"] for s in pb.selects if s["name"] == sel), None)
                if dd:
                    mode = {"INPUT": "input", "OUTPUT": "output", "EXTEND": "mod", "I-O": "both"}[f.data["mode"]]
                    if dd.upper() in opens and opens[dd.upper()] != mode:
                        mode = "both"
                    opens[dd.upper()] = mode
        self.opens_of[pb.name] = opens

    # ------------------------------------------------------------ listings
    def build_listings(self) -> None:
        """Enterprise COBOL listings for the programs recover must read (D2, D5), the compiler's own
        way: numbered source, a C on every copied line, page breaks with the banner and ruler, and
        the copybook-source table after the source."""
        from synth_listing import ibm_listing
        want = {"POLUPD03": [("POLMISSB", "SYSLIB", "PROD.POL.COPYLIB.ARCHIVE"), ("POLWORKA", "SYSLIB", "PROD.POL.COPYLIB"),
                             ("CMNERRA", "SYSLIB", "PROD.CMN.COPYLIB")],
                "POLUPD04": [("POLMISSB", "SYSLIB", "PROD.POL.COPYLIB.ARCHIVE"), ("POLWORKA", "SYSLIB", "PROD.POL.COPYLIB")],
                "POLRPT01": [("STDHDR", "SYSLIB", "PROD.POL.COPYLIB"), ("POLRPTL", "SYSLIB", "PROD.POL.COPYLIB")],
                "CLMRPT01": [("STDHDR", "SYSLIB", "PROD.POL.COPYLIB"), ("CLMRPTL", "SYSLIB", "PROD.CLM.COPYLIB")],
                "BILRPT01": None}
        os.makedirs(self.listings_dir, exist_ok=True)
        for pname, table in want.items():
            sys_ = next(s for s in SYSTEMS if pname.startswith(S3[s]))
            pb = self.prog_builders.get(f"{sys_}/{pname}")
            if pb is None:
                continue
            books = {}
            for c in pb.copies:
                cb = self.books.get(c["copybook"])
                if cb is not None and cb.text_lines:
                    books[c["copybook"]] = cb.text_lines
                    for nested in cb.nested:
                        nb = self.books.get(nested)
                        if nb is not None:
                            books[nested] = nb.text_lines
            if pname.startswith("CLMRPT01"):
                # the CLAIMS listing shows the POLICY header's text: the compiler read PROD.POL.COPYLIB
                books["STDHDR"] = next(b for b in self.book_copies["STDHDR"] if b.system == "POLICY").text_lines
            text = ibm_listing(pname, pb.records(), books, page_lines=50, copy_table=table)
            path = os.path.join(self.listings_dir, pname + ".lst")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            self.listings[pname] = {"path": path, "copy_table": table, "system": sys_}

    # ------------------------------------------------------------- defects
    def build_defects(self) -> None:
        self.defects = {
            "D1": {"title": "a copybook chosen among several same-named ones (LESSONS 181)", "copybook": "STDHDR",
                   "programs": [f"{S3[s]}RPT01" for s in SYSTEMS] + [f"{S3[s]}EXT01" for s in SYSTEMS] + ["CMNUTIL"],
                   "coverage_words": ["Complete, with a copybook chosen among several", "NOT parsed only in part"],
                   "program_words": ["parse: complete (copybook chosen among several - see notes)", "copies of STDHDR with different content; used"],
                   "not_words": []},
            "D2": {"title": "the listing's copybook-source table checked against the build's choice (LESSONS 182)",
                   "confirmed": ["POLRPT01"], "contradicted": ["CLMRPT01"], "copybook": "STDHDR",
                   "recover_words": ["copybook choices checked against the listings:"],
                   "program_words": {"POLRPT01": "listing says: STDHDR came from PROD.POL.COPYLIB (SYSLIB) - confirms the copy the build used",
                                     "CLMRPT01": "listing says: STDHDR came from PROD.POL.COPYLIB (SYSLIB) - CONTRADICTS the copy the build used"},
                   "coverage_words": ["of these choices is confirmed by the program's listing", "contradicted"]},
            "D3": {"title": "a procedure copybook filed proc by its folder (LESSONS 183)", "copybook": "POLPROCB", "program": "POLUPD02",
                   "recover_words": ["copybook name(s) exist in the index only as a member of a kind the build does not expand (proc)"],
                   "coverage_words": ["filed as proc (folder PROD.POL.PROCS)", "rename the folder to end in COPYLIB"],
                   "copybook_words": ["**NOT FOUND** as a copybook - a member with this name exists, filed as proc (folder PROD.POL.PROCS)"],
                   "program_words": ["NOT FOUND"], "final_status": "partial"},
            "D4": {"title": "copybooks the classifier typed asm by a line of their text, re-filed by recover (LESSONS 186-189)",
                   "copybooks": ["CMNDATEA", "CMNCUSTP"], "words_before": ["filed as asm by its content"],
                   "recover_words": ["misfiled copybook(s) re-filed as copybook in the index (the classifier had read them as asm by a line of their text)"],
                   "report_words": ["## Re-filed as copybook", "START-DATE", "START CUSTFILE"],
                   "copybook_words": ["Indexed as a copybook (re-filed by atlas.recover; its own layout rows arrive with the next full re-parse)"],
                   "programs_whole_after": ["POLUPD01", "CLMUPD01", "BILUPD01", "CMNCUST1"]},
            "D5": {"title": "a missing copybook recovered from the listings, one COPY with a REPLACING over two lines (LESSONS 190)",
                   "copybook": "POLMISSB", "programs": ["POLUPD03", "POLUPD04"],
                   "recover_words": ["recovered: 1 of", "## Written", "POLMISSB"], "final_status": "ok",
                   "recovered_path": os.path.join(self.root, "SHARED", "RECOVERED-COPYBOOKS", "POLMISSB.cpy")},
            "D6": {"title": "a stub copybook whose text sits in columns 1-7, filed empty (LESSONS 192)", "copybook": "POLSTUBB",
                   "program": "POLUPD05", "recover_words": ["## Missing copybooks, checked on disk", "columns 1-7"],
                   "coverage_words": ["on disk?"], "copybook_words": ["columns 1-7"], "final_status": "partial"},
            "ARRIVED": {"title": "a copybook that arrived after its program was parsed (LESSONS 183/192)", "copybook": "POLARRVB",
                        "program": "POLUPD06", "recover_words": ["arrived after the last build"],
                        "recover_words_after_build": ["copy a copybook that has arrived since they were parsed"], "final_status": "ok"},
        }

    # ------------------------------------------------------------- output
    def write(self) -> None:
        for path, recs in self.files.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(recs) + "\n")
        manifest = {"authoritative": [f"PROD.{S3[s]}.SRC" for s in SYSTEMS] + [f"PROD.{S3[s]}.COPYLIB" for s in SYSTEMS]
                    + ["PROD.CMN.COPYLIB", "PROD.CMN.SRC"],
                    "external_interfaces": [{"kind": "ndm", "peer": "REINSURER-X", "direction": "out", "dataset": "STG.POL.EXTRACT",
                                             "note": "nightly bordereau"},
                                            {"kind": "ddf", "peer": "CLAIMS-WEB", "direction": "in", "table": "PRD.CLAIM_TBL"}]}
        with open(os.path.join(self.out, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1)
        self.manifest = manifest

    def truth(self) -> dict:
        datasets: Dict[str, dict] = {}
        for jname, j in self.jobs.items():
            for st in j["steps"]:
                for d in st["dds"]:
                    dsn = d["dsn_resolved"]
                    if not dsn or d["is_temp"] or dsn.startswith("&"):
                        continue
                    if st["proc_called"] and not st["from_proc"]:
                        continue                              # the EXEC PROC step itself carries overrides only
                    ent = datasets.setdefault(dsn, {"writers": [], "readers": [], "other": [], "gdg": False, "systems_w": [], "systems_r": []})
                    row = {"job": jname, "step": st["name"], "pgm": st["effective_pgm"], "mode": d["mode"],
                           "source": d["mode_source"], "system": j["system"], "line": d["line"], "dd": d["name"]}
                    if d["gdg_rel"]:
                        ent["gdg"] = True
                    if d["mode"] in ("output", "mod", "both"):
                        ent["writers"].append(row)
                    elif d["mode"] == "input":
                        ent["readers"].append(row)
                    else:
                        ent["other"].append(row)
                for op, dsn, mode in st["idcams"]:
                    if op.startswith("DEFINE") or op == "DELETE" or op == "REPRO":
                        ent = datasets.setdefault(dsn, {"writers": [], "readers": [], "other": [], "gdg": False, "systems_w": [], "systems_r": []})
                        ent["other"].append({"job": jname, "step": st["name"], "pgm": "*IDCAMS*", "mode": mode, "source": "idcams",
                                             "system": j["system"], "line": st["line"], "dd": op})
        for dsn, ent in datasets.items():
            ent["systems_w"] = sorted({r["system"] for r in ent["writers"]})
            ent["systems_r"] = sorted({r["system"] for r in ent["readers"]})
        jobs = {n: {k: v for k, v in j.items() if k != "builder"} for n, j in self.jobs.items()}
        programs = {}
        for name, copies in self.programs.items():
            for t in copies:
                key = name if len(copies) == 1 else f"{t['system']}/{name}"
                jobs_running = []
                for jname, j in self.jobs.items():
                    for st in j["steps"]:
                        if st["effective_pgm"] == name:
                            jobs_running.append({"job": jname, "step": st["name"], "from_proc": st["from_proc"], "launcher": st["launcher"]})
                t["jobs"] = jobs_running
                t["transactions"] = [x["code"] for x in self.transactions if x.get("program") == name]
                programs[key] = t
        # DB2 CRUD per program from the SQL facts
        for key, t in programs.items():
            crud: Dict[str, set] = {}
            for f in t["facts"]:
                if f["fk"] == "sql":
                    for tb in f["tables"]:
                        letters = crud.setdefault(tb, set())
                        st = f["stmt_type"]
                        if st == "INSERT":
                            letters.add("C")
                        elif st in ("SELECT", "DECLARE", "FETCH", "OPEN"):
                            letters.add("R")
                        elif st in ("UPDATE", "MERGE"):
                            letters.add("U")
                        elif st == "DELETE":
                            letters.add("D")
            for tb, letters in crud.items():
                q = tb if "." in tb else f"PRD.{tb}"
                if q in self.db2:
                    self.db2[q]["programs"][t["name"]] = "".join(c for c in "CRUD" if c in letters)
            t["crud_db2"] = {tb: "".join(c for c in "CRUD" if c in letters) for tb, letters in crud.items()}
        universe = {
            "programs": sorted(self.programs), "copybooks": sorted(set(self.book_copies) | {"POLMISSB", "POLARRVB"}),
            "jobs": sorted(self.jobs), "procs": sorted(self.procs), "datasets": sorted(datasets),
            "tables": sorted(self.db2), "dbds": sorted(self.ims["dbds"]), "psbs": sorted(self.ims["psbs"]),
            "transactions": sorted(x["code"] for x in self.transactions), "screens": sorted(self.screens),
            "fields": sorted({r[1] for ct in self.copy_truth.values() for r in ct["fields"] if r[1] and r[1] != "FILLER"}
                             | {n for pl in self.programs.values() for t in pl for n in t["declared"]}),
            "cards": sorted(self.cards), "segments": sorted(s["name"] for d in self.ims["dbds"].values() for s in d["segments"]),
            "paragraphs": sorted({p["name"] for pl in self.programs.values() for t in pl for p in t["paragraphs"]}),
        }
        return {"seed": self.seed, "root": self.root, "listings_dir": self.listings_dir, "manifest": os.path.join(self.out, "manifest.json"),
                "members": self.members, "copybooks": self.copy_truth, "programs": programs, "jobs": jobs,
                "procs": self.proc_truth, "cards": self.cards, "datasets": datasets, "db2": self.db2, "ims": self.ims,
                "transactions": self.transactions, "screens": self.screens, "interfaces": self.interfaces,
                "cics_files": self.cics_files, "defects": self.defects, "listings": self.listings, "docs": self.docs,
                "arrived": self.arrived, "incremental": self.incremental, "universe": universe, "notes": self.notes,
                "record_meta": {k: {"key": m.key, "status": m.status} for k, m in self.record_meta.items()}}


def generate(out_dir: str, seed: int = 20260925, scale: int = 12) -> Estate:
    e = Estate(out_dir, seed, scale)
    e.build_copybooks()
    e.build_ims()
    e.build_cics()
    e.build_docs()
    e.build_programs()
    e.build_jcl()
    e.build_listings()
    e.build_defects()
    e.write()
    return e
