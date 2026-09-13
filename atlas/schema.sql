-- ============================================================================
-- Mainframe Atlas - deterministic fact store for a COBOL/JCL/IMS/DB2 estate.
--
-- Design rules:
--   1. Every row is a FACT extracted by a parser. No LLM output lives here.
--      Model-written prose goes in `derived_summary`, which is explicitly
--      marked as unverified and is never joined into an impact answer.
--   2. Every fact carries member_id + line so any claim can be cited and
--      re-checked against the source.
--   3. Anything the parser could not resolve goes in `unresolved`. Nothing is
--      ever silently dropped - a missing row must never be read as "no link".
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- inventory

CREATE TABLE IF NOT EXISTS member (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,   -- absolute path on disk
    name          TEXT NOT NULL,          -- PDS member name (stem, upper)
    kind          TEXT NOT NULL,          -- cobol|copybook|jcl|proc|ctlcard|dbd|psb|bms|mfs|sql|doc|unknown
    library       TEXT,                   -- the folder that acts as the PDS
    ext           TEXT,
    sha256        TEXT NOT NULL,          -- hash of the raw bytes (provenance)
    norm_sha      TEXT,                   -- hash of cols 8-72 only, trailing blanks
                                          -- stripped, upper-cased. Raw hashes almost
                                          -- never collide because cols 1-6 hold
                                          -- sequence numbers and 73-80 hold change
                                          -- stamps; THIS is the duplicate detector.
    bytes         INTEGER,
    lines         INTEGER,
    fixed_format  INTEGER,                -- 1 = cols 7/72 rules applied
    authoritative INTEGER DEFAULT 0,      -- 1 = declared production copy (manifest)
    system        TEXT,                   -- department / system (manifest: systems / system_of);
                                          -- copybook resolution prefers the program's own system
    parse_status  TEXT DEFAULT 'pending', -- ok|partial|failed|skipped
    parse_error   TEXT,
    scanned_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_member_name   ON member(name);
CREATE INDEX IF NOT EXISTS ix_member_kind   ON member(kind);
CREATE INDEX IF NOT EXISTS ix_member_sha    ON member(sha256);

-- Same member name appearing in >1 library, or same content in >1 path.
-- The "which copy is production?" question is the #1 source of wrong answers
-- when analysing a folder dump, so it gets a first-class view.
-- Same name AND same kind: SAMPPGM.cbl next to SAMPPGM.psb and SAMPPGM.jcl
-- is a convention, not a duplicate.
CREATE VIEW IF NOT EXISTS v_ambiguous_member AS
SELECT name, kind, COUNT(*) AS copies, COUNT(DISTINCT norm_sha) AS distinct_content,
       GROUP_CONCAT(DISTINCT system) AS systems,
       GROUP_CONCAT(path, ' | ') AS paths
FROM member
WHERE kind IN ('cobol','copybook','jcl','proc','dbd','psb','ctlcard')
GROUP BY name, kind
HAVING COUNT(*) > 1;

-- ------------------------------------------------------------------ program

CREATE TABLE IF NOT EXISTS program (
    id            INTEGER PRIMARY KEY,
    member_id     INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    program_id    TEXT NOT NULL,          -- from PROGRAM-ID
    is_initial    INTEGER DEFAULT 0,
    uses_sql      INTEGER DEFAULT 0,
    uses_cics     INTEGER DEFAULT 0,
    uses_dli      INTEGER DEFAULT 0,
    uses_mq       INTEGER DEFAULT 0,
    linkage_using TEXT,                   -- JSON array, positional - order matters
    src_lines     INTEGER,
    exp_lines     INTEGER                 -- lines after copybook expansion
);
CREATE INDEX IF NOT EXISTS ix_program_pid ON program(program_id);

CREATE TABLE IF NOT EXISTS paragraph (
    id          INTEGER PRIMARY KEY,
    program_id  INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    section     TEXT,
    name        TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    ordinal     INTEGER NOT NULL,         -- needed for PERFORM..THRU and fall-through
    kind        TEXT DEFAULT 'paragraph'  -- paragraph | section (a performed section runs its paragraphs)
);
CREATE INDEX IF NOT EXISTS ix_para_prog ON paragraph(program_id, ordinal);

CREATE TABLE IF NOT EXISTS perform_edge (
    id          INTEGER PRIMARY KEY,
    program_id  INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    from_para   TEXT,
    to_para     TEXT NOT NULL,
    thru_para   TEXT,                     -- PERFORM A THRU B
    line        INTEGER,
    kind        TEXT DEFAULT 'perform'    -- perform|goto|goto_depending|alter|fallthrough|sort_proc
);

-- CALL: the single most important cross-program edge, and the one most often
-- got wrong. kind='dynamic' means the target is a variable - `resolved` holds
-- literals traced back via MOVE, and may be incomplete. Treat an empty
-- `resolved` on a dynamic call as UNKNOWN, never as "calls nothing".
CREATE TABLE IF NOT EXISTS call_edge (
    id          INTEGER PRIMARY KEY,
    program_id  INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,            -- static|dynamic|cics_link|cics_xctl|proc_call
    target      TEXT,                     -- literal name, when kind='static'
    via_var     TEXT,                     -- variable name, when kind='dynamic'
    resolved    TEXT,                     -- JSON array of candidate targets
    resolution  TEXT,                     -- how: move_literal|value_clause|unresolved
    using_args  TEXT,                     -- JSON array, positional
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_call_target ON call_edge(target);
CREATE INDEX IF NOT EXISTS ix_call_prog   ON call_edge(program_id);

-- ----------------------------------------------------------------- copybook

CREATE TABLE IF NOT EXISTS copy_use (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    copybook    TEXT NOT NULL,
    of_library  TEXT,                     -- COPY X OF Y / IN Y
    replacing   TEXT,                     -- raw REPLACING text; changes field names!
    line        INTEGER,
    resolved_member_id INTEGER REFERENCES member(id)
);
CREATE INDEX IF NOT EXISTS ix_copy_book ON copy_use(copybook);
CREATE INDEX IF NOT EXISTS ix_copy_mem  ON copy_use(member_id);

-- Field layout with computed byte offsets. This is what makes "if I change
-- this field, what breaks" answerable, and what stops the model inventing
-- field names or generating test data that violates the record layout.
CREATE TABLE IF NOT EXISTS field (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES field(id),
    level       INTEGER NOT NULL,
    name        TEXT NOT NULL,
    qualified   TEXT,                     -- A.B.C path, for OF/IN qualification
    pic         TEXT,
    usage       TEXT,                     -- DISPLAY|COMP|COMP-3|COMP-5|POINTER...
    sign_clause TEXT,
    occurs      INTEGER,
    occurs_max  INTEGER,
    odo_on      TEXT,                     -- OCCURS DEPENDING ON <field>
    redefines   TEXT,
    value_lit   TEXT,
    offset      INTEGER,                  -- 0-based byte offset in the 01 group
    length      INTEGER,                  -- storage bytes (COMP-3 = packed!)
    digits      INTEGER,
    scale       INTEGER,
    is_group    INTEGER DEFAULT 0,
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_field_name ON field(name);
CREATE INDEX IF NOT EXISTS ix_field_mem  ON field(member_id);

-- 88-levels are encoded business rules and the best free source of test
-- conditions in the entire estate.
CREATE TABLE IF NOT EXISTS cond88 (
    id          INTEGER PRIMARY KEY,
    field_id    INTEGER NOT NULL REFERENCES field(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    values_lit  TEXT,                     -- JSON array of literals/ranges
    line        INTEGER
);

CREATE TABLE IF NOT EXISTS field_ref (
    id          INTEGER PRIMARY KEY,
    program_id  INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    mode        TEXT,                     -- read|write|test|display
    stmt        TEXT,                     -- MOVE|COMPUTE|CALL-USING|EXEC-SQL|READ|...
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_fieldref_name ON field_ref(name);
CREATE INDEX IF NOT EXISTS ix_fieldref_mode ON field_ref(name, mode);

-- Literals are facts. An error code is 'E123' long before it is a field name:
-- defined by an 88-level or VALUE in a copybook, MOVEd to a field in one
-- program, compared in another, DISPLAYed in a third. Indexing the literal is
-- what makes "where does E123 come from and where is it shown" a query
-- instead of a guess. `field` is the target of a MOVE, the subject of a
-- comparison/WHEN, the owner of a VALUE, or "<parent>/<88-name>" for an 88.
CREATE TABLE IF NOT EXISTS literal_ref (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    program_id  INTEGER REFERENCES program(id) ON DELETE CASCADE,
    literal     TEXT NOT NULL,
    context     TEXT NOT NULL,            -- move_to|compare|when|display|string|value|cond88
    field       TEXT,
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_literal_val ON literal_ref(literal);
CREATE INDEX IF NOT EXISTS ix_literal_ctx ON literal_ref(literal, context);

-- ---------------------------------------------------------------- files/IO

CREATE TABLE IF NOT EXISTS file_decl (
    id          INTEGER PRIMARY KEY,
    program_id  INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    select_name TEXT NOT NULL,            -- SELECT <name>
    assign_dd   TEXT,                     -- ASSIGN TO <ddname>  <- the JCL join key
    organization TEXT,                    -- SEQUENTIAL|INDEXED|RELATIVE
    access_mode TEXT,
    record_key  TEXT,
    alt_keys    TEXT,                     -- JSON array
    fd_record   TEXT,                     -- 01 record name under the FD
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_filedecl_dd ON file_decl(assign_dd);

CREATE TABLE IF NOT EXISTS io_op (
    id          INTEGER PRIMARY KEY,
    program_id  INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    target      TEXT NOT NULL,            -- file / table / segment name
    target_kind TEXT NOT NULL,            -- file|db2|ims|mq|cics
    op          TEXT NOT NULL,            -- READ|WRITE|REWRITE|DELETE|OPEN|CLOSE|START|SELECT|INSERT|...
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ioop_target ON io_op(target, target_kind);

-- -------------------------------------------------------------------- DB2

CREATE TABLE IF NOT EXISTS sql_stmt (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    program_id  INTEGER REFERENCES program(id) ON DELETE CASCADE,
    stmt_type   TEXT,                     -- SELECT|INSERT|UPDATE|DELETE|DECLARE|OPEN|FETCH|CLOSE|CALL|MERGE
    cursor_name TEXT,
    tables      TEXT,                     -- JSON array
    columns     TEXT,                     -- JSON array
    host_vars   TEXT,                     -- JSON array
    is_dynamic  INTEGER DEFAULT 0,
    start_line  INTEGER,
    end_line    INTEGER,
    text        TEXT
);
CREATE INDEX IF NOT EXISTS ix_sql_prog ON sql_stmt(program_id);

-- Column-level DB2 lineage: which COBOL field a column is read INTO, written
-- FROM (INSERT/UPDATE), or compared with in a predicate. "Where is column X
-- created and used" is unanswerable from table names alone.
CREATE TABLE IF NOT EXISTS sql_col_ref (
    id          INTEGER PRIMARY KEY,
    program_id  INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    tbl         TEXT,                     -- resolved table (alias expanded); '?' if ambiguous
    col         TEXT NOT NULL,
    host_var    TEXT,                     -- COBOL field (no colon)
    mode        TEXT NOT NULL,            -- read (col -> host var) | write (host var -> col) | predicate
    stmt        TEXT,                     -- SELECT|FETCH|INSERT|UPDATE|DELETE|DECLARE
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_sqlcol_col ON sql_col_ref(col);
CREATE INDEX IF NOT EXISTS ix_sqlcol_hv  ON sql_col_ref(host_var);

CREATE TABLE IF NOT EXISTS db2_object (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,            -- table|view|alias|proc
    qualifier   TEXT,
    name        TEXT NOT NULL,
    source      TEXT,                     -- ddl|dclgen|inferred_from_sql|catalog_unload
    member_id   INTEGER REFERENCES member(id)
);
CREATE INDEX IF NOT EXISTS ix_db2obj_name ON db2_object(name);

-- --------------------------------------------------------------------- IMS

CREATE TABLE IF NOT EXISTS ims_dbd (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    access      TEXT,                     -- HDAM|HIDAM|DEDB|INDEX|LOGICAL|GSAM...
    line        INTEGER,
    dd1         TEXT,                     -- DATASET DD1= : the JCL DD (GSAM: the file the program writes/reads)
    dd2         TEXT
);

CREATE TABLE IF NOT EXISTS ims_field (
    id          INTEGER PRIMARY KEY,
    segment_id  INTEGER NOT NULL REFERENCES ims_segment(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    start       INTEGER,                  -- 1-based byte in the segment
    bytes       INTEGER,
    is_seq      INTEGER DEFAULT 0,
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ims_field_name ON ims_field(name);

CREATE TABLE IF NOT EXISTS ims_xdfld (
    id          INTEGER PRIMARY KEY,
    dbd_id      INTEGER NOT NULL REFERENCES ims_dbd(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,            -- secondary index search field
    segment     TEXT,
    srch        TEXT,                     -- JSON array of source fields
    line        INTEGER
);

CREATE TABLE IF NOT EXISTS ims_online_db (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    dbd         TEXT NOT NULL,            -- stage-1 DATABASE DBD=
    access      TEXT,                     -- UP|RO|RD|EX
    line        INTEGER
);

CREATE TABLE IF NOT EXISTS ims_segment (
    id          INTEGER PRIMARY KEY,
    dbd_id      INTEGER NOT NULL REFERENCES ims_dbd(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    parent      TEXT,
    bytes       INTEGER,
    seq_field   TEXT,
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_seg_name ON ims_segment(name);

-- PCB ordering is POSITIONAL. A program addresses PCBs by their index in the
-- PSB, so `ordinal` is load-bearing: off-by-one here means the analysis names
-- the wrong database. Store it explicitly and never re-sort this table.
CREATE TABLE IF NOT EXISTS ims_psb (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    psb_type    TEXT,                     -- TP|DB|batch
    lang        TEXT,
    cmpat       TEXT,                     -- CMPAT=YES inserts an I/O PCB in FRONT of
                                          -- the DB PCBs, shifting every position by 1
    io_pcb_first INTEGER,                 -- 1 = program's first PCB is the I/O PCB
                                          -- (CMPAT=YES or TP PCBs present); NULL =
                                          -- depends on region type (BMP/MPP yes,
                                          -- DLI batch no) - resolve from the JCL
    line        INTEGER
);

CREATE TABLE IF NOT EXISTS ims_pcb (
    id          INTEGER PRIMARY KEY,
    psb_id      INTEGER NOT NULL REFERENCES ims_psb(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,         -- 1-based position  <- positional!
    pcb_type    TEXT,                     -- DB|TP|GSAM|IO
    dbd_name    TEXT,
    procopt     TEXT,                     -- G|GO|I|R|D|A ... read vs update intent
    keylen      INTEGER,
    sensegs     TEXT,                     -- JSON array
    line        INTEGER,
    list_no     INTEGER DEFAULT 0,        -- LIST=NO: not in the program's PCB address list
    procseq     TEXT                      -- PROCSEQ=: accessed through this secondary index
);

CREATE TABLE IF NOT EXISTS dli_call (
    id          INTEGER PRIMARY KEY,
    program_id  INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    interface   TEXT,                     -- CBLTDLI|AIBTDLI|EXEC DLI
    func        TEXT,                     -- GU|GN|GHU|GNP|ISRT|REPL|DLET|CHKP|XRST|ROLB
    pcb_arg     TEXT,                     -- the PCB variable as written
    pcb_ordinal INTEGER,                  -- resolved PCB number in the PSB, NULL if unresolved
    ssa_args    TEXT,                     -- JSON array
    io_area     TEXT,
    line        INTEGER,
    pcb_index   INTEGER,                  -- EXEC DLI USING PCB(n)
    resolution  TEXT,                     -- how the function was known: literal|value_clause|parmcount|unresolved
    dest        TEXT,                     -- CHNG destination (message switch target)
    psb_name    TEXT,                     -- PSB the position was resolved through
    dbd_name    TEXT,                     -- the database the PCB addresses (after resolution)
    procopt     TEXT,                     -- its PROCOPT: read vs update intent
    pcb_source  TEXT                      -- how (or why not) the PCB was resolved
);

-- ---------------------------------------------------------------- JCL / PROC

CREATE TABLE IF NOT EXISTS job (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    job_name    TEXT NOT NULL,
    line        INTEGER,
    joblib      TEXT,                     -- JSON list: //JOBLIB DD concatenation
    job_cond    TEXT,                     -- COND= on the JOB card (applies to every step)
    jcllib      TEXT                      -- JSON list: // JCLLIB ORDER=(...) PROC/INCLUDE search order
);
CREATE INDEX IF NOT EXISTS ix_job_name ON job(job_name);

CREATE TABLE IF NOT EXISTS proc_def (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    proc_name   TEXT NOT NULL,
    symbolics   TEXT,                     -- JSON object of default values
    instream    INTEGER DEFAULT 0,
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_proc_name ON proc_def(proc_name);

-- `pgm` is what the EXEC says. `effective_pgm` is what actually runs, after
-- unwrapping utility launchers - IKJEFT01/DSN runs a DB2 program named in
-- SYSTSIN, DFSRRC00 runs an IMS program named in its PARM, and PGM=SORT runs
-- control cards. Analysis that reads `pgm` alone is wrong for every such step.
CREATE TABLE IF NOT EXISTS step (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER REFERENCES job(id) ON DELETE CASCADE,
    proc_id       INTEGER REFERENCES proc_def(id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    step_name     TEXT,
    pgm           TEXT,
    proc_called   TEXT,
    effective_pgm TEXT,
    launcher      TEXT,                   -- IKJEFT01|DFSRRC00|SORT|IEBGENER|IDCAMS|...
    parm          TEXT,
    cond          TEXT,
    from_proc     TEXT,                   -- EFFECTIVE step: the PROC it came from,
    parent_step   TEXT,                   -- and the job step whose EXEC PROC= produced it.
                                          -- Symbolics and //STEP.DD overrides are applied,
                                          -- so THESE rows carry the datasets a job really uses.
    guard         TEXT,                   -- enclosing // IF (...) THEN / ELSE: runs only when true
    line          INTEGER
);
CREATE INDEX IF NOT EXISTS ix_step_pgm  ON step(effective_pgm);
CREATE INDEX IF NOT EXISTS ix_step_job  ON step(job_id, ordinal);
CREATE INDEX IF NOT EXISTS ix_step_parent ON step(job_id, parent_step);

CREATE TABLE IF NOT EXISTS dd (
    id          INTEGER PRIMARY KEY,
    step_id     INTEGER NOT NULL REFERENCES step(id) ON DELETE CASCADE,
    dd_name     TEXT,                     -- NULL/'' for a concatenation continuation
    concat_seq  INTEGER DEFAULT 0,
    dsn         TEXT,                     -- as written, symbolics unresolved
    dsn_resolved TEXT,                    -- after symbolic substitution
    gdg_rel     TEXT,                     -- +1 / 0 / -1
    disp        TEXT,
    mode        TEXT,                     -- input|output|mod|sysout|dummy|unknown
    mode_source TEXT,                     -- open_verb|gdg_relative|dd_convention|
                                          -- disp_new_weak|disp_mod_weak|undetermined
                                          -- DISP is NOT direction; see jcl._direction()
    sysin_text  TEXT,                     -- inline control cards live HERE - also the text of
                                          -- DSN=LIB(MEMBER) when that card member is indexed
    is_override INTEGER DEFAULT 0,        -- //STEP1.DD1 style override of a PROC DD
    card_member TEXT,                     -- DSN=PROD.PARMLIB(SRTCLM) -> 'SRTCLM'
    is_temp     INTEGER DEFAULT 0,        -- &&TEMP: exists only between steps of THIS job;
                                          -- never a link between two jobs
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_dd_step ON dd(step_id);
CREATE INDEX IF NOT EXISTS ix_dd_dsn  ON dd(dsn_resolved);

CREATE TABLE IF NOT EXISTS dataset (
    id          INTEGER PRIMARY KEY,
    dsn         TEXT NOT NULL UNIQUE,     -- normalised, GDG base without (+1)
    is_gdg      INTEGER DEFAULT 0,
    is_vsam     INTEGER DEFAULT 0,
    vsam_type   TEXT,                     -- KSDS|ESDS|RRDS|LDS|AIX|PATH|GDG (from IDCAMS DEFINE)
    recordsize_max INTEGER,               -- DEFINE CLUSTER RECORDSIZE(avg max): must equal the copybook length
    key_len     INTEGER,                  -- KEYS(len off)
    key_off     INTEGER,
    gdg_limit   INTEGER,                  -- DEFINE GDG LIMIT(n)
    relates_to  TEXT                      -- AIX RELATE(base) / PATH PATHENTRY(aix)
);

-- A batch utility step touching a DB2 table: LOAD/UNLOAD (DSNUTILB) and the
-- SQL a DSNTIAUL/DSNTEP2 step runs. `table X` writers include the LOAD.
CREATE TABLE IF NOT EXISTS step_table (
    id          INTEGER PRIMARY KEY,
    step_id     INTEGER NOT NULL REFERENCES step(id) ON DELETE CASCADE,
    op          TEXT,                     -- LOAD|UNLOAD|REORG|RUNSTATS|COPY|SELECT|INSERT|UPDATE|DELETE
    tbl         TEXT NOT NULL,
    via_dd      TEXT,                     -- the DD carrying the rows (INDDN / UNLDDN / SYSREC00)
    direction   TEXT                      -- read|write|reorg
);
CREATE INDEX IF NOT EXISTS ix_step_table ON step_table(tbl);

-- The producer/consumer edge that reveals real batch data flow. GDG relative
-- refs are why this cannot be done by string-matching DSNs.
CREATE VIEW IF NOT EXISTS v_dataset_flow AS
SELECT d.dsn_resolved AS dsn, s.effective_pgm AS pgm, j.job_name, s.step_name,
       d.mode, d.mode_source, d.gdg_rel, m.path, m.system, s.from_proc,
       s.proc_called, s.job_id, s.proc_id, pd.proc_name, d.is_temp, d.dd_name,
       d.line AS dd_line, m.name AS member_name
FROM dd d
JOIN step s   ON s.id = d.step_id
LEFT JOIN job j ON j.id = s.job_id
LEFT JOIN proc_def pd ON pd.id = s.proc_id
LEFT JOIN member m ON m.id = COALESCE(j.member_id, pd.member_id)
WHERE d.dsn_resolved IS NOT NULL;

-- Byte positions referenced by SORT/MERGE/INCLUDE/OMIT/INREC/OUTREC/OUTFIL
-- cards. Sort cards are business logic that addresses the record by byte
-- position; the impact query joins `pos` against computed field offsets so a
-- copybook change that moves bytes surfaces every sort step it breaks.
CREATE TABLE IF NOT EXISTS card_field_ref (
    id          INTEGER PRIMARY KEY,
    step_id     INTEGER NOT NULL REFERENCES step(id) ON DELETE CASCADE,
    card_kind   TEXT,                     -- SORT|MERGE|INCLUDE|OMIT|INREC|OUTREC|OUTFIL|JOINKEYS|SUM
    pos         INTEGER,                  -- 1-based byte position (RDW excluded on VB!)
    length      INTEGER,
    fmt         TEXT,                     -- CH|ZD|PD|BI|FI|...
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS ix_card_pos ON card_field_ref(pos);

-- ------------------------------------------------------------ screens
-- BMS maps (CICS) and MFS formats/messages (IMS DC). The first place a code
-- value is validated online is the program reading the screen field; the
-- screen definition holds the field's length, picture and default.
CREATE TABLE IF NOT EXISTS screen (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,            -- bms_map | mfs_fmt | mfs_msg
    name        TEXT NOT NULL,            -- map / FMT / MSG (MID or MOD) label
    parent      TEXT,                     -- BMS mapset ; MFS msg -> FMT (SOR=)
    mode        TEXT,                     -- IN|OUT|INOUT ; INPUT|OUTPUT
    next_msg    TEXT,                     -- MFS NXT=
    lang        TEXT,
    size        TEXT,
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_screen_name ON screen(name);

-- BMS field GENDER is referenced by programs as GENDERI / GENDERO (generated
-- symbolic map). MFS MFLDs carry a byte OFFSET within the segment data; the
-- program's I/O copybook mirrors that order, so offset is the join key.
CREATE TABLE IF NOT EXISTS screen_field (
    id          INTEGER PRIMARY KEY,
    screen_id   INTEGER NOT NULL REFERENCES screen(id) ON DELETE CASCADE,
    name        TEXT,                     -- NULL = unnamed constant/label on the screen
    ordinal     INTEGER,
    row         INTEGER,
    col         INTEGER,
    length      INTEGER,
    offset      INTEGER,                  -- MFS only (after the 4-byte LL ZZ)
    seg         INTEGER,                  -- MFS segment number
    attrb       TEXT,
    initial     TEXT,                     -- BMS INITIAL= / MFS default literal
    picin       TEXT,
    picout      TEXT,
    literal     TEXT,                     -- constant field text (labels, trancode)
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_sfield_name ON screen_field(name);

-- ----------------------------------------------------- online / interfaces

-- Transaction -> program routing from the CICS CSD (DEFINE TRANSACTION ...
-- PROGRAM(...)) and the IMS stage-1 SYSGEN (APPLCTN PSB= / TRANSACT CODE=).
-- Nothing in COBOL, BMS or MFS holds this; without it an online program
-- looks dead and "which transaction runs this" is a guess.
CREATE TABLE IF NOT EXISTS transaction_def (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER REFERENCES member(id) ON DELETE CASCADE,
    tran_code   TEXT NOT NULL,
    system      TEXT,                     -- cics|ims_dc
    program     TEXT,                     -- IMS: assumed = PSB name unless GPSB= (see detail)
    psb         TEXT,
    group_name  TEXT,                     -- CICS GROUP()
    map_or_mfs  TEXT,
    detail      TEXT,                     -- REMOTESYSTEM, PGMTYPE, assumptions
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_txn_code ON transaction_def(tran_code);
CREATE INDEX IF NOT EXISTS ix_txn_prog ON transaction_def(program);

CREATE TABLE IF NOT EXISTS cics_program (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER REFERENCES member(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    group_name  TEXT,
    language    TEXT,
    line        INTEGER
);

-- FCT: EXEC CICS READ FILE('POLMAST') names this entry; only the CSD says
-- which dataset it is.
CREATE TABLE IF NOT EXISTS cics_file (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER REFERENCES member(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    dsname      TEXT,
    group_name  TEXT,
    line        INTEGER
);

-- Every EXEC CICS command that names a resource: the online call graph
-- (START/RETURN TRANSID), the program<->map edge (SEND/RECEIVE MAP), the
-- queues that carry data between programs and to batch (WRITEQ/READQ TD/TS).
CREATE TABLE IF NOT EXISTS cics_cmd (
    id            INTEGER PRIMARY KEY,
    program_id    INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    verb          TEXT,                   -- SEND|RECEIVE|START|RETURN|WRITEQ|READQ|LINK|PUT|...
    resource_kind TEXT,                   -- map|transid|tdq|tsq|container|webservice|program|file|web
    resource      TEXT,                   -- MAPSET.MAP | tran code | queue | ...
    direction     TEXT,                   -- in|out|delete|NULL
    line          INTEGER
);
CREATE INDEX IF NOT EXISTS ix_cics_cmd_res ON cics_cmd(resource_kind, resource);

-- Every CSD resource block, whatever its type (TDQUEUE, DB2ENTRY, URIMAP...).
CREATE TABLE IF NOT EXISTS cics_resource (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER REFERENCES member(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    name        TEXT NOT NULL,
    group_name  TEXT,
    attrs       TEXT,                     -- JSON object of the block's attributes
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_cics_resource ON cics_resource(type, name);

-- Columns of a DB2 table as declared (DCLGEN DECLARE TABLE, DDL, catalog):
-- the positional list behind `SELECT * INTO :DCLPOLICY`.
CREATE TABLE IF NOT EXISTS db2_column (
    id          INTEGER PRIMARY KEY,
    object_id   INTEGER NOT NULL REFERENCES db2_object(id) ON DELETE CASCADE,
    ordinal     INTEGER,
    name        TEXT NOT NULL,
    type        TEXT,
    check_text  TEXT
);
CREATE INDEX IF NOT EXISTS ix_db2_column ON db2_column(name);
CREATE UNIQUE INDEX IF NOT EXISTS ux_db2_object ON db2_object(kind, name, COALESCE(qualifier, ''));

-- ENTRY 'name' USING ...: CALL 'name' reaches this program.
CREATE TABLE IF NOT EXISTS program_alias (
    id            INTEGER PRIMARY KEY,
    program_id    INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    alias         TEXT NOT NULL,
    linkage_using TEXT,                   -- JSON array, positional
    line          INTEGER
);
CREATE INDEX IF NOT EXISTS ix_program_alias ON program_alias(alias);

-- COPY ... REPLACING renamed a copybook field inside this program:
-- `field PM-POLICY-STATUS` must find the program's LK-POLICY-STATUS references.
CREATE TABLE IF NOT EXISTS field_alias (
    id          INTEGER PRIMARY KEY,
    program_id  INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    copybook    TEXT,
    orig_name   TEXT NOT NULL,            -- as stored in the copybook
    new_name    TEXT NOT NULL,            -- as the compiler sees it in this program
    line        INTEGER                   -- copybook line
);
CREATE INDEX IF NOT EXISTS ix_field_alias_orig ON field_alias(orig_name);
CREATE INDEX IF NOT EXISTS ix_field_alias_new ON field_alias(new_name);
CREATE INDEX IF NOT EXISTS ix_cicsfile_name ON cics_file(name);

CREATE TABLE IF NOT EXISTS interface_edge (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,            -- mq|ftp|ndm|zosconnect|webservice|flatfile|ims_msw
    detail      TEXT,                     -- queue name / host / service / dsn
    direction   TEXT,                     -- in|out|both
    peer_system TEXT,                     -- filled in by hand from the manifest
    line        INTEGER
);

-- ---------------------------------------------------- scheduler (CA-7/CTM/TWS)

CREATE TABLE IF NOT EXISTS sched_job (
    id          INTEGER PRIMARY KEY,
    job_name    TEXT NOT NULL,
    system      TEXT,
    schedule    TEXT,
    calendar    TEXT,
    source      TEXT                      -- which export this came from
);

CREATE TABLE IF NOT EXISTS sched_dep (
    id          INTEGER PRIMARY KEY,
    job_name    TEXT NOT NULL,
    depends_on  TEXT NOT NULL,
    kind        TEXT                      -- predecessor|trigger|resource|dataset
);

-- ------------------------------------------------------------- bookkeeping

-- Anything the parser saw but could not resolve. A grounded answer must report
-- the relevant rows here alongside its conclusion; an impact analysis with 40
-- unresolved dynamic CALLs in scope is not a complete impact analysis.
CREATE TABLE IF NOT EXISTS unresolved (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER REFERENCES member(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,            -- dynamic_call|missing_copybook|missing_proc|
                                          -- unparsed_stmt|symbolic|missing_pgm|launcher_parm
    detail      TEXT,
    line        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_unres_kind ON unresolved(kind);

-- Line map from expanded source back to (member, line) so every citation
-- points at a real line in a real member, not at an expansion artefact.
CREATE TABLE IF NOT EXISTS expand_map (
    id          INTEGER PRIMARY KEY,
    program_id  INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    exp_line    INTEGER NOT NULL,
    src_member  INTEGER NOT NULL REFERENCES member(id),
    src_line    INTEGER NOT NULL,
    depth       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_expmap ON expand_map(program_id, exp_line);

-- LLM-written prose. Quarantined on purpose: never joined into a fact query,
-- always rendered with an "unverified" banner.
CREATE TABLE IF NOT EXISTS derived_summary (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    model       TEXT,
    generated_at TEXT,
    summary     TEXT,
    verified_by TEXT                      -- human initials, NULL until reviewed
);

-- A fetched library as the fetcher saw it: which members the host lists,
-- which arrived. "NOT FOUND" and "NOT FETCHED" are different answers.
CREATE TABLE IF NOT EXISTS library (
    id          INTEGER PRIMARY KEY,
    dataset     TEXT NOT NULL,
    folder      TEXT NOT NULL,
    fetched_at  TEXT,
    rc          INTEGER,
    expected    INTEGER,                  -- members the host listed
    present     INTEGER,                  -- files on disk
    complete    INTEGER,                  -- 1 = every listed member is on disk and rc = 0
    missing     TEXT,                     -- JSON list of listed-but-absent members
    stale       TEXT                      -- JSON list of local files the host no longer lists
);

CREATE TABLE IF NOT EXISTS build_run (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT,
    finished_at TEXT,
    root        TEXT,
    members     INTEGER,
    ok          INTEGER,
    partial     INTEGER,
    failed      INTEGER,
    tool_version TEXT,
    fingerprint TEXT,                     -- sha256 of the parser source: a change re-parses everything
    manifest_sha TEXT                     -- sha256 of the manifest used
);
