#!/usr/bin/env python3
"""Merge semantic and language-review shards into auditable CSV outputs.

This script does not semantically classify posts. It validates and merges the
row-level decisions produced by LLM subagents, applies the deterministic
curation exclusions, and writes an immutable Pacific-time-stamped
classification set.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


DATA_DIR = Path(__file__).resolve().parent
RAW_DATA_DIR = DATA_DIR / "raw_data"
SOURCE = RAW_DATA_DIR / "dedup_posts.csv"
SEMANTIC_BUNDLE_DIR = DATA_DIR / "2026-07-28_21-20-11"
SHARD_DIR = SEMANTIC_BUNDLE_DIR / "semantic_review_shards"
INITIAL_SHARD = SHARD_DIR / "part_0000_0979.tsv"
CORRECTED_INDICES = set(range(181, 200)) | {311}
SHARDS = [
    INITIAL_SHARD,
    SHARD_DIR / "part_0980_1958.csv",
    SHARD_DIR / "part_1959_2937.tsv",
    SHARD_DIR / "part_2938_4406.csv",
    SHARD_DIR / "part_4407_5140.tsv",
    SHARD_DIR / "part_5141_5874.csv",
    SHARD_DIR / "manual_5875_8811.jsonl",
    SHARD_DIR / "part_8255_8532.tsv",
    SHARD_DIR / "part_8533_8811.csv",
    SHARD_DIR / "corrections_0181_0199_0311.csv",
]
VALID_LABELS = {"INCLUDE", "FALSE_POSITIVE", "UNCERTAIN"}
LANGUAGE_BUNDLE_DIR = DATA_DIR / "2026-08-30_18-18-07"
LANGUAGE_SHARD_DIR = LANGUAGE_BUNDLE_DIR / "language_review_shards"
LANGUAGE_SHARDS = [
    LANGUAGE_SHARD_DIR / "part_0000_2937.csv",
    LANGUAGE_SHARD_DIR / "part_2938_5874.csv",
    LANGUAGE_SHARD_DIR / "part_5875_8811.csv",
]
VALID_LANGUAGE_CLASSIFICATIONS = {"NON_ENGLISH", "UNCERTAIN"}
PACIFIC_TIME = ZoneInfo("America/Los_Angeles")
RESULT_CLASSIFICATIONS = {"INCLUDE", "UNCERTAIN"}
WITHHELD_QUERY_TERMS = {
    "sibling aggression",
    "aggressive sibling",
    "aggressive sister",
    "aggressive brother",
}
CURATION_FIELDS = [
    "curation_status",
    "curation_exclusion_reason",
    "curation_language",
    "curation_rationale",
]
FILTER_METADATA_FIELDS = [
    "stage_order",
    "stage_name",
    "rule_order",
    "rule_id",
    "action",
    "Removal criteria",
    "records_before",
    "records_matched",
    "records_after",
    "notes",
]
FILTER_RULE_DEFINITIONS = {
    "REMOVED_BODY": (
        "contains only text: '[removed]' after trimming whitespace "
        "(case-insensitive)"
    ),
    "DELETED_BODY": (
        "contains only text: '[deleted]', optionally followed by one "
        "standalone link"
    ),
    "BLANK_BODY": "contains only: blank or whitespace body",
    "LINK_ONLY_BODY": (
        "contains only: one standalone plain or Markdown link"
    ),
    "AGGRESSION_QUERY_ONLY": (
        "search-term membership contains only: 'sibling aggression', "
        "'aggressive sibling', 'aggressive sister', or 'aggressive brother'"
    ),
    "NON_ENGLISH": (
        "contains category: non-English language"
    ),
    "LANGUAGE_UNCERTAIN": (
        "retained category: uncertain or mixed language"
    ),
}
URL_PATTERN = r"(?:https?://|www\.)[^\s<>]+"
PLAIN_LINK_ONLY_PATTERN = re.compile(
    rf"^(?:{URL_PATTERN}|<{URL_PATTERN}>)$",
    flags=re.IGNORECASE,
)
MARKDOWN_LINK_ONLY_PATTERN = re.compile(
    rf"^\[[^\]\r\n]*\]\s*\(\s*<?{URL_PATTERN}>?"
    r"(?:\s+[\"'][^\"']*[\"'])?\s*\)$",
    flags=re.IGNORECASE,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_delimited(path: Path) -> list[dict[str, str]]:
    delimiter = "\t" if path.suffix == ".tsv" else ","
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle, delimiter=delimiter))


def read_shard(path: Path) -> list[dict[str, str]]:
    if path.suffix != ".jsonl":
        return read_delimited(path)
    records: list[dict[str, str]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return records


def canonical_reason(label: str, raw_reason: str) -> str:
    if label == "INCLUDE":
        return ""

    reason = re.sub(r"[^a-z0-9]+", "_", (raw_reason or "").lower()).strip("_")

    if label == "UNCERTAIN":
        if "victim_status" in reason:
            return "VICTIM_STATUS_UNCLEAR"
        if any(token in reason for token in ("relationship", "narrator", "ambiguous_sibling")):
            return "RELATIONSHIP_UNCLEAR"
        if "reality_status" in reason:
            return "REALITY_STATUS_UNCLEAR"
        return "INSUFFICIENT_CONTEXT"

    if any(token in reason for token in ("porn", "roleplay", "sexual_fantasy", "fantasy")):
        return "SEXUAL_FANTASY_ROLEPLAY"
    if any(
        token in reason
        for token in (
            "fiction",
            "media",
            "book",
            "game",
            "writing_prompt",
            "clip",
        )
    ):
        return "FICTION_MEDIA"
    if any(token in reason for token in ("repost", "archive", "transcription", "copypasta")):
        return "REPOST_ARCHIVE"
    if any(token in reason for token in ("nonhuman", "animal")):
        return "NONHUMAN"
    if "perpetrator" in reason:
        return "PERPETRATOR_ONLY"
    if any(token in reason for token in ("third_person", "third_party", "observer")):
        return "THIRD_PERSON_VICTIM"
    if any(
        token in reason
        for token in (
            "in_law",
            "wrong_relation",
            "other_relation",
            "partner_sibling",
            "unrelated_people",
            "parent_history",
        )
    ):
        return "WRONG_RELATION"
    if any(
        token in reason
        for token in (
            "ordinary",
            "no_targeted_abuse",
            "no_abuse",
            "non_abuse",
            "nonabusive",
            "sibling_conflict",
            "passive_aggressive",
            "caregiving",
        )
    ):
        return "ORDINARY_CONFLICT"
    if any(
        token in reason
        for token in ("joke", "meme", "hypothetical", "parody", "satire", "sports")
    ):
        return "JOKE_OR_HYPOTHETICAL"
    if any(
        token in reason
        for token in (
            "generic",
            "research",
            "news",
            "article",
            "public_figure",
            "meta",
            "podcast",
            "resource",
            "promotional",
            "educational",
            "commentary",
            "question",
            "subreddit_announcement",
        )
    ):
        return "META_RESEARCH_GENERIC"
    if any(
        token in reason
        for token in (
            "irrelevant",
            "tangential",
            "no_personal_victimization",
            "title_collision",
            "keyword",
            "link_only",
            "test_post",
        )
    ):
        return "IRRELEVANT_KEYWORD"
    raise ValueError(f"Unmapped FALSE_POSITIVE reason: {raw_reason!r}")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def timestamped_dataset_path(moment: Optional[datetime] = None) -> Path:
    """Return a Pacific-time-stamped classification dataset path."""
    generated_at = moment or datetime.now(timezone.utc)
    stamp = generated_at.astimezone(PACIFIC_TIME).strftime(
        "%Y-%m-%d_%H-%M-%S"
    )
    return DATA_DIR / stamp / "dedup_posts.csv"


def build_filter_metadata(
    *,
    source_count: int,
    false_positive_count: int,
    semantic_uncertain_count: int,
    semantic_reason_counts: Counter[str],
    curation_rule_counts: list[tuple[str, int]],
) -> list[dict[str, str]]:
    """Build sequential, machine-readable metadata for applied filters."""
    if false_positive_count > source_count:
        raise ValueError("False-positive count exceeds the source count")

    semantic_output = source_count - false_positive_count
    reason_notes = "; ".join(
        f"{reason}={count}"
        for reason, count in sorted(semantic_reason_counts.items())
    )
    rows = [
        {
            "stage_order": "1",
            "stage_name": "semantic_review",
            "rule_order": "1",
            "rule_id": "SEMANTIC_FALSE_POSITIVE",
            "action": "EXCLUDE",
            "Removal criteria": (
                "contains category: semantic false positive (not a "
                "first-person account of real or plausibly real harmful "
                "sibling behavior)"
            ),
            "records_before": str(source_count),
            "records_matched": str(false_positive_count),
            "records_after": str(semantic_output),
            "notes": reason_notes,
        },
        {
            "stage_order": "1",
            "stage_name": "semantic_review",
            "rule_order": "2",
            "rule_id": "SEMANTIC_UNCERTAIN",
            "action": "RETAIN_FLAGGED",
            "Removal criteria": (
                "retained category: semantic review uncertain"
            ),
            "records_before": str(semantic_output),
            "records_matched": str(semantic_uncertain_count),
            "records_after": str(semantic_output),
            "notes": "Uncertain records remain in the classification set.",
        },
    ]

    current_count = semantic_output
    for rule_order, (rule_id, matched_count) in enumerate(
        curation_rule_counts,
        start=1,
    ):
        if rule_id not in FILTER_RULE_DEFINITIONS:
            raise ValueError(f"Unknown filter metadata rule: {rule_id}")
        action = (
            "RETAIN_FLAGGED"
            if rule_id == "LANGUAGE_UNCERTAIN"
            else "EXCLUDE"
        )
        next_count = (
            current_count
            if action == "RETAIN_FLAGGED"
            else current_count - matched_count
        )
        if matched_count < 0 or next_count < 0:
            raise ValueError(f"Invalid matched count for {rule_id}")
        rows.append(
            {
                "stage_order": "2",
                "stage_name": "deterministic_curation",
                "rule_order": str(rule_order),
                "rule_id": rule_id,
                "action": action,
                "Removal criteria": FILTER_RULE_DEFINITIONS[rule_id],
                "records_before": str(current_count),
                "records_matched": str(matched_count),
                "records_after": str(next_count),
                "notes": (
                    "Evaluated after every preceding curation rule."
                    if action == "EXCLUDE"
                    else "Flag does not remove records."
                ),
            }
        )
        current_count = next_count
    return rows


def current_filter_metadata(
    classified: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Build filter metadata for the current curation configuration."""
    semantic_reasons = Counter(
        row["exclusion_reason"]
        for row in classified
        if row["classification"] == "FALSE_POSITIVE"
    )
    curation_reasons = Counter(
        row["curation_exclusion_reason"]
        for row in classified
        if row["curation_exclusion_reason"]
    )
    rule_order = [
        "REMOVED_BODY",
        "DELETED_BODY",
        "BLANK_BODY",
        "LINK_ONLY_BODY",
        "AGGRESSION_QUERY_ONLY",
        "NON_ENGLISH",
        "LANGUAGE_UNCERTAIN",
    ]
    return build_filter_metadata(
        source_count=len(classified),
        false_positive_count=sum(
            row["classification"] == "FALSE_POSITIVE"
            for row in classified
        ),
        semantic_uncertain_count=sum(
            row["classification"] == "UNCERTAIN" for row in classified
        ),
        semantic_reason_counts=semantic_reasons,
        curation_rule_counts=[
            (rule_id, curation_reasons[rule_id]) for rule_id in rule_order
        ],
    )


def is_removed_body(body: str) -> bool:
    """Return whether a body contains only Reddit's removal placeholder."""
    return (body or "").strip().casefold() == "[removed]"


def is_blank_body(body: str) -> bool:
    """Return whether a body contains no non-whitespace text."""
    return not (body or "").strip()


def is_link_only_body(body: str) -> bool:
    """Return whether a body is one standalone plain or Markdown link."""
    normalized = (body or "").strip()
    return bool(
        PLAIN_LINK_ONLY_PATTERN.fullmatch(normalized)
        or MARKDOWN_LINK_ONLY_PATTERN.fullmatch(normalized)
    )


def is_deleted_body(body: str) -> bool:
    """Return whether a body has only Reddit's deletion placeholder/link."""
    normalized = (body or "").strip()
    marker = "[deleted]"
    if normalized.casefold() == marker:
        return True
    if not normalized.casefold().startswith(marker):
        return False
    remainder = normalized[len(marker) :].strip()
    return bool(remainder) and is_link_only_body(remainder)


def matching_query_terms(value: str) -> set[str]:
    """Parse the semicolon-delimited query-membership field."""
    return {
        item.strip().casefold()
        for item in (value or "").split(";")
        if item.strip()
    }


def is_withheld_query_only(value: str) -> bool:
    """Return whether all result membership comes from withheld queries."""
    memberships = matching_query_terms(value)
    return bool(memberships) and memberships <= WITHHELD_QUERY_TERMS


def read_language_decisions(
    source_rows: list[dict[str, str]],
    semantic_decisions: dict[int, dict[str, str]],
    shard_paths: Optional[list[Path]] = None,
) -> dict[int, dict[str, str]]:
    """Validate and load exception-only language-review decisions."""
    language_decisions: dict[int, dict[str, str]] = {}
    selected_shards = LANGUAGE_SHARDS if shard_paths is None else shard_paths
    for shard in selected_shards:
        if not shard.exists():
            raise FileNotFoundError(f"Missing language review shard: {shard}")
        for record in read_delimited(shard):
            index = int(record["row_index"])
            if index < 0 or index >= len(source_rows):
                raise ValueError(
                    f"{shard}: row_index {index} is outside source bounds"
                )
            if index in language_decisions:
                raise ValueError(
                    f"Duplicate language decision for row_index {index}"
                )
            if (
                semantic_decisions[index]["classification"]
                not in RESULT_CLASSIFICATIONS
            ):
                raise ValueError(
                    f"{shard}: row {index} is already a semantic false "
                    "positive"
                )
            if is_removed_body(source_rows[index]["body"]) or is_deleted_body(
                source_rows[index]["body"]
            ):
                raise ValueError(
                    f"{shard}: row {index} has a placeholder-only body "
                    "and does not require language review"
                )

            provided_link = (record.get("post_link") or "").strip()
            source_link = source_rows[index]["post_link"]
            if provided_link != source_link:
                raise ValueError(
                    f"{shard}: row {index} link mismatch: "
                    f"{provided_link!r} != {source_link!r}"
                )

            classification = (
                record["language_classification"].strip().upper()
            )
            if classification not in VALID_LANGUAGE_CLASSIFICATIONS:
                raise ValueError(
                    f"{shard}: row {index} has invalid language "
                    f"classification {classification!r}"
                )
            detected_language = (
                record.get("detected_language") or ""
            ).strip()
            if not detected_language:
                raise ValueError(
                    f"{shard}: row {index} has no detected language"
                )
            rationale = (
                record.get("classification_rationale") or ""
            ).strip()
            if not rationale:
                raise ValueError(
                    f"{shard}: row {index} has no language rationale"
                )

            language_decisions[index] = {
                "language_classification": classification,
                "detected_language": detected_language,
                "classification_rationale": rationale,
            }
    return language_decisions


def curation_decision(
    source_row: dict[str, str],
    semantic_classification: str,
    language_decision: Optional[dict[str, str]],
) -> dict[str, str]:
    """Build the secondary curation decision for one classified row."""
    if semantic_classification not in RESULT_CLASSIFICATIONS:
        return {
            "curation_status": "NOT_APPLICABLE",
            "curation_exclusion_reason": "",
            "curation_language": "",
            "curation_rationale": "",
        }
    if is_removed_body(source_row["body"]):
        return {
            "curation_status": "EXCLUDE",
            "curation_exclusion_reason": "REMOVED_BODY",
            "curation_language": "",
            "curation_rationale": (
                "Body text is exactly [removed] after trimming whitespace "
                "and ignoring case."
            ),
        }
    if is_deleted_body(source_row["body"]):
        return {
            "curation_status": "EXCLUDE",
            "curation_exclusion_reason": "DELETED_BODY",
            "curation_language": "",
            "curation_rationale": (
                "Body contains only [deleted], optionally followed by one "
                "standalone link, and has no narrative text."
            ),
        }
    if is_blank_body(source_row["body"]):
        return {
            "curation_status": "EXCLUDE",
            "curation_exclusion_reason": "BLANK_BODY",
            "curation_language": "",
            "curation_rationale": (
                "Body text is empty after trimming whitespace."
            ),
        }
    if is_link_only_body(source_row["body"]):
        return {
            "curation_status": "EXCLUDE",
            "curation_exclusion_reason": "LINK_ONLY_BODY",
            "curation_language": "",
            "curation_rationale": (
                "Body contains only one standalone link and no narrative "
                "text."
            ),
        }
    if is_withheld_query_only(source_row.get("matching_queries", "")):
        return {
            "curation_status": "EXCLUDE",
            "curation_exclusion_reason": "AGGRESSION_QUERY_ONLY",
            "curation_language": "",
            "curation_rationale": (
                "Every result-membership query is in the configured "
                "aggression-only withholding set."
            ),
        }
    if language_decision is None:
        return {
            "curation_status": "RETAIN",
            "curation_exclusion_reason": "",
            "curation_language": "English",
            "curation_rationale": "Submission is primarily in English.",
        }
    if language_decision["language_classification"] == "NON_ENGLISH":
        return {
            "curation_status": "EXCLUDE",
            "curation_exclusion_reason": "NON_ENGLISH",
            "curation_language": language_decision["detected_language"],
            "curation_rationale": language_decision[
                "classification_rationale"
            ],
        }
    return {
        "curation_status": "REVIEW",
        "curation_exclusion_reason": "LANGUAGE_UNCERTAIN",
        "curation_language": language_decision["detected_language"],
        "curation_rationale": language_decision[
            "classification_rationale"
        ],
    }


def main() -> int:
    source_hash_before = sha256(SOURCE)
    with SOURCE.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        source_fields = list(reader.fieldnames or [])
        source_rows = list(reader)

    if len(source_rows) != 8812:
        raise ValueError(f"Expected 8,812 source rows, found {len(source_rows):,}")
    if len({row["post_link"] for row in source_rows}) != len(source_rows):
        raise ValueError("Source post_link values are not unique")

    decisions: dict[int, dict[str, str]] = {}
    for shard in SHARDS:
        if not shard.exists():
            raise FileNotFoundError(f"Missing semantic review shard: {shard}")
        for record in read_shard(shard):
            index = int(record["row_index"])
            if shard == INITIAL_SHARD and index in CORRECTED_INDICES:
                continue
            if index < 0 or index >= len(source_rows):
                raise ValueError(f"{shard}: row_index {index} is outside source bounds")
            if index in decisions:
                raise ValueError(f"Duplicate semantic decision for row_index {index}")

            label = record["classification"].strip().upper()
            if label not in VALID_LABELS:
                raise ValueError(f"{shard}: row {index} has invalid label {label!r}")

            provided_link = (record.get("post_link") or "").strip()
            source_link = source_rows[index]["post_link"]
            if provided_link and provided_link != source_link:
                raise ValueError(
                    f"{shard}: row {index} link mismatch: {provided_link!r} != {source_link!r}"
                )

            rationale = (record.get("classification_rationale") or "").strip()
            if not rationale:
                raise ValueError(f"{shard}: row {index} has no classification rationale")

            decisions[index] = {
                "classification": label,
                "exclusion_reason": canonical_reason(
                    label, (record.get("exclusion_reason") or "").strip()
                ),
                "classification_rationale": rationale,
            }

    expected_indices = set(range(len(source_rows)))
    actual_indices = set(decisions)
    if actual_indices != expected_indices:
        missing = sorted(expected_indices - actual_indices)
        extras = sorted(actual_indices - expected_indices)
        raise ValueError(f"Coverage failure: missing={missing[:20]}, extras={extras[:20]}")

    language_decisions = read_language_decisions(
        source_rows,
        decisions,
    )

    audit_fields = source_fields + [
        "review_row_index",
        "classification",
        "exclusion_reason",
        "classification_rationale",
    ] + CURATION_FIELDS
    classified: list[dict[str, str]] = []
    for index, source_row in enumerate(source_rows):
        classified.append(
            {
                **source_row,
                "review_row_index": str(index),
                **decisions[index],
                **curation_decision(
                    source_row,
                    decisions[index]["classification"],
                    language_decisions.get(index),
                ),
            }
        )

    false_positives = [
        row for row in classified if row["classification"] == "FALSE_POSITIVE"
    ]
    uncertain = [row for row in classified if row["classification"] == "UNCERTAIN"]
    curation_exclusions = [
        row for row in classified if row["curation_status"] == "EXCLUDE"
    ]
    language_uncertain = [
        row
        for row in classified
        if row["curation_exclusion_reason"] == "LANGUAGE_UNCERTAIN"
    ]
    included_with_uncertain = [
        row
        for row in classified
        if row["classification"] in RESULT_CLASSIFICATIONS
        and row["curation_status"] != "EXCLUDE"
    ]

    timestamped_output = timestamped_dataset_path()
    output_dir = timestamped_output.parent
    if output_dir.exists():
        raise FileExistsError(
            f"Refusing to overwrite timestamped bundle: {output_dir}"
        )
    output_dir.mkdir(parents=True, exist_ok=False)

    write_csv(output_dir / "dedup_posts_classified.csv", audit_fields, classified)
    write_csv(
        output_dir / "dedup_posts_false_positives.csv",
        audit_fields,
        false_positives,
    )
    write_csv(output_dir / "dedup_posts_uncertain.csv", audit_fields, uncertain)
    write_csv(
        output_dir / "dedup_posts_curation_exclusions.csv",
        audit_fields,
        curation_exclusions,
    )
    write_csv(
        output_dir / "dedup_posts_language_uncertain.csv",
        audit_fields,
        language_uncertain,
    )
    write_csv(timestamped_output, audit_fields, included_with_uncertain)
    filter_metadata_output = timestamped_output.with_name(
        "filter_metadata.csv"
    )
    write_csv(
        filter_metadata_output,
        FILTER_METADATA_FIELDS,
        current_filter_metadata(classified),
    )

    source_hash_after = sha256(SOURCE)
    if source_hash_after != source_hash_before:
        raise RuntimeError("Source CSV changed during merge")

    labels = Counter(row["classification"] for row in classified)
    reasons = Counter(
        row["exclusion_reason"]
        for row in classified
        if row["exclusion_reason"]
    )
    curation_reasons = Counter(
        row["curation_exclusion_reason"]
        for row in classified
        if row["curation_exclusion_reason"]
    )
    print(f"source_sha256={source_hash_after}")
    print(f"source_rows={len(source_rows)}")
    print(f"unique_source_links={len({row['post_link'] for row in source_rows})}")
    for label in ("INCLUDE", "FALSE_POSITIVE", "UNCERTAIN"):
        print(f"{label}={labels[label]}")
    print(
        "INITIAL_INCLUDE_PLUS_UNCERTAIN="
        f"{labels['INCLUDE'] + labels['UNCERTAIN']}"
    )
    print(
        "FINAL_INCLUDE_PLUS_UNCERTAIN="
        f"{len(included_with_uncertain)}"
    )
    print(f"TIMESTAMPED_OUTPUT={timestamped_output}")
    print(f"FILTER_METADATA_OUTPUT={filter_metadata_output}")
    for reason, count in sorted(reasons.items()):
        print(f"{reason}={count}")
    for reason, count in sorted(curation_reasons.items()):
        print(f"CURATION_{reason}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
