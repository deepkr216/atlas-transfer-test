# Mainframe Atlas

A deterministic fact index over a COBOL / JCL / IMS / DB2 / VSAM estate, built
with **zero model calls**, so that a language model can answer analysis,
design, development and testing questions from **facts it queries** instead of
**text it half-remembers**.

Standard-library Python only (3.9+). No `pip install`. Copy the folder, run it.

## Why this exists

The usual "index the folder and chat with it" approach hallucinates on a large
mainframe estate, and not because the estate is big:

1. **Fine-tuning / embedding does not store facts.** It teaches naming style.
   A model trained on the estate becomes fluent at inventing plausible program
   names, field names and job flows.
2. **Embeddings are near-useless on cryptic identifiers.** `WS-PLCY-STAT-CD-01`
   and `WS-PLCY-STAT-CD-02` are the same point in vector space; thousands of
   near-identical CRUD paragraphs collapse together; the retriever returns the
   *wrong but similar* member and the model reasons fluently about it.
3. **Silent zero-recall.** When retrieval brings back nothing relevant, the
   model fills the gap from memory instead of saying "not found". That *is* the
   hallucination mechanism.
4. **The facts are not in the text.** What a job does lives in control cards,
   PROC symbolics, `IKJEFT01`'s SYSTSIN, `DFSRRC00`'s PARM, GDG relative
   generations, PCB order, COPY REPLACING and the scheduler - none of which a
   chunk of COBOL contains.

So the answer is a parser, not a model. Atlas walks the folder, extracts hard
facts into SQLite with a line citation on every row, records everything it
could **not** resolve, and gives the model a small **fact pack** to reason
over. The model is demoted to a reader that cites; a mechanical gate rejects
any answer whose citations do not check out.

## Getting the code from the mainframe

The index is built from a folder; something has to fill that folder correctly
and repeatably. `sources.json` says where the automation looks: each source
maps a mainframe dataset to a local folder (PDS) or file (sequential), a kind,
a system, and whether it is the **production** copy. Zowe CLI does the
download; the manifest `build.py` needs is generated from the same file, so
the two cannot disagree.

```bash
python -m atlas.ui --config sources.json               # desktop UI (Tkinter, no installs)
```

or the same thing from the command line:

```bash
python -m atlas.fetch --config sources.json --init     # starter config, then edit it
```

```bash
python -m atlas.fetch --config sources.json --check    # is zowe on PATH, does --version work
```

```bash
python -m atlas.fetch --config sources.json --plan     # print every zowe command, run nothing
```

```bash
python -m atlas.fetch --config sources.json --all --build   # download everything, then re-index incrementally
```

The UI is a table of sources with Add / Edit / Remove, Plan, Fetch selected /
Fetch all, Build index / Rebuild from empty, Coverage, and a live log that
shows every command exactly as run. Shop-specific Zowe flags (a profile, an
encoding, `--preserve-original-letter-case`) go in `extra_args` — a config
edit, not a code change. See `sources.example.json`.

Re-indexing is **incremental**: unchanged members keep their facts, a changed
copybook forces every program that expands it to be re-parsed, and members
that disappeared are pruned. A full estate re-run after a small change takes
seconds, not minutes.

### The mainframe password

Run by hand, `zowe` asks for your password when the profile does not store
it. Run by the toolkit it cannot ask, so a fetch would create the folders and
download nothing - the log then says so and what to do. Two ways to give it:

- once, in the profile: `zowe config secure` (Zowe v2/v3; the password goes
  into Windows Credential Manager, never into a file), or
- for one session: the UI's **Sign in** button, or `--ask-password` on the
  command line. The password is passed to the `zowe` subprocess through its
  own `ZOWE_OPT_PASSWORD` environment variable for that run and is never
  written to `sources.json`, the log, or a crash file.

### Several departments, each with its own libraries

Every source row carries a `system` (the department). That one field drives
everything else:

- **Folders** — each department gets its own folder under `local_root`
  (`C:\estate\CLAIMS\PROD.CLAIMS.SRC`), so same-named members in two
  departments never collide on disk; the library folder name is still the
  dataset name, so classification and the manifest keep working. The
  folder between the estate root and the library folder **names the
  system** even without a manifest (`estate\GC\PROD.GC.SRC` → system `GC`;
  `estate\SHARED\...` → `SHARED`). A lower environment goes in its own
  system folder (`estate\GC-TEST\TEST.GC.SRC`): its programs expand its
  copybooks, and `diff GC/PGM GC-TEST/PGM` compares the two copies.
- **Bulk add** — paste a department's dataset list (one per line); kinds are
  inferred from the names (`SRC`, `COPYLIB`, `JCLLIB`, `PROCLIB`, `PARMLIB`,
  `PSBSOURCE`, `DBDSRC`, `BMS`, `MFS`, `CSD`, `STAGE1`, `CA7`…), and
  compiled libraries (`PSBLIB`, `DBDLIB`, `ACBLIB`, `LOADLIB`) are added
  **disabled** with a warning — there is nothing to parse in them.
- **System filter / Fetch system** — work on one department at a time; the
  others stay untouched.
- **Copybook resolution** — a CLAIMS program that says `COPY POLREC` gets the
  CLAIMS copy, never POLICY's: candidates are ranked `COPY … OF lib` › same
  department in its **declared order** › same department › authoritative ›
  same folder › first found — and the choice is written into the
  `ambiguous_copybook` note either way. The declared order is simply the
  order of a department's copybook rows in the table (Move up / Move down):
  that is the SYSLIB concatenation its programs compile against.
- **Cross-department flow** — `dataset X` shows each writer's and reader's
  department and says **Crosses departments** when they differ: a layout or
  value change to that file is an interface change, not an internal one.
- `program X` shows its department; `ambiguous` shows which departments hold
  each duplicate name. Same name in two departments is normal; same name
  with different content inside *one* department is the dangerous case.

Shared exports — the IMS stage-1, the CSD extract, the scheduler CSV — go
under a system of their own (`SHARED` in `sources.example.json`).

## Quick start

```bash
# 1. index the estate (source + documents; minutes for a few thousand members, hours for a large estate)
python -m atlas.build  C:/estate  --db atlas.db  --manifest manifest.json  --write-expanded out/expanded
#    a big estate: the same arguments under the watchdog - a build that goes silent (a parser
#    frozen inside a regular expression) is killed, the member in hand is recorded in
#    atlas-skip.txt, and the build restarts keeping what was parsed
python -m atlas.supervise  C:/estate  --db atlas.db  --manifest manifest.json
#    build or rebuild: WITHOUT --rebuild only new and changed files are parsed (and a toolkit
#    or manifest change re-parses everything by itself); WITH --rebuild the index is deleted
#    and every member parsed from zero - for the very first build only. After a stop, continue
#    WITHOUT it. The watchdog never passes --rebuild to a restart, and it refuses to start when the
#    command names fewer folders than the index holds (an --also left out would remove every
#    document from the index): add the folder, or --allow-prune if the folder really moved.

# 2. read the coverage report FIRST - it lists what the index cannot know
python -m atlas.query --db atlas.db coverage
python -m atlas.query --db atlas.db ambiguous          # duplicate member names -> fix the manifest

# 3. ask
python -m atlas.query --db atlas.db program  CLMPOST
python -m atlas.query --db atlas.db job      CLMNIGHT
python -m atlas.query --db atlas.db field    PM-POLICY-STATUS
python -m atlas.query --db atlas.db literal  E001               # where is this code set / tested / shown
python -m atlas.query --db atlas.db values   WS-GENDER-CD       # every value the code assumes (incl. undocumented)
python -m atlas.query --db atlas.db pair     WS-REL-CD WS-GENDER-CD   # cross-field rules (son must be male)
python -m atlas.query --db atlas.db messages GENDER             # message texts naming a rule
python -m atlas.query --db atlas.db screen   MEMMAP             # BMS map / MFS MID-MOD: fields, defaults, validating programs
python -m atlas.query --db atlas.db transaction MEMB            # CICS CSD / IMS stage-1: which program a transaction runs
python -m atlas.query --db atlas.db column   MEMBER_TBL.GENDER_CD   # DB2 column: written from / read into which fields, where
python -m atlas.query --db atlas.db copybook PMASTREC           # impact
python -m atlas.query --db atlas.db callers  RATECALC --depth 3
python -m atlas.query --db atlas.db dataset  PROD.POLICY.EXTRACT
python -m atlas.query --db atlas.db search   '"WS-PLCY-STAT-CD"'

# 4. hand a model a fact pack, not the repository
python -m atlas.query --db atlas.db pack CLMPOST > pack.md

# 5. gate the model's answer before anyone reads it
python -m atlas.verify_citations answer.md --db atlas.db
```

`selfcheck.py` runs the regression suite and a smoke build; run it after every
change to the toolkit and before copying it to another machine.

## What gets extracted

| Artifact | Facts | The trap it avoids |
|---|---|---|
| COBOL | PROGRAM-ID, paragraphs **and SECTIONs** (Area A), PERFORM/THRU, **GO TO / GO TO DEPENDING / ALTER / fall-through** edges (each kind labelled), `PERFORM n TIMES` never an edge, CALL literal **and** identifier (resolved via MOVE/VALUE/`SET 88 TO TRUE`, else *unresolved*) - every CALL in a sentence, USING lists cut at ELSE/END-xxx with `LENGTH OF`/`ADDRESS OF`/subscripts/`OF` qualifiers reduced, `ENTRY 'name' USING` as an alias, COPY / `EXEC SQL INCLUDE` / `++INCLUDE` / `-INC`, SELECT…ASSIGN→DD, `OPEN INPUT A B OUTPUT C` lists, WRITE/REWRITE keyed to the **file that owns the record**, SORT USING/GIVING and INPUT/OUTPUT PROCEDURE, EXEC SQL tables+host vars (INTO = write, `SELECT * INTO :group` expanded through the DCLGEN, FETCH ROWSET, `EXEC SQL CALL` as a stored-procedure call), CBLTDLI/AIBTDLI with the function taken from its VALUE field, parm-count first argument, CHNG message switches, `EXEC DLI` PCB(n)/SEGMENT/INTO/WHERE, every EXEC CICS resource (maps, START/RETURN TRANSID, TD/TS queues, containers, web services, COMMAREA as the positional argument, INTO/FROM/RIDFLD/RESP host fields), MQ calls with the **queue name and message layout**, every field reference with **read/write/test/display** mode, every **literal** with how it is used | Commented-out CALLs indexed as live; `WS-CALL-FLAG` matched as a CALL; multi-line statements split; `PROGRAM-ID.` on its own line; period inside PIC/literal; EXEC text read as COBOL (a file called `FILE`); `EJECT` swallowing the next paragraph; `AUTHOR. PAT O'BRIEN.` opening a literal that eats the program |
| Copybook | Field tree with **byte offsets/lengths** (COMP-3 packed, COMP 2/4/8, SIGN SEPARATE, **group USAGE inherited**, `PIC …DB` two bytes), REDEFINES held at the same offset, OCCURS DEPENDING ON flagged variable, anonymous `05 PIC X(3)` FILLERs, level 77 as its own root, 88-levels (`VALUE`/`VALUES ARE`), VALUE literals, fragments (05s without an 01) wrapped correctly | Test data of the wrong length; "change field X" without knowing what moves; `COMP` inside `WS-COMP-CNT` read as a usage |
| Expansion | Every program materialised with COPY/INCLUDE inline and REPLACING applied (pseudo-text as a character string - `==PM-== BY ==LK-==` renames the prefix; `LEADING`/`TRAILING`; `==01== BY ==05==` renumbers levels only), with a line map back to (member, line) and a **field alias** table so `field PM-POLICY-STATUS` finds the program's `LK-POLICY-STATUS` references | Fields renamed by REPLACING are invisible to grep; procedure copybooks attributed to the wrong member; a COPY word inside a DISPLAY literal expanded |
| DB2 | DCLGEN `DECLARE TABLE` columns (positional list behind `SELECT *`), qualified and unqualified names matched as one table, cursors, dynamic SQL flagged, stored-procedure calls, per-column lineage to host variables, CICS DB2ENTRY/DB2TRAN plans | `column X` saying "no references" for every program that reads the whole row; a cursor recorded as a table |
| CICS | CSD `DEFINE`/`LIST` for TRANSACTION, PROGRAM, FILE→DSNAME, **TDQUEUE→dataset**, DB2ENTRY/DB2TRAN, URIMAP→program; every resource block kept; online readers/updaters of a VSAM file in `dataset`; program↔map and transaction chaining from `EXEC CICS` facts | The fifty CICS programs that update a master file missing from its lineage; a transaction reached only by `RETURN TRANSID` listed as dead |
| JCL / PROC | JOB/EXEC/DD with continuations, inline control cards captured, symbolics resolved (EXEC override > PROC default > SET), GDG base split from `(+1)`, `//STEP.DD` overrides, **effective program** unwrapped from IKJEFT01/DFSRRC00/SORT/IDCAMS/IEBGENER/IEFBR14/DSNUTILB/FTP/NDM, sort-card **byte positions** | Steps attributed to TSO or the IMS region driver; producer/consumer linkage lost through GDG; `DISP` mistaken for direction |
| IMS | DBD segments, **FIELDs (start/bytes/SEQ), XDFLD secondary indexes, DATASET DD1** (GSAM = the JCL DD), several DBDs per member; PSB PCBs in **positional** order with PROCOPT, `LIST=NO`, `PROCSEQ`, `CMPAT=YES` / TP PCB → I/O PCB first; **every DL/I call resolved to its database and PROCOPT** through the PSB named by the DFSRRC00 PARM / stage-1 / convention, with the reasoning stored; GSAM ISRT/GN sets the JCL DD's direction; stage-1 decks inside JCL SYSIN, SPA/INQUIRY, DATABASE access | Off-by-one PCB → wrong database named; an extract written through GSAM with no writer |
| Screens | BMS maps (DFHMSD/DFHMDI/DFHMDF): field, position, length, attributes, INITIAL, PICIN/PICOUT, and the generated symbolic names (`GENDERI`/`GENDERO`) that programs actually reference; MFS (FMT/DFLD, MSG/SEG/MFLD): MID/MOD with each MFLD's **byte offset** in the segment, `ATTR=YES` bytes, DO/ENDDO repeats, TYPE inferred from `…FIP`/`…FOP`/`…MID`/`…MOD` labels when missing | Online validation invisible; screen defaults and labels missed by a message search |
| Documents | .docx/.xlsx/.pptx/.vsdx text, headings, tables, image manifests; PDF best-effort; legacy .doc/.xls reported | Docs indexed as if they were facts |

Every parse failure is recorded on the member; every unresolved reference is a
row in `unresolved`; every report ends with the unresolved items in its scope.
[ROADMAP.md](ROADMAP.md) holds the full coverage matrix - every artefact type
and developer question, what produces facts, what is text only, and what is
not built yet - from the 2026-09-12 coverage audit.

## Direction of I/O

`DISP` is serialisation, not direction. `DISP=OLD` is routinely an output
dataset. Direction is taken from, in order: the program's own `OPEN`
verb (joined through `ASSIGN`), the GDG relative generation, utility DD-name
conventions (SORTIN/SORTOUT, SYSUT1/SYSUT2), and only then `DISP=NEW/MOD` as a
weak hint. Every DD row records which signal decided it (`mode_source`).

## Symbolics, PROCs and where a file is created or used

A job is indexed twice: its literal statements, and its **effective steps** —
every `EXEC PROC=` replaced by the PROC's steps with symbolics resolved in JCL
precedence (`EXEC PROC=X,SYM=value` › PROC statement defaults › instream
`SET` - a SET value reaches the PROC only for a symbol the PROC statement
does not define; `jcl.PROC_DEFAULT_BEATS_SET` documents how to confirm this
with one job on your system) and
`//PROCSTEP.DDNAME` overrides and additions applied, `INCLUDE MEMBER=`
spliced in first, nested PROCs expanded, instream PROCs (`// PROC … // PEND`
inside the job) collected and used before cataloged ones. `&SYM` and
`&SYM.` forms are substituted and `..` collapsed; anything still containing
`&` afterwards is reported as `symbolic`, never guessed. GDG relative
generations are split off so `(+1)` and `(0)` join as one dataset.

Three more shapes that production JCL has and toy JCL does not:

- **Control cards in a library member** — `//SYSIN DD DSN=PROD.PARMLIB(SRTCLM)`.
  If `SRTCLM` is indexed (fetch the PARMLIB/CNTL libraries as `ctlcard`),
  its text becomes the step's cards exactly as if coded `DD *`: the TSO
  `RUN PROGRAM(...)` resolves, sort byte positions and IDCAMS operations are
  harvested. `program SRTCLM` says which jobs use the member. If the member
  is not indexed the job dossier says so ("card member NOT indexed").
- **`&&TEMP` datasets** exist only between the steps of one job. They never
  become `dataset` rows and `dataset` hides them, so two jobs that both use
  `&&SORTED` are never joined; `job X` lists them under "Job-local datasets".
- **Referbacks** `DSN=*.STEP.DD` (also `*.DD`, `*.STEP.PROCSTEP.DD`) are
  followed to the dataset the earlier DD allocated, after PROC expansion, so
  lineage has no hole at the job's own intermediate files. A referback to a
  `(+1)` reads the generation just created - it is not a second writer. A
  referback whose target does not exist is reported as `referback`.

And the details that decide whether a job dossier is right or merely
plausible: the operand field ends at the first blank (trailing comments
are not part of a DSN), quoted PARMs continue at column 71, a member with
two JOB cards is two jobs, `JOBLIB` reaches every step without its own
STEPLIB (`[joblib]`), steps inside `// IF (S1.RC > 4) THEN` are marked
**runs only when** and the JOB-card `COND` is shown, `PARM.PS010=` /
`COND.PS020=` / bare `PARM=` on `EXEC PROC` reach the right PROC step,
override concatenations replace entry by entry and `DD DUMMY` really
removes the PROC's dataset, an `EXEC PROC=X,HLQ=` override never changes
the job's own later steps, nested PROCs receive values not `&SYMBOL` text,
`IEFBR14` steps `delete` / `alloc` and are never writers, `DUMMY` is the
first operand (not a substring) and `NULLFILE` is DUMMY, TSO `CALL
'LIB(PGM)'` and a second `RUN PROGRAM` are seen, `RUN PROGRAM(DSNTIAUL)`
is a utility not an application, `ULU`/`UDR` regions name a **DBD** not a
PSB, Easytrieve (`EZTPA00`) steps expose their files and byte-position
fields, batch FTP reads `//INPUT` with the userid/password **redacted
before storage** and `put`/`get` become dataset rows, Connect:Direct
`SUBMIT PROC=` follows the process member, `PROD.G.G0012V00` joins its
GDG, run-time symbols (`%%ODATE`, `&LYYMMDD`) become `<VAR>` and are
reported. PROCs and INCLUDEs are resolved by the job's `JCLLIB ORDER`,
then its department, then the manifest - two departments each owning a
`NIGHTLY` PROC no longer cross - and an undecidable choice is reported as
`ambiguous_proc`. The IMS/DB2 system PROCs that are never in the estate
folder (`EXEC DLIBATCH,MBR=CLMPOST,PSB=CLMPSB`, IMSBMP, DBBBATCH,
DSNUPROC) are synthesised from their overrides and reported as
`proc_synthesised`; `SYSOUT=(A,INTRDR)` becomes a scheduler edge to the
submitted job; `DD PATH='/u/...'` files carry their PATHOPTS direction;
ICETOOL `TOOLIN`/`xxxxCNTL`, `OUTFIL FNAMES=`, `JOINKEYS F1=/F2=` and
`SYMNAMES` symbols give sort steps their DD roles and byte positions
(`c:p,l` items included); `LOAD ... INTO TABLE` / `UNLOAD` / DSNTIAUL SQL
join tables to the flat files they fill or drain (`table X` lists them);
IDCAMS `RECORDSIZE`, `KEYS`, `LIMIT` and `RELATE` are kept on the dataset
so a copybook length can be checked against the cluster.

Because of that, `dataset PROD.CLM.MASTER` lists the *jobs* that create and
read it (`NIGHTJOB NIGHT.PS010`, direction with its source), not just a PROC
with `&HLQ` in it. IDCAMS steps add `create` / `delete` / `input` / `output`
rows from their `DEFINE`, `DELETE` and `REPRO` cards, so a VSAM file's
lineage starts where it is defined. `program X` shows the job-level rows and
falls back to the bare PROC only when no indexed job expands it.

## Fields, columns and messages

- `table [QUAL.]NAME` — who creates/reads/updates/deletes a DB2 table
  (CRUD per program with cites), its declared columns (DCLGEN/DDL), cursors,
  dynamic-SQL count, CICS plans.
- `dbd NAME` — an IMS database: segments and fields, XDFLD indexes, the
  PSBs/PCBs addressing it with PROCOPT, the programs whose DL/I calls
  resolve to it (and whether they update), utility jobs, online access.
- `segment NAME` — who touches a segment: PCBs sensitive to it and the
  programs using them; `EXEC DLI SEGMENT()` is exact.
- `layout COPYBOOK|01 [--program PGM]` — the byte layout (offset, length,
  PIC, usage, OCCURS/ODO/REDEFINES, 88 values, record length) from the
  parser's numbers; with `--program` the 01 as that program sees it after
  REPLACING. This is what test data and interface contracts are built from.
- `crud PGM… | --job JOB | --copybook X | --system S` — one matrix: programs ×
  datasets / DB2 tables / IMS databases / CICS files with C/R/U/D and a cite
  per cell (the design-document table).
- `conditions PGM` — every IF/WHEN/UNTIL with the field and literal or 88 name
  it tests (88s expanded), per paragraph, plus one negative case per field.
- `paragraph PGM NAME|line` — who reaches a paragraph and how (PERFORM, GO TO,
  fall-through, THRU range, section), what it performs/calls/does, its source.
- `walk PGM [--budget N] [--from PARA] [--depth D] [--no-source]` — the
  program in **reading order**: the entry paragraph first, then each
  paragraph the first time control reaches it (PERFORM returns at the end of
  its target or THRU range, GO TO does not return, fall-through, performed
  SECTIONs, ENTRY points as extra roots, DECLARATIVES listed not walked),
  each with its resolved facts (call targets, tables, database + PROCOPT,
  CICS resources, files, codes set) and its source with original line
  numbers - copybook paragraphs cite the copybook; the fields the PROCEDURE
  DIVISION names, and only those, with offsets; the paragraphs nothing
  reaches. A budget keeps the order and the facts and drops source from the
  end. This is the program-by-program read, done for the model.
- `diff OLD NEW [--budget N]` — two versions of a member, the production
  copy and the changed one (`GC/CLMPOST GC-TEST/CLMPOST`, `NAME@LIBRARY`, or
  the path of a file just downloaded): the paragraphs, calls, copybooks,
  tables, files, DL/I and CICS facts that changed, the fields added or
  redefined and the fields that only **shifted** (grouped: "2 fields shift
  +1 byte from X to Y"), the paragraphs with changed lines, then the changed
  lines of both sides with their own line numbers - citable on either side
  through the system prefix. Columns 1-6 and 73-80 are ignored. `diff` with
  no member (`diff --system GC-TEST`) lists every member whose copies differ:
  the contents of a release.
- `callers X --args` — every call site with its full USING list against the
  callee's LINKAGE; a count mismatch is flagged (the S0C4 check).
- `interfaces [--system S] [--dsn X]` — what leaves and enters the
  mainframe: FTP / Connect:Direct steps with the dataset, MQ queues with the
  message layout, IMS message switches, CICS TD queues and web entry points,
  and the peers you declare in the manifest (`"external_interfaces":
  [{"kind":"ndm","peer":"REINSURER-X","direction":"out","dataset":"PROD.POLICY.EXTRACT"}]`)
  - the one fact no source file states. `dataset X` says when X crosses the
  boundary.
- `field NAME` — definitions with offsets, references by mode
  (write/read/test/display), 88s, literals, screen fields, sort cards, the
  **DB2 columns** it is loaded from or stored to, and the **IMS DL/I calls**
  whose I/O area contains it (GU/GN read into it, ISRT/REPL write from it).
- `column TABLE.COL` — every program that writes the column (INSERT/UPDATE
  from a host variable, with where that host variable was set), reads it
  (SELECT INTO / FETCH INTO, with where the value goes next) or filters on
  it. Pairing is positional where SQL is positional (select list ↔ INTO,
  INSERT columns ↔ VALUES, cursor select list ↔ FETCH INTO).
- `messages PATTERN` — literals, texts assembled from consecutive FILLER
  VALUEs, messages built at run time by `STRING`/`DISPLAY` (stored as
  templates such as `INVALID GENDER <WS-GENDER-CD> FOR MEMBER <WS-MEMBER-ID>`,
  multi-line statements included), messages assembled by MOVEs into sibling
  fields of one group, screen labels and defaults.

## Documents and the pictures inside them

The documentation folder is usually not a mainframe dataset — it already sits
somewhere on the laptop. Name it under **Document folders** in the UI (or
`extra_roots` in `sources.json`, or `--also DIR` on the build) and the build
indexes it alongside the code: `.docx / .xlsx / .pptx / .vsdx` text, headings,
tables and slide notes; `.pdf` best-effort; `.txt/.md/.html`. Legacy `.doc /
.xls / .ppt` cannot be read directly: **Prepare documents** in the UI (or
`python -m atlas.convert <folder>`) first extracts every `.zip` under the
document folders — subfolders and zips inside zips included — into a
`<name>.unzipped` folder beside the archive, then saves every `.doc / .xls /
.ppt` as `.docx / .xlsx / .pptx` beside the original, using the Office
installed on the laptop (nothing installed, nothing deleted; LibreOffice is
used if Office is absent). The build then indexes the copies and skips the
archives and the originals; a copy older than its edited original is
reported as STALE (`--refresh` remakes it).

**PDFs.** A PDF with real text is read directly (simple fonts come out
clean; the coverage report flags the ones that used CID/Identity-H fonts).
A **scanned** PDF, or one whose fonts defeat the extractor, is handled by
`OCR images`: its pages are rendered with the Windows PDF renderer
(`Windows.Data.Pdf`, part of Windows 10/11 — nothing to install) and read by
the same OCR engine as the pictures; each page becomes a citable section
1001+ of that document (`--pdf-pages all` renders every PDF, `none` turns it
off; 400 pages per document at most).

**Pictures are read, not just counted.** `OCR images` (UI) or
`python -m atlas.ocr --db atlas.db --out out/images` pulls every image out of
the documents - including the metafiles (EMF/WMF) that Word, Excel and Visio
store a pasted diagram as, which are drawn onto a bitmap first because the
engine cannot decode them, and every page of a multi-page TIFF - and runs the
OCR engine that ships with Windows 10/11 (a big scan is read in tiles, so its
small print is not lost to the engine's own downsampling; a page it refused on
an earlier run is tried again, and rendered again when its file came out empty;
a picture the document declares but stores no data for - the red X Word shows -
is reported once and counted as empty, not failed)
(`Windows.Media.Ocr`, driven through PowerShell) — no install, no network,
**no model tokens**. The recognised text becomes a section of the document
(`image 1`, `image 2`…), searchable and citable. Pictures OCR cannot read
(hand-drawn diagrams, photos of whiteboards) are listed with their extracted
path; those are the few worth a vision-model call, chosen by hand.

**Recorded sessions are read too.** A knowledge-transfer or outage
walkthrough recording holds what nobody wrote down.
`python -m atlas.video "C:\Recordings" --out "C:\docs\Video transcripts"`
reads every .mp4/.m4v/.mov/.wmv/.avi under the folder with what Windows
already has - a frame every 10 seconds through the same OCR engine (a screen
shown for a minute is written once, at the time it appeared), and what was
said from the meeting's own caption file beside the recording (`<name>.vtt`
or `.srt`: the transcript Teams or Stream makes - download it) - and writes
`<name>.video.docx`: one section per two minutes, every line stamped
`[00:12:05] SAID:` or `[00:12:10] SCREEN:`. Then run your usual build
command - the same one as always, with its `--also` folders, and `--out`
must be one of those folders - and it is indexed as document `<NAME>.VIDEO`,
found by `docs TERM` and cited as the report prints it
(`[[KT-SESSION.VIDEO 3 "CLMPOST"]]`); no parser changes, so no re-parse. A
12-minute 788 MB test recording took 28 seconds. What the engine reads off a
recorded screen is checked against the index (`--db atlas.db`, the default
when the index is in the folder): a shared mainframe screen inside a
recording is small and blurred, and the engine invents where it cannot read
- `000100` comes back as `eeeløø`, `WS-RESTART-FLAG` as `KS-RESTART-FLAG` -
so every name-shaped piece is kept only when the index, a keyword list or a
word list knows it, a misread name is snapped to the real one, an unknown
one is marked `?`, and a line with nothing recognisable is dropped; the
transcript says how many lines were kept and dropped. Expect a full-screen
emulator in a 1080p recording to read mostly, and a shared window in a 720p
recording to read in fragments. `--keep-frames` saves a dozen frames beside
the recording with the text read from each, before and after the check, so
anyone can see what the toolkit saw. A change of a few characters on a
screen already written is not written again. Without a caption file the speech is NOT transcribed and
the transcript says so: the speech recogniser Windows ships reads synthetic
speech well and real meetings badly - several voices, room noise, accents and
mainframe words come back as fluent sentences nobody said - so it runs only
on request (`--speech-recogniser`) and its lines are labelled unreliable. A
Teams download whose name differs from the recording's is matched to its
caption file when they are alone in the folder, and a caption file on its
own, without its recording, is read too. A recording OneDrive holds only as a placeholder
('Free up space') is downloaded on its own, read, and handed back to
OneDrive, so the folder never needs the disk space. Keep the
recordings and caption files outside the folders the build reads: a video
inside them is read in full on every build only to be skipped, one over 300
MB is listed as a problem, and a caption file is indexed as whatever its
words look like. A recording with nothing readable gets a one-line
transcript saying so, so it is not read again (`--refresh` reads it again);
one the Windows media editor will not open as it is (some screen recorder
MP4s, some Teams downloads) is converted to a plain MP4 first and read from
that; one nothing can open is retried next time and the message says why. A transcript is what was shown and said, never a fact
about what runs.

**Spreadsheets and tables are rows, not counts.** A workbook's tab is a
section whose text is its rows — `row 12: TC-GEN-01 | Add gender N | PASS`,
with the sheet's own row numbers — so a QA results workbook is searched
(`docs TC-GEN-01`), read (`doc QAREL12 --sections 2`) and cited
(`[[QAREL12 2 "TC-GEN-01"]]`) tab by tab; a tab of thousands of rows is
cut into parts between rows. Word and PowerPoint tables are rendered the
same way inside their section. Every picture is anchored to where it sits
— the tab, the slide, or the Word heading above it — and the OCR section's
heading says so (`image: image3.png (sheet: Gender Tests)`); `doc NAME`
lists the pictures by place, `doc --list [FOLDER]` the indexed documents.

- `docs TERM` — every document mentioning a program, job, field, code or
  phrase, with the section (or image) and an excerpt.
- `doc DOC [--sections 3-5 | --grep TERM] [--budget N]` — one document: its
  outline (section numbers, headings, sizes), or the **full text** of chosen
  sections by number or by the words they contain. Long heading-less
  documents (a book-sized PDF or text file) are cut at build time into parts
  of about 4,000 characters, so a business rule always sits in a section the
  model can read whole and cite exactly.
- `images [DOC]` — documents with pictures, how many were extracted, read,
  and had text.
- `program`, `job` and `field` dossiers end with **Documents mentioning it**.

Documents are indexed as **prose, never as facts**: they say what was
*intended*; the code says what *runs*. A document is cited as
`[[DOCNAME 3 "token"]]` (section 3; images are 1001+), and the gate checks the
token against that section. Where a document and the code disagree, the
answer must quote both and say so.

## The four-names problem

PDS member name, `PROGRAM-ID`, CSECT and load-module/alias routinely differ.
Atlas keys members by path, stores `PROGRAM-ID` separately, and never merges
by name. Members with the same name and different content are listed by
`ambiguous`; declare the production libraries in `manifest.json` or the index
cannot know which copy is real.

## Working with a small token budget

The model must never read the repository. It reads **packs**:

1. `query pack PGM` (or `program`/`job`/`field`/`literal`) - a few hundred
   lines of facts with `MEMBER:line` citations and the exact evidence lines.
2. The model answers using **only** the pack, citing `[[MEMBER line "token"]]`.
   Anything it cannot cite it must mark `UNVERIFIED` or `INSUFFICIENT EVIDENCE`.
3. `verify_citations.py` rejects the answer if any citation fails.
4. `query cite MEMBER a-b` fetches more lines only when the pack was not enough.

With this loop a typical analysis question costs one model call over ~2-5k
tokens of input. Bulk work (indexing, impact sets, dossiers, test-condition
inventories) costs zero.

`templates/CLAUDE.md` is the operating contract to put at the root of the
estate folder for a coding agent; `templates/PLAYBOOKS.md` has the per-task
procedures (impact analysis, job dossier, error-code trace, design doc,
code-generation bundle, test conditions, abend triage).

### From VS Code

Open the toolkit folder as the workspace, open the chat, type `/atlas` and
ask in your own words: the model runs the queries itself, answers with a
citation on every fact and ends with the gate's verdict. That is the whole
loop for most questions - see [docs/VSCODE.md](docs/VSCODE.md). The pieces
below do the same step by step, when you want to choose the report yourself:

- `Terminal > Run Task > Atlas: pack NAME -> work/pack.md` (and `field`,
  `layout`, `job`, `crud`, `conditions`, `literal`, `value domain`,
  `interfaces`, `coverage`, `whole member`, `paragraph`) - every report is
  written with `--out` into `work/` as UTF-8. Never use the shell's `>` for
  a report: PowerShell 5.1 writes UTF-16 and the model reads garbage.
- For a release hand-over, one task does the gathering:
  `Atlas: handover pack` (`python -m atlas.handover --system GC-DEV --docs
  PLAN,STORIES,QA...`) writes every report the transition document needs
  into `work/handover.md` in the right order - release list, a diff per
  changed member, the walk of each changed program's new copy, copybook
  users and layouts, crud and jobs, the documents' sections - and creates
  `work/release-facts.md` from the template for the facts no file holds.
  Its first lines give the size in tokens. Then `/atlas-handover` in ask
  mode is a single request, even on a laptop with a small token quota.
- In Copilot Chat type `/atlas-answer` (or `/atlas-program`, `/atlas-impact`,
  `/atlas-job`, `/atlas-trace`, `/atlas-populate`, `/atlas-abend`,
  `/atlas-values`, `/atlas-tests`, `/atlas-design`, `/atlas-handover`): the prompt attaches
  the `work/` files and carries the rules. `.github/copilot-instructions.md`
  is the contract Copilot loads by itself; for another chat tool the same
  requests are in `templates/REQUESTS.md`.
- Save the answer as `work/answer.md`, `Run Task > Atlas: verify`. On FAIL,
  `/atlas-fix` returns the corrected answer.

## Freshness and hygiene

An answer is only as current as the index, so every pack and `coverage`
start with **when the index was built**, warn when a build started and
never finished, and warn when a fetched library is incomplete. The fetch
reconciles each PDS folder with the host's member list (`zowe zos-files
list all-members`): a download that stopped part-way is reported
`INCOMPLETE n/m`, the listed-but-missing members are named, local members
the host no longer lists are moved to `<folder>/.stale/`, and the record
(`.atlas-library.json`) is loaded so `program X` answers **NOT FETCHED**
instead of NOT FOUND. The build re-parses everything when a **parser**
module changed (`git pull` that touches `cobol.py`, `jcl.py`, `docs.py`… -
never for a query, prompt or UI change) or the manifest changed, re-parses
every job when a PROC / INCLUDE / card member changed, announces every step
with the time (`== inventory`, `== removing the old facts of N member(s)`, `== parsing`, `== post: ...`),
reports each folder when it is done and each kind of member when it is done, prints every 10 s
how many members are parsed, the member in hand with its size, the rate and the **time left at that rate**,
stops cleanly on **Ctrl+C** (what was parsed is kept; the same command
continues from there), prints every problem with the time (a parse failure with the toolkit line to fix), writes them all to `atlas-problems.txt` and lists them again at the end of the build, stops once with a plain message when the index cannot be written (disk full, locked), gives up on one member after **15 minutes**
(`--member-limit`; the member is recorded as failed with the reason, its
half-written facts dropped, and it is not retried until the parser changes)
and, when a member cannot even be interrupted, stops and names it so the
file can be moved aside and reported; after 5 minutes on one item it
writes the exact position (toolkit line numbers, nothing from the estate)
to `atlas-stuck.txt` for sharing, and `python -m atlas.diag --doc FILE`
runs one document through the same steps in the foreground with timings
and a position dump every 30 s (the inventory step - reading and
classifying every file - has the same guard: a file still being read after
30 s is named, one over 15 minutes is recorded and skipped, and a text
file over 32 MB is a document, never decoded as a member), never indexes
its own outputs
(`.atlas-output` marker, `*.exp.cbl`), types empty and comment-only members
as `empty`, refuses a program row to a member with no `PROGRAM-ID` or
DIVISION header, re-types a card deck found in a JCL library as `ctlcard`,
never files a compiler listing as a program or a mainframe member as a
document, keeps a hand-written `manifest.json` (the generated one goes to
`manifest.generated.json`), and never prints a `--password` / token from
`extra_args` into a log.

## What Atlas is not

- **Not a compiler.** Extraction is grammar-aware regex over properly
  tokenised source. It is deliberately conservative and reports what it
  skipped, but a compiler listing (LIST/MAP/XREF) is the authoritative expanded
  source and cross-reference - index listings when you have them.
- **Not a scheduler.** Batch flow lives in CA-7/Control-M/TWS. Export it to CSV
  and load it with `--sched`; until then "dead job" and "what runs before"
  are unanswerable, and the reports say so.
- **Transaction routing needs the exports.** BMS/MFS screens and the CICS
  CSD (DFHCSDUP `DEFINE` deck or `LIST` report, also when embedded in a JCL
  SYSIN) and IMS stage-1 (`APPLCTN`/`TRANSACT`) are parsed; if those exports
  are not in the folder, "which transaction runs this" is unknown and
  `coverage` says so. IMS program names are assumed equal to PSB names
  unless `GPSB=` was used — recorded on every such row.
- **Not modelling** SYNC alignment slack, GO TO in dead-paragraph analysis
  (never-PERFORMed paragraphs are *candidates* only), dynamic SQL targets, or
  targets read from control tables (these appear as `unresolved`).

## Repository layout

```
atlas/
  reader.py            fixed-format reader: cols 7/72, ragged margins, continuations, statement splitting
  classify.py          content-sniffed member typing (extensions are hints only)
  copybook.py          field tree + byte offsets + 88s
  cobol.py             program facts: calls, copies, files, SQL, DL/I, field & literal refs
  expand.py            COPY/INCLUDE expansion with REPLACING and a line map
  jcl.py               jobs/steps/DDs, symbolics, effective program, direction, sort cards
  ims.py               DBD / PSB (HLASM macro format), PCB positions
  docs.py              docx/xlsx/pptx/vsdx/pdf/html/text extraction (stdlib only)
  schema.sql           the fact store
  build.py             folder -> atlas.db
  query.py             questions -> markdown with citations
  verify_citations.py  the gate
templates/             CLAUDE.md operating contract, PLAYBOOKS.md, REQUESTS.md (chat requests as text)
.vscode/tasks.json     Run Task > "Atlas: ..." - queries into work/, the gate, the UI
.github/               copilot-instructions.md (the contract Copilot loads), prompts/atlas-*.prompt.md (/atlas-... in chat)
docs/                  FieldManual.html (offline manual), VSCODE.md (the loop in VS Code)
work/                  reports and answers written by the tasks; git-ignored
tests/                 regression suite; every bug found becomes a fixture + assertion (see LESSONS.md)
selfcheck.py           run before trusting or copying the toolkit
manifest.example.json  how to declare production libraries
```

## Getting it onto a locked-down machine

The repository is plain files. Copy it (git bundle, zip, USB - whatever policy
allows), run `python selfcheck.py`, then `python -m atlas.build`. The database
never leaves the machine it is built on. Nothing here makes a network call.
