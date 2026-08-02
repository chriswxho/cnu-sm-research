#!/usr/bin/env python3
"""Merge independently reviewed semantic shards into auditable CSV outputs.

This script does not classify posts. It only validates and merges the row-level
decisions produced by LLM subagents after reading each post's full title/body.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent
SOURCE = DATA_DIR / "dedup_posts.csv"
SHARD_DIR = DATA_DIR / "semantic_review_shards"
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
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


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

    audit_fields = source_fields + [
        "review_row_index",
        "classification",
        "exclusion_reason",
        "classification_rationale",
    ]
    classified: list[dict[str, str]] = []
    for index, source_row in enumerate(source_rows):
        classified.append(
            {
                **source_row,
                "review_row_index": str(index),
                **decisions[index],
            }
        )

    false_positives = [
        row for row in classified if row["classification"] == "FALSE_POSITIVE"
    ]
    uncertain = [row for row in classified if row["classification"] == "UNCERTAIN"]
    included_with_uncertain = [
        row
        for row in classified
        if row["classification"] in {"INCLUDE", "UNCERTAIN"}
    ]

    write_csv(DATA_DIR / "dedup_posts_classified.csv", audit_fields, classified)
    write_csv(
        DATA_DIR / "dedup_posts_false_positives.csv", audit_fields, false_positives
    )
    write_csv(DATA_DIR / "dedup_posts_uncertain.csv", audit_fields, uncertain)
    write_csv(
        DATA_DIR / "dedup_posts_included_with_uncertain.csv",
        audit_fields,
        included_with_uncertain,
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
    print(f"source_sha256={source_hash_after}")
    print(f"source_rows={len(source_rows)}")
    print(f"unique_source_links={len({row['post_link'] for row in source_rows})}")
    for label in ("INCLUDE", "FALSE_POSITIVE", "UNCERTAIN"):
        print(f"{label}={labels[label]}")
    print(f"INCLUDE_PLUS_UNCERTAIN={len(included_with_uncertain)}")
    for reason, count in sorted(reasons.items()):
        print(f"{reason}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
