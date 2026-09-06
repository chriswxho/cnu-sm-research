import csv
from collections import Counter
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


MODULE_PATH = (
    Path(__file__).parents[1] / "data" / "merge_semantic_reviews.py"
)
SPEC = importlib.util.spec_from_file_location(
    "merge_semantic_reviews",
    MODULE_PATH,
)
merge_reviews = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = merge_reviews
SPEC.loader.exec_module(merge_reviews)


class CurationOverlayTests(unittest.TestCase):
    def test_filter_metadata_reconciles_sequential_counts(self):
        rows = merge_reviews.build_filter_metadata(
            source_count=10,
            false_positive_count=3,
            semantic_uncertain_count=1,
            semantic_reason_counts=Counter({"WRONG_RELATION": 3}),
            curation_rule_counts=[
                ("REMOVED_BODY", 2),
                ("LANGUAGE_UNCERTAIN", 1),
            ],
        )

        self.assertEqual(
            [
                (
                    row["rule_id"],
                    row["records_before"],
                    row["records_matched"],
                    row["records_after"],
                )
                for row in rows
            ],
            [
                ("SEMANTIC_FALSE_POSITIVE", "10", "3", "7"),
                ("SEMANTIC_UNCERTAIN", "7", "1", "7"),
                ("REMOVED_BODY", "7", "2", "5"),
                ("LANGUAGE_UNCERTAIN", "5", "1", "5"),
            ],
        )
        self.assertTrue(
            all("Removal criteria" in row for row in rows)
        )

    def test_removed_body_matches_only_the_normalized_placeholder(self):
        self.assertTrue(merge_reviews.is_removed_body(" \n[ReMoVeD]\t"))
        self.assertFalse(merge_reviews.is_removed_body("[deleted]"))
        self.assertFalse(merge_reviews.is_removed_body("note: [removed]"))
        self.assertFalse(merge_reviews.is_removed_body(""))

    def test_blank_body_matches_only_whitespace_or_empty_text(self):
        self.assertTrue(merge_reviews.is_blank_body(""))
        self.assertTrue(merge_reviews.is_blank_body(" \n\t"))
        self.assertFalse(merge_reviews.is_blank_body("[deleted]"))

    def test_deleted_body_accepts_placeholder_and_link_wrapper_only(self):
        self.assertTrue(merge_reviews.is_deleted_body(" \n[DeLeTeD]\t"))
        self.assertTrue(
            merge_reviews.is_deleted_body(
                "[deleted]\n\n[View Poll](https://www.reddit.com/poll/example)"
            )
        )
        self.assertFalse(
            merge_reviews.is_deleted_body("Context before [deleted]")
        )
        self.assertFalse(
            merge_reviews.is_deleted_body("[deleted]\nNarrative remains here.")
        )

    def test_timestamped_dataset_path_uses_pacific_dts_format(self):
        moment = datetime(2026, 9, 6, 4, 6, 1, tzinfo=timezone.utc)
        self.assertEqual(
            merge_reviews.timestamped_dataset_path(moment),
            merge_reviews.DATA_DIR
            / "2026-09-05_21-06-01"
            / "dedup_posts.csv",
        )

    def test_link_only_body_accepts_single_plain_or_markdown_link(self):
        self.assertTrue(
            merge_reviews.is_link_only_body("https://example.com/post")
        )
        self.assertTrue(
            merge_reviews.is_link_only_body("<https://example.com/post>")
        )
        self.assertTrue(
            merge_reviews.is_link_only_body(
                "[Demonstration](https://example.com/post)"
            )
        )

    def test_query_only_withholding_preserves_other_memberships(self):
        self.assertTrue(
            merge_reviews.is_withheld_query_only("aggressive brother")
        )
        self.assertTrue(
            merge_reviews.is_withheld_query_only(
                " Aggressive Sister ; sibling aggression "
            )
        )
        self.assertFalse(
            merge_reviews.is_withheld_query_only(
                "aggressive brother; violent brother"
            )
        )
        self.assertFalse(
            merge_reviews.is_withheld_query_only("sibling abuse")
        )
        self.assertFalse(merge_reviews.is_withheld_query_only(""))
        self.assertFalse(
            merge_reviews.is_link_only_body(
                "Read this: https://example.com/post"
            )
        )
        self.assertFalse(
            merge_reviews.is_link_only_body(
                "[One](https://example.com/1) "
                "[Two](https://example.com/2)"
            )
        )
        self.assertFalse(
            merge_reviews.is_link_only_body(
                "[removed] [View Poll](https://example.com/poll)"
            )
        )

    def test_curation_decision_preserves_semantic_false_positives(self):
        decision = merge_reviews.curation_decision(
            {"body": "[removed]"},
            "FALSE_POSITIVE",
            None,
        )

        self.assertEqual(decision["curation_status"], "NOT_APPLICABLE")
        self.assertEqual(decision["curation_exclusion_reason"], "")

    def test_curation_decision_excludes_placeholders_and_non_english(self):
        removed = merge_reviews.curation_decision(
            {"body": " [removed] "},
            "INCLUDE",
            None,
        )
        deleted = merge_reviews.curation_decision(
            {
                "body": (
                    "[deleted]\n\n"
                    "[View Poll](https://www.reddit.com/poll/example)"
                )
            },
            "INCLUDE",
            None,
        )
        non_english = merge_reviews.curation_decision(
            {"body": "Mi hermano me golpeaba."},
            "UNCERTAIN",
            {
                "language_classification": "NON_ENGLISH",
                "detected_language": "Spanish",
                "classification_rationale": (
                    "The submission is primarily in Spanish."
                ),
            },
        )

        self.assertEqual(removed["curation_status"], "EXCLUDE")
        self.assertEqual(
            removed["curation_exclusion_reason"],
            "REMOVED_BODY",
        )
        self.assertEqual(deleted["curation_status"], "EXCLUDE")
        self.assertEqual(
            deleted["curation_exclusion_reason"],
            "DELETED_BODY",
        )
        self.assertEqual(non_english["curation_status"], "EXCLUDE")
        self.assertEqual(
            non_english["curation_exclusion_reason"],
            "NON_ENGLISH",
        )
        self.assertEqual(non_english["curation_language"], "Spanish")

    def test_curation_decision_excludes_blank_and_link_only_bodies(self):
        blank = merge_reviews.curation_decision(
            {"body": " \n", "matching_queries": "sibling abuse"},
            "INCLUDE",
            None,
        )
        link_only = merge_reviews.curation_decision(
            {
                "body": "[Video](https://example.com/video)",
                "matching_queries": "sibling abuse",
            },
            "UNCERTAIN",
            None,
        )

        self.assertEqual(blank["curation_status"], "EXCLUDE")
        self.assertEqual(
            blank["curation_exclusion_reason"],
            "BLANK_BODY",
        )
        self.assertEqual(link_only["curation_status"], "EXCLUDE")
        self.assertEqual(
            link_only["curation_exclusion_reason"],
            "LINK_ONLY_BODY",
        )

    def test_curation_decision_withholds_aggression_only_membership(self):
        withheld = merge_reviews.curation_decision(
            {
                "body": "A substantive English-language account.",
                "matching_queries": "aggressive sibling; aggressive sister",
            },
            "INCLUDE",
            None,
        )
        retained = merge_reviews.curation_decision(
            {
                "body": "A substantive English-language account.",
                "matching_queries": "aggressive sibling; sibling abuse",
            },
            "INCLUDE",
            None,
        )

        self.assertEqual(withheld["curation_status"], "EXCLUDE")
        self.assertEqual(
            withheld["curation_exclusion_reason"],
            "AGGRESSION_QUERY_ONLY",
        )
        self.assertEqual(retained["curation_status"], "RETAIN")

    def test_uncertain_language_is_flagged_without_exclusion(self):
        decision = merge_reviews.curation_decision(
            {"body": "A substantially mixed-language account."},
            "INCLUDE",
            {
                "language_classification": "UNCERTAIN",
                "detected_language": "Mixed",
                "classification_rationale": "No language clearly dominates.",
            },
        )

        self.assertEqual(decision["curation_status"], "REVIEW")
        self.assertEqual(
            decision["curation_exclusion_reason"],
            "LANGUAGE_UNCERTAIN",
        )

    def test_language_shard_validates_row_link_and_eligibility(self):
        source_rows = [
            {
                "post_link": "https://www.reddit.com/comments/example/",
                "body": "Texto principalmente en español.",
            }
        ]
        semantic_decisions = {
            0: {
                "classification": "INCLUDE",
                "exclusion_reason": "",
                "classification_rationale": "In scope.",
            }
        }
        with TemporaryDirectory() as temp_dir:
            shard = Path(temp_dir) / "language.csv"
            with shard.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "row_index",
                        "post_link",
                        "language_classification",
                        "detected_language",
                        "classification_rationale",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "row_index": "0",
                        "post_link": source_rows[0]["post_link"],
                        "language_classification": "NON_ENGLISH",
                        "detected_language": "Spanish",
                        "classification_rationale": (
                            "The meaningful text is primarily Spanish."
                        ),
                    }
                )

            decisions = merge_reviews.read_language_decisions(
                source_rows,
                semantic_decisions,
                [shard],
            )

        self.assertEqual(
            decisions[0]["language_classification"],
            "NON_ENGLISH",
        )


if __name__ == "__main__":
    unittest.main()
