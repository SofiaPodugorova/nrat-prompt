"""Resume a year/day of OK downloads without resetting history or retry budgets."""

from dataclasses import asdict
from datetime import date
from pathlib import Path
import uuid

from .day import collect_day
from .download import download_pdf, verified_file
from .http import search_params
from .state import load_state, now, save_state, year_days


def refresh_summary(state, *, restricted=False, stopped=False):
    days = [d.isoformat() for d in year_days(state["year"])]
    unfinished = []
    search_unfinished = []
    documents = state["documents"]
    for d in days:
        item = state["days"].get(d)
        search_ok = item is not None and item["listing_complete"]
        processing_ok = item is not None and all(documents[i]["status"] in {"ok", "skip", "notpdf"}
                                                for i in item["doc_ids"])
        if not search_ok:
            search_unfinished.append(d)
        if not search_ok or not processing_ok:
            unfinished.append(d)
        if item is not None:
            item["processing_complete"] = processing_ok
            item["status"] = "complete" if search_ok and processing_ok else "incomplete"
    unfinished_docs = [i for i, doc in documents.items() if doc["status"] not in {"ok", "skip", "notpdf"}]
    missing = [i for i, doc in documents.items() if doc["status"] == "notpdf"]
    present = [i for i, doc in documents.items() if doc["status"] in {"ok", "skip"} and doc["file"]]
    search_complete = not search_unfinished
    processing_complete = not unfinished_docs
    all_present = len(present) == len(documents)
    complete = search_complete and processing_complete and all_present and not restricted and not stopped
    reasons = []
    if search_unfinished:
        reasons.append("search_incomplete")
    if unfinished_docs:
        reasons.append("downloads_incomplete")
    if missing:
        reasons.append("confirmed_missing_pdfs")
    if restricted:
        reasons.append("restricted_run")
    if stopped:
        reasons.append("run_stopped")
    summary = {"year": state["year"], "status": "complete" if complete else "incomplete",
               "archive_status": "complete" if complete else "partial", "search_complete": search_complete,
               "processing_complete": processing_complete, "all_found_pdfs_present": all_present,
               "accounted_collection_complete": search_complete and processing_complete and not restricted and not stopped,
               "restricted_run": restricted, "stopped": stopped, "expected_days": len(days),
               "days_with_listing": len(state["days"]), "found": len(documents),
               "downloaded_or_verified": len(present), "downloaded_current_status": sum(d["status"] == "ok" for d in documents.values()),
               "skipped_current_status": sum(d["status"] == "skip" for d in documents.values()),
               "missing": len(missing), "unfinished_documents": len(unfinished_docs),
               "unfinished_doc_ids": unfinished_docs, "unavailable_doc_ids": missing,
               "unfinished_dates": unfinished, "search_unfinished_dates": search_unfinished,
               "incomplete_reasons": reasons}
    state["summary"] = summary
    return summary


def collect(client, year: int, work_dir: Path, *, day: date | None = None,
            max_downloads: int | None = None, make_archive=True):
    calendar = list(year_days(year))
    if day is not None and (type(day) is not date or day.year != year):
        raise ValueError("--day must belong to --year")
    if max_downloads is not None and (type(max_downloads) is not int or max_downloads < 0):
        raise ValueError("max_downloads must be a nonnegative integer")
    work_dir = Path(work_dir)
    checkpoint = work_dir / "checkpoint.json"
    state = load_state(checkpoint, year)
    run = {"run_id": uuid.uuid4().hex, "started_at": now(), "finished_at": None,
           "scope": "day" if day else "year", "day": day.isoformat() if day else None,
           "max_downloads": max_downloads, "download_operations": 0, "status": "running"}
    state["runs"].append(run)
    restricted = day is not None or max_downloads is not None
    attempted = set()
    events_seen = set()

    def persist():
        refresh_summary(state, restricted=restricted, stopped=run["status"] in {"interrupted", "failed"})
        # Never declare the active operation completed in an intermediate snapshot.
        if run["status"] in {"running", "finalizing"}:
            state["summary"]["status"] = "incomplete"
            state["summary"]["archive_status"] = "partial"
        save_state(checkpoint, state)

    def log(domain, category, reason, query_date=None, doc=None, **extra):
        event = {"time": now(), "run_id": run["run_id"], "domain": domain, "category": category,
                 "reason": reason, "query_date": query_date, "doc_id": doc["doc_id"] if doc else None,
                 "listing_number": doc["metadata"]["listing_number"] if doc else None, **extra}
        state["errors"].append(event)
        return event

    def ingest(snapshot):
        d = snapshot["query_date"]
        item = state["days"].setdefault(d, {"doc_ids": []})
        observations = snapshot["records"] + [r["observed"] for r in snapshot["repeats"]]
        for record in observations:
            identifier = record["doc_id"]
            doc = state["documents"].setdefault(identifier, {
                "doc_id": identifier, "metadata": dict(record), "variants": [], "discovery_dates": [],
                "status": "pending", "file": None, "total_attempts": 0, "http500_count": 0,
                "first500_at": None, "last500_at": None, "last_error": None,
            })
            if record not in doc["variants"]:
                doc["variants"].append(dict(record))
            if d not in doc["discovery_dates"]:
                doc["discovery_dates"].append(d)
            if identifier not in item["doc_ids"]:
                item["doc_ids"].append(identifier)
        item.update(listing=snapshot, listing_complete=snapshot["status"] == "complete")
        for error in snapshot["http_attempt_errors"]:
            key = (d, "attempt", error["page"], error["attempt"])
            if key not in events_seen:
                events_seen.add(key)
                log("search", error["category"], error["reason"], d, url=snapshot["search"]["url"],
                    parameters=search_params(date.fromisoformat(d), error["page"]),
                    attempt=error["attempt"], http_code=error["http_code"],
                    exception_type=error.get("exception_type"), page=error["page"])
        for error in snapshot["errors"]:
            key = (d, "problem", error["page"], error["category"], error["reason"])
            if key not in events_seen:
                events_seen.add(key)
                log("search", error["category"], error["reason"], d, page=error["page"],
                    url=snapshot["search"]["url"])
        persist()

    def process(identifier, d):
        doc = state["documents"][identifier]
        path = work_dir / "pdf" / f"{identifier}.pdf"
        if identifier in attempted or doc["status"] == "notpdf":
            return True
        if verified_file(path, doc["file"]):
            doc["status"] = "skip"
            persist()
            return True
        if path.exists() or doc["file"]:
            log("pdf", "existing_file_invalid", "Existing PDF missing, invalid, or differs from saved size/hash",
                d, doc, url=doc["metadata"]["source_url"], attempt=0, http_code=None)
        doc["file"] = None
        doc["status"] = "pending"
        if max_downloads is not None and run["download_operations"] >= max_downloads:
            run["status"] = "limited"
            persist()
            return False
        attempted.add(identifier)  # Do not reset the budget on another discovery/date.
        run["download_operations"] += 1
        persist()

        def on_attempt(attempt):
            doc["total_attempts"] += 1
            doc["status"] = "pending"
            persist()

        def on_failure(error, final_url):
            event = log("pdf", error.category, error.reason, d, doc,
                        url=doc["metadata"]["source_url"], final_url=final_url,
                        attempt=error.attempt, http_code=error.http_code, exception_type=error.exception_type)
            doc["last_error"] = event
            if error.http_code == 500:
                doc["http500_count"] += 1
                doc["first500_at"] = doc["first500_at"] or event["time"]
                doc["last500_at"] = event["time"]
            persist()

        result = download_pdf(client, date.fromisoformat(d), identifier, doc["metadata"]["source_url"],
                              path, on_attempt=on_attempt, on_failure=on_failure)
        doc["status"] = result.status
        if result.file:
            doc["file"] = {"path": f"pdf/{identifier}.pdf", **asdict(result.file), "final_url": result.final_url}
        persist()
        return True

    persist()  # Test checkpoint writing before network access.
    try:
        # Revalidate every claimed PDF, including dates skipped on resume.
        for identifier, doc in state["documents"].items():
            if doc["status"] in {"ok", "skip"}:
                if not verified_file(work_dir / "pdf" / f"{identifier}.pdf", doc["file"]):
                    doc["status"] = "pending"
                    log("pdf", "existing_file_invalid", "Saved PDF no longer passes size/hash/signature checks",
                        doc["metadata"]["query_date"], doc, url=doc["metadata"]["source_url"], attempt=0, http_code=None)
        persist()
        for query_date in ([day] if day is not None else calendar):
            d = query_date.isoformat()
            item = state["days"].get(d)
            if item is None or not item["listing_complete"]:
                snapshot = collect_day(client, query_date, work_dir / "days" / f"{d}.json", on_snapshot=ingest)
                if snapshot["interrupted"]:
                    raise KeyboardInterrupt
            item = state["days"][d]
            for identifier in item["doc_ids"]:
                if not process(identifier, d):
                    break
            if run["status"] == "limited":
                break
        if run["status"] == "running":
            run["status"] = "finalizing"
    except KeyboardInterrupt:
        run["status"] = "interrupted"
        log("run", "interrupted", "Stopped by Ctrl+C; resume using the same work directory")
    except OSError as exc:
        run["status"] = "failed"
        log("storage", "disk_error", f"Storage operation failed ({type(exc).__name__})", exception_type=type(exc).__name__)
        # If storage has failed completely, propagate and preserve the prior atomic checkpoint.
        persist()
        raise
    except Exception as exc:
        run["status"] = "failed"
        log("run", "collection_error", f"Collector stopped ({type(exc).__name__})", exception_type=type(exc).__name__)
    persist()
    # Export the candidate completeness, but keep the on-disk checkpoint incomplete
    # until reports/ZIP have succeeded. A power loss during finalization is resumable.
    refresh_summary(state, restricted=restricted, stopped=run["status"] in {"interrupted", "failed"})
    from .reports import write_reports
    phase = "storage"
    export_failed = False
    try:
        write_reports(work_dir, state)
        if make_archive:
            from .archive import build_archive
            phase = "archive"
            archive = build_archive(work_dir, state)
            run["archive"] = str(archive)
    except KeyboardInterrupt:
        export_failed = True
        run["status"] = "interrupted"
        log(phase, "interrupted", "Report/archive creation interrupted; working PDFs retained")
    except (OSError, ValueError) as exc:
        export_failed = True
        run["status"] = "failed"
        log(phase, "archive_fail" if phase == "archive" else "disk_error",
            f"Report/archive validation or write failed ({type(exc).__name__})", exception_type=type(exc).__name__)
    if run["status"] == "finalizing":
        run["status"] = "finished"
    run["finished_at"] = now()
    persist()
    if export_failed:
        write_reports(work_dir, state)
    return state
