---
mode: 'ask'
description: 'Atlas A5: abend triage - three ranked hypotheses, each with a disconfirming check'
---
Triage this abend using ONLY: [the JES messages](../../work/abend.txt) (paste the JESMSGLG /
JESYSMSG lines, the failing step and the abend code or SQLCODE / IMS status into that file),
[the job report](../../work/job.md) (task "Atlas: job"), [the pack](../../work/pack.md) (task
"Atlas: pack" on the failing program), [the conditions](../../work/conditions.md) (task
"Atlas: conditions") and [the layout](../../work/layout.md) (task "Atlas: layout" on the record
being processed).

Abend / code: ${input:code:e.g. S0C7, S0C4, SQLCODE -805, IMS status AI}

Write:

## FACTS
The step, the effective program, its inputs at that step, the paragraphs and statements the
reports show around the failing operation - each cited.

## HYPOTHESES
At most three, ranked. For each: the mechanism, the facts that support it (cited), and the ONE
check that would disconfirm it (a dataset to inspect, a field to dump, a bind to compare).
Per code, the reports already give you: S0C7 - which COMP-3 / numeric fields the layout puts at
the record offsets, and the tests around them; S0C4 - call sites whose USING count differs from
the callee's LINKAGE; SQLCODE -805 / -818 - bind or timestamp, not code; -911 / -913 -
contention, look at concurrent jobs; IMS AI / AJ / AM - an update function on a PROCOPT=G PCB
(the pack shows database and PROCOPT per call); GE / GB - SSA or path, not a bug.
If the offset in a S0C7 cannot be mapped without the compile listing, say "cannot localise".

## UNRESOLVED IN SCOPE
## NOT SEARCHED

Rules: use only the attached files; what they lack is UNKNOWN. Every fact carries
`[[MEMBER line "token"]]` (token copied verbatim, never a comment line); uncitable claims go
under `## UNVERIFIED` or are not written. Never state a cause as certain; the ranking and the
disconfirming checks are the deliverable.
