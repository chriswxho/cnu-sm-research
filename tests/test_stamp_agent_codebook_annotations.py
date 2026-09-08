import csv
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "data"
    / "stamp_agent_codebook_annotations.py"
)
SPEC = importlib.util.spec_from_file_location(
    "stamp_agent_codebook_annotations",
    MODULE_PATH,
)
stamp_annotations = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = stamp_annotations
SPEC.loader.exec_module(stamp_annotations)


class StampedAgentCodebookTests(unittest.TestCase):
    def test_annotation_path_reuses_post_dataset_timestamp(self):
        posts = Path("data/2026-09-05_21-06-01/dedup_posts.csv")
        self.assertEqual(
            stamp_annotations.annotation_path_for_posts(posts),
            Path(
                "data/2026-09-05_21-06-01/agent_codebook_annotations.csv"
            ),
        )
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD_HH-MM-SS"):
            stamp_annotations.annotation_path_for_posts(
                Path("data/latest/dedup_posts.csv")
            )

        nested_posts = Path(
            "data/2026-09-05_21-06-01/agent_codebook/dedup_posts.csv"
        )
        self.assertEqual(
            stamp_annotations.annotation_path_for_posts(nested_posts),
            nested_posts.with_name("agent_codebook_annotations.csv"),
        )

    def test_writes_binary_annotations_in_post_dataset_order(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            posts = root / "2026-09-05_21-06-01" / "dedup_posts.csv"
            posts.parent.mkdir()
            codebook = root / "codebook.csv"
            annotations = root / "annotations.csv"
            posts.write_text(
                "post_link\n"
                "https://reddit.com/r/x/comments/second/title/\n"
                "https://reddit.com/r/x/comments/first/title/\n",
                encoding="utf-8",
            )
            codebook.write_text("term one\n", encoding="utf-8")
            annotations.write_text(
                "post_id,term one\n"
                "first,1\n"
                "second,0\n"
                "extra,1\n",
                encoding="utf-8",
            )

            output, count = stamp_annotations.write_paired_annotations(
                posts,
                annotations_path=annotations,
                codebook_path=codebook,
            )
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(count, 2)
        self.assertEqual(
            rows,
            [
                {"post_id": "second", "term one": "0"},
                {"post_id": "first", "term one": "1"},
            ],
        )

    def test_rejects_missing_and_nonbinary_annotations(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            posts = root / "2026-09-05_21-06-01" / "dedup_posts.csv"
            posts.parent.mkdir()
            codebook = root / "codebook.csv"
            annotations = root / "annotations.csv"
            posts.write_text(
                "post_link\n"
                "https://reddit.com/r/x/comments/first/title/\n",
                encoding="utf-8",
            )
            codebook.write_text("term one\n", encoding="utf-8")
            annotations.write_text(
                "post_id,term one\nsecond,yes\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "non-binary"):
                stamp_annotations.write_paired_annotations(
                    posts,
                    annotations_path=annotations,
                    codebook_path=codebook,
                )
            annotations.write_text(
                "post_id,term one\nsecond,0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "missing 1"):
                stamp_annotations.write_paired_annotations(
                    posts,
                    annotations_path=annotations,
                    codebook_path=codebook,
                )


if __name__ == "__main__":
    unittest.main()
