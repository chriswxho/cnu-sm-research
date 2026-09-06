import csv
from datetime import date, datetime
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo


MODULE_PATH = Path(__file__).parents[1] / "data" / "dedup_posts.py"
SPEC = importlib.util.spec_from_file_location("dedup_posts", MODULE_PATH)
dedup_posts = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = dedup_posts
SPEC.loader.exec_module(dedup_posts)


class DeduplicatedPullPushCsvTests(unittest.TestCase):
    @staticmethod
    def _write_query(
        data_root: Path,
        slug: str,
        query_text: str,
        posts: list[dict[str, object]],
    ) -> None:
        query_directory = data_root / slug
        query_directory.mkdir()
        (query_directory / "manifest.json").write_text(
            json.dumps({"query_text": query_text}),
            encoding="utf-8",
        )
        (query_directory / "pullpush-filtered-posts.json").write_text(
            json.dumps(posts),
            encoding="utf-8",
        )

    def test_deduplicates_ids_and_aggregates_query_labels(self):
        shared_post = {
            "id": "ABC123",
            "subreddit": "CPTSD",
            "created_utc": 1704067200,
            "permalink": "/r/CPTSD/comments/abc123/example/",
            "score": 17,
            "title": "A shared result",
            "selftext": "Review body",
            "is_self": False,
            "url": "https://example.com/media",
        }
        second_post = {
            "name": "t3_def456",
            "subreddit": "relationships",
            "created_utc": 1609459200,
            "permalink": "/r/relationships/comments/def456/example/",
            "score": 3,
            "title": "Another result",
            "selftext": "",
            "is_self": True,
            "url": "https://www.reddit.com/r/relationships/",
        }

        with TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            self._write_query(
                data_root,
                "query-one",
                "query one",
                [shared_post, second_post],
            )
            self._write_query(
                data_root,
                "query-two",
                "query two",
                [{**shared_post, "id": "abc123"}],
            )

            rows, stats = dedup_posts.build_deduplicated_rows(data_root)
            output_path = data_root / "review.csv"
            dedup_posts.write_review_csv(rows, output_path)
            with output_path.open(encoding="utf-8", newline="") as csv_file:
                serialized_rows = list(csv.DictReader(csv_file))

        self.assertEqual(stats.query_directories, 2)
        self.assertEqual(stats.source_rows, 3)
        self.assertEqual(stats.unique_posts, 2)
        self.assertEqual(stats.duplicate_occurrences, 1)
        self.assertEqual(stats.multi_query_posts, 1)
        self.assertEqual(
            serialized_rows[0]["matching_queries"],
            "query one; query two",
        )
        self.assertEqual(
            serialized_rows[0]["post_link"],
            "https://www.reddit.com/r/CPTSD/comments/abc123/example/",
        )
        self.assertEqual(
            serialized_rows[0]["datetime_pst"],
            "2023-12-31 16:00:00 PST",
        )
        self.assertEqual(
            serialized_rows[0]["media_url"],
            "https://example.com/media",
        )
        self.assertEqual(serialized_rows[1]["media_url"], "")
        self.assertEqual(
            list(serialized_rows[0]),
            dedup_posts.CSV_COLUMNS,
        )

    def test_excludes_an_exact_query_label(self):
        with TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            post = {
                "id": "abc123",
                "subreddit": "CPTSD",
                "created_utc": 1704067200,
                "permalink": "/comments/abc123/",
                "score": 1,
                "title": "Example",
                "selftext": "",
                "is_self": True,
            }
            self._write_query(
                data_root,
                "included",
                "included query",
                [post],
            )
            self._write_query(
                data_root,
                "excluded",
                "excluded query",
                [{**post, "id": "def456"}],
            )

            rows, stats = dedup_posts.build_deduplicated_rows(
                data_root,
                exclude_queries=["EXCLUDED QUERY"],
            )

        self.assertEqual(stats.query_directories, 1)
        self.assertEqual(stats.unique_posts, 1)
        self.assertEqual(rows[0]["matching_queries"], "included query")

    def test_pacific_date_filter_is_inclusive(self):
        pacific = ZoneInfo("America/Los_Angeles")

        def row(year, month, day, hour=0):
            return {
                "_created_epoch": datetime(
                    year,
                    month,
                    day,
                    hour,
                    tzinfo=pacific,
                ).timestamp()
            }

        rows = [
            row(2005, 4, 30, 23),
            row(2005, 5, 1),
            row(2025, 4, 30, 23),
            row(2025, 5, 1),
        ]

        filtered = dedup_posts.filter_rows_by_pacific_date(
            rows,
            start_date=date(2005, 5, 1),
            end_date=date(2025, 4, 30),
        )

        self.assertEqual(filtered, rows[1:3])

    def test_pacific_date_filter_rejects_reversed_bounds(self):
        with self.assertRaisesRegex(
            ValueError,
            "start_date must be on or before end_date",
        ):
            dedup_posts.filter_rows_by_pacific_date(
                [],
                start_date=date(2025, 4, 30),
                end_date=date(2005, 5, 1),
            )


if __name__ == "__main__":
    unittest.main()
