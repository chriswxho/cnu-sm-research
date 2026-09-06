from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import requests

from api import (
    PullPushRequestManager,
    PullPushSort,
    PullPushSortType,
    RedditRequestManager,
    SortBy,
)
from api.pullpush import (
    PullPushAPIError,
    PullPushPaginationError,
    PULLPUSH_SUBMISSION_SEARCH_URL,
    _GaussianRequestPacer,
    _PersistentSlidingWindowRateLimiter,
    _SlidingWindowRateLimiter,
)
from api.reddit import RedditRequestManager as RedditRequestManagerFromModule
from csv_dumper import RedditResultType, map_research_record


class FakeResponse:
    def __init__(
        self,
        payload=None,
        status_code=200,
        headers=None,
        text="",
        json_error=None,
    ):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def comment(comment_id, created_utc, subreddit="CPTSD", link_id="t3_post"):
    return {
        "id": comment_id,
        "body": f"body {comment_id}",
        "created_utc": created_utc,
        "link_id": link_id,
        "parent_id": link_id,
        "score": 1,
        "subreddit": subreddit,
    }


def submission(submission_id, created_utc, subreddit="CPTSD"):
    return {
        "id": submission_id,
        "title": f"title {submission_id}",
        "selftext": f"body {submission_id}",
        "created_utc": created_utc,
        "score": 1,
        "subreddit": subreddit,
        "is_self": True,
        "url": f"https://www.reddit.com/comments/{submission_id}/",
    }


def response(data, metadata=None, error=None):
    return FakeResponse(
        {
            "data": data,
            "metadata": metadata or {},
            "error": error,
        }
    )


def manager(responses, max_retries=0, sleeper=None):
    return PullPushRequestManager(
        max_retries=max_retries,
        _session=FakeSession(responses),
        _rate_limiter=_SlidingWindowRateLimiter(((1.0, 1000),)),
        _sleeper=sleeper,
    )


class PullPushRequestManagerTests(unittest.TestCase):
    def test_package_reexports_existing_and_new_clients(self):
        self.assertIs(RedditRequestManager, RedditRequestManagerFromModule)
        self.assertEqual(SortBy.NEW.value, "new")
        self.assertEqual(PullPushSort.DESC.value, "desc")
        self.assertEqual(PullPushSortType.CREATED_UTC.value, "created_utc")

    def test_search_encodes_and_normalizes_documented_filters(self):
        request_manager = manager([response([])])

        request_manager.search_comments(
            '"sibling abuse"',
            "/r/CPTSD",
            1,
            sort="asc",
            author="/u/researcher",
            after="30d",
            before=1_700_000_000,
            link_id="t3_abc123",
        )

        _, request_kwargs = request_manager._session.calls[0]
        self.assertEqual(
            request_kwargs["params"],
            {
                "size": 1,
                "sort": "asc",
                "sort_type": "created_utc",
                "q": '"sibling abuse"',
                "subreddit": "CPTSD",
                "author": "researcher",
                "after": "30d",
                "before": 1_700_000_000,
                "link_id": "abc123",
            },
        )
        self.assertEqual(request_kwargs["timeout"], 30.0)

    def test_search_auto_paginates_deduplicates_and_truncates(self):
        first_page = [
            comment(f"first_{index}", 200 - index)
            for index in range(100)
        ]
        second_page = [first_page[-1]] + [
            comment(f"second_{index}", 100 - index)
            for index in range(60)
        ]
        request_manager = manager([
            response(first_page),
            response(second_page),
        ])

        comments = request_manager.search_comments(
            "sibling abuse",
            "CPTSD",
            150,
        )

        self.assertEqual(len(comments), 150)
        self.assertEqual(len({item["id"] for item in comments}), 150)
        self.assertEqual(comments[0]["query"], "sibling abuse")
        self.assertEqual(
            comments[0]["permalink"],
            "/r/CPTSD/comments/post/_/first_0/",
        )
        _, second_request = request_manager._session.calls[1]
        self.assertEqual(second_request["params"]["before"], 101)
        self.assertEqual(second_request["params"]["size"], 100)

    def test_raw_search_preserves_response_envelope(self):
        payload = {
            "data": [comment("raw_id", 100)],
            "metadata": {"query_str": "'sibling abuse'"},
            "error": None,
        }
        request_manager = manager([FakeResponse(payload)])

        pages = request_manager.search_comments_raw(
            "sibling abuse",
            "CPTSD",
            1,
        )

        self.assertIs(pages[0], payload)
        self.assertNotIn("permalink", pages[0]["data"][0])
        self.assertEqual(pages[0]["metadata"]["query_str"], "'sibling abuse'")

    def test_rejects_multi_page_non_timestamp_sort(self):
        request_manager = manager([])

        with self.assertRaisesRegex(ValueError, "require.*created_utc"):
            request_manager.search_comments(
                "sibling abuse",
                "CPTSD",
                101,
                sort_type=PullPushSortType.SCORE,
            )

        self.assertEqual(request_manager._session.calls, [])

    def test_raises_when_inclusive_cursor_cannot_advance(self):
        page = [comment(f"id_{index}", 100) for index in range(100)]
        request_manager = manager([response(page), response(page)])

        with self.assertRaises(PullPushPaginationError):
            request_manager.search_comments(
                "sibling abuse",
                "CPTSD",
                101,
            )

    def test_comment_id_lookup_batches_and_normalizes_ids(self):
        request_manager = manager([
            response([comment("first", 2)]),
            response([comment("last", 1)]),
        ])
        comment_ids = [f"t1_id_{index}" for index in range(101)]

        comments = request_manager.get_comments_by_ids(comment_ids)

        self.assertEqual(len(comments), 2)
        self.assertEqual(len(request_manager._session.calls), 2)
        first_params = request_manager._session.calls[0][1]["params"]
        second_params = request_manager._session.calls[1][1]["params"]
        self.assertEqual(first_params["size"], 100)
        self.assertTrue(first_params["ids"].startswith("id_0,id_1"))
        self.assertNotIn("t1_", first_params["ids"])
        self.assertEqual(second_params["size"], 1)
        self.assertEqual(second_params["ids"], "id_100")

    def test_flat_comment_integrates_with_research_csv_mapper(self):
        request_manager = manager([
            response([comment("comment_id", "1700000000")]),
        ])

        pullpush_comment = request_manager.search_comments(
            "sibling abuse",
            "CPTSD",
            1,
        )[0]
        mapped = map_research_record(
            pullpush_comment,
            RedditResultType.COMMENT,
        )

        self.assertEqual(mapped["type"], "comment")
        self.assertEqual(mapped["title"], "")
        self.assertEqual(mapped["body"], "body comment_id")
        self.assertEqual(mapped["media_url"], "")
        self.assertEqual(
            mapped["url"],
            "https://www.reddit.com/r/CPTSD/comments/post/_/comment_id/",
        )

    def test_submission_search_uses_documented_endpoint_and_filters(self):
        request_manager = manager([response([])])

        request_manager.search_submissions(
            subreddit_name="/r/CPTSD",
            num_results=1,
            title='"sibling abuse"',
            author="/u/researcher",
            after=1_469_502_000,
            before=1_785_034_800,
        )

        request_url, request_kwargs = request_manager._session.calls[0]
        self.assertEqual(request_url, PULLPUSH_SUBMISSION_SEARCH_URL)
        self.assertEqual(
            request_kwargs["params"],
            {
                "size": 1,
                "sort": "desc",
                "sort_type": "created_utc",
                "title": '"sibling abuse"',
                "subreddit": "CPTSD",
                "author": "researcher",
                "after": 1_469_502_000,
                "before": 1_785_034_800,
            },
        )

    def test_submission_search_exhausts_timestamp_pages_and_normalizes(self):
        first_page = [
            submission(f"first_{index}", 300 - index)
            for index in range(100)
        ]
        second_page = [
            submission(f"second_{index}", 199 - index)
            for index in range(3)
        ]
        request_manager = manager([
            response(first_page),
            response(second_page),
        ])

        submissions = request_manager.search_submissions(
            num_results=None,
            selftext='"sibling abuse"',
            after=1,
            before=400,
        )

        self.assertEqual(len(submissions), 103)
        self.assertEqual(len({item["id"] for item in submissions}), 103)
        self.assertEqual(submissions[0]["name"], "t3_first_0")
        self.assertEqual(
            submissions[0]["permalink"],
            "/r/CPTSD/comments/first_0/",
        )
        self.assertEqual(
            submissions[0]["query"],
            'selftext="sibling abuse"',
        )
        second_params = request_manager._session.calls[1][1]["params"]
        self.assertEqual(second_params["before"], 200)
        self.assertEqual(second_params["after"], 1)

    def test_flat_submission_integrates_with_research_csv_mapper(self):
        request_manager = manager([
            response([submission("submission_id", 1_700_000_000)]),
        ])

        pullpush_submission = request_manager.search_submissions(
            num_results=1,
            title='"sibling abuse"',
        )[0]
        mapped = map_research_record(
            pullpush_submission,
            RedditResultType.POST,
        )

        self.assertEqual(mapped["type"], "post")
        self.assertEqual(mapped["title"], "title submission_id")
        self.assertEqual(mapped["body"], "body submission_id")
        self.assertEqual(
            mapped["url"],
            "https://www.reddit.com/r/CPTSD/comments/submission_id/",
        )

    def test_adaptive_submission_search_keeps_complete_leaf_window(self):
        request_manager = manager([
            response([
                submission("first", 101),
                submission("second", 199),
            ]),
        ])

        windows, submissions = request_manager.search_submissions_adaptive(
            title='"sibling abuse"',
            start_utc=100,
            end_utc=200,
            max_results_per_window=3,
        )

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["start_utc_inclusive"], 100)
        self.assertEqual(windows[0]["end_utc_exclusive"], 200)
        self.assertEqual(windows[0]["result_count"], 2)
        self.assertFalse(windows[0]["saturated"])
        self.assertTrue(windows[0]["leaf"])
        self.assertEqual(len(submissions), 2)
        self.assertEqual(submissions[0]["id"], "second")
        self.assertEqual(
            submissions[0]["query"],
            'title="sibling abuse"',
        )

        request_params = request_manager._session.calls[0][1]["params"]
        self.assertEqual(request_params["size"], 3)
        self.assertEqual(request_params["after"], 99)
        self.assertEqual(request_params["before"], 200)

    def test_adaptive_submission_search_bisects_saturated_window(self):
        root = [
            submission("left", 1),
            submission("middle", 6),
            submission("right", 9),
        ]
        request_manager = manager([
            response(root),
            response([root[0]]),
            response(root[1:]),
        ])

        windows, submissions = request_manager.search_submissions_adaptive(
            selftext='"sibling abuse"',
            start_utc=0,
            end_utc=10,
            max_results_per_window=3,
        )

        self.assertEqual(len(windows), 3)
        self.assertTrue(windows[0]["saturated"])
        self.assertFalse(windows[0]["leaf"])
        self.assertEqual(
            [
                (
                    window["start_utc_inclusive"],
                    window["end_utc_exclusive"],
                    window["leaf"],
                )
                for window in windows
            ],
            [
                (0, 10, False),
                (0, 5, True),
                (5, 10, True),
            ],
        )
        self.assertEqual(
            {item["id"] for item in submissions},
            {"left", "middle", "right"},
        )

        request_params = [
            call[1]["params"]
            for call in request_manager._session.calls
        ]
        self.assertEqual(
            [
                (params["after"], params["before"])
                for params in request_params
            ],
            [
                (-1, 10),
                (-1, 5),
                (4, 10),
            ],
        )

    def test_adaptive_submission_search_bisects_query_too_complex_window(self):
        too_complex = FakeResponse(
            status_code=400,
            text=(
                '{"error":"This query is too complex. Try setting time '
                'limits"}'
            ),
            payload={
                "error": (
                    "This query is too complex. Try setting time limits"
                )
            },
        )
        request_manager = manager([
            too_complex,
            response([submission("left", 1)]),
            response([submission("right", 9)]),
        ])

        windows, submissions = request_manager.search_submissions_adaptive(
            selftext='"sibling abuse"',
            start_utc=0,
            end_utc=10,
            max_results_per_window=3,
        )

        self.assertEqual(len(windows), 3)
        self.assertIsNone(windows[0]["result_count"])
        self.assertIsNone(windows[0]["saturated"])
        self.assertFalse(windows[0]["leaf"])
        self.assertEqual(
            windows[0]["split_reason"],
            "query_too_complex",
        )
        self.assertEqual(
            windows[0]["response"]["error"],
            "This query is too complex. Try setting time limits",
        )
        self.assertEqual(
            [
                (
                    window["start_utc_inclusive"],
                    window["end_utc_exclusive"],
                )
                for window in windows
            ],
            [(0, 10), (0, 5), (5, 10)],
        )
        self.assertEqual(
            {item["id"] for item in submissions},
            {"left", "right"},
        )

    def test_adaptive_submission_search_checkpoints_before_later_failure(self):
        root = [
            submission("left", 1),
            submission("middle", 6),
            submission("right", 9),
        ]
        request_manager = manager([
            response(root),
            response([root[0]]),
            requests.ConnectionError("later child failed"),
        ])
        checkpoint_lengths = []
        checkpoint_snapshots = []

        with self.assertRaisesRegex(
            requests.ConnectionError,
            "later child failed",
        ):
            request_manager.search_submissions_adaptive(
                selftext='"sibling abuse"',
                start_utc=0,
                end_utc=10,
                max_results_per_window=3,
                checkpoint_callback=lambda windows: (
                    checkpoint_lengths.append(len(windows)),
                    checkpoint_snapshots.append(list(windows)),
                ),
            )

        self.assertEqual(checkpoint_lengths, [1, 2])
        self.assertFalse(checkpoint_snapshots[-1][-1]["saturated"])
        self.assertTrue(checkpoint_snapshots[-1][-1]["leaf"])
        self.assertEqual(
            checkpoint_snapshots[-1][-1]["response"]["data"][0]["id"],
            "left",
        )

    def test_adaptive_submission_search_refuses_saturated_second(self):
        request_manager = manager([
            response([submission("collision", 10)]),
        ])

        with self.assertRaisesRegex(
            PullPushPaginationError,
            "indivisible window",
        ):
            request_manager.search_submissions_adaptive(
                query_term="collision",
                start_utc=10,
                end_utc=11,
                max_results_per_window=1,
            )

    def test_adaptive_submission_search_rejects_out_of_window_record(self):
        request_manager = manager([
            response([submission("outside", 20)]),
        ])

        with self.assertRaisesRegex(
            PullPushPaginationError,
            "outside the requested window",
        ):
            request_manager.search_submissions_adaptive(
                start_utc=10,
                end_utc=20,
                max_results_per_window=2,
            )

    def test_adaptive_submission_search_validates_cap_and_sort(self):
        request_manager = manager([])

        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            request_manager.search_submissions_adaptive(
                start_utc=0,
                end_utc=10,
                max_results_per_window=101,
            )
        with self.assertRaisesRegex(ValueError, "created_utc"):
            request_manager.search_submissions_adaptive(
                start_utc=0,
                end_utc=10,
                sort_type=PullPushSortType.SCORE,
            )

        self.assertEqual(request_manager._session.calls, [])

    def test_retries_429_using_retry_after(self):
        sleeps = []
        request_manager = manager(
            [
                FakeResponse(
                    status_code=429,
                    headers={"Retry-After": "2"},
                    text="rate limited",
                ),
                response([]),
            ],
            max_retries=1,
            sleeper=sleeps.append,
        )

        result = request_manager.search_comments(num_results=1)

        self.assertEqual(result, [])
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(len(request_manager._session.calls), 2)

    def test_headerless_429_uses_full_minute_cooldown(self):
        sleeps = []
        request_manager = manager(
            [
                FakeResponse(
                    status_code=429,
                    text="rate limited",
                ),
                response([]),
            ],
            max_retries=1,
            sleeper=sleeps.append,
        )

        result = request_manager.search_comments(num_results=1)

        self.assertEqual(result, [])
        self.assertEqual(sleeps, [60.0])

    def test_retries_transport_errors_with_backoff(self):
        sleeps = []
        request_manager = manager(
            [
                requests.ConnectionError("offline"),
                response([]),
            ],
            max_retries=1,
            sleeper=sleeps.append,
        )

        result = request_manager.search_comments(num_results=1)

        self.assertEqual(result, [])
        self.assertEqual(sleeps, [1])

    def test_retries_server_errors_with_backoff(self):
        sleeps = []
        request_manager = manager(
            [
                FakeResponse(status_code=503, text="unavailable"),
                response([]),
            ],
            max_retries=1,
            sleeper=sleeps.append,
        )

        result = request_manager.search_comments(num_results=1)

        self.assertEqual(result, [])
        self.assertEqual(sleeps, [1])

    def test_rejects_malformed_or_error_responses(self):
        malformed_manager = manager([
            FakeResponse(json_error=ValueError("bad json")),
        ])
        error_manager = manager([
            response([], error="query failed"),
        ])

        with self.assertRaisesRegex(PullPushAPIError, "non-JSON"):
            malformed_manager.search_comments(num_results=1)
        with self.assertRaisesRegex(PullPushAPIError, "query failed"):
            error_manager.search_comments(num_results=1)


class SlidingWindowRateLimiterTests(unittest.TestCase):
    def test_enforces_thirty_requests_per_minute(self):
        clock = FakeClock()
        limiter = _SlidingWindowRateLimiter(
            ((60.0, 30),),
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )

        with redirect_stdout(StringIO()):
            for _ in range(31):
                limiter.acquire()

        self.assertEqual(clock.now, 60.0)
        self.assertEqual(sum(clock.sleeps), 60.0)

    def test_enforces_one_thousand_requests_per_hour_with_minute_limit(self):
        clock = FakeClock()
        limiter = _SlidingWindowRateLimiter(
            ((60.0, 30), (3600.0, 1000)),
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )

        with redirect_stdout(StringIO()):
            for _ in range(1001):
                limiter.acquire()

        self.assertEqual(clock.now, 3600.0)
        self.assertEqual(len(limiter._request_times[1]), 971)

    def test_persistent_limiter_shares_minute_history_across_instances(self):
        clock = FakeClock()
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "rate-limit.sqlite3"
            first_process = _PersistentSlidingWindowRateLimiter(
                ((60.0, 2), (3600.0, 1000)),
                state_path,
                clock=clock.monotonic,
                sleeper=clock.sleep,
            )
            restarted_process = _PersistentSlidingWindowRateLimiter(
                ((60.0, 2), (3600.0, 1000)),
                state_path,
                clock=clock.monotonic,
                sleeper=clock.sleep,
            )

            first_process.acquire()
            first_process.acquire()
            with redirect_stdout(StringIO()):
                restarted_process.acquire()

        self.assertEqual(clock.now, 60.0)
        self.assertEqual(sum(clock.sleeps), 60.0)

    def test_persistent_limiter_shares_hour_history_across_instances(self):
        clock = FakeClock()
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "rate-limit.sqlite3"
            first_process = _PersistentSlidingWindowRateLimiter(
                ((60.0, 30), (3600.0, 2)),
                state_path,
                clock=clock.monotonic,
                sleeper=clock.sleep,
            )
            restarted_process = _PersistentSlidingWindowRateLimiter(
                ((60.0, 30), (3600.0, 2)),
                state_path,
                clock=clock.monotonic,
                sleeper=clock.sleep,
            )

            first_process.acquire()
            first_process.acquire()
            with redirect_stdout(StringIO()):
                restarted_process.acquire()

        self.assertEqual(clock.now, 3600.0)
        self.assertEqual(sum(clock.sleeps), 3600.0)


class GaussianRequestPacerTests(unittest.TestCase):
    def test_spaces_requests_using_standard_normal_jitter(self):
        clock = FakeClock()
        samples = iter((1.0, -1.0))
        pacer = _GaussianRequestPacer(
            30.0,
            1.0,
            clock=clock.monotonic,
            sleeper=clock.sleep,
            standard_normal_sampler=lambda: next(samples),
        )

        pacer.wait()
        pacer.mark_request_started()
        pacer.wait()
        pacer.mark_request_started()
        pacer.wait()
        pacer.mark_request_started()

        self.assertEqual(clock.sleeps, [31.0, 29.0])
        self.assertEqual(clock.now, 60.0)

    def test_clamps_negative_sampled_interval_to_zero(self):
        clock = FakeClock()
        pacer = _GaussianRequestPacer(
            0.5,
            1.0,
            clock=clock.monotonic,
            sleeper=clock.sleep,
            standard_normal_sampler=lambda: -1.0,
        )

        pacer.mark_request_started()
        pacer.wait()

        self.assertEqual(clock.sleeps, [])
        self.assertEqual(clock.now, 0.0)


if __name__ == "__main__":
    unittest.main()
