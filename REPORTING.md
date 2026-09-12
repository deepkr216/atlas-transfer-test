# Reporting a problem (without sharing the estate)

Three kinds of problem, three things to paste. None of them contain source
code, dataset names or file paths.

## 1. It crashed

`build` and `query` write `atlas-crash.txt` in the current folder when they
fail. Paste that file. It holds: toolkit version, Python/OS, the exception,
and the toolkit's own code frames - paths and dataset names are already
stripped. Then also run:

```
python -m atlas.diag --db atlas.db --redact
```

and paste that block too. Member names are replaced by short hashes.

## 2. It ran, but coverage looks wrong

(many `failed`/`partial` members, copybooks "not found", strange
`format detection` counts, thousands of `unresolved`)

```
python -m atlas.diag --db atlas.db --redact
```

The "failure signatures" section groups identical errors; the top three
signatures are usually one bug each.

## 3. It gave a wrong answer (the important one)

Write five lines:

```
command:   python -m atlas.query program CLMPOST
expected:  CLMPOST calls RATECALC (it does - CALL is on the line after a comment)
got:       Calls table is empty
pattern:   the CALL literal is split across two lines with a col-7 hyphen
fixture:   (below)
```

Then write a **tiny fake member** (10-30 lines) that reproduces the pattern
with invented names - exactly like the ones in `tests/fixtures/`. Fake names
are fine; what matters is the *shape*: the continuation, the level numbers,
the PROC symbolic, the PARM layout. That fixture becomes a regression test
before the fix is made (see `LESSONS.md`), so the same mistake cannot return.

Tip: `python -m atlas.query cite MEMBER a-b` prints the lines you are looking
at; copy their *shape*, replace the names.

## Never paste

- real source members, copybooks, JCL
- dataset names, library names, job names that identify the company
- anything from `out/expanded/` or `atlas.db`

The toolkit never sends anything anywhere; sharing is always your copy-paste.
