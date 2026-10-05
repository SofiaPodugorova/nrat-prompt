"""Synthetic PDF bytes and responses; no live requests or actual sleeps."""

from dataclasses import asdict
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from nrat.download import download_pdf, inspect_pdf
from nrat.http import HttpClient
from nrat.search import fetch_page
from tests.examples import DAY, ID, URL, html_page
from tests.test_http import FakeClock, response

# Deliberately synthetic minimal PDF-shaped data; only initial checks are tested.
PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
D = date.fromisoformat(DAY)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / f"{ID}.pdf"

    def client(self, replies):
        clock = FakeClock()
        session = Mock(spec=requests.Session)
        session.get.side_effect = replies
        return HttpClient(session=session, clock=clock, sleep=clock.sleep), session, clock

    def download(self, replies, **kwargs):
        client, session, clock = self.client(replies)
        return download_pdf(client, D, ID, URL, self.path, **kwargs), session, clock

    def test_success_signature_size_hash_and_original_host(self):
        result, session, _ = self.download([response(content=PDF, media="application/pdf")])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.file, inspect_pdf(self.path))
        self.assertEqual(result.file.size, len(PDF))
        self.assertEqual(len(result.file.sha256), 64)
        self.assertEqual(session.get.call_args.args, (URL,))
        self.assertEqual(session.get.call_args.kwargs["timeout"], (30, 120))
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertTrue(session.get.call_args.kwargs["stream"])
        self.assertFalse(self.path.with_suffix(".part").exists())

    def test_skip_requires_both_signature_and_saved_hash(self):
        self.path.write_bytes(PDF)
        expected = asdict(inspect_pdf(self.path))
        r, session, _ = self.download([], expected=expected)
        self.assertEqual(r.status, "skip")
        session.get.assert_not_called()
        # A different signature-valid PDF must not silently be trusted.
        self.path.write_bytes(PDF + b"changed")
        r, session, _ = self.download([response(content=PDF)], expected=expected)
        self.assertEqual(r.status, "ok")
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(self.path.read_bytes(), PDF)

    def test_corrupt_existing_or_untracked_file_is_downloaded(self):
        for content in (b"broken", PDF):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                r, session, _ = self.download([response(content=PDF)])
                self.assertEqual(r.status, "ok")
                self.assertEqual(session.get.call_count, 1)

    def test_empty_and_html_are_retried_and_never_published(self):
        for content, status, category in [(b"", "empty", "empty"),
                                          (b"<html>Error</html>", "fail", "html_instead_pdf"),
                                          (b"some text", "fail", "invalid_pdf")]:
            r, session, clock = self.download([response(content=content)] * 3)
            self.assertEqual(r.status, status)
            self.assertEqual([e.category for e in r.errors], [category] * 3)
            self.assertEqual(session.get.call_count, 3)
            self.assertEqual(clock.waits, [15, 30])
            self.assertFalse(self.path.exists())
            self.assertFalse(self.path.with_suffix(".part").exists())

    def test_404_410_no_retry(self):
        for code in (404, 410):
            r, session, clock = self.download([response(code)])
            self.assertEqual(r.status, "notpdf")
            self.assertEqual(r.errors[0].http_code, code)
            self.assertEqual(session.get.call_count, 1)
            self.assertEqual(clock.waits, [])

    def test_500_success_and_three_500(self):
        r, _, clock = self.download([response(500), response(content=PDF)])
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.attempts, 2)
        self.assertEqual(r.errors[0].http_code, 500)
        self.assertEqual(clock.waits, [15])
        self.path.unlink()
        r, session, clock = self.download([response(500)] * 3)
        self.assertEqual(r.status, "server500")
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(clock.waits, [15, 30])

    def test_network_timeout_and_retry_after(self):
        r, _, clock = self.download([requests.ReadTimeout("SECRET"), response(503, retry_after="900"),
                                     response(content=PDF)])
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.errors[0].exception_type, "ReadTimeout")
        self.assertNotIn("SECRET", repr(r))
        self.assertEqual(clock.waits, [15, 300])

    def test_retriable_http_responses(self):
        for code in (408, 425, 429, 502, 503, 504):
            r, session, _ = self.download([response(code)] * 3)
            self.assertEqual(r.status, "fail")
            self.assertEqual(session.get.call_count, 3)

    def test_terminal_http_does_not_retry(self):
        r, session, _ = self.download([response(403)])
        self.assertEqual(r.status, "fail")
        self.assertEqual(session.get.call_count, 1)

    def test_safe_redirect_preserves_final_address(self):
        redirect = response(302)
        final = f"https://dir.ukrintei.ua/view/ok/{ID}"
        redirect.headers["Location"] = final
        r, session, clock = self.download([redirect, response(content=PDF)])
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.final_url, final)
        self.assertEqual(r.attempts, 1)
        self.assertEqual([c.args[0] for c in session.get.call_args_list], [URL, final])
        self.assertEqual(clock.waits, [1])

    def test_unsafe_or_excessive_redirects_not_followed(self):
        for target in ("http://dir.ukrintei.ua/file", "https://evil.example/file",
                       "https://dir.ukrintei.ua/file?token=SECRET", "https://user:pass@dir.ukrintei.ua/file", "https://["):
            redirect = response(302)
            redirect.headers["Location"] = target
            r, session, _ = self.download([redirect])
            self.assertEqual(r.status, "fail")
            self.assertEqual(session.get.call_count, 1)
            self.assertNotIn("SECRET", repr(r))
        redirect = response(302)
        redirect.headers["Location"] = URL
        r, session, _ = self.download([redirect] * 4)
        self.assertEqual(r.status, "fail")
        self.assertEqual(session.get.call_count, 4)
        self.assertEqual(r.attempts, 1)

    def test_pdf_spacing_is_one_second_search_three_and_days_five(self):
        client, session, clock = self.client([response(), response(content=PDF), response(content=PDF),
                                             response(content=html_page(day="2010-03-13").encode())])
        fetch_page(client, D, 1)
        download_pdf(client, D, ID, URL, self.path)
        download_pdf(client, D, ID, URL, self.path)
        fetch_page(client, date(2010, 3, 13), 1)
        self.assertEqual(clock.waits, [1, 1, 5])

    def test_disk_error_is_distinct_and_not_retried(self):
        with patch("nrat.download.os.replace", side_effect=PermissionError("SECRET")):
            r, session, clock = self.download([response(content=PDF)])
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.errors[0].category, "disk_error")
        self.assertEqual(r.errors[0].exception_type, "PermissionError")
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(clock.waits, [])
        self.assertFalse(self.path.exists())

    def test_stream_timeout_removes_part_before_retry(self):
        first = response()
        def chunks(**_):
            yield b"%PDF"
            raise requests.ReadTimeout("SECRET")
        first.iter_content = chunks
        r, _, clock = self.download([first, response(content=PDF)])
        self.assertEqual(r.status, "ok")
        self.assertEqual(self.path.read_bytes(), PDF)
        self.assertEqual(clock.waits, [15])

    def test_invalid_source_rejected_before_request(self):
        client, session, _ = self.client([])
        with self.assertRaises(ValueError):
            download_pdf(client, D, ID, f"https://dir.ukrintei.ua/view/rk/{ID}", self.path)
        session.get.assert_not_called()
