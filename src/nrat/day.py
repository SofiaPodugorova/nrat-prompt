"""Collect metadata for one explicit day, with durable incomplete snapshots."""

from dataclasses import asdict
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit

from .http import BASE_URL, HttpClient, search_params
from .search import fetch_page

PAGE_LIMIT = 100


def write_json_atomic(path: Path, value: dict) -> None:
    """A failed write/replace leaves the previous JSON intact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _next_page(url: str, query_date: date, current: int) -> int:
    """Recheck the parser's link; send our own fixed parameters, never its URL."""
    p = urlsplit(url)
    expected = search_params(query_date, current + 1)
    query = parse_qs(p.query, keep_blank_values=True)
    if (p.scheme != "https" or p.netloc != "nrat.ukrintei.ua" or p.path != "/searchdb/"
            or p.fragment or any(query.get(k) != [v] for k, v in expected.items())):
        raise ValueError("Next page does not preserve filters or advance by one")
    return current + 1


def collect_day(client: HttpClient, query_date: date, output: Path, *, on_snapshot=None) -> dict:
    """Start at page 1; no resume, PDF requests, or other-day traversal."""
    params = search_params(query_date, 1)
    output = Path(output)
    if output.suffix.lower() != ".json":
        raise ValueError("Daily output must have the .json extension")
    report = {
        "schema_version": 1,
        "query_date": query_date.isoformat(),
        "search": {"url": BASE_URL, "parameters": params},
        "reported_count": None,
        "reported_counts": [],
        "unique_count": 0,
        "records": [],
        "pages": [],
        "pages_processed": 0,
        "repeat_count": 0,
        "repeats": [],
        "conflicts": [],
        "http_attempt_errors": [],
        "errors": [],
        "status": "incomplete",
        "incomplete_reasons": [],
        "pagination_finished": False,
        "empty_day_confirmed": False,
        "limited": False,
        "count_ge_1000": False,
        "interrupted": False,
        "updated_at": None,
    }
    records = {}
    first_pages = {}
    failures_seen = set()

    def save():
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        snapshot = dict(report)
        if report["status"] == "incomplete" and not report["incomplete_reasons"]:
            snapshot["incomplete_reasons"] = [{"category": "in_progress", "page": None,
                                               "reason": "Collection has not yet finished"}]
        write_json_atomic(output, snapshot)
        if on_snapshot is not None:
            on_snapshot(snapshot)

    def problem(category, reason, page, **details):
        entry = {"category": category, "reason": reason, "page": page, **details}
        report["errors"].append(entry)
        report["incomplete_reasons"].append(entry)

    def attempt_failure(error, page_entry):
        key = (page_entry["page"], error.attempt)
        if key not in failures_seen:
            failures_seen.add(key)
            report["http_attempt_errors"].append({"page": key[0], "query_date": query_date.isoformat(),
                                                  **asdict(error)})
            page_entry["attempts"] = max(page_entry["attempts"], error.attempt)
            save()

    save()  # Verify storage before any network request; snapshot is incomplete.
    requested = 1
    entry = None
    try:
        while requested <= PAGE_LIMIT:
            entry = {"page": requested, "parameters": search_params(query_date, requested),
                     "status": "requesting", "attempts": 0}
            report["pages"].append(entry)
            save()
            result = fetch_page(client, query_date, requested,
                                on_failure=lambda error: attempt_failure(error, entry))
            for error in result.errors:
                attempt_failure(error, entry)
            entry["attempts"] = result.attempts
            if result.value is None:
                entry["status"] = "page_fail"
                problem("page_fail", "Requested search page could not be obtained or validated", requested)
                break
            page = result.value
            entry.update(status=result.status, reported_count=page.total_count, card_count=page.card_count,
                         next_page_url=page.next_page_url, issues=[asdict(i) for i in page.issues],
                         warnings=list(page.warnings), new_count=0, repeat_count=0, doc_ids=[])
            report["pages_processed"] += 1
            report["reported_counts"].append({"page": requested, "count": page.total_count})
            initial = report["reported_count"]
            if initial is None:
                report["reported_count"] = page.total_count
            elif initial != page.total_count:
                problem("count_changed", "Site result counter changed between pages", requested,
                        initial_count=initial, observed_count=page.total_count)
            if page.current_page != requested or page.query_date != query_date.isoformat():
                problem("pagination_mismatch", "Response page/date differs from request", requested)
            report["limited"] |= page.limited
            report["count_ge_1000"] |= page.count_ge_1000
            if page.limited:
                problem("result_limit", "Site explicitly limits the search results", requested)
            if page.count_ge_1000:
                problem("count_ge_1000", "Counter is at least 1000; completeness cannot be confirmed", requested)
            observations = list(page.records) + [i.record for i in page.issues if i.record is not None]
            for observation in observations:
                record = asdict(observation)
                doc_id = observation.doc_id
                entry["doc_ids"].append(doc_id)
                if doc_id not in records:
                    records[doc_id] = record
                    first_pages[doc_id] = requested
                    report["records"].append(record)
                    entry["new_count"] += 1
                else:
                    entry["repeat_count"] += 1
                    report["repeat_count"] += 1
                    report["repeats"].append({"doc_id": doc_id, "page": requested,
                                              "first_page": first_pages[doc_id], "observed": record})
                    changed = [field for field in ("listing_number", "source_url")
                               if records[doc_id][field] != record[field]]
                    if changed:
                        report["conflicts"].append({"doc_id": doc_id, "page": requested,
                                                    "changed_fields": changed,
                                                    "first": dict(records[doc_id]), "observed": record})
                        problem("metadata_conflict", "Same doc_id has conflicting number or source URL", requested,
                                doc_id=doc_id, changed_fields=changed)
            report["unique_count"] = len(records)
            if not page.parsing_complete:
                problem("partial", "Page contains cards that could not be fully parsed", requested)
            repeated_page = requested > 1 and bool(observations) and entry["new_count"] == 0
            if repeated_page:
                problem("page_repeat", "Nonempty page adds no new doc_id", requested)
            save()
            if (repeated_page or not page.parsing_complete or initial is not None and initial != page.total_count
                    or page.current_page != requested or page.query_date != query_date.isoformat()):
                break
            if page.next_page_url is None:
                report["pagination_finished"] = True
                report["empty_day_confirmed"] = page.empty_day_confirmed and requested == 1
                break
            try:
                next_number = _next_page(page.next_page_url, query_date, requested)
            except ValueError:
                problem("pagination_mismatch", "Next link loses parameters or does not move forward", requested)
                break
            if requested == PAGE_LIMIT:
                problem("page_limit", "100 pages processed but pagination still continues", requested)
                break
            requested = next_number
    except KeyboardInterrupt:
        report["interrupted"] = True
        if entry is not None:
            entry["status"] = "interrupted"
        problem("interrupted", "Collection was stopped; previously found records are retained", requested)
    except OSError:
        # Do not report success if storage is unavailable; previous snapshot survives.
        raise
    except Exception as exc:
        if entry is not None:
            entry["status"] = "collection_error"
        problem("collection_error", f"Collection stopped ({type(exc).__name__})", requested)
    expected = report["reported_count"]
    if expected is not None and report["unique_count"] != expected:
        problem("count_mismatch", "Unique doc_id count differs from initial site counter", requested,
                expected_count=expected, unique_count=report["unique_count"])
    if report["pagination_finished"] and not report["incomplete_reasons"]:
        report["status"] = "complete"
    save()
    return report
