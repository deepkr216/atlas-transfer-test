---
mode: 'ask'
description: 'Atlas A2: what a job does, step by step, from its effective steps and control cards'
---
Describe this job using ONLY [the job report](../../work/job.md) (task "Atlas: job") and
[the job pack](../../work/pack.md) (task "Atlas: pack NAME (choose kind)" with kind `job` -
it carries the FULL control cards).

Focus: ${input:focus:Whole job, or one step / dataset / condition to concentrate on}

Write:

## FACTS
Step by step, in order, for the **effective steps** (`EXEC PROC=` steps are shown expanded as
`STEP.PROCSTEP (from PROC X)` - use those rows, not the PROC member):
- the effective program (not `PGM=`; IKJEFT01 / DFSRRC00 / SORT / IDCAMS are launchers),
- what its control cards make it do - quote the cards,
- datasets in and out with the `mode_source` that decided direction, GDG generation,
- the guard (`runs only when` - COND / IF) that marks error and backout steps,
- JOBLIB, JCLLIB, and any job it submits through INTRDR.

## INFERENCE
What the job achieves end to end, derived only from the facts.

## UNRESOLVED IN SCOPE
Copied verbatim: PROCs not in the index (that step's contents are UNKNOWN), DSNs still holding
`&` (`symbolic`), control-card members the JCL names but the folder lacks, no scheduler
(predecessors are UNKNOWN).

## NOT SEARCHED

Rules: use only the two reports; what they lack is UNKNOWN. Every fact carries
`[[MEMBER line "token"]]` with the token copied verbatim; DISP is not direction; never cite a
`//*` comment line. Uncitable claims go under `## UNVERIFIED` or are not written.
