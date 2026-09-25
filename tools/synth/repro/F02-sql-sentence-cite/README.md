# F02-sql-sentence-cite - EXEC SQL statements in one sentence are cited at the sentence's first line (statements and host-variable references)

The MOVE on line 10 opens a sentence that holds a SELECT (line 11) and an UPDATE (line 15). Truth: each statement cited at its own first line. Tool: both at line 10, and the host variables the SQL reads/writes at line 10 too. `table PRD.POLICY_TBL` then prints two different statements with the same cite.

In the estate: every DB2 program (POLDB201 and its twins), 6 statements each.
