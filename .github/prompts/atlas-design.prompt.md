---
mode: 'ask'
description: 'Atlas D1: change design document from the facts already gathered'
---
Write the change design using ONLY the reports gathered for it. Run the tasks first, one per
thing touched, then attach here what exists: [pack](../../work/pack.md), [field](../../work/field.md),
[layout](../../work/layout.md), [job](../../work/job.md), [crud](../../work/crud.md),
[interfaces](../../work/interfaces.md). For more than one field or job, keep each earlier report
under another name in `work/` and attach it with `#file:`.

Requirement: ${input:requirement:The change request, in one or two sentences}

Write:

## FACTS
Everything the reports establish about the components touched, each cited.

## AFFECTED COMPONENTS
A table: component, kind (program / copybook / job / PROC / card / table / DBD / PSB / dataset /
interface), change type - RECOMPILE-ONLY / SOURCE-CHANGE / BIND / PSBGEN+ACBGEN / DBDGEN+RELOAD /
JCL-CHANGE / DATA-CONVERSION / EXTERNAL-CONTRACT - and the fact (cited) that decides it.

## CRUD MATRIX
Copied from `crud` (it already combines SQL verbs, DL/I functions, CICS file commands and
OPEN modes); mark the cells the change alters.

## DATA FLOW
Before and after, step by step, from the job report's effective steps.

## INTERFACE CONTRACTS
In bytes, from `layout` and `interfaces`: RECFM, LRECL, RDW, code page, whether COMP / COMP-3
crosses the boundary, the peer.

## BACKOUT
Per change type. A DBD BYTES change or a data conversion has no fast backout - say so.

## RISKS AND UNKNOWNS
Non-empty. Seeded from the reports' UNRESOLVED lists (copied verbatim), plus every interface
whose peer is undeclared.

## NOT SEARCHED
## HUMAN MUST VERIFY

Rules: use only the attached reports; what they lack is UNKNOWN. Every fact carries
`[[MEMBER line "token"]]` (token copied verbatim, never a comment line). Uncitable claims go
under `## UNVERIFIED` or are not written. Offsets and lengths from `layout` only.
