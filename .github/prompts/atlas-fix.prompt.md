---
mode: 'ask'
description: 'Atlas: the gate failed - repair the answer using only the pack'
---
The citation gate rejected [the answer](../../work/answer.md). Its report is
[work/gate.txt](../../work/gate.txt) (task "Atlas: verify work/answer.md -> work/gate.txt").
Repair the answer using ONLY [the pack](../../work/pack.md) and the other reports that were
attached when it was written.

For each line of the gate report:
- `FAIL ... token not on line` - the quoted token is not on the cited line. Find the line in the
  pack that really holds it and cite that line, copying the token verbatim; if no line holds it,
  the claim is not supported: move it to `## UNVERIFIED` or delete it.
- `FAIL ... member not in index` / `not found` - the member name is wrong or the fact came from
  memory. Same treatment: find it in the pack or drop the claim.
- `FAIL ... changed since index` - the index is stale; tell the user to rebuild, do not guess.
- `WARN ... comment line` - the fact is cited to a `//*` or `*` line; cite the statement instead.
- `WARN ... short token` / `wide range` - cite the exact line with a token of six or more characters.
- `? <line>` (assertive line with no citation) - add the citation from the pack, or move the
  sentence to `## UNVERIFIED`, or delete it.

Return the complete corrected answer, same headings (`## FACTS`, `## INFERENCE`,
`## UNRESOLVED IN SCOPE`, `## NOT SEARCHED`, and `## UNVERIFIED` if anything remains
uncitable), ready to be saved over `work/answer.md` and verified again. Do not soften a claim
to make it pass; a claim without a line behind it is removed, not reworded.
