from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
import random
import sqlite3
import time
from typing import Callable, Optional, Protocol, Sequence, Union

import requests

from .reddit import USER_AGENT


PULLPUSH_COMMENT_SEARCH_URL = "https://api.pullpush.io/reddit/search/comment/"
PULLPUSH_SUBMISSION_SEARCH_URL = (
    "https://api.pullpush.io/reddit/search/submission/"
)
PULLPUSH_LOG_PREFIX = "[PullPushRequestManager]"
PULLPUSH_MAX_PAGE_SIZE = 100
PULLPUSH_RATE_LIMITS = ((60.0, 30), (3600.0, 1000))
PULLPUSH_RATE_LIMIT_STATE_PATH = (
    Path(__file__).resolve().parent.parent
    / ".pullpush-rate-limit.sqlite3"
)

TimeFilter = Union[int, float, str]


class PullPushSort(str, Enum):
    ASC = "asc"
    DESC = "desc"


class PullPushSortType(str, Enum):
    CREATED_UTC = "created_utc"
    SCORE = "score"
    NUM_COMMENTS = "num_comments"


class PullPushAPIError(requests.RequestException):
    """Raised when PullPush returns an invalid or error response."""


class PullPushQueryTooComplexError(PullPushAPIError):
    """Raised when PullPush requires a narrower timestamp window."""

    def __init__(
        self,
        message: str,
        *,
        raw_payload: object,
    ) -> None:
        super().__init__(message)
        self.raw_payload = raw_payload


class PullPushPaginationError(PullPushAPIError):
    """Raised when PullPush's timestamp pagination cannot safely advance."""


class _RateLimiter(Protocol):
    def acquire(self) -> None:
        ...


class _GaussianRequestPacer:
    """Space request starts using a Gaussian-distributed target interval."""

    def __init__(
        self,
        mean_seconds: float,
        stddev_seconds: float,
        clock: Optional[Callable[[], float]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        standard_normal_sampler: Optional[Callable[[], float]] = None,
    ) -> None:
        if mean_seconds < 0 or stddev_seconds < 0:
            raise ValueError(
                "Request-spacing mean and standard deviation "
                "must be non-negative"
            )
        self._mean_seconds = mean_seconds
        self._stddev_seconds = stddev_seconds
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._standard_normal_sampler = (
            standard_normal_sampler
            or (lambda: random.gauss(0.0, 1.0))
        )
        self._last_request_started_at: Optional[float] = None

    def wait(self) -> None:
        if self._last_request_started_at is None:
            return
        interval_seconds = max(
            0.0,
            self._mean_seconds
            + self._stddev_seconds * self._standard_normal_sampler(),
        )
        wait_until = self._last_request_started_at + interval_seconds
        while True:
            remaining = wait_until - self._clock()
            if remaining <= 0:
                return
            self._sleeper(min(remaining, 60.0))

    def mark_request_started(self) -> None:
        self._last_request_started_at = self._clock()


class _SlidingWindowRateLimiter:
    def __init__(
        self,
        limits: Sequence[tuple[float, int]],
        clock: Optional[Callable[[], float]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        if not limits:
            raise ValueError("At least one rate limit is required")
        for window_seconds, max_requests in limits:
            if window_seconds <= 0 or max_requests <= 0:
                raise ValueError(
                    "Rate-limit windows and request counts must be positive"
                )

        self._limits = tuple(limits)
        self._request_times = [deque() for _ in limits]
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep

    def acquire(self) -> None:
        while True:
            now = self._clock()
            wait_seconds = 0.0

            for (window_seconds, max_requests), request_times in zip(
                self._limits,
                self._request_times,
            ):
                while request_times and now - request_times[0] >= window_seconds:
                    request_times.popleft()
                if len(request_times) >= max_requests:
                    wait_seconds = max(
                        wait_seconds,
                        window_seconds - (now - request_times[0]),
                    )

            if wait_seconds <= 0:
                break

            print(
                f"{PULLPUSH_LOG_PREFIX} rate limit reached; "
                f"waiting up to {wait_seconds:.1f}s"
            )
            self._sleeper(min(wait_seconds, 10.0))

        request_time = self._clock()
        for request_times in self._request_times:
            request_times.append(request_time)


class _PersistentSlidingWindowRateLimiter:
    """SQLite-backed sliding-window limiter shared across Python processes."""

    def __init__(
        self,
        limits: Sequence[tuple[float, int]],
        state_path: Union[str, Path],
        clock: Optional[Callable[[], float]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        if not limits:
            raise ValueError("At least one rate limit is required")
        for window_seconds, max_requests in limits:
            if window_seconds <= 0 or max_requests <= 0:
                raise ValueError(
                    "Rate-limit windows and request counts must be positive"
                )

        self._limits = tuple(limits)
        self._state_path = Path(state_path)
        self._clock = clock or time.time
        self._sleeper = sleeper or time.sleep
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._state_path),
            timeout=30.0,
            isolation_level=None,
        )
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS request_log "
                "(requested_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS "
                "request_log_requested_at_idx "
                "ON request_log(requested_at)"
            )
        finally:
            connection.close()
        self._schema_ready = True

    def acquire(self) -> None:
        self._ensure_schema()
        largest_window = max(window for window, _ in self._limits)

        while True:
            now = self._clock()
            wait_seconds = 0.0
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM request_log WHERE requested_at <= ?",
                    (now - largest_window,),
                )
                for window_seconds, max_requests in self._limits:
                    limiting_request = connection.execute(
                        "SELECT requested_at FROM request_log "
                        "WHERE requested_at > ? "
                        "ORDER BY requested_at ASC "
                        "LIMIT 1 OFFSET ?",
                        (now - window_seconds, max_requests - 1),
                    ).fetchone()
                    if limiting_request is not None:
                        wait_seconds = max(
                            wait_seconds,
                            float(limiting_request[0])
                            + window_seconds
                            - now,
                        )

                if wait_seconds <= 0:
                    connection.execute(
                        "INSERT INTO request_log(requested_at) VALUES (?)",
                        (now,),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

            if wait_seconds <= 0:
                return
            print(
                f"{PULLPUSH_LOG_PREFIX} persistent rate limit reached; "
                f"waiting up to {wait_seconds:.1f}s"
            )
            self._sleeper(min(wait_seconds, 10.0))


_SHARED_RATE_LIMITER = _PersistentSlidingWindowRateLimiter(
    PULLPUSH_RATE_LIMITS,
    PULLPUSH_RATE_LIMIT_STATE_PATH,
)


def _normalize_reddit_name(value: Optional[str], prefix: str) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip().lstrip("/")
    if normalized.lower().startswith(prefix.lower()):
        normalized = normalized[len(prefix):]
    return normalized or None


def _coerce_enum(value: object, enum_type: type[Enum], parameter_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except ValueError as exc:
        accepted_values = ", ".join(item.value for item in enum_type)
        raise ValueError(
            f"{parameter_name} must be one of: {accepted_values}"
        ) from exc


class PullPushRequestManager:
    def __init__(
        self,
        timeout_sec: float = 30.0,
        max_retries: int = 3,
        request_spacing_mean_sec: float = 0.0,
        request_spacing_stddev_sec: float = 0.0,
        *,
        _session: Optional[requests.Session] = None,
        _rate_limiter: Optional[_RateLimiter] = None,
        _sleeper: Optional[Callable[[float], None]] = None,
        _pacing_clock: Optional[Callable[[], float]] = None,
        _standard_normal_sampler: Optional[Callable[[], float]] = None,
    ) -> None:
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if request_spacing_mean_sec < 0 or request_spacing_stddev_sec < 0:
            raise ValueError(
                "Request-spacing mean and standard deviation "
                "must be non-negative"
            )

        self._timeout_sec = timeout_sec
        self._max_retries = max_retries
        self._session = _session or requests.Session()
        self._rate_limiter = _rate_limiter or _SHARED_RATE_LIMITER
        self._sleeper = _sleeper or time.sleep
        self._request_pacer = None
        if request_spacing_mean_sec or request_spacing_stddev_sec:
            self._request_pacer = _GaussianRequestPacer(
                request_spacing_mean_sec,
                request_spacing_stddev_sec,
                clock=_pacing_clock,
                sleeper=self._sleeper,
                standard_normal_sampler=_standard_normal_sampler,
            )
        self._headers = {"User-Agent": USER_AGENT}

    @staticmethod
    def _retry_after_seconds(response: requests.Response) -> Optional[float]:
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            return None

        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                (retry_at - datetime.now(timezone.utc)).total_seconds(),
            )

    def _request_get(
        self,
        params: dict[str, object],
        *,
        url: str = PULLPUSH_COMMENT_SEARCH_URL,
    ) -> dict[str, object]:
        for attempt in range(self._max_retries + 1):
            if self._request_pacer is not None:
                self._request_pacer.wait()
            self._rate_limiter.acquire()
            if self._request_pacer is not None:
                self._request_pacer.mark_request_started()
            print(f"{PULLPUSH_LOG_PREFIX} sent query with params: {params}")

            try:
                response = self._session.get(
                    url,
                    params=params,
                    headers=self._headers,
                    timeout=self._timeout_sec,
                )
            except requests.RequestException:
                if attempt >= self._max_retries:
                    raise
                self._sleeper(min(2 ** attempt, 60))
                continue

            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise PullPushAPIError(
                        "PullPush returned a non-JSON response"
                    ) from exc
                if not isinstance(payload, dict):
                    raise PullPushAPIError(
                        "Expected the PullPush response to be a JSON object"
                    )
                if payload.get("error") is not None:
                    raise PullPushAPIError(
                        f"PullPush returned an API error: {payload['error']}"
                    )
                if not isinstance(payload.get("data"), list):
                    raise PullPushAPIError(
                        "Expected the PullPush response `data` field to be a list"
                    )
                return payload

            should_retry = (
                response.status_code == 429
                or 500 <= response.status_code < 600
            )
            if should_retry and attempt < self._max_retries:
                retry_after = self._retry_after_seconds(response)
                if retry_after is not None:
                    wait_seconds = retry_after
                elif response.status_code == 429:
                    wait_seconds = 60.0
                else:
                    wait_seconds = min(2 ** attempt, 60)
                self._sleeper(wait_seconds)
                continue

            response_text = getattr(response, "text", "")
            if (
                response.status_code == 400
                and "query is too complex" in response_text.lower()
            ):
                try:
                    raw_payload = response.json()
                except ValueError:
                    raw_payload = {"raw_text": response_text}
                raise PullPushQueryTooComplexError(
                    "PullPush rejected a broad timestamp window as too "
                    f"complex: {response_text[:500]}",
                    raw_payload=raw_payload,
                )
            raise PullPushAPIError(
                f"PullPush request failed with HTTP {response.status_code}: "
                f"{response_text[:500]}"
            )

        raise AssertionError("Unreachable retry state")

    @staticmethod
    def _build_comment_params(
        *,
        size: int,
        query_term: Optional[str],
        subreddit_name: Optional[str],
        sort: PullPushSort,
        sort_type: PullPushSortType,
        author: Optional[str],
        after: Optional[TimeFilter],
        before: Optional[TimeFilter],
        link_id: Optional[str],
        comment_ids: Optional[Sequence[str]] = None,
    ) -> dict[str, object]:
        if size < 1 or size > PULLPUSH_MAX_PAGE_SIZE:
            raise ValueError(
                f"size must be between 1 and {PULLPUSH_MAX_PAGE_SIZE}"
            )

        params: dict[str, object] = {
            "size": size,
            "sort": sort.value,
            "sort_type": sort_type.value,
        }
        normalized_subreddit = _normalize_reddit_name(subreddit_name, "r/")
        normalized_author = _normalize_reddit_name(author, "u/")
        normalized_link_id = _normalize_reddit_name(link_id, "t3_")

        if query_term is not None:
            params["q"] = query_term
        if normalized_subreddit is not None:
            params["subreddit"] = normalized_subreddit
        if normalized_author is not None:
            params["author"] = normalized_author
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        if normalized_link_id is not None:
            params["link_id"] = normalized_link_id
        if comment_ids is not None:
            normalized_ids = [
                _normalize_reddit_name(comment_id, "t1_")
                for comment_id in comment_ids
            ]
            params["ids"] = ",".join(
                comment_id for comment_id in normalized_ids if comment_id
            )

        return params

    @staticmethod
    def _build_submission_params(
        *,
        size: int,
        query_term: Optional[str],
        title: Optional[str],
        selftext: Optional[str],
        subreddit_name: Optional[str],
        sort: PullPushSort,
        sort_type: PullPushSortType,
        author: Optional[str],
        after: Optional[TimeFilter],
        before: Optional[TimeFilter],
        submission_ids: Optional[Sequence[str]] = None,
    ) -> dict[str, object]:
        if size < 1 or size > PULLPUSH_MAX_PAGE_SIZE:
            raise ValueError(
                f"size must be between 1 and {PULLPUSH_MAX_PAGE_SIZE}"
            )

        params: dict[str, object] = {
            "size": size,
            "sort": sort.value,
            "sort_type": sort_type.value,
        }
        normalized_subreddit = _normalize_reddit_name(subreddit_name, "r/")
        normalized_author = _normalize_reddit_name(author, "u/")

        if query_term is not None:
            params["q"] = query_term
        if title is not None:
            params["title"] = title
        if selftext is not None:
            params["selftext"] = selftext
        if normalized_subreddit is not None:
            params["subreddit"] = normalized_subreddit
        if normalized_author is not None:
            params["author"] = normalized_author
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        if submission_ids is not None:
            normalized_ids = [
                _normalize_reddit_name(submission_id, "t3_")
                for submission_id in submission_ids
            ]
            params["ids"] = ",".join(
                submission_id for submission_id in normalized_ids
                if submission_id
            )

        return params

    @staticmethod
    def _normalize_comment(
        data: dict[str, object],
        query_term: Optional[str],
    ) -> dict[str, object]:
        comment = dict(data)
        if not comment.get("permalink"):
            subreddit = str(comment["subreddit"])
            link_id = str(comment["link_id"])
            comment_id = str(comment["id"])
            if link_id.startswith("t3_"):
                link_id = link_id[3:]
            if comment_id.startswith("t1_"):
                comment_id = comment_id[3:]
            comment["permalink"] = (
                f"/r/{subreddit}/comments/{link_id}/_/{comment_id}/"
            )
        if query_term is not None:
            comment["query"] = query_term
        return comment

    @staticmethod
    def _normalize_submission(
        data: dict[str, object],
        query_term: Optional[str],
        title: Optional[str],
        selftext: Optional[str],
    ) -> dict[str, object]:
        submission = dict(data)
        submission_id = str(submission["id"])
        if submission_id.startswith("t3_"):
            submission_id = submission_id[3:]
        submission.setdefault("name", f"t3_{submission_id}")
        if not submission.get("permalink"):
            subreddit = str(submission["subreddit"])
            submission["permalink"] = (
                f"/r/{subreddit}/comments/{submission_id}/"
            )

        search_terms = []
        if query_term is not None:
            search_terms.append(f"q={query_term}")
        if title is not None:
            search_terms.append(f"title={title}")
        if selftext is not None:
            search_terms.append(f"selftext={selftext}")
        if search_terms:
            submission["query"] = " & ".join(search_terms)
        return submission

    def _collect_submission_pages(
        self,
        query_term: Optional[str],
        title: Optional[str],
        selftext: Optional[str],
        subreddit_name: Optional[str],
        num_results: Optional[int],
        *,
        sort: Union[PullPushSort, str],
        sort_type: Union[PullPushSortType, str],
        author: Optional[str],
        after: Optional[TimeFilter],
        before: Optional[TimeFilter],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        if num_results is not None and num_results < 0:
            raise ValueError("num_results must be non-negative or None")
        if num_results == 0:
            return [], []

        resolved_sort = _coerce_enum(sort, PullPushSort, "sort")
        resolved_sort_type = _coerce_enum(
            sort_type,
            PullPushSortType,
            "sort_type",
        )
        if (
            (num_results is None or num_results > PULLPUSH_MAX_PAGE_SIZE)
            and resolved_sort_type != PullPushSortType.CREATED_UTC
        ):
            raise ValueError(
                "PullPush searches over 100 results require "
                "sort_type='created_utc' for timestamp pagination"
            )

        pages: list[dict[str, object]] = []
        submissions: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        cursor_after = after
        cursor_before = before

        while num_results is None or len(submissions) < num_results:
            remaining = (
                PULLPUSH_MAX_PAGE_SIZE
                if num_results is None
                else num_results - len(submissions)
            )
            page_size = min(PULLPUSH_MAX_PAGE_SIZE, remaining)
            params = self._build_submission_params(
                size=page_size,
                query_term=query_term,
                title=title,
                selftext=selftext,
                subreddit_name=subreddit_name,
                sort=resolved_sort,
                sort_type=resolved_sort_type,
                author=author,
                after=cursor_after,
                before=cursor_before,
            )
            payload = self._request_get(
                params,
                url=PULLPUSH_SUBMISSION_SEARCH_URL,
            )
            pages.append(payload)
            page_data = payload["data"]
            if not page_data:
                break

            new_submissions = 0
            for record in page_data:
                if not isinstance(record, dict):
                    raise PullPushAPIError(
                        "Expected each PullPush submission to be a JSON object"
                    )
                if "id" not in record:
                    raise PullPushAPIError(
                        "PullPush submission is missing the `id` field"
                    )
                submission_id = str(record["id"])
                if submission_id not in seen_ids:
                    seen_ids.add(submission_id)
                    submissions.append(record)
                    new_submissions += 1

            if num_results is not None and len(submissions) >= num_results:
                break
            if len(page_data) < page_size:
                break
            if resolved_sort_type != PullPushSortType.CREATED_UTC:
                break

            try:
                timestamps = [
                    int(float(record["created_utc"]))
                    for record in page_data
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise PullPushPaginationError(
                    "PullPush submission is missing a valid "
                    "`created_utc` timestamp"
                ) from exc

            if resolved_sort == PullPushSort.DESC:
                previous_cursor = cursor_before
                next_cursor = min(timestamps) - 1
                cursor_before = next_cursor
                if (
                    cursor_after is not None
                    and isinstance(cursor_after, (int, float))
                    and next_cursor <= cursor_after
                ):
                    break
            else:
                previous_cursor = cursor_after
                next_cursor = max(timestamps) + 1
                cursor_after = next_cursor
                if (
                    cursor_before is not None
                    and isinstance(cursor_before, (int, float))
                    and next_cursor >= cursor_before
                ):
                    break

            if new_submissions == 0 or (
                previous_cursor is not None
                and str(previous_cursor) == str(next_cursor)
            ):
                raise PullPushPaginationError(
                    "PullPush submission pagination could not advance beyond "
                    f"timestamp {next_cursor}"
                )

        if num_results is not None:
            submissions = submissions[:num_results]
        return pages, submissions

    def _collect_submission_windows(
        self,
        query_term: Optional[str],
        title: Optional[str],
        selftext: Optional[str],
        subreddit_name: Optional[str],
        *,
        sort: Union[PullPushSort, str],
        sort_type: Union[PullPushSortType, str],
        author: Optional[str],
        start_utc: int,
        end_utc: int,
        max_results_per_window: int,
        max_depth: int,
        checkpoint_callback: Optional[
            Callable[[list[dict[str, object]]], None]
        ],
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        """Exhaust a bounded submission search by bisecting saturated windows.

        Each logical window is the non-overlapping integer timestamp interval
        ``[start_utc, end_utc)``. PullPush's documented ``after`` and ``before``
        filters are strict, so the request uses ``after=start_utc - 1`` and
        ``before=end_utc`` to include the first second and exclude the last.

        A response containing ``max_results_per_window`` rows is treated as
        potentially truncated and split into two child windows. Only records
        from unsaturated leaf windows are included in the returned submissions.
        Every API response, including saturated internal nodes, is retained in
        the audit-window list.
        """
        if isinstance(start_utc, bool) or not isinstance(start_utc, int):
            raise TypeError("start_utc must be an integer epoch timestamp")
        if isinstance(end_utc, bool) or not isinstance(end_utc, int):
            raise TypeError("end_utc must be an integer epoch timestamp")
        if start_utc >= end_utc:
            raise ValueError("start_utc must be less than end_utc")
        if (
            max_results_per_window < 1
            or max_results_per_window > PULLPUSH_MAX_PAGE_SIZE
        ):
            raise ValueError(
                "max_results_per_window must be between 1 and "
                f"{PULLPUSH_MAX_PAGE_SIZE}"
            )
        if max_depth < 0:
            raise ValueError("max_depth must be non-negative")

        resolved_sort = _coerce_enum(sort, PullPushSort, "sort")
        resolved_sort_type = _coerce_enum(
            sort_type,
            PullPushSortType,
            "sort_type",
        )
        if resolved_sort_type != PullPushSortType.CREATED_UTC:
            raise ValueError(
                "Adaptive PullPush window searches require "
                "sort_type='created_utc'"
            )

        audit_windows: list[dict[str, object]] = []
        leaf_submissions_by_id: dict[str, dict[str, object]] = {}
        pending_windows = [(start_utc, end_utc, 0)]

        while pending_windows:
            window_start, window_end, depth = pending_windows.pop()
            params = self._build_submission_params(
                size=max_results_per_window,
                query_term=query_term,
                title=title,
                selftext=selftext,
                subreddit_name=subreddit_name,
                sort=resolved_sort,
                sort_type=resolved_sort_type,
                author=author,
                after=window_start - 1,
                before=window_end,
            )
            try:
                payload = self._request_get(
                    params,
                    url=PULLPUSH_SUBMISSION_SEARCH_URL,
                )
            except PullPushQueryTooComplexError as exc:
                audit_windows.append({
                    "start_utc_inclusive": window_start,
                    "end_utc_exclusive": window_end,
                    "depth": depth,
                    "result_count": None,
                    "max_results_per_window": max_results_per_window,
                    "saturated": None,
                    "leaf": False,
                    "split_reason": "query_too_complex",
                    "response": exc.raw_payload,
                })
                if checkpoint_callback is not None:
                    checkpoint_callback(audit_windows)
                if depth >= max_depth:
                    raise PullPushPaginationError(
                        "Adaptive PullPush search reached max_depth="
                        f"{max_depth} after a query-too-complex response for "
                        f"[{window_start}, {window_end})"
                    ) from exc
                midpoint = window_start + (window_end - window_start) // 2
                if midpoint <= window_start or midpoint >= window_end:
                    raise PullPushPaginationError(
                        "Adaptive PullPush search received a "
                        "query-too-complex response for indivisible window "
                        f"[{window_start}, {window_end})"
                    ) from exc
                pending_windows.append((midpoint, window_end, depth + 1))
                pending_windows.append((window_start, midpoint, depth + 1))
                continue
            page_data = payload["data"]
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                if metadata.get("timed_out") is True:
                    raise PullPushAPIError(
                        "PullPush reported a timed-out adaptive-window query "
                        f"for [{window_start}, {window_end})"
                    )
                shards = metadata.get("shards")
                if (
                    isinstance(shards, dict)
                    and int(shards.get("failed", 0)) > 0
                ):
                    raise PullPushAPIError(
                        "PullPush reported failed shards for adaptive-window "
                        f"query [{window_start}, {window_end})"
                    )

            for record in page_data:
                if not isinstance(record, dict):
                    raise PullPushAPIError(
                        "Expected each PullPush submission to be a JSON object"
                    )
                if "id" not in record:
                    raise PullPushAPIError(
                        "PullPush submission is missing the `id` field"
                    )
                try:
                    created_utc = int(float(record["created_utc"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise PullPushPaginationError(
                        "PullPush submission is missing a valid "
                        "`created_utc` timestamp"
                    ) from exc
                if not window_start <= created_utc < window_end:
                    raise PullPushPaginationError(
                        "PullPush returned a submission outside the requested "
                        f"window [{window_start}, {window_end}): "
                        f"{created_utc}"
                    )

            saturated = len(page_data) == max_results_per_window
            audit_window = {
                "start_utc_inclusive": window_start,
                "end_utc_exclusive": window_end,
                "depth": depth,
                "result_count": len(page_data),
                "max_results_per_window": max_results_per_window,
                "saturated": saturated,
                "leaf": not saturated,
                "response": payload,
            }
            audit_windows.append(audit_window)
            if checkpoint_callback is not None:
                checkpoint_callback(audit_windows)

            if saturated:
                if depth >= max_depth:
                    raise PullPushPaginationError(
                        "Adaptive PullPush search reached max_depth="
                        f"{max_depth} with a saturated window "
                        f"[{window_start}, {window_end})"
                    )
                midpoint = window_start + (window_end - window_start) // 2
                if midpoint <= window_start or midpoint >= window_end:
                    raise PullPushPaginationError(
                        "Adaptive PullPush search found "
                        f"{max_results_per_window} results in indivisible "
                        f"window [{window_start}, {window_end}); timestamp "
                        "windowing cannot prove completeness"
                    )

                # Stack the right side first so the left/older interval is
                # processed next. The child intervals are non-overlapping.
                pending_windows.append((midpoint, window_end, depth + 1))
                pending_windows.append((window_start, midpoint, depth + 1))
                continue

            for record in page_data:
                submission_id = str(record["id"])
                leaf_submissions_by_id.setdefault(submission_id, record)

        submissions = list(leaf_submissions_by_id.values())
        submissions.sort(
            key=lambda record: int(float(record["created_utc"])),
            reverse=resolved_sort == PullPushSort.DESC,
        )
        return audit_windows, submissions

    def _collect_comment_pages(
        self,
        query_term: Optional[str],
        subreddit_name: Optional[str],
        num_results: int,
        *,
        sort: Union[PullPushSort, str],
        sort_type: Union[PullPushSortType, str],
        author: Optional[str],
        after: Optional[TimeFilter],
        before: Optional[TimeFilter],
        link_id: Optional[str],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        if num_results < 0:
            raise ValueError("num_results must be non-negative")
        if num_results == 0:
            return [], []

        resolved_sort = _coerce_enum(sort, PullPushSort, "sort")
        resolved_sort_type = _coerce_enum(
            sort_type,
            PullPushSortType,
            "sort_type",
        )
        if (
            num_results > PULLPUSH_MAX_PAGE_SIZE
            and resolved_sort_type != PullPushSortType.CREATED_UTC
        ):
            raise ValueError(
                "PullPush searches over 100 results require "
                "sort_type='created_utc' for timestamp pagination"
            )

        pages: list[dict[str, object]] = []
        comments: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        cursor_after = after
        cursor_before = before
        first_page = True

        while len(comments) < num_results:
            page_size = (
                min(PULLPUSH_MAX_PAGE_SIZE, num_results)
                if first_page
                else PULLPUSH_MAX_PAGE_SIZE
            )
            params = self._build_comment_params(
                size=page_size,
                query_term=query_term,
                subreddit_name=subreddit_name,
                sort=resolved_sort,
                sort_type=resolved_sort_type,
                author=author,
                after=cursor_after,
                before=cursor_before,
                link_id=link_id,
            )
            payload = self._request_get(params)
            pages.append(payload)
            page_data = payload["data"]
            if not page_data:
                break

            new_comments = 0
            for record in page_data:
                if not isinstance(record, dict):
                    raise PullPushAPIError(
                        "Expected each PullPush comment to be a JSON object"
                    )
                if "id" not in record:
                    raise PullPushAPIError(
                        "PullPush comment is missing the `id` field"
                    )
                comment_id = str(record["id"])
                if comment_id not in seen_ids:
                    seen_ids.add(comment_id)
                    comments.append(record)
                    new_comments += 1

            if len(comments) >= num_results:
                break
            if len(page_data) < page_size:
                break
            if resolved_sort_type != PullPushSortType.CREATED_UTC:
                break

            try:
                timestamps = [
                    int(float(record["created_utc"]))
                    for record in page_data
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise PullPushPaginationError(
                    "PullPush comment is missing a valid `created_utc` timestamp"
                ) from exc

            if resolved_sort == PullPushSort.DESC:
                next_cursor = min(timestamps)
                previous_cursor = cursor_before
                cursor_before = next_cursor
            else:
                next_cursor = max(timestamps)
                previous_cursor = cursor_after
                cursor_after = next_cursor

            if new_comments == 0 and (
                previous_cursor is None
                or str(previous_cursor) == str(next_cursor)
            ):
                raise PullPushPaginationError(
                    "PullPush pagination could not advance beyond timestamp "
                    f"{next_cursor}; refusing to silently omit comments"
                )

            first_page = False

        return pages, comments[:num_results]

    def search_comments(
        self,
        query_term: Optional[str] = None,
        subreddit_name: Optional[str] = None,
        num_results: int = 100,
        *,
        sort: Union[PullPushSort, str] = PullPushSort.DESC,
        sort_type: Union[PullPushSortType, str] = PullPushSortType.CREATED_UTC,
        author: Optional[str] = None,
        after: Optional[TimeFilter] = None,
        before: Optional[TimeFilter] = None,
        link_id: Optional[str] = None,
    ) -> list[dict[str, object]]:
        _, comments = self._collect_comment_pages(
            query_term,
            subreddit_name,
            num_results,
            sort=sort,
            sort_type=sort_type,
            author=author,
            after=after,
            before=before,
            link_id=link_id,
        )
        normalized_comments = [
            self._normalize_comment(comment, query_term)
            for comment in comments
        ]
        print(
            f"{PULLPUSH_LOG_PREFIX} comment search yielded "
            f"{len(normalized_comments)} comments"
        )
        return normalized_comments

    def search_comments_raw(
        self,
        query_term: Optional[str] = None,
        subreddit_name: Optional[str] = None,
        num_results: int = 100,
        *,
        sort: Union[PullPushSort, str] = PullPushSort.DESC,
        sort_type: Union[PullPushSortType, str] = PullPushSortType.CREATED_UTC,
        author: Optional[str] = None,
        after: Optional[TimeFilter] = None,
        before: Optional[TimeFilter] = None,
        link_id: Optional[str] = None,
    ) -> list[dict[str, object]]:
        pages, _ = self._collect_comment_pages(
            query_term,
            subreddit_name,
            num_results,
            sort=sort,
            sort_type=sort_type,
            author=author,
            after=after,
            before=before,
            link_id=link_id,
        )
        return pages

    def search_submissions(
        self,
        query_term: Optional[str] = None,
        subreddit_name: Optional[str] = None,
        num_results: Optional[int] = 100,
        *,
        title: Optional[str] = None,
        selftext: Optional[str] = None,
        sort: Union[PullPushSort, str] = PullPushSort.DESC,
        sort_type: Union[PullPushSortType, str] = PullPushSortType.CREATED_UTC,
        author: Optional[str] = None,
        after: Optional[TimeFilter] = None,
        before: Optional[TimeFilter] = None,
    ) -> list[dict[str, object]]:
        _, submissions = self._collect_submission_pages(
            query_term,
            title,
            selftext,
            subreddit_name,
            num_results,
            sort=sort,
            sort_type=sort_type,
            author=author,
            after=after,
            before=before,
        )
        normalized_submissions = [
            self._normalize_submission(
                submission,
                query_term,
                title,
                selftext,
            )
            for submission in submissions
        ]
        print(
            f"{PULLPUSH_LOG_PREFIX} submission search yielded "
            f"{len(normalized_submissions)} submissions"
        )
        return normalized_submissions

    def search_submissions_raw(
        self,
        query_term: Optional[str] = None,
        subreddit_name: Optional[str] = None,
        num_results: Optional[int] = 100,
        *,
        title: Optional[str] = None,
        selftext: Optional[str] = None,
        sort: Union[PullPushSort, str] = PullPushSort.DESC,
        sort_type: Union[PullPushSortType, str] = PullPushSortType.CREATED_UTC,
        author: Optional[str] = None,
        after: Optional[TimeFilter] = None,
        before: Optional[TimeFilter] = None,
    ) -> list[dict[str, object]]:
        pages, _ = self._collect_submission_pages(
            query_term,
            title,
            selftext,
            subreddit_name,
            num_results,
            sort=sort,
            sort_type=sort_type,
            author=author,
            after=after,
            before=before,
        )
        return pages

    def search_submissions_adaptive(
        self,
        query_term: Optional[str] = None,
        subreddit_name: Optional[str] = None,
        *,
        title: Optional[str] = None,
        selftext: Optional[str] = None,
        sort: Union[PullPushSort, str] = PullPushSort.DESC,
        sort_type: Union[
            PullPushSortType,
            str,
        ] = PullPushSortType.CREATED_UTC,
        author: Optional[str] = None,
        start_utc: int,
        end_utc: int,
        max_results_per_window: int = PULLPUSH_MAX_PAGE_SIZE,
        max_depth: int = 64,
        checkpoint_callback: Optional[
            Callable[[list[dict[str, object]]], None]
        ] = None,
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        """Search submissions with adaptive divide-and-conquer windows.

        Returns ``(audit_windows, submissions)`` from a single API pass.
        ``audit_windows`` contains the untouched PullPush response under each
        record's ``response`` key plus its exact interval, depth, and saturation
        decision. ``submissions`` contains normalized, ID-deduplicated records
        from complete leaf windows only. When ``checkpoint_callback`` is
        provided, it receives the accumulated audit-window list after every
        accepted API response, including internal split responses.
        """
        audit_windows, submissions = self._collect_submission_windows(
            query_term,
            title,
            selftext,
            subreddit_name,
            sort=sort,
            sort_type=sort_type,
            author=author,
            start_utc=start_utc,
            end_utc=end_utc,
            max_results_per_window=max_results_per_window,
            max_depth=max_depth,
            checkpoint_callback=checkpoint_callback,
        )
        normalized_submissions = [
            self._normalize_submission(
                submission,
                query_term,
                title,
                selftext,
            )
            for submission in submissions
        ]
        leaf_windows = sum(
            bool(window["leaf"]) for window in audit_windows
        )
        print(
            f"{PULLPUSH_LOG_PREFIX} adaptive submission search yielded "
            f"{len(normalized_submissions)} submissions across "
            f"{leaf_windows} complete leaf windows "
            f"({len(audit_windows)} total requests)"
        )
        return audit_windows, normalized_submissions

    def get_comments_by_ids(
        self,
        comment_ids: Sequence[str],
    ) -> list[dict[str, object]]:
        normalized_ids = [
            _normalize_reddit_name(comment_id, "t1_")
            for comment_id in comment_ids
        ]
        normalized_ids = [
            comment_id for comment_id in normalized_ids if comment_id
        ]
        comments: list[dict[str, object]] = []

        for start in range(0, len(normalized_ids), PULLPUSH_MAX_PAGE_SIZE):
            comment_ids_batch = normalized_ids[
                start:start + PULLPUSH_MAX_PAGE_SIZE
            ]
            params = self._build_comment_params(
                size=len(comment_ids_batch),
                query_term=None,
                subreddit_name=None,
                sort=PullPushSort.DESC,
                sort_type=PullPushSortType.CREATED_UTC,
                author=None,
                after=None,
                before=None,
                link_id=None,
                comment_ids=comment_ids_batch,
            )
            payload = self._request_get(params)
            comments.extend(
                self._normalize_comment(comment, None)
                for comment in payload["data"]
            )

        return comments
