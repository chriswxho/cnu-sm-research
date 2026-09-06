#!/usr/bin/env python3
"""Count top subreddit sources for each codebook category, retaining ties."""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CODEBOOK = (
    DATA_DIR / "2026-08-30_18-18-07" / "agent_codebook.csv"
)
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


def paired_paths(posts_path: Path) -> tuple[Path, Path]:
    """Return annotation and analysis paths in the post bundle."""
    if (
        posts_path.name != "dedup_posts.csv"
        or STAMP_PATTERN.fullmatch(posts_path.parent.name) is None
    ):
        raise ValueError(
            "Expected data/YYYY-MM-DD_HH-MM-SS/dedup_posts.csv, "
            f"found {posts_path}"
        )
    return (
        posts_path.with_name("agent_codebook_annotations.csv"),
        posts_path.with_name("agent_codebook_top_subreddits.csv"),
    )


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


def read_paired_rows(
    posts_path: Path,
    annotations_path: Path,
    terms: list[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Read and validate post and annotation rows in identical ID order."""
    with posts_path.open(newline="", encoding="utf-8-sig") as handle:
        post_reader = csv.DictReader(handle)
        if not post_reader.fieldnames or not {
            "post_link",
            "subreddit",
        } <= set(post_reader.fieldnames):
            raise ValueError(f"{posts_path} requires post_link and subreddit")
        post_rows = list(post_reader)
    with annotations_path.open(newline="", encoding="utf-8-sig") as handle:
        annotation_reader = csv.DictReader(handle)
        if annotation_reader.fieldnames != ["post_id", *terms]:
            raise ValueError(
                f"{annotations_path} headers do not match the codebook"
            )
        annotation_rows = list(annotation_reader)

    post_ids = [post_id_from_link(row["post_link"]) for row in post_rows]
    annotation_ids = [row["post_id"] for row in annotation_rows]
    if len(post_ids) != len(set(post_ids)):
        raise ValueError(f"Post IDs must be unique in {posts_path}")
    if post_ids != annotation_ids:
        raise ValueError("Post and annotation IDs differ or are out of order")
    invalid = sorted(
        {
            row[term]
            for row in annotation_rows
            for term in terms
            if row[term] not in {"0", "1"}
        }
    )
    if invalid:
        raise ValueError(
            f"{annotations_path} contains non-binary labels: {invalid}"
        )
    return post_rows, annotation_rows


def summarize_top_subreddits(
    post_rows: list[dict[str, str]],
    annotation_rows: list[dict[str, str]],
    terms: list[str],
) -> list[dict[str, str | int]]:
    """Return top three subreddit counts per term plus third-place ties."""
    results: list[dict[str, str | int]] = []
    for term in terms:
        counts: Counter[str] = Counter()
        display_names: dict[str, str] = {}
        for post, annotation in zip(post_rows, annotation_rows):
            if annotation[term] != "1":
                continue
            subreddit = (post["subreddit"] or "").strip()
            if not subreddit:
                subreddit = "(missing)"
            key = subreddit.casefold()
            display_names.setdefault(key, subreddit)
            counts[key] += 1

        ranked = sorted(
            counts.items(),
            key=lambda item: (-item[1], display_names[item[0]].casefold()),
        )
        if len(ranked) < 3:
            raise ValueError(
                f"{term!r} has only {len(ranked)} positive-source subreddits"
            )
        cutoff = ranked[2][1]
        selected = [item for item in ranked if item[1] >= cutoff]
        previous_count: int | None = None
        rank = 0
        for position, (key, count) in enumerate(selected, start=1):
            if count != previous_count:
                rank = position
                previous_count = count
            results.append(
                {
                    "codebook_category": term,
                    "rank": rank,
                    "subreddit": display_names[key],
                    "post_count": count,
                }
            )
    return results


def write_summary(
    posts_path: Path,
    *,
    codebook_path: Path = CODEBOOK,
) -> tuple[Path, int]:
    """Create the immutable top-subreddit summary for a stamped pair."""
    annotations_path, output_path = paired_paths(posts_path)
    if output_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite timestamped analysis: {output_path}"
        )
    terms = read_codebook(codebook_path)
    post_rows, annotation_rows = read_paired_rows(
        posts_path,
        annotations_path,
        terms,
    )
    results = summarize_top_subreddits(
        post_rows,
        annotation_rows,
        terms,
    )
    fieldnames = ["codebook_category", "rank", "subreddit", "post_count"]
    with output_path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(results)
    return output_path, len(results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "posts_csv",
        nargs="?",
        type=Path,
        help="Timestamped dedup_posts CSV; defaults to the newest one.",
    )
    args = parser.parse_args()
    posts_path = args.posts_csv or newest_post_dataset()
    output_path, row_count = write_summary(posts_path)
    print(f"posts={posts_path}")
    print(f"output={output_path}")
    print(f"rows={row_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
