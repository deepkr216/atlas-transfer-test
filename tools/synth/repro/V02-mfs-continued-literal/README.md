# V02-mfs-continued-literal - an MFS field literal continued on the next line with the words MOVE ... TO files the format as a copybook

Found by the verifier of ROADMAP re-parse items 20 and 22, confirmed by the acceptance test. XQFM3's DFLD literal runs past column 71 (the X in column 72 continues the statement); its continuation line opens with the literal's own text `MOVE THE CURSOR TO THE ACTION FIELD'`, which the COBOL-statement signature - checked before the MFS shape - read as a MOVE statement. Truth: MFS source, its format XQFM3 a screen row. Tool: a copybook, and `screen XQFM3` says NOT FOUND.

Fixed in LESSONS 248: the text of a literal continued from the line before (an Assembler or MFS continuation, or a COBOL one with '-' in column 7) is text to every COBOL signature (`classify.code_view`).
