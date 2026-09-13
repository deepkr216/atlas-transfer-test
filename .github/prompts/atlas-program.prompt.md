---
mode: 'ask'
description: 'Atlas: explain what a program does, in execution order, cited'
---
Explain what this program does, using ONLY [the pack](../../work/pack.md) (task
"Atlas: pack NAME -> work/pack.md") and [the numbered source](../../work/cite.md) (task
"Atlas: whole member -> work/cite.md"). The pack carries the facts outside the program -
the jobs and transactions that run it, the PROC symbolics, the copybook offsets after
REPLACING, the PSB order behind each DL/I call; the source carries the paragraphs.

Focus: ${input:focus:Whole program, or a paragraph / file / table / code to concentrate on}

Write:

## FACTS
- Where it runs (jobs, steps, transactions) and what it is given (PARM, COMMAREA, control cards).
- Inputs and outputs: every file, table, database, queue and screen, with direction.
- The processing in execution order: start at the first paragraph, follow PERFORMs and GO TOs,
  one line per paragraph - what it reads, tests, moves, writes, calls - each line cited to the
  source line that shows it.
- Every CALL / LINK / XCTL with its arguments, and every error / abend path.

## INFERENCE
What the program is for, in business terms, derived only from the facts above.

## UNRESOLVED IN SCOPE
Copied verbatim from the pack.

## NOT READ
Paragraphs you did not walk (unreached, or beyond the budget) - list them by name so the reader
knows what the explanation leaves out.

Rules: use only the two files; what they do not contain is UNKNOWN. Every fact carries
`[[MEMBER line "token"]]` with the token copied verbatim from the file; never paraphrase, never
cite a comment line. A claim you cannot cite goes under `## UNVERIFIED` or is not written.
