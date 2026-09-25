# F15-ws-exec-sql-no-period - an EXEC SQL in WORKING-STORAGE without its period (a compile error) erases every procedure-division fact while the member reads ok

The DECLARE CURSOR's END-EXEC lacks its period, which the compiler rejects. The toolkit is not a compiler and need not accept it - but it says nothing: no paragraphs, no PERFORM, no OPEN, and `parse: ok`. A member with a data division and no paragraphs is a shape worth one line in the notes.
