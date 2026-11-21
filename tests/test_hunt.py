import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from promo_hunter.__main__ import process_batch, select_candidates
from promo_hunter.candidate import Candidate, url_key, web_url
from promo_hunter.filtering import MAX_BATCH, Offer, classify, keyword_score, parse_verdicts
from promo_hunter.state import Seen
from promo_hunter.telegram import TelegramError, format_alert, send_message

TEMPORARY_OFFER = "https://vendor.example/promo"
POST = Candidate(
    "hn:12", "Devin free credits", "", "https://news.ycombinator.com/item?id=12",
    links=(TEMPORARY_OFFER,),
)
SECOND_POST = Candidate("reddit:abc", "Free promo", "", "https://www.reddit.com/r/cursor/comments/abc/")
NOW = datetime(2026, 11, 21, 15, 55, tzinfo=timezone.utc)
VERDICT = {
    "index": 0,
    "approved": True,
    "tool_name": "Devin",
    "offer_summary": "Tặng tín dụng trải nghiệm",
    "deadline": "2026-11-30",
    "claim_window_minutes": None,
    "how_to_claim": "Đăng ký trên website",
    "url": TEMPORARY_OFFER,
}
OFFER = Offer("Devin", "Tặng tín dụng trải nghiệm", "2026-11-30", "Đăng ký trên website", TEMPORARY_OFFER)


class PrefilterTests(unittest.TestCase):
    def test_matching_keywords_are_case_insensitive_and_can_be_in_body(self):
        for title, text in [
            ("Claude FREE credits", ""), ("AI tool", "100 % off today"),
            ("Claude Pro", ""), ("Tặng tín dụng", ""), ("New offer", ""),
        ]:
            with self.subTest(title=title):
                self.assertGreater(keyword_score(title, text), 0)

    def test_does_not_match_substrings(self):
        self.assertEqual(keyword_score("A professional freezes OpenAI workloads"), 0)


class StateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "seen.json"
        self.seen = Seen(self.path)

    def test_canonical_url_strips_tracking_not_codes(self):
        self.assertEqual(
            url_key("http://WWW.tools.example/promo/?utm_source=hn&year=26&code=a#top"),
            "https://tools.example/promo?code=a&year=26",
        )
        for url in ("javascript:alert(1)", "https://user@tools.example", "https://tools.example/?s=\n"):
            with self.subTest(url=url):
                self.assertIsNone(web_url(url))

    def test_select_dedupes_repeated_ids_and_urls(self):
        with_same_id = Candidate(POST.source_id, "Free credits", "", "https://vendor.example/other")
        with_same_url = Candidate("hn:13", "Free deal", "", POST.url + "&utm_source=test")
        unrelated = Candidate("hn:14", "New release", "Now supports Linux", TEMPORARY_OFFER)
        self.assertEqual(select_candidates([POST, with_same_id, with_same_url, unrelated], self.seen), [POST])
        self.seen.remember(POST.keys)
        self.assertEqual(select_candidates([with_same_id, with_same_url], self.seen), [])

    def test_select_reserves_room_for_open_web_matches_without_increasing_the_batch(self):
        community = [
            Candidate(f"hn:{i}", "Free credits", "", f"https://example.org/community/{i}", NOW)
            for i in range(MAX_BATCH)
        ]
        open_web = Candidate("open-web:1", "Limited giveaway", "", "https://vendor.example/promotion")
        batch = select_candidates([*community, open_web], self.seen)
        self.assertIn(open_web, batch)
        self.assertEqual(len(batch), MAX_BATCH)
        self.seen.remember(open_web.keys)
        self.assertEqual(select_candidates([*community, open_web], self.seen), community)

    def test_save_caps_history_at_500_entries_and_skips_unchanged_file(self):
        for index in range(502):
            self.seen.remember((f"hn:{index}", f"https://vendor.example/{index}"))
        self.seen.save()
        data = json.loads(self.path.read_text())
        self.assertEqual(len(data), 500)
        self.assertEqual(data[0]["keys"][0], "hn:2")
        self.assertEqual(data[-1]["keys"][0], "hn:501")
        reloaded = Seen(self.path)
        self.assertTrue(reloaded.contains(("hn:501",)))
        with patch.object(Path, "write_text") as write:
            reloaded.save()
        write.assert_not_called()

    def test_only_one_alert_for_cross_posted_claim_link_across_runs(self):
        notify = Mock()
        self.assertEqual(process_batch([POST], [OFFER], self.seen, notify), 1)
        self.assertEqual(process_batch([SECOND_POST], [OFFER], Seen(self.path), notify), 0)
        notify.assert_called_once()
        self.assertTrue(Seen(self.path).contains(SECOND_POST.keys))

    def test_failed_send_can_retry_and_earlier_delivery_is_saved(self):
        second_offer = Offer("Cursor", "Tặng một tháng", None, "Đăng ký", "https://cursor.example/new")
        notify = Mock(side_effect=[None, TelegramError("Timeout")])
        with self.assertRaises(TelegramError):
            process_batch([POST, SECOND_POST], [OFFER, second_offer], self.seen, notify)
        state = Seen(self.path)
        self.assertTrue(state.contains(POST.keys))
        self.assertFalse(state.contains(SECOND_POST.keys))

    def test_rejection_is_recorded_without_suppressing_future_cross_post_alert(self):
        notify = Mock()
        self.assertEqual(process_batch([POST], [None], self.seen, notify), 0)
        self.assertTrue(self.seen.contains(POST.keys))
        self.assertFalse(self.seen.contains((url_key(POST.url),), alerted_only=True))
        notify.assert_not_called()


class VerdictTests(unittest.TestCase):
    def test_success_and_invented_url_falls_back_to_source(self):
        self.assertEqual(parse_verdicts(json.dumps([VERDICT]), [POST], NOW), [OFFER])
        altered = dict(VERDICT, url="https://invented.example")
        offer = parse_verdicts(json.dumps([altered]), [POST], NOW)[0]
        self.assertEqual(offer.url, POST.url)
        self.assertTrue(offer.source_only)
        self.assertIn(f"[Xem bài đăng nguồn]({POST.url})", format_alert(offer))

    def test_expired_and_explicit_rejection(self):
        for change in ({"deadline": "2026-11-20"}, {"approved": False}):
            self.assertEqual(parse_verdicts(json.dumps([VERDICT | change]), [POST], NOW), [None])

    def test_relative_claim_deadline_rejects_expired_or_unverifiable_offers(self):
        relative = json.dumps([dict(VERDICT, deadline=None, claim_window_minutes=1440)])
        for published in (
            None,
            datetime(2026, 11, 19, 12, 24, tzinfo=timezone.utc),
            datetime(2026, 11, 20, 15, 55, tzinfo=timezone.utc),
            datetime(2026, 11, 21, 16, 0, tzinfo=timezone.utc),
        ):
            with self.subTest(published=published):
                self.assertEqual(parse_verdicts(relative, [replace(POST, published=published)], NOW), [None])

    def test_active_relative_window_displays_computed_deadline(self):
        published = datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)
        post = replace(POST, published=published)
        relative = json.dumps([dict(VERDICT, deadline=None, claim_window_minutes=1440)])
        offer = parse_verdicts(relative, [post], NOW)[0]
        self.assertEqual(offer.deadline, "2026-11-21 17:00 UTC")

    def test_incomplete_duplicate_or_invalid_output_raises(self):
        for bad_output in ([], [VERDICT, VERDICT], [dict(VERDICT, approved="true")]):
            with self.subTest(bad_output=bad_output), self.assertRaises(ValueError):
                parse_verdicts(json.dumps(bad_output), [POST], NOW)

    def test_sdk_receives_structured_config_and_all_evidence(self):
        with patch("promo_hunter.filtering.genai.Client") as Client:
            models = Client.return_value.__enter__.return_value.models
            models.generate_content.return_value.text = json.dumps([VERDICT])
            self.assertEqual(classify("fake-key", "test-model", [POST], NOW), [OFFER])
        request = models.generate_content.call_args.kwargs
        self.assertEqual(request["config"].response_mime_type, "application/json")
        self.assertEqual(request["config"].response_json_schema["type"], "array")
        self.assertIn(TEMPORARY_OFFER, request["contents"])


class TelegramTests(unittest.TestCase):
    def test_formats_vietnamese_and_escapes_dynamic_markdown(self):
        offer = Offer(
            "AI_Pro (Max)!*", "Tặng 100% + mã [FREE].", "2026-12-01",
            "Use a #code | then go!", "https://vendor.example/deal(a)?code=1",
        )
        self.assertEqual(
            format_alert(offer).splitlines(),
            [
                r"*Ưu đãi: AI\_Pro \(Max\)\!\**",
                r"*Nội dung:* Tặng 100% \+ mã \[FREE\]\.",
                r"*Hạn chót:* 2026\-12\-01",
                r"*Cách nhận:* Use a \#code \| then go\!",
                r"[Mở trang nhận ưu đãi](https://vendor.example/deal(a\)?code=1)",
            ],
        )

    def test_unknown_deadline_is_readable_and_message_is_bounded(self):
        offer = Offer("Claude", "!" * 5000, None, "_" * 5000, TEMPORARY_OFFER)
        text = format_alert(offer)
        self.assertIn("Chưa công bố", text)
        self.assertLess(len(text), 4096)

    def test_requests_markdown_v2_and_handles_api_failure(self):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {"ok": True}
        send_message(session, "fake-token", "-123", "Alert")
        self.assertEqual(session.post.call_args.kwargs["json"]["parse_mode"], "MarkdownV2")
        session.post.return_value.json.return_value = {"ok": False}
        with self.assertRaises(TelegramError):
            send_message(session, "fake-token", "-123", "Alert")

    def test_provider_network_error_does_not_expose_token(self):
        session = Mock()
        session.post.side_effect = requests.Timeout("https://api.telegram.org/botfake-token/sendMessage")
        with self.assertRaises(TelegramError) as raised:
            send_message(session, "fake-token", "-123", "Alert")
        self.assertNotIn("fake-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
