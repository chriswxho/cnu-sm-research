# Repository agent instructions

## Generated research datasets

- Store raw query definitions, retrieval checkpoints, API responses, source-specific CSVs, and the deduplicated 8,812-row source under `data/raw_data/`. Keep intentionally withheld queries under `data/raw_data/withheld_queries/` so default aggregation does not include them.
- Put every newly generated handoff bundle in `data/{dts-stamp}/`.
- Format `{dts-stamp}` as Pacific local time (`America/Los_Angeles`) using `YYYY-MM-DD_HH-MM-SS`, for example `data/2026-09-05_21-04-32/`. Use the applicable PST or PDT offset automatically.
- Write the classification-ready post dataset as `data/{dts-stamp}/dedup_posts.csv`.
- Write the corresponding annotation matrix as `data/{dts-stamp}/agent_codebook_annotations.csv`, reusing the exact directory from its `dedup_posts.csv` input even when annotations are generated later.
- Write derived analyses tied to that post/annotation pair into the same directory with stable descriptive filenames, such as `agent_codebook_top_subreddits.csv`.
- Write `filter_metadata.csv` in every handoff bundle. Use one row per applied rule and include a human-readable column titled `Removal criteria`, plus the action, sequential input count, matched count, output count, and any relevant reason breakdown.
- Treat timestamped bundle directories as immutable handoff artifacts: never overwrite an existing artifact within one.
- Require the paired post and annotation files to contain the same unique post IDs in the same order. Annotation term cells must contain only integer `0` or `1` values.
- Store processed audit outputs and review shards inside the timestamped bundle for the stage that produced them. Do not write generated data files directly under `data/`; reserve that level for `raw_data/`, timestamped bundles, code, and documentation.
- Validate CSV record counts with a CSV parser, not `wc -l`, because post bodies may contain embedded newlines.
