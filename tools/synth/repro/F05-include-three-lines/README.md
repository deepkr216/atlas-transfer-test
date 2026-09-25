# F05-include-three-lines - EXEC SQL INCLUDE written on three lines (EXEC SQL / INCLUDE name / END-EXEC.) is not expanded

R05INC writes the INCLUDE the way DCLGEN-era programs do - EXEC SQL, the INCLUDE on the next line, END-EXEC. on the third; R05ONE writes it on one line. Truth: both expand R05TBL (a copy_use row, the DCLGEN's fields in the program). Tool: only R05ONE does - R05INC has no copy_use row, no STATUS-CD, `copybook R05TBL` does not list it, and `flow` cannot start from the host variable. The member reads `parse: ok`.
