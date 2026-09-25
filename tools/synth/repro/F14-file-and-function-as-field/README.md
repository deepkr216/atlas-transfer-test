# F14-file-and-function-as-field - a SELECT file name and an intrinsic FUNCTION name are recorded as field references

IN-FILE is a file (SELECT / FD), INTEGER-OF-DATE an intrinsic function. Truth: neither is a data item, so neither is a field reference. Tool: both appear in field_ref, so `field IN-FILE` answers with references and `walk` counts them among the fields the paragraph names.
