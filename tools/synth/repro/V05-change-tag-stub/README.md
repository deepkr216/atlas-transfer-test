# V05-change-tag-stub - a copybook whose lines carry a change tag in columns 1-6 and hold one line of digits is filed as a stub

Found by the verifier of ROADMAP re-parse item 23, confirmed by the acceptance test. Every line of QXTAG starts with the change tag `*CR01 ` in the sequence area; one line of its 88-level VALUES holds only `004 005`. The stub test read a record whose first character is '*' and with words where the compiler reads as a remark, so the one line of digits was all it counted. Truth: a copybook (the reader reads columns 8-72 as code whatever the sequence area holds). Tool: `stub`, never expanded, QXPGM `partial`.

Fixed in LESSONS 248: such a record is a remark only when its text runs through column 7, as a floating `*>` remark from column 1 does; a '*' in the sequence area with a blank, D or '-' in column 7 is a tag.
