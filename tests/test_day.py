"""Artificial responses only; all requests and sleeps are replaced locally."""

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from nrat.day import collect_day, write_json_atomic
from nrat.http import HttpClient
from tests.examples import DAY, card, html_page, pagination
from tests.test_http import FakeClock, response

D = date.fromisoformat(DAY)


def example(page, ids, count, *, next_page=None, limited=False, number=None, domain="nddkr"):
    cards = "".join(card(f"https://{domain}.ukrintei.ua/view/ok/{i:032x}",
                         number or f"0000U{i:06d}") for i in ids)
    pages = pagination(page=page, next_page=next_page) if count else ""
    return html_page(cards=cards, count=count, pages=pages, limited=limited).encode()


class DayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "daily.json"

    def run_day(self, replies, *, sleep=None):
        session = Mock(spec=requests.Session)
        session.get.side_effect = replies
        clock = FakeClock()
        client = HttpClient(session=session, sleep=sleep or clock.sleep, clock=clock)
        result = collect_day(client, D, self.output)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8")), result)
        return result, session, clock

    def categories(self, report):
        return {e["category"] for e in report["incomplete_reasons"]}

    def test_multiple_short_pages_complete_by_pagination(self):
        r, session, clock = self.run_day([
            response(content=example(1, [1], 3, next_page=2)),
            response(content=example(2, [2], 3, next_page=3)),
            response(content=example(3, [3], 3)),
        ])
        self.assertEqual(r["status"], "complete")
        self.assertEqual(r["unique_count"], 3)
        self.assertEqual(r["pages_processed"], 3)
        self.assertEqual(r["records"][0]["listing_number"], "0000U000001")
        self.assertEqual(r["records"][0]["query_date"], DAY)
        self.assertEqual(r["incomplete_reasons"], [])
        self.assertEqual(clock.waits, [3, 3])
        self.assertEqual([c.kwargs["params"]["pa"] for c in session.get.call_args_list], ["1", "2", "3"])
        for call in session.get.call_args_list:
            self.assertEqual(call.kwargs["params"]["typeSearch2"], "ok")
            self.assertEqual(call.kwargs["params"]["dateFromSearch"], DAY)
            self.assertEqual(call.kwargs["params"]["dateToSearch"], DAY)
            self.assertEqual(call.kwargs["params"]["sortDir"], "asc")

    def test_confirmed_zero_day(self):
        r, session, clock = self.run_day([response(content=example(1, [], 0))])
        self.assertEqual(r["status"], "complete")
        self.assertTrue(r["empty_day_confirmed"])
        self.assertEqual(r["reported_count"], 0)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(clock.waits, [])

    def test_repeated_page_stops_and_preserves_one_record(self):
        r, session, clock = self.run_day([
            response(content=example(1, [1], 2, next_page=2)),
            response(content=example(2, [1], 2, next_page=3)),
        ])
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual(r["unique_count"], 1)
        self.assertEqual(r["repeat_count"], 1)
        self.assertIn("page_repeat", self.categories(r))
        self.assertIn("count_mismatch", self.categories(r))
        self.assertEqual(session.get.call_count, 2)

    def test_overlap_with_new_records_can_complete(self):
        r, _, _ = self.run_day([
            response(content=example(1, [1, 2], 3, next_page=2)),
            response(content=example(2, [2, 3], 3)),
        ])
        self.assertEqual(r["status"], "complete")
        self.assertEqual(r["repeat_count"], 1)
        self.assertEqual(len(r["records"]), 3)

    def test_conflicting_metadata_is_preserved(self):
        r, _, _ = self.run_day([
            response(content=example(1, [1, 2], 3, next_page=2)),
            response(content=example(2, [2, 3], 3, number="DIFFERENT", domain="dir")),
        ])
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual(r["unique_count"], 3)
        self.assertIn("metadata_conflict", self.categories(r))
        self.assertEqual(r["records"][1]["listing_number"], "0000U000002")
        conflict = r["conflicts"][0]
        self.assertEqual(set(conflict["changed_fields"]), {"listing_number", "source_url"})
        self.assertEqual(conflict["observed"]["listing_number"], "DIFFERENT")

    def test_duplicates_inside_page_are_counted_with_conflicts(self):
        cards = card(f"https://nddkr.ukrintei.ua/view/ok/{1:032x}", "FIRST")
        cards += card(f"https://dir.ukrintei.ua/view/ok/{1:032x}", "SECOND")
        r, _, _ = self.run_day([response(content=html_page(cards=cards, count=2).encode())])
        self.assertEqual(r["unique_count"], 1)
        self.assertEqual(r["repeat_count"], 1)
        self.assertEqual(len(r["conflicts"]), 1)
        self.assertIn("partial", self.categories(r))

    def test_intermediate_page_500_failure_retains_records(self):
        r, session, clock = self.run_day([
            response(content=example(1, [1], 2, next_page=2)),
            response(500), response(500), response(500),
        ])
        self.assertEqual(r["unique_count"], 1)
        self.assertEqual(r["status"], "incomplete")
        self.assertIn("page_fail", self.categories(r))
        self.assertEqual([e["attempt"] for e in r["http_attempt_errors"]], [1, 2, 3])
        self.assertTrue(all(e["page"] == 2 and e["http_code"] == 500 for e in r["http_attempt_errors"]))
        self.assertEqual(clock.waits, [3, 15, 30])
        self.assertEqual(session.get.call_count, 4)
        self.assertNotIn("pdf", r)

    def test_recovered_failure_does_not_block_completion(self):
        r, _, clock = self.run_day([response(500), response(content=example(1, [1], 1))])
        self.assertEqual(r["status"], "complete")
        self.assertEqual(len(r["http_attempt_errors"]), 1)
        self.assertEqual(r["http_attempt_errors"][0]["http_code"], 500)
        self.assertEqual(clock.waits, [15])

    def test_final_unique_count_mismatch(self):
        r, _, _ = self.run_day([
            response(content=example(1, [1], 3, next_page=2)),
            response(content=example(2, [2], 3)),
        ])
        self.assertTrue(r["pagination_finished"])
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual(self.categories(r), {"count_mismatch"})

    def test_counter_change_is_reported_even_if_initial_count_matches(self):
        r, session, _ = self.run_day([
            response(content=example(1, [1], 2, next_page=2)),
            response(content=example(2, [2], 3, next_page=3)),
        ])
        self.assertEqual(r["reported_count"], 2)
        self.assertEqual(r["unique_count"], 2)
        self.assertEqual(r["reported_counts"], [{"page": 1, "count": 2}, {"page": 2, "count": 3}])
        self.assertEqual(self.categories(r), {"count_changed"})
        self.assertEqual(session.get.call_count, 2)

    def test_partial_page_retains_valid_records(self):
        cards = card(f"https://nddkr.ukrintei.ua/view/ok/{1:032x}")+card("#")
        r, _, _ = self.run_day([response(content=html_page(cards=cards, count=2).encode())])
        self.assertEqual(r["unique_count"], 1)
        self.assertEqual(r["pages"][0]["status"], "partial")
        self.assertIn("partial", self.categories(r))
        self.assertEqual(r["pages"][0]["issues"][0]["category"], "card_link_invalid")

    def test_explicit_limit_blocks_completion_even_when_count_matches(self):
        r, _, _ = self.run_day([response(content=example(1, [1], 1, limited=True))])
        self.assertEqual(r["unique_count"], r["reported_count"])
        self.assertTrue(r["limited"])
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual(self.categories(r), {"result_limit"})

    def test_threshold_blocks_completion_even_without_limit_message(self):
        r, _, _ = self.run_day([response(content=example(1, range(1000), 1000))])
        self.assertEqual(r["unique_count"], 1000)
        self.assertFalse(r["limited"])
        self.assertTrue(r["count_ge_1000"])
        self.assertEqual(self.categories(r), {"count_ge_1000"})
        self.assertEqual(r["status"], "incomplete")

    def test_100_pages_limit_without_request_101(self):
        replies = [response(content=example(p, [p], 101, next_page=p+1)) for p in range(1, 101)]
        r, session, clock = self.run_day(replies)
        self.assertEqual(session.get.call_count, 100)
        self.assertEqual(r["pages_processed"], 100)
        self.assertEqual(r["unique_count"], 100)
        self.assertIn("page_limit", self.categories(r))
        self.assertEqual(clock.waits, [3] * 99)

    def test_exactly_100_pages_can_complete(self):
        replies = [response(content=example(p, [p], 100, next_page=p+1 if p < 100 else None))
                   for p in range(1, 101)]
        r, _, _ = self.run_day(replies)
        self.assertEqual(r["status"], "complete")
        self.assertNotIn("page_limit", self.categories(r))

    def test_wrong_page_is_rejected_after_three_attempts(self):
        r, _, _ = self.run_day([
            response(content=example(1, [1], 2, next_page=2)),
            *[response(content=example(1, [2], 2, next_page=2)) for _ in range(3)],
        ])
        self.assertEqual(r["unique_count"], 1)
        self.assertIn("page_fail", self.categories(r))
        self.assertTrue(all(e["category"] == "pagination_mismatch" for e in r["http_attempt_errors"]))

    def test_backward_next_page_is_not_followed(self):
        bad = example(1, [1], 2, next_page=1)
        r, session, clock = self.run_day([response(content=bad)] * 3)
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual([c.kwargs["params"]["pa"] for c in session.get.call_args_list], ["1"] * 3)
        self.assertEqual(clock.waits, [15, 30])

    def test_interrupt_during_retry_keeps_failed_attempt_and_prior_records(self):
        def sleep(seconds):
            if seconds == 15:
                saved = json.loads(self.output.read_text(encoding="utf-8"))
                self.assertEqual(saved["unique_count"], 1)
                self.assertEqual(saved["http_attempt_errors"][0]["http_code"], 500)
                raise KeyboardInterrupt
        r, _, _ = self.run_day([
            response(content=example(1, [1], 2, next_page=2)), response(500),
        ], sleep=sleep)
        self.assertTrue(r["interrupted"])
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual(r["unique_count"], 1)
        self.assertEqual(len(r["http_attempt_errors"]), 1)
        self.assertIn("interrupted", self.categories(r))

    def test_unexpected_exception_keeps_previous_records_and_hides_message(self):
        r, _, _ = self.run_day([
            response(content=example(1, [1], 2, next_page=2)), RuntimeError("SECRET_TOKEN"),
        ])
        self.assertEqual(r["unique_count"], 1)
        self.assertIn("collection_error", self.categories(r))
        self.assertNotIn("SECRET_TOKEN", json.dumps(r))


class AtomicTests(unittest.TestCase):
    def test_failed_replace_keeps_old_json_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.json"
            write_json_atomic(path, {"old": "Український текст"})
            with patch("nrat.day.os.replace", side_effect=OSError("Artificial disk error")):
                with self.assertRaises(OSError):
                    write_json_atomic(path, {"new": "data"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"old": "Український текст"})
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_non_json_output_is_rejected_before_network(self):
        client = Mock()
        with self.assertRaises(ValueError):
            collect_day(client, D, Path("README.md"))
        client.fetch.assert_not_called()
