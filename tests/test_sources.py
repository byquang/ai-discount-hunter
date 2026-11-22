import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from promo_hunter.sources import (
    HN_QUERIES, HN_URL, OFFICIAL_FEEDS, SUBREDDITS, discover, fetch_web, hn_posts, official_posts,
    scheduled_web_scopes, web_posts,
)

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=2)
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
RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>Free credits for one week</title><link>https://vendor.example/blog/credits</link>
<pubDate>Tue, 17 Nov 2026 08:00:00 -0800</pubDate>
<description><![CDATA[Claim by November 24 at <a href="https://vendor.example/claim">the offer page</a>.]]></description></item>
<item><title>Old promo</title><link>https://vendor.example/old</link><pubDate>Sun, 01 Nov 2026 00:00:00 GMT</pubDate></item>
</channel></rss>"""


class SourceTests(unittest.TestCase):
    def test_hacker_news_falls_back_to_discussion_and_preserves_link_evidence(self):
        post = hn_posts(HN_RESPONSE)[0]
        self.assertEqual(post.keys, ("hn:42", "https://news.ycombinator.com/item?id=42"))
        self.assertEqual(post.text, "Free & available Link")
        self.assertEqual(post.links, ("https://vendor.example/new",))

    def test_keyless_discovery_checks_official_feeds_and_keeps_older_announcements(self):
        session = Mock()
        rss = requests.Response()
        rss.status_code = 200
        rss._content = RSS
        rss._content_consumed = True
        hn = Mock()
        hn.json.return_value = HN_RESPONSE
        session.get.side_effect = lambda url, **kwargs: hn if url == HN_URL else rss
        result = discover(session, NOW)
        self.assertFalse(result.partial)
        posts = result.candidates
        official = [post for post in posts if post.source_id.startswith("official:")]
        self.assertEqual(len(official), len(OFFICIAL_FEEDS))
        self.assertEqual(official[0].published, datetime(2026, 11, 17, 16, tzinfo=timezone.utc))
        self.assertEqual(official[0].links, ("https://vendor.example/claim",))
        self.assertEqual([post.source_id for post in posts if post.source_id.startswith("hn:")], ["hn:42"] * 5)
        self.assertEqual(session.get.call_count, len(OFFICIAL_FEEDS) + len(HN_QUERIES))
        for call in session.get.call_args_list[len(OFFICIAL_FEEDS):]:
            self.assertEqual(call.args, (HN_URL,))
            self.assertEqual(call.kwargs["params"]["numericFilters"], f"created_at_i>{int(SINCE.timestamp())}")
        session.post.assert_not_called()

    def test_optional_web_failure_does_not_block_hacker_news(self):
        session = Mock()
        session.get.return_value.json.return_value = HN_RESPONSE
        session.post.side_effect = requests.Timeout()
        with self.assertLogs("promo_hunter.sources", level="WARNING"):
            result = discover(session, NOW, tavily_key="fake", include_feeds=False)
        self.assertTrue(result.candidates)
        self.assertTrue(result.partial)
        self.assertEqual(session.post.call_count, 3)

    def test_all_sources_failing_is_an_error(self):
        session = Mock()
        session.get.side_effect = requests.Timeout()
        with self.assertLogs("promo_hunter.sources", level="WARNING"), self.assertRaises(RuntimeError):
            discover(session, NOW)

    def test_search_masks_results_outside_requested_sites(self):
        results = [
            {"url": REDDIT_URL, "title": "Free credits"},
            {"url": "https://reddit.com/r/Other/comments/abc/free_credits", "title": "Free credits"},
            {"url": "https://notreddit.com/r/ClaudeAI/comments/abc", "title": "Free credits"},
            {"url": "https://x.com/user/status/123", "title": "Free credits"},
            {"url": "https://x.com.example.org/user/status/123", "title": "Free credits"},
        ]
        self.assertEqual([post.source_id for post in web_posts(results, "reddit")], ["reddit:abc"])
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
        posts = fetch_web(session, "tavily-secret", "brave-secret", SINCE, "reddit")
        self.assertEqual([post.url for post in posts], [REDDIT_URL])
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["search_depth"], "basic")
        for subreddit in SUBREDDITS:
            self.assertIn(f"site:reddit.com/r/{subreddit}/comments", payload["query"])
        session.get.assert_not_called()

    def test_brave_with_no_tavily_key_searches_x_and_threads(self):
        session = Mock()
        session.get.return_value.json.return_value = {"web": {"results": []}}
        self.assertEqual(fetch_web(session, "", "brave-secret", SINCE, "social"), [])
        params = session.get.call_args.kwargs["params"]
        self.assertIn("site:x.com", params["q"])
        self.assertIn("site:threads.net", params["q"])
        self.assertTrue(params["freshness"].startswith("2026-11-19to"))
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_open_web_surfaces_older_vendor_offers_outside_the_community_window(self):
        session = Mock()
        session.get.return_value.json.return_value = {"hits": []}
        url = "https://poke.com/bots"
        other = "https://new-vendor.example/launch"
        session.post.return_value.json.return_value = {"results": [
            {"url": url, "title": "Pitch your idea, get a free Devin Bot", "published_date": "2026-11-17",
             "content": "For a limited time, convince Poke how many free credits you deserve."},
            {"url": other, "title": "Free month for coding agents", "published_date": "2026-11-20"},
            {"url": "https://x.com/user/status/123", "title": "Free credits"},
            {"url": "https://vendor.example/old", "title": "Expired giveaway", "published_date": "2026-11-01"},
        ]}
        posts = discover(session, NOW, tavily_key="fake", web_targets=("web",), include_feeds=False).candidates
        self.assertEqual([post.url for post in posts], [url, other])
        self.assertTrue(all(post.source_id.startswith("open-web:") for post in posts))
        payload = session.post.call_args.kwargs["json"]
        self.assertNotIn("include_domains", payload)
        self.assertNotIn("poke.com", payload["query"])
        self.assertEqual(payload["start_date"], "2026-11-07")

    def test_rss_failure_does_not_break_community_discovery(self):
        session = Mock()
        session.get.return_value.json.return_value = HN_RESPONSE
        with patch("promo_hunter.sources.fetch_official", side_effect=ValueError("Invalid XML")):
            with self.assertLogs("promo_hunter.sources", level="WARNING"):
                result = discover(session, NOW)
                self.assertTrue(result.candidates)
                self.assertTrue(result.partial)

    def test_rss_allows_html_doctype_in_article_content_but_not_the_feed(self):
        html_article = RSS.replace(b"<![CDATA[", b"<![CDATA[<!DOCTYPE html>", 1)
        self.assertEqual(len(official_posts(html_article, NOW - timedelta(days=14), NOW)), 1)
        with self.assertRaisesRegex(ValueError, "Unexpected document type"):
            official_posts(b"<!DOCTYPE rss><rss><channel/></rss>", SINCE, NOW)

    def test_scheduled_scopes_stay_within_the_previous_search_budget(self):
        slots = [scheduled_web_scopes(NOW + timedelta(minutes=30*i)) for i in range(31*48)]
        self.assertEqual(sum(map(len, slots)), 744)
        self.assertEqual(set(filter(None, slots)), {("web",), ("social",), ("reddit",)})
