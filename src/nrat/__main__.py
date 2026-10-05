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


if __name__ == "__main__":
    sys.exit(main())
