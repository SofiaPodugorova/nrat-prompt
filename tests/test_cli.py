from contextlib import redirect_stdout
from datetime import date
import io
import json
import unittest
from unittest.mock import patch

from nrat.__main__ import main
from nrat.http import AttemptFailure, HttpResult
from nrat.search import parse_page
from tests.examples import DAY, card, html_page


class CliTests(unittest.TestCase):
    def test_json_status_and_exit_codes_without_network(self):
        cases = [
            (HttpResult(parse_page(html_page(), date.fromisoformat(DAY), 1), 1, ()), "ok", 0),
            (HttpResult(parse_page(html_page(cards=card("#")), date.fromisoformat(DAY), 1), 1, ()), "partial", 2),
            (HttpResult(None, 1, (AttemptFailure(1, "page_fail", "HTTP 404", 404),)), "page_fail", 1),
        ]
        for result, status, code in cases:
            with self.subTest(status=status):
                output = io.StringIO()
                with patch("sys.argv", ["nrat", "--date", DAY, "--page", "1"]), \
                        patch("nrat.__main__.HttpClient"), \
                        patch("nrat.__main__.fetch_page", return_value=result) as fetch, redirect_stdout(output):
                    self.assertEqual(main(), code)
                self.assertEqual(json.loads(output.getvalue())["status"], status)
                self.assertEqual(fetch.call_args.args[1:], (date.fromisoformat(DAY), 1))
