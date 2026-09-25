# F04-end-date-reference - a data name beginning with END- (END-DATE) is never recorded as a field reference

START-DATE is written on line 9 and tested on line 11; END-DATE is written on line 10 and tested on line 11. Truth: two references each. Tool: none for END-DATE, and START-DATE's test on line 11 is lost too - the `IF END-DATE < START-DATE` is cut at the END- word. `field END-DATE` says the field is never read, written or tested. END-DATE, END-TIME, END-BALANCE are everyday insurance names.
