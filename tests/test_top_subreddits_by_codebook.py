import importlib.util
import csv
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


MODULE_PATH = (
    Path(__file__).parents[1] / "data" / "top_subreddits_by_codebook.py"
)
SPEC = importlib.util.spec_from_file_location(
    "top_subreddits_by_codebook",
    MODULE_PATH,
)
top_subreddits = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = top_subreddits
SPEC.loader.exec_module(top_subreddits)


class TopSubredditsByCodebookTests(unittest.TestCase):
    def test_paired_paths_reuse_post_dataset_timestamp(self):
        annotations, summary = top_subreddits.paired_paths(
            Path("data/2026-09-05_21-06-01/dedup_posts.csv")
        )
        self.assertEqual(
            annotations.name,
            "agent_codebook_annotations.csv",
        )
        self.assertEqual(
            summary.name,
            "agent_codebook_top_subreddits.csv",
        )
        self.assertEqual(annotations.parent, summary.parent)

        nested_posts = Path(
            "data/2026-09-05_21-06-01/agent_codebook/dedup_posts.csv"
        )
        nested_annotations, nested_summary = top_subreddits.paired_paths(
            nested_posts
        )
        self.assertEqual(nested_annotations.parent, nested_posts.parent)
        self.assertEqual(nested_summary.parent, nested_posts.parent)

    def test_keeps_every_tie_at_third_place(self):
        sources = (
            ["Alpha"] * 5
            + ["Beta"] * 4
            + ["Gamma"] * 3
            + ["Delta"] * 3
            + ["Epsilon"] * 2
        )
        posts = [{"subreddit": source} for source in sources]
        annotations = [{"term": "1"} for _ in sources]

        results = top_subreddits.summarize_top_subreddits(
            posts,
            annotations,
            ["term"],
        )

        self.assertEqual(
            results,
            [
                {
                    "codebook_category": "term",
                    "rank": 1,
                    "subreddit": "Alpha",
                    "post_count": 5,
                },
                {
                    "codebook_category": "term",
                    "rank": 2,
                    "subreddit": "Beta",
                    "post_count": 4,
                },
                {
                    "codebook_category": "term",
                    "rank": 3,
                    "subreddit": "Delta",
                    "post_count": 3,
                },
                {
                    "codebook_category": "term",
                    "rank": 3,
                    "subreddit": "Gamma",
                    "post_count": 3,
                },
            ],
        )

    def test_aggregates_subreddit_names_case_insensitively(self):
        posts = [
            {"subreddit": "CPTSD"},
            {"subreddit": "cptsd"},
            {"subreddit": "Abuse"},
            {"subreddit": "Support"},
        ]
        annotations = [{"term": "1"} for _ in posts]

        results = top_subreddits.summarize_top_subreddits(
            posts,
            annotations,
            ["term"],
        )

        self.assertEqual(results[0]["subreddit"], "CPTSD")
        self.assertEqual(results[0]["post_count"], 2)
        self.assertEqual(len(results), 3)

    def test_keeps_all_sources_when_fewer_than_three_are_positive(self):
        posts = [
            {"subreddit": "Alpha"},
            {"subreddit": "Alpha"},
            {"subreddit": "Beta"},
        ]
        annotations = [{"term": "1"} for _ in posts]

        results = top_subreddits.summarize_top_subreddits(
            posts,
            annotations,
            ["term"],
        )

        self.assertEqual(
            [(row["subreddit"], row["post_count"]) for row in results],
            [("Alpha", 2), ("Beta", 1)],
        )

    def test_reads_annotations_as_an_ordered_subset_of_posts(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            posts = root / "posts.csv"
            annotations = root / "annotations.csv"
            with posts.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["post_link", "subreddit"],
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {
                            "post_link": "https://reddit.com/comments/a/",
                            "subreddit": "Alpha",
                        },
                        {
                            "post_link": "https://reddit.com/comments/b/",
                            "subreddit": "Beta",
                        },
                        {
                            "post_link": "https://reddit.com/comments/c/",
                            "subreddit": "Gamma",
                        },
                    ]
                )
            with annotations.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["post_id", "term"],
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {"post_id": "a", "term": "1"},
                        {"post_id": "c", "term": "0"},
                    ]
                )

            paired_posts, paired_annotations = (
                top_subreddits.read_paired_rows(
                    posts,
                    annotations,
                    ["term"],
                )
            )

        self.assertEqual(
            [row["subreddit"] for row in paired_posts],
            ["Alpha", "Gamma"],
        )
        self.assertEqual(
            [row["post_id"] for row in paired_annotations],
            ["a", "c"],
        )


if __name__ == "__main__":
    unittest.main()
