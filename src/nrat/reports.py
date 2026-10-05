"""Derived reports; authoritative attempt history stays in checkpoint.json."""

import csv
import json
import os
from pathlib import Path
import tempfile

from .day import write_json_atomic

REPORT_FILES = ("manifest.json", "summary.json", "unavailable.csv", "unresolved.csv", "unfinished_dates.csv",
                "errors.csv", "search_errors.csv", "http500.csv")


def write_csv_atomic(path, fields, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                                 for k, v in row.items()})
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def manifest_for(state):
    return {"schema_version": 1, "year": state["year"], "card_type": "ok", "key": "doc_id",
            "query_date_note": "Search query date, not an extracted card registration date",
            "summary": state["summary"], "documents": state["documents"],
            "files": [{"doc_id": identifier, **doc["file"]} for identifier, doc in state["documents"].items()
                      if doc["status"] in {"ok", "skip"} and doc["file"]]}


def write_reports(work_dir, state):
    work_dir = Path(work_dir)
    write_json_atomic(work_dir / "manifest.json", manifest_for(state))
    write_json_atomic(work_dir / "summary.json", state["summary"])
    doc_fields = ["doc_id", "listing_number", "source_url", "title", "description", "query_date",
                  "discovery_dates", "variants", "status", "total_attempts", "http500_count",
                  "first500_at", "last500_at", "http_code", "final_url", "size", "sha256", "last_error"]
    rows = [{**doc["metadata"], **{k: doc.get(k) for k in doc_fields if k not in doc["metadata"]}}
            for doc in state["documents"].values()]
    for row in rows:
        doc = state["documents"][row["doc_id"]]
        row["http_code"] = (doc["last_error"] or {}).get("http_code")
        for field in ("final_url", "size", "sha256"):
            row[field] = (doc["file"] or {}).get(field)
    write_csv_atomic(work_dir / "unavailable.csv", doc_fields, (r for r in rows if r["status"] == "notpdf"))
    write_csv_atomic(work_dir / "unresolved.csv", doc_fields,
                     (r for r in rows if r["status"] not in {"ok", "skip", "notpdf"}))
    write_csv_atomic(work_dir / "http500.csv", doc_fields, (r for r in rows if r["http500_count"] > 0))
    error_fields = ["time", "run_id", "domain", "query_date", "category", "http_code", "exception_type",
                    "listing_number", "doc_id", "attempt", "page", "reason", "url", "final_url", "parameters"]
    write_csv_atomic(work_dir / "errors.csv", error_fields, state["errors"])
    write_csv_atomic(work_dir / "search_errors.csv", error_fields,
                     (e for e in state["errors"] if e["domain"] == "search"))
    day_rows = []
    for d in state["summary"]["unfinished_dates"]:
        item = state["days"].get(d, {})
        day_rows.append({"query_date": d, "listing_complete": item.get("listing_complete", False),
                         "processing_complete": item.get("processing_complete", False),
                         "listing_issues": item.get("listing", {}).get("incomplete_reasons", []),
                         "unfinished_doc_ids": [i for i in item.get("doc_ids", [])
                                                if state["documents"][i]["status"] not in {"ok", "skip", "notpdf"}]})
    write_csv_atomic(work_dir / "unfinished_dates.csv",
                     ["query_date", "listing_complete", "processing_complete", "listing_issues", "unfinished_doc_ids"], day_rows)
