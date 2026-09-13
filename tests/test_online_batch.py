"""
IMS / DB2 / CICS / MQ / MFS facts that were missing (coverage audit):

  DL/I function codes held in VALUE fields, a parm-count first argument,
  CHNG message switches, EXEC DLI operands, ENTRY 'DLITCBL' USING, PCB
  position resolved through the PSB (I/O PCB first, LIST=NO skipped),
  GSAM PCB -> JCL DD direction, DBD FIELD/XDFLD/DATASET, stage-1 inside
  JCL SYSIN with SPA/INQUIRY, CSD TDQUEUE/DB2ENTRY/URIMAP, EXEC CICS facts
  persisted (maps, START/RETURN TRANSID, queues, COMMAREA), online readers
  of a VSAM file in `dataset`, DCLGEN DECLARE TABLE + SELECT * INTO :group,
  qualified vs unqualified table names, FETCH ROWSET, EXEC SQL CALL, MQ
  queue names, MFS MFLD without LTH= and ATTR=(YES,n).
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, cobol, ims, query, screens, txn  # noqa: E402

FILES = {
    "PSBLIB/CLMPSB.psb": (
        "         PCB   TYPE=DB,DBDNAME=POLDBD,PROCOPT=G,KEYLEN=12\n"
        "         SENSEG NAME=POLICY,PARENT=0\n"
        "         PCB   TYPE=DB,DBDNAME=CLMDBD,PROCOPT=A,PROCSEQ=CLMXNDX\n"
        "         SENSEG NAME=CLAIM,PARENT=0\n"
        "         PSBGEN LANG=COBOL,PSBNAME=CLMPSB,CMPAT=YES\n"),
    "PSBLIB/EXTPSB.psb": (
        "         PCB   TYPE=DB,DBDNAME=CLMDBD,PROCOPT=G,KEYLEN=20\n"
        "         SENSEG NAME=CLAIM,PARENT=0\n"
        "         PCB   TYPE=GSAM,DBDNAME=EXTGSAM,PROCOPT=LS\n"
        "         PSBGEN LANG=COBOL,PSBNAME=EXTPSB\n"),
    "PSBLIB/MIXPSB.psb": (
        "         PCB   TYPE=DB,DBDNAME=POLDBD,PROCOPT=G,KEYLEN=12,LIST=NO,PCBNAME=POLAIB\n"
        "         SENSEG NAME=POLICY,PARENT=0\n"
        "         PCB   TYPE=DB,DBDNAME=CLMDBD,PROCOPT=A,KEYLEN=20\n"
        "         SENSEG NAME=CLAIM,PARENT=0\n"
        "         PSBGEN LANG=COBOL,PSBNAME=MIXPSB,CMPAT=YES\n"),
    "DBDLIB/CLMDBD.dbd": (
        "         DBD   NAME=CLMDBD,ACCESS=HIDAM\n"
        "         SEGM  NAME=CLAIM,PARENT=0,BYTES=120\n"
        "         FIELD NAME=(CLMNO,SEQ,U),BYTES=10,START=1\n"
        "         FIELD NAME=CLMSTAT,BYTES=2,START=25\n"
        "         LCHILD NAME=(CLMXSEG,CLMXNDX),PTR=INDX\n"
        "         XDFLD NAME=CLMXSTAT,SRCH=CLMSTAT\n"
        "         DBDGEN\n"),
    "DBDLIB/EXTGSAM.dbd": (
        "EXTGSAM  DBD   NAME=EXTGSAM,ACCESS=(GSAM,BSAM)\n"
        "         DATASET DD1=EXTOUT,RECFM=FB,RECORD=200\n"
        "         DBDGEN\n"),
    "SRC/CLMUPD.cbl": (
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. CLMUPD.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
        "       01  DLI-FUNCS.\n"
        "           05  DLI-GHU    PIC X(4) VALUE 'GHU '.\n"
        "           05  DLI-REPL   PIC X(4) VALUE 'REPL'.\n"
        "       01  PARM-CT       PIC S9(9) COMP VALUE 4.\n"
        "       01  CLAIM-SEG     PIC X(120).\n"
        "       01  CLAIM-SSA     PIC X(30).\n"
        "       LINKAGE SECTION.\n       01  IO-PCB PIC X(20).\n       01  POL-PCB PIC X(40).\n       01  CLM-PCB PIC X(40).\n"
        "       PROCEDURE DIVISION USING IO-PCB POL-PCB CLM-PCB.\n"
        "           CALL 'CBLTDLI' USING DLI-GHU CLM-PCB CLAIM-SEG CLAIM-SSA.\n"
        "           CALL 'CBLTDLI' USING PARM-CT DLI-REPL CLM-PCB CLAIM-SEG.\n"
        "           GOBACK.\n"),
    "SRC/CLMEXTR.cbl": (
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. CLMEXTR.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
        "       01  WS-GN         PIC X(4) VALUE 'GN  '.\n"
        "       01  WS-ISRT       PIC X(4) VALUE 'ISRT'.\n"
        "       01  WS-CLM-AREA   PIC X(120).\n       01  WS-EXTRACT-REC PIC X(200).\n"
        "       PROCEDURE DIVISION.\n"
        "           ENTRY 'DLITCBL' USING CLM-PCB EXT-PCB.\n"
        "       0000-MAIN.\n"
        "           CALL 'CBLTDLI' USING WS-GN CLM-PCB WS-CLM-AREA.\n"
        "           CALL 'CBLTDLI' USING WS-ISRT EXT-PCB WS-EXTRACT-REC.\n"
        "           GOBACK.\n"),
    "SRC/MEMSW.cbl": (
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. MEMSW.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
        "       01  WS-DEST   PIC X(8) VALUE 'CLMP    '.\n"
        "       01  CHNG      PIC X(4) VALUE 'CHNG'.\n"
        "       01  ISRT      PIC X(4) VALUE 'ISRT'.\n"
        "       01  WS-OUT-MSG PIC X(80).\n"
        "       LINKAGE SECTION.\n       01  IO-PCB PIC X(20).\n       01  ALT-PCB PIC X(20).\n"
        "       PROCEDURE DIVISION USING IO-PCB ALT-PCB.\n"
        "           CALL 'CBLTDLI' USING CHNG ALT-PCB WS-DEST.\n"
        "           CALL 'CBLTDLI' USING ISRT ALT-PCB WS-OUT-MSG.\n"
        "           EXEC DLI GHU USING PCB(2) SEGMENT(CLAIM) INTO(WS-OUT-MSG)\n"
        "                SEGLENGTH(80) WHERE(CLMNO = WS-KEY) END-EXEC.\n"
        "           GOBACK.\n"),
    "SRC/CLMPOST.cbl": (
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. CLMPOST.\n"
        "       PROCEDURE DIVISION.\n           GOBACK.\n"),
    "JCL/CLMBMP.jcl": (
        "//CLMBMP JOB (A)\n"
        "//S1 EXEC PGM=DFSRRC00,PARM='BMP,CLMUPD,CLMPSB'\n"
        "//S2 EXEC PGM=DFSRRC00,PARM='DLI,CLMEXTR,EXTPSB'\n"
        "//EXTOUT DD DSN=PROD.CLAIMS.EXTRACT(+1),DISP=(NEW,CATLG)\n"
        "//S3 EXEC PGM=ASMA90\n"
        "//SYSIN DD *\n"
        "         APPLCTN PSB=MEMSW,PGMTYPE=TP\n"
        "         TRANSACT CODE=MEMB,SPA=(200),INQUIRY=NO\n"
        "         APPLCTN PSB=CLMPOST,PGMTYPE=TP\n"
        "         TRANSACT CODE=CLMP,INQUIRY=YES\n"
        "         DATABASE DBD=CLMDBD,ACCESS=UP\n"
        "/*\n"),
    "SRC/CICSPGM.cbl": (
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. CICSPGM.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
        "       01  WS-NEXT-TRAN PIC X(4) VALUE 'MEM2'.\n"
        "       01  WS-FILE      PIC X(8) VALUE 'POLMAST'.\n"
        "       01  WS-COMM      PIC X(100).\n       01  WS-POL-REC PIC X(200).\n"
        "       01  WS-KEY PIC X(10).\n       01  WS-RESP PIC S9(8) COMP.\n       01  WS-LOG PIC X(80).\n"
        "       01  MEM-IN PIC X(500).\n"
        "       PROCEDURE DIVISION.\n"
        "           EXEC CICS RECEIVE MAP('MEMMAP') MAPSET('MEMSET') INTO(MEM-IN)\n"
        "                END-EXEC.\n"
        "           EXEC CICS READ FILE(WS-FILE) INTO(WS-POL-REC) RIDFLD(WS-KEY)\n"
        "                RESP(WS-RESP) END-EXEC.\n"
        "           EXEC CICS REWRITE FILE('POLMAST') FROM(WS-POL-REC) END-EXEC.\n"
        "           EXEC CICS WRITEQ TD QUEUE('LOGQ') FROM(WS-LOG) LENGTH(80) END-EXEC.\n"
        "           EXEC CICS LINK PROGRAM('CONDLOGX') COMMAREA(WS-COMM)\n"
        "                LENGTH(100) END-EXEC.\n"
        "           EXEC CICS START TRANSID('BAT1') FROM(WS-COMM) END-EXEC.\n"
        "           EXEC CICS SEND MAP('MEMMAP') MAPSET('MEMSET') ERASE END-EXEC.\n"
        "           EXEC CICS RETURN TRANSID(WS-NEXT-TRAN) COMMAREA(WS-COMM)\n"
        "                END-EXEC.\n"),
    "SRC/MEMPG2.cbl": "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. MEMPG2.\n       PROCEDURE DIVISION.\n           GOBACK.\n",
    "SRC/BATPGM.cbl": "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. BATPGM.\n       PROCEDURE DIVISION.\n           GOBACK.\n",
    "CSD/MEMCSD2.csd": (
        "DEFINE TRANSACTION(MEM2) GROUP(MEMGRP) PROGRAM(MEMPG2)\n"
        "DEFINE TRANSACTION (BAT1) GROUP(MEMGRP) PROGRAM(BATPGM)\n"
        "DEFINE FILE(POLMAST) GROUP(MEMGRP) DSNAME(PROD.POLICY.MASTER.KSDS)\n"
        "DEFINE TDQUEUE(LOGQ) GROUP(MEMGRP) TYPE(EXTRA) DDNAME(LOGQ)\n"
        "       DSNAME(PROD.CICS.LOGQ.OUT) TYPEFILE(OUTPUT)\n"
        "DEFINE DB2ENTRY(MEMBE) GROUP(MEMGRP) PLAN(MEMBPLAN)\n"
        "DEFINE DB2TRAN(MEMBT) GROUP(MEMGRP) ENTRY(MEMBE) TRANSID(MEM2)\n"
        "DEFINE URIMAP(MEMBURI) GROUP(MEMGRP) USAGE(PIPELINE)\n"
        "       PATH(/member/*) PROGRAM(MEMBWS)\n"),
    "JCL/POLLOAD.jcl": (
        "//POLLOAD JOB (A)\n//S1 EXEC PGM=POLLOAD\n"
        "//POLMAST DD DSN=PROD.POLICY.MASTER.KSDS,DISP=OLD\n"),
    "COPYLIB/DCLPOL.cpy": (
        "           EXEC SQL DECLARE PRD.POLICY_TBL TABLE\n"
        "           ( POL_NO     CHAR(12) NOT NULL,\n"
        "             STATUS_CD  CHAR(2),\n"
        "             PREM_AMT   DECIMAL(11,2) ) END-EXEC.\n"
        "       01  DCLPOLICY.\n"
        "           10 POL-NO    PIC X(12).\n"
        "           10 STATUS-CD PIC X(02).\n"
        "           10 PREM-AMT  PIC S9(9)V99 COMP-3.\n"),
    "SRC/DCLPGM.cbl": (
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. DCLPGM.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
        "           EXEC SQL INCLUDE DCLPOL END-EXEC.\n"
        "       01  WS-KEY PIC X(12).\n       01  WS-ST PIC X(2).\n"
        "       01  WS-POL-NO-ARR PIC X(12) OCCURS 10.\n       01  WS-STATUS-ARR PIC X(2) OCCURS 10.\n"
        "       01  WS-POL PIC X(12).\n       01  WS-RC PIC S9(4) COMP.\n"
        "       PROCEDURE DIVISION.\n"
        "           EXEC SQL SELECT * INTO :DCLPOLICY FROM POLICY_TBL\n"
        "                WHERE POL_NO = :WS-KEY END-EXEC.\n"
        "           EXEC SQL UPDATE PRD.POLICY_TBL SET STATUS_CD = :WS-ST\n"
        "                WHERE POL_NO = :WS-KEY END-EXEC.\n"
        "           EXEC SQL DECLARE POLCUR CURSOR WITH ROWSET POSITIONING FOR\n"
        "                SELECT POL_NO, STATUS_CD FROM POLICY_TBL END-EXEC.\n"
        "           EXEC SQL FETCH NEXT ROWSET FROM POLCUR FOR 10 ROWS\n"
        "                INTO :WS-POL-NO-ARR, :WS-STATUS-ARR END-EXEC.\n"
        "           EXEC SQL CALL PRD.POLPROC (:WS-POL, :WS-RC) END-EXEC.\n"
        "           GOBACK.\n"),
    "SRC/MQPGM.cbl": (
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. MQPGM.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
        "       01  WS-MQOD.\n           05  MQOD-OBJECTNAME PIC X(48) VALUE 'CLAIMS.OUT.QUEUE'.\n"
        "       01  HCONN PIC S9(9) COMP.\n       01  HOBJ PIC S9(9) COMP.\n       01  OPTS PIC S9(9) COMP.\n"
        "       01  CC PIC S9(9) COMP.\n       01  RC PIC S9(9) COMP.\n       01  MQMD PIC X(324).\n"
        "       01  PMO PIC X(128).\n       01  BUFLEN PIC S9(9) COMP.\n"
        "       01  WS-CLAIM-MSG.\n           05  CM-POL-NO PIC X(12).\n           05  CM-GENDER PIC X.\n"
        "       PROCEDURE DIVISION.\n"
        "           CALL 'MQOPEN' USING HCONN WS-MQOD OPTS HOBJ CC RC.\n"
        "           CALL 'MQPUT' USING HCONN HOBJ MQMD PMO BUFLEN WS-CLAIM-MSG CC RC.\n"
        "           GOBACK.\n"),
}

MFS = ("MEMFMT   FMT\n         DEV   TYPE=3270-A2,FEAT=IGNORE\n         DIV   TYPE=INOUT\n"
       "GENDER   DFLD  POS=(3,10),LTH=1\nNAME     DFLD  POS=(5,10),LTH=30\n         FMTEND\n"
       "MEMFIP   MSG   TYPE=INPUT,SOR=(MEMFMT,IGNORE),NXT=MEMFOP\n         SEG\n"
       "         MFLD  'MEMB ',LTH=5\n         MFLD  GENDER\n         MFLD  NAME\n         MSGEND\n"
       "MEMFOP   MSG   TYPE=OUTPUT,SOR=(MEMFMT,IGNORE)\n         SEG\n"
       "         MFLD  GENDER,LTH=1,ATTR=(YES,1)\n         MFLD  NAME,LTH=30\n         MSGEND\n")


class ParseLevel(unittest.TestCase):

    def test_dli_function_from_value_and_parmcount(self):
        f = cobol.parse_program(FILES["SRC/CLMUPD.cbl"])
        self.assertEqual([(d.func, d.pcb_arg, d.io_area, d.resolution) for d in f.dli],
                         [("GHU", "CLM-PCB", "CLAIM-SEG", "value_clause"), ("REPL", "CLM-PCB", "CLAIM-SEG", "parmcount")])
        self.assertFalse(any(u[0] == "dli_function" for u in f.unresolved))

    def test_entry_dlitcbl_gives_the_pcb_list(self):
        f = cobol.parse_program(FILES["SRC/CLMEXTR.cbl"])
        self.assertEqual(f.linkage_using, ["CLM-PCB", "EXT-PCB"])
        self.assertEqual([(e[0], e[1]) for e in f.entries], [("DLITCBL", ["CLM-PCB", "EXT-PCB"])])

    def test_message_switch_and_exec_dli(self):
        f = cobol.parse_program(FILES["SRC/MEMSW.cbl"])
        chng = next(d for d in f.dli if d.func == "CHNG")
        self.assertEqual(chng.dest, "CLMP")
        self.assertIn(("ims_switch", "CLMP"), [(c.kind, c.target) for c in f.calls])
        ed = next(d for d in f.dli if d.interface == "EXEC DLI")
        self.assertEqual((ed.func, ed.pcb_index, ed.io_area, ed.ssa_args[0]), ("GHU", 2, "WS-OUT-MSG", "CLAIM"))
        self.assertIn("CLMNO = WS-KEY", ed.ssa_args[1])
        self.assertIn(("WS-OUT-MSG", "write", "EXEC-DLI"), [(n, m, s) for (n, m, s, _l) in f.field_refs])

    def test_psb_list_no_and_procseq(self):
        p = ims.parse_psb(FILES["PSBLIB/MIXPSB.psb"])
        self.assertTrue(p.pcbs[0].list_no)
        self.assertEqual([(pos, pcb.dbd_name if pcb else None) for (pos, _lbl, pcb) in ims.program_positions(p)],
                         [(1, None), (2, "CLMDBD")])
        q = ims.parse_psb(FILES["PSBLIB/CLMPSB.psb"])
        self.assertEqual(q.pcbs[1].procseq, "CLMXNDX")

    def test_dbd_dataset_fields_xdfld_and_multi_dbd(self):
        d = ims.parse_dbd(FILES["DBDLIB/EXTGSAM.dbd"])
        self.assertEqual((d.name, d.access, d.dd1), ("EXTGSAM", "GSAM", "EXTOUT"))
        both = ims.parse_dbd_all(FILES["DBDLIB/CLMDBD.dbd"] + FILES["DBDLIB/EXTGSAM.dbd"])
        self.assertEqual([b.name for b in both], ["CLMDBD", "EXTGSAM"])
        self.assertEqual(len(both[0].segments), 1)
        self.assertEqual(both[0].xdfld[0][0], "CLMXSTAT")

    def test_cics_commands_and_host_fields(self):
        f = cobol.parse_program(FILES["SRC/CICSPGM.cbl"])
        cmds = {(v, k, r, d) for (v, k, r, d, _l) in f.cics_cmds}
        self.assertIn(("RECEIVE", "map", "MEMSET.MEMMAP", "in"), cmds)
        self.assertIn(("SEND", "map", "MEMSET.MEMMAP", "out"), cmds)
        self.assertIn(("WRITEQ", "tdq", "LOGQ", "out"), cmds)
        self.assertIn(("START", "transid", "BAT1", "out"), cmds)
        self.assertIn(("RETURN", "transid", "MEM2", "out"), cmds)      # via VALUE 'MEM2'
        calls = {(c.kind, c.target, tuple(c.using_args)) for c in f.calls}
        self.assertIn(("cics_link", "CONDLOGX", ("WS-COMM",)), calls)
        self.assertIn(("cics_start", "BAT1", ("WS-COMM",)), calls)
        self.assertIn(("cics_return", "MEM2", ("WS-COMM",)), calls)
        self.assertIn(("POLMAST", "cics", "READ"), [(t, k, o) for (t, k, o, _l) in f.io_ops])   # FILE(WS-FILE) via VALUE
        refs = {(n, m) for (n, m, _s, _l) in f.field_refs}
        self.assertIn(("WS-POL-REC", "write"), refs)
        self.assertIn(("WS-POL-REC", "read"), refs)
        self.assertIn(("WS-KEY", "read"), refs)
        self.assertIn(("WS-RESP", "write"), refs)
        self.assertNotIn(("FILE", "file", "READ"), [(t, k, o) for (t, k, o, _l) in f.io_ops])

    def test_sql_dclgen_group_into_fetch_rowset_and_call(self):
        f = cobol.parse_program(FILES["COPYLIB/DCLPOL.cpy"] + FILES["SRC/DCLPGM.cbl"].replace(
            "           EXEC SQL INCLUDE DCLPOL END-EXEC.\n", ""))
        self.assertEqual([c[0] for c in f.declared_tables["PRD.POLICY_TBL"]], ["POL_NO", "STATUS_CD", "PREM_AMT"])
        self.assertEqual([(hv, cols, stmt) for (hv, _t, cols, stmt, _l) in f.sql_group_intos],
                         [("DCLPOLICY", ["*"], "SELECT")])
        # FETCH ROWSET INTO two host arrays pairs positionally with the cursor's two columns
        self.assertIn(("POLICY_TBL", "STATUS_CD", "WS-STATUS-ARR", "read", "FETCH"),
                      [(t, c, h, m, s) for (t, c, h, m, s, _l) in f.sql_cols])
        tables = {t for s in f.sql for t in s.tables}
        self.assertNotIn("POLCUR", tables)
        self.assertIn(("sql_call", "POLPROC"), [(c.kind, c.target) for c in f.calls])
        self.assertFalse(any(c.kind == "dynamic" for c in f.calls))

    def test_mq_queue_and_layout(self):
        f = cobol.parse_program(FILES["SRC/MQPGM.cbl"])
        self.assertIn(("MQPUT", "CLAIMS.OUT.QUEUE", "out", "WS-CLAIM-MSG"), [m[:4] for m in f.mq])
        self.assertIn(("MQOPEN", "CLAIMS.OUT.QUEUE"), [m[:2] for m in f.mq])

    def test_mfs_inherited_lengths_and_extended_attributes(self):
        scr = {s.name: s for s in screens.parse_mfs(MFS)}
        fip = {f.name: f for f in scr["MEMFIP"].fields if f.name}
        self.assertEqual((fip["GENDER"].offset, fip["GENDER"].length), (5, 1))
        self.assertEqual((fip["NAME"].offset, fip["NAME"].length), (6, 30))
        fop = {f.name: f for f in scr["MEMFOP"].fields if f.name}
        self.assertEqual(fop["GENDER"].length, 5)                 # 1 + 2 + 2*1
        self.assertEqual(fop["NAME"].offset, 5)

    def test_csd_extra_resources_and_blank_before_paren(self):
        r = txn.parse_csd(FILES["CSD/MEMCSD2.csd"])
        self.assertEqual({t.tran_code for t in r.transactions}, {"MEM2", "BAT1"})
        types = {t for (t, _n, _a, _l) in r.resources}
        self.assertEqual(types, {"TRANSACTION", "FILE", "TDQUEUE", "DB2ENTRY", "DB2TRAN", "URIMAP"})

    def test_stage1_detail(self):
        r = txn.parse_imsgen(FILES["JCL/CLMBMP.jcl"].split("//SYSIN DD *\n")[1].split("/*")[0])
        memb = next(t for t in r.transactions if t.tran_code == "MEMB")
        self.assertIn("conversational SPA 200", memb.detail)
        clmp = next(t for t in r.transactions if t.tran_code == "CLMP")
        self.assertIn("inquiry-only", clmp.detail)


class BuildLevel(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        for rel, text in FILES.items():
            p = os.path.join(cls.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def _dli(self, pgm):
        return self.conn.execute("""SELECT d.func, d.pcb_ordinal, d.dbd_name, d.procopt, d.psb_name, d.pcb_source
                                    FROM dli_call d JOIN program p ON p.id=d.program_id WHERE p.program_id=? ORDER BY d.line""",
                                 (pgm,)).fetchall()

    def test_pcb_position_resolved_with_io_pcb_first(self):
        rows = self._dli("CLMUPD")
        self.assertEqual([(r["func"], r["pcb_ordinal"], r["dbd_name"], r["procopt"]) for r in rows],
                         [("GHU", 2, "CLMDBD", "A"), ("REPL", 2, "CLMDBD", "A")])
        self.assertIn("USING position 3", rows[0]["pcb_source"])
        self.assertIn("DFSRRC00 PARM", rows[0]["pcb_source"])

    def test_gsam_pcb_sets_the_dd_direction(self):
        rows = self._dli("CLMEXTR")
        self.assertEqual([(r["func"], r["dbd_name"]) for r in rows], [("GN", "CLMDBD"), ("ISRT", "EXTGSAM")])
        dd = self.conn.execute("""SELECT d.mode, d.mode_source FROM dd d JOIN step s ON s.id=d.step_id
                                  WHERE d.dd_name='EXTOUT' AND s.effective_pgm='CLMEXTR'""").fetchone()
        self.assertEqual((dd["mode"], dd["mode_source"]), ("output", "gsam_isrt"))
        self.assertIn("CLMEXTR", query.cmd_dataset(self.conn, "PROD.CLAIMS.EXTRACT"))

    def test_dbd_and_segment_queries(self):
        out = query.cmd_dbd(self.conn, "CLMDBD")
        self.assertIn("CLMSTAT@25/2", out)
        self.assertIn("CLMXSTAT on CLAIM from CLMSTAT", out)
        self.assertIn("CLMUPD", out)
        self.assertIn("| A |", out)                                   # PROCOPT A -> updater
        self.assertIn("ACCESS=UP", out)                               # stage-1 DATABASE inside JCL SYSIN
        seg = query.cmd_segment(self.conn, "CLAIM")
        self.assertIn("CLMUPD", seg)
        self.assertIn("MEMSW", seg)                                   # EXEC DLI SEGMENT(CLAIM)

    def test_message_switch_reaches_the_transaction(self):
        out = query.cmd_transaction(self.conn, "CLMP")
        self.assertIn("IMS message switch", out)
        self.assertIn("MEMSW", out)
        self.assertNotIn("CLMPOST", query.cmd_dead(self.conn).split("Programs never called")[1].split("\n\n")[0]
                         if "Programs never called" in query.cmd_dead(self.conn) else "")
        dead = query.cmd_dead(self.conn)
        self.assertNotRegex(dead, r"\| CLMPOST \|")

    def test_cics_chaining_maps_and_dead(self):
        tx = query.cmd_transaction(self.conn, "MEM2")
        self.assertIn("RETURN TRANSID", tx)
        self.assertIn("CICSPGM", tx)
        self.assertIn("DB2 plan MEMBPLAN", tx)
        dead = query.cmd_dead(self.conn)
        self.assertNotRegex(dead, r"\| MEMPG2 \|")
        self.assertNotRegex(dead, r"\| BATPGM \|")
        callers = query.cmd_graph(self.conn, "MEMPG2", "callers", 1)
        self.assertIn("CICSPGM", callers)
        prog = query.cmd_program(self.conn, "CICSPGM")
        self.assertIn("MEMSET.MEMMAP", prog)
        self.assertIn("LOGQ", prog)
        self.assertIn("MEMBWS", query.cmd_transaction(self.conn, "MEMBURI"))

    def test_dataset_shows_online_readers_and_tdq(self):
        out = query.cmd_dataset(self.conn, "PROD.POLICY.MASTER.KSDS")
        self.assertIn("POLLOAD", out)
        self.assertIn("CICSPGM", out)
        self.assertIn("[cics_verb]", out)
        self.assertIn("FCT POLMAST", out)
        self.assertIn("READ, REWRITE", out)
        tdq = query.cmd_dataset(self.conn, "PROD.CICS.LOGQ.OUT")
        self.assertIn("[cics_tdq]", tdq)

    def test_dclgen_select_star_and_table_query(self):
        rows = self.conn.execute("""SELECT c.tbl, c.col, c.host_var, c.mode FROM sql_col_ref c JOIN program p ON p.id=c.program_id
                                    WHERE p.program_id='DCLPGM' ORDER BY c.line, c.col""").fetchall()
        got = {(r["col"], r["host_var"], r["mode"]) for r in rows}
        self.assertIn(("STATUS_CD", "STATUS-CD", "read"), got)
        self.assertIn(("PREM_AMT", "PREM-AMT", "read"), got)
        self.assertIn(("STATUS_CD", "WS-STATUS-ARR", "read"), got)  # FETCH ROWSET INTO two host arrays
        self.assertIn(("STATUS_CD", "WS-ST", "write"), got)
        col = query.cmd_column(self.conn, "POLICY_TBL.STATUS_CD")
        self.assertIn("declared:", col)
        self.assertIn("PRD.POLICY_TBL", col)                          # qualified and unqualified both listed
        tbl = query.cmd_table(self.conn, "POLICY_TBL")
        self.assertIn("| DCLPGM | RU |", tbl)
        self.assertIn("PREM_AMT", tbl)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM unresolved WHERE kind='dynamic_call'").fetchone())

    def test_mq_interface_edge_names_the_queue(self):
        row = self.conn.execute("SELECT detail, direction FROM interface_edge WHERE kind='mq' AND detail LIKE 'MQPUT%'").fetchone()
        self.assertIn("queue CLAIMS.OUT.QUEUE", row["detail"])
        self.assertIn("layout WS-CLAIM-MSG", row["detail"])
        self.assertEqual(row["direction"], "out")


if __name__ == "__main__":
    unittest.main()
