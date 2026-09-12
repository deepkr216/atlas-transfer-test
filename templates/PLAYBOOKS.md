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
python -m atlas.query --db atlas.db field    PM-POLICY-STATUS      > f.md
python -m atlas.query --db atlas.db copybook PMASTREC              > c.md
python -m atlas.query --db atlas.db literal  AC                    > l.md   # for each 88 value in play
```

Hand the model: `f.md`, `c.md`, the relevant `l.md`s, and the change request.

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
authoritative, any consumer outside the index.

## A2. What does this job do

```
python -m atlas.query --db atlas.db job CLMNIGHT > j.md
```

The model narrates step by step from `j.md` only: effective program (not
`PGM=`), what the control cards make the step do (quote them), datasets in/out
with direction source, GDG generation, COND/IF flow. `EXEC PROC=` steps are
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

`l.md` already contains: where the literal is defined (VALUE / 88), set (MOVE),
tested (IF / WHEN), displayed, and where the value flows after being set -
positionally through `CALL ... USING` to the callee's LINKAGE, back to callers,
and through file records to other programs at the same **bytes**. The model
turns that into a chain with citations, and lists routes it could not follow
(DB2/IMS/MQ, group-level MOVEs, unresolved CALLs).

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
filters on it. `field <host-var>` shows the same from the COBOL side. For an
IMS field: `field` lists the DL/I calls whose I/O area holds it (GU/GN read
into the area, ISRT/REPL write from it) with the positional PCB; map the PCB
through the PSB in the `program` dossier to name the database and segment.

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
localise"); S0C4 - LINKAGE/USING count mismatch (compare `linkage_using` vs the
caller's USING list in `program`); SQLCODE -805/-818 - bind/timestamp, not
code; -911/-913 - contention, look at the concurrent jobs; IMS AI/AJ/AM -
PROCOPT vs call function (`ims_pcb.procopt`), GE/GB - SSA/path, not a bug.

## A6. Dead code

```
python -m atlas.query --db atlas.db dead > d.md
```

Candidates only. The report states whether the scheduler and online
definitions were loaded and how many dynamic CALLs are unresolved; the model
repeats those caveats and recommends the export that would close them. Never
recommend deletion from this report alone.

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

The model then produces, in FACTS: per system, the value table, the rule
list, the message list, the reader list. In INFERENCE: for each rule, keep /
rewrite / delete; for each message, new text; for each reader outside the
mainframe (interface_edge rows), a contract note. The mandatory HUMAN MUST
VERIFY items: values that exist in data but not in code (compare against the
tables' actual distinct values), screens (BMS/MFS validation and picklists
are not parsed), DB2 CHECK constraints and lookup tables, and any program the
index could not expand.

Tests come straight from `values`: every old value, every new value, every
pair combination the rules mention, and one value outside all of them.

---

## D1. Change design document (two passes)

**Pass 1 - facts (no model):** run A1 for every field touched and A2 for every
job touched; concatenate into `facts.md`.

**Pass 2 - narrative (one model call):** hand `facts.md` + the requirement.
The design must contain: affected components table (from FACTS, each with a
change type: RECOMPILE-ONLY / SOURCE-CHANGE / BIND / PSBGEN+ACBGEN /
DBDGEN+RELOAD / JCL-CHANGE / DATA-CONVERSION / EXTERNAL-CONTRACT); CRUD matrix
derived from `sql_stmt` verbs, DL/I functions and file OPEN modes; before/after
data flow; interface contracts in bytes (RECFM, LRECL, RDW, code page, whether
COMP/COMP-3 crosses the boundary); backout plan per change type (a DBD BYTES
change has no fast backout - say so); a non-empty risks/unknowns section
seeded from UNRESOLVED.

## V1. Generating a program / change bundle

```
python -m atlas.query --db atlas.db pack     <similar existing program>  > exemplar-facts.md
python -m atlas.query --db atlas.db cite     <similar member> 1-400     > exemplar.cbl
python -m atlas.query --db atlas.db copybook <each record copybook>     > layout.md
python -m atlas.query --db atlas.db job      <a job that runs the exemplar> > exemplar-jcl.md
```

The bundle contains: source (field names only from `layout.md`); copybook
changes; compile/bind member modelled on the shop's real one; run JCL modelled
on the real job (real HLQ patterns, real PROC names, real GDG conventions -
never invented DSNs); control cards with their exact expected layout; PSB/DBD
and bind implications from the index; and the fixed **HUMAN MUST VERIFY** list
(offsets/lengths, PROCOPT/SSA, checkpoint frequency, error paragraphs, restart
logic, anything crossing to a non-mainframe system).

## T1. Test conditions and data

```
python -m atlas.query --db atlas.db field    <each input field>   > f.md   # 88 values
python -m atlas.query --db atlas.db pack     <program>            > p.md   # paragraphs, calls, SQL
python -m atlas.query --db atlas.db copybook <record copybook>    > layout.md
```

Condition inventory: every 88 value is a positive class, every THRU range gives
low/high/mid, one value outside all 88s under a field is the mandatory
negative; every IF gets true and false (implicit false when no ELSE); every
EVALUATE gets each WHEN plus OTHER. Test records are built to the copybook
byte layout (COMP-3 packed, correct lengths, RDW if VB) - the parser's numbers,
not the model's.

Expected results come from the specification, not from reading the program:
one call with spec + inputs (no source) produces expected outputs; a second
with the program produces predicted outputs; the diff is the finding.
Agreement is not proof - it is two readings that agree.
