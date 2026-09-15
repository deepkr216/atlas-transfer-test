# Release facts

Copy this file to `work/release-facts.md` and fill it in. It holds what no
source file or document states, so the transition document can cite it by
path and line, `[[work/release-facts.md 12 "token"]]`, and the gate checks
that line like any other. Leave a line as UNKNOWN rather than guessing; the
document carries it under HUMAN MUST VERIFY.

## Identity
- Release name / change record number: UNKNOWN
- Go-live date and time: UNKNOWN
- Freeze / backout deadline: UNKNOWN
- Systems and libraries promoted (the PDS names, one per line): UNKNOWN

## Members promoted
One per line: member name, kind (program / copybook / JCL / PROC / control card / PSB / DBD / MFS),
and the story or requirement it belongs to.
- UNKNOWN

## Documents
- Implementation plan: (document name as `doc --list` shows it)
- Stories / requirements: (document names)
- Test results workbooks: (document names)

## People
- Development contact: UNKNOWN
- Application Operations on-call: UNKNOWN
- Contact Center lead: UNKNOWN
- Escalation path (who, then who): UNKNOWN

## Operations instructions not in any file
- Schedule changes (new jobs, moved jobs, removed jobs): UNKNOWN
- Manual steps at go-live (loads, conversions, one-time jobs): UNKNOWN
- Backout procedure and its owner: UNKNOWN

## Contact Center instructions not in any file
- What agents may tell customers about the change: UNKNOWN
- Letters, notices or screens customers will receive or see: UNKNOWN
- When to escalate, and to whom: UNKNOWN
