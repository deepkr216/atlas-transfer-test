# Mainframe Atlas - operating contract for the chat model in this workspace

This workspace holds the Atlas toolkit and `atlas.db`, an index of a
mainframe estate (COBOL, JCL and PROCs, control cards, copybooks, IMS, DB2,
CICS, MQ, documents). The estate's source members are NOT in this
workspace. Do not look for them and do not guess them. The same contract, in
long form, is `templates/CLAUDE.md`; the procedures are `templates/PLAYBOOKS.md`.

## 1. The index knows this estate. You do not.

Nothing you recall about COBOL, IMS, DB2, JCL or insurance systems is a fact
about *this* estate. Program names, field names, job flows, table names,
error codes and byte offsets come from the report files under `work/`
(written by `python -m atlas.query ... --out work/NAME.md`) or from a source
line those reports quote. If neither gives the fact, the fact is **UNKNOWN**
and you say so.

- In **ask** mode: use only the files attached to the request. Do not open
  other files in the workspace; do not search the workspace.
- In **agent** mode: run the matching query first, always with `--out` into
  `work/`, then answer from that file:

```
python -m atlas.query --db atlas.db --out work/pack.md       pack <NAME> [--kind program|job|copybook|transaction|field] [--budget 12000]
python -m atlas.query --db atlas.db --out work/field.md      field <FIELD> [--all] [--program PGM]
python -m atlas.query --db atlas.db --out work/layout.md     layout <COPYBOOK> [--program PGM]
python -m atlas.query --db atlas.db --out work/literal.md    literal <CODE>
python -m atlas.query --db atlas.db --out work/values.md     values <FIELD>
python -m atlas.query --db atlas.db --out work/pair.md       pair <FIELD1> <FIELD2>
python -m atlas.query --db atlas.db --out work/messages.md   messages <PATTERN>
python -m atlas.query --db atlas.db --out work/job.md        job <JOB>
python -m atlas.query --db atlas.db --out work/crud.md       crud --job <JOB> | <PGM...> | --copybook <X>
python -m atlas.query --db atlas.db --out work/conditions.md conditions <PGM>
python -m atlas.query --db atlas.db --out work/paragraph.md  paragraph <PGM> <NAME|line>
python -m atlas.query --db atlas.db --out work/walk.md       walk <PGM> [--budget 12000] [--from PARA] [--depth D]   # the program in reading order
python -m atlas.query --db atlas.db --out work/diff.md       diff <SYS/PGM> <SYS-TEST/PGM> [--budget N]   # two versions: changed facts, then the lines of both sides
python -m atlas.query --db atlas.db --out work/release.md    diff --system <SYS-TEST>                     # every member the release changed
python -m atlas.query --db atlas.db --out work/callers.md    callers|callees <PGM> --depth 2 [--args]
python -m atlas.query --db atlas.db --out work/dataset.md    dataset <DSN>
python -m atlas.query --db atlas.db --out work/table.md      table <TABLE> | column <TABLE.COL>
python -m atlas.query --db atlas.db --out work/dbd.md        dbd <DBD> | segment <SEGMENT>
python -m atlas.query --db atlas.db --out work/tran.md       transaction <CODE|PGM> | screen <MAP>
python -m atlas.query --db atlas.db --out work/interfaces.md interfaces [--system S] [--dsn X]
python -m atlas.query --db atlas.db --out work/doclist.md    doc --list [FOLDER]                       # the indexed documents (workbooks: tabs = sections, rows = text)
python -m atlas.query --db atlas.db --out work/doc.md        doc <DOCUMENT> [--grep TERM | --sections 3-5] [--budget N]   # a document's sections in full
python -m atlas.query --db atlas.db --out work/cite.md       cite <MEMBER> <a>-<b> [--kind K]
python -m atlas.query --db atlas.db --out work/coverage.md   coverage
```

Never read `atlas.db` directly. Never open more than three `cite` ranges for
one question; if you need more, say which query is missing. Never use the
shell's `>` for these reports (`--out` writes UTF-8; the shell may not).

## 2. Cite or abstain

Every factual claim carries a citation in exactly one of these forms, and
the quoted token is copied verbatim from the report or source line - never
paraphrased, never from a `//*` or `*` comment line:

```
[[MEMBER line "token"]]          [[CLMPOST 412 "CALL 'RATECALC'"]]
[[MEMBER:line "token"]]          the form the reports print
[[MEMBER first-last "token"]]    keep ranges under 20 lines
[[MEMBER(kind) line "token"]]    when a PSB or a job shares the program's name
[[SYSTEM/MEMBER line "token"]]   when two departments own different copies
```

A claim you cannot cite is written under `UNVERIFIED`, or not at all. When
the reports have no answer, the answer is `INSUFFICIENT EVIDENCE` plus what
would resolve it (which member, which export). Before finishing, the answer
is saved as `work/answer.md` and checked with
`python -m atlas.verify_citations work/answer.md --db atlas.db --out work/gate.txt`
(in ask mode the user runs the task "Atlas: verify"). A FAIL means the
answer is wrong: fix it, do not soften it.

## 3. Facts, inference, gaps - never mixed

Every answer has these sections, in this order:

1. `## FACTS` - rows from the reports, each cited.
2. `## INFERENCE` - your reasoning over the facts, labelled as such.
3. `## UNRESOLVED IN SCOPE` - copied verbatim from the reports' unresolved
   lists (dynamic CALLs, missing copybooks, ambiguous members, `symbolic`
   DSNs, no scheduler). The answer is incomplete to that extent and says so.
4. `## NOT SEARCHED` - what the reports you had did not cover.
5. `## HUMAN MUST VERIFY` - when the answer proposes a change or generates
   anything.

## 4. Wrong by default

- A missing row is not "no link": no caller found is not no caller.
- DISP is not direction (`mode_source` is). `PGM=` is not the program
  (`effective_pgm` is). PCB arguments are positional; the report already
  mapped them through the PSB - do not re-map from memory.
- The same copybook differs between systems; check the skew the `field`
  report flags. Same member name is not the same program (`ambiguous`).
- Group-level MOVEs, READ INTO, WRITE FROM, INITIALIZE and CALL BY REFERENCE
  touch fields without naming them.
- A never-PERFORMed paragraph may still run (GO TO, fall-through, SORT
  procedures); a program with no caller may be started by a transaction,
  an INTRDR submit or the scheduler.
- Documents describe intent; code describes behaviour. Quote both when they
  disagree and say they disagree.
- A document named `*.VIDEO` is a recording's transcript: `SCREEN:` lines are
  OCR (0/O and 1/I confused), `SAID:` lines are speech recognition (jargon
  garbled). Quote it as "said in the recording at 00:12:05", never as fact.

## 5. Generating code, JCL or test data

Field names come from the `field`/`layout` reports; byte offsets and lengths
from `layout`, never from your arithmetic. Style (paragraph naming, error
paragraphs, SQLCODE and status-code handling, checkpoints) comes from a real
exemplar quoted in the pack. Test data follows the byte layout and the 88
values; one value outside every 88 under a field is a mandatory negative
case. Every generated bundle ends with a `## HUMAN MUST VERIFY` list.

## 6. Spend tokens on reasoning, not on reading

The budget is small. Bulk work is free and already done by the index. Read
the `work/` files you are given, reason, answer in a few hundred lines. Do
not read the toolkit's own source (`atlas/`, `tests/`) to answer an
estate question - it contains no facts about the estate.
