# F05-include-three-lines - EXEC SQL INCLUDE written on three lines (EXEC SQL / INCLUDE name / END-EXEC.) is not expanded

R05INC writes the INCLUDE the way DCLGEN-era programs do - EXEC SQL, the INCLUDE on the next line, END-EXEC. on the third; R05ONE writes it on one line. Truth: both expand R05TBL (a copy_use row, the DCLGEN's fields in the program). Tool: only R05ONE does - R05INC has no copy_use row, no STATUS-CD, `copybook R05TBL` does not list it, and `flow` cannot start from the host variable. The member reads `parse: ok`.

Fixed by ROADMAP re-parse item 27 (LESSONS 214): the expander reads the words of an INCLUDE over the lines after an `EXEC SQL` that ends its line, so R05INC expands R05TBL like R05ONE - its copy_use row at the EXEC SQL line, STATUS-CD its own field, `copybook R05TBL` lists both programs. Once the words are there only END-EXEC may follow: a statement without one ends there and the next line stays the program's.
