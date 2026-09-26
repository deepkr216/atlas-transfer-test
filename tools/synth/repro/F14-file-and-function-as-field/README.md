# F14-file-and-function-as-field - a SELECT file name and an intrinsic FUNCTION name are recorded as field references

IN-FILE is a file (SELECT / FD), INTEGER-OF-DATE an intrinsic function. Truth: neither is a data item, so neither is a field reference. Tool: both appear in field_ref, so `field IN-FILE` answers with references and `walk` counts them among the fields the paragraph names.

Fixed by ROADMAP re-parse item 26 (LESSONS 211): OPEN and CLOSE record no field reference, START and DELETE not their file, a SELECT / FD name is never a field reference, and the name after FUNCTION is the function's - its argument WS-DATE is read on line 18, INTEGER-OF-DATE is no field.
