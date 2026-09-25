"""
synth_programs.py - the program templates: the shapes an insurance shop
writes (batch masters, extracts, reports, DB2, DL/I batch and BMP, IMS MPP,
CICS, subroutines, dynamic calls, ENTRY aliases, OS/VS COPY forms, a
SECTION then COPY on one line, a REPLACING over two lines) parameterised
per system, every fact recorded as it is written.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import synth_domain as dom
from synth_cobol import ProgramBuilder
from synth_layout import Item, Replacing

OPENS: Dict[str, Dict[str, str]] = {}


def S(estate, sys_: str) -> dict:
    from synth_estate import PFX, S3, HLQ
    p = PFX[sys_]
    key, klen = dom.KEY_FIELD[sys_]
    return {"s3": S3[sys_], "p": p, "key": key, "klen": klen, "h": HLQ[sys_], "amts": dom.AMOUNTS[sys_], "dates": dom.DATES[sys_],
            "status": dom.STATUS_SETS[sys_], "tables": estate.tables[sys_], "variant": ("POLICY", "CLAIMS", "BILLING").index(sys_)}


def new(estate, name: str, sys_: str, **kw) -> ProgramBuilder:
    from synth_estate import lib
    return ProgramBuilder(name, sys_, lib(sys_, "src"), estate.books, **kw)


def ws_constants(pb: ProgramBuilder, name: str, extra: Sequence[Item] = ()) -> None:
    pb.item(Item(1, "WS-CONSTANTS", children=[
        Item(5, "WS-PROGRAM-NAME", pic="X(08)", value=f"'{name}'"),
        Item(5, "WS-VERSION", pic="X(04)", value="'V2.1'"),
        *extra]))


# --------------------------------------------------------------------------
# batch master update
# --------------------------------------------------------------------------

def upd01(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    pm, pt, pw, pc = p["master"], p["trans"], p["work"], p["control"]
    pb = new(estate, f"{s3}UPD01", sys_, seq=(sys_ == "POLICY"), tag=(s3 + "U1") if sys_ == "CLAIMS" else "")
    pb.identification(author="PAT O'BRIEN" if sys_ == "POLICY" else "SYNTH ESTATE",
                      remarks=f"NIGHTLY {sys_} MASTER UPDATE FROM THE TRANSACTION FILE.\nA COPY OF EACH CHANGE GOES TO THE HISTORY FILE.")
    pb.environment([{"name": "MASTER-FILE", "dd": f"{s3}MAST", "org": "INDEXED", "access": "DYNAMIC", "key": f"{pm}-{key}", "status": "WS-MAST-STATUS"},
                    {"name": "TRAN-FILE", "dd": f"{s3}TRAN", "status": "WS-TRAN-STATUS"},
                    {"name": "ERROR-FILE", "dd": f"{s3}ERR"},
                    {"name": "CONTROL-FILE", "dd": f"{s3}CTL"},
                    {"name": "CUSTFILE", "dd": "CUSTFILE", "org": "INDEXED", "access": "DYNAMIC", "key": "CF-CUST-KEY"}])
    pb.data_division()
    pb.file_section()
    pb.fd("MASTER-FILE", f"{pm}-MASTER-RECORD", copybook=f"{s3}MASTR", quoted=(sys_ == "BILLING"))
    pb.fd("TRAN-FILE", f"{pt}-TRAN-RECORD", recording="F", root=None, copybook=None) if False else None
    pb.line(pb.AREA_A, "FD  TRAN-FILE")
    pb.line(pb.AREA_B, "RECORDING MODE IS F")
    pb.line(pb.AREA_B, "LABEL RECORDS ARE STANDARD.")
    pb.fd_records["TRAN-FILE"] = f"{pt}-TRAN-RECORD"
    for s in pb.selects:
        if s["name"] == "TRAN-FILE":
            s["fd_record"] = f"{pt}-TRAN-RECORD"
    pb.copy(f"{s3}TRANR")                                        # a full 01 under the FD
    pb.fd("ERROR-FILE", "ERROR-RECORD", root=Item(1, "ERROR-RECORD", children=[Item(5, "ERR-LINE", pic="X(133)")]))
    pb.fd("CONTROL-FILE", f"{pc}-CONTROL-RECORD", copybook=f"{s3}CTLR")
    pb.line(pb.AREA_A, "FD  CUSTFILE")
    pb.line(pb.AREA_B, "LABEL RECORDS ARE STANDARD.")
    pb.fd_records["CUSTFILE"] = "CUST-RECORD"
    for s in pb.selects:
        if s["name"] == "CUSTFILE":
            s["fd_record"] = "CUST-RECORD"
    pb.copy("CMNCUSTR")
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-MAST-STATUS", pic="X(02)"), Item(5, "WS-TRAN-STATUS", pic="X(02)"),
                               Item(5, "WS-RTN-NAME", pic="X(08)", value=f"'{s3}RATE1'"),
                               Item(5, "WS-CUST-KEY", pic="X(12)"), Item(5, "WS-CUST-NO", pic="X(10)"),
                               Item(5, "WS-CUST-FOUND-SW", pic="X(01)", value="'N'"),
                               Item(5, "WS-CHG-COUNT", pic="S9(05)", usage="COMP"),
                               Item(5, "WS-NEW-COUNT", pic="S9(05)", usage="COMP")])
    pb.copy(f"{s3}WORKA")
    pb.copy("CMNDATEA", expect="not_found_then_ok")               # D4: START-DATE inside
    pb.copy("CMNERRA")
    pb.copy(f"{s3}MSGT")
    pb.wrapper_01("WS-HISTORY-AREA")
    pb.copy(f"{s3}HISTR", replacing=[Replacing("leading", f"{p['history']}-", "WH-")])
    pb.wrapper_01("WS-EDIT-COMM")
    pb.copy(f"{s3}COMMA", replacing=[Replacing("leading", f"{p['comm']}-", "WE-")], of_lib=None)
    pb.item(Item(1, "WS-MSG-LINE", children=[Item(5, "WS-MSG-CODE", pic="X(04)"), Item(5, "FILLER", pic="X(01)", value="SPACE"),
                                             Item(5, "WS-MSG-TEXT", pic="X(45)")]))
    pb.item(Item(1, "WS-SUB-PROGRAM", pic="X(08)", value="'CMNDATE'"))
    pb.procedure()
    pb.para("0000-MAIN-CONTROL")
    pb.perform("1000-INIT", thru="1000-INIT-EXIT")
    pb.perform("2000-PROCESS-TRAN", thru="2000-PROCESS-EXIT", until=f"{pw}-EOF", tests=[f"{pw}-EOF"])
    pb.perform("3000-WRAP-UP")
    pb.stmt("STOP RUN.")
    pb.para("1000-INIT")
    pb.open_([("INPUT", ["TRAN-FILE", "CONTROL-FILE", "CUSTFILE"]), ("I-O", ["MASTER-FILE"]), ("OUTPUT", ["ERROR-FILE"])])
    pb.end_stmt()
    pb.read("CONTROL-FILE", at_end=f"MOVE 'N' TO {pw}-FIRST-SW")
    pb.end_stmt()
    pb.move_lit("N", [f"{pw}-EOF-SW", f"{pw}-ERROR-SW"])
    pb.initialize(f"{pw}-COUNTERS")
    pb.move(f"{pc}-RUN-DATE", ["START-DATE"])
    pb.call_static("CMNDATE", using=["CMN-DATE-AREA"])
    pb.if_open("NOT DATE-OK", tests=["DATE-OK"])
    pb.move_lit("E003", ["ERR-CODE"], indent=4)
    pb.perform("9000-ERROR", indent=4)
    pb.end_if()
    pb.perform("2100-READ-TRAN")
    pb.end_stmt()
    pb.exit_para("1000-INIT-EXIT")
    pb.para("2000-PROCESS-TRAN")
    pb.move(f"{pt}-{key}", [f"{pm}-{key}", "WE-" + key])
    pb.evaluate_open("TRUE")
    pb.when(f"{pt}-ADD", lit=None)
    pb.perform("2200-ADD-MASTER", indent=8)
    pb.when(f"{pt}-CHANGE")
    pb.perform("2300-CHANGE-MASTER", thru="2300-CHANGE-EXIT", indent=8)
    pb.when(f"{pt}-DELETE")
    pb.perform("2400-DELETE-MASTER", indent=8)
    pb.when("OTHER")
    pb.move_lit("E002", ["ERR-CODE"], indent=8)
    pb.perform("9000-ERROR", indent=8)
    pb.end_evaluate()
    pb.if_open(f"{pt}-SOURCE-CD = 'X'", tests=[f"{pt}-SOURCE-CD"], lits=[("X", f"{pt}-SOURCE-CD")])
    pb.add_to("1", f"{pw}-WRITE-CNT", indent=4)
    pb.end_if()
    pb.perform("2100-READ-TRAN")
    pb.end_stmt()
    pb.exit_para("2000-PROCESS-EXIT")
    pb.para("2100-READ-TRAN")
    pb.read("TRAN-FILE", at_end=f"SET {pw}-EOF TO TRUE")
    pb.end_stmt()
    pb.if_open(f"NOT {pw}-EOF", tests=[f"{pw}-EOF"])
    pb.add_to("1", f"{pw}-READ-CNT", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.para("2200-ADD-MASTER")
    pb.initialize(f"{pm}-MASTER-RECORD")
    pb.move(f"{pt}-{key}", [f"{pm}-{key}"])
    pb.move_lit("PN", [f"{pm}-STATUS"])
    pb.move(f"{pt}-TRAN-DT", [f"{pm}-{c['dates'][0]}"])
    pb.move(f"{pt}-TRAN-AMT", [f"{pm}-{c['amts'][0]}"])
    pb.move_lit("A", ["WE-FUNCTION"])
    pb.call_static(f"{s3}EDIT", using=["WS-EDIT-COMM", f"{pm}-MASTER-RECORD"])
    pb.if_open("WE-RETURN-CODE = '00'", tests=["WE-RETURN-CODE"], lits=[("00", "WE-RETURN-CODE")])
    pb.write(f"{pm}-MASTER-RECORD", "MASTER-FILE", indent=4)
    pb.stmt("INVALID KEY", 8)
    pb.move_lit("E006", ["ERR-CODE"], indent=12)
    pb.perform("9000-ERROR", indent=12)
    pb.stmt("END-WRITE", 4)
    pb.add_to("1", "WS-NEW-COUNT", indent=4)
    pb.else_()
    pb.move("WE-MESSAGE", ["ERR-TEXT"], indent=4)
    pb.perform("9000-ERROR", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.para("2300-CHANGE-MASTER")
    pb.read("MASTER-FILE", key=f"{pm}-{key}", invalid=f"MOVE 'E001' TO ERR-CODE")
    pb.fact("literal", pb.em.n - 1, literal="E001", context="move_to", field="ERR-CODE")
    pb.end_stmt()
    pb.if_open("ERR-CODE = 'E001'", tests=["ERR-CODE"], lits=[("E001", "ERR-CODE")])
    pb.perform("9000-ERROR", indent=4)
    pb.goto("2300-CHANGE-EXIT", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.move(f"{pm}-MASTER-RECORD", ["WH-BEFORE-IMAGE"])
    pb.move(f"{pm}-{c['amts'][0]}", ["WH-OLD-AMT"])
    pb.move(f"{pt}-TRAN-AMT", [f"{pm}-{c['amts'][0]}", "WH-NEW-AMT"])
    pb.move_lit("C", ["WE-FUNCTION"])
    pb.call_static(f"{s3}EDIT", using=["WS-EDIT-COMM", f"{pm}-MASTER-RECORD"])
    pb.if_open(f"{pm}-{c['status'][0][0]} OR {pm}-{c['status'][3][0]}", tests=[f"{pm}-{c['status'][0][0]}", f"{pm}-{c['status'][3][0]}"])
    if sys_ == "POLICY":
        pb.if_open("PM-LIFE", tests=["PM-LIFE"], indent=4)
        pb.move_lit("POLRATE2", ["WS-RTN-NAME"], indent=8)
        pb.end_if(indent=4)
    pb.call_dynamic("WS-RTN-NAME", using=[f"{pm}-MASTER-RECORD", f"{pw}-WORK-AMT"],
                    resolved=[f"{s3}RATE1"] + ([f"{s3}RATE2"] if sys_ == "POLICY" else []), indent=4)
    pb.end_if()
    pb.write(f"{pm}-MASTER-RECORD", "MASTER-FILE", rewrite=True)
    pb.stmt("INVALID KEY PERFORM 9000-ERROR", 4)
    pb.fact("perform", pb.em.n, frm="2300-CHANGE-MASTER", to="9000-ERROR", thru=None, kind="perform")
    pb.stmt("END-REWRITE", 0)
    pb.move(f"{pm}-MASTER-RECORD", ["WH-AFTER-IMAGE"])
    pb.move("CORRESPONDING " + f"{pw}-WORK-DATE", ["WH-HIST-KEY"]) if False else None
    pb.move_lit("U", ["WH-CHG-TYPE"])
    pb.perform("2500-WRITE-HISTORY")
    pb.add_to("1", "WS-CHG-COUNT")
    pb.end_stmt()
    pb.exit_para("2300-CHANGE-EXIT")
    pb.para("2400-DELETE-MASTER")
    pb.read("MASTER-FILE", key=f"{pm}-{key}", invalid="MOVE 'E001' TO ERR-CODE")
    pb.fact("literal", pb.em.n - 1, literal="E001", context="move_to", field="ERR-CODE")
    pb.end_stmt()
    pb.move_lit("CN", [f"{pm}-STATUS"])
    pb.write(f"{pm}-MASTER-RECORD", "MASTER-FILE", rewrite=True)
    pb.end_stmt()
    pb.move_lit("D", ["WH-CHG-TYPE"])
    pb.perform("2500-WRITE-HISTORY")
    pb.end_stmt()
    pb.para("2500-WRITE-HISTORY")
    pb.move(f"{pm}-{key}", [f"WH-{key}"])
    pb.move(f"{pt}-USER-ID", ["WH-CHG-USER"])
    pb.move(f"{pc}-RUN-DATE", ["WH-CHG-DT"])
    pb.move("WS-CHG-COUNT", ["WH-SEQ-NO"])
    pb.move("WS-CUST-NO", ["CF-CUST-NO"])
    pb.call_static("CMNCUST1", using=["CUST-RECORD", "WS-CUST-FOUND-SW"])
    pb.display(["'HISTORY WRITTEN FOR '", f"WH-{key}"], fields=[f"WH-{key}"])
    pb.end_stmt()
    pb.para("3000-WRAP-UP")
    pb.display(["'TRANSACTIONS READ:   '", f"{pw}-READ-CNT"], fields=[f"{pw}-READ-CNT"])
    pb.display(["'MASTERS CHANGED:     '", "WS-CHG-COUNT"], fields=["WS-CHG-COUNT"])
    pb.display(["'ERRORS:              '", f"{pw}-ERROR-CNT"], fields=[f"{pw}-ERROR-CNT"])
    pb.add_giving("WS-CHG-COUNT", "WS-NEW-COUNT", f"{pw}-WRITE-CNT")
    pb.close(["TRAN-FILE", "MASTER-FILE", "ERROR-FILE", "CONTROL-FILE", "CUSTFILE"])
    pb.if_open(f"{pw}-ERROR-FOUND", tests=[f"{pw}-ERROR-FOUND"])
    pb.move_lit("U100", ["ABEND-CODE"], indent=4) if "ABEND-CODE" in pb.declared else pb.move_lit("08", [f"{pw}-RETURN-CD"], indent=4)
    pb.call_static("CMNABEND", using=["ERR-CODE"], indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.para("9000-ERROR")
    pb.add_to("1", f"{pw}-ERROR-CNT")
    pb.set_true(f"{pw}-ERROR-FOUND")
    pb.move("WS-PROGRAM-NAME", ["ERR-PROGRAM"])
    pb.perform("9100-FIND-MESSAGE", varying=f"{pw}-SUB FROM 1 BY 1", until=f"{pw}-SUB > 8 OR {pw}-MSG-CODE ({pw}-SUB) = ERR-CODE",
               tests=[f"{pw}-SUB", f"{pw}-MSG-CODE", "ERR-CODE"])
    pb.string_(["ERR-CODE DELIMITED BY SIZE", "' '", "ERR-TEXT DELIMITED BY SIZE"], "ERR-LINE", fields=["ERR-CODE", "ERR-TEXT"])
    pb.write("ERROR-RECORD", "ERROR-FILE")
    pb.call_static("CMNERR", using=["CMN-ERROR-AREA"])
    pb.end_stmt()
    pb.para("9100-FIND-MESSAGE")
    pb.if_open(f"{pw}-MSG-CODE ({pw}-SUB) = ERR-CODE", tests=[f"{pw}-MSG-CODE", "ERR-CODE"])
    pb.move(f"{pw}-MSG-TEXT ({pw}-SUB)", ["ERR-TEXT"], indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.para("9900-NEVER-REACHED")
    pb.display(["'THIS PARAGRAPH IS NEVER PERFORMED'"])
    pb.end_stmt()
    return pb


# --------------------------------------------------------------------------
# the D3 / D5 / D6 / arrived programs (POLICY only) and a plain twin
# --------------------------------------------------------------------------

def small_batch(estate, sys_: str, name: str, copies: Sequence[dict], extra_ws: Sequence[Item] = (),
                body=None, pid_own_line: bool = False) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p = c["s3"], c["p"]
    pw, pt = p["work"], p["trans"]
    pb = new(estate, name, sys_)
    pb.identification(pid_own_line=pid_own_line)
    pb.environment([{"name": "TRAN-FILE", "dd": f"{s3}TRAN"}])
    pb.data_division()
    pb.file_section()
    pb.line(pb.AREA_A, "FD  TRAN-FILE")
    pb.line(pb.AREA_B, "LABEL RECORDS ARE STANDARD.")
    pb.fd_records["TRAN-FILE"] = f"{pt}-TRAN-RECORD"
    for s in pb.selects:
        s["fd_record"] = f"{pt}-TRAN-RECORD"
    pb.copy(f"{s3}TRANR")
    pb.working_storage()
    ws_constants(pb, name, list(extra_ws))
    pb.copy(f"{s3}WORKA")
    pb.copy("CMNERRA")
    for cp in copies:
        if cp.get("wrapper"):
            pb.wrapper_01(cp["wrapper"])
        pb.copy(**{k: v for k, v in cp.items() if k != "wrapper"})
    pb.procedure()
    pb.para("0000-MAIN")
    pb.open_([("INPUT", ["TRAN-FILE"])])
    pb.end_stmt()
    pb.perform("1000-PROCESS", until=f"{pw}-EOF", tests=[f"{pw}-EOF"])
    pb.close(["TRAN-FILE"])
    pb.end_stmt()
    pb.stmt("GOBACK.")
    pb.para("1000-PROCESS")
    pb.read("TRAN-FILE", at_end=f"SET {pw}-EOF TO TRUE")
    pb.end_stmt()
    if body:
        body(pb, c)
    pb.add_to("1", f"{pw}-READ-CNT")
    pb.end_stmt()
    return pb


def build_policy_specials(estate) -> None:
    sys_ = "POLICY"
    c = S(estate, sys_)
    pt, pw = c["p"]["trans"], c["p"]["work"]
    # D3: SECTION then COPY of a procedure copybook that sits in a PROCS folder
    pb = new(estate, "POLUPD02", sys_)
    pb.identification()
    pb.environment([{"name": "TRAN-FILE", "dd": "POLTRAN"}])
    pb.data_division()
    pb.file_section()
    pb.line(pb.AREA_A, "FD  TRAN-FILE")
    pb.line(pb.AREA_B, "LABEL RECORDS ARE STANDARD.")
    pb.fd_records["TRAN-FILE"] = f"{pt}-TRAN-RECORD"
    pb.selects[0]["fd_record"] = f"{pt}-TRAN-RECORD"
    pb.copy("POLTRANR")
    pb.working_storage()
    ws_constants(pb, "POLUPD02")
    pb.copy("POLWORKA")
    pb.procedure()
    pb.para("0000-MAIN")
    pb.open_([("INPUT", ["TRAN-FILE"])])
    pb.end_stmt()
    pb.perform("A-100-BEGIN")
    pb.perform("B-100-EDIT-KEY", thru="B-200-EDIT-DATE")
    pb.close(["TRAN-FILE"])
    pb.end_stmt()
    pb.stmt("GOBACK.")
    pb.section("A-100-BEGIN")
    pb.read("TRAN-FILE", at_end=f"SET {pw}-EOF TO TRUE")
    pb.end_stmt()
    n = pb.copy("POLPROCB", prefix_text="Z-100-EDITS SECTION.", expect="not_found")
    pb.sections.append({"name": "Z-100-EDITS", "line": n})
    pb.fact("section", n, name="Z-100-EDITS")
    estate.add_program(pb, status="partial", why="COPY POLPROCB NOT FOUND (filed proc by its folder)", role="D3")
    # D5: two programs copy the missing POLMISSB, one with a REPLACING over two lines
    def body3(pb, c):
        pb.move(f"{pt}-{c['key']}", ["WA-AUDIT-KEY"])
        pb.move(f"{pt}-USER-ID", ["WA-AUDIT-USER"])
        pb.move(f"{pt}-TRAN-AMT", ["WA-AUDIT-AMT"])
        pb.display(["'AUDIT '", "WA-AUDIT-KEY"], fields=["WA-AUDIT-KEY"])
    pb = small_batch(estate, sys_, "POLUPD03",
                     [{"book": "POLMISSB", "replacing": [Replacing("name_dot", "PMSS-AUDIT-REC", "WS-POL-AUDIT-RECORD"),
                                                         Replacing("leading", "PMSS-", "WA-")], "split_replacing": True,
                       "expect": "not_found_then_ok"}], body=body3)
    estate.add_program(pb, role="D5")

    def body4(pb, c):
        pb.move(f"{pt}-{c['key']}", ["PMSS-AUDIT-KEY"])
        pb.display(["'AUDIT '", "PMSS-AUDIT-KEY"], fields=["PMSS-AUDIT-KEY"])
    pb = small_batch(estate, sys_, "POLUPD04", [{"book": "POLMISSB", "quoted": True, "expect": "not_found_then_ok"}], body=body4)
    estate.add_program(pb, role="D5")
    # D6: the stub
    pb = small_batch(estate, sys_, "POLUPD05", [{"wrapper": "WS-STUB-AREA", "book": "POLSTUBB", "expect": "not_found"},
                                                 {"wrapper": "WS-STUB-AREA-2", "book": "POLSTUBC", "expect": "resolved_empty"}],
                     pid_own_line=True)
    estate.add_program(pb, status="partial", why="COPY POLSTUBB NOT FOUND (member filed empty: text in columns 1-7)", role="D6")
    # arrived after the first build
    def body6(pb, c):
        pb.if_open("PARR-RESTARTING", tests=["PARR-RESTARTING"])
        pb.add_to("1", "PARR-RESTART-CNT", indent=4)
        pb.end_if()
    pb = small_batch(estate, sys_, "POLUPD06", [{"book": "POLARRVB", "expect": "not_found_then_ok"}], body=body6)
    estate.add_program(pb, role="ARRIVED")
    # the OS/VS COPY form: `01 NAME COPY 'BOOK'.` renames the library's 01 (LESSONS 177)
    def body7(pb, c):
        pb.move(f"{pt}-{c['key']}", ["PG1-" + c["key"]])
        pb.move("POL-GEN1-AREA", ["WS-SAVE-AREA"])
        pb.display(["'GEN1 '", "WS-SAVE-AREA"], fields=["WS-SAVE-AREA"])
    pb = small_batch(estate, sys_, "POLUPD07", [{"book": "POLGEN1R", "osvs_01": "POL-GEN1-AREA", "quoted": True}],
                     extra_ws=[Item(5, "WS-SAVE-AREA", pic="X(200)")], body=body7)
    pb.notes.append("OS/VS level-01 COPY form: POL-GEN1-AREA stands for PG1-RECORD")
    estate.add_program(pb, role="osvs 01 COPY")


# --------------------------------------------------------------------------
# extract, report, history
# --------------------------------------------------------------------------

def ext01(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    pm, px, pw, pc = p["master"], p["extract"], p["work"], p["control"]
    pb = new(estate, f"{s3}EXT01", sys_, tag=(s3 + "X1") if sys_ == "BILLING" else "")
    pb.identification(remarks="EXTRACT OF THE MASTER FILE FOR THE DOWNSTREAM WAREHOUSE.")
    pb.environment([{"name": "MASTER-FILE", "dd": f"{s3}MAST", "org": "INDEXED", "access": "SEQUENTIAL", "key": f"{pm}-{key}"},
                    {"name": "EXTRACT-FILE", "dd": f"{s3}EXTR"},
                    {"name": "CONTROL-FILE", "dd": f"{s3}CTL"}])
    pb.data_division()
    pb.file_section()
    pb.fd("MASTER-FILE", f"{pm}-MASTER-RECORD", copybook=f"{s3}MASTR")
    pb.fd("EXTRACT-FILE", f"{px}-EXTRACT-RECORD", copybook=f"{s3}EXTR", lrecl=200)
    pb.fd("CONTROL-FILE", f"{pc}-CONTROL-RECORD", copybook=f"{s3}CTLR")
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-EXCLUDE-LAPSED", pic="X(01)", value="'Y'")])
    pb.copy(f"{s3}WORKA")
    pb.copy("STDHDR", expect="chosen")
    pb.copy("CMNDATEA", expect="not_found_then_ok")
    pb.procedure()
    pb.para("0000-MAIN")
    pb.open_([("INPUT", ["MASTER-FILE", "CONTROL-FILE"]), ("OUTPUT", ["EXTRACT-FILE"])])
    pb.end_stmt()
    pb.read("CONTROL-FILE", at_end="CONTINUE")
    pb.end_stmt()
    pb.move("WS-PROGRAM-NAME", ["HDR-PROGRAM"])
    pb.move(f"{pc}-RUN-DATE", ["HDR-RUN-DATE", "START-DATE"])
    pb.perform("1000-READ-MASTER")
    pb.perform("2000-EXTRACT", until=f"{pw}-EOF", tests=[f"{pw}-EOF"])
    pb.close(["MASTER-FILE", "EXTRACT-FILE", "CONTROL-FILE"])
    pb.display(["'EXTRACTED '", f"{pw}-WRITE-CNT", "' OF '", f"{pw}-READ-CNT"], fields=[f"{pw}-WRITE-CNT", f"{pw}-READ-CNT"])
    pb.stmt("GOBACK.")
    pb.para("1000-READ-MASTER")
    pb.read("MASTER-FILE", next_=True, at_end=f"SET {pw}-EOF TO TRUE")
    pb.end_stmt()
    pb.if_open(f"NOT {pw}-EOF", tests=[f"{pw}-EOF"])
    pb.add_to("1", f"{pw}-READ-CNT", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.para("2000-EXTRACT")
    pb.if_open(f"{pm}-{c['status'][1][0]} AND WS-EXCLUDE-LAPSED = 'Y'", tests=[f"{pm}-{c['status'][1][0]}", "WS-EXCLUDE-LAPSED"],
               lits=[("Y", "WS-EXCLUDE-LAPSED")])
    pb.stmt("CONTINUE", 4)
    pb.else_()
    pb.initialize(f"{px}-EXTRACT-RECORD", indent=4)
    pb.move(f"{pm}-{key}", [f"{px}-{key}"], indent=4)
    pb.move(f"{pm}-STATUS", [f"{px}-STATUS"], indent=4)
    pb.move(f"{pm}-{c['amts'][0]}", [f"{px}-{c['amts'][0]}"], indent=4)
    pb.move(f"{pm}-{c['amts'][1]}", [f"{px}-{c['amts'][1]}"], indent=4)
    pb.move(f"{pm}-{c['dates'][0]}", [f"{px}-{c['dates'][0]}"], indent=4)
    pb.move(f"{pm}-AGENT-ID", [f"{px}-AGENT-ID"], indent=4)
    pb.move(f"{pm}-POSTAL-CD", [f"{px}-POSTAL-CD"], indent=4)
    pb.move(f"{pc}-RUN-DATE", [f"{px}-AS-OF-DT"], indent=4)
    pb.string_([f"{pm}-LAST-NAME DELIMITED BY SPACE", "', '", f"{pm}-FIRST-NAME DELIMITED BY SPACE"], f"{px}-NAME-TEXT",
               indent=4, fields=[f"{pm}-LAST-NAME", f"{pm}-FIRST-NAME"])
    pb.move(f"{pm}-ITEM-COUNT", [f"{px}-COUNT-1"], indent=4)
    pb.write(f"{px}-EXTRACT-RECORD", "EXTRACT-FILE", indent=4)
    pb.add_to("1", f"{pw}-WRITE-CNT", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.perform("1000-READ-MASTER")
    pb.end_stmt()
    return pb


def rpt01(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    px, pr, pw = p["extract"], p["report"], p["work"]
    pb = new(estate, f"{s3}RPT01", sys_, seq=(sys_ == "CLAIMS"))
    pb.identification(remarks="CONTROL REPORT FROM THE SORTED EXTRACT. ONE LINE PER RECORD, TOTALS AT THE END.")
    pb.environment([{"name": "SORTED-FILE", "dd": f"{s3}SRTD"}, {"name": "REPORT-FILE", "dd": f"{s3}RPT"}])
    pb.data_division()
    pb.file_section()
    pb.fd("SORTED-FILE", f"{px}-EXTRACT-RECORD", copybook=f"{s3}EXTR")
    pb.fd("REPORT-FILE", "REPORT-LINE", root=Item(1, "REPORT-LINE", children=[Item(5, "RPT-CC", pic="X(01)"), Item(5, "RPT-TEXT", pic="X(132)")]))
    pb.working_storage()
    ws_constants(pb, pb.name)
    pb.copy(f"{s3}WORKA")
    pb.copy(f"{s3}RPTL")
    pb.copy("STDHDR", expect="chosen")
    pb.item(Item(1, "WS-TOTALS", children=[Item(5, "WS-TOT-AMT-1", pic="S9(13)V99", usage="COMP-3", value="ZERO"),
                                          Item(5, "WS-TOT-AMT-2", pic="S9(13)V99", usage="COMP-3", value="ZERO"),
                                          Item(5, "WS-PREV-KEY", pic=f"X({c['klen']})", value="SPACES")]))
    pb.procedure()
    pb.para("0000-MAIN")
    pb.open_([("INPUT", ["SORTED-FILE"]), ("OUTPUT", ["REPORT-FILE"])])
    pb.end_stmt()
    pb.move("WS-PROGRAM-NAME", ["HDR-PROGRAM"])
    pb.perform("1000-HEADINGS")
    pb.perform("2000-DETAIL", until=f"{pw}-EOF", tests=[f"{pw}-EOF"])
    pb.perform("3000-TOTALS")
    pb.close(["SORTED-FILE", "REPORT-FILE"])
    pb.stmt("GOBACK.")
    pb.para("1000-HEADINGS")
    pb.add_to("1", f"{pw}-PAGE-CNT")
    pb.move(f"{pw}-PAGE-CNT", [f"{pr}-H1-PAGE"])
    pb.move("HDR-RUN-DATE", [f"{pr}-H1-DATE"])
    pb.write("REPORT-LINE", "REPORT-FILE", frm=f"{pr}-HEADING-1", after="PAGE")
    pb.write("REPORT-LINE", "REPORT-FILE", frm=f"{pr}-HEADING-2", after="2 LINES")
    pb.move_lit("+3", [f"{pw}-LINE-CNT"], quote=False)
    pb.end_stmt()
    pb.para("2000-DETAIL")
    pb.read("SORTED-FILE", at_end=f"SET {pw}-EOF TO TRUE")
    pb.end_stmt()
    pb.if_open(f"NOT {pw}-EOF", tests=[f"{pw}-EOF"])
    pb.if_open(f"{pw}-LINE-CNT > 55", tests=[f"{pw}-LINE-CNT"], indent=4)
    pb.perform("1000-HEADINGS", indent=8)
    pb.end_if(indent=4)
    pb.if_open(f"{px}-{key} = WS-PREV-KEY", tests=[f"{px}-{key}", "WS-PREV-KEY"], indent=4)
    pb.move_lit("E006", ["ERR-CODE"], indent=8) if "ERR-CODE" in pb.declared else pb.move_lit("*", [f"{pr}-CC"], indent=8)
    pb.end_if(indent=4)
    pb.move(f"{px}-{key}", [f"{pr}-{key}", "WS-PREV-KEY"], indent=4)
    pb.move(f"{px}-NAME-TEXT", [f"{pr}-NAME"], indent=4)
    pb.move(f"{px}-{c['amts'][0]}", [f"{pr}-AMT-1"], indent=4)
    pb.move(f"{px}-{c['amts'][1]}", [f"{pr}-AMT-2"], indent=4)
    pb.move(f"{px}-STATUS", [f"{pr}-STATUS"], indent=4)
    pb.move(f"{px}-AS-OF-DT", [f"{pr}-DATE"], indent=4)
    pb.add_to(f"{px}-{c['amts'][0]}", "WS-TOT-AMT-1", indent=4)
    pb.add_to(f"{px}-{c['amts'][1]}", "WS-TOT-AMT-2", indent=4)
    pb.write("REPORT-LINE", "REPORT-FILE", frm=f"{pr}-DETAIL-LINE", indent=4)
    pb.add_to("1", f"{pw}-LINE-CNT", indent=4)
    pb.add_to("1", f"{pw}-READ-CNT", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.para("3000-TOTALS")
    pb.move("WS-TOT-AMT-1", [f"{pr}-AMT-1"])
    pb.move("WS-TOT-AMT-2", [f"{pr}-AMT-2"])
    pb.move_lit("TOTAL", [f"{pr}-NAME"])
    pb.write("REPORT-LINE", "REPORT-FILE", frm=f"{pr}-DETAIL-LINE", after="2 LINES")
    pb.display(["'REPORT LINES: '", f"{pw}-READ-CNT"], fields=[f"{pw}-READ-CNT"])
    pb.end_stmt()
    return pb


def his01(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    ph, pw = p["history"], p["work"]
    pb = new(estate, f"{s3}HIS01", sys_)
    pb.identification(remarks="HISTORY FILE LISTING AND PURGE CANDIDATES.")
    pb.environment([{"name": "HIST-FILE", "dd": f"{s3}HIST"}, {"name": "REPORT-FILE", "dd": f"{s3}RPT"}])
    pb.data_division()
    pb.file_section()
    pb.line(pb.AREA_A, "FD  HIST-FILE")
    pb.line(pb.AREA_B, "LABEL RECORDS ARE STANDARD.")
    pb.fd_records["HIST-FILE"] = f"{ph}-HIST-RECORD"
    pb.selects[0]["fd_record"] = f"{ph}-HIST-RECORD"
    pb.copy(f"{s3}HISTR")
    pb.fd("REPORT-FILE", "REPORT-LINE", root=Item(1, "REPORT-LINE", pic="X(133)"))
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-PURGE-DATE", pic="9(08)")])
    pb.copy(f"{s3}WORKA")
    pb.copy("CMNDATEA", expect="not_found_then_ok")
    pb.item(Item(1, "WS-DETAIL", children=[Item(5, "WD-KEY", pic=f"X({c['klen']})"), Item(5, "FILLER", pic="X(02)", value="SPACES"),
                                          Item(5, "WD-TYPE", pic="X(01)"), Item(5, "FILLER", pic="X(02)", value="SPACES"),
                                          Item(5, "WD-USER", pic="X(08)"), Item(5, "FILLER", pic="X(02)", value="SPACES"),
                                          Item(5, "WD-DATE", pic="9(08)"), Item(5, "FILLER", pic="X(100)", value="SPACES")]))
    pb.procedure()
    pb.para("0000-MAIN")
    pb.open_([("INPUT", ["HIST-FILE"]), ("OUTPUT", ["REPORT-FILE"])])
    pb.end_stmt()
    pb.stmt("ACCEPT WS-PURGE-DATE FROM DATE YYYYMMDD.")
    pb._refs(pb.em.n, ["WS-PURGE-DATE"], "write", "ACCEPT")
    pb.perform("1000-LIST", until=f"{pw}-EOF", tests=[f"{pw}-EOF"])
    pb.close(["HIST-FILE", "REPORT-FILE"])
    pb.stmt("GOBACK.")
    pb.para("1000-LIST")
    pb.read("HIST-FILE", at_end=f"SET {pw}-EOF TO TRUE")
    pb.end_stmt()
    pb.if_open(f"NOT {pw}-EOF", tests=[f"{pw}-EOF"])
    pb.move(f"{ph}-{key}", ["WD-KEY"], indent=4)
    pb.move(f"{ph}-CHG-TYPE", ["WD-TYPE"], indent=4)
    pb.move(f"{ph}-CHG-USER", ["WD-USER"], indent=4)
    pb.move(f"{ph}-CHG-DT", ["WD-DATE", "START-DATE"], indent=4)
    pb.move("WS-PURGE-DATE", ["END-DATE"], indent=4)
    pb.call_static("CMNDATE", using=["CMN-DATE-AREA"], indent=4)
    pb.if_open("DAYS-BETWEEN > 730", tests=["DAYS-BETWEEN"], indent=4)
    pb.move_lit("P", ["WD-TYPE"], indent=8)
    pb.end_if(indent=4)
    pb.write("REPORT-LINE", "REPORT-FILE", frm="WS-DETAIL", indent=4)
    pb.add_to("1", f"{pw}-READ-CNT", indent=4)
    pb.end_if()
    pb.end_stmt()
    return pb


# --------------------------------------------------------------------------
# DB2
# --------------------------------------------------------------------------

def db201(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    pt, pw, ph = p["trans"], p["work"], p["history"]
    tbl = c["tables"][0]
    hist = c["tables"][1]
    kcol = tbl.key
    amt0 = c["amts"][0].replace("-", "_")
    pb = new(estate, f"{s3}DB201", sys_)
    pb.identification(remarks=f"APPLY THE TRANSACTION FILE TO {tbl.qualified}; EVERY CHANGE INTO {hist.qualified}.")
    pb.environment([{"name": "TRAN-FILE", "dd": f"{s3}TRAN"}, {"name": "HIST-FILE", "dd": f"{s3}HIST"}])
    pb.data_division()
    pb.file_section()
    pb.line(pb.AREA_A, "FD  TRAN-FILE")
    pb.line(pb.AREA_B, "LABEL RECORDS ARE STANDARD.")
    pb.fd_records["TRAN-FILE"] = f"{pt}-TRAN-RECORD"
    pb.selects[0]["fd_record"] = f"{pt}-TRAN-RECORD"
    pb.copy(f"{s3}TRANR")
    pb.line(pb.AREA_A, "FD  HIST-FILE")
    pb.line(pb.AREA_B, "LABEL RECORDS ARE STANDARD.")
    pb.fd_records["HIST-FILE"] = f"{ph}-HIST-RECORD"
    pb.selects[1]["fd_record"] = f"{ph}-HIST-RECORD"
    pb.copy(f"{s3}HISTR")
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-OLD-AMT", pic="S9(09)V99", usage="COMP-3"), Item(5, "WS-SEQ-NO", pic="S9(09)", usage="COMP"),
                               Item(5, "WS-CHG-USER", pic="X(08)"), Item(5, "WS-ROWS", pic="S9(09)", usage="COMP"),
                               Item(5, "WS-KEY-IN", pic=f"X({c['klen']})")])
    pb.copy(f"{s3}WORKA")
    pb.copy("CMNSQLW")
    pb.copy("CMNERRA")
    pb.copy("SQLCA", sql_include=True, expect="system")
    pb.copy(tbl.dclgen, sql_include=True)
    pb.exec_sql([f"DECLARE {s3}CUR CURSOR FOR", f"SELECT {kcol}, STATUS_CD, {amt0}", f"FROM {tbl.qualified}",
                 "WHERE STATUS_CD = :STATUS-CD", f"ORDER BY {kcol}"], "DECLARE", [tbl.qualified], cursor=f"{s3}CUR",
                cols=[{"tbl": tbl.name, "col": "STATUS_CD", "host": "STATUS-CD", "mode": "predicate"}], hosts=["STATUS-CD"])
    pb.procedure()
    pb.para("0000-MAIN")
    pb.open_([("INPUT", ["TRAN-FILE"]), ("EXTEND", ["HIST-FILE"])])
    pb.end_stmt()
    pb.move_lit("AC", ["STATUS-CD"])
    pb.exec_sql([f"OPEN {s3}CUR"], "OPEN", [tbl.qualified], cursor=f"{s3}CUR")
    pb.perform("1000-FETCH", until="SQLCODE NOT = 0", tests=["SQLCODE"])
    pb.exec_sql([f"CLOSE {s3}CUR"], "CLOSE", [tbl.qualified], cursor=f"{s3}CUR")
    pb.perform("2000-APPLY", until=f"{pw}-EOF", tests=[f"{pw}-EOF"])
    pb.close(["TRAN-FILE", "HIST-FILE"])
    pb.display(["'ROWS FETCHED '", "WS-ROWS"], fields=["WS-ROWS"])
    pb.stmt("GOBACK.")
    pb.para("1000-FETCH")
    pb.exec_sql([f"FETCH {s3}CUR", f"INTO :{tbl.host(kcol)}, :STATUS-CD, :{tbl.host(amt0)}"], "FETCH", [tbl.qualified],
                cursor=f"{s3}CUR", cols=[{"tbl": tbl.name, "col": kcol, "host": tbl.host(kcol), "mode": "read"},
                                          {"tbl": tbl.name, "col": "STATUS_CD", "host": "STATUS-CD", "mode": "read"},
                                          {"tbl": tbl.name, "col": amt0, "host": tbl.host(amt0), "mode": "read"}])
    pb.if_open("SQLCODE = 0", tests=["SQLCODE"])
    pb.add_to("1", "WS-ROWS", indent=4)
    pb.add_to("1", "SQL-ROW-COUNT", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.para("2000-APPLY")
    pb.read("TRAN-FILE", at_end=f"SET {pw}-EOF TO TRUE")
    pb.end_stmt()
    pb.if_open(f"{pw}-EOF", tests=[f"{pw}-EOF"])
    pb.goto("2000-EXIT", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.move(f"{pt}-{key}", ["WS-KEY-IN"])
    pb.exec_sql([f"SELECT {amt0}, STATUS_CD", f"INTO :WS-OLD-AMT, :STATUS-CD", f"FROM {tbl.qualified}", f"WHERE {kcol} = :WS-KEY-IN"],
                "SELECT", [tbl.qualified], cols=[{"tbl": tbl.name, "col": amt0, "host": "WS-OLD-AMT", "mode": "read"},
                                                  {"tbl": tbl.name, "col": "STATUS_CD", "host": "STATUS-CD", "mode": "read"},
                                                  {"tbl": tbl.name, "col": kcol, "host": "WS-KEY-IN", "mode": "predicate"}])
    pb.evaluate_open("SQLCODE")
    pb.when("0", lit="0")
    pb.perform("2100-UPDATE", indent=8)
    pb.when("+100", lit="+100")
    pb.perform("2200-INSERT", indent=8)
    pb.when("OTHER")
    pb.move("SQLCODE", ["ERR-SQLCODE"], indent=8)
    pb.call_static("CMNSQLER", using=["CMN-ERROR-AREA", "SQLCA"], indent=8)
    pb.end_evaluate()
    pb.end_stmt()
    pb.exit_para("2000-EXIT")
    pb.para("2100-UPDATE")
    pb.exec_sql([f"UPDATE {tbl.qualified}", f"SET {amt0} = :{pt}-TRAN-AMT,", "LAST_UPD_USER = :" + f"{pt}-USER-ID,",
                 "LAST_UPD_TS = CURRENT TIMESTAMP", f"WHERE {kcol} = :WS-KEY-IN"], "UPDATE", [tbl.qualified],
                cols=[{"tbl": tbl.name, "col": amt0, "host": f"{pt}-TRAN-AMT", "mode": "write"},
                      {"tbl": tbl.name, "col": "LAST_UPD_USER", "host": f"{pt}-USER-ID", "mode": "write"},
                      {"tbl": tbl.name, "col": kcol, "host": "WS-KEY-IN", "mode": "predicate"}])
    pb.add_to("1", "WS-SEQ-NO")
    pb.exec_sql([f"INSERT INTO {hist.qualified}", f"({kcol}, SEQ_NO, CHG_DT, CHG_USER, CHG_TYPE, OLD_AMT, NEW_AMT)",
                 f"VALUES (:WS-KEY-IN, :WS-SEQ-NO, CURRENT DATE, :{pt}-USER-ID, 'U',", f":WS-OLD-AMT, :{pt}-TRAN-AMT)"],
                "INSERT", [hist.qualified], cols=[{"tbl": hist.name, "col": kcol, "host": "WS-KEY-IN", "mode": "write"},
                                                  {"tbl": hist.name, "col": "SEQ_NO", "host": "WS-SEQ-NO", "mode": "write"},
                                                  {"tbl": hist.name, "col": "CHG_USER", "host": f"{pt}-USER-ID", "mode": "write"},
                                                  {"tbl": hist.name, "col": "OLD_AMT", "host": "WS-OLD-AMT", "mode": "write"},
                                                  {"tbl": hist.name, "col": "NEW_AMT", "host": f"{pt}-TRAN-AMT", "mode": "write"}])
    pb.move("WS-KEY-IN", [f"{ph}-{key}"])
    pb.move("WS-OLD-AMT", [f"{ph}-OLD-AMT"])
    pb.move(f"{pt}-TRAN-AMT", [f"{ph}-NEW-AMT"])
    pb.move_lit("U", [f"{ph}-CHG-TYPE"])
    pb.write(f"{ph}-HIST-RECORD", "HIST-FILE")
    pb.end_stmt()
    pb.para("2200-INSERT")
    pb.if_open(f"{pt}-DELETE", tests=[f"{pt}-DELETE"])
    pb.exec_sql([f"DELETE FROM {tbl.qualified}", f"WHERE {kcol} = :WS-KEY-IN"], "DELETE", [tbl.qualified],
                cols=[{"tbl": tbl.name, "col": kcol, "host": "WS-KEY-IN", "mode": "predicate"}], indent=4)
    pb.else_()
    pb.exec_sql([f"INSERT INTO {tbl.qualified}", f"({kcol}, STATUS_CD, EFF_DT, AGENT_ID, REGION_CD, {amt0}, LAST_UPD_USER, LAST_UPD_TS)",
                 f"VALUES (:WS-KEY-IN, 'PN', CURRENT DATE, 'SYSTEM', 'ON',", f":{pt}-TRAN-AMT, :{pt}-USER-ID, CURRENT TIMESTAMP)"],
                "INSERT", [tbl.qualified], cols=[{"tbl": tbl.name, "col": kcol, "host": "WS-KEY-IN", "mode": "write"},
                                                 {"tbl": tbl.name, "col": amt0, "host": f"{pt}-TRAN-AMT", "mode": "write"},
                                                 {"tbl": tbl.name, "col": "LAST_UPD_USER", "host": f"{pt}-USER-ID", "mode": "write"}], indent=4)
    pb.end_if()
    pb.end_stmt()
    return pb


def db202(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    px, pw = p["extract"], p["work"]
    item = c["tables"][2]
    kcol = item.key
    pb = new(estate, f"{s3}DB202", sys_)
    pb.identification(remarks=f"UNLOAD {item.qualified} TO A FLAT FILE, MATCHED AGAINST THE SORTED POLICY EXTRACT.")
    pb.environment([{"name": "UNLOAD-FILE", "dd": f"{s3}UNLD"}, {"name": "SORTED-FILE", "dd": f"{s3}SRTD"}])
    pb.data_division()
    pb.file_section()
    pb.fd("UNLOAD-FILE", "UNLOAD-RECORD", root=Item(1, "UNLOAD-RECORD", children=[Item(5, "UR-KEY", pic=f"X({c['klen']})"),
                                                                                  Item(5, "UR-ITEM-SEQ", pic="9(04)"),
                                                                                  Item(5, "UR-ITEM-CD", pic="X(04)"),
                                                                                  Item(5, "UR-ITEM-LIMIT", pic="S9(11)V99", usage="COMP-3"),
                                                                                  Item(5, "UR-ITEM-PREM", pic="S9(07)V99", usage="COMP-3"),
                                                                                  Item(5, "UR-DESC", pic="X(30)"),
                                                                                  Item(5, "FILLER", pic=f"X({200 - c['klen'] - 4 - 4 - 7 - 5 - 30})")]))
    pb.fd("SORTED-FILE", "PX-EXTRACT-RECORD" if sys_ == "POLICY" else f"{px}-EXTRACT-RECORD",
          copybook="POLEXTR" if sys_ != "POLICY" else f"{s3}EXTR")
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-ROWS", pic="S9(09)", usage="COMP", value="ZERO")])
    pb.copy(f"{s3}WORKA")
    pb.copy("CMNSQLW")
    pb.copy("SQLCA", sql_include=True, expect="system")
    pb.copy(item.dclgen, sql_include=True)
    pb.exec_sql([f"DECLARE ITEMCUR CURSOR FOR", f"SELECT {kcol}, ITEM_SEQ, ITEM_CD, ITEM_LIMIT, ITEM_PREM, ITEM_DESC",
                 f"FROM {item.qualified}", f"WHERE {kcol} = :{item.host(kcol)}"], "DECLARE", [item.qualified], cursor="ITEMCUR",
                cols=[{"tbl": item.name, "col": kcol, "host": item.host(kcol), "mode": "predicate"}])
    pb.procedure()
    pb.para("0000-MAIN")
    pb.open_([("INPUT", ["SORTED-FILE"]), ("OUTPUT", ["UNLOAD-FILE"])])
    pb.end_stmt()
    pb.perform("1000-EACH-KEY", until=f"{pw}-EOF", tests=[f"{pw}-EOF"])
    pb.close(["SORTED-FILE", "UNLOAD-FILE"])
    pb.display(["'UNLOADED ROWS '", "WS-ROWS"], fields=["WS-ROWS"])
    pb.stmt("GOBACK.")
    pb.para("1000-EACH-KEY")
    pb.read("SORTED-FILE", at_end=f"SET {pw}-EOF TO TRUE")
    pb.end_stmt()
    pb.if_open(f"NOT {pw}-EOF", tests=[f"{pw}-EOF"])
    pxk = "PX-POLICY-NO" if sys_ != "POLICY" else f"{px}-{key}"
    pb.move(pxk, [item.host(kcol)], indent=4)
    pb.exec_sql(["OPEN ITEMCUR"], "OPEN", [item.qualified], cursor="ITEMCUR", indent=4)
    pb.perform("1100-FETCH-ITEM", until="SQLCODE NOT = 0", tests=["SQLCODE"], indent=4)
    pb.exec_sql(["CLOSE ITEMCUR"], "CLOSE", [item.qualified], cursor="ITEMCUR", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.para("1100-FETCH-ITEM")
    pb.exec_sql(["FETCH ITEMCUR", f"INTO :{item.host(kcol)}, :ITEM-SEQ, :ITEM-CD, :ITEM-LIMIT,", ":ITEM-PREM, :ITEM-DESC"],
                "FETCH", [item.qualified], cursor="ITEMCUR",
                cols=[{"tbl": item.name, "col": kcol, "host": item.host(kcol), "mode": "read"},
                      {"tbl": item.name, "col": "ITEM_SEQ", "host": "ITEM-SEQ", "mode": "read"},
                      {"tbl": item.name, "col": "ITEM_CD", "host": "ITEM-CD", "mode": "read"},
                      {"tbl": item.name, "col": "ITEM_LIMIT", "host": "ITEM-LIMIT", "mode": "read"},
                      {"tbl": item.name, "col": "ITEM_PREM", "host": "ITEM-PREM", "mode": "read"}])
    pb.if_open("SQLCODE = 0", tests=["SQLCODE"])
    pb.move(item.host(kcol), ["UR-KEY"], indent=4)
    pb.move("ITEM-SEQ", ["UR-ITEM-SEQ"], indent=4)
    pb.move("ITEM-CD", ["UR-ITEM-CD"], indent=4)
    pb.move("ITEM-LIMIT", ["UR-ITEM-LIMIT"], indent=4)
    pb.move("ITEM-PREM", ["UR-ITEM-PREM"], indent=4)
    pb.move("ITEM-DESC-TEXT", ["UR-DESC"], indent=4)
    pb.write("UNLOAD-RECORD", "UNLOAD-FILE", indent=4)
    pb.add_to("1", "WS-ROWS", indent=4)
    pb.end_if()
    pb.end_stmt()
    return pb


# --------------------------------------------------------------------------
# IMS DL/I batch, BMP, MPP
# --------------------------------------------------------------------------

def ims01(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    ps, psc, pw, pcb = p["seg"], p["seg2"], p["work"], p["pcb"]
    kf = key.replace("-", "")[:8]
    pb = new(estate, f"{s3}IMS01", sys_)
    pb.identification(remarks=f"DL/I BATCH: READ EVERY {s3}ROOT AND ITS {s3}CHLD SEGMENTS, RE-STATUS THE LAPSED ONES.")
    pb.environment([])
    pb.data_division()
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-GU", pic="X(04)", value="'GU  '"), Item(5, "WS-GN", pic="X(04)", value="'GN  '"),
                               Item(5, "WS-GNP", pic="X(04)", value="'GNP '"), Item(5, "WS-GHU", pic="X(04)", value="'GHU '"),
                               Item(5, "WS-REPL", pic="X(04)", value="'REPL'"), Item(5, "WS-CHKP", pic="X(04)", value="'CHKP'"),
                               Item(5, "WS-CHKP-ID", pic="X(08)", value=f"'{s3}IMS01A'"), Item(5, "WS-PARM-CT", pic="S9(04)", usage="COMP", value="+4")])
    pb.copy(f"{s3}WORKA")
    pb.copy("CMNERRA")
    pb.wrapper_01("WS-ROOT-AREA")
    pb.copy(f"{s3}SEGA")
    pb.copy(f"{s3}SEGB")
    pb.item(Item(1, "WS-ROOT-SSA", children=[Item(5, "FILLER", pic="X(08)", value=f"'{s3}ROOT '"), Item(5, "FILLER", pic="X(01)", value="'('"),
                                            Item(5, "FILLER", pic="X(08)", value=f"'{kf:<8}'"), Item(5, "FILLER", pic="X(02)", value="' ='"),
                                            Item(5, "SSA-ROOT-KEY", pic=f"X({c['klen']})"), Item(5, "FILLER", pic="X(01)", value="')'")]))
    pb.item(Item(1, "WS-CHILD-SSA", pic="X(09)", value=f"'{s3}CHLD '"))
    pb.linkage_section()
    pb.copy("CMNIOPCB")
    pb.copy("CMNPCBM", replacing=[Replacing("tag", ":PCB:", pcb)])
    pb.procedure(using=["IO-PCB", f"{pcb}-PCB"])
    pb.para("0000-MAIN")
    pb.move_lit("N", [f"{pw}-EOF-SW"])
    pb.move("SPACES", ["SSA-ROOT-KEY"])
    pb.dli("WS-GU", "GU", f"{pcb}-PCB", io_area=f"{ps}-ROOT-SEG", ssas=["WS-ROOT-SSA"])
    pb.perform("1000-EACH-ROOT", until=f"{pw}-EOF", tests=[f"{pw}-EOF"])
    pb.dli("WS-CHKP", "CHKP", "IO-PCB", io_area="WS-CHKP-ID")
    pb.display(["'ROOTS READ '", f"{pw}-READ-CNT"], fields=[f"{pw}-READ-CNT"])
    pb.stmt("GOBACK.")
    pb.para("1000-EACH-ROOT")
    pb.evaluate_open(f"{pcb}-STATUS")
    pb.when("SPACES")
    pb.perform("1100-PROCESS-ROOT", indent=8)
    pb.when("'GB'", lit="GB")
    pb.set_true(f"{pw}-EOF", indent=8)
    pb.when("OTHER")
    pb.move(f"{pcb}-STATUS", ["ERR-CODE"], indent=8)
    pb.call_static("CMNERR", using=["CMN-ERROR-AREA"], indent=8)
    pb.end_evaluate()
    pb.end_stmt()
    pb.para("1100-PROCESS-ROOT")
    pb.add_to("1", f"{pw}-READ-CNT")
    pb.if_open(f"{ps}-STATUS = 'LP'", tests=[f"{ps}-STATUS"], lits=[("LP", f"{ps}-STATUS")])
    pb.move_lit("CN", [f"{ps}-STATUS"], indent=4)
    pb.dli("WS-REPL", "REPL", f"{pcb}-PCB", io_area=f"{ps}-ROOT-SEG", indent=4)
    pb.add_to("1", f"{pw}-WRITE-CNT", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.dli("WS-GNP", "GNP", f"{pcb}-PCB", io_area=f"{psc}-CHILD-SEG", ssas=["WS-CHILD-SSA"])
    pb.perform("1200-EACH-CHILD", until=f"{pcb}-STATUS NOT = SPACES", tests=[f"{pcb}-STATUS"])
    pb.dli("WS-GN", "GN", f"{pcb}-PCB", io_area=f"{ps}-ROOT-SEG", ssas=["WS-ROOT-SSA"], parmcount="WS-PARM-CT")
    pb.end_stmt()
    pb.para("1200-EACH-CHILD")
    pb.add_to(f"{psc}-ITEM-PREM", f"{pw}-TOTAL-AMT")
    pb.dli("WS-GNP", "GNP", f"{pcb}-PCB", io_area=f"{psc}-CHILD-SEG", ssas=["WS-CHILD-SSA"])
    pb.end_stmt()
    return pb


def ims02(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    ps, pw, pcb, px = p["seg"], p["work"], p["pcb"], p["extract"]
    pb = new(estate, f"{s3}IMS02", sys_)
    pb.identification(remarks="BMP: EXTRACT THE ROOT SEGMENTS" + (" TO A GSAM FILE." if sys_ == "POLICY" else "."))
    pb.environment([])
    pb.data_division()
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-GN", pic="X(04)", value="'GN  '"), Item(5, "WS-ISRT", pic="X(04)", value="'ISRT'"),
                               Item(5, "WS-GU", pic="X(04)", value="'GU  '")])
    pb.copy(f"{s3}WORKA")
    pb.wrapper_01("WS-ROOT-AREA")
    pb.copy(f"{s3}SEGA")
    pb.wrapper_01("WS-GSAM-RECORD")
    pb.copy(f"{s3}EXTR")
    if sys_ == "CLAIMS":
        pb.item(Item(1, "WS-INDEX-SSA", children=[Item(5, "FILLER", pic="X(08)", value="'CLMROOT '"), Item(5, "FILLER", pic="X(01)", value="'('"),
                                                 Item(5, "FILLER", pic="X(08)", value="'CLMXNAME'"), Item(5, "FILLER", pic="X(02)", value="' ='"),
                                                 Item(5, "SSA-NAME-KEY", pic="X(30)"), Item(5, "FILLER", pic="X(01)", value="')'")]))
    pb.linkage_section()
    pb.copy("CMNIOPCB")
    if sys_ == "POLICY":
        pb.copy("CMNPCBM", replacing=[Replacing("tag", ":PCB:", "POLGS")])
    pb.copy("CMNPCBM", replacing=[Replacing("tag", ":PCB:", pcb)])
    if sys_ == "CLAIMS":
        pb.copy("CMNPCBM", replacing=[Replacing("tag", ":PCB:", "CLMU")])
    using = ["IO-PCB"] + (["POLGS-PCB"] if sys_ == "POLICY" else []) + [f"{pcb}-PCB"] + (["CLMU-PCB"] if sys_ == "CLAIMS" else [])
    pb.procedure(using=using)
    pb.para("0000-MAIN")
    if sys_ == "CLAIMS":
        pb.move_lit("SMITH", ["SSA-NAME-KEY"])
        pb.dli("WS-GU", "GU", f"{pcb}-PCB", io_area=f"{ps}-ROOT-SEG", ssas=["WS-INDEX-SSA"])
    else:
        pb.dli("WS-GN", "GN", f"{pcb}-PCB", io_area=f"{ps}-ROOT-SEG")
    pb.perform("1000-EACH", until=f"{pcb}-STATUS NOT = SPACES", tests=[f"{pcb}-STATUS"])
    pb.display(["'BMP EXTRACTED '", f"{pw}-WRITE-CNT"], fields=[f"{pw}-WRITE-CNT"])
    pb.stmt("GOBACK.")
    pb.para("1000-EACH")
    pb.move(f"{ps}-{key}", [f"{px}-{key}"])
    pb.move(f"{ps}-STATUS", [f"{px}-STATUS"])
    pb.move(f"{ps}-INSURED-NAME", [f"{px}-NAME-TEXT"])
    pb.move(f"{ps}-AGENT-ID", [f"{px}-AGENT-ID"])
    if sys_ == "POLICY":
        pb.dli("WS-ISRT", "ISRT", "POLGS-PCB", io_area="WS-GSAM-RECORD")
    else:
        pb.display(["'ROOT '", f"{px}-{key}"], fields=[f"{px}-{key}"])
    pb.add_to("1", f"{pw}-WRITE-CNT")
    pb.dli("WS-GN", "GN", f"{pcb}-PCB", io_area=f"{ps}-ROOT-SEG")
    pb.end_stmt()
    return pb


def onl03(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    ps, pw, pcb, pa = p["seg"], p["work"], p["pcb"], p["comm"]
    pb = new(estate, f"{s3}ONL03", sys_)
    pb.identification(remarks="IMS MPP: INQUIRY BY KEY FROM THE TERMINAL, REPLY ON THE I/O PCB, SWITCH TO THE NEXT TRANSACTION.")
    pb.environment([])
    pb.data_division()
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-GU", pic="X(04)", value="'GU  '"), Item(5, "WS-ISRT", pic="X(04)", value="'ISRT'"),
                               Item(5, "WS-CHNG", pic="X(04)", value="'CHNG'"), Item(5, "WS-DEST-NAME", pic="X(08)", value=f"'{s3}4'")])
    pb.item(Item(1, "WS-INPUT-MSG", children=[Item(5, "IN-LL", pic="S9(04)", usage="COMP"), Item(5, "IN-ZZ", pic="S9(04)", usage="COMP"),
                                             Item(5, "IN-TRANCODE", pic="X(08)"), Item(5, "IN-KEY", pic=f"X({c['klen']})"),
                                             Item(5, "IN-FUNCTION", pic="X(01)")]))
    pb.item(Item(1, "WS-OUTPUT-MSG", children=[Item(5, "OUT-LL", pic="S9(04)", usage="COMP", value="+90"), Item(5, "OUT-ZZ", pic="S9(04)", usage="COMP", value="ZERO"),
                                              Item(5, "OUT-TEXT", pic="X(86)")]))
    pb.item(Item(1, "WS-ROOT-SSA", children=[Item(5, "FILLER", pic="X(08)", value=f"'{s3}ROOT '"), Item(5, "FILLER", pic="X(01)", value="'('"),
                                            Item(5, "FILLER", pic="X(08)", value=f"'{key.replace('-', '')[:8]:<8}'"), Item(5, "FILLER", pic="X(02)", value="' ='"),
                                            Item(5, "SSA-ROOT-KEY", pic=f"X({c['klen']})"), Item(5, "FILLER", pic="X(01)", value="')'")]))
    pb.wrapper_01("WS-ROOT-AREA")
    pb.copy(f"{s3}SEGA")
    pb.copy(f"{s3}MSGT")
    pb.linkage_section()
    pb.copy("CMNIOPCB")
    pb.item(Item(1, "ALT-PCB", children=[Item(5, "ALT-DEST", pic="X(08)"), Item(5, "FILLER", pic="X(02)"), Item(5, "ALT-STATUS", pic="X(02)")]))
    pb.copy("CMNPCBM", replacing=[Replacing("tag", ":PCB:", pcb)])
    pb.procedure(using=["IO-PCB", "ALT-PCB", f"{pcb}-PCB"])
    pb.para("0000-MAIN")
    pb.dli("WS-GU", "GU", "IO-PCB", io_area="WS-INPUT-MSG")
    pb.perform("1000-EACH-MESSAGE", until="IO-STATUS NOT = SPACES", tests=["IO-STATUS"])
    pb.stmt("GOBACK.")
    pb.para("1000-EACH-MESSAGE")
    pb.move("IN-KEY", ["SSA-ROOT-KEY"])
    pb.dli("WS-GU", "GU", f"{pcb}-PCB", io_area=f"{ps}-ROOT-SEG", ssas=["WS-ROOT-SSA"])
    pb.if_open(f"{pcb}-OK", tests=[f"{pcb}-OK"])
    pb.string_([f"{ps}-{key} DELIMITED BY SIZE", "' '", f"{ps}-INSURED-NAME DELIMITED BY SIZE"], "OUT-TEXT", indent=4,
               fields=[f"{ps}-{key}", f"{ps}-INSURED-NAME"])
    pb.else_()
    pb.move(f"{pw}-MSG-TEXT (1)", ["OUT-TEXT"], indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.dli("WS-ISRT", "ISRT", "IO-PCB", io_area="WS-OUTPUT-MSG")
    pb.if_open("IN-FUNCTION = 'N'", tests=["IN-FUNCTION"], lits=[("N", "IN-FUNCTION")])
    pb.dli("WS-CHNG", "CHNG", "ALT-PCB", io_area="WS-DEST-NAME", indent=4)
    pb.dli("WS-ISRT", "ISRT", "ALT-PCB", io_area="WS-OUTPUT-MSG", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.dli("WS-GU", "GU", "IO-PCB", io_area="WS-INPUT-MSG")
    pb.end_stmt()
    return pb


# --------------------------------------------------------------------------
# CICS
# --------------------------------------------------------------------------

def onl01(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    pm, pa = p["master"], p["comm"]
    mapset, map1 = f"{s3}MAPS", f"{s3}MAP1"
    pb = new(estate, f"{s3}ONL01", sys_)
    pb.identification(remarks=f"CICS: {sys_} MAINTENANCE SCREEN {map1}.")
    pb.environment([])
    pb.data_division()
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-RESP", pic="S9(08)", usage="COMP"), Item(5, "WS-KEY", pic=f"X({c['klen']})"),
                               Item(5, "WS-TSQ-NAME", pic="X(08)", value=f"'{s3}TSQ01'"),
                               Item(5, "WS-NEXT-PGM", pic="X(08)", value=f"'{s3}ONL02'")])
    pb.copy(mapset)
    pb.wrapper_01(f"{pm}-MASTER-RECORD")
    pb.copy(f"{s3}MASTR")
    pb.copy(f"{s3}COMMA")
    pb.copy("DFHAID", expect="system")
    pb.linkage_section()
    pb.item(Item(1, "DFHCOMMAREA", pic="X(200)"))
    pb.procedure()
    pb.para("0000-MAIN")
    pb.if_open("EIBCALEN = 0", tests=["EIBCALEN"])
    pb.cics("SEND", f"MAP('{map1}') MAPSET('{mapset}') MAPONLY ERASE", "map", f"{mapset}.{map1}", "out", indent=4)
    pb.cics("RETURN", f"TRANSID('{s3}1') COMMAREA({pa}-COMMAREA) LENGTH(100)", "transid", f"{s3}1", None, indent=4,
            fields_out=[f"{pa}-COMMAREA"])
    pb.end_if()
    pb.end_stmt()
    pb.cics("RECEIVE", f"MAP('{map1}') MAPSET('{mapset}') INTO({map1}I) RESP(WS-RESP)", "map", f"{mapset}.{map1}", "in",
            fields_in=[f"{map1}I", "WS-RESP"])
    pb.move("KEYINI", ["WS-KEY", f"{pa}-{key}"])
    pb.cics("READ", f"FILE('{s3}MAST') INTO({pm}-MASTER-RECORD) RIDFLD(WS-KEY) RESP(WS-RESP)", "file", f"{s3}MAST", "in",
            fields_in=[f"{pm}-MASTER-RECORD"], fields_out=["WS-KEY"])
    pb.if_open("WS-RESP = DFHRESP(NORMAL)", tests=["WS-RESP"])
    pb.move(f"{pm}-STATUS", ["STATUSO"], indent=4)
    pb.move(f"{pm}-LAST-NAME", ["NAMEO"], indent=4)
    pb.move(f"{pm}-{c['amts'][0]}", ["AMOUNTO"], indent=4)
    pb.move_lit("I", [f"{pa}-FUNCTION"], indent=4)
    pb.cics("LINK", f"PROGRAM('{s3}ONL02') COMMAREA({pa}-COMMAREA) LENGTH(LENGTH OF {pa}-COMMAREA)", "program", f"{s3}ONL02", None,
            indent=4, fields_out=[f"{pa}-COMMAREA"])
    pb.fact("call", pb.em.n - 1, kind="cics_link", target=f"{s3}ONL02", using=[f"{pa}-COMMAREA"], returning=None, exists=True)
    pb.move(f"{pa}-MESSAGE", ["ERRMSGO"], indent=4)
    pb.else_()
    pb.move_lit(f"{key.replace('-', ' ')} NOT FOUND ON THE MASTER FILE", ["ERRMSGO"], indent=4)
    pb.cics("WRITEQ", f"TS QUEUE(WS-TSQ-NAME) FROM(WS-KEY) LENGTH(12) MAIN", "tsq", f"{s3}TSQ01", "out", indent=4, fields_out=["WS-KEY"])
    pb.end_if()
    pb.end_stmt()
    pb.cics("SEND", f"MAP('{map1}') MAPSET('{mapset}') FROM({map1}O) ERASE", "map", f"{mapset}.{map1}", "out", fields_out=[f"{map1}O"])
    if sys_ == "CLAIMS":
        pb.cics("START", f"TRANSID('{s3}2') INTERVAL(0)", "transid", f"{s3}2", None)
        pb.fact("call", pb.em.n - 1, kind="cics_start", target=f"{s3}2", using=[], returning=None, exists=True)
    if sys_ == "BILLING":
        pb.cics("XCTL", f"PROGRAM(WS-NEXT-PGM) COMMAREA({pa}-COMMAREA) LENGTH(100)", "program", f"{s3}ONL02", None,
                fields_out=[f"{pa}-COMMAREA"])
        pb.fact("call", pb.em.n - 1, kind="cics_xctl", target=None, via_var="WS-NEXT-PGM", using=[f"{pa}-COMMAREA"],
                resolved=[f"{s3}ONL02"], resolution="value_clause")
    pb.cics("RETURN", f"TRANSID('{s3}1') COMMAREA({pa}-COMMAREA) LENGTH(100)", "transid", f"{s3}1", None, fields_out=[f"{pa}-COMMAREA"])
    pb.fact("call", pb.em.n - 1, kind="cics_return", target=f"{s3}1", using=[], returning=None, exists=True)
    pb.stmt("GOBACK.")
    return pb


def onl02(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    pa = p["comm"]
    agent = c["tables"][3]
    tbl = c["tables"][0]
    pb = new(estate, f"{s3}ONL02", sys_)
    pb.identification(remarks="CICS: LINKED WITH THE COMMAREA; READS THE AGENT TABLE AND LOGS TO THE TD QUEUE.")
    pb.environment([])
    pb.data_division()
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-LOG-REC", pic="X(80)"), Item(5, "WS-AGENT-ID", pic="X(06)")])
    pb.copy("CMNSQLW")
    pb.copy("SQLCA", sql_include=True, expect="system")
    pb.copy(agent.dclgen, sql_include=True)
    pb.linkage_section()
    pb.copy(f"{s3}COMMA", replacing=[Replacing("name", f"{pa}-COMMAREA", "DFHCOMMAREA")])
    pb.procedure()
    pb.para("0000-MAIN")
    pb.exec_sql([f"SELECT AGENT_ID, STATUS_CD", "INTO :WS-AGENT-ID, :STATUS-CD", f"FROM {tbl.qualified}", f"WHERE {tbl.key} = :{pa}-{key}"],
                "SELECT", [tbl.qualified], cols=[{"tbl": tbl.name, "col": "AGENT_ID", "host": "WS-AGENT-ID", "mode": "read"},
                                                  {"tbl": tbl.name, "col": "STATUS_CD", "host": "STATUS-CD", "mode": "read"},
                                                  {"tbl": tbl.name, "col": tbl.key, "host": f"{pa}-{key}", "mode": "predicate"}])
    pb.if_open("SQLCODE = 0", tests=["SQLCODE"])
    pb.exec_sql(["SELECT AGENT_NAME, BRANCH_CD", "INTO :AGENT-NAME, :BRANCH-CD", f"FROM {agent.qualified}", "WHERE AGENT_ID = :WS-AGENT-ID"],
                "SELECT", [agent.qualified], cols=[{"tbl": agent.name, "col": "AGENT_NAME", "host": "AGENT-NAME", "mode": "read"},
                                                    {"tbl": agent.name, "col": "BRANCH_CD", "host": "BRANCH-CD", "mode": "read"},
                                                    {"tbl": agent.name, "col": "AGENT_ID", "host": "WS-AGENT-ID", "mode": "predicate"}], indent=4)
    pb.move("AGENT-NAME-TEXT", [f"{pa}-MESSAGE"], indent=4)
    pb.move_lit("00", [f"{pa}-RETURN-CODE"], indent=4)
    pb.else_()
    pb.move_lit("08", [f"{pa}-RETURN-CODE"], indent=4)
    pb.move_lit("AGENT NOT FOUND FOR THIS RECORD", [f"{pa}-MESSAGE"], indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.string_([f"{pa}-{key} DELIMITED BY SIZE", f"{pa}-RETURN-CODE DELIMITED BY SIZE"], "WS-LOG-REC", fields=[f"{pa}-{key}", f"{pa}-RETURN-CODE"])
    pb.cics("WRITEQ", f"TD QUEUE('{s3}Q') FROM(WS-LOG-REC) LENGTH(80)", "tdq", f"{s3}Q", "out", fields_out=["WS-LOG-REC"])
    pb.cics("RETURN", "", None)
    pb.stmt("GOBACK.")
    return pb


# --------------------------------------------------------------------------
# subroutines
# --------------------------------------------------------------------------

def edit(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    pm, pa = p["master"], p["comm"]
    pb = new(estate, f"{s3}EDIT", sys_, tag=(s3 + "E0") if sys_ == "POLICY" else "")
    pb.identification(remarks="EDIT SUBROUTINE: VALIDATES A MASTER RECORD AND RETURNS A CODE IN THE COMMAREA.")
    pb.environment([])
    pb.data_division()
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-MIN-AMT", pic="S9(09)V99", usage="COMP-3", value="+1.00"),
                               Item(5, "WS-MAX-AMT", pic="S9(09)V99", usage="COMP-3", value="+999999.99")])
    pb.copy(f"{s3}MSGT")
    pb.copy("CMNDATEA", expect="not_found_then_ok")
    pb.item(Item(1, "WS-EDIT-SW", pic="X(01)", value="'N'", conds=[("WS-EDIT-FAILED", ["'Y'"]), ("WS-EDIT-PASSED", ["'N'"])]))
    pb.linkage_section()
    pb.copy(f"{s3}COMMA", replacing=[Replacing("leading", f"{pa}-", "LK-")])
    pb.wrapper_01("LK-MASTER-RECORD")
    pb.copy(f"{s3}MASTR")
    pb.procedure(using=["LK-COMMAREA", "LK-MASTER-RECORD"])
    pb.para("0000-MAIN")
    pb.move_lit("00", ["LK-RETURN-CODE"])
    pb.move("SPACES", ["LK-MESSAGE"])
    pb.set_true("WS-EDIT-PASSED")
    pb.perform("1000-EDIT-KEY")
    pb.perform("2000-EDIT-STATUS")
    pb.perform("3000-EDIT-AMOUNT")
    pb.perform("4000-EDIT-DATES")
    pb.if_open("WS-EDIT-FAILED", tests=["WS-EDIT-FAILED"])
    pb.move_lit("08", ["LK-RETURN-CODE"], indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.stmt("GOBACK.")
    pb.para("1000-EDIT-KEY")
    pb.if_open(f"{pm}-{key} = SPACES OR {pm}-{key} = LOW-VALUES", tests=[f"{pm}-{key}"])
    pb.move(f"{p['work']}-MSG-TEXT (1)", ["LK-MESSAGE"], indent=4)
    pb.set_true("WS-EDIT-FAILED", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.para("2000-EDIT-STATUS")
    pb.evaluate_open("TRUE")
    pb.when(f"{pm}-{c['status'][0][0]}")
    pb.stmt("CONTINUE", 8)
    pb.when(f"{pm}-{c['status'][1][0]}")
    pb.stmt("CONTINUE", 8)
    pb.when(f"{pm}-{c['status'][2][0]}")
    pb.if_open("LK-FUNCTION = 'C'", tests=["LK-FUNCTION"], lits=[("C", "LK-FUNCTION")], indent=8)
    pb.move(f"{p['work']}-MSG-TEXT (2)", ["LK-MESSAGE"], indent=12)
    pb.set_true("WS-EDIT-FAILED", indent=12)
    pb.end_if(indent=8)
    pb.when(f"{pm}-STATUS = 'PN'", lit="PN")
    pb.stmt("CONTINUE", 8)
    pb.when("OTHER")
    pb.move(f"{p['work']}-MSG-TEXT (2)", ["LK-MESSAGE"], indent=8)
    pb.set_true("WS-EDIT-FAILED", indent=8)
    pb.end_evaluate()
    pb.end_stmt()
    pb.para("3000-EDIT-AMOUNT")
    pb.if_open(f"{pm}-{c['amts'][0]} < WS-MIN-AMT OR {pm}-{c['amts'][0]} > WS-MAX-AMT", tests=[f"{pm}-{c['amts'][0]}", "WS-MIN-AMT", "WS-MAX-AMT"])
    pb.move(f"{p['work']}-MSG-TEXT (4)", ["LK-MESSAGE"], indent=4)
    pb.set_true("WS-EDIT-FAILED", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.para("4000-EDIT-DATES")
    pb.move(f"{pm}-{c['dates'][0]}", ["START-DATE"])
    pb.move(f"{pm}-{c['dates'][1]}", ["END-DATE"])
    pb.call_static("CMNDATE", using=["CMN-DATE-AREA"])
    pb.if_open("DATE-RANGE-ERR", tests=["DATE-RANGE-ERR"])
    pb.move(f"{p['work']}-MSG-TEXT (3)", ["LK-MESSAGE"], indent=4)
    pb.set_true("WS-EDIT-FAILED", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.para("9000-ENTRY-POINT")
    pb.entry(f"{s3}EDIT2", using=["LK-MASTER-RECORD"])
    pb.perform("1000-EDIT-KEY")
    pb.stmt("GOBACK.")
    return pb


def rate(estate, sys_: str, n: int) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p = c["s3"], c["p"]
    pm = p["master"]
    pb = new(estate, f"{s3}RATE{n}", sys_)
    pb.identification(remarks=f"RATING ROUTINE {n}: RECOMPUTES THE MAIN AMOUNT FROM THE COVERAGE ITEMS.")
    pb.environment([])
    pb.data_division()
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-FACTOR", pic="S9(03)V9(04)", usage="COMP-3", value=f"+1.0{n}25"),
                               Item(5, "WS-I", pic="S9(04)", usage="COMP")])
    pb.linkage_section()
    pb.wrapper_01("LK-MASTER-RECORD")
    pb.copy(f"{s3}MASTR")
    pb.item(Item(1, "LK-RESULT-AMT", pic="S9(11)V99", usage="COMP-3"))
    pb.procedure(using=["LK-MASTER-RECORD", "LK-RESULT-AMT"])
    pb.para("0000-RATE")
    pb.move("ZERO", ["LK-RESULT-AMT"])
    pb.perform("1000-EACH-ITEM", varying="WS-I FROM 1 BY 1", until=f"WS-I > {pm}-ITEM-COUNT", tests=["WS-I", f"{pm}-ITEM-COUNT"])
    pb.compute(f"{pm}-{c['amts'][0]}", "LK-RESULT-AMT * WS-FACTOR", reads=["LK-RESULT-AMT", "WS-FACTOR"])
    pb.stmt("GOBACK.")
    pb.para("1000-EACH-ITEM")
    pb.add_to(f"{pm}-ITEM-PREM (WS-I)", "LK-RESULT-AMT")
    pb.end_stmt()
    return pb


def sub01(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p = c["s3"], c["p"]
    pb = new(estate, f"{s3}SUB01", sys_)
    pb.identification(remarks="A FUNCTION-STYLE SUBROUTINE: RETURNING A CODE. CALLED BY THE ONLINE PROGRAMS' TWIN.")
    pb.environment([])
    pb.data_division()
    pb.working_storage()
    ws_constants(pb, pb.name)
    pb.linkage_section()
    pb.item(Item(1, "LK-KEY", pic=f"X({c['klen']})"))
    pb.item(Item(1, "LK-RC", pic="S9(04)", usage="COMP"))
    pb.procedure(using=["LK-KEY"], returning="LK-RC")
    pb.para("0000-CHECK")
    pb.if_open("LK-KEY = SPACES", tests=["LK-KEY"])
    pb.move_lit("+8", ["LK-RC"], indent=4, quote=False)
    pb.else_()
    pb.move_lit("0", ["LK-RC"], indent=4, quote=False)
    pb.end_if()
    pb.end_stmt()
    pb.stmt("GOBACK.")
    return pb


def old01(estate, sys_: str) -> ProgramBuilder:
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    pm, pw = p["master"], p["work"]
    pb = new(estate, f"{s3}OLD01", sys_)
    pb.identification(remarks="RETIRED: NOTHING RUNS OR CALLS THIS PROGRAM ANY MORE.")
    pb.environment([{"name": "MASTER-FILE", "dd": f"{s3}MAST", "org": "INDEXED", "access": "SEQUENTIAL", "key": f"{pm}-{key}"}])
    pb.data_division()
    pb.file_section()
    pb.fd("MASTER-FILE", f"{pm}-MASTER-RECORD", copybook=f"{s3}MASTR")
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-RC", pic="S9(04)", usage="COMP")])
    pb.copy(f"{s3}WORKA")
    pb.item(Item(1, "WS-KEY-COPY", pic=f"X({c['klen']})"))
    pb.procedure()
    pb.para("0000-MAIN")
    pb.open_([("INPUT", ["MASTER-FILE"])])
    pb.end_stmt()
    pb.perform("1000-COUNT", until=f"{pw}-EOF", tests=[f"{pw}-EOF"])
    pb.close(["MASTER-FILE"])
    pb.display(["'COUNTED '", f"{pw}-READ-CNT"], fields=[f"{pw}-READ-CNT"])
    pb.stmt("GOBACK.")
    pb.para("1000-COUNT")
    pb.read("MASTER-FILE", next_=True, at_end=f"SET {pw}-EOF TO TRUE")
    pb.end_stmt()
    pb.move(f"{pm}-{key}", ["WS-KEY-COPY"])
    pb.call_static(f"{s3}SUB01", using=["WS-KEY-COPY"], returning="WS-RC")
    pb.stmt("*    CALL 'OLDRATER' USING WS-KEY-COPY.") if False else pb.comment(f"    CALL 'OLDRATER' USING WS-KEY-COPY.")
    pb.add_to("1", f"{pw}-READ-CNT")
    pb.end_stmt()
    return pb


# --------------------------------------------------------------------------
# SHARED programs
# --------------------------------------------------------------------------

def build_shared(estate) -> None:
    from synth_estate import lib
    sys_ = "SHARED"
    # CMNDATE: date routine
    pb = ProgramBuilder("CMNDATE", sys_, lib(sys_, "src"), estate.books)
    pb.identification(remarks="DATE ROUTINE: VALIDATES START/END DATES AND RETURNS THE DAYS BETWEEN.")
    pb.environment([])
    pb.data_division()
    pb.working_storage()
    ws_constants(pb, "CMNDATE", [Item(5, "WS-START-INT", pic="S9(09)", usage="COMP"), Item(5, "WS-END-INT", pic="S9(09)", usage="COMP")])
    pb.linkage_section()
    pb.copy("CMNDATEA", expect="not_found_then_ok")
    pb.procedure(using=["CMN-DATE-AREA"])
    pb.para("0000-MAIN")
    pb.move_lit("0", ["DATE-RC"], quote=False)
    pb.if_open("START-DATE NOT NUMERIC OR END-DATE NOT NUMERIC", tests=["START-DATE", "END-DATE"])
    pb.move_lit("1", ["DATE-RC"], indent=4, quote=False)
    pb.stmt("GOBACK", 4)
    pb.end_if()
    pb.end_stmt()
    pb.compute("WS-START-INT", "FUNCTION INTEGER-OF-DATE(START-DATE)", reads=["START-DATE"])
    pb.compute("WS-END-INT", "FUNCTION INTEGER-OF-DATE(END-DATE)", reads=["END-DATE"])
    pb.compute("DAYS-BETWEEN", "WS-END-INT - WS-START-INT", reads=["WS-END-INT", "WS-START-INT"])
    pb.if_open("DAYS-BETWEEN < 0", tests=["DAYS-BETWEEN"])
    pb.move_lit("2", ["DATE-RC"], indent=4, quote=False)
    pb.end_if()
    pb.end_stmt()
    pb.stmt("GOBACK.")
    estate.add_program(pb, role="D4 copier")
    # CMNERR: error handler
    pb = ProgramBuilder("CMNERR", sys_, lib(sys_, "src"), estate.books)
    pb.identification(remarks="ERROR HANDLER: DISPLAYS THE ERROR AREA; A FATAL ONE ABENDS.")
    pb.environment([])
    pb.data_division()
    pb.working_storage()
    ws_constants(pb, "CMNERR")
    pb.copy("CMNABNDA")
    pb.linkage_section()
    pb.copy("CMNERRA")
    pb.procedure(using=["CMN-ERROR-AREA"])
    pb.para("0000-MAIN")
    pb.display(["'*** ERROR IN '", "ERR-PROGRAM", "' AT '", "ERR-PARAGRAPH"], fields=["ERR-PROGRAM", "ERR-PARAGRAPH"])
    pb.display(["'*** CODE '", "ERR-CODE", "' '", "ERR-TEXT"], fields=["ERR-CODE", "ERR-TEXT"])
    pb.if_open("ERR-SQLCODE NOT = 0", tests=["ERR-SQLCODE"])
    pb.display(["'*** SQLCODE '", "ERR-SQLCODE"], fields=["ERR-SQLCODE"], indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.if_open("ERR-FATAL", tests=["ERR-FATAL"])
    pb.move("ERR-PROGRAM", ["ABEND-PROGRAM"], indent=4)
    pb.move_lit("U100", ["ABEND-CODE"], indent=4)
    pb.call_static("CMNABEND", using=["ABEND-CODE"], indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.stmt("GOBACK.")
    estate.add_program(pb)
    # CMNABEND
    pb = ProgramBuilder("CMNABEND", sys_, lib(sys_, "src"), estate.books)
    pb.identification(remarks="ABEND ROUTINE: WRITES THE CODE AND CALLS THE SYSTEM ABEND.")
    pb.environment([])
    pb.data_division()
    pb.working_storage()
    ws_constants(pb, "CMNABEND", [Item(5, "WS-ABEND-RC", pic="S9(04)", usage="COMP", value="+100")])
    pb.linkage_section()
    pb.item(Item(1, "LK-ABEND-CODE", pic="X(04)"))
    pb.procedure(using=["LK-ABEND-CODE"])
    pb.para("0000-MAIN")
    pb.display(["'*** ABENDING WITH '", "LK-ABEND-CODE"], fields=["LK-ABEND-CODE"])
    pb.call_static("ILBOABN0", using=["WS-ABEND-RC"], exists=False)
    pb.stmt("GOBACK.")
    estate.add_program(pb)
    # CMNCUST1: customer lookup, the START verb in a procedure copybook (D4 procedure)
    pb = ProgramBuilder("CMNCUST1", sys_, lib(sys_, "src"), estate.books)
    pb.identification(remarks="CUSTOMER LOOKUP ON THE SHARED KSDS. THE POSITIONING PARAGRAPHS ARE A PROCEDURE COPYBOOK.")
    pb.environment([{"name": "CUSTFILE", "dd": "CUSTFILE", "org": "INDEXED", "access": "DYNAMIC", "key": "CF-CUST-KEY"}])
    pb.data_division()
    pb.file_section()
    pb.line(pb.AREA_A, "FD  CUSTFILE")
    pb.line(pb.AREA_B, "LABEL RECORDS ARE STANDARD.")
    pb.fd_records["CUSTFILE"] = "CUST-RECORD"
    pb.selects[0]["fd_record"] = "CUST-RECORD"
    pb.copy("CMNCUSTR")
    pb.working_storage()
    ws_constants(pb, "CMNCUST1", [Item(5, "WS-CUST-KEY", pic="X(12)"), Item(5, "WS-CUST-NO", pic="X(10)"), Item(5, "WS-CUST-FOUND-SW", pic="X(01)")])
    pb.linkage_section()
    pb.item(Item(1, "LK-CUST-RECORD", pic="X(174)"))
    pb.item(Item(1, "LK-FOUND-SW", pic="X(01)"))
    pb.procedure(using=["LK-CUST-RECORD", "LK-FOUND-SW"])
    pb.para("0000-MAIN")
    pb.open_([("INPUT", ["CUSTFILE"])])
    pb.end_stmt()
    pb.move("LK-CUST-RECORD", ["WS-CUST-KEY"])
    pb.perform("A-100-BEGIN")
    pb.move("WS-CUST-FOUND-SW", ["LK-FOUND-SW"])
    pb.move("CUST-RECORD", ["LK-CUST-RECORD"])
    pb.close(["CUSTFILE"])
    pb.end_stmt()
    pb.stmt("GOBACK.")
    n = pb.copy("CMNCUSTP", prefix_text="A-100-BEGIN SECTION.", expect="not_found_then_ok")
    pb.sections.append({"name": "A-100-BEGIN", "line": n})
    pb.fact("section", n, name="A-100-BEGIN")
    estate.add_program(pb, role="D4 copier (procedure)")
    # CMNSQLER
    pb = ProgramBuilder("CMNSQLER", sys_, lib(sys_, "src"), estate.books)
    pb.identification(remarks="DB2 ERROR HANDLER: FORMATS THE SQLCA WITH DSNTIAR.")
    pb.environment([])
    pb.data_division()
    pb.working_storage()
    ws_constants(pb, "CMNSQLER", [Item(5, "WS-MSG-LEN", pic="S9(09)", usage="COMP", value="+960"),
                                  Item(5, "WS-LRECL", pic="S9(09)", usage="COMP", value="+80")])
    pb.item(Item(1, "WS-ERROR-MESSAGE", children=[Item(5, "WS-EM-LEN", pic="S9(04)", usage="COMP", value="+960"),
                                                 Item(5, "WS-EM-TEXT", pic="X(80)", occurs=12)]))
    pb.linkage_section()
    pb.copy("CMNERRA")
    pb.copy("SQLCA", sql_include=True, expect="system")
    pb.procedure(using=["CMN-ERROR-AREA", "SQLCA"])
    pb.uses["sql"] = False
    pb.para("0000-MAIN")
    pb.call_static("DSNTIAR", using=["SQLCA", "WS-ERROR-MESSAGE", "WS-LRECL"], exists=False)
    pb.move("WS-EM-TEXT (1)", ["ERR-TEXT"])
    pb.set_true("ERR-FATAL")
    pb.call_static("CMNERR", using=["CMN-ERROR-AREA"])
    pb.stmt("GOBACK.")
    estate.add_program(pb)


def cmnutil(estate, sys_: str) -> ProgramBuilder:
    """The same program name in two systems with different content (ambiguous)."""
    pb = new(estate, "CMNUTIL", sys_)
    pb.identification(remarks=f"{sys_} COPY OF THE CUSTOMER REPORT UTILITY - THE TWO COPIES DIFFER.")
    pb.environment([{"name": "CUSTFILE", "dd": "CUSTFILE", "org": "INDEXED", "access": "SEQUENTIAL", "key": "CF-CUST-KEY"},
                    {"name": "CUST-REPORT", "dd": "CUSTRPT"}])
    pb.data_division()
    pb.file_section()
    pb.line(pb.AREA_A, "FD  CUSTFILE")
    pb.line(pb.AREA_B, "LABEL RECORDS ARE STANDARD.")
    pb.fd_records["CUSTFILE"] = "CUST-RECORD"
    pb.selects[0]["fd_record"] = "CUST-RECORD"
    pb.copy("CMNCUSTR")
    pb.fd("CUST-REPORT", "CUST-LINE", root=Item(1, "CUST-LINE", pic="X(133)"))
    pb.working_storage()
    ws_constants(pb, "CMNUTIL", [Item(5, "WS-EOF", pic="X(01)", value="'N'", conds=[("END-OF-CUST", ["'Y'"])])])
    pb.copy("STDHDR", expect="chosen")
    if sys_ == "CLAIMS":
        pb.item(Item(1, "WS-CLAIMS-ONLY", pic="X(20)", value="'CLAIMS VERSION'"))
    pb.procedure()
    pb.para("0000-MAIN")
    pb.open_([("INPUT", ["CUSTFILE"]), ("OUTPUT", ["CUST-REPORT"])])
    pb.end_stmt()
    pb.move("WS-PROGRAM-NAME", ["HDR-PROGRAM"])
    pb.perform("1000-LIST", until="END-OF-CUST", tests=["END-OF-CUST"])
    pb.close(["CUSTFILE", "CUST-REPORT"])
    pb.stmt("GOBACK.")
    pb.para("1000-LIST")
    pb.read("CUSTFILE", next_=True, at_end="SET END-OF-CUST TO TRUE")
    pb.end_stmt()
    pb.if_open("CF-CUST-ACTIVE", tests=["CF-CUST-ACTIVE"])
    pb.write("CUST-LINE", "CUST-REPORT", frm="CUST-RECORD", indent=4)
    pb.end_if()
    pb.end_stmt()
    return pb


# --------------------------------------------------------------------------
# all of them
# --------------------------------------------------------------------------

def gen_batch(estate, sys_: str, k: int) -> ProgramBuilder:
    """A plain extract program of the scale family: reads the GEN1R file, writes the GEN2R file,
    moves the fields it can, tests a flag, counts, calls the date routine on odd numbers."""
    c = S(estate, sys_)
    s3, p, key = c["s3"], c["p"], c["key"]
    pw = p["work"]
    m1 = estate.record_meta[f"{s3}GEN1R"]
    m2 = estate.record_meta[f"{s3}GEN2R"]
    rng = estate.rng
    pb = new(estate, f"{s3}GEN{k:02d}", sys_, seq=(k % 3 == 0), tag=(s3 + f"G{k % 10}") if k % 4 == 1 else "")
    pb.identification(remarks=f"GENERIC EXTRACT {k}: {s3}GEN1R IN, {s3}GEN2R OUT.")
    pb.environment([{"name": "GEN-IN", "dd": f"{s3}GIN"}, {"name": "GEN-OUT", "dd": f"{s3}GOUT"}])
    pb.data_division()
    pb.file_section()
    pb.line(pb.AREA_A, "FD  GEN-IN")
    pb.line(pb.AREA_B, "LABEL RECORDS ARE STANDARD.")
    pb.fd_records["GEN-IN"] = f"{p['gen1']}-RECORD"
    pb.selects[0]["fd_record"] = f"{p['gen1']}-RECORD"
    pb.copy(f"{s3}GEN1R")
    pb.line(pb.AREA_A, "FD  GEN-OUT")
    pb.line(pb.AREA_B, "LABEL RECORDS ARE STANDARD.")
    pb.fd_records["GEN-OUT"] = f"{p['gen2']}-RECORD"
    pb.selects[1]["fd_record"] = f"{p['gen2']}-RECORD"
    pb.copy(f"{s3}GEN2R", quoted=(k % 2 == 0))
    pb.working_storage()
    ws_constants(pb, pb.name, [Item(5, "WS-CYCLE", pic="9(02)", value=f"{k:02d}"), Item(5, "WS-SEL-CNT", pic="S9(07)", usage="COMP-3", value="ZERO")])
    pb.copy(f"{s3}WORKA")
    if k % 2:
        pb.copy("CMNDATEA", expect="not_found_then_ok")
    pb.procedure()
    pb.para("0000-MAIN")
    pb.open_([("INPUT", ["GEN-IN"]), ("OUTPUT", ["GEN-OUT"])])
    pb.end_stmt()
    pb.perform("1000-READ")
    pb.perform("2000-SELECT", until=f"{pw}-EOF", tests=[f"{pw}-EOF"])
    pb.close(["GEN-IN", "GEN-OUT"])
    pb.display(["'SELECTED '", "WS-SEL-CNT", "' OF '", f"{pw}-READ-CNT"], fields=["WS-SEL-CNT", f"{pw}-READ-CNT"])
    pb.stmt("GOBACK.")
    pb.para("1000-READ")
    pb.read("GEN-IN", at_end=f"SET {pw}-EOF TO TRUE")
    pb.end_stmt()
    pb.if_open(f"NOT {pw}-EOF", tests=[f"{pw}-EOF"])
    pb.add_to("1", f"{pw}-READ-CNT", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.para("2000-SELECT")
    pb.initialize(f"{p['gen2']}-RECORD")
    pb.move(m1.key, [m2.key])
    pairs = [(a, b) for a in m1.alnums for b in m2.alnums]
    rng.shuffle(pairs)
    for (a, _la), (b, _lb) in pairs[:3]:
        pb.move(a, [b])
    if m1.amounts and m2.amounts:
        pb.compute(m2.amounts[0], f"{m1.amounts[0]} * 1.{k:02d}", reads=[m1.amounts[0]])
    if m1.dates and k % 2:
        pb.move(m1.dates[0], ["START-DATE"])
        pb.move(m1.dates[0], ["END-DATE"])
        pb.call_static("CMNDATE", using=["CMN-DATE-AREA"])
    cond_field = (m1.alnums[0][0] if m1.alnums else m1.key)
    pb.if_open(f"{cond_field} = 'Z'", tests=[cond_field], lits=[("Z", cond_field)])
    pb.move_lit(f"G{k:02d}", [f"{pw}-RETURN-CD"], indent=4)
    pb.else_()
    pb.write(f"{p['gen2']}-RECORD", "GEN-OUT", indent=4)
    pb.add_to("1", "WS-SEL-CNT", indent=4)
    pb.end_if()
    pb.end_stmt()
    pb.perform("1000-READ")
    pb.end_stmt()
    return pb


def build_all(estate) -> None:
    from synth_estate import SYSTEMS
    for sys_ in SYSTEMS:
        for k in range(1, estate.scale + 1):
            estate.add_program(gen_batch(estate, sys_, k), role="scale")
        estate.add_program(upd01(estate, sys_), role="flagship")
        estate.add_program(ext01(estate, sys_), status="partial", why="STDHDR chosen among several", role="D1 chosen")
        estate.add_program(rpt01(estate, sys_), status="partial", why="STDHDR chosen among several", role="D1 chosen")
        estate.add_program(his01(estate, sys_), role="D4 copier")
        estate.add_program(db201(estate, sys_), role="db2")
        estate.add_program(db202(estate, sys_), role="db2")
        estate.add_program(ims01(estate, sys_), role="ims batch")
        estate.add_program(ims02(estate, sys_), role="ims bmp")
        estate.add_program(onl03(estate, sys_), role="ims mpp")
        estate.add_program(onl01(estate, sys_), status="partial", why="COPY DFHAID NOT FOUND (an IBM-supplied copybook the estate does not hold)", role="cics")
        estate.add_program(onl02(estate, sys_), role="cics link")
        estate.add_program(edit(estate, sys_), role="sub + ENTRY")
        estate.add_program(rate(estate, sys_, 1), role="sub (dynamic target)")
        if sys_ == "POLICY":
            estate.add_program(rate(estate, sys_, 2), role="sub (dynamic target, POLICY only)")
        estate.add_program(sub01(estate, sys_), role="sub RETURNING")
        estate.add_program(old01(estate, sys_), role="dead")
        if sys_ in ("POLICY", "CLAIMS"):
            estate.add_program(cmnutil(estate, sys_), status="partial", why="STDHDR chosen (authoritative / first found)", role="ambiguous")
    build_policy_specials(estate)
    build_shared(estate)
    OPENS.update(estate.opens_of)
