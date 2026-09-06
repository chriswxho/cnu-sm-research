#!/usr/bin/env python3
"""Validate per-term agent reviews and merge them into one binary matrix."""

from __future__ import annotations

import csv
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BUNDLE_DIR = DATA_DIR / "2026-08-30_18-18-07"
SOURCE = BUNDLE_DIR / "dedup_posts.csv"
CODEBOOK = BUNDLE_DIR / "agent_codebook.csv"
SHARD_DIR = BUNDLE_DIR / "agent_codebook_shards"
OUTPUT = BUNDLE_DIR / "agent_codebook_annotations.csv"
POST_ID_PATTERN = re.compile(r"/comments/([^/]+)/", flags=re.IGNORECASE)


def post_id_from_link(post_link: str) -> str:
    """Extract Reddit's canonical submission ID from a permalink."""
    match = POST_ID_PATTERN.search(post_link or "")
    if match is None:
        raise ValueError(f"Cannot extract post ID from {post_link!r}")
    return match.group(1).casefold()


def read_codebook(path: Path = CODEBOOK) -> list[str]:
    """Read a headerless, one-column codebook and validate unique terms."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    if not rows or any(len(row) != 1 or not row[0].strip() for row in rows):
        raise ValueError(f"Expected one nonblank term per row in {path}")
    terms = [row[0] for row in rows]
    if len(terms) != len(set(terms)):
        raise ValueError(f"Codebook terms must be unique in {path}")
    return terms


def read_source_ids(path: Path = SOURCE) -> list[str]:
    """Read source posts in output order and validate unique IDs."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    post_ids = [post_id_from_link(row["post_link"]) for row in rows]
    if len(post_ids) != len(set(post_ids)):
        raise ValueError(f"Source post IDs must be unique in {path}")
    return post_ids


def read_term_shard(path: Path, expected_ids: list[str]) -> list[str]:
    """Read one source-ordered `post_id,label` shard."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["post_id", "label"]:
            raise ValueError(
                f"{path} must have exactly the header post_id,label"
            )
        rows = list(reader)
    actual_ids = [row["post_id"] for row in rows]
    if actual_ids != expected_ids:
        mismatch = next(
            (
                index
                for index, (actual, expected) in enumerate(
                    zip(actual_ids, expected_ids)
                )
                if actual != expected
            ),
            min(len(actual_ids), len(expected_ids)),
        )
        raise ValueError(
            f"{path} post IDs differ from source order at row {mismatch}; "
            f"expected {len(expected_ids)} rows, found {len(actual_ids)}"
        )
    labels = [row["label"] for row in rows]
    invalid = sorted(set(labels) - {"0", "1"})
    if invalid:
        raise ValueError(f"{path} contains non-binary labels: {invalid}")
    return labels


def merge_agent_codebook(
    *,
    source_path: Path = SOURCE,
    codebook_path: Path = CODEBOOK,
    shard_dir: Path = SHARD_DIR,
    output_path: Path = OUTPUT,
) -> dict[str, int]:
    """Validate all term shards and write the combined binary matrix."""
    terms = read_codebook(codebook_path)
    post_ids = read_source_ids(source_path)
    labels_by_term: dict[str, list[str]] = {}
    positive_counts: dict[str, int] = {}
    for index, term in enumerate(terms):
        shard_path = shard_dir / f"term_{index:02d}.csv"
        if not shard_path.exists():
            raise FileNotFoundError(f"Missing agent review shard: {shard_path}")
        labels = read_term_shard(shard_path, post_ids)
        labels_by_term[term] = labels
        positive_counts[term] = labels.count("1")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["post_id", *terms]
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        for row_index, post_id in enumerate(post_ids):
            writer.writerow(
                {
                    "post_id": post_id,
                    **{
                        term: labels_by_term[term][row_index]
                        for term in terms
                    },
                }
            )
    return positive_counts


def main() -> int:
    positive_counts = merge_agent_codebook()
    print(f"output={OUTPUT}")
    print(f"posts={len(read_source_ids())}")
    print(f"terms={len(positive_counts)}")
    for term, count in positive_counts.items():
        print(f"{term}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
