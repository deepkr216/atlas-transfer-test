# Lessons - wrong facts we have produced, and the test that now prevents each

The rule: when a wrong fact is found (by a human, by the citation gate, by a
smoke run), the fixture and the failing assertion land in `tests/` **first**,
then the fix, then a row here. Nothing is "fixed" until the test exists.

| # | Wrong fact produced | Root cause | Guarded by |
|---|---|---|---|
| 1 | Identifiers made of change-ticket stamps; statements never terminating | Identification area (cols 73-80) leaked into code because records were 79 columns, not 80 | `RaggedMargins.test_identification_area_does_not_leak` |
| 2 | Every copybook field at offset 0 | A copybook *fragment* (05s with no 01) parsed as separate roots | `PMASTREC` offsets in `test_multiline` |
| 3 | Variable-length table indexed as fixed | `OCCURS ... DEPENDING ON` on a continuation line without a hyphen was dropped | `test_occurs_depending_on_across_lines` |
| 4 | Paragraph `1000-EXIT` missing from the PERFORM graph | `1000-EXIT.  EXIT.` treated as one statement | `test_paragraph_header_and_statement_on_same_line` |
| 5 | Single-word statements (`EXIT.`) indexed as paragraphs | Area A check without a reserved-word guard | same test |
| 6 | `PIC ZZZ,ZZ9.99` truncated to `ZZZ,ZZ9` | Period inside a picture string / numeric literal treated as terminator | `test_pic_with_embedded_period_is_not_split` |
| 7 | `PROGRAM-ID` unknown when the name is on the next line | Name looked for in the same statement only | `test_program_id_on_following_line` |
| 8 | Commented-out `CALL 'OLDRATER'` reported as a live dependency | Column-7 `*` ignored | `CommentedCode.test_commented_call_is_not_an_edge` |
| 9 | Dynamic `CALL WS-SUB-PROGRAM` reported as calling nothing | Only one literal traced; VALUE clause missed | `test_dynamic_call_resolves_every_candidate` |
| 10 | Continued literal lost its first character | Fixture literal extended into column 73 - the *compiler* would drop it too; fixture was wrong, parser right | `test_column7_hyphen_literal_continuation` |
| 11 | `MOVE WS-CALL-FLAG TO WS-KEY` indexed as a dynamic CALL to program `TO` | `\b` treats `-` as a word boundary; every verb pattern needed hyphen-aware lookarounds | `test_identifier_containing_call_is_not_a_call` |
| 12 | DCLGEN host structures invisible | `EXEC SQL INCLUDE` not treated as a copy; `++INCLUDE`/`-INC` live in the sequence area | `test_exec_sql_include_is_a_copy` |
| 13 | DD name `UT-S-CLMFILE` never joined to the JCL | ASSIGN device/organisation prefixes not stripped | `test_assign_with_device_prefix_yields_ddname` |
| 14 | Host variables harvested as DB2 tables | `INTO` matched in `SELECT ... INTO :hv` | `test_select_into_hostvar_is_not_a_table` |
| 15 | Column `PREM_AMT` reported as a table | `FOR UPDATE OF col` matched as `UPDATE table` | `test_for_update_of_is_not_a_table` |
| 16 | Second `MOVE` inside `IF ... ELSE ... END-IF` not indexed | Only the first verb of a compound statement dispatched | `test_both_moves_inside_compound_if_are_captured` |
| 17 | Both MOVEs cited to the IF's line | Statement start used as the line for every verb inside it | same test (asserts lines 30 and 32) |
| 18 | Batch lineage arrows reversed | `DISP=OLD/SHR` read as input, `NEW` as output; DISP is not direction | `JclDirection.test_disp_old_is_not_direction`, `test_gdg_plus_one_is_output` |
| 19 | Sort steps treated as pass-through | Control-card byte positions not extracted | `test_sort_card_byte_positions` |
| 20 | DBD/PSB members classified as `unknown` | Signatures anchored on leading whitespace; HLASM puts the label in column 1 | `test_classify_hlasm_label_and_level_49` |
| 21 | DCLGEN copybooks with level 49 misfiled | Classifier tested only levels 01/05/10/15 | same test |
| 22 | PSB `KEYLEN` missing | Column-72 continuation not joined | `test_psb_column72_continuation_and_cmpat` |
| 23 | Wrong database named for a DL/I call | I/O PCB shifts positions under `CMPAT=YES`/TP; region type decides otherwise | same test + `test_psb_without_cmpat_is_undetermined` |
| 24 | Fields renamed by `COPY ... REPLACING` untraceable | No expanded source | `CopybookExpansion.test_replacing_and_line_map` |
| 25 | Citations drift after expansion | Line numbers not mapped back to the including member | same test (asserts origin of a post-COPY line) |
| 26 | Missing copybook silently ignored | Expansion swallowed the failure | `test_missing_copybook_is_a_warning_not_silence` |
| 27 | Legacy `.doc` counted as "indexed, empty" | No extractor, no report | `test_legacy_binary_is_reported_not_silent` |
| 28 | A model's citation to a comment line accepted | Gate checked text presence only | `CitationGate.test_citation_to_commented_line_warns` |

## Known gaps (not yet guarded - contributions welcome)

- SYNC alignment slack is flagged, not computed.
- GO TO and fall-through are not evaluated for dead-paragraph candidates.
- CICS CSD / IMS SYSGEN loaders for `transaction_def` are not written.
- PDF text with CID fonts may be garbled; the extractor says so but cannot fix it.
- Compiler listings are the authoritative expansion; a listing loader would supersede `expand.py`.
