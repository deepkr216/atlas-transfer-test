# Playbooks

Each playbook is: the queries to run (free), what to hand the model (small),
the output template (fixed), and the gate (mechanical). The model never sees
the repository; it sees packs.

Common output skeleton for every playbook:

```
## FACTS            (cited: [[MEMBER line "token"]])
## INFERENCE        (labelled reasoning over the facts)
## UNRESOLVED IN SCOPE   (copied verbatim from the query output)
## NOT SEARCHED     (what was out of scope)
## HUMAN MUST VERIFY
```

Gate for every playbook: `python -m atlas.verify_citations answer.md --db atlas.db`
must print `RESULT: citations verified` (warnings allowed only with an
explanation in the answer).

---

## A1. Impact of changing a field

Question shape: "If I change `PM-POLICY-STATUS` (PIC/length/values), what breaks?"

```
python -m atlas.query --db atlas.db field    PM-POLICY-STATUS      > f.md   # --all for every site
python -m atlas.query --db atlas.db copybook PMASTREC              > c.md   # copies grouped by resolved version
python -m atlas.query --db atlas.db layout   PMASTREC              > y.md   # the byte layout (record length!)
python -m atlas.query --db atlas.db crud     --copybook PMASTREC   > x.md   # who creates/reads/updates/deletes
python -m atlas.query --db atlas.db literal  AC                    > l.md   # for each 88 value in play
python -m atlas.query --db atlas.db interfaces --dsn PROD.POLICY   > i.md   # does the record leave the mainframe?
python -m atlas.query --db atlas.db flow PM-POLICY-STATUS --program CLMPOST      > w.md   # where its VALUE goes, per writing program
python -m atlas.query --db atlas.db flow PM-POLICY-STATUS --program CLMPOST --up > u.md   # where it comes from
```

Hand the model: `f.md`, `c.md`, `y.md`, `x.md`, the relevant `l.md`s, `i.md`,
`w.md` / `u.md` for each program `f.md` lists as a writer, and the change
request - or one `pack copybook PMASTREC --budget 12000`.
`field` follows COPY REPLACING renames (`LK-POLICY-STATUS`), answers an 88
name, and never drops a program silently. `flow` follows the value by bytes:
MOVEs (group moves cut to the target), the record to the dataset and every
program that reads those bytes, CALL USING positions into the callee and back,
DB2 columns to their readers; each branch ends with a fixed `[end: ...]`
reason the model repeats, never smooths over.

The model must produce, in FACTS: (1) every copybook copy and its offset/length
for the field, flagging version skew; (2) every program including each copy,
with REPLACING noted; (3) every job/step running those programs; (4) every
dataset those steps write and who reads it; (5) every sort/control card whose
byte range overlaps the field; (6) callers of the including programs (LINKAGE
may carry the record). In INFERENCE: whether record length changes (then VSAM
DEFINE RECORDSIZE, DCB/LRECL, VB RDW +4, downstream fixed-width parsers,
non-mainframe consumers are all in scope), and which programs need
RECOMPILE-ONLY vs SOURCE-CHANGE vs BIND vs DATA-CONVERSION.

HUMAN MUST VERIFY: unresolved dynamic CALLs in scope, copies not marked
authoritative, any consumer outside the index, every `flow` branch that ends
at a width/node/hop cap or says HUMAN MUST VERIFY.

## A2. What does this job do

```
python -m atlas.query --db atlas.db job  CLMNIGHT > j.md
python -m atlas.query --db atlas.db pack CLMNIGHT > p.md    # pack job: the FULL control cards, cited
```

The model narrates step by step from `j.md` / `p.md` only: effective program
(not `PGM=`), what the control cards make the step do (quote them - cards in
`PROD.PARMLIB(MEMBER)` are in the pack too), datasets in/out with direction
source, GDG generation, COND/IF flow (**runs only when** marks the
error/backout steps), JOBLIB, the jobs it submits through INTRDR. `EXEC PROC=` steps are
shown as their **effective steps** (`NIGHT.PS010 (from PROC NIGHTLY)`) with
this job's symbolics and `//STEP.DD` overrides applied — those rows, not the
PROC member, carry the real dataset names. IDCAMS steps show `create` /
`delete` / `input` / `output` rows from their cards. If a step runs a PROC not
in the index, the step's contents are UNKNOWN; a DSN still containing `&`
is listed under `symbolic` in UNRESOLVED. If no scheduler is loaded,
predecessors are UNKNOWN. All of it is stated, not glossed.

## A3. Trace an error / status code

Question shape: "Where does `E001` come from and where is it shown?"

```
python -m atlas.query --db atlas.db literal E001 > l.md
```

`l.md` already contains: where the literal is defined (VALUE / 88 - and, for a
code kept in a message table, the text beside it), set (MOVE), tested (IF /
WHEN), displayed, and where the value flows after being set - positionally
through `CALL ... USING` (or a CICS LINK's COMMAREA) to the callee's LINKAGE,
back to callers, and through file records to other programs at the same
**bytes**. The model turns that into a chain with citations, and lists routes
it could not follow (DB2/IMS/MQ, group-level MOVEs, unresolved CALLs).
`callers <PGM> --args` shows every call site with its USING list when the
chain crosses a subroutine.

If `l.md` says the literal is not found: the code may be built (STRING,
arithmetic), read from a table, or spelled differently. Say so; do not guess a
program.

## A4. Where is this field populated / trace a policy number end to end

```
python -m atlas.query --db atlas.db field POL-NO > f.md         # 'write' references, ranked
python -m atlas.query --db atlas.db pack  <each writing program> > p.md
```

Writers include MOVE/COMPUTE targets, `SELECT ... INTO`/`FETCH ... INTO`,
`READ ... INTO`, `CALL ... USING` (by reference), group-level writes. For the
end-to-end trace, walk dataset producers/consumers (`dataset`) and CALL
positions; every hop is a FACT with a citation or it is a gap.

For a DB2 column: `column TABLE.COL` gives every program that writes it
(INSERT/UPDATE from a host variable — and where that host variable was set),
reads it (SELECT INTO / FETCH INTO — and where the value goes next) or
filters on it; `SELECT * INTO :DCLGEN-group` is expanded column by column.
`table [QUAL.]NAME` gives the CRUD per program plus the batch LOAD/UNLOAD
steps. `field <host-var>` shows the same from the COBOL side. For an IMS
field: `field` lists the DL/I calls whose I/O area holds it (GU/GN read into
the area, ISRT/REPL write from it) with the database and PROCOPT already
resolved through the PSB; `dbd <NAME>` / `segment <NAME>` give every program
touching the database or segment and whether it updates. When the question
is "where is the gender field of segment X populated" across databases that
each name the byte differently, the field NAME is the wrong handle: run
`segment <SEG> --dbd <DBD>` - it puts each DBD FIELD at its bytes in every
program's I/O area (`GENDER (bytes 45-45): PGMA DEP-GENDER via copybook
DEPSEG ... - stored by this program; PGMB WS-DEP-SEX via copybook DEPREC2
... - layout differs from PGMA's; PGMC: no field at that offset`). The
programs marked "stored by this program" are the writers; then `field` /
`flow --up` on each program's own name for the byte.

## A5. Abend triage

Inputs the human must paste: the JESMSGLG/JESYSMSG lines, failing job/step,
abend code or SQLCODE/IMS status. Then:

```
python -m atlas.query --db atlas.db job  <JOB>     > j.md
python -m atlas.query --db atlas.db pack <PROGRAM> > p.md
```

The model gives at most three ranked hypotheses, each with a disconfirming
check. Per code: S0C7 - which COMP-3/numeric field at the reported offset
(needs the compile listing to map offset→statement; without it say "cannot
localise"; `layout` gives the field lengths, `conditions` the tests around
them); S0C4 - `callers <PGM> --args` flags every call site whose USING count
differs from the callee's LINKAGE; SQLCODE -805/-818 - bind/timestamp, not
code (`transaction` shows the DB2 plan of a CICS transaction); -911/-913 -
contention, look at the concurrent jobs; IMS AI/AJ/AM - the `program`
dossier already shows each call's database and PROCOPT: an update function
on a PROCOPT=G PCB is the answer; GE/GB - SSA/path, not a bug. `paragraph
<PGM> <line>` explains the paragraph around any line the dump names.

## A6. Dead code

```
python -m atlas.query --db atlas.db dead > d.md
```

Candidates only. The report states whether the scheduler and online
definitions were loaded and how many dynamic CALLs are unresolved; the model
repeats those caveats and recommends the export that would close them. Never
recommend deletion from this report alone. A program reached only by
`START`/`RETURN TRANSID`, an IMS message switch, an ENTRY alias, a
stored-procedure CALL or a synthesised system PROC is **not** a candidate.
The same report lists datasets written but never read (and read but never
written), copybooks nobody COPYs, and jobs absent from the scheduler export;
`paragraph <PGM> <NAME>` says whether a paragraph is reached by PERFORM,
GO TO, fall-through or a THRU range before anyone calls it dead.

For an online program, `transaction <PROGRAM>` shows which CICS transactions
(CSD) or IMS transactions (stage-1 `APPLCTN`/`TRANSACT`) route to it; if the
CSD extract or stage-1 source is not in the folder, the routing is UNKNOWN,
not absent - fetch them (`sources.json` kinds `csd` / `imsgen`).

## A7. Value-domain change (a new code value, or codes merged)

Question shape: "Gender gets a new value N; son (3) and daughter (4) both
become 4; spouse sometimes uses 5. What has to change?"

This is not a search for the fields - it is an inventory of every place a
specific VALUE is assumed. Four queries, run for each affected field in each
system's copy of it:

```
python -m atlas.query --db atlas.db values   WS-GENDER-CD           > v1.md   # observed domain
python -m atlas.query --db atlas.db values   WS-REL-CD              > v2.md
python -m atlas.query --db atlas.db pair     WS-REL-CD WS-GENDER-CD > p.md    # cross-field rules
python -m atlas.query --db atlas.db messages GENDER                 > m1.md   # message texts
python -m atlas.query --db atlas.db messages RELATION               > m2.md
python -m atlas.query --db atlas.db field    WS-GENDER-CD           > f.md    # copies, skew, refs
python -m atlas.query --db atlas.db copybook <record copybook>      > c.md    # jobs, datasets, readers
python -m atlas.query --db atlas.db values   GENDER                 > s.md    # the SCREEN field: default, validators
python -m atlas.query --db atlas.db screen   <map or MID/MOD name>  > s2.md   # every field on the screen
python -m atlas.query --db atlas.db table    <DB2 table holding the code>  > t.md   # who INSERTs/UPDATEs the column, declared type
python -m atlas.query --db atlas.db interfaces --dsn <extract DSN>  > i.md   # non-mainframe readers of the value
```

Online: `values` on the *screen* field name (BMS `GENDER`, referenced by
programs as `GENDERI`/`GENDERO`; MFS by MFLD name and byte offset) lists the
screen default and the programs that validate the input - that is where a
new gender value is rejected first. `screen` lists every field on the map or
message with length, default and offset, so the picklist/validation change
can be scoped per screen.

What each inventory gives the design:

- **`values`** - every value the code knows: documented by an 88-level, set,
  tested (including tests through 88 names that never mention the value),
  used in SQL predicates/SET/INSERT on the matching column, compared by
  sort INCLUDE/OMIT cards. Two lines matter most: **USED BUT NOT DOCUMENTED**
  (the special-case `5` nobody wrote down) and **documented but never used**.
- **`pair`** - every statement where the two fields (or their 88s) are used
  together: the "son must be male" validations and the "if relationship 3
  then gender M" derivations. Every row is a rule that the new value
  invalidates or redefines.
- **`messages`** - every message literal naming the old rule, with the field
  it is moved to; plus documents mentioning the term.
- **`field` / `copybook`** - the physical spread: copies per system with
  offset/length (skew!), programs, jobs, datasets written and who reads them,
  callers. The value change does not move bytes, so file contracts survive -
  but every reader that tests the old values is on this list.
- **`table`** - if the code lives in a DB2 column: every program that
  INSERTs/UPDATEs it (with the host field it is written from), the declared
  type and length, cursors and dynamic SQL that filter on it, and the DB2
  utilities/plans that touch the table.
- **`interfaces`** - every NDM/FTP/MQ/DDF/web-service edge carrying the
  value off the mainframe, with the peer from `manifest.json`; each is an
  external contract change, not a code change.

The model then produces, in FACTS: per system, the value table, the rule
list, the message list, the reader list. In INFERENCE: for each rule, keep /
rewrite / delete; for each message, new text; for each reader outside the
mainframe (interface_edge rows), a contract note. The mandatory HUMAN MUST
VERIFY items: values that exist in data but not in code (compare against the
tables' actual distinct values), screen picklists and any validation done
outside COBOL (BMS/MFS field lists, defaults and lengths ARE parsed; the
validating IF/EVALUATE is in the program and `values` finds it), DB2 CHECK
constraints and lookup tables, non-mainframe consumers not declared in
`manifest.json`, and any program the index could not expand.

Tests come straight from `values`: every old value, every new value, every
pair combination the rules mention, and one value outside all of them.

## A8. What does this program do (the program-by-program read)

Question shape: "Explain CLMPOST", "walk me through the nightly posting
program", "what happens between the read and the write".

```
python -m atlas.query --db atlas.db walk CLMPOST --budget 12000 > w.md   # reading order + facts + source + data
python -m atlas.query --db atlas.db pack CLMPOST --budget 6000  > p.md   # where it runs, JCL datasets, copybooks (optional)
```

`walk` is the read an analyst does by hand, done once by the toolkit: the
entry paragraph first, then every paragraph the first time control reaches
it - PERFORM (which returns at the end of its target or THRU range, so the
paragraph after a performed one is *not* reached by falling out of it), GO TO
(no return), fall-through, THRU range, performed SECTION. Each paragraph
carries its resolved facts (call targets, SQL verbs and tables, DL/I with the
database and PROCOPT from the PSB, CICS resources, file operations, codes
set) and its source with the original line numbers; a paragraph from a
procedure copybook cites the copybook. Before the walk: the fields the
PROCEDURE DIVISION names - and only those - with offsets and lengths. After
it: paragraphs reached more than once, paragraphs nothing reaches (with the
reason), ENTRY points as their own roots, DECLARATIVES listed, not walked.

The budget drops **source** from the end of the walk, never order or facts;
a header's span (`CLMPOST:412-440`) is the `cite` range for anything left
out. `--from 2000-PROCESS` starts mid-way; `--depth 1` keeps source only for
the main line; `--no-source` gives the skeleton.

The model produces, in FACTS: where it runs and what it is given; inputs and
outputs with direction; the processing in the walk's order, one cited line per
paragraph, saying where a PERFORM returns and where a GO TO does not; every
CALL/LINK/XCTL with its arguments; every error path. In INFERENCE: what the
program is for. In NOT READ: the paragraphs the walk lists as not reached and
those whose source was left out - by name. HUMAN MUST VERIFY: run-time
decisions (dynamic CALL targets, GO TO DEPENDING, values from tables) and
anything the walk marks unresolved.

---

## D1. Change design document (two passes)

**Pass 1 - facts (no model):** run A1 for every field touched and A2 for every
job touched, then the cross-cutting matrices:

```
python -m atlas.query --db atlas.db crud       --job <JOB> [--job ...]     > crud.md    # C/R/U/D per program x dataset/table/segment, cited
python -m atlas.query --db atlas.db interfaces --system <SYSTEM>           > ifc.md     # what leaves/enters the mainframe and via whom
python -m atlas.query --db atlas.db table      <TABLE>                     > t-<T>.md   # per touched DB2 table: writers, columns, plans
python -m atlas.query --db atlas.db dbd        <DBD>                       > d-<D>.md   # per touched IMS DB: segments, PSBs+PROCOPT, utilities
python -m atlas.query --db atlas.db layout     <COPYBOOK> [--program PGM]  > l-<C>.md   # byte contract, as the named program sees it
```

Concatenate into `facts.md`. `crud` is the affected-components table in
raw form; `interfaces` is the external-contract section; `layout` is the
"contracts in bytes" section; `dbd` says whether a DBDGEN/PSBGEN is in play.

**Pass 2 - narrative (one model call):** hand `facts.md` + the requirement.
The design must contain: affected components table (from FACTS, each with a
change type: RECOMPILE-ONLY / SOURCE-CHANGE / BIND / PSBGEN+ACBGEN /
DBDGEN+RELOAD / JCL-CHANGE / DATA-CONVERSION / EXTERNAL-CONTRACT); CRUD matrix
copied from `crud` (it already combines SQL verbs, DL/I functions, CICS
file commands and file OPEN modes); before/after
data flow; interface contracts in bytes from `layout` and `interfaces` (RECFM, LRECL,
RDW, code page, whether COMP/COMP-3 crosses the boundary, the peer); backout plan per change type (a DBD BYTES
change has no fast backout - say so); a non-empty risks/unknowns section
seeded from UNRESOLVED.

## V1. Generating a program / change bundle

```
python -m atlas.query --db atlas.db pack     <similar existing program>  > exemplar-facts.md
python -m atlas.query --db atlas.db cite     <similar member> 1-400     > exemplar.cbl
python -m atlas.query --db atlas.db layout   <each record copybook> --program <exemplar> > layout.md  # offsets as the exemplar sees them
python -m atlas.query --db atlas.db copybook <each record copybook>     > copies.md   # every copy, skew, readers
python -m atlas.query --db atlas.db paragraph <exemplar> <error/checkpoint paragraph> > house-style.md  # the shop's real error handling
python -m atlas.query --db atlas.db pack     <a job that runs the exemplar> --kind job > exemplar-jcl.md  # full control cards
python -m atlas.query --db atlas.db dbd      <each IMS DB touched>      > dbd.md      # PROCOPT the exemplar's PSB really has
```

`paragraph` on the exemplar's error/abend/checkpoint paragraph is the house
style in source form: SQLCODE tests, status-code tests, the ABEND routine it
calls, the restart logic. `pack --kind job` carries the exact control cards
(sort, IDCAMS, DB2 utility) the new job must imitate.

The bundle contains: source (field names only from `layout.md`); copybook
changes; compile/bind member modelled on the shop's real one; run JCL modelled
on the real job (real HLQ patterns, real PROC names, real GDG conventions -
never invented DSNs); control cards with their exact expected layout; PSB/DBD
and bind implications from the index; and the fixed **HUMAN MUST VERIFY** list
(offsets/lengths, PROCOPT/SSA, checkpoint frequency, error paragraphs, restart
logic, anything crossing to a non-mainframe system).

## T1. Test conditions and data

```
python -m atlas.query --db atlas.db conditions <program>          > cond.md  # every IF/WHEN with its literal or 88 values, cited
python -m atlas.query --db atlas.db field    <each input field>   > f.md     # 88 values
python -m atlas.query --db atlas.db pack     <program>            > p.md     # paragraphs, calls, SQL
python -m atlas.query --db atlas.db layout   <record copybook> --program <program> > layout.md  # byte positions for the records
python -m atlas.query --db atlas.db pack     <record copybook> --kind copybook       > cb.md     # layout + who else reads it
```

Condition inventory comes from `conditions`: it lists every IF / EVALUATE
WHEN / sort INCLUDE-OMIT test in the program with the field, the operator, the
literal or the 88 name (expanded to its values) and the line - so every 88
value is a positive class, every THRU range gives low/high/mid, one value
outside all 88s under a field is the mandatory negative; every IF gets true
and false (implicit false when no ELSE); every EVALUATE gets each WHEN plus
OTHER. Test records are built to the copybook
byte layout (COMP-3 packed, correct lengths, RDW if VB) - the parser's numbers,
not the model's.

Expected results come from the specification, not from reading the program:
one call with spec + inputs (no source) produces expected outputs; a second
with the program produces predicted outputs; the diff is the finding.
Agreement is not proof - it is two readings that agree.


## H1. Transition / handover document for a release

Question shape: "3 programs and 4 copybooks changed, QA is done, write the
transition document for the support team". The production copies are in the
index (`estate\GC\...`); the changed copies live in a lower-environment PDS.

**Pass 1 - facts (no model), the one-command way:**

```
zowe zos-files download all-members "TEST.GC.SRC" -d estate\GC-TEST\TEST.GC.SRC   # the changed copies (or only the changed members)
zowe zos-files download all-members "TEST.GC.CPY" -d estate\GC-TEST\TEST.GC.CPY
python -m atlas.build estate --db atlas.db                                        # incremental: only the new members are parsed
python -m atlas.handover --db atlas.db --system GC-TEST --docs PLAN12,STORIES12,QAREL12 --out work/handover.md
```

`atlas.handover` (VS Code: `Atlas: handover pack`) writes every report below
into ONE file in the right order - the release list, a diff per changed
member, the walk of each changed program's new copy, each copybook's users
and layout, crud and program for the jobs, the documents' sections - and
copies `templates/release-facts.md` to `work/release-facts.md` for the
facts no file holds. Its first lines say the size in tokens; `--budget`
and `--doc-budget` shrink it. Without a second system (the changes are
already in production and no previous copy was indexed) give `--members
P1,P2,C1` instead of `--system`. Then `/atlas-handover`: one request.

**Pass 1, report by report (the same thing by hand):**

```
python -m atlas.query --db atlas.db --out work/release.md diff --system GC-TEST    # every member whose copies differ + members new to GC-TEST
python -m atlas.query --db atlas.db --out work/diff-<PGM>.md diff GC/<PGM> GC-TEST/<PGM>      # per changed program
python -m atlas.query --db atlas.db --out work/diff-<CPY>.md diff GC/<CPY> GC-TEST/<CPY>      # per changed copybook: fields added / changed / shifted
python -m atlas.query --db atlas.db --out work/walk-<PGM>.md walk <PGM> --budget 12000        # what each changed program does now
python -m atlas.query --db atlas.db --out work/pack-<CPY>.md pack <CPY> --kind copybook       # every other program that expands the copybook
python -m atlas.query --db atlas.db --out work/crud.md crud --job <JOB> [--job ...]           # the jobs that run them: C/R/U/D per dataset / table / segment
python -m atlas.query --db atlas.db --out work/doclist.md doc --list QA                       # the QA workbooks in the index (file name = document name)
python -m atlas.query --db atlas.db --out work/doc.md doc <QA-WORKBOOK>                       # its tabs as sections, screenshots and where they sit
python -m atlas.query --db atlas.db --out work/doc.md doc <QA-WORKBOOK> --grep <TEST-CASE>    # the rows of the tab(s) mentioning the test case
```

The folder under `estate\` names the system: `GC` and `GC-TEST` are two
systems by layout alone (no manifest needed), so a GC-TEST program expands
GC-TEST copybooks and `[[GC-TEST/PGM line "token"]]` cites the new copy,
`[[GC/PGM line "token"]]` the old one. `diff` ignores columns 1-6 and 73-80
(sequence numbers and change stamps are not changes); it names the paragraphs,
calls, copybooks, tables, files and fields that changed, groups the fields
that only shifted ("2 fields shift +1 byte from X to Y"), then prints the
changed lines of both sides with their own numbers. A copy that is not in the
index yet (a file just downloaded to a scratch folder) can be diffed by path:
lines only, no facts.

**Pass 2 - narrative (one model call, `/atlas-handover`):** the document
has two audiences and says so: Part 2 for Application Operations (jobs and
schedules from `crud` / `job`, what changed in each program from `walk` /
`diff`, files and record layouts, messages / abends / restart, what to watch
in the first runs) and Part 3 for the Contact Center (what customers and
agents will notice from the stories and the screen / message reports, new or
changed values and codes, what to tell a caller, known issues and open test
cases from the QA workbooks). Part 1 is the summary and the changed-components
table; Part 4 the test evidence and the gaps. What no file holds - go-live
date, contacts, escalation, schedule changes, what agents may say - goes into
`work/release-facts.md` (copy `templates/release-facts.md`), attached and
cited by path; a line left UNKNOWN there surfaces under HUMAN MUST VERIFY
instead of being written around. Why a chat model alone fails at this
document: it has none of the facts, so it fills the shape with generic
advice; the reports give it the names, the rule gives it the discipline,
the gate catches what it still invents.

**No previous copy of the changed members?** When the changes are already
in the production PDS and no baseline library exists, `diff` has nothing to
compare: the changed-components table then comes from the release facts
(the promote list) and the plan, and the code side shows the programs as
they are now (`walk`). If a backup or previous-release library exists,
download it into `estate\<SYSTEM>-PREV\<library>\` and diff PREV against
current.
