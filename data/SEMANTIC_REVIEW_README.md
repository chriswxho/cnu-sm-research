# Semantic review of `data/raw_data/dedup_posts.csv`

## Research criterion

A post is `INCLUDE` only when its title/body presents a real-life first-person
account in which the Reddit author personally may have been or was abused,
bullied, threatened, coerced, controlled, assaulted, or subjected to a harmful
ongoing pattern by the author's own biological, adoptive, half, step, or foster
sibling.

`FALSE_POSITIVE` is used when the text clearly falls outside that scope, such
as an in-law or other person's sibling, a third-person victim, a perpetrator-only
account, fiction/media, porn or roleplay, generic research/news, a repost,
nonhuman sibling behavior, ordinary conflict, or an irrelevant query collision.

`UNCERTAIN` is used when the available title/body does not establish enough
information to decide. These rows should be reviewed rather than automatically
discarded.

## Method

Three LLM subagents reviewed disjoint row ranges. The final judgment for every
row was made from the full `title` and `body`, with a post-specific rationale.
Regexes and subreddit/query metadata were not used to assign the final labels.
Code was used only to parse batches, save decisions, merge shards, and validate
coverage.

A second curation pass applies six criteria to rows initially labeled
`INCLUDE` or `UNCERTAIN`:

- Body text equal to `[removed]` after trimming whitespace and ignoring case is
  excluded deterministically. Bodies that merely contain the placeholder are
  not included in this rule.
- Body text containing only `[deleted]` is excluded as `DELETED_BODY`. A
  `[deleted]` placeholder followed only by Reddit's standalone poll link is
  also excluded because it contains no narrative text.
- Bodies with no non-whitespace text are excluded as `BLANK_BODY`.
- Bodies containing only one standalone plain, angle-bracket, or Markdown link
  and no narrative text are excluded as `LINK_ONLY_BODY`. Posts that provide
  any explanatory text alongside a link are retained by this rule.
- Posts whose complete `matching_queries` membership consists only of
  `sibling aggression`, `aggressive sibling`, `aggressive sister`, and/or
  `aggressive brother` are withheld as `AGGRESSION_QUERY_ONLY`. A post remains
  eligible when it also matches any other qualifying query.
- Three LLM subagents audited disjoint ranges covering all 5,089 candidates
  left after the `[removed]` rule. They judged the dominant meaningful language
  from the title and full body, without using subreddit or query metadata.
  Offline language recognition, script scans, and language-marker scans were
  used to find plausible non-English or mixed-language rows for full-text
  review, not to make the final decision. English-dominant posts with foreign
  quotations or minor code-switching were retained.

The language pass excludes only confidently `NON_ENGLISH` posts. Substantially
mixed posts with no safely dominant language are marked `LANGUAGE_UNCERTAIN`,
retained in the final result, and written to a separate review file.

The source file was not modified. Its SHA-256 is:

`ce2ba3c970784277e6ebaa17103ac63b40f0a8657fe405f72f99b537dc2edc8e`

Merge QA found 20 early shard decisions whose stored links did not match the
current source rows. Those exact rows (181–199 and 311) were re-read from the
current source and saved in
`2026-07-28_21-20-11/semantic_review_shards/corrections_0181_0199_0311.csv`
before final output generation.

## Results

| Classification | Rows |
|---|---:|
| `INCLUDE` | 5,485 |
| `FALSE_POSITIVE` | 3,000 |
| `UNCERTAIN` | 327 |
| **Total** | **8,812** |

Secondary curation of the 5,812 initial `INCLUDE` plus `UNCERTAIN` rows:

| Curation decision | Rows |
|---|---:|
| Excluded: body is `[removed]` | 723 |
| Excluded: body is `[deleted]` or `[deleted]` plus a link | 646 |
| Excluded: blank body | 100 |
| Excluded: link-only body | 1 |
| Withheld: aggression-query membership only | 106 |
| Excluded: confidently non-English | 10 |
| Retained but language uncertain | 5 |
| Retained: primarily English | 4,221 |
| **Final retained result** | **4,226** |

False-positive reasons:

| Reason | Rows |
|---|---:|
| `THIRD_PERSON_VICTIM` | 768 |
| `FICTION_MEDIA` | 604 |
| `WRONG_RELATION` | 439 |
| `META_RESEARCH_GENERIC` | 332 |
| `SEXUAL_FANTASY_ROLEPLAY` | 310 |
| `REPOST_ARCHIVE` | 170 |
| `ORDINARY_CONFLICT` | 104 |
| `NONHUMAN` | 90 |
| `PERPETRATOR_ONLY` | 90 |
| `IRRELEVANT_KEYWORD` | 83 |
| `JOKE_OR_HYPOTHETICAL` | 10 |

Uncertain reasons:

| Reason | Rows |
|---|---:|
| `INSUFFICIENT_CONTEXT` | 322 |
| `REALITY_STATUS_UNCLEAR` | 2 |
| `RELATIONSHIP_UNCLEAR` | 2 |
| `VICTIM_STATUS_UNCLEAR` | 1 |

## Outputs

The current processed outputs are stored together in the applicable
`YYYY-MM-DD_HH-MM-SS/` bundle:

- `dedup_posts_classified.csv`: all 8,812 source rows in original order, with
  `review_row_index`, `classification`, `exclusion_reason`, and
  `classification_rationale`, plus the secondary `curation_*` audit columns.
- `dedup_posts_false_positives.csv`: the 3,000 clear false positives.
- `dedup_posts_uncertain.csv`: the 327 rows that need additional review.
- `dedup_posts_curation_exclusions.csv`: the 1,586 initially retained rows
  removed or withheld by the curation criteria (723 removed bodies, 646
  deleted bodies, 100 blank bodies, one link-only body, 106
  aggression-query-only memberships, and 10 non-English posts). Reasons are
  mutually exclusive and assigned in the order listed above.
- `dedup_posts_language_uncertain.csv`: the 5 mixed-language rows retained for
  now but flagged for human review.
- `dedup_posts.csv`: the final 4,226-row result in
  original source order, with semantic false positives, disallowed body types,
  aggression-query-only memberships, and confidently non-English submissions
  omitted.
- Each bundle is an immutable Pacific-time-stamped snapshot. The timestamp uses
  `America/Los_Angeles`, applying PST or PDT as appropriate. Related annotations
  and analyses use stable filenames in the same timestamped directory, making
  the directory an upload-ready bundle. Use the newest timestamped directory as
  the input to a new classification run.
- `YYYY-MM-DD_HH-MM-SS/filter_metadata.csv`: one row per semantic or curation
  rule applied to that bundle, including the rule definition and sequential
  before, matched, and after counts.

Run `python3 data/merge_semantic_reviews.py` to validate and regenerate the
output CSVs into a new timestamped bundle from the saved semantic- and
language-review shards.

## Limitations

- The review determines whether a post is presented as a real personal account;
  it cannot verify the poster's identity or whether the reported events occurred.
- These labels are research-screening judgments, not clinical or legal findings.
- The deliberately broad “potential abuse” threshold favors retaining plausible
  first-person victim accounts. Ambiguous records remain separate instead of
  being treated as false positives.
- Dominant-language judgments can be difficult for heavily code-switched text;
  the five genuinely mixed cases remain explicit rather than being silently
  classified as English or non-English.
