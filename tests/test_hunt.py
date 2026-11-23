import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import requests
from google import genai
from google.genai.errors import APIError

from promo_hunter.__main__ import main, process_batch, select_candidates
from promo_hunter.candidate import Candidate, url_key, web_url
from promo_hunter.filtering import MAX_BATCH, Offer, classify, keyword_score, parse_verdicts
from promo_hunter.sources import DiscoveryResult
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
    "offer_summary": "Free trial credits",
    "deadline": "2026-11-30",
    "claim_window_minutes": None,
    "how_to_claim": "Sign up on the website",
    "url": TEMPORARY_OFFER,
}
OFFER = Offer("Devin", "Free trial credits", "2026-11-30", "Sign up on the website", TEMPORARY_OFFER)


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
        second_offer = Offer("Cursor", "One month free", None, "Sign up", "https://cursor.example/new")
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


class ScanTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.seen = Seen(Path(folder.name) / "seen.json")
        env = {"GEMINI_API_KEY": "fake-key", "TELEGRAM_BOT_TOKEN": "fake-token", "TELEGRAM_CHAT_ID": "fake-chat"}
        argv_patch = patch.object(sys, "argv", ["promo_hunter"])
        env_patch = patch.dict(os.environ, env, clear=True)
        path_patch = patch("promo_hunter.__main__.Path", side_effect=lambda name: Path(folder.name) / name)
        for setup in (argv_patch, env_patch, path_patch):
            setup.start()
            self.addCleanup(setup.stop)
        for name in ("discover", "classify", "send_message"):
            setup = patch(f"promo_hunter.__main__.{name}")
            setattr(self, name, setup.start())
            self.addCleanup(setup.stop)

    def test_no_candidates_sends_one_update_without_classification(self):
        self.discover.return_value = DiscoveryResult([])
        self.assertEqual(main(), 0)
        self.classify.assert_not_called()
        self.assertEqual(self.send_message.call_count, 1)
        self.assertIn("No new promotions found in this scan", self.send_message.call_args.args[-1])

    def test_only_first_scheduled_run_in_an_hour_searches_tavily(self):
        self.discover.return_value = DiscoveryResult([])
        now = NOW.replace(hour=2)
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule", "TAVILY_API_KEY": "test"}):
            with patch("promo_hunter.__main__.datetime") as clock:
                clock.now.side_effect = (now.replace(minute=15), now, now.replace(hour=3, minute=0))
                for _ in range(3):
                    self.assertEqual(main(), 0)
        self.assertEqual([call.args[-1] for call in self.discover.call_args_list], [("social",), (), ("reddit",)])

    def test_manual_and_keyless_runs_do_not_reserve_a_search_hour(self):
        self.discover.return_value = DiscoveryResult([])
        with patch.dict(os.environ, {"TAVILY_API_KEY": "test"}):
            self.assertEqual(main(), 0)
            self.assertEqual(len(self.discover.call_args.args), 4)
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"}):
            self.assertEqual(main(), 0)
            self.assertEqual(self.discover.call_args.args[-1], ())
        self.assertFalse((self.seen.path.parent / "web-search-hour.txt").exists())

    def test_scheduled_search_results_are_classified_even_with_a_full_community_batch(self):
        community = [
            Candidate(f"hn:{i}", "Free AI credits", "", f"https://news.ycombinator.com/item?id={i}")
            for i in range(MAX_BATCH)
        ]
        social = [
            Candidate(f"web:{i}", "Cursor Pro", "", f"https://x.com/vendor/status/{i}")
            for i in range(10)
        ]
        self.discover.return_value = DiscoveryResult(community + social)
        self.classify.return_value = [None] * MAX_BATCH
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule", "TAVILY_API_KEY": "test"}):
            with patch("promo_hunter.__main__.datetime") as clock:
                clock.now.return_value = NOW.replace(hour=2)
                self.assertEqual(main(), 0)
        self.assertEqual(self.classify.call_args.args[2], social + community[:2])

    def test_all_rejected_sends_one_update_and_records_the_checked_post(self):
        self.discover.return_value = DiscoveryResult([POST])
        self.classify.return_value = [None]
        self.assertEqual(main(), 0)
        self.assertEqual(self.send_message.call_count, 1)
        self.assertIn("No new promotions found", self.send_message.call_args.args[-1])
        self.assertTrue(Seen(self.seen.path).contains(POST.keys))

    def test_duplicate_claim_still_gets_an_empty_scan_update(self):
        self.seen.remember((url_key(OFFER.url),), alerted=True)
        self.seen.save()
        self.discover.return_value = DiscoveryResult([POST])
        self.classify.return_value = [OFFER]
        self.assertEqual(main(), 0)
        self.assertEqual(self.send_message.call_count, 1)
        self.assertIn("No new promotions found", self.send_message.call_args.args[-1])

    def test_partial_scan_discloses_missing_sources(self):
        self.discover.return_value = DiscoveryResult([], partial=True)
        self.assertEqual(main(), 0)
        self.assertEqual(self.send_message.call_count, 1)
        self.assertIn("Some sources could not be checked", self.send_message.call_args.args[-1])

    def test_promotion_is_not_followed_by_an_empty_update(self):
        self.discover.return_value = DiscoveryResult([POST])
        self.classify.return_value = [OFFER]
        self.assertEqual(main(), 0)
        self.assertEqual(self.send_message.call_count, 1)
        self.assertEqual(self.send_message.call_args.args[-1], format_alert(OFFER))

    def test_total_source_failure_does_not_send_an_empty_update(self):
        self.discover.side_effect = RuntimeError("All promotion sources failed")
        with self.assertLogs("promo_hunter.__main__", level="ERROR"):
            self.assertEqual(main(), 1)
        self.send_message.assert_not_called()

    def test_classification_failure_does_not_send_an_empty_update(self):
        self.discover.return_value = DiscoveryResult([POST])
        self.classify.side_effect = ValueError("Unusable verdict")
        with self.assertLogs("promo_hunter.__main__", level="ERROR"):
            self.assertEqual(main(), 1)
        self.send_message.assert_not_called()

    def test_telegram_connection_test_only_sends_its_english_test_message(self):
        with patch.object(sys, "argv", ["promo_hunter", "--test-telegram"]):
            self.assertEqual(main(), 0)
        self.discover.assert_not_called()
        self.assertEqual(self.send_message.call_count, 1)
        self.assertIn("Telegram connection test passed", self.send_message.call_args.args[-1])


class VerdictTests(unittest.TestCase):
    def classify_with_transport(self, handler):
        client = genai.Client
        with httpx.Client(transport=httpx.MockTransport(handler)) as transport_client:
            def create_client(*, http_options, **kwargs):
                return client(
                    http_options=http_options.model_copy(update={"httpx_client": transport_client}), **kwargs
                )

            with patch("promo_hunter.filtering.genai.Client", side_effect=create_client), patch("time.sleep"):
                return classify("fake-key", "test-model", [POST], NOW)

    def test_gemini_retries_503_and_uses_the_second_response(self):
        handler = Mock(side_effect=[
            httpx.Response(503, json={"error": {"code": 503, "message": "Unavailable"}}),
            httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps([VERDICT])}]}}]}),
        ])
        self.assertEqual(self.classify_with_transport(handler), [OFFER])
        self.assertEqual(handler.call_count, 2)

    def test_gemini_retries_are_bounded_and_skip_permanent_errors(self):
        for code, attempts in ((503, 3), (403, 1)):
            with self.subTest(code=code):
                handler = Mock(side_effect=lambda _: httpx.Response(code, json={"error": {"code": code}}))
                with self.assertRaises(APIError) as raised:
                    self.classify_with_transport(handler)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(handler.call_count, attempts)

    def test_success_and_invented_url_falls_back_to_source(self):
        self.assertEqual(parse_verdicts(json.dumps([VERDICT]), [POST], NOW), [OFFER])
        altered = dict(VERDICT, url="https://invented.example")
        offer = parse_verdicts(json.dumps([altered]), [POST], NOW)[0]
        self.assertEqual(offer.url, POST.url)
        self.assertTrue(offer.source_only)
        self.assertIn(f"[View source post]({POST.url})", format_alert(offer))

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
        self.assertIn("in English", request["config"].system_instruction)
        self.assertIn(TEMPORARY_OFFER, request["contents"])


class TelegramTests(unittest.TestCase):
    def test_formats_english_and_escapes_dynamic_markdown(self):
        offer = Offer(
            "AI_Pro (Max)!*", "100% off + code [FREE].", "2026-12-01",
            "Use a #code | then go!", "https://vendor.example/deal(a)?code=1",
        )
        self.assertEqual(
            format_alert(offer).splitlines(),
            [
                r"*Promotion: AI\_Pro \(Max\)\!\**",
                r"*Offer:* 100% off \+ code \[FREE\]\.",
                r"*Deadline:* 2026\-12\-01",
                r"*How to claim:* Use a \#code \| then go\!",
                r"[Open claim page](https://vendor.example/deal(a\)?code=1)",
            ],
        )

    def test_unknown_deadline_is_readable_and_message_is_bounded(self):
        offer = Offer("Claude", "!" * 5000, None, "_" * 5000, TEMPORARY_OFFER)
        text = format_alert(offer)
        self.assertIn("Not announced", text)
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
