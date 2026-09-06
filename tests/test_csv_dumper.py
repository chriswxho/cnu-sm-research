import unittest

from csv_dumper import (
    RedditResultType,
    contains_exact_phrase_ignoring_quotes,
    map_research_record,
)


class ResearchRecordMappingTests(unittest.TestCase):
    def test_exact_phrase_filter_keeps_quotes_only(self):
        phrase = "sibling abuse"

        self.assertTrue(
            contains_exact_phrase_ignoring_quotes(
                "Sibling abuse is often minimized.",
                phrase,
            )
        )
        self.assertTrue(
            contains_exact_phrase_ignoring_quotes(
                "Can a sibling 'abuse' another sibling?",
                phrase,
            )
        )
        self.assertTrue(
            contains_exact_phrase_ignoring_quotes(
                'Can a "sibling" “abuse” another sibling?',
                phrase,
            )
        )

    def test_exact_phrase_filter_rejects_other_separators_and_word_forms(self):
        phrase = "sibling abuse"

        self.assertFalse(
            contains_exact_phrase_ignoring_quotes(
                "There may be a connection between sibling, abuse, and trauma.",
                phrase,
            )
        )
        self.assertFalse(
            contains_exact_phrase_ignoring_quotes(
                "Sibling  abuse with two spaces.",
                phrase,
            )
        )
        self.assertFalse(
            contains_exact_phrase_ignoring_quotes(
                "An abuse sibling phrase in reverse.",
                phrase,
            )
        )
        self.assertFalse(
            contains_exact_phrase_ignoring_quotes(
                "A sibling abuser.",
                phrase,
            )
        )

    def test_post_title_is_mapped_from_reddit_api_record(self):
        mapped = map_research_record(
            {
                "subreddit": "CPTSD",
                "title": "Sibling abuse is not taken seriously",
                "permalink": "/r/CPTSD/comments/post/example/",
                "created_utc": 1700000000,
                "score": 42,
                "selftext": "Post body",
                "is_self": True,
            },
            RedditResultType.POST,
        )

        self.assertEqual(
            mapped["title"],
            "Sibling abuse is not taken seriously",
        )

    def test_comment_title_is_blank(self):
        mapped = map_research_record(
            {
                "subreddit": "CPTSD",
                "permalink": "/r/CPTSD/comments/post/_/comment/",
                "created_utc": 1700000000,
                "score": 3,
                "body": "Comment body",
            },
            RedditResultType.COMMENT,
        )

        self.assertEqual(mapped["title"], "")


if __name__ == "__main__":
    unittest.main()
