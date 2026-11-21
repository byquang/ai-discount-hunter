import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import requests

from promo_hunter.sources import HN_QUERIES, HN_URL, SUBREDDITS, discover, fetch_web, hn_posts, web_posts

SINCE = datetime(2026, 11, 19, tzinfo=timezone.utc)
HN_RESPONSE = {
    "hits": [
        {
            "objectID": "42", "title": "Free credits",
            "created_at": "2026-11-20T10:00:00.000Z",
            "story_text": '<p>Free &amp; available</p><a href="https://vendor.example/new">Link</a>',
        },
        {"objectID": "99", "title": "Old free credits", "created_at": "2026-11-18T10:00:00.000Z"},
    ],
}
REDDIT_URL = "https://www.reddit.com/r/ClaudeAI/comments/abc/free_credits"


class SourceTests(unittest.TestCase):
    def test_hacker_news_falls_back_to_discussion_and_preserves_link_evidence(self):
        post = hn_posts(HN_RESPONSE)[0]
        self.assertEqual(post.keys, ("hn:42", "https://news.ycombinator.com/item?id=42"))
        self.assertEqual(post.text, "Free & available Link")
        self.assertEqual(post.links, ("https://vendor.example/new",))

    def test_discover_without_optional_keys_only_calls_hacker_news_and_filters_stale_items(self):
        session = Mock()
        session.get.return_value.json.return_value = HN_RESPONSE
        self.assertTrue(all(post.source_id == "hn:42" for post in discover(session, SINCE)))
        self.assertEqual(session.get.call_count, len(HN_QUERIES))
        for call in session.get.call_args_list:
            self.assertEqual(call.args, (HN_URL,))
            self.assertEqual(call.kwargs["params"]["numericFilters"], f"created_at_i>{int(SINCE.timestamp())}")
        session.post.assert_not_called()

    def test_optional_web_failure_does_not_block_hacker_news(self):
        session = Mock()
        session.get.return_value.json.return_value = HN_RESPONSE
        session.post.side_effect = requests.Timeout()
        with self.assertLogs("promo_hunter.sources", level="WARNING"):
            candidates = discover(session, SINCE, tavily_key="fake")
        self.assertTrue(candidates)
        self.assertEqual(session.post.call_count, 2)

    def test_all_sources_failing_is_an_error(self):
        session = Mock()
        session.get.side_effect = requests.Timeout()
        with self.assertLogs("promo_hunter.sources", level="WARNING"), self.assertRaises(RuntimeError):
            discover(session, SINCE)

    def test_search_masks_results_outside_requested_sites(self):
        results = [
            {"url": REDDIT_URL, "title": "Free credits"},
            {"url": "https://reddit.com/r/Other/comments/abc/free_credits", "title": "Free credits"},
            {"url": "https://notreddit.com/r/ClaudeAI/comments/abc", "title": "Free credits"},
            {"url": "https://x.com/user/status/123", "title": "Free credits"},
            {"url": "https://x.com.example.org/user/status/123", "title": "Free credits"},
        ]
        self.assertEqual([post.source_id for post in web_posts(results, reddit=True)], ["reddit:abc"])
        self.assertEqual([post.url for post in web_posts(results)], ["https://x.com/user/status/123"])

    def test_x_uses_post_id_time_not_index_date_and_skips_non_post_links(self):
        result = {
            "title": "A 24-hour offer",
            "url": "https://x.com/someone/status/2123392055573385525",
            "published_date": "2026-11-21T12:00:00Z",
            "content": '<a href="https://vendor.example/deal">Claim</a>',
        }
        posts = web_posts([result, dict(result, url="https://x.com/someone")])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].published, datetime(2026, 11, 19, 12, 24, 51, 422000, tzinfo=timezone.utc))
        self.assertEqual(posts[0].links, ("https://vendor.example/deal",))

    def test_tavily_reddit_query_includes_all_requested_subreddits(self):
        session = Mock()
        session.post.return_value.json.return_value = {"results": [{"url": REDDIT_URL, "title": "Offer"}]}
        posts = fetch_web(session, "tavily-secret", "brave-secret", SINCE, reddit=True)
        self.assertEqual([post.url for post in posts], [REDDIT_URL])
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["search_depth"], "basic")
        for subreddit in SUBREDDITS:
            self.assertIn(f"site:reddit.com/r/{subreddit}/comments", payload["query"])
        session.get.assert_not_called()

    def test_brave_with_no_tavily_key_searches_x_and_threads(self):
        session = Mock()
        session.get.return_value.json.return_value = {"web": {"results": []}}
        self.assertEqual(fetch_web(session, "", "brave-secret", SINCE, reddit=False), [])
        params = session.get.call_args.kwargs["params"]
        self.assertIn("site:x.com", params["q"])
        self.assertIn("site:threads.net", params["q"])
        self.assertTrue(params["freshness"].startswith("2026-11-19to"))
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
