from datetime import date
import unittest
from unittest.mock import Mock

import requests

from nrat.http import BASE_URL, HttpClient, ResponseValidationError, retry_wait
from nrat.search import fetch_page
from tests.examples import DAY, card, html_page

D = date.fromisoformat(DAY)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def response(status=200, content=None, retry_after=None, media="text/html; charset=UTF-8"):
    r = requests.Response()
    r.status_code = status
    r._content = html_page().encode() if content is None else content
    r._content_consumed = True
    r.headers["Content-Type"] = media
    if retry_after is not None:
        r.headers["Retry-After"] = retry_after
    return r


class HttpTests(unittest.TestCase):
    def client(self, responses):
        session = Mock(spec=requests.Session)
        session.get.side_effect = responses
        clock = FakeClock()
        return HttpClient(session=session, sleep=clock.sleep, clock=clock), session, clock

    def test_request_parameters_and_timeouts(self):
        client, session, clock = self.client([response()])
        result = fetch_page(client, D, 1)
        args, kwargs = session.get.call_args
        self.assertEqual(args, (BASE_URL,))
        self.assertEqual(kwargs["timeout"], (30, 90))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["params"], dict(typeSearch2="ok", dateFromSearch=DAY, dateToSearch=DAY,
                                              pa="1", sortOrder="registration_date", sortDir="asc", tab="big"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(clock.waits, [])

    def test_500_then_success_keeps_attempt_history(self):
        client, session, clock = self.client([response(500), response()])
        r = fetch_page(client, D, 1)
        self.assertEqual(r.attempts, 2)
        self.assertEqual(r.errors[0].http_code, 500)
        self.assertEqual(r.errors[0].attempt, 1)
        self.assertEqual(clock.waits, [15])

    def test_three_attempts_total(self):
        client, session, clock = self.client([response(500)] * 3)
        r = fetch_page(client, D, 1)
        self.assertIsNone(r.value)
        self.assertEqual(r.status, "page_fail")
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual([e.attempt for e in r.errors], [1, 2, 3])
        self.assertEqual(clock.waits, [15, 30])

    def test_retry_after_maximum_and_cap(self):
        client, _, clock = self.client([response(429, retry_after="20"), response(503, retry_after="900"), response()])
        self.assertEqual(fetch_page(client, D, 1).attempts, 3)
        self.assertEqual(clock.waits, [20, 300])

    def test_retry_after_edge_cases(self):
        for raw in [None, "", "-1", "1.5", "tomorrow", "Wed, 21 Oct 2026 07:28:00 GMT"]:
            self.assertEqual(retry_wait(30, raw), 30)
        self.assertEqual(retry_wait(15, "0000000000016"), 16)
        self.assertEqual(retry_wait(15, "9" * 10000), 300)
        self.assertEqual(retry_wait(30, "2"), 30)

    def test_network_timeout_and_connection_error(self):
        client, _, clock = self.client([requests.ReadTimeout("SECRET_COOKIE"), requests.ConnectionError("SECRET_TOKEN"), response()])
        r = fetch_page(client, D, 1)
        self.assertEqual(r.attempts, 3)
        self.assertIn("ReadTimeout", r.errors[0].reason)
        self.assertIn("ConnectionError", r.errors[1].reason)
        self.assertNotIn("SECRET", repr(r.errors))
        self.assertEqual(clock.waits, [15, 30])

    def test_404_410_are_search_errors_not_pdf_absence(self):
        for status in (404, 410):
            client, session, clock = self.client([response(status)])
            r = fetch_page(client, D, 1)
            self.assertEqual(r.status, "page_fail")
            self.assertEqual(r.errors[0].category, "page_fail")
            self.assertEqual(r.errors[0].http_code, status)
            self.assertEqual(session.get.call_count, 1)
            self.assertEqual(clock.waits, [])

    def test_redirect_is_not_followed(self):
        client, session, _ = self.client([response(302)])
        self.assertEqual(fetch_page(client, D, 1).status, "page_fail")
        self.assertEqual(session.get.call_count, 1)

    def test_empty_nonhtml_and_invalid_page_are_retried(self):
        for bad in [response(content=b""), response(content=b"oops", media="application/json"),
                    response(content=b"<html>temporary failure</html>"), response(content=b"\xff")]:
            client, _, clock = self.client([bad, response()])
            self.assertEqual(fetch_page(client, D, 1).attempts, 2)
            self.assertEqual(clock.waits, [15])

    def test_validation_failure_after_three_attempts(self):
        client, _, clock = self.client([response(content=html_page(count=None).encode())] * 3)
        r = fetch_page(client, D, 1)
        self.assertEqual(r.status, "page_fail")
        self.assertTrue(all(e.category == "count_missing" for e in r.errors))
        self.assertEqual(clock.waits, [15, 30])

    def test_page_and_date_pacing(self):
        client, _, clock = self.client([response()] * 3)
        client.fetch(D, 1, lambda h: h)
        client.fetch(D, 2, lambda h: h)
        client.fetch(date(2010, 3, 14), 1, lambda h: h)
        self.assertEqual(clock.waits, [3, 5])

    def test_invalid_input_never_requests_network(self):
        client, session, _ = self.client([])
        for page in (0, -1, True):
            with self.assertRaises(ValueError):
                fetch_page(client, D, page)
        session.get.assert_not_called()

    def test_partial_parse_has_distinct_status(self):
        client, session, _ = self.client([response(content=html_page(cards=card("#")).encode())])
        r = fetch_page(client, D, 1)
        self.assertEqual(r.status, "partial")
        self.assertFalse(r.value.parsing_complete)
        self.assertEqual(session.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
