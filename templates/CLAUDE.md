# Operating contract for this estate

Copy this file to the root of the estate folder (next to `atlas.db`) as
`CLAUDE.md`. It applies to any coding agent or chat model working here; the
rules are about evidence, not about a vendor. In VS Code with GitHub Copilot
the same contract ships as `.github/copilot-instructions.md` (loaded
automatically) with the requests as `/atlas-*` prompts; Cursor reads it as
`.cursorrules`, Cline as `.clinerules` - see `docs/VSCODE.md`. Every query
below takes `--out work/NAME.md` (UTF-8) - prefer it to the shell's `>`.

## 1. You do not know this estate. The index does.

Nothing you recall about COBOL, IMS, DB2, JCL or insurance systems is a fact
about *this* estate. Program names, field names, job flows, table names and
error codes come from `atlas.db` or from a line of source you have opened.
If neither gives you the fact, the fact is **UNKNOWN** and you say so.

Before answering any question about a program, job, field, copybook, dataset,
error code or dependency, run the matching query first:

```
python -m atlas.query --db atlas.db program  <NAME>
python -m atlas.query --db atlas.db job      <NAME>
python -m atlas.query --db atlas.db field    <NAME>
python -m atlas.query --db atlas.db literal  <VALUE> [--field <FIELD>] [--like]
python -m atlas.query --db atlas.db values   <FIELD>          # every value the code assumes
python -m atlas.query --db atlas.db pair     <FIELD1> <FIELD2> # cross-field rules
python -m atlas.query --db atlas.db messages <PATTERN>        # message texts naming a rule
python -m atlas.query --db atlas.db screen   <MAP|MID|MOD>    # screen fields, defaults, validating programs
python -m atlas.query --db atlas.db transaction <CODE|PGM>    # CICS/IMS routing: transaction <-> program
python -m atlas.query --db atlas.db column   <TABLE.COL>      # DB2 column: written from / read into which fields
python -m atlas.query --db atlas.db table    <TABLE>          # CRUD per program, declared columns, cursors, dynamic SQL
python -m atlas.query --db atlas.db dbd      <DBD>            # IMS database: segments/fields, PSBs+PROCOPT, programs, utilities
python -m atlas.query --db atlas.db segment  <SEGMENT>        # who reads/updates an IMS segment
python -m atlas.query --db atlas.db layout   <COPYBOOK> [--program PGM]  # byte layout for test data / contracts
python -m atlas.query --db atlas.db docs     <TERM>           # documents (and OCR'd pictures) mentioning it
python -m atlas.query --db atlas.db doc      <DOC> [--grep TERM | --sections 3-5]  # a document's sections in FULL (outline when alone)
python -m atlas.query --db atlas.db images   [DOC]            # pictures inside documents, read or not
```

Documents are PROSE, never facts: they say what was intended, the code says
what runs. Cite a document as `[[DOCNAME 3 "token"]]` (section 3; OCR'd
images are sections 1001+). When a document and the code disagree, quote both
and say they disagree - do not pick one silently.

```
python -m atlas.query --db atlas.db copybook <NAME>
python -m atlas.query --db atlas.db callers|callees <NAME> --depth N [--args]   # --args: USING vs LINKAGE per call site
python -m atlas.query --db atlas.db dataset  <DSN>
python -m atlas.query --db atlas.db crud     <PGM...> | --job JOB | --copybook X   # C/R/U/D matrix with cites
python -m atlas.query --db atlas.db conditions <PGM>          # every IF/WHEN with its literal or 88 values
python -m atlas.query --db atlas.db paragraph <PGM> <NAME|line>  # who reaches it, what it does, its source
python -m atlas.query --db atlas.db walk <PGM> [--budget N] [--from PARA]  # the program in reading order: entry first, each paragraph as reached, facts + source
python -m atlas.query --db atlas.db interfaces [--system S] [--dsn X]  # what leaves/enters the mainframe, and via whom
python -m atlas.query --db atlas.db field    <NAME> [--all] [--program PGM]
python -m atlas.query --db atlas.db search   '"<token>"'
python -m atlas.query --db atlas.db cite     <MEMBER> <a>-<b> [--kind cobol|jcl|psb|...]
python -m atlas.query --db atlas.db pack     <PROGRAM|JOB|COPYBOOK|TRAN|FIELD> [--budget CHARS] [--sections a,b]
```

Prefer `pack` for anything about a program, a job (it carries the FULL
control cards), a copybook (it carries the byte layout) or a transaction: it
is the dossier plus the exact evidence lines, inside `--budget` characters
(about 4 per token), with the token estimate on its first line. Open a whole
member only when the pack and `cite` are not enough, and say why.

## 2. Cite or abstain

Every factual claim carries a citation in exactly this form:

```
[[MEMBER line "token that appears on that line"]]      e.g. [[CLMPOST 412 "CALL 'RATECALC'"]]
[[MEMBER:line "token"]]                                 the form the reports print - also accepted
[[MEMBER first-last "token"]]                           e.g. [[CLMNIGHT 17-19 "RUN PROGRAM(PREMCALC)"]]
[[MEMBER(kind) line "token"]]                           e.g. [[CLMPOST(jcl) 3 "PGM=CLMPOST"]] when a PSB or a
                                                        job shares the program's name
[[SYSTEM/MEMBER line "token"]]                          e.g. [[POLICY/DUPREC 1 "PIC X(9)"]] when two
                                                        departments own different copies
```

- The token must literally appear on the cited line(s). Quote it from the
  pack or from `cite`; never paraphrase it. A token shorter than 6
  characters or a range wider than 20 lines is a WARN: cite the line that
  holds the fact. A `//*` or `*` comment line proves nothing.
- The gate FAILS a citation when the member changed since the index was
  built: rebuild, then cite again.
- A claim you cannot cite is written as `UNVERIFIED:` and goes in its own
  section, or is not written at all.
- When the index has no answer, the answer is `INSUFFICIENT EVIDENCE` plus
  what would resolve it (which member to obtain, which export to load).
- Run `python -m atlas.verify_citations <answer.md> --db atlas.db` before
  delivering. A FAIL means the answer is wrong; fix it, do not soften it.

## 3. Facts, inference, gaps - never mixed

Every answer has these sections, in this order:

1. **FACTS** - rows from the index, each cited.
2. **INFERENCE** - your reasoning over the facts, labelled as such.
3. **UNRESOLVED IN SCOPE** - copied from the query output. If a query listed
   unresolved dynamic CALLs, missing copybooks, ambiguous members or missing
   scheduler data, the answer is incomplete to that extent and says so.
4. **WHAT WAS NOT SEARCHED** - members/systems out of scope of the queries run.

## 4. Things that are wrong by default

- **A missing row is not "no link."** No caller in the index means "no caller
  *found*"; dynamic CALLs, unindexed source, online transactions and the
  scheduler can all still reach it.
- **DISP is not direction.** Use `mode`/`mode_source` from the query.
- **PGM= is not the program.** Use `effective_pgm`.
- **PCB arguments are positional**, and the I/O PCB comes first in BMP/MPP or
  `CMPAT=YES`. Map through the PSB before naming a database.
- **The same copybook differs between systems.** Check `field` for version
  skew before saying "this field is at offset N".
- **Same member name ≠ same program.** Check `ambiguous`. If the copy is not
  marked authoritative, say which copy you used.
- **Group-level MOVEs, READ INTO, WRITE FROM, INITIALIZE and CALL BY REFERENCE
  touch fields without naming them.** Field-name search alone understates
  impact; follow the parent group and the CALL USING positions.
- **A never-PERFORMed paragraph may still run** (fall-through, GO TO). Never
  call code dead without saying which caveats were not checked.
- **Documents describe intent; code describes behaviour.** Quote both when
  they disagree and say they disagree.

## 5. Generating code, JCL or test data

- Field names come from `field`/`copybook` output, never from memory. Byte
  offsets and lengths come from the parser, never from your arithmetic.
- Copy a real, recent, similar member as the exemplar for style (paragraph
  naming, error paragraphs, SQLCODE/status-code handling, checkpoint logic);
  do not invent a house style.
- Test data is built from the copybook layout (`field` gives offsets, lengths,
  COMP-3 sizes) and from 88-level values; one value outside every 88 under a
  field is a mandatory negative case.
- Every generated bundle ends with a **HUMAN MUST VERIFY** list: computed
  offsets/lengths, PROCOPT/SSA choices, checkpoint frequency, bind/PSB/DBD
  implications, anything touching a non-mainframe interface.

## 6. Spend tokens on reasoning, not on reading

The budget is small. Bulk work is free: build the index, run queries, run the
gate. Your call should read a pack of a few hundred lines and produce an
answer of a few hundred lines. If a task needs more than ~3 `cite` fetches,
stop and say which query is missing from the toolkit so it can be added.

## 7. Self-correction

When a wrong fact is found - by you, by the gate, or by a reviewer - the fix
is a regression test in `tests/` plus a line in `LESSONS.md` **before** the
code change. Say what was wrong, what the test asserts, and what changed.
