import argparse
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import sys

from .day import collect_day
from .http import HttpClient
from .search import fetch_page


def main() -> int:
    if sys.argv[1:2] == ["collect"]:
        return collect_main(sys.argv[2:])
    if sys.argv[1:2] == ["day"]:
        return day_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description="Fetch one daily NRAT OK listing page (no PDFs)")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="Query date YYYY-MM-DD")
    parser.add_argument("--page", required=True, type=int, help="Page number, starting at 1")
    args = parser.parse_args()
    if args.page < 1:
        parser.error("--page must be positive")
    with HttpClient() as client:
        result = fetch_page(client, args.date, args.page)
    output = asdict(result)
    output["status"] = result.status
    # ASCII escapes preserve Ukrainian on all Windows console encodings.
    print(json.dumps(output, ensure_ascii=True, indent=2))
    return 1 if result.value is None else (0 if result.value.parsing_complete else 2)


def day_main(argv) -> int:
    parser = argparse.ArgumentParser(description="Collect OK metadata for one explicit day (no PDFs)")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--output", type=Path, help="JSON path; default: data/days/YYYY-MM-DD.json")
    args = parser.parse_args(argv)
    output = args.output or Path("data/days") / f"{args.date.isoformat()}.json"
    if output.suffix.lower() != ".json":
        parser.error("--output must have the .json extension")
    try:
        with HttpClient() as client:
            result = collect_day(client, args.date, output)
    except OSError as exc:
        print(f"Cannot save daily JSON ({type(exc).__name__}); collection stopped", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(output), "status": result["status"],
                      "query_date": result["query_date"], "reported_count": result["reported_count"],
                      "unique_count": result["unique_count"], "pages_processed": result["pages_processed"],
                      "repeat_count": result["repeat_count"], "incomplete_reasons": result["incomplete_reasons"]},
                     ensure_ascii=True, indent=2))
    return 130 if result["interrupted"] else (0 if result["status"] == "complete" else 2)


def collect_main(argv) -> int:
    from .collector import collect
    from .state import CheckpointError, year_days
    parser = argparse.ArgumentParser(description="Collect OK PDFs for an explicit year or diagnostic day")
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--day", type=date.fromisoformat, help="Diagnostic day within the year")
    parser.add_argument("--max-downloads", type=int, help="Limit distinct PDF operations (each up to three attempts)")
    parser.add_argument("--work-dir", type=Path, help="Resume directory; default data/YEAR")
    parser.add_argument("--no-archive", action="store_true", help="Write reports/checkpoint but omit ZIP")
    args = parser.parse_args(argv)
    try:
        next(year_days(args.year))
        if args.day is not None and args.day.year != args.year:
            parser.error("--day must belong to --year")
        if args.max_downloads is not None and args.max_downloads < 0:
            parser.error("--max-downloads must be nonnegative")
    except ValueError as exc:
        parser.error(str(exc))
    work_dir = args.work_dir or Path("data") / str(args.year)
    try:
        with HttpClient() as client:
            state = collect(client, args.year, work_dir, day=args.day, max_downloads=args.max_downloads,
                            make_archive=not args.no_archive)
    except CheckpointError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Storage failure ({type(exc).__name__}); prior atomic checkpoint retained", file=sys.stderr)
        return 1
    print(json.dumps({"work_dir": str(work_dir), "summary": state["summary"], "run": state["runs"][-1]},
                     ensure_ascii=True, indent=2))
    run_status = state["runs"][-1]["status"]
    return 130 if run_status == "interrupted" else (1 if run_status == "failed" else
                                                   0 if state["summary"]["status"] == "complete" else 2)


if __name__ == "__main__":
    sys.exit(main())
