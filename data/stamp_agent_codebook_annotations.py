#!/usr/bin/env python3
"""Create a timestamp-matched annotation matrix for a post dataset."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CODEBOOK_BUNDLE = DATA_DIR / "2026-08-30_18-18-07"
CODEBOOK = CODEBOOK_BUNDLE / "agent_codebook.csv"
ANNOTATIONS = CODEBOOK_BUNDLE / "agent_codebook_annotations.csv"
STAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$"
)
POST_ID_PATTERN = re.compile(r"/comments/([^/]+)/", flags=re.IGNORECASE)


def post_id_from_link(post_link: str) -> str:
    """Extract Reddit's canonical submission ID from a permalink."""
    match = POST_ID_PATTERN.search(post_link or "")
    if match is None:
        raise ValueError(f"Cannot extract post ID from {post_link!r}")
    return match.group(1).casefold()


def read_codebook(path: Path = CODEBOOK) -> list[str]:
    """Read the headerless, one-column codebook."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    if not rows or any(len(row) != 1 or not row[0].strip() for row in rows):
        raise ValueError(f"Expected one nonblank term per row in {path}")
    terms = [row[0] for row in rows]
    if len(terms) != len(set(terms)):
        raise ValueError(f"Codebook terms must be unique in {path}")
    return terms


def annotation_path_for_posts(posts_path: Path) -> Path:
    """Derive the annotation path within a timestamped bundle."""
    if (
        posts_path.name != "dedup_posts.csv"
        or STAMP_PATTERN.fullmatch(posts_path.parent.name) is None
    ):
        raise ValueError(
            "Expected data/YYYY-MM-DD_HH-MM-SS/dedup_posts.csv, "
            f"found {posts_path}"
        )
    return posts_path.with_name("agent_codebook_annotations.csv")


def newest_post_dataset(data_dir: Path = DATA_DIR) -> Path:
    """Return the post dataset in the newest Pacific-time bundle."""
    candidates = [
        directory / "dedup_posts.csv"
        for directory in data_dir.iterdir()
        if directory.is_dir()
        and STAMP_PATTERN.fullmatch(directory.name)
        and (directory / "dedup_posts.csv").is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No timestamped dataset bundle found in {data_dir}"
        )
    return max(candidates, key=lambda path: path.parent.name)


def read_post_ids(path: Path) -> list[str]:
    """Read unique canonical post IDs in dataset order."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "post_link" not in reader.fieldnames:
            raise ValueError(f"{path} has no post_link column")
        post_ids = [post_id_from_link(row["post_link"]) for row in reader]
    if len(post_ids) != len(set(post_ids)):
        raise ValueError(f"Post IDs must be unique in {path}")
    return post_ids


def read_annotations(
    path: Path,
    terms: list[str],
) -> dict[str, dict[str, str]]:
    """Read and validate a binary annotation matrix keyed by post ID."""
    expected_fields = ["post_id", *terms]
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != expected_fields:
            raise ValueError(
                f"{path} headers do not match post_id plus the codebook terms"
            )
        rows = list(reader)
    post_ids = [row["post_id"] for row in rows]
    if len(post_ids) != len(set(post_ids)):
        raise ValueError(f"Post IDs must be unique in {path}")
    invalid = sorted(
        {
            row[term]
            for row in rows
            for term in terms
            if row[term] not in {"0", "1"}
        }
    )
    if invalid:
        raise ValueError(f"{path} contains non-binary labels: {invalid}")
    return {row["post_id"]: row for row in rows}


def write_paired_annotations(
    posts_path: Path,
    *,
    annotations_path: Path = ANNOTATIONS,
    codebook_path: Path = CODEBOOK,
    output_path: Optional[Path] = None,
) -> tuple[Path, int]:
    """Write annotations selected and ordered to match a stamped dataset."""
    expected_output = annotation_path_for_posts(posts_path)
    selected_output = output_path or expected_output
    if selected_output != expected_output:
        raise ValueError(
            f"Annotations must share the post bundle: {expected_output}"
        )
    if selected_output.exists():
        raise FileExistsError(
            f"Refusing to overwrite timestamped annotations: {selected_output}"
        )

    terms = read_codebook(codebook_path)
    post_ids = read_post_ids(posts_path)
    annotations = read_annotations(annotations_path, terms)
    missing = [post_id for post_id in post_ids if post_id not in annotations]
    if missing:
        raise ValueError(
            f"Annotations are missing {len(missing)} dataset post IDs; "
            f"first missing ID: {missing[0]}"
        )

    fieldnames = ["post_id", *terms]
    selected_output.parent.mkdir(parents=True, exist_ok=True)
    with selected_output.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(annotations[post_id] for post_id in post_ids)
    return selected_output, len(post_ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "posts_csv",
        nargs="?",
        type=Path,
        help="Timestamped dedup_posts CSV; defaults to the newest one.",
    )
    parser.add_argument(
        "--annotations",
        type=Path,
        default=ANNOTATIONS,
        help="Existing superset annotation matrix.",
    )
    args = parser.parse_args()

    posts_path = args.posts_csv or newest_post_dataset()
    output_path, count = write_paired_annotations(
        posts_path,
        annotations_path=args.annotations,
    )
    print(f"posts={posts_path}")
    print(f"annotations={output_path}")
    print(f"rows={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
