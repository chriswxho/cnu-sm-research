import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

import requests

from fetch_posts import (
    DEFAULT_DATA_ROOT,
    DatasetPaths,
    PULLPUSH_ARCHIVE_CUTOFF_EXCLUSIVE,
    _effective_pullpush_cutoff,
    _five_year_epoch_windows,
    _run_adaptive_field_search,
    _uncovered_intervals,
    slugify_query_text,
)

PACIFIC = ZoneInfo("America/Los_Angeles")
QUERY_TEXT = "sibling abuse"
PULLPUSH_QUERY = '"sibling abuse"'
START = datetime(2005, 1, 1, tzinfo=PACIFIC)
CUTOFF = datetime(2026, 7, 25, 20, 0, tzinfo=PACIFIC)


class OfflineRebuildTests(unittest.TestCase):
    def test_default_data_root_is_raw_data(self):
        self.assertEqual(
            DEFAULT_DATA_ROOT,
            Path(__file__).resolve().parents[1] / "data" / "raw_data",
        )

    def test_query_slug_selects_hyphenated_dataset_directory(self):
        with TemporaryDirectory() as temp_dir:
            paths = DatasetPaths.for_query(
                "  Sibling, Abuse!  ",
                Path(temp_dir),
            )

        self.assertEqual(paths.data_dir.name, "sibling-abuse")
        self.assertEqual(
            slugify_query_text("Sibling   Abuse"),
            "sibling-abuse",
        )

    def test_query_slug_rejects_empty_or_punctuation_only_text(self):
        with self.assertRaisesRegex(ValueError, "letter or digit"):
            slugify_query_text(" ' \" ")

    def test_pullpush_cutoff_is_capped_after_last_archive_date(self):
        self.assertEqual(
            _effective_pullpush_cutoff(CUTOFF),
            PULLPUSH_ARCHIVE_CUTOFF_EXCLUSIVE,
        )
        earlier = datetime(2024, 1, 1, tzinfo=PACIFIC)
        self.assertEqual(_effective_pullpush_cutoff(earlier), earlier)

    def test_pullpush_uses_five_year_root_windows(self):
        roots = _five_year_epoch_windows(
            START,
            PULLPUSH_ARCHIVE_CUTOFF_EXCLUSIVE,
        )

        self.assertEqual(len(roots), 5)
        self.assertEqual(
            roots[0],
            (
                int(START.timestamp()),
                int(datetime(2010, 1, 1, tzinfo=PACIFIC).timestamp()),
            ),
        )
        self.assertEqual(
            roots[-1],
            (
                int(datetime(2025, 1, 1, tzinfo=PACIFIC).timestamp()),
                int(PULLPUSH_ARCHIVE_CUTOFF_EXCLUSIVE.timestamp()),
            ),
        )

    def test_resume_subtracts_existing_leaf_coverage(self):
        root_start = int(
            datetime(2010, 1, 1, tzinfo=PACIFIC).timestamp()
        )
        gap_start = int(
            datetime(2012, 1, 1, tzinfo=PACIFIC).timestamp()
        )
        gap_end = int(
            datetime(2013, 1, 1, tzinfo=PACIFIC).timestamp()
        )
        second_gap_start = int(
            datetime(2014, 1, 1, tzinfo=PACIFIC).timestamp()
        )
        root_end = int(
            datetime(2015, 1, 1, tzinfo=PACIFIC).timestamp()
        )
        audit_windows = [
            {
                "start_utc_inclusive": root_start,
                "end_utc_exclusive": gap_start,
                "leaf": True,
            },
            {
                "start_utc_inclusive": gap_end,
                "end_utc_exclusive": second_gap_start,
                "leaf": True,
            },
        ]

        self.assertEqual(
            _uncovered_intervals(
                audit_windows,
                root_start,
                root_end,
            ),
            [
                (gap_start, gap_end),
                (second_gap_start, root_end),
            ],
        )

    def test_offline_search_rejects_missing_checkpoint_interval(self):
        with TemporaryDirectory() as temp_dir:
            checkpoint = Path(temp_dir) / "missing.json"

            with self.assertRaisesRegex(
                RuntimeError,
                "Offline rebuild cannot fill missing title interval",
            ):
                _run_adaptive_field_search(
                    None,
                    field="title",
                    output_path=checkpoint,
                    pullpush_query=PULLPUSH_QUERY,
                    start=START,
                    cutoff=CUTOFF,
                    root_attempts=1,
                    retry_delay_sec=0,
                    retry_jitter_sec=0,
                )

    def test_offline_search_reuses_complete_checkpoint(self):
        with TemporaryDirectory() as temp_dir:
            checkpoint = Path(temp_dir) / "complete.json"
            audit_windows = [
                {
                    "start_utc_inclusive": int(START.timestamp()),
                    "end_utc_exclusive": int(CUTOFF.timestamp()),
                    "depth": 0,
                    "result_count": 0,
                    "max_results_per_window": 100,
                    "saturated": False,
                    "leaf": True,
                    "response": {
                        "data": [],
                        "metadata": {
                            "selftext": PULLPUSH_QUERY,
                        },
                        "error": None,
                    },
                }
            ]
            checkpoint.write_text(
                json.dumps(audit_windows),
                encoding="utf-8",
            )

            windows, submissions = _run_adaptive_field_search(
                None,
                field="selftext",
                output_path=checkpoint,
                pullpush_query=PULLPUSH_QUERY,
                start=START,
                cutoff=CUTOFF,
                root_attempts=1,
                retry_delay_sec=0,
                retry_jitter_sec=0,
            )

        self.assertEqual(windows, audit_windows)
        self.assertEqual(submissions, [])

    def test_failed_adaptive_root_retries_only_uncovered_suffix(self):
        start = datetime(2005, 1, 1, tzinfo=PACIFIC)
        cutoff = datetime(2006, 1, 1, tzinfo=PACIFIC)
        root_start = int(start.timestamp())
        root_end = int(cutoff.timestamp())
        midpoint = root_start + (root_end - root_start) // 2

        def record(record_id, created_utc):
            return {
                "id": record_id,
                "created_utc": created_utc,
                "subreddit": "CPTSD",
            }

        left_record = record("left", root_start + 1)
        right_record = record("right", midpoint + 1)
        internal_window = {
            "start_utc_inclusive": root_start,
            "end_utc_exclusive": root_end,
            "depth": 0,
            "result_count": 100,
            "max_results_per_window": 100,
            "saturated": True,
            "leaf": False,
            "response": {"data": [], "metadata": {}, "error": None},
        }
        left_window = {
            "start_utc_inclusive": root_start,
            "end_utc_exclusive": midpoint,
            "depth": 1,
            "result_count": 1,
            "max_results_per_window": 100,
            "saturated": False,
            "leaf": True,
            "response": {
                "data": [left_record],
                "metadata": {},
                "error": None,
            },
        }
        right_window = {
            "start_utc_inclusive": midpoint,
            "end_utc_exclusive": root_end,
            "depth": 0,
            "result_count": 1,
            "max_results_per_window": 100,
            "saturated": False,
            "leaf": True,
            "response": {
                "data": [right_record],
                "metadata": {},
                "error": None,
            },
        }

        class PartialThenSuccessfulManager:
            def __init__(self):
                self.calls = []

            def search_submissions_adaptive(
                self,
                *,
                start_utc,
                end_utc,
                checkpoint_callback,
                **_kwargs,
            ):
                self.calls.append((start_utc, end_utc))
                if len(self.calls) == 1:
                    checkpoint_callback([internal_window, left_window])
                    raise requests.ConnectionError("late failure")
                checkpoint_callback([right_window])
                return [right_window], [right_record]

        request_manager = PartialThenSuccessfulManager()
        with TemporaryDirectory() as temp_dir:
            checkpoint = Path(temp_dir) / "partial.json"
            windows, submissions = _run_adaptive_field_search(
                request_manager,
                field="title",
                output_path=checkpoint,
                pullpush_query=PULLPUSH_QUERY,
                start=start,
                cutoff=cutoff,
                root_attempts=2,
                retry_delay_sec=0,
                retry_jitter_sec=0,
            )
            serialized_windows = json.loads(
                checkpoint.read_text(encoding="utf-8")
            )

        self.assertEqual(
            request_manager.calls,
            [(root_start, root_end), (midpoint, root_end)],
        )
        self.assertEqual(windows, serialized_windows)
        self.assertEqual(
            {submission["id"] for submission in submissions},
            {"left", "right"},
        )


if __name__ == "__main__":
    unittest.main()
