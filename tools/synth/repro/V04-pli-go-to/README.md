# V04-pli-go-to - a PL/I member with GO TO in a folder named like a dataset is filed as a copybook

Reported by the verifier of ROADMAP re-parse items 20 and 22 (minor). XQPLI is a PL/I program: `ON ENDFILE(POLIN) GO TO EOJ;` and `GO TO EOJ;` begin their lines as a COBOL GO TO would. Truth: a member the toolkit cannot type (PL/I), `unknown` in a dataset-named folder with no hint. Tool: a copybook of COBOL procedure statements.

Fixed in LESSONS 248: a procedure's label and PROC (`XQPLI: PROC OPTIONS(MAIN);`) or a DECLARE with its level or attributes make it a PL/I program, never typed by a COBOL signature (`classify.not_cobol`).
