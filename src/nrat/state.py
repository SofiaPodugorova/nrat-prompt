"""Checkpoint schema validation: invalid state is never silently reset."""

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re

from .day import write_json_atomic
from .download import allowed_url, source_matches
from .http import search_params

STATUSES = {"pending", "ok", "skip", "notpdf", "server500", "empty", "fail"}
RECORD_FIELDS = {"doc_id", "listing_number", "source_url", "title", "description", "query_date"}


class CheckpointError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def year_days(year):
    if type(year) is not int or not 1 <= year <= 9998:
        raise ValueError("year must be an integer from 1 to 9998")
    current = date(year, 1, 1)
    end = date(year + 1, 1, 1)
    while current < end:
        yield current
        current += timedelta(days=1)


def valid_record(record, year, doc_id=None):
    if not isinstance(record, dict) or not RECORD_FIELDS <= record.keys():
        return False
    if any(not isinstance(record[f], str) for f in RECORD_FIELDS):
        return False
    identifier = record["doc_id"]
    if not re.fullmatch(r"[0-9a-f]{32}", identifier) or doc_id is not None and identifier != doc_id:
        return False
    try:
        day = date.fromisoformat(record["query_date"])
    except ValueError:
        return False
    return day.year == year and source_matches(record["source_url"], identifier)


def validate_state(state, year):
    """Validate identity, paths, collections and the fields needed for safe resume."""
    try:
        if (not isinstance(state, dict) or type(state["schema_version"]) is not int
                or state["schema_version"] != 1 or state["year"] != year
                or type(state["year"]) is not int):
            raise ValueError()
        for field in ("documents", "days"):
            if not isinstance(state[field], dict):
                raise ValueError()
        for field in ("errors", "runs"):
            if not isinstance(state[field], list) or any(not isinstance(x, dict) for x in state[field]):
                raise ValueError()
        for run in state["runs"]:
            if (not re.fullmatch(r"[0-9a-f]{32}", run["run_id"])
                    or run["status"] not in {"running", "finalizing", "finished", "limited", "interrupted", "failed"}):
                raise ValueError()
            datetime.fromisoformat(run["started_at"])
            if run["finished_at"] is not None:
                datetime.fromisoformat(run["finished_at"])
        known_runs = {r["run_id"] for r in state["runs"]}
        pdf500 = {}
        for error in state["errors"]:
            datetime.fromisoformat(error["time"])
            if (error["run_id"] not in known_runs or error["domain"] not in {"pdf", "search", "run", "storage", "archive"}
                    or not isinstance(error["reason"], str) or not isinstance(error["category"], str)):
                raise ValueError()
            if error["doc_id"] is not None and error["doc_id"] not in state["documents"]:
                raise ValueError()
            if error["query_date"] is not None and date.fromisoformat(error["query_date"]).year != year:
                raise ValueError()
            if error["domain"] == "pdf" and error.get("http_code") == 500:
                pdf500.setdefault(error["doc_id"], []).append(error["time"])
        for identifier, doc in state["documents"].items():
            if (not re.fullmatch(r"[0-9a-f]{32}", identifier) or not isinstance(doc, dict)
                    or doc["doc_id"] != identifier or doc["status"] not in STATUSES
                    or not valid_record(doc["metadata"], year, identifier)
                    or not isinstance(doc["variants"], list) or not doc["variants"]
                    or any(not valid_record(v, year, identifier) for v in doc["variants"])):
                raise ValueError()
            if not isinstance(doc["discovery_dates"], list) or not doc["discovery_dates"]:
                raise ValueError()
            if any(date.fromisoformat(d).year != year for d in doc["discovery_dates"]):
                raise ValueError()
            if set(doc["discovery_dates"]) != {v["query_date"] for v in doc["variants"]}:
                raise ValueError()
            for field in ("total_attempts", "http500_count"):
                if type(doc[field]) is not int or doc[field] < 0:
                    raise ValueError()
            if doc["http500_count"] > doc["total_attempts"]:
                raise ValueError()
            for field in ("first500_at", "last500_at"):
                if doc[field] is not None:
                    datetime.fromisoformat(doc[field])
            info = doc["file"]
            if info is not None:
                if (not isinstance(info, dict) or info["path"] != f"pdf/{identifier}.pdf"
                        or type(info["size"]) is not int or info["size"] <= 0
                        or not re.fullmatch(r"[0-9a-f]{64}", info["sha256"])
                        or not allowed_url(info["final_url"])):
                    raise ValueError()
            if doc["status"] in {"ok", "skip"} and info is None:
                raise ValueError()
            if doc["http500_count"] and (doc["first500_at"] is None or doc["last500_at"] is None):
                raise ValueError()
            times = pdf500.get(identifier, [])
            if (len(times) != doc["http500_count"]
                    or times and (doc["first500_at"] != times[0] or doc["last500_at"] != times[-1])):
                raise ValueError()
        for day, item in state["days"].items():
            if date.fromisoformat(day).year != year or not isinstance(item, dict):
                raise ValueError()
            if type(item["listing_complete"]) is not bool or not isinstance(item["doc_ids"], list):
                raise ValueError()
            if any(i not in state["documents"] for i in item["doc_ids"]):
                raise ValueError()
            report = item["listing"]
            if (not isinstance(report, dict) or report["query_date"] != day
                    or report["status"] not in {"complete", "incomplete"}
                    or item["listing_complete"] != (report["status"] == "complete")
                    or not isinstance(report["records"], list)
                    or any(not valid_record(r, year) for r in report["records"])):
                raise ValueError()
            if item["listing_complete"]:
                ids = {r["doc_id"] for r in report["records"]}
                if (report["unique_count"] != len(ids) or report["reported_count"] != len(ids)
                        or len(report["records"]) != len(ids)
                        or not report["pagination_finished"] or report["incomplete_reasons"]
                        or report["limited"] or report["count_ge_1000"]
                        or report["search"]["parameters"] != search_params(date.fromisoformat(day), 1)
                        or len(ids) == 0 and not report["empty_day_confirmed"]
                        or not ids <= set(item["doc_ids"])):
                    raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CheckpointError("Checkpoint is damaged, incompatible, or belongs to another year; nothing was reset") from None


def load_state(path: Path, year):
    if not path.exists():
        if (any((path.parent / name).exists() for name in ("manifest.json", "errors.csv", "http500.csv"))
                or any((path.parent / "pdf").glob("*.pdf")) or any((path.parent / "days").glob("*.json"))):
            raise CheckpointError("Checkpoint is missing but collection data exists; history was not reset")
        return {"schema_version": 1, "year": year, "documents": {}, "days": {}, "errors": [],
                "runs": [], "updated_at": now(), "summary": {}}
    try:
        with path.open(encoding="utf-8") as stream:
            def no_duplicates(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate JSON key")
                    result[key] = value
                return result
            state = json.load(stream, object_pairs_hook=no_duplicates,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise CheckpointError("Checkpoint JSON is damaged; nothing was reset") from None
    validate_state(state, year)
    return state


def save_state(path, state):
    state["updated_at"] = now()
    write_json_atomic(path, state)
