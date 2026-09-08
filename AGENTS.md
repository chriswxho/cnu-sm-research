# Repository agent instructions

## Generated research datasets

- Store raw query definitions, retrieval checkpoints, API responses, source-specific CSVs, and the deduplicated 8,812-row source under `data/raw_data/`. Keep intentionally withheld queries under `data/raw_data/withheld_queries/` so default aggregation does not include them.
- Put every newly generated handoff bundle in `data/{dts-stamp}/`.
- Format `{dts-stamp}` as Pacific local time (`America/Los_Angeles`) using `YYYY-MM-DD_HH-MM-SS`, for example `data/2026-09-05_21-04-32/`. Use the applicable PST or PDT offset automatically.
- Write the classification-ready post dataset as `data/{dts-stamp}/dedup_posts.csv`.
- By default, keep each handoff bundle limited to exactly two top-level CSV files: `dedup_posts.csv` and `filter_metadata.csv`.
- Do not generate or add `agent_codebook_annotations.csv` or `agent_codebook_top_subreddits.csv` unless the user explicitly asks for agent-codebook annotations or statistics. Do not create an `agent_codebook/` subdirectory.
- When explicitly requested, write the corresponding annotation matrix as `data/{dts-stamp}/agent_codebook_annotations.csv`, reusing the exact directory from its `dedup_posts.csv` input even when annotations are generated later.
- When explicitly requested, write derived analyses tied to that post/annotation pair into the same directory with stable descriptive filenames, such as `agent_codebook_top_subreddits.csv`.
- Write `filter_metadata.csv` in every handoff bundle. Use one row per applied rule and include a human-readable column titled `Removal criteria`, plus the action, sequential input count, matched count, output count, and any relevant reason breakdown.
- Treat timestamped bundle directories as immutable handoff artifacts: never overwrite an existing artifact within one.
- Require annotation post IDs to be unique and to appear in the same relative order as their posts in `dedup_posts.csv`. The annotation IDs may be an ordered subset when posts are intentionally withheld from agent-codebook analysis. Annotation term cells must contain only integer `0` or `1` values.
- Do not retain classified-row audits, exclusion extracts, review shards, withheld-post extracts, or other generated files in a handoff bundle. Preserve any required filter counts and reasons in `filter_metadata.csv`. Do not write generated data files directly under `data/`; reserve that level for `raw_data/`, timestamped bundles, code, and documentation.
- Validate CSV record counts with a CSV parser, not `wc -l`, because post bodies may contain embedded newlines.
