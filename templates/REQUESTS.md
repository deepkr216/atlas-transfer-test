# Requests to type into the chat

The same requests as the `/atlas-*` prompt files in `.github/prompts/`, for a chat
tool that has no prompt files (Cursor, Cline, Continue, a web chat, an older
Copilot). Run the task (or the `--out` command) first, **attach the file(s)
named**, paste the request, fill the `<...>`.

Every request ends with the same rules block - keep it; it is what makes the
answer checkable by `python -m atlas.verify_citations work/answer.md --db atlas.db`.

```
Rules: use only the attached file(s); what they do not contain is UNKNOWN - say
so, never fill a gap from general COBOL, JCL, IMS, DB2 or insurance knowledge.
Every fact carries [[MEMBER line "token"]] (or [[MEMBER:line "token"]]) with the
token copied verbatim from the file - never paraphrased, never from a //* or *
comment line. A claim you cannot cite goes under "## UNVERIFIED" or is not
written. If the files cannot answer, write INSUFFICIENT EVIDENCE and what would
resolve it. Headings, in this order: ## FACTS, ## INFERENCE,
## UNRESOLVED IN SCOPE (copied verbatim from the file), ## NOT SEARCHED.
```

## One question about a program / job / copybook / transaction / field

Attach `work/pack.md` (`--out work/pack.md pack NAME [--kind K] [--budget 12000]`).

```
Answer this question using ONLY the attached pack: <question>
<rules>
```

## Explain what a program does

Attach `work/walk.md` (`--out work/walk.md walk PGM --budget 12000`) and, if
useful, `work/pack.md`.

```
Explain what this program does using ONLY the attached walk (the program in
reading order: entry first, each paragraph the first time control reaches it,
with resolved facts and numbered source; the fields it names; the paragraphs
nothing reaches) and the pack if attached. FACTS: where it runs and what it is
given; inputs and outputs with direction; the processing in the walk's order,
one cited line per paragraph, saying where a PERFORM returns and where a GO TO
does not; every CALL/LINK/XCTL with arguments; every error path. INFERENCE:
what it is for. NOT READ: the paragraphs the walk lists as not reached, and
those whose source was left out for the budget, by name. A copybook paragraph
cites the copybook named in its block.
<rules>
```

## A1 Impact of changing a field

Attach `work/field.md`, `work/layout.md`, `work/crud.md`, `work/pack.md` (copybook), `work/interfaces.md`.

```
Impact analysis for this change: <field, and what changes - PIC / length / values>.
FACTS: (1) every copy of the field with offset and length per department, skew
flagged; (2) every program including each copy, REPLACING noted; (3) every job
and step running them; (4) every dataset those steps write and who reads it;
(5) every sort / control card overlapping the field's bytes; (6) callers of the
including programs; (7) every interface carrying the record off the mainframe.
INFERENCE: does the record length change (then RECORDSIZE, LRECL, RDW, downstream
readers, external consumers are in scope); per program RECOMPILE-ONLY /
SOURCE-CHANGE / BIND / DATA-CONVERSION / EXTERNAL-CONTRACT with the deciding
fact. HUMAN MUST VERIFY: unresolved CALLs, copies not authoritative, consumers
outside the index. Offsets from the layout only.
<rules>
```

## A2 What a job does

Attach `work/job.md` and `work/pack.md` (`pack JOB --kind job`).

```
Describe this job step by step from the EFFECTIVE steps (EXEC PROC= steps are
shown expanded - use those rows, not the PROC member): effective program (not
PGM=), what the quoted control cards make it do, datasets in/out with the
mode_source that decided direction, GDG generation, the guard that marks
error/backout steps, JOBLIB/JCLLIB, INTRDR submissions. INFERENCE: what the job
achieves. UNRESOLVED: PROCs not in the index, symbolic DSNs, missing card
members, no scheduler. Focus: <whole job | a step>.
<rules>
```

## A3 Trace an error / status code

Attach `work/literal.md` (`--out work/literal.md literal CODE`).

```
Trace <CODE> from the attached report: defined -> set (condition, paragraph) ->
carried (CALL USING position / COMMAREA / record bytes) -> tested -> displayed
or written, one cited row per hop, with the message text where the report has
it. INFERENCE: what situation produces it, which program is the origin.
UNRESOLVED: routes the report could not follow. If the literal is not found,
say it may be built (STRING, arithmetic), read from a table, or spelled
differently - do not guess a program.
<rules>
```

## A4 Where a field is populated

Attach `work/field.md` and `work/pack.md` (the main writing program).

```
Where does <FIELD> get its value? FACTS: every write site (program, paragraph,
statement, source of the value), every read site that carries it onward, the
datasets / tables / segments between them with producer and consumer.
INFERENCE: the end-to-end path, hop by hop; where it originates; where it stops
being traceable. Every hop is a cited fact or a gap.
<rules>
```

## A5 Abend triage

Attach `work/abend.txt` (the JES lines you pasted), `work/job.md`, `work/pack.md`, `work/conditions.md`, `work/layout.md`.

```
Triage abend <CODE> in step <STEP>. FACTS: the effective program, its inputs at
that step, the paragraphs and statements around the failing operation.
HYPOTHESES: at most three, ranked, each with the supporting facts and ONE
disconfirming check. S0C7: the COMP-3 / numeric fields at the record offsets
and the tests around them - if the offset cannot be mapped without the compile
listing say "cannot localise". S0C4: call sites whose USING count differs from
the callee's LINKAGE. SQLCODE -805/-818: bind, not code. IMS AI/AJ/AM: an
update on a PROCOPT=G PCB (the pack shows PROCOPT per call). Never state a
cause as certain.
<rules>
```

## A7 Value-domain change

Attach `work/values.md`, `work/pair.md`, `work/messages.md`, `work/field.md`.

```
Inventory every place a value of <FIELD> is assumed, for this change: <new value
/ merge / special case>. Per department: (1) the value table from values -
mark USED BUT NOT DOCUMENTED and documented-but-never-used; (2) the rule list
from pair - every statement where the two fields are used together; (3) the
message list from messages, with the field each is moved to; (4) the reader
list from field - copies with offset/length, programs, jobs, datasets and their
readers, callers. INFERENCE: per rule keep / rewrite / delete; per message the
new text; per external reader a contract note. HUMAN MUST VERIFY: values in
data but not in code, screen picklists, DB2 CHECK constraints and lookup
tables, undeclared consumers. TEST CONDITIONS: every old value, every new
value, every pair combination, one value outside all of them.
<rules>
```

## T1 Test conditions and data

Attach `work/conditions.md`, `work/layout.md`, `work/pack.md`.

```
Build the test-condition inventory for <PROGRAM> under <requirement>.
CONDITIONS: one cited row per test in the program (field, operator, value or
88 with its values, line); then the classes - every 88 value a positive class,
THRU ranges low/high/mid, one value outside all 88s the mandatory negative,
every IF true and false, every EVALUATE each WHEN plus OTHER, every sort
INCLUDE/OMIT a record on each side. TEST RECORDS: built to the byte layout
(offset, length, PIC, usage; COMP-3 packed; RDW if VB), one per class, as
field -> value with byte position - the layout's numbers, never yours.
EXPECTED RESULTS: from the requirement only; where it is silent write
"EXPECTED: UNKNOWN - specification needed".
<rules>
```

## D1 Change design

Attach every report gathered for the change (`work/pack.md`, `work/field.md`, `work/layout.md`, `work/job.md`, `work/crud.md`, `work/interfaces.md`, ...).

```
Write the change design for: <requirement>. FACTS (cited). AFFECTED COMPONENTS:
table of component, kind, change type (RECOMPILE-ONLY / SOURCE-CHANGE / BIND /
PSBGEN+ACBGEN / DBDGEN+RELOAD / JCL-CHANGE / DATA-CONVERSION / EXTERNAL-CONTRACT)
and the deciding fact. CRUD MATRIX copied from crud, altered cells marked. DATA
FLOW before and after from the job's effective steps. INTERFACE CONTRACTS in
bytes from layout and interfaces (RECFM, LRECL, RDW, code page, COMP fields
crossing, the peer). BACKOUT per change type - a DBD BYTES change or a data
conversion has no fast backout, say so. RISKS AND UNKNOWNS, non-empty, seeded
from the UNRESOLVED lists. HUMAN MUST VERIFY.
<rules>
```

## H1 Transition document for a release (Application Operations + Contact Center)

Attach `work/release-facts.md` (copied from `templates/release-facts.md` and filled in), `work/release.md`, every `work/diff-*.md`, `work/walk-*.md`, `work/layout.md`, `work/pack.md`, `work/crud.md`, and the `work/doc*.md` reports for the plan, the stories and the QA workbooks.

```
Write the transition document for: <release>. Plain working English for the
people who run and support it, one fact per sentence, names exactly as the
reports spell them, no filler. Part 1 for everyone: WHAT THIS RELEASE DOES
(from the release facts, the plan and the stories, cited), CHANGED COMPONENTS
(the release table with "what changed" per member from the diffs; from the
plan when no previous copy exists). Part 2 for Application Operations: JOBS
AND SCHEDULES (crud, job), WHAT CHANGED IN EACH PROGRAM (walk, diff; cited on
the new copy, and on the old when it exists), FILES AND RECORD LAYOUTS (diff,
layout, pack: added / changed / shifted fields and every other program that
expands the copybook), MESSAGES / ABENDS / RESTART, MONITORING FOR THE FIRST
RUNS. Part 3 for the Contact Center: WHAT CUSTOMERS AND AGENTS WILL NOTICE
(stories, screens, messages; UNKNOWN if the stories do not say), NEW OR
CHANGED VALUES AND CODES, WHAT TO TELL A CALLER (only from the facts and the
release facts; "instruction needed" where they give none), KNOWN ISSUES AND
OPEN TEST CASES (QA workbook rows not marked passed; changed paragraphs no
test covers). Part 4: TEST EVIDENCE (workbook sections and screenshot OCR
sections, cited), UNRESOLVED IN SCOPE, NOT SEARCHED, HUMAN MUST VERIFY (the
promote list, the QA sign-off, dates and contacts, files already written with
the old layout, every "instruction needed"). Release facts are cited as
[[work/release-facts.md <line> "token"]].
<rules>
```

## The gate failed

Attach `work/answer.md`, `work/gate.txt`, `work/pack.md`.

```
The citation gate rejected the attached answer; the report is attached. For
each FAIL find the line in the pack that really holds the token and cite it
verbatim, or move the claim to "## UNVERIFIED", or delete it; for each WARN on
a comment line cite the statement instead; for each "?" line add the citation
or move the sentence. Return the complete corrected answer with the same
headings. Do not soften a claim to make it pass - a claim without a line
behind it is removed, not reworded.
```
