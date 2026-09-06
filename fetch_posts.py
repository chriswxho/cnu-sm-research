"""Fetch and compare Reddit/PullPush exact-phrase post-search coverage.

Each query is stored under ``data/raw_data/<hyphenated-query>/``. The Reddit side
exhausts every supported /r/all/search sort with ``t=all``. The PullPush side
exhausts independent title and selftext searches across resumable time
windows. Both sources are deduplicated by submission ID and passed through
the same local exact-phrase filter before their coverage is compared.
"""

import argparse
import html
import json
import random
import re
import shlex
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from api.pullpush import PullPushRequestManager
from api.reddit import RedditRequestManager, SortBy
from csv_dumper import (
    RedditResultType,
    contains_exact_phrase_ignoring_quotes,
    extract_raw_data,
    extract_research_data,
)

PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_START = datetime(2005, 1, 1, tzinfo=PACIFIC)
# PullPush's submission archive currently stops during 2025-Q2. Use the
# beginning of Q3 as an exclusive upper bound so new runs preserve every
# result returned within Q2 without issuing predictably empty later-quarter
# requests.
PULLPUSH_ARCHIVE_CUTOFF_EXCLUSIVE = datetime(
    2025,
    7,
    1,
    tzinfo=PACIFIC,
)
PULLPUSH_ROOT_WINDOW_YEARS = 5
PULLPUSH_REQUEST_TIMEOUT_SEC = 120.0
SORTS = tuple(SortBy)
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data" / "raw_data"


def slugify_query_text(query_text: str) -> str:
    """Convert query text to a safe lowercase hyphen-delimited directory."""
    slug = re.sub(r"[^a-z0-9]+", "-", query_text.strip().lower()).strip("-")
    if not slug:
        raise ValueError(
            "query_text must contain at least one ASCII letter or digit"
        )
    return slug


def _quoted_query_text(query_text: str) -> str:
    escaped = query_text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _coerce_pacific_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=PACIFIC)
    return value.astimezone(PACIFIC)


def _effective_pullpush_cutoff(requested_cutoff: datetime) -> datetime:
    return min(
        _coerce_pacific_datetime(requested_cutoff),
        PULLPUSH_ARCHIVE_CUTOFF_EXCLUSIVE,
    )


def parse_datetime_argument(value: str) -> datetime:
    """Parse an ISO-8601 datetime, treating a missing offset as Pacific."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected an ISO-8601 datetime, got {value!r}"
        ) from exc
    return _coerce_pacific_datetime(parsed)


@dataclass(frozen=True)
class DatasetPaths:
    data_dir: Path

    @classmethod
    def for_query(
        cls,
        query_text: str,
        data_root: Path = DEFAULT_DATA_ROOT,
    ) -> "DatasetPaths":
        return cls(data_root / slugify_query_text(query_text))

    def reddit_sort(self, sort: SortBy) -> Path:
        return self.data_dir / f"reddit-api-posts-{sort.value}.json"

    @property
    def reddit_candidates(self) -> Path:
        return self.data_dir / "reddit-union-candidates.json"

    @property
    def reddit_filtered(self) -> Path:
        return self.data_dir / "reddit-filtered-posts.json"

    @property
    def reddit_csv(self) -> Path:
        return self.data_dir / "reddit-posts.csv"

    @property
    def pullpush_title_windows(self) -> Path:
        return self.data_dir / "pullpush-api-title-windows.json"

    @property
    def pullpush_selftext_windows(self) -> Path:
        return self.data_dir / "pullpush-api-selftext-windows.json"

    @property
    def pullpush_candidates(self) -> Path:
        return self.data_dir / "pullpush-union-candidates.json"

    @property
    def pullpush_filtered(self) -> Path:
        return self.data_dir / "pullpush-filtered-posts.json"

    @property
    def pullpush_csv(self) -> Path:
        return self.data_dir / "pullpush-posts.csv"

    @property
    def comparison(self) -> Path:
        return self.data_dir / "comparison.json"

    @property
    def manifest(self) -> Path:
        return self.data_dir / "manifest.json"

    @property
    def readme(self) -> Path:
        return self.data_dir / "README.md"


def _post_id(post: dict[str, object]) -> str:
    return str(post["id"]).removeprefix("t3_")


def _created_utc(post: dict[str, object]) -> float:
    return float(post.get("created_utc", post.get("created", 0)))


def _is_exact(post: dict[str, object], query_text: str) -> bool:
    return (
        contains_exact_phrase_ignoring_quotes(
            post.get("title", ""),
            query_text,
        )
        or contains_exact_phrase_ignoring_quotes(
            post.get("selftext", ""),
            query_text,
        )
    )


def _is_quote_rescued(
    post: dict[str, object],
    query_text: str,
) -> bool:
    if not _is_exact(post, query_text):
        return False

    def matches_without_quote_removal(value: object) -> bool:
        normalized = html.unescape(str(value))
        return re.search(
            rf"(?<!\w){re.escape(query_text)}(?!\w)",
            normalized,
            flags=re.IGNORECASE,
        ) is not None

    return not (
        matches_without_quote_removal(post.get("title", ""))
        or matches_without_quote_removal(post.get("selftext", ""))
    )


def _union_posts(
    post_groups: Iterable[Iterable[dict[str, object]]],
) -> list[dict[str, object]]:
    by_id: dict[str, dict[str, object]] = {}
    for group in post_groups:
        for post in group:
            by_id.setdefault(_post_id(post), post)
    return list(by_id.values())


def _five_year_epoch_windows(
    start: datetime,
    cutoff: datetime,
) -> list[tuple[int, int]]:
    start = _coerce_pacific_datetime(start)
    cutoff = _coerce_pacific_datetime(cutoff)
    if start >= cutoff:
        raise ValueError("start must be earlier than cutoff")

    windows = []
    window_start = start
    while window_start < cutoff:
        next_year = window_start.year + PULLPUSH_ROOT_WINDOW_YEARS
        try:
            next_root = window_start.replace(year=next_year)
        except ValueError:
            # Preserve a February start when the target year is not leap.
            next_root = window_start.replace(
                year=next_year,
                month=2,
                day=28,
            )
        window_end = min(next_root, cutoff)
        windows.append(
            (int(window_start.timestamp()), int(window_end.timestamp()))
        )
        window_start = window_end
    return windows


def _uncovered_intervals(
    audit_windows: list[dict[str, object]],
    window_start: int,
    window_end: int,
) -> list[tuple[int, int]]:
    leaf_intervals = sorted(
        (
            int(window["start_utc_inclusive"]),
            int(window["end_utc_exclusive"]),
        )
        for window in audit_windows
        if window["leaf"]
    )
    uncovered = []
    covered_until = window_start
    for leaf_start, leaf_end in leaf_intervals:
        if leaf_end <= covered_until:
            continue
        if leaf_start >= window_end:
            break
        if leaf_start > covered_until:
            uncovered.append(
                (covered_until, min(leaf_start, window_end))
            )
        covered_until = max(covered_until, leaf_end)
        if covered_until >= window_end:
            return uncovered
    if covered_until < window_end:
        uncovered.append((covered_until, window_end))
    return uncovered


def _window_is_covered(
    audit_windows: list[dict[str, object]],
    window_start: int,
    window_end: int,
) -> bool:
    return not _uncovered_intervals(
        audit_windows,
        window_start,
        window_end,
    )


def _submissions_from_audit_windows(
    audit_windows: list[dict[str, object]],
    *,
    title: Optional[str] = None,
    selftext: Optional[str] = None,
) -> list[dict[str, object]]:
    by_id: dict[str, dict[str, object]] = {}
    for window in audit_windows:
        if not window["leaf"]:
            continue
        for record in window["response"]["data"]:
            normalized = PullPushRequestManager._normalize_submission(
                record,
                query_term=None,
                title=title,
                selftext=selftext,
            )
            by_id.setdefault(_post_id(normalized), normalized)
    return list(by_id.values())


def _run_adaptive_field_search(
    manager: Optional[PullPushRequestManager],
    *,
    field: str,
    output_path: Path,
    pullpush_query: str,
    start: datetime,
    cutoff: datetime,
    root_attempts: int,
    retry_delay_sec: float,
    retry_jitter_sec: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if root_attempts < 1:
        raise ValueError("root_attempts must be positive")
    if retry_delay_sec < 0 or retry_jitter_sec < 0:
        raise ValueError("retry delay and jitter must be non-negative")

    if output_path.exists():
        audit_windows = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(audit_windows, list):
            raise TypeError(
                f"Expected {output_path} to contain a JSON list"
            )
    else:
        audit_windows = []

    query_kwargs = {field: pullpush_query}
    root_windows = _five_year_epoch_windows(start, cutoff)
    for root_start, root_end in root_windows:
        if not _uncovered_intervals(audit_windows, root_start, root_end):
            print(
                f"[fetch-posts] reusing covered {field} interval "
                f"[{root_start}, {root_end})"
            )
            continue

        failed_attempts = 0
        while True:
            uncovered_intervals = _uncovered_intervals(
                audit_windows,
                root_start,
                root_end,
            )
            if not uncovered_intervals:
                break
            window_start, window_end = uncovered_intervals[0]
            print(
                f"[fetch-posts] {field} uncovered interval "
                f"[{window_start}, {window_end})"
            )
            if manager is None:
                raise RuntimeError(
                    "Offline rebuild cannot fill missing "
                    f"{field} interval [{window_start}, {window_end}) in "
                    f"{output_path}"
                )

            checkpointed_windows: list[dict[str, object]] = []

            def checkpoint_partial(
                in_progress_windows: list[dict[str, object]],
            ) -> None:
                checkpointed_windows[:] = in_progress_windows
                extract_raw_data(
                    [*audit_windows, *checkpointed_windows],
                    str(output_path),
                )

            try:
                new_windows, _ = manager.search_submissions_adaptive(
                    **query_kwargs,
                    start_utc=window_start,
                    end_utc=window_end,
                    checkpoint_callback=checkpoint_partial,
                )
            except requests.RequestException as exc:
                audit_windows.extend(checkpointed_windows)
                failed_attempts += 1
                if failed_attempts >= root_attempts:
                    raise
                jitter = (
                    (2 * random.random() - 1) * retry_jitter_sec
                )
                wait_seconds = max(0.0, retry_delay_sec + jitter)
                remaining_intervals = _uncovered_intervals(
                    audit_windows,
                    root_start,
                    root_end,
                )
                print(
                    f"[fetch-posts] {field} root "
                    f"[{root_start}, {root_end}) failed on "
                    f"attempt {failed_attempts}/{root_attempts}: {exc}; "
                    f"{len(remaining_intervals)} uncovered interval(s) "
                    f"remain after checkpointing; retrying in "
                    f"{wait_seconds:.1f}s"
                )
                wait_until = time.monotonic() + wait_seconds
                while True:
                    remaining = wait_until - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(remaining, 60.0))
                continue

            audit_windows.extend(new_windows)
            # The callback already checkpoints every response. Write once more
            # so a zero-response edge case also has a durable checkpoint.
            extract_raw_data(audit_windows, str(output_path))

    submissions = _submissions_from_audit_windows(
        audit_windows,
        title=pullpush_query if field == "title" else None,
        selftext=pullpush_query if field == "selftext" else None,
    )
    return audit_windows, submissions


def _date_summary(posts: list[dict[str, object]]) -> dict[str, object]:
    if not posts:
        return {
            "earliest_epoch": None,
            "earliest_pacific": None,
            "latest_epoch": None,
            "latest_pacific": None,
        }
    earliest = min(_created_utc(post) for post in posts)
    latest = max(_created_utc(post) for post in posts)
    return {
        "earliest_epoch": int(earliest),
        "earliest_pacific": datetime.fromtimestamp(
            earliest,
            tz=PACIFIC,
        ).isoformat(),
        "latest_epoch": int(latest),
        "latest_pacific": datetime.fromtimestamp(
            latest,
            tz=PACIFIC,
        ).isoformat(),
    }


def _existing_dataset_config(
    paths: DatasetPaths,
) -> Optional[dict[str, str]]:
    if paths.manifest.exists():
        payload = json.loads(paths.manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(
                f"Expected {paths.manifest} to contain a JSON object"
            )
        return {
            "query_text": str(payload["query_text"]),
            "start_pacific": str(payload["start_pacific"]),
            "cutoff_pacific_exclusive": str(
                payload["cutoff_pacific_exclusive"]
            ),
        }

    if paths.comparison.exists():
        payload = json.loads(paths.comparison.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(
            payload.get("criteria"),
            dict,
        ):
            raise TypeError(
                f"Expected {paths.comparison} to contain comparison criteria"
            )
        criteria = payload["criteria"]
        return {
            "query_text": str(criteria["phrase"]),
            "start_pacific": str(criteria["pullpush_start_pacific"]),
            "cutoff_pacific_exclusive": str(
                criteria["pullpush_cutoff_pacific_exclusive"]
            ),
        }
    return None


def _resolve_run_bounds(
    *,
    query_text: str,
    paths: DatasetPaths,
    start: Optional[datetime],
    cutoff: Optional[datetime],
    offline: bool,
) -> tuple[datetime, datetime]:
    existing = _existing_dataset_config(paths)
    if existing is not None and existing["query_text"] != query_text:
        raise ValueError(
            f"Query {query_text!r} collides with existing dataset query "
            f"{existing['query_text']!r} at {paths.data_dir}"
        )

    saved_start = (
        datetime.fromisoformat(existing["start_pacific"])
        if existing is not None
        else None
    )
    saved_cutoff = (
        datetime.fromisoformat(existing["cutoff_pacific_exclusive"])
        if existing is not None
        else None
    )

    if start is None:
        start = saved_start or DEFAULT_START
    else:
        start = _coerce_pacific_datetime(start)
        if saved_start is not None and start != saved_start:
            raise ValueError(
                f"Existing dataset starts at {saved_start.isoformat()}; "
                f"cannot resume it from {start.isoformat()}"
            )

    if cutoff is None:
        if offline:
            if saved_cutoff is None:
                raise ValueError(
                    "Offline rebuild requires --cutoff when no saved "
                    "manifest or comparison exists"
                )
            cutoff = saved_cutoff
        else:
            cutoff = datetime.now(PACIFIC)
    else:
        cutoff = _coerce_pacific_datetime(cutoff)

    start = _coerce_pacific_datetime(start)
    cutoff = _coerce_pacific_datetime(cutoff)
    if start >= cutoff:
        raise ValueError("start must be earlier than cutoff")
    return start, cutoff


def _write_dataset_metadata(
    *,
    paths: DatasetPaths,
    query_text: str,
    reddit_query: str,
    pullpush_query: str,
    start: datetime,
    cutoff: datetime,
    pullpush_cutoff: datetime,
) -> None:
    slug = slugify_query_text(query_text)
    manifest = {
        "schema_version": 1,
        "query_text": query_text,
        "query_slug": slug,
        "reddit_query": reddit_query,
        "pullpush_title_query": pullpush_query,
        "pullpush_selftext_query": pullpush_query,
        "start_pacific": start.isoformat(),
        "cutoff_pacific_exclusive": cutoff.isoformat(),
        "pullpush_archive_cutoff_pacific_exclusive": (
            PULLPUSH_ARCHIVE_CUTOFF_EXCLUSIVE.isoformat()
        ),
        "pullpush_effective_cutoff_pacific_exclusive": (
            pullpush_cutoff.isoformat()
        ),
        "pullpush_root_window_years": PULLPUSH_ROOT_WINDOW_YEARS,
        "pullpush_request_timeout_sec": PULLPUSH_REQUEST_TIMEOUT_SEC,
    }
    paths.manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    restore_command = (
        "cnu-sm-research/bin/python fetch_posts.py "
        f"{shlex.quote(query_text)} --offline"
    )
    paths.readme.write_text(
        "\n".join([
            f"# Post-search dataset: {query_text}",
            "",
            "Canonical Reddit and PullPush post-search inputs and derived "
            "outputs.",
            "",
            "## Query",
            "",
            f"- Exact phrase: `{query_text}`",
            f"- Reddit: `{reddit_query}`",
            "- PullPush: independent title and selftext exact-phrase "
            "searches",
            f"- PullPush root windows: {PULLPUSH_ROOT_WINDOW_YEARS} years; "
            "100-result roots are recursively bisected",
            f"- PullPush request timeout: "
            f"{PULLPUSH_REQUEST_TIMEOUT_SEC:g} seconds",
            f"- Start: `{start.isoformat()}`",
            f"- Requested cutoff (exclusive): `{cutoff.isoformat()}`",
            "- PullPush searches stop at the end of 2025-Q2; "
            f"effective cutoff: `{pullpush_cutoff.isoformat()}`",
            "",
            "## Source inputs",
            "",
            "- `reddit-api-posts-{relevance,hot,top,new,comments}.json`",
            "- `pullpush-api-title-windows.json`",
            "- `pullpush-api-selftext-windows.json`",
            "",
            "The Reddit files contain complete post records. The PullPush "
            "files retain response envelopes and resumable time-window "
            "metadata.",
            "",
            "## Derived outputs",
            "",
            "- `reddit-union-candidates.json`",
            "- `reddit-filtered-posts.json`",
            "- `reddit-posts.csv`",
            "- `pullpush-union-candidates.json`",
            "- `pullpush-filtered-posts.json`",
            "- `pullpush-posts.csv`",
            "- `comparison.json`",
            "",
            "Both CSVs use:",
            "",
            "```text",
            "type,subreddit,title,url,datetime_pst,score,body,media_url",
            "```",
            "",
            "## Offline rebuild",
            "",
            "```bash",
            restore_command,
            "```",
            "",
            "Offline mode makes no API requests and fails if a source file "
            "or PullPush interval is missing.",
            "",
        ]),
        encoding="utf-8",
    )


def main(
    query_text: str,
    *,
    start: Optional[datetime] = None,
    cutoff: Optional[datetime] = None,
    data_root: Path = DEFAULT_DATA_ROOT,
    reuse_reddit: bool = False,
    root_attempts: int = 3,
    retry_delay_sec: float = 300.0,
    retry_jitter_sec: float = 1.0,
    request_spacing_mean_sec: float = 60.0,
    request_spacing_stddev_sec: float = 0.0,
    offline: bool = False,
) -> None:
    query_text = query_text.strip()
    if not query_text:
        raise ValueError("query_text must not be blank")
    paths = DatasetPaths.for_query(query_text, data_root)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    start, cutoff = _resolve_run_bounds(
        query_text=query_text,
        paths=paths,
        start=start,
        cutoff=cutoff,
        offline=offline,
    )
    pullpush_cutoff = _effective_pullpush_cutoff(cutoff)
    if start >= pullpush_cutoff:
        raise ValueError(
            "PullPush search start must be earlier than its archive cutoff "
            f"({PULLPUSH_ARCHIVE_CUTOFF_EXCLUSIVE.isoformat()})"
        )
    quoted_query = _quoted_query_text(query_text)
    reddit_query = (
        f"title:{quoted_query} OR selftext:{quoted_query}"
    )
    pullpush_query = quoted_query
    _write_dataset_metadata(
        paths=paths,
        query_text=query_text,
        reddit_query=reddit_query,
        pullpush_query=pullpush_query,
        start=start,
        cutoff=cutoff,
        pullpush_cutoff=pullpush_cutoff,
    )

    if offline:
        reuse_reddit = True

    reddit_by_sort: dict[str, list[dict[str, object]]] = {}
    if reuse_reddit:
        for sort in SORTS:
            input_path = paths.reddit_sort(sort)
            posts = json.loads(input_path.read_text(encoding="utf-8"))
            if not isinstance(posts, list):
                raise TypeError(
                    f"Expected {input_path} to contain a JSON list"
                )
            reddit_by_sort[sort.value] = posts
            print(
                f"[fetch-posts] reused {len(posts)} Reddit "
                f"{sort.value} posts"
            )
    else:
        reddit_manager = RedditRequestManager(
            window_time_sec=600,
            max_requests_in_window=100,
        )
        for sort in SORTS:
            posts = reddit_manager.search_posts(
                subreddit_name="all",
                query_term=reddit_query,
                num_results=None,
                sort_by=sort,
            )
            reddit_by_sort[sort.value] = posts
            extract_raw_data(
                posts,
                str(paths.reddit_sort(sort)),
            )

    reddit_candidates = _union_posts(reddit_by_sort.values())
    reddit_exact = sorted(
        (
            post
            for post in reddit_candidates
            if _is_exact(post, query_text)
        ),
        key=_created_utc,
        reverse=True,
    )
    extract_raw_data(
        reddit_candidates,
        str(paths.reddit_candidates),
    )
    extract_raw_data(
        reddit_exact,
        str(paths.reddit_filtered),
    )
    extract_research_data(
        reddit_exact,
        RedditResultType.POST,
        str(paths.reddit_csv),
    )

    pullpush_start_epoch = int(start.timestamp())
    cutoff_epoch = int(pullpush_cutoff.timestamp())
    # Root-level retry scheduling defines the total API attempts. Disable the
    # request manager's nested retry loop so each scheduled attempt is one call.
    pullpush_manager = None
    if not offline:
        pullpush_manager = PullPushRequestManager(
            max_retries=0,
            timeout_sec=PULLPUSH_REQUEST_TIMEOUT_SEC,
            request_spacing_mean_sec=request_spacing_mean_sec,
            request_spacing_stddev_sec=request_spacing_stddev_sec,
        )
    title_windows, pullpush_title = _run_adaptive_field_search(
        pullpush_manager,
        field="title",
        output_path=paths.pullpush_title_windows,
        pullpush_query=pullpush_query,
        start=start,
        cutoff=pullpush_cutoff,
        root_attempts=root_attempts,
        retry_delay_sec=retry_delay_sec,
        retry_jitter_sec=retry_jitter_sec,
    )
    selftext_windows, pullpush_selftext = _run_adaptive_field_search(
        pullpush_manager,
        field="selftext",
        output_path=paths.pullpush_selftext_windows,
        pullpush_query=pullpush_query,
        start=start,
        cutoff=pullpush_cutoff,
        root_attempts=root_attempts,
        retry_delay_sec=retry_delay_sec,
        retry_jitter_sec=retry_jitter_sec,
    )

    pullpush_candidates = _union_posts(
        (pullpush_title, pullpush_selftext)
    )
    pullpush_in_bounds = [
        post
        for post in pullpush_candidates
        if pullpush_start_epoch <= _created_utc(post) < cutoff_epoch
    ]
    pullpush_exact = sorted(
        (
            post
            for post in pullpush_in_bounds
            if _is_exact(post, query_text)
        ),
        key=_created_utc,
        reverse=True,
    )
    extract_raw_data(
        pullpush_candidates,
        str(paths.pullpush_candidates),
    )
    extract_raw_data(
        pullpush_exact,
        str(paths.pullpush_filtered),
    )
    extract_research_data(
        pullpush_exact,
        RedditResultType.POST,
        str(paths.pullpush_csv),
    )

    reddit_ids = {_post_id(post) for post in reddit_exact}
    pullpush_ids = {_post_id(post) for post in pullpush_exact}
    comparison = {
        "criteria": {
            "result_type": "post",
            "phrase": query_text,
            "query_slug": slugify_query_text(query_text),
            "data_directory": str(
                paths.data_dir.relative_to(PROJECT_ROOT)
                if paths.data_dir.is_relative_to(PROJECT_ROOT)
                else paths.data_dir
            ),
            "match_rule": (
                "Case-insensitive whole-word phrase after removing "
                "ASCII/full-width/curly quote characters only; all other "
                "punctuation and whitespace preserved."
            ),
            "reddit_query": reddit_query,
            "reddit_time_filter": "all",
            "reddit_local_date_filter": None,
            "pullpush_title_query": pullpush_query,
            "pullpush_selftext_query": pullpush_query,
            "pullpush_start_epoch": pullpush_start_epoch,
            "pullpush_start_pacific": start.isoformat(),
            "requested_cutoff_epoch_exclusive": int(cutoff.timestamp()),
            "requested_cutoff_pacific_exclusive": cutoff.isoformat(),
            "pullpush_archive_cutoff_epoch_exclusive": int(
                PULLPUSH_ARCHIVE_CUTOFF_EXCLUSIVE.timestamp()
            ),
            "pullpush_archive_cutoff_pacific_exclusive": (
                PULLPUSH_ARCHIVE_CUTOFF_EXCLUSIVE.isoformat()
            ),
            "pullpush_cutoff_epoch_exclusive": cutoff_epoch,
            "pullpush_cutoff_pacific_exclusive": (
                pullpush_cutoff.isoformat()
            ),
            "pullpush_root_window_years": PULLPUSH_ROOT_WINDOW_YEARS,
            "pullpush_request_timeout_sec": (
                PULLPUSH_REQUEST_TIMEOUT_SEC
            ),
        },
        "reddit": {
            "sorts_exhausted": {
                sort: len(posts)
                for sort, posts in reddit_by_sort.items()
            },
            "union_api_candidates": len(reddit_candidates),
            "post_filter_rejections": (
                len(reddit_candidates) - len(reddit_exact)
            ),
            "quote_rescued_posts": sum(
                _is_quote_rescued(post, query_text)
                for post in reddit_exact
            ),
            "yielded_posts": len(reddit_exact),
            "unique_subreddits": len(
                {str(post["subreddit"]) for post in reddit_exact}
            ),
            **_date_summary(reddit_exact),
        },
        "pullpush": {
            "max_results_per_window": 100,
            "title_total_requests": len(title_windows),
            "title_leaf_windows": sum(
                bool(window["leaf"]) for window in title_windows
            ),
            "title_saturated_windows": sum(
                bool(window["saturated"]) for window in title_windows
            ),
            "title_leaf_rows": sum(
                int(window["result_count"])
                for window in title_windows
                if window["leaf"]
            ),
            "title_unique_ids": len(pullpush_title),
            "selftext_total_requests": len(selftext_windows),
            "selftext_leaf_windows": sum(
                bool(window["leaf"]) for window in selftext_windows
            ),
            "selftext_saturated_windows": sum(
                bool(window["saturated"]) for window in selftext_windows
            ),
            "selftext_leaf_rows": sum(
                int(window["result_count"])
                for window in selftext_windows
                if window["leaf"]
            ),
            "selftext_unique_ids": len(pullpush_selftext),
            "union_candidates": len(pullpush_candidates),
            "candidates_in_bounds": len(pullpush_in_bounds),
            "post_filter_rejections": (
                len(pullpush_in_bounds) - len(pullpush_exact)
            ),
            "quote_rescued_posts": sum(
                _is_quote_rescued(post, query_text)
                for post in pullpush_exact
            ),
            "yielded_posts": len(pullpush_exact),
            "unique_subreddits": len(
                {str(post["subreddit"]) for post in pullpush_exact}
            ),
            **_date_summary(pullpush_exact),
        },
        "overlap": {
            "shared_post_ids": len(reddit_ids & pullpush_ids),
            "reddit_only_post_ids": len(reddit_ids - pullpush_ids),
            "pullpush_only_post_ids": len(pullpush_ids - reddit_ids),
            "union_post_ids": len(reddit_ids | pullpush_ids),
            "reddit_coverage_of_pullpush_percent": round(
                100 * len(reddit_ids & pullpush_ids) / len(pullpush_ids),
                2,
            ) if pullpush_ids else None,
            "pullpush_coverage_of_reddit_percent": round(
                100 * len(reddit_ids & pullpush_ids) / len(reddit_ids),
                2,
            ) if reddit_ids else None,
        },
    }
    paths.comparison.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(comparison, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Fetch exact-phrase posts from Reddit and PullPush into "
            "data/raw_data/<hyphenated-query>/."
        )
    )
    parser.add_argument(
        "query_text",
        help="Exact phrase to match in post titles or body text.",
    )
    parser.add_argument(
        "--start",
        type=parse_datetime_argument,
        help=(
            "PullPush range start as ISO-8601. Defaults to the saved start "
            "or 2005-01-01 Pacific."
        ),
    )
    parser.add_argument(
        "--cutoff",
        type=parse_datetime_argument,
        help=(
            "Exclusive PullPush cutoff as ISO-8601. Defaults to the saved "
            "cutoff offline or the current Pacific time online."
        ),
    )
    parser.add_argument(
        "--reuse-reddit",
        action="store_true",
        help="Reuse already serialized per-sort Reddit results.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Rebuild only from organized saved inputs; fail if a Reddit "
            "file or PullPush checkpoint interval is missing."
        ),
    )
    parser.add_argument(
        "--root-attempts",
        type=int,
        default=3,
        help="Maximum total API attempts for each failed root window.",
    )
    parser.add_argument(
        "--retry-delay-sec",
        type=float,
        default=300.0,
        help="Base delay between root-window attempts.",
    )
    parser.add_argument(
        "--retry-jitter-sec",
        type=float,
        default=1.0,
        help="Symmetric random jitter added to the root retry delay.",
    )
    parser.add_argument(
        "--request-spacing-mean-sec",
        type=float,
        default=60.0,
        help="Mean delay between successive PullPush request starts.",
    )
    parser.add_argument(
        "--request-spacing-stddev-sec",
        type=float,
        default=0.0,
        help=(
            "Standard deviation of normally distributed request-spacing "
            "jitter."
        ),
    )
    args = parser.parse_args()
    main(
        args.query_text,
        start=args.start,
        cutoff=args.cutoff,
        reuse_reddit=args.reuse_reddit,
        root_attempts=args.root_attempts,
        retry_delay_sec=args.retry_delay_sec,
        retry_jitter_sec=args.retry_jitter_sec,
        request_spacing_mean_sec=args.request_spacing_mean_sec,
        request_spacing_stddev_sec=args.request_spacing_stddev_sec,
        offline=args.offline,
    )
