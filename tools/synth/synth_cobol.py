"""
synth_cobol.py - a COBOL program written statement by statement, recording
the ground truth of every fact the toolkit is expected to extract from it
(calls, COPY statements, files, SQL, DL/I, CICS, paragraphs, PERFORM edges,
field references, literals) with the member line each fact sits on.

The builder never reads atlas/: the truth is what the generator MEANT, at
the line the generator WROTE it, so a disagreement is a finding and not a
copy of the toolkit's own reading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from synth_layout import Emitter, Item, Replacing, apply_replacing, flatten, layout, render_items, truth_rows


def split_words(text: str) -> List[str]:
    """Words of a statement, a quoted literal kept whole (never broken at a blank inside it)."""
    return re.findall(r"'(?:[^']|'')*'|\"[^\"]*\"|\S+", text)


@dataclass
class CopybookDef:
    """What the estate knows about a copybook: its name, the trees it holds,
    and the level at which its first entry starts (1 = a full 01; 5 = a
    fragment the program wraps in its own 01)."""
    name: str
    roots: List[Item]                      # laid out (offset/length set)
    kind: str = "data"                     # data | procedure | dclgen | sqlca | missing | stub | symbolic_map
    nested: List[str] = dc_field(default_factory=list)   # copybooks it copies itself
    paragraphs: List[str] = dc_field(default_factory=list)   # procedure copybook: its paragraph names
    text_lines: List[str] = dc_field(default_factory=list)   # rendered records (set by the estate)
    system: str = ""
    library: str = ""
    path: str = ""

    def names(self) -> List[str]:
        out: List[str] = []
        for r in self.roots:
            for it in flatten(r):
                if it.name and it.name != "FILLER":
                    out.append(it.name)
                for c, _v in it.conds:
                    out.append(c)
        return out


@dataclass
class Fact:
    kind: str
    line: int
    data: dict


class ProgramBuilder:
    """Writes one program. Every helper emits the statement and records what
    the toolkit must find on that line."""

    AREA_A = 8
    AREA_B = 12

    def __init__(self, name: str, system: str, library: str, books: Dict[str, CopybookDef],
                 seq: bool = False, tag: str = "", osvs: bool = False) -> None:
        self.name = name
        self.system = system
        self.library = library
        self.books = books
        self.seq = seq
        self.tag = tag
        self.osvs = osvs
        self.em = Emitter()
        self.facts: List[Fact] = []
        self.declared: Set[str] = set()          # data names visible to the PROCEDURE DIVISION
        self.own_roots: List[Item] = []          # the program's own 01/77 trees (rendered)
        self.copies: List[dict] = []
        self.paragraphs: List[dict] = []
        self.sections: List[dict] = []
        self.current_para: Optional[str] = None
        self.current_section: Optional[str] = None
        self.uses = {"sql": False, "cics": False, "dli": False, "mq": False}
        self.linkage_using: List[str] = []
        self.aliases: List[dict] = []            # ENTRY points
        self.field_aliases: List[Tuple[str, str, str]] = []   # (copybook, orig, new)
        self.selects: List[dict] = []
        self.fd_records: Dict[str, str] = {}     # select name -> 01 record name
        self.division = ""
        self.in_section_name = ""
        self.cur_indent = self.AREA_B
        self.notes: List[str] = []

    # ------------------------------------------------------------------ emit
    def line(self, col: int, text: str) -> int:
        return self.em.at(col, text)

    def comment(self, text: str = "") -> int:
        return self.em.comment(text)

    def blank(self) -> int:
        return self.em.blank()

    def wrap(self, col: int, text: str, cont_col: Optional[int] = None) -> int:
        """A statement broken at blanks (never inside a literal) so every
        record ends by column 72; returns the line of its first record."""
        cont_col = cont_col or min(col + 4, 40)
        words = split_words(text)
        first = None
        cur = ""
        c = col
        for w in words:
            cand = w if not cur else cur + " " + w
            if c - 1 + len(cand) > 72 and cur:
                n = self.em.at(c, cur)
                first = first if first is not None else n
                cur = w
                c = cont_col
            else:
                cur = cand
        if cur:
            n = self.em.at(c, cur)
            first = first if first is not None else n
        return first if first is not None else self.em.n

    def fact(self, fkind: str, line: int, **data) -> None:
        self.facts.append(Fact(fkind, line, data))

    # --------------------------------------------------------- identification
    def identification(self, author: str = "SYNTH ESTATE", remarks: Optional[str] = None,
                       pid_own_line: bool = False, date_written: str = "2019-04-01") -> None:
        self.line(self.AREA_A, "IDENTIFICATION DIVISION.")
        if pid_own_line:
            self.line(self.AREA_A, "PROGRAM-ID.")
            self.line(self.AREA_B, f"{self.name}.")
        else:
            self.line(self.AREA_A, f"PROGRAM-ID.    {self.name}.")
        self.line(self.AREA_A, f"AUTHOR.        {author}.")
        self.line(self.AREA_A, f"DATE-WRITTEN.  {date_written}.")
        if remarks:
            self.line(self.AREA_A, "REMARKS.")
            for ln in remarks.split("\n"):
                self.wrap(self.AREA_B + 4, ln, cont_col=self.AREA_B + 4)
        self.division = "ID"

    def environment(self, selects: Sequence[dict]) -> None:
        """selects: [{name, dd, org: SEQUENTIAL|INDEXED, access, key, alt_keys, status}]"""
        self.line(self.AREA_A, "ENVIRONMENT DIVISION.")
        self.line(self.AREA_A, "CONFIGURATION SECTION.")
        self.line(self.AREA_A, "SOURCE-COMPUTER. IBM-370.")
        self.line(self.AREA_A, "OBJECT-COMPUTER. IBM-370.")
        if selects:
            self.line(self.AREA_A, "INPUT-OUTPUT SECTION.")
            self.line(self.AREA_A, "FILE-CONTROL.")
            for s in selects:
                n = self.line(self.AREA_B, f"SELECT {s['name']} ASSIGN TO {s['dd']}")
                org = s.get("org", "SEQUENTIAL")
                if org == "INDEXED":
                    self.line(self.AREA_B + 4, "ORGANIZATION IS INDEXED")
                    self.line(self.AREA_B + 4, f"ACCESS MODE  IS {s.get('access', 'DYNAMIC')}")
                    self.line(self.AREA_B + 4, f"RECORD KEY   IS {s['key']}")
                    for ak in s.get("alt_keys", []):
                        self.line(self.AREA_B + 4, f"ALTERNATE RECORD KEY IS {ak} WITH DUPLICATES")
                else:
                    self.line(self.AREA_B + 4, "ORGANIZATION IS SEQUENTIAL")
                if s.get("status"):
                    self.line(self.AREA_B + 4, f"FILE STATUS  IS {s['status']}.")
                else:
                    self.em.lines[-1] = self.em.lines[-1] + "."
                self.selects.append({"name": s["name"], "dd": s["dd"], "org": org, "key": s.get("key"),
                                     "line": n, "alt_keys": list(s.get("alt_keys", []))})
                self.fact("select", n, name=s["name"], dd=s["dd"], org=org, key=s.get("key"))
        self.division = "ENV"

    # ------------------------------------------------------------------ data
    def data_division(self) -> None:
        self.line(self.AREA_A, "DATA DIVISION.")
        self.division = "DATA"

    def file_section(self) -> None:
        self.line(self.AREA_A, "FILE SECTION.")
        self.in_section_name = "FILE"

    def fd(self, select_name: str, record_name: str, recording: Optional[str] = None,
           copybook: Optional[str] = None, root: Optional[Item] = None, quoted: bool = False,
           lrecl: Optional[int] = None) -> None:
        """FD with one 01 record: either the program's own tree or a COPY of a
        fragment / a full 01 inside it."""
        self.line(self.AREA_A, f"FD  {select_name}")
        if recording:
            self.line(self.AREA_B, f"RECORDING MODE IS {recording}")
        if lrecl:
            self.line(self.AREA_B, f"RECORD CONTAINS {lrecl} CHARACTERS")
        self.line(self.AREA_B, "LABEL RECORDS ARE STANDARD.")
        self.fd_records[select_name] = record_name
        for s in self.selects:
            if s["name"] == select_name:
                s["fd_record"] = record_name
        if root is not None:
            root.name = record_name
            self.item(root)
        else:
            n = self.line(self.AREA_A, f"01  {record_name}.")
            self.fact("root", n, name=record_name, section="FILE")
            self.declared.add(record_name)
            if copybook:
                self.copy(copybook, quoted=quoted, wrapped_by=record_name)

    def working_storage(self) -> None:
        self.line(self.AREA_A, "WORKING-STORAGE SECTION.")
        self.in_section_name = "WORKING-STORAGE"

    def linkage_section(self) -> None:
        self.line(self.AREA_A, "LINKAGE SECTION.")
        self.in_section_name = "LINKAGE"

    def item(self, root: Item, style: Optional[str] = None) -> Item:
        """Render one of the program's own 01/77 trees and declare its names."""
        layout(root)
        before = self.em.n
        render_items(self.em, root, style=style or ("osvs" if self.osvs else "std"))
        self.own_roots.append(root)
        for it in flatten(root):
            if it.name and it.name != "FILLER":
                self.declared.add(it.name)
            for c, _v in it.conds:
                self.declared.add(c)
        self.fact("root", before + 1, name=root.name, section=self.in_section_name, level=root.level)
        return root

    def wrapper_01(self, name: str) -> int:
        n = self.line(self.AREA_A, f"01  {name}.")
        self.declared.add(name)
        self.fact("root", n, name=name, section=self.in_section_name)
        return n

    def copy(self, book: str, quoted: bool = False, replacing: Optional[Sequence[Replacing]] = None,
             split_replacing: bool = False, of_lib: Optional[str] = None, sql_include: bool = False,
             osvs_01: Optional[str] = None, wrapped_by: Optional[str] = None, level_01_text: bool = False,
             prefix_text: Optional[str] = None, expect: str = "resolved") -> int:
        """Emit a COPY / EXEC SQL INCLUDE and declare what it brings in.

        expect: resolved | not_found | chosen | skipped - what the index will say of it.
        split_replacing: the REPLACING clause continued on the next record with the period of
        one pseudo-text on the first (LESSONS 190).
        osvs_01: `01 NAME COPY 'BOOK'.` - the copybook's own 01 renamed (LESSONS 177).
        prefix_text: text before COPY on the same line (`A-100-BEGIN SECTION.  COPY X.`, LESSONS 178)."""
        name_txt = f"'{book}'" if quoted else book
        rep_text = " ".join(r.text() for r in replacing) if replacing else ""
        col = self.AREA_B if not prefix_text else self.AREA_A
        if sql_include:
            n = self.line(self.AREA_B, "EXEC SQL")
            self.line(self.AREA_B + 4, f"INCLUDE {book}")
            self.line(self.AREA_B, "END-EXEC.")
        elif osvs_01:
            n = self.line(self.AREA_A, f"01  {osvs_01}   COPY  {name_txt}.")
            self.declared.add(osvs_01)
        elif prefix_text:
            n = self.line(self.AREA_A, f"{prefix_text}  COPY {name_txt}.")
        elif replacing and split_replacing:
            # `COPY BOOK REPLACING ==A.== BY ==B.==` on the first record - a period inside the pseudo-text -
            # and the next pair with the statement's own period on the next record (LESSONS 190)
            first = replacing[0]
            dot = "." if first.kind == "name_dot" else ""
            n = self.line(self.AREA_B, f"COPY {name_txt} REPLACING =={first.src}{dot}== BY")
            rest = f"=={first.dst}{dot}== " + " ".join(r.text() for r in replacing[1:])
            self.line(self.AREA_B + 4, rest.rstrip() + ".")
        else:
            txt = f"COPY {name_txt}"
            if of_lib:
                txt += f" OF {of_lib}"
            if rep_text:
                txt += f" REPLACING {rep_text}"
            n = self.wrap(col, txt + ".", cont_col=self.AREA_B + 4)
        cb = self.books.get(book)
        alias_rows: List[Tuple[str, str]] = []
        if cb is not None:
            if replacing:
                for r in cb.roots:
                    _new, al = apply_replacing(r, replacing)
                    alias_rows.extend(al)
                    for it in flatten(_new):
                        if it.name and it.name != "FILLER":
                            self.declared.add(it.name)
                        for c, _v in it.conds:
                            self.declared.add(c)
                self.field_aliases.extend((book, o, nw) for o, nw in alias_rows)
            else:
                self.declared.update(cb.names())
            if osvs_01 and cb.roots:
                # the library's 01 name is replaced by the program's: an alias, and the program's name declared
                self.field_aliases.append((book, cb.roots[0].name, osvs_01))
            for nested in cb.nested:
                nb = self.books.get(nested)
                if nb is not None:
                    self.declared.update(nb.names())
        self.copies.append({"copybook": book, "line": n, "replacing": rep_text or None, "quoted": quoted,
                            "of": of_lib, "sql_include": sql_include, "expect": expect, "osvs_01": osvs_01,
                            "wrapped_by": wrapped_by, "aliases": alias_rows,
                            "split": bool(replacing and split_replacing), "prefix": prefix_text})
        self.fact("copy", n, copybook=book, replacing=rep_text or None, expect=expect)
        return n

    # ------------------------------------------------------------- procedure
    def procedure(self, using: Sequence[str] = (), returning: Optional[str] = None) -> None:
        txt = "PROCEDURE DIVISION"
        if using:
            txt += " USING " + " ".join(using)
        if returning:
            txt += f" RETURNING {returning}"
        self.wrap(self.AREA_A, txt + ".", cont_col=self.AREA_B + 8)
        self.linkage_using = list(using)
        self.division = "PROC"

    def section(self, name: str) -> int:
        n = self.line(self.AREA_A, f"{name} SECTION.")
        self.current_section = name
        self.current_para = None
        self.sections.append({"name": name, "line": n})
        self.fact("section", n, name=name)
        return n

    def para(self, name: str) -> int:
        n = self.line(self.AREA_A, f"{name}.")
        self.current_para = name
        self.paragraphs.append({"name": name, "line": n, "section": self.current_section})
        self.fact("paragraph", n, name=name, section=self.current_section)
        return n

    def stmt(self, text: str, indent: int = 0) -> int:
        col = self.AREA_B + indent
        return self.wrap(col, text, cont_col=col + 4)

    FIGURATIVE = {"SPACE", "SPACES", "ZERO", "ZEROS", "ZEROES", "LOW-VALUE", "LOW-VALUES", "HIGH-VALUE", "HIGH-VALUES",
                  "TRUE", "FALSE", "OTHER", "NULL", "NULLS", "QUOTE", "QUOTES", "ALL"}

    def _refs(self, line: int, names: Sequence[str], mode: str, stmt: str) -> None:
        for nm in names:
            base = nm.split("(")[0].strip()
            base = base.split(" OF ")[0].strip()
            if base and base[0].isalpha() and base.upper() not in self.FIGURATIVE:
                self.fact("ref", line, name=base, mode=mode, stmt=stmt)

    # statements -----------------------------------------------------------
    def perform(self, to: str, thru: Optional[str] = None, until: Optional[str] = None,
                times: Optional[str] = None, varying: Optional[str] = None, indent: int = 0,
                tests: Sequence[str] = ()) -> int:
        txt = f"PERFORM {to}"
        if thru:
            txt += f" THRU {thru}"
        if varying:
            txt += f" VARYING {varying}"
        if until:
            txt += f" UNTIL {until}"
        if times:
            txt += f" {times} TIMES"
        n = self.stmt(txt, indent)
        self.fact("perform", n, frm=self.current_para or self.current_section, to=to, thru=thru,
                  kind="perform", inline=False)
        self._refs(n, tests, "test", "PERFORM")
        return n

    def perform_inline_open(self, until: str, indent: int = 0, tests: Sequence[str] = ()) -> int:
        n = self.stmt(f"PERFORM UNTIL {until}", indent)
        self._refs(n, tests, "test", "PERFORM")
        return n

    def goto(self, to: str, indent: int = 0) -> int:
        n = self.stmt(f"GO TO {to}", indent)
        self.fact("perform", n, frm=self.current_para or self.current_section, to=to, thru=None, kind="goto")
        return n

    def goto_depending(self, targets: Sequence[str], on: str, indent: int = 0) -> int:
        n = self.stmt(f"GO TO {' '.join(targets)} DEPENDING ON {on}", indent)
        for t in targets:
            self.fact("perform", n, frm=self.current_para or self.current_section, to=t, thru=None,
                      kind="goto_depending")
        self._refs(n, [on], "read", "GO TO")
        return n

    def call_static(self, target: str, using: Sequence[str] = (), returning: Optional[str] = None,
                    indent: int = 0, exists: bool = True, by: Optional[Sequence[str]] = None) -> int:
        txt = f"CALL '{target}'"
        if using:
            if by:
                parts = []
                for how, arg in zip(by, using):
                    parts.append((f"BY {how} " if how else "") + arg)
                txt += " USING " + " ".join(parts)
            else:
                txt += " USING " + " ".join(using)
        if returning:
            txt += f" RETURNING {returning}"
        n = self.stmt(txt, indent)
        self.fact("call", n, kind="static", target=target, using=list(using), returning=returning, exists=exists)
        self._refs(n, using, "write", "CALL-USING")
        if returning:
            self._refs(n, [returning], "write", "CALL")
        return n

    def call_dynamic(self, var: str, using: Sequence[str] = (), resolved: Sequence[str] = (),
                     resolution: str = "value_clause", indent: int = 0) -> int:
        txt = f"CALL {var}"
        if using:
            txt += " USING " + " ".join(using)
        n = self.stmt(txt, indent)
        self.fact("call", n, kind="dynamic", via_var=var, using=list(using), resolved=list(resolved),
                  resolution=resolution)
        self._refs(n, using, "write", "CALL-USING")
        return n

    def move_lit(self, lit: str, targets: Sequence[str], indent: int = 0, quote: bool = True) -> int:
        val = f"'{lit}'" if quote else lit
        n = self.stmt(f"MOVE {val} TO {' '.join(targets)}", indent)
        for t in targets:
            self.fact("literal", n, literal=lit, context="move_to", field=t.split("(")[0].split(" OF ")[0])
        self._refs(n, targets, "write", "MOVE")
        return n

    def move(self, src: str, targets: Sequence[str], indent: int = 0, corr: bool = False) -> int:
        n = self.stmt(f"MOVE {'CORRESPONDING ' if corr else ''}{src} TO {' '.join(targets)}", indent)
        self._refs(n, [src], "read", "MOVE")
        self._refs(n, targets, "write", "MOVE")
        self.fact("move", n, src=src, dst=list(targets), corr=corr)
        return n

    def set_true(self, cond: str, indent: int = 0) -> int:
        n = self.stmt(f"SET {cond} TO TRUE", indent)
        self._refs(n, [cond], "write", "SET")
        return n

    def add_giving(self, a: str, b: str, giving: str, indent: int = 0) -> int:
        n = self.stmt(f"ADD {a} {b} GIVING {giving}", indent)
        self._refs(n, [a, b], "read", "ADD")
        self._refs(n, [giving], "write", "ADD")
        return n

    def add_to(self, a: str, to: str, indent: int = 0) -> int:
        n = self.stmt(f"ADD {a} TO {to}", indent)
        self._refs(n, [a], "read", "ADD")
        self._refs(n, [to], "write", "ADD")
        return n

    def compute(self, target: str, expr: str, reads: Sequence[str], indent: int = 0) -> int:
        n = self.stmt(f"COMPUTE {target} = {expr}", indent)
        self._refs(n, [target], "write", "COMPUTE")
        self._refs(n, reads, "read", "COMPUTE")
        return n

    def if_open(self, cond: str, tests: Sequence[str], lits: Sequence[Tuple[str, str]] = (), indent: int = 0) -> int:
        """IF cond ; tests: fields tested; lits: (literal, field) compared."""
        n = self.stmt(f"IF {cond}", indent)
        self._refs(n, tests, "test", "IF")
        for lit, fld in lits:
            self.fact("literal", n, literal=lit, context="compare", field=fld)
        return n

    def else_(self, indent: int = 0) -> int:
        return self.stmt("ELSE", indent)

    def end_if(self, indent: int = 0) -> int:
        return self.stmt("END-IF", indent)

    def evaluate_open(self, subject: str, indent: int = 0) -> int:
        n = self.stmt(f"EVALUATE {subject}", indent)
        self._refs(n, [subject], "test", "EVALUATE")
        self._eval_subject = subject
        return n

    def when(self, value: str, indent: int = 4, lit: Optional[str] = None) -> int:
        n = self.stmt(f"WHEN {value}", indent)
        if lit is not None:
            # `WHEN X = 'lit'` under EVALUATE TRUE names its own field; `WHEN 'lit'` the subject's
            m = re.match(r"^\s*([A-Z0-9-]+)\s*=\s*", value)
            fld = m.group(1) if m else getattr(self, "_eval_subject", None)
            self.fact("literal", n, literal=lit, context="when", field=fld)
        return n

    def end_evaluate(self, indent: int = 0) -> int:
        return self.stmt("END-EVALUATE", indent)

    def display(self, parts: Sequence[str], indent: int = 0, fields: Sequence[str] = ()) -> int:
        n = self.stmt("DISPLAY " + " ".join(parts), indent)
        self._refs(n, fields, "display", "DISPLAY")
        for p in parts:
            if p.startswith("'") and len(p) > 8:
                self.fact("literal", n, literal=p.strip("'"), context="display", field=None)
        return n

    def open_(self, mode_files: Sequence[Tuple[str, Sequence[str]]], indent: int = 0) -> int:
        txt = "OPEN " + " ".join(f"{mode} {' '.join(files)}" for mode, files in mode_files)
        n = self.stmt(txt, indent)
        for mode, files in mode_files:
            for f in files:
                self.fact("io", n, target=f, op="OPEN", mode=mode)
        return n

    def close(self, files: Sequence[str], indent: int = 0) -> int:
        n = self.stmt("CLOSE " + " ".join(files), indent)
        for f in files:
            self.fact("io", n, target=f, op="CLOSE")
        return n

    def read(self, file: str, into: Optional[str] = None, at_end: Optional[str] = None, key: Optional[str] = None,
             invalid: Optional[str] = None, next_: bool = False, indent: int = 0) -> int:
        txt = f"READ {file}{' NEXT' if next_ else ''}"
        if into:
            txt += f" INTO {into}"
        if key:
            txt += f" KEY IS {key}"
        n = self.stmt(txt, indent)
        if at_end:
            self.stmt(f"AT END {at_end}", indent + 4)
        if invalid:
            self.stmt(f"INVALID KEY {invalid}", indent + 4)
        self.stmt("END-READ", indent)
        self.fact("io", n, target=file, op="READ")
        if into:
            self._refs(n, [into], "write", "READ")
        return n

    def start_(self, file: str, key: str, indent: int = 0) -> int:
        n = self.stmt(f"START {file} KEY IS NOT LESS THAN {key}", indent)
        self.fact("io", n, target=file, op="START")
        return n

    def write(self, record: str, file: str, frm: Optional[str] = None, after: Optional[str] = None,
              indent: int = 0, rewrite: bool = False) -> int:
        txt = f"{'REWRITE' if rewrite else 'WRITE'} {record}"
        if frm:
            txt += f" FROM {frm}"
        if after:
            txt += f" AFTER ADVANCING {after}"
        n = self.stmt(txt, indent)
        self.fact("io", n, target=file, op="REWRITE" if rewrite else "WRITE", record=record)
        if frm:
            self._refs(n, [frm], "read", "WRITE")
        return n

    def delete_rec(self, file: str, indent: int = 0) -> int:
        n = self.stmt(f"DELETE {file} RECORD", indent)
        self.fact("io", n, target=file, op="DELETE")
        return n

    def string_(self, parts: Sequence[str], into: str, indent: int = 0, fields: Sequence[str] = ()) -> int:
        n = self.stmt("STRING " + " ".join(parts) + f" INTO {into}", indent)
        self._refs(n, [into], "write", "STRING")
        self._refs(n, fields, "read", "STRING")
        return n

    def initialize(self, target: str, indent: int = 0) -> int:
        n = self.stmt(f"INITIALIZE {target}", indent)
        self._refs(n, [target], "write", "INITIALIZE")
        return n

    def inspect(self, target: str, indent: int = 0) -> int:
        n = self.stmt(f"INSPECT {target} REPLACING ALL ',' BY ' '", indent)
        self._refs(n, [target], "write", "INSPECT")
        return n

    def exec_sql(self, lines: Sequence[str], stmt_type: str, tables: Sequence[str], cols: Sequence[dict] = (),
                 cursor: Optional[str] = None, hosts: Sequence[str] = (), indent: int = 0) -> int:
        """cols: [{tbl, col, host, mode}]"""
        n = self.stmt("EXEC SQL", indent)
        for ln in lines:
            self.wrap(self.AREA_B + indent + 4, ln, cont_col=self.AREA_B + indent + 8)
        self.stmt("END-EXEC" + ("." if self.division != "PROC" else ""), indent)
        self.uses["sql"] = True
        self.fact("sql", n, stmt_type=stmt_type, tables=list(tables), cursor=cursor, cols=list(cols), hosts=list(hosts))
        for c in cols:
            if c.get("host"):
                self._refs(n, [c["host"]], "write" if c["mode"] == "read" else "read", "EXEC-SQL")
        return n

    def dli(self, func_field: str, func: str, pcb: str, io_area: Optional[str] = None,
            ssas: Sequence[str] = (), indent: int = 0, parmcount: Optional[str] = None) -> int:
        args = ([parmcount] if parmcount else []) + [func_field, pcb] + ([io_area] if io_area else []) + list(ssas)
        n = self.stmt("CALL 'CBLTDLI' USING " + " ".join(args), indent)
        self.uses["dli"] = True
        self.fact("dli", n, func=func, pcb_arg=pcb, io_area=io_area, ssas=list(ssas), parmcount=parmcount)
        if io_area:
            self._refs(n, [io_area], "write" if func in ("GU", "GN", "GHU", "GHN", "GNP") else "read", "CALL-USING")
        return n

    def cics(self, verb: str, body: str, resource_kind: Optional[str] = None, resource: Optional[str] = None,
             direction: Optional[str] = None, indent: int = 0, fields_in: Sequence[str] = (),
             fields_out: Sequence[str] = (), extra_facts: Sequence[dict] = ()) -> int:
        n = self.stmt(f"EXEC CICS {verb} {body}", indent)
        self.stmt("END-EXEC", indent)
        self.uses["cics"] = True
        if resource_kind:
            self.fact("cics", n, verb=verb, resource_kind=resource_kind, resource=resource, direction=direction)
        for f in extra_facts:
            self.fact("cics", n, **f)
        self._refs(n, fields_in, "write", "EXEC-CICS")
        self._refs(n, fields_out, "read", "EXEC-CICS")
        return n

    def entry(self, alias: str, using: Sequence[str]) -> int:
        n = self.stmt(f"ENTRY '{alias}' USING " + " ".join(using))
        self.aliases.append({"alias": alias, "using": list(using), "line": n})
        self.fact("entry", n, alias=alias, using=list(using))
        return n

    def exit_para(self, name: str) -> None:
        self.para(name)
        self.stmt("EXIT.")

    def end_stmt(self) -> None:
        """A period on the last statement."""
        self.em.lines[-1] = self.em.lines[-1].rstrip() + "."

    # ---------------------------------------------------------------- output
    def records(self) -> List[str]:
        """The member's records with sequence numbers / change tags applied."""
        out = []
        for k, ln in enumerate(self.em.lines, 1):
            rec = ln.ljust(72)
            if self.seq:
                rec = f"{k * 100:06d}" + rec[6:]
            if self.tag:
                rec = rec + (self.tag + f"{k:04d}")[:8]
            out.append(rec.rstrip() if not self.tag else rec)
        return out

    def truth(self) -> dict:
        facts = [{"fk": f.kind, "line": f.line, **f.data} for f in self.facts]
        return {
            "name": self.name, "system": self.system, "library": self.library,
            "uses": dict(self.uses), "linkage_using": list(self.linkage_using),
            "copies": self.copies, "paragraphs": self.paragraphs, "sections": self.sections,
            "selects": self.selects, "aliases": self.aliases, "field_aliases": self.field_aliases,
            "declared": sorted(self.declared), "facts": facts, "lines": self.em.n, "notes": self.notes,
            "own_roots": [truth_rows(r, "", member=self.name) for r in self.own_roots],
        }
