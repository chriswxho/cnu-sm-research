#!/usr/bin/env python3
"""Recover historical classification bundles from surviving provenance."""

from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
import io
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo

import merge_semantic_reviews as merge_reviews


DATA_DIR = Path(__file__).resolve().parent
PACIFIC_TIME = ZoneInfo("America/Los_Angeles")
SEMANTIC_BUNDLE = DATA_DIR / "2026-07-28_21-20-11"
CURATED_BUNDLE = DATA_DIR / "2026-08-30_18-18-07"
CURRENT_BUNDLE = DATA_DIR / "2026-09-05_21-06-01"
SEMANTIC_GIT_OBJECT = (
    "740b994:data/dedup_posts_included_with_uncertain.csv"
)
POST_ID_PATTERN = re.compile(r"/comments/([^/]+)/", flags=re.IGNORECASE)


def creation_stamp(path: Path) -> str:
    """Return a Pacific timestamp from macOS file-creation metadata."""
    created = getattr(path.stat(), "st_birthtime", None)
    if created is None:
        raise RuntimeError(f"No file-creation timestamp is available for {path}")
    return datetime.fromtimestamp(created, PACIFIC_TIME).strftime(
        "%Y-%m-%d_%H-%M-%S"
    )


def post_id(post_link: str) -> str:
    match = POST_ID_PATTERN.search(post_link or "")
    if match is None:
        raise ValueError(f"Cannot extract post ID from {post_link!r}")
    return match.group(1).casefold()


def read_dict_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_dict_rows(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> None:
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def semantic_counts(
    classified: list[dict[str, str]],
) -> tuple[int, int, Counter[str]]:
    false_positive_count = sum(
        row["classification"] == "FALSE_POSITIVE" for row in classified
    )
    uncertain_count = sum(
        row["classification"] == "UNCERTAIN" for row in classified
    )
    reasons = Counter(
        row["exclusion_reason"]
        for row in classified
        if row["classification"] == "FALSE_POSITIVE"
    )
    return false_positive_count, uncertain_count, reasons


def recover_curated_rows(
    classified: list[dict[str, str]],
    annotation_ids: list[str],
) -> tuple[list[dict[str, str]], Counter[str]]:
    """Reapply the pre-DELETED_BODY rules and restore retained audit fields."""
    remaining = [
        row
        for row in classified
        if row["classification"] in merge_reviews.RESULT_CLASSIFICATIONS
    ]
    predicates = [
        ("REMOVED_BODY", lambda row: merge_reviews.is_removed_body(row["body"])),
        ("BLANK_BODY", lambda row: merge_reviews.is_blank_body(row["body"])),
        ("LINK_ONLY_BODY", lambda row: merge_reviews.is_link_only_body(row["body"])),
        (
            "AGGRESSION_QUERY_ONLY",
            lambda row: merge_reviews.is_withheld_query_only(
                row.get("matching_queries", "")
            ),
        ),
        (
            "NON_ENGLISH",
            lambda row: row["curation_exclusion_reason"] == "NON_ENGLISH",
        ),
    ]
    counts: Counter[str] = Counter()
    for rule_id, predicate in predicates:
        retained = []
        for row in remaining:
            if predicate(row):
                counts[rule_id] += 1
            else:
                retained.append(row)
        remaining = retained

    recovered_ids = [post_id(row["post_link"]) for row in remaining]
    if recovered_ids != annotation_ids:
        raise ValueError(
            "Recovered 4,855-post membership/order does not match annotations"
        )

    recovered = []
    for row in remaining:
        restored = dict(row)
        if restored["curation_exclusion_reason"] == "DELETED_BODY":
            restored.update(
                {
                    "curation_status": "RETAIN",
                    "curation_exclusion_reason": "",
                    "curation_language": "English",
                    "curation_rationale": (
                        "Submission is primarily in English."
                    ),
                }
            )
        if restored["curation_status"] == "EXCLUDE":
            raise ValueError("Recovered historical row remains excluded")
        recovered.append(restored)
    counts["LANGUAGE_UNCERTAIN"] = sum(
        row["curation_exclusion_reason"] == "LANGUAGE_UNCERTAIN"
        for row in recovered
    )
    return recovered, counts


def main() -> int:
    classified_fields, classified = read_dict_rows(
        CURRENT_BUNDLE / "dedup_posts_classified.csv"
    )
    annotation_bytes = (
        CURATED_BUNDLE / "agent_codebook_annotations.csv"
    ).read_bytes()
    with io.StringIO(annotation_bytes.decode("utf-8-sig")) as handle:
        annotation_rows = list(csv.DictReader(handle))
    annotation_ids = [row["post_id"] for row in annotation_rows]
    if len(annotation_ids) != 4855 or len(annotation_ids) != len(
        set(annotation_ids)
    ):
        raise ValueError("Expected 4,855 unique historical annotation IDs")

    semantic_bytes = subprocess.check_output(
        ["git", "show", SEMANTIC_GIT_OBJECT],
        cwd=DATA_DIR.parent,
    )
    semantic_csv = list(
        csv.reader(io.StringIO(semantic_bytes.decode("utf-8-sig")))
    )
    if len(semantic_csv) - 1 != 5812:
        raise ValueError("Git history does not contain the 5,812-post set")

    recovered_curated, historical_counts = recover_curated_rows(
        classified,
        annotation_ids,
    )
    if len(recovered_curated) != 4855:
        raise ValueError("Historical curated reconstruction is not 4,855 rows")

    false_positive_count, uncertain_count, semantic_reasons = semantic_counts(
        classified
    )
    semantic_metadata = merge_reviews.build_filter_metadata(
        source_count=len(classified),
        false_positive_count=false_positive_count,
        semantic_uncertain_count=uncertain_count,
        semantic_reason_counts=semantic_reasons,
        curation_rule_counts=[],
    )
    historical_rule_order = [
        "REMOVED_BODY",
        "BLANK_BODY",
        "LINK_ONLY_BODY",
        "AGGRESSION_QUERY_ONLY",
        "NON_ENGLISH",
        "LANGUAGE_UNCERTAIN",
    ]
    curated_metadata = merge_reviews.build_filter_metadata(
        source_count=len(classified),
        false_positive_count=false_positive_count,
        semantic_uncertain_count=uncertain_count,
        semantic_reason_counts=semantic_reasons,
        curation_rule_counts=[
            (rule_id, historical_counts[rule_id])
            for rule_id in historical_rule_order
        ],
    )

    semantic_dir = SEMANTIC_BUNDLE
    curated_dir = CURATED_BUNDLE
    current_dir = CURRENT_BUNDLE

    output_paths = [
        semantic_dir / "dedup_posts.csv",
        semantic_dir / "filter_metadata.csv",
        curated_dir / "dedup_posts.csv",
        curated_dir / "agent_codebook_annotations.csv",
        curated_dir / "filter_metadata.csv",
        current_dir / "filter_metadata.csv",
    ]
    existing = [path for path in output_paths if path.exists()]
    if existing:
        raise FileExistsError(f"Refusing to overwrite outputs: {existing}")

    semantic_dir.mkdir(parents=False, exist_ok=False)
    curated_dir.mkdir(parents=False, exist_ok=False)
    with (semantic_dir / "dedup_posts.csv").open("xb") as handle:
        handle.write(semantic_bytes)
    write_dict_rows(
        semantic_dir / "filter_metadata.csv",
        merge_reviews.FILTER_METADATA_FIELDS,
        semantic_metadata,
    )
    write_dict_rows(
        curated_dir / "dedup_posts.csv",
        classified_fields,
        recovered_curated,
    )
    with (curated_dir / "agent_codebook_annotations.csv").open("xb") as handle:
        handle.write(annotation_bytes)
    write_dict_rows(
        curated_dir / "filter_metadata.csv",
        merge_reviews.FILTER_METADATA_FIELDS,
        curated_metadata,
    )
    write_dict_rows(
        current_dir / "filter_metadata.csv",
        merge_reviews.FILTER_METADATA_FIELDS,
        merge_reviews.current_filter_metadata(classified),
    )

    print(f"semantic_bundle={semantic_dir}")
    print(f"curated_bundle={curated_dir}")
    print(f"current_bundle={current_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
