# Semantic review of `dedup_posts.csv`

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

The source file was not modified. Its SHA-256 is:

`ce2ba3c970784277e6ebaa17103ac63b40f0a8657fe405f72f99b537dc2edc8e`

Merge QA found 20 early shard decisions whose stored links did not match the
current source rows. Those exact rows (181–199 and 311) were re-read from the
current source and saved in `semantic_review_shards/corrections_0181_0199_0311.csv`
before final output generation.

## Results

| Classification | Rows |
|---|---:|
| `INCLUDE` | 5,485 |
| `FALSE_POSITIVE` | 3,000 |
| `UNCERTAIN` | 327 |
| **Total** | **8,812** |

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

- `dedup_posts_classified.csv`: all 8,812 source rows in original order, with
  `review_row_index`, `classification`, `exclusion_reason`, and
  `classification_rationale`.
- `dedup_posts_false_positives.csv`: the 3,000 clear false positives.
- `dedup_posts_uncertain.csv`: the 327 rows that need additional review.
- `dedup_posts_included_with_uncertain.csv`: the 5,485 included rows plus the
  327 uncertain rows, in original source order, with confirmed false positives
  omitted.

Run `python3 data/merge_semantic_reviews.py` to validate and regenerate the
three output CSVs from the saved semantic-review shards.

## Limitations

- The review determines whether a post is presented as a real personal account;
  it cannot verify the poster's identity or whether the reported events occurred.
- These labels are research-screening judgments, not clinical or legal findings.
- The deliberately broad “potential abuse” threshold favors retaining plausible
  first-person victim accounts. Ambiguous records remain separate instead of
  being treated as false positives.
