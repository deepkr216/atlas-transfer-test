# V06-program-id-field - a copybook with a field named CA-PROGRAM-ID is taken for a program and dropped from the choice

Found by the verifier of ROADMAP re-parse items 11 and 12, confirmed by the acceptance test. GC's KCCOMM1 carries `05 CA-PROGRAM-ID PIC X(8).`; `\bPROGRAM-ID\b` matched after the hyphen, so the classifier filed the copybook `cobol`, the build wrote a program row named PIC for it, and the resolver - which drops a member holding a PROGRAM-ID from a choice among several - expanded SHARED's copy into KCPGM with no 'ambiguous_copybook' row and without CA-PROGRAM-ID. Truth: GC's copy (same system), the choice recorded, the field there.

Fixed in LESSONS 248: PROGRAM-ID (and a DIVISION header) is the paragraph's own word, never the tail of a data-name, and a literal is blanked before the words are looked for.
