import unittest
from urllib.parse import parse_qs, urlparse

from api.reddit import SortBy, build_search_endpoint_query


class RedditSearchQueryTests(unittest.TestCase):
    def test_search_query_encodes_exact_phrase_and_uses_documented_sort(self):
        query = build_search_endpoint_query(
            subreddit_name="all",
            query_term='title:"sibling abuse" OR selftext:"sibling abuse"',
            sort_by=SortBy.NEW,
            after_fullname="t3_example",
            count=100,
        )
        parsed = urlparse(query)
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.path, "/r/all/search")
        self.assertEqual(
            params["q"],
            ['title:"sibling abuse" OR selftext:"sibling abuse"'],
        )
        self.assertEqual(params["sort"], ["new"])
        self.assertNotIn("sort_by", params)
        self.assertEqual(params["limit"], ["100"])
        self.assertEqual(params["restrict_sr"], ["true"])
        self.assertEqual(params["show"], ["all"])
        self.assertEqual(params["t"], ["all"])
        self.assertEqual(params["type"], ["link"])
        self.assertEqual(params["raw_json"], ["1"])
        self.assertEqual(params["after"], ["t3_example"])
        self.assertEqual(params["count"], ["100"])


if __name__ == "__main__":
    unittest.main()
