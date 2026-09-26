# F04-end-date-reference - a data name beginning with END- (END-DATE) is never recorded as a field reference

START-DATE is written on line 9 and tested on line 11; END-DATE is written on line 10 and tested on line 11. Truth: two references each. Tool: none for END-DATE, and START-DATE's test on line 11 is lost too - the `IF END-DATE < START-DATE` is cut at the END- word. `field END-DATE` says the field is never read, written or tested. END-DATE, END-TIME, END-BALANCE are everyday insurance names.

Fixed by ROADMAP re-parse item 26 (LESSONS 211): the scope terminators are a word list (END-IF, END-PERFORM, END-EVALUATE, END-READ ... `cobol.SCOPE_TERMINATORS`), so END-DATE is a data name: written on line 11 and tested on line 12, START-DATE written on line 10 and tested on line 12. The same list now ends USING lists, OPEN / CLOSE lists, GO TO ... DEPENDING and SORT USING, where the END- prefix had cut names such as WS-END-DATE.
