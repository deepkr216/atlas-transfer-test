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
| 29 | `WHEN 3 ALSO 'M'` attributed `'M'` to the relationship field | EVALUATE subject taken as one field; ALSO positions ignored | `ConditionLogic.test_evaluate_also_attaches_when_values_by_position` |
| 30 | `IF A = 1 AND B = 'M' OR 'F'` recorded `'F'` against A as well as B | Abbreviated-condition scan ran to end of statement instead of to the next compare | `test_abbreviated_or_after_and_is_not_over_attributed` |
| 31 | `messages GENDER` missed `RELATIONSHIP/GENDER MISMATCH` built from two FILLER VALUEs | Each piece indexed, the whole never assembled | `test_message_assembled_from_fillers`, `ScreensEndToEnd.test_messages_finds_filler_assembled_text` |
| 32 | Screen validation invisible: program tests `GENDERI`, map field is `GENDER` | BMS symbolic names not derived | `BmsMfsParsing.test_bms_map_fields_defaults_and_symbolic_names`, `test_screen_dossier_links_bms_field_to_validating_program` |
| 33 | MFS field offsets wrong by 2 on output messages | `ATTR=YES` attribute bytes not counted | `test_mfs_offsets_and_type_inferred_from_fip_fop_label` |
| 34 | MFS message with no `TYPE=` classified as unknown direction | Shop names MIDs/MODs `…FIP`/`…FOP`; inference from the label with a recorded warning | same test |
| 35 | Screen labels (`'GENDER:'`) and defaults (`INITIAL='U'`) absent from message/value searches | Screen literals not indexed as literals | `test_values_on_a_screen_field_shows_default_and_validator` |

| 36 | Online programs listed as dead; "which transaction runs this" unanswerable | No routing source loaded | `RoutingParsing.*`, `RoutingEndToEnd.test_online_programs_are_no_longer_dead_candidates` |
| 37 | Re-running `build` without `--rebuild` duplicated every fact and every FTS row | `INSERT OR REPLACE` on member plus FTS rows with no member key | `IncrementalBuild.test_rerun_is_idempotent_and_changes_propagate` |
| 38 | A changed copybook left programs with facts derived from its OLD text | Incremental skip did not follow COPY dependencies | same test (asserts `exp_lines` grows after the copybook changes) |
| 39 | A dataset the CSD declares as VSAM stayed `is_vsam=0` | `INSERT OR IGNORE` after the JCL had created the row | `RoutingEndToEnd.test_cics_file_maps_to_dataset` |
| 40 | **Every `//PROCSTEP.DDNAME` override silently dropped** | JCL statement regex allowed an 8-character name only; qualified names never matched | `ProcExpansion.test_included_dd_overrides_proc_dd`, `test_qualified_override_and_added_dd`, `test_inline_sysin_override_replaces_dummy` |
| 41 | DSNs in cataloged PROCs stayed `&HLQ..MASTER`; jobs showed no datasets | Members resolved in isolation; no PROC expansion with the calling job's symbolics | `ProcExpansion.test_exec_override_beats_proc_default`, `JobLevelLineage.*` |
| 42 | `&&TEMP` treated as a symbolic and reported unresolved | Symbol regex matched the second `&` | `test_temporary_dataset_is_not_a_symbolic` |
| 43 | VSAM files had no origin: lineage began at the first reader | IDCAMS `DEFINE`/`DELETE`/`REPRO` cards not read | `test_idcams_ops`, `test_job_dossier_shows_effective_steps` |
| 44 | "Where is column X populated" unanswerable: only table names indexed | No column ↔ host-variable pairing | `ColumnLineage.*`, `ColumnQueries.test_column_report` |
| 45 | A message built by `STRING` across four lines invisible to `messages` | Only the literal pieces were indexed, never the assembled text | `test_string_and_display_templates`, `test_messages_finds_runtime_templates` |
| 46 | A program with no source (load module only) reported NOT FOUND although JCL runs it | Dossier required a program row | `test_program_runs_in_shows_the_job_not_the_bare_proc` |
| 47 | A DB2 batch step "runs IKJEFT01", no sort fields, no IDCAMS ops, because the cards are in `PROD.PARMLIB(MEMBER)` not `DD *` | Only inline data became `sysin_text`; the `(MEMBER)` reference was never followed to the indexed card member | `test_card_member_text_is_attached_to_the_dd`, `test_launcher_resolves_through_the_card_member`, `test_card_member_is_findable_by_name` |
| 48 | "PROC SRTPROC not found" and the PROC's steps counted as the job's own steps, for a `// PROC … // PEND` coded inside the job | Instream PROCs were flattened into the job instead of collected and expanded by `EXEC` | `test_instream_proc_is_collected_not_flattened_into_the_job`, `test_instream_proc_shadows_cataloged_proc` |
| 49 | `dataset SORTED` joined two unrelated jobs through `&&SORTED`; `&&TEMP` looked like an estate dataset | Temporary datasets are job-scoped but the `dataset` table is global | `test_temp_is_job_local`, `test_temp_never_becomes_a_dataset_row` |
| 50 | A step reading `DSN=*.STEP1.SORTOUT` showed no dataset - a hole in lineage exactly at the job's own intermediate file; a referback to a `(+1)` inherited the `+1` and looked like a second writer | Referbacks were kept as text; direction was copied from the source DD | `test_referback_inside_the_proc_follows_the_temp`, `test_referback_across_the_proc_boundary_reads_the_created_generation`, `test_bad_referback_is_reported_not_dropped` |

## Known gaps (not yet guarded - contributions welcome)

- SYNC alignment slack is flagged, not computed.
- GO TO and fall-through are not evaluated for dead-paragraph candidates.
- IMS program name = PSB name is an assumption (recorded per row); `APPLCTN GPSB=` is handled, `PGMTYPE=BATCH` APPLCTNs without transactions are listed as programs only.
- MFS `DO` repeats are expanded with the default 2-digit suffix only; `SUF=` and `BOUND=` are not modelled.
- Zowe command syntax is built from the documented `zos-files download all-members / data-set` forms; a shop's profile type or flags go in `extra_args` — run `--check` and `--plan` before the first real fetch.
- PDF text with CID fonts may be garbled; the extractor says so but cannot fix it.
- Compiler listings are the authoritative expansion; a listing loader would supersede `expand.py`.
