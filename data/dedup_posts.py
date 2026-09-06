#!/usr/bin/env python3
"""Flatten and deduplicate saved PullPush post results for review.

By default, the script scans query subdirectories under ``data/raw_data`` and
writes ``dedup_posts.csv`` there. Only
``pullpush-filtered-posts.json`` files are read, so Reddit API results and raw
PullPush candidates that failed the exact-match post-filter are excluded.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo


PACIFIC_TIMEZONE = ZoneInfo("America/Los_Angeles")
REDDIT_BASE_URL = "https://www.reddit.com"
PULLPUSH_FILTERED_FILENAME = "pullpush-filtered-posts.json"
DEFAULT_OUTPUT_FILENAME = "dedup_posts.csv"
DEFAULT_RAW_DATA_ROOT = Path(__file__).resolve().parent / "raw_data"
CSV_COLUMNS = [
    "subreddit",
    "datetime_pst",
    "matching_queries",
    "post_link",
    "score",
    "title",
    "body",
    "media_url",
]


@dataclass(frozen=True)
class BuildStats:
    query_directories: int
    source_rows: int
    unique_posts: int
    duplicate_occurrences: int
    multi_query_posts: int


def _post_id(post: dict[str, object]) -> str:
    """Return Reddit's canonical base36 submission ID."""
    raw_id = str(post.get("id") or post.get("name") or "")
    post_id = raw_id.removeprefix("t3_").strip().casefold()
    if not post_id:
        raise ValueError("PullPush post is missing both `id` and `name`")
    return post_id


def _created_epoch(post: dict[str, object]) -> float:
    value = post.get("created_utc", post.get("created"))
    if value is None:
        raise ValueError(
            f"PullPush post {_post_id(post)!r} is missing creation time"
        )
    return float(value)


def _absolute_reddit_url(value: object) -> str:
    url = str(value or "")
    if url.startswith("/"):
        return f"{REDDIT_BASE_URL}{url}"
    return url


def _post_link(post: dict[str, object]) -> str:
    permalink = post.get("permalink") or post.get("full_link")
    if permalink:
        return _absolute_reddit_url(permalink)
    return f"{REDDIT_BASE_URL}/comments/{_post_id(post)}/"


def _media_url(post: dict[str, object]) -> str:
    media_url = post.get("url_overridden_by_dest")
    if not media_url and not bool(post.get("is_self", False)):
        media_url = post.get("url")
    return _absolute_reddit_url(media_url)


def _query_text(query_directory: Path) -> str:
    manifest_path = query_directory / "manifest.json"
    if manifest_path.exists():
        with manifest_path.open(encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
        query_text = str(manifest.get("query_text") or "").strip()
        if query_text:
            return query_text

    comparison_path = query_directory / "comparison.json"
    if comparison_path.exists():
        with comparison_path.open(encoding="utf-8") as comparison_file:
            comparison = json.load(comparison_file)
        query_text = str(
            comparison.get("criteria", {}).get("phrase") or ""
        ).strip()
        if query_text:
            return query_text

    return query_directory.name.replace("-", " ")


def _load_filtered_posts(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as source_file:
        posts = json.load(source_file)
    if not isinstance(posts, list):
        raise TypeError(f"Expected a JSON list in {path}")
    if not all(isinstance(post, dict) for post in posts):
        raise TypeError(f"Expected every item in {path} to be a JSON object")
    return posts


def _record_quality(post: dict[str, object]) -> tuple[int, int, float]:
    """Prefer the most reviewable snapshot when an ID has multiple copies."""
    body = str(post.get("selftext") or "")
    body_length = (
        0 if body.casefold() in {"[deleted]", "[removed]"} else len(body)
    )
    populated_fields = sum(
        bool(post.get(field))
        for field in (
            "subreddit",
            "title",
            "permalink",
            "created_utc",
            "score",
            "url_overridden_by_dest",
            "url",
        )
    )
    metadata = post.get("_meta")
    metadata = metadata if isinstance(metadata, dict) else {}
    retrieved = max(
        (
            float(value)
            for value in (
                metadata.get("retrieved_2nd_on"),
                post.get("retrieved_on"),
            )
            if value is not None
        ),
        default=0.0,
    )
    return body_length, populated_fields, retrieved


def _review_row(
    post: dict[str, object],
    matching_queries: Iterable[str],
) -> dict[str, object]:
    created_epoch = _created_epoch(post)
    created_pacific = datetime.fromtimestamp(
        created_epoch,
        tz=timezone.utc,
    ).astimezone(PACIFIC_TIMEZONE)
    return {
        "subreddit": post.get("subreddit", ""),
        "datetime_pst": created_pacific.strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        ),
        "matching_queries": "; ".join(
            sorted(set(matching_queries), key=str.casefold)
        ),
        "post_link": _post_link(post),
        "score": post.get("score", ""),
        "title": post.get("title", ""),
        "body": post.get("selftext", ""),
        "media_url": _media_url(post),
        "_created_epoch": created_epoch,
        "_post_id": _post_id(post),
    }


def build_deduplicated_rows(
    data_root: Path,
    *,
    exclude_queries: Iterable[str] = (),
) -> tuple[list[dict[str, object]], BuildStats]:
    """Build review rows from all PullPush filtered-result subdirectories."""
    excluded = {
        query.strip().casefold()
        for query in exclude_queries
        if query.strip()
    }
    posts_by_id: dict[str, dict[str, object]] = {}
    queries_by_id: dict[str, set[str]] = {}
    source_rows = 0
    query_directories = 0

    for query_directory in sorted(data_root.iterdir()):
        if not query_directory.is_dir():
            continue
        source_path = query_directory / PULLPUSH_FILTERED_FILENAME
        if not source_path.exists():
            continue

        query_text = _query_text(query_directory)
        if query_text.casefold() in excluded:
            continue

        query_directories += 1
        for post in _load_filtered_posts(source_path):
            source_rows += 1
            post_id = _post_id(post)
            queries_by_id.setdefault(post_id, set()).add(query_text)
            current = posts_by_id.get(post_id)
            if current is None or _record_quality(post) > _record_quality(
                current
            ):
                posts_by_id[post_id] = post

    rows = [
        _review_row(post, queries_by_id[post_id])
        for post_id, post in posts_by_id.items()
    ]
    rows.sort(
        key=lambda row: (
            -float(row["_created_epoch"]),
            str(row["_post_id"]),
        )
    )
    stats = BuildStats(
        query_directories=query_directories,
        source_rows=source_rows,
        unique_posts=len(rows),
        duplicate_occurrences=source_rows - len(rows),
        multi_query_posts=sum(
            len(matching_queries) > 1
            for matching_queries in queries_by_id.values()
        ),
    )
    return rows, stats


def filter_rows_by_pacific_date(
    rows: Iterable[dict[str, object]],
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list[dict[str, object]]:
    """Keep rows within inclusive Pacific calendar-date boundaries."""
    if start_date is not None and end_date is not None:
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")

    start_epoch = (
        datetime.combine(
            start_date,
            datetime.min.time(),
            tzinfo=PACIFIC_TIMEZONE,
        ).timestamp()
        if start_date is not None
        else float("-inf")
    )
    end_epoch_exclusive = (
        datetime.combine(
            end_date + timedelta(days=1),
            datetime.min.time(),
            tzinfo=PACIFIC_TIMEZONE,
        ).timestamp()
        if end_date is not None
        else float("inf")
    )
    return [
        row
        for row in rows
        if start_epoch
        <= float(row["_created_epoch"])
        < end_epoch_exclusive
    ]


def write_review_csv(
    rows: Iterable[dict[str, object]],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=CSV_COLUMNS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected a date in YYYY-MM-DD format, got {value!r}"
        ) from exc


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Deduplicate all saved PullPush exact-match post datasets into "
            "one review CSV."
        )
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_RAW_DATA_ROOT,
        help="Directory containing one subdirectory per query.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Output CSV path. Defaults to <data-root>/"
            f"{DEFAULT_OUTPUT_FILENAME}."
        ),
    )
    parser.add_argument(
        "--exclude-query",
        action="append",
        default=[],
        help="Exact query label to exclude; may be provided more than once.",
    )
    parser.add_argument(
        "--start-date",
        type=_iso_date,
        default=None,
        help="Earliest Pacific creation date to include, in YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end-date",
        type=_iso_date,
        default=None,
        help="Latest Pacific creation date to include, in YYYY-MM-DD.",
    )
    return parser


def main() -> None:
    args = _argument_parser().parse_args()
    data_root = args.data_root.resolve()
    output_path = (
        args.output.resolve()
        if args.output is not None
        else data_root / DEFAULT_OUTPUT_FILENAME
    )
    all_rows, stats = build_deduplicated_rows(
        data_root,
        exclude_queries=args.exclude_query,
    )
    try:
        rows = filter_rows_by_pacific_date(
            all_rows,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    write_review_csv(rows, output_path)
    retained_query_occurrences = sum(
        len(str(row["matching_queries"]).split("; "))
        for row in rows
    )
    retained_multi_query_posts = sum(
        len(str(row["matching_queries"]).split("; ")) > 1
        for row in rows
    )
    print(
        f"Wrote {len(rows)} unique PullPush posts from "
        f"{retained_query_occurrences} included query-post rows across "
        f"{stats.query_directories} query directories to {output_path}"
    )
    print(
        f"Collapsed {retained_query_occurrences - len(rows)} duplicate "
        f"occurrences; {retained_multi_query_posts} posts matched multiple "
        "queries"
    )
    if args.start_date is not None or args.end_date is not None:
        print(
            f"Withheld {len(all_rows) - len(rows)} posts outside Pacific "
            f"date range {args.start_date or '-infinity'} through "
            f"{args.end_date or 'infinity'}, inclusive"
        )


if __name__ == "__main__":
    main()
