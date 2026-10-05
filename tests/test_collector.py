"""Offline collection/resume/archive scenarios with artificial pages and PDF bytes."""

import csv
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import requests

from nrat.archive import build_archive, verify_archive
from nrat.collector import collect
from nrat.http import HttpClient
from nrat.reports import manifest_for
from nrat.state import CheckpointError, load_state, year_days
from tests.examples import DAY, ID, URL, card, html_page
from tests.test_download import PDF
from tests.test_http import FakeClock, response

D = date.fromisoformat(DAY)
ID2 = "00000000000000000000000000000002"
URL2 = f"https://dir.ukrintei.ua/view/ok/{ID2}"


def listing(day=D, cards=None, count=1):
    return response(content=html_page(day=day.isoformat(), cards=cards, count=count).encode())


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def client(self, replies, *, sleep=None):
        session = Mock(spec=requests.Session)
        session.get.side_effect = replies
        clock = FakeClock()
        return HttpClient(session=session, clock=clock, sleep=sleep or clock.sleep), session, clock

    def run_day(self, replies, **kwargs):
        client, session, clock = self.client(replies)
        result = collect(client, 2010, self.root, day=D, **kwargs)
        self.assertEqual(json.loads((self.root / "checkpoint.json").read_text(encoding="utf-8")), result)
        return result, session, clock

    def test_success_then_resume_skips_network_and_preserves_metadata(self):
        state, _, _ = self.run_day([listing(), response(content=PDF)])
        doc = state["documents"][ID]
        self.assertEqual(doc["status"], "ok")
        self.assertEqual(doc["metadata"]["listing_number"], "0210U000704")
        self.assertEqual(doc["metadata"]["source_url"], URL)
        self.assertTrue(state["days"][DAY]["processing_complete"])
        self.assertEqual(state["summary"]["status"], "incomplete")  # One day is not a year.
        self.assertTrue(state["summary"]["restricted_run"])
        resumed, session, _ = self.run_day([])
        self.assertEqual(resumed["documents"][ID]["status"], "skip")
        self.assertEqual(resumed["documents"][ID]["total_attempts"], 1)
        session.get.assert_not_called()

    def test_500_history_survives_resume_and_success(self):
        state, _, _ = self.run_day([listing(), *[response(500) for _ in range(3)]])
        self.assertEqual(state["documents"][ID]["status"], "server500")
        resumed, _, _ = self.run_day([response(500), response(content=PDF)])
        doc = resumed["documents"][ID]
        self.assertEqual(doc["status"], "ok")
        self.assertEqual(doc["total_attempts"], 5)
        self.assertEqual(doc["http500_count"], 4)
        self.assertEqual(doc["first500_at"], state["documents"][ID]["first500_at"])
        with (self.root / "http500.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["doc_id"], ID)
        self.assertEqual(rows[0]["total_attempts"], "5")
        self.assertEqual(rows[0]["http500_count"], "4")
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(len([e for e in resumed["errors"] if e.get("http_code") == 500]), 4)

    def test_confirmed_missing_is_accounted_but_not_all_pdfs_present(self):
        state, _, _ = self.run_day([listing(), response(404)])
        self.assertEqual(state["documents"][ID]["status"], "notpdf")
        self.assertTrue(state["days"][DAY]["processing_complete"])
        self.assertFalse(state["summary"]["all_found_pdfs_present"])
        self.assertEqual(state["summary"]["missing"], 1)
        resumed, session, _ = self.run_day([])
        session.get.assert_not_called()
        self.assertEqual(resumed["documents"][ID]["total_attempts"], 1)

    def test_search_404_is_not_a_pdf_error(self):
        state, _, _ = self.run_day([response(404)])
        self.assertEqual(state["documents"], {})
        self.assertFalse(state["days"][DAY]["listing_complete"])
        self.assertTrue(all(e["domain"] == "search" for e in state["errors"]))
        resumed, _, _ = self.run_day([listing(cards="", count=0)])
        self.assertTrue(resumed["days"][DAY]["listing_complete"])
        self.assertGreater(len(resumed["errors"]), 0)  # Prior errors are not erased.

    def test_corrupted_saved_pdf_is_redownloaded(self):
        self.run_day([listing(), response(content=PDF)])
        path = self.root / "pdf" / f"{ID}.pdf"
        path.write_bytes(PDF + b"mutated")
        state, session, _ = self.run_day([response(content=PDF)])
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(state["documents"][ID]["status"], "ok")
        self.assertEqual(path.read_bytes(), PDF)
        self.assertTrue(any(e["category"] == "existing_file_invalid" for e in state["errors"]))

    def test_interruption_during_retry_retains_documents_and_can_resume(self):
        cards = card() + card(URL2, "00002")
        clock = FakeClock()
        def stop(seconds):
            if seconds == 15:
                snapshot = json.loads((self.root / "checkpoint.json").read_text(encoding="utf-8"))
                self.assertEqual(snapshot["documents"][ID2]["http500_count"], 1)
                raise KeyboardInterrupt
            clock.sleep(seconds)
        session = Mock(spec=requests.Session)
        session.get.side_effect = [listing(cards=cards, count=2), response(content=PDF), response(500)]
        state = collect(HttpClient(session=session, sleep=stop, clock=clock), 2010, self.root, day=D)
        self.assertEqual(state["runs"][-1]["status"], "interrupted")
        self.assertEqual(state["documents"][ID]["status"], "ok")
        self.assertEqual(state["documents"][ID2]["status"], "pending")
        resumed, session, _ = self.run_day([response(content=PDF)])
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(resumed["documents"][ID]["status"], "skip")
        self.assertEqual(resumed["documents"][ID2]["status"], "ok")
        self.assertEqual(resumed["documents"][ID2]["http500_count"], 1)

    def test_limit_is_distinct_operations_not_attempts_and_resumes(self):
        cards = card() + card(URL2, "00002")
        state, session, clock = self.run_day([listing(cards=cards, count=2), response(500), response(content=PDF)],
                                              max_downloads=1)
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(state["runs"][-1]["download_operations"], 1)
        self.assertEqual(state["runs"][-1]["status"], "limited")
        self.assertEqual(state["documents"][ID2]["status"], "pending")
        resumed, session, _ = self.run_day([response(content=PDF)])
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(resumed["documents"][ID2]["status"], "ok")

    def test_same_id_on_another_date_does_not_reset_three_attempts(self):
        second = date(2010, 3, 13)
        client, session, clock = self.client([listing(), response(500), response(500), response(500),
                                             listing(second, cards=card(URL, "OTHER"))])
        with patch("nrat.collector.year_days", side_effect=lambda _: iter([D, second])):
            state = collect(client, 2010, self.root)
        self.assertEqual(session.get.call_count, 5)
        doc = state["documents"][ID]
        self.assertEqual(doc["total_attempts"], 3)
        self.assertEqual(doc["http500_count"], 3)
        self.assertEqual(doc["discovery_dates"], [DAY, second.isoformat()])
        self.assertEqual(len(doc["variants"]), 2)
        self.assertEqual(doc["metadata"]["listing_number"], "0210U000704")
        self.assertEqual(doc["variants"][1]["listing_number"], "OTHER")
        self.assertIn(5, clock.waits)

    def test_repeated_run_500_after_success_stays_in_csv_as_skip(self):
        self.run_day([listing(), response(500), response(content=PDF)])
        self.run_day([])
        with (self.root / "http500.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["status"], "skip")
        self.assertEqual(rows[0]["http500_count"], "1")

    def test_year_calendar_including_leap_day(self):
        self.assertEqual(len(list(year_days(2010))), 365)
        self.assertEqual(len(list(year_days(2012))), 366)
        self.assertIn(date(2012, 2, 29), list(year_days(2012)))
        self.assertEqual(len(list(year_days(2000))), 366)
        self.assertEqual(len(list(year_days(1900))), 365)

    def test_full_leap_year_of_confirmed_empty_days_then_no_network_resume(self):
        days = list(year_days(2012))
        client, session, clock = self.client([listing(d, cards="", count=0) for d in days])
        state = collect(client, 2012, self.root, make_archive=False)
        self.assertEqual(state["summary"]["status"], "complete")
        self.assertEqual(len(state["days"]), 366)
        self.assertEqual(session.get.call_count, 366)
        self.assertEqual(clock.waits, [5] * 365)
        client, session, _ = self.client([])
        resumed = collect(client, 2012, self.root, make_archive=False)
        self.assertEqual(resumed["summary"]["status"], "complete")
        session.get.assert_not_called()

    def test_invalid_checkpoint_never_resets_or_requests(self):
        path = self.root / "checkpoint.json"
        for raw in ("{broken", "{}", '{"year":2010,"year":2011}', "null", "NaN"):
            path.write_text(raw, encoding="utf-8")
            client, session, _ = self.client([])
            with self.assertRaises(CheckpointError):
                collect(client, 2010, self.root, day=D)
            self.assertEqual(path.read_text(encoding="utf-8"), raw)
            session.get.assert_not_called()
        path.unlink()
        state, _, _ = self.run_day([listing(), response(content=PDF)])
        state["documents"][ID]["file"]["path"] = "../../evil.pdf"
        path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            load_state(path, 2010)

    def test_missing_checkpoint_does_not_silently_lose_history(self):
        self.run_day([listing(), response(500), response(content=PDF)])
        (self.root / "checkpoint.json").unlink()
        client, session, _ = self.client([])
        with self.assertRaises(CheckpointError):
            collect(client, 2010, self.root, day=D)
        session.get.assert_not_called()
        self.assertTrue((self.root / "http500.csv").exists())

    def test_checkpoint_losing_500_history_is_rejected(self):
        state, _, _ = self.run_day([listing(), response(500), response(content=PDF)])
        state["errors"] = []
        (self.root / "checkpoint.json").write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            load_state(self.root / "checkpoint.json", 2010)

    def test_stop_mid_listing_and_resume_preserves_discoveries(self):
        from tests.examples import pagination
        first = listing(cards=card(), count=2)
        first._content = html_page(cards=card(), count=2, pages=pagination()).encode()
        client, _, _ = self.client([first, KeyboardInterrupt()])
        state = collect(client, 2010, self.root, day=D, make_archive=False)
        self.assertEqual(state["runs"][-1]["status"], "interrupted")
        self.assertIn(ID, state["documents"])
        self.assertFalse(state["days"][DAY]["listing_complete"])
        second = response(content=html_page(cards=card(URL2, "00002"), count=2,
                                            pages=pagination(page=2, next_page=None)).encode())
        resumed, session, _ = self.run_day([first, second, response(content=PDF), response(content=PDF)])
        self.assertEqual(session.get.call_count, 4)
        self.assertEqual(resumed["summary"]["found"], 2)
        self.assertTrue(resumed["days"][DAY]["listing_complete"])
        self.assertEqual(resumed["documents"][ID]["discovery_dates"], [DAY])

    def test_archive_source_corruption_marks_document_unfinished(self):
        state, _, _ = self.run_day([listing(), response(content=PDF)])
        (self.root / "pdf" / f"{ID}.pdf").write_bytes(b"broken")
        with self.assertRaises(ValueError):
            build_archive(self.root, state)
        self.assertEqual(state["documents"][ID]["status"], "pending")

    def test_incomplete_search_is_not_made_complete_by_successful_download(self):
        bad = response(content=html_page(cards=card() + card("#"), count=2).encode())
        state, _, _ = self.run_day([bad, response(content=PDF)])
        self.assertEqual(state["documents"][ID]["status"], "ok")
        self.assertFalse(state["days"][DAY]["listing_complete"])
        self.assertEqual(state["summary"]["status"], "incomplete")
        self.assertTrue(any(e["category"] == "partial" and e["domain"] == "search" for e in state["errors"]))

    def test_mismatching_year_and_scope_rejected(self):
        self.run_day([listing(cards="", count=0)])
        with self.assertRaises(CheckpointError):
            load_state(self.root / "checkpoint.json", 2012)
        client, session, _ = self.client([])
        for kwargs in (dict(day=date(2012, 1, 1)), dict(max_downloads=-1)):
            with self.assertRaises(ValueError):
                collect(client, 2010, self.root, **kwargs)
        session.get.assert_not_called()

    def test_archive_contains_manifest_lists_histories_and_verified_pdf(self):
        state, _, _ = self.run_day([listing(), response(500), response(content=PDF)])
        archive = self.root / "nrat-2010-partial.zip"
        verify_archive(archive, manifest_for(state))
        with zipfile.ZipFile(archive) as z:
            self.assertIn("http500.csv", z.namelist())
            self.assertIn("unavailable.csv", z.namelist())
            self.assertEqual(z.read(f"pdf/{ID}.pdf"), PDF)
            self.assertEqual(json.loads(z.read("summary.json"))["archive_status"], "partial")
        self.assertTrue((self.root / "pdf" / f"{ID}.pdf").exists())
        self.assertEqual(list(self.root.glob("*.zip.tmp")), [])

    def test_archive_tampering_missing_manifest_and_hash_are_rejected(self):
        state, _, _ = self.run_day([listing(), response(content=PDF)])
        source = self.root / "nrat-2010-partial.zip"
        with zipfile.ZipFile(source) as z:
            members = {name: z.read(name) for name in z.namelist()}
        for kind in ("missing", "pdf", "summary"):
            bad = dict(members)
            if kind == "missing":
                del bad["manifest.json"]
            elif kind == "pdf":
                bad[f"pdf/{ID}.pdf"] += b"mutated"
            else:
                bad["summary.json"] = b"{}"
            target = self.root / "bad.zip"
            with zipfile.ZipFile(target, "w") as z:
                for name, content in bad.items():
                    z.writestr(name, content)
            with self.assertRaises(ValueError):
                verify_archive(target, manifest_for(state))

    def test_archive_failure_keeps_previous_zip_and_working_pdfs(self):
        state, _, _ = self.run_day([listing(), response(content=PDF)])
        target = self.root / "nrat-2010-partial.zip"
        prior = target.read_bytes()
        with patch("nrat.archive.verify_archive", side_effect=ValueError("Artificial failed verification")):
            with self.assertRaises(ValueError):
                build_archive(self.root, state)
        self.assertEqual(target.read_bytes(), prior)
        self.assertTrue((self.root / "pdf" / f"{ID}.pdf").exists())
        self.assertEqual(list(self.root.glob(".nrat-*.zip.tmp")), [])

    def test_checkpoint_stays_incomplete_until_archive_verification_finishes(self):
        def check_then_build(work_dir, state):
            snapshot = json.loads((work_dir / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["summary"]["status"], "incomplete")
            self.assertEqual(snapshot["runs"][-1]["status"], "finalizing")
            self.assertEqual(state["summary"]["status"], "complete")
            return build_archive(work_dir, state)
        client, _, _ = self.client([listing(), response(content=PDF)])
        # Artificial one-day calendar isolates finalization without live traversal.
        with patch("nrat.collector.year_days", side_effect=lambda _: iter([D])), \
                patch("nrat.archive.build_archive", side_effect=check_then_build):
            state = collect(client, 2010, self.root)
        self.assertEqual(state["summary"]["status"], "complete")
        self.assertEqual(state["runs"][-1]["status"], "finished")
        self.assertTrue((self.root / "nrat-2010-complete.zip").exists())

    def test_report_disk_failure_cannot_leave_successful_checkpoint(self):
        client, _, _ = self.client([listing(), response(content=PDF)])
        with patch("nrat.reports.write_reports", side_effect=[PermissionError("SECRET"), None]):
            state = collect(client, 2010, self.root, day=D)
        self.assertEqual(state["runs"][-1]["status"], "failed")
        self.assertEqual(state["summary"]["status"], "incomplete")
        self.assertEqual(state["errors"][-1]["category"], "disk_error")
        self.assertNotIn("SECRET", json.dumps(state))
        load_state(self.root / "checkpoint.json", 2010)
