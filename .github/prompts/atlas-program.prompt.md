---
mode: 'ask'
description: 'Atlas: explain what a program does, in execution order, cited'
---
Explain what this program does, using ONLY [the walk](../../work/walk.md) (task
"Atlas: walk PROGRAM -> work/walk.md") and, if attached, [the pack](../../work/pack.md) (task
"Atlas: pack"). The walk is the program in READING order: the entry paragraph first, then each
paragraph the first time control reaches it - PERFORM (returns at the end of its target), GO TO
(no return), fall-through, THRU range, performed SECTION - with its resolved facts (call targets,
tables, IMS database and PROCOPT, CICS resources, files, codes set) and its source with original
line numbers; the fields it names with their offsets; the paragraphs nothing reaches. When a
paragraph's source was left out for the budget, its header still gives the `cite` range. The
pack adds where it runs, the JCL datasets behind each file, and the copybooks.

Focus: ${input:focus:Whole program, or a paragraph / file / table / code to concentrate on}

Write:

## FACTS
- Where it runs (jobs, steps, transactions) and what it is given (PARM, COMMAREA, control cards).
- Inputs and outputs: every file, table, database, queue and screen, with direction.
- The processing in the walk's order: one line per paragraph - what it reads, tests, moves,
  writes, calls - each cited to the source line that shows it. Say where a PERFORM returns and
  where a GO TO does not.
- Every CALL / LINK / XCTL with its arguments, and every error / abend path.

## INFERENCE
What the program is for, in business terms, derived only from the facts above.

## UNRESOLVED IN SCOPE
Copied verbatim from the walk (unresolved CALLs, PCBs, missing copybooks).

## NOT READ
Paragraphs the walk lists as not reached, and paragraphs whose source was left out for the
budget - by name, so the reader knows what the explanation leaves out.

Rules: use only the attached files; what they do not contain is UNKNOWN. Every fact carries
`[[MEMBER line "token"]]` with the token copied verbatim from the file - the member is the one
named in the block's header or its `cite as` line (a copybook paragraph cites the copybook);
never paraphrase, never cite a comment line. A claim you cannot cite goes under `## UNVERIFIED`
or is not written.
