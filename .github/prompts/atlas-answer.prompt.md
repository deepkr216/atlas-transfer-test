---
mode: 'ask'
description: 'Atlas: answer one question from work/pack.md, every fact cited'
---
Answer the question below using ONLY [the pack](../../work/pack.md), written by the task
"Atlas: pack NAME -> work/pack.md". The pack is the dossier of one program, job, copybook,
transaction or field plus the exact source lines behind each fact, with `MEMBER:line` cites.

Question: ${input:question:What do you want to know about it?}

Write the answer with these headings, in this order:

## FACTS
Rows from the pack that answer the question, one per line, each ending with its citation.

## INFERENCE
Your reasoning over the facts, labelled as reasoning. Nothing new is asserted here.

## UNRESOLVED IN SCOPE
The pack's unresolved items copied verbatim (dynamic CALLs, missing copybooks, ambiguous
members, `symbolic` DSNs, no scheduler). Say how far they limit the answer.

## NOT SEARCHED
What the pack does not cover and would need another report (name the query).

Rules:
- Use only the pack. What it does not contain is UNKNOWN - say so; never fill a gap from
  general COBOL, JCL, IMS, DB2 or insurance knowledge.
- Every fact carries `[[MEMBER line "token"]]` or `[[MEMBER:line "token"]]`. Copy the token
  verbatim from the pack; never paraphrase it; never cite a `//*` or `*` comment line.
- A claim you cannot cite goes under a heading `## UNVERIFIED`, or is not written.
- If the pack cannot answer, write `INSUFFICIENT EVIDENCE` and what would resolve it.
