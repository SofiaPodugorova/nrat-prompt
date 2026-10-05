from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
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

    def test_day_command_paths_summary_and_exit_codes(self):
        for status, interrupted, expected in [("complete", False, 0), ("incomplete", False, 2),
                                               ("incomplete", True, 130)]:
            for custom in (False, True):
                with self.subTest(status=status, interrupted=interrupted, custom=custom):
                    report = dict(status=status, interrupted=interrupted, query_date=DAY,
                                  reported_count=2, unique_count=2, pages_processed=2,
                                  repeat_count=1, incomplete_reasons=[])
                    args = ["nrat", "day", "--date", DAY]
                    path = Path("data/custom.json") if custom else Path(f"data/days/{DAY}.json")
                    if custom:
                        args += ["--output", str(path)]
                    stdout = io.StringIO()
                    with patch("sys.argv", args), patch("nrat.__main__.HttpClient"), \
                            patch("nrat.__main__.collect_day", return_value=report) as collect, \
                            patch("nrat.__main__.fetch_page") as single_page, redirect_stdout(stdout):
                        self.assertEqual(main(), expected)
                    self.assertEqual(collect.call_args.args[1:], (date.fromisoformat(DAY), path))
                    single_page.assert_not_called()
                    summary = json.loads(stdout.getvalue())
                    self.assertEqual(summary["output"], str(path))
                    self.assertEqual(summary["status"], status)

    def test_day_storage_error_is_sanitized(self):
        stderr = io.StringIO()
        with patch("sys.argv", ["nrat", "day", "--date", DAY]), \
                patch("nrat.__main__.HttpClient"), \
                patch("nrat.__main__.collect_day", side_effect=OSError("secret-token")), \
                redirect_stderr(stderr):
            self.assertEqual(main(), 1)
        self.assertIn("OSError", stderr.getvalue())
        self.assertNotIn("secret-token", stderr.getvalue())

    def test_day_invalid_output_rejected_before_client_creation(self):
        with patch("sys.argv", ["nrat", "day", "--date", DAY, "--output", "README.md"]), \
                patch("nrat.__main__.HttpClient") as client, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)
        client.assert_not_called()

    def test_collect_command_requires_year_and_checks_limits(self):
        for args in ([], ["--year", "0"], ["--year", "2010", "--day", "2012-01-01"],
                     ["--year", "2010", "--max-downloads", "-1"]):
            with self.subTest(args=args), patch("sys.argv", ["nrat", "collect", *args]), \
                    patch("nrat.__main__.HttpClient") as client, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertEqual(raised.exception.code, 2)
                client.assert_not_called()

    def test_collect_command_arguments_and_exit_codes(self):
        for status, code in (("finished", 0), ("limited", 2), ("interrupted", 130), ("failed", 1)):
            result = {"summary": {"status": "complete" if code == 0 else "incomplete"},
                      "runs": [{"status": status}]}
            output = io.StringIO()
            with self.subTest(status=status), \
                    patch("sys.argv", ["nrat", "collect", "--year", "2010", "--day", DAY,
                                       "--max-downloads", "3", "--work-dir", "data/test", "--no-archive"]), \
                    patch("nrat.__main__.HttpClient"), \
                    patch("nrat.collector.collect", return_value=result) as collect, redirect_stdout(output):
                self.assertEqual(main(), code)
            self.assertEqual(collect.call_args.args[1:], (2010, Path("data/test")))
            self.assertEqual(collect.call_args.kwargs,
                             dict(day=date.fromisoformat(DAY), max_downloads=3, make_archive=False))
            self.assertEqual(json.loads(output.getvalue())["run"]["status"], status)

    def test_collect_checkpoint_error_is_reported_without_reset(self):
        from nrat.state import CheckpointError
        stderr = io.StringIO()
        with patch("sys.argv", ["nrat", "collect", "--year", "2010"]), \
                patch("nrat.__main__.HttpClient"), \
                patch("nrat.collector.collect", side_effect=CheckpointError("Damaged checkpoint; nothing was reset")), \
                redirect_stderr(stderr):
            self.assertEqual(main(), 1)
        self.assertIn("nothing was reset", stderr.getvalue())
