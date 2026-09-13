---
mode: 'ask'
description: 'Atlas A1: impact of changing a field (PIC, length, values) across the estate'
---
Impact analysis for a field change, using ONLY these reports:
[field](../../work/field.md) (task "Atlas: field"), [layout](../../work/layout.md) (task
"Atlas: layout" on the record copybook), [crud](../../work/crud.md) (task "Atlas: crud for a job",
or `crud --copybook X` from the terminal), [pack](../../work/pack.md) (task "Atlas: pack" on the
copybook) and [interfaces](../../work/interfaces.md) (task "Atlas: interfaces").

Change: ${input:change:What changes - PIC, length, new values, meaning? Name the field.}

Write:

## FACTS
1. Every copybook copy of the field with offset and length, per department - flag version skew.
2. Every program including each copy, with REPLACING noted (the program's own name for it).
3. Every job and step that runs those programs.
4. Every dataset those steps write, and who reads it (`dataset` rows in the pack).
5. Every sort / control card whose byte range overlaps the field.
6. Callers of the including programs (LINKAGE may carry the record).
7. Every interface row carrying the record off the mainframe.

## INFERENCE
- Does the record length change? If yes: VSAM DEFINE RECORDSIZE, DCB/LRECL, VB RDW +4,
  downstream fixed-width readers, non-mainframe consumers are all in scope - list them.
- Per program: RECOMPILE-ONLY / SOURCE-CHANGE / BIND / DATA-CONVERSION / EXTERNAL-CONTRACT,
  with the fact that decides it.

## UNRESOLVED IN SCOPE
Copied verbatim from the reports.

## NOT SEARCHED
## HUMAN MUST VERIFY
Unresolved dynamic CALLs in scope, copies not marked authoritative, consumers outside the index.

Rules: use only the attached reports; what they lack is UNKNOWN. Every fact carries
`[[MEMBER line "token"]]` with the token copied verbatim; never paraphrase; never cite a comment
line. Uncitable claims go under `## UNVERIFIED` or are not written. Offsets and lengths come from
`layout`, never from your arithmetic.
