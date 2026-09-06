import csv
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


MODULE_PATH = (
    Path(__file__).parents[1] / "data" / "merge_agent_codebook.py"
)
SPEC = importlib.util.spec_from_file_location("merge_agent_codebook", MODULE_PATH)
merge_codebook = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = merge_codebook
SPEC.loader.exec_module(merge_codebook)


class AgentCodebookMergeTests(unittest.TestCase):
    def test_extracts_canonical_post_id(self):
        self.assertEqual(
            merge_codebook.post_id_from_link(
                "https://www.reddit.com/r/test/comments/AbC123/title/"
            ),
            "abc123",
        )

    def test_term_shard_requires_source_order_and_binary_labels(self):
        with TemporaryDirectory() as temp_dir:
            shard = Path(temp_dir) / "term.csv"
            with shard.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["post_id", "label"])
                writer.writerow(["first", "1"])
                writer.writerow(["second", "0"])

            self.assertEqual(
                merge_codebook.read_term_shard(
                    shard,
                    ["first", "second"],
                ),
                ["1", "0"],
            )

            with self.assertRaisesRegex(ValueError, "source order"):
                merge_codebook.read_term_shard(
                    shard,
                    ["second", "first"],
                )

            shard.write_text(
                "post_id,label\nfirst,yes\nsecond,0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "non-binary"):
                merge_codebook.read_term_shard(
                    shard,
                    ["first", "second"],
                )

    def test_merges_exact_term_headers_and_binary_cells(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            codebook = root / "codebook.csv"
            shards = root / "shards"
            output = root / "combined.csv"
            shards.mkdir()
            source.write_text(
                "post_link\n"
                "https://reddit.com/r/x/comments/first/title/\n"
                "https://reddit.com/r/x/comments/second/title/\n",
                encoding="utf-8",
            )
            codebook.write_text("term one\nterm, two\n", encoding="utf-8")
            # Rewrite with csv so the comma-containing term is one field.
            with codebook.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["term one"])
                writer.writerow(["term, two"])
            (shards / "term_00.csv").write_text(
                "post_id,label\nfirst,1\nsecond,0\n",
                encoding="utf-8",
            )
            (shards / "term_01.csv").write_text(
                "post_id,label\nfirst,0\nsecond,1\n",
                encoding="utf-8",
            )

            counts = merge_codebook.merge_agent_codebook(
                source_path=source,
                codebook_path=codebook,
                shard_dir=shards,
                output_path=output,
            )
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(counts, {"term one": 1, "term, two": 1})
        self.assertEqual(
            list(rows[0]),
            ["post_id", "term one", "term, two"],
        )
        self.assertEqual(rows[0], {
            "post_id": "first",
            "term one": "1",
            "term, two": "0",
        })
        self.assertEqual(rows[1], {
            "post_id": "second",
            "term one": "0",
            "term, two": "1",
        })


if __name__ == "__main__":
    unittest.main()
