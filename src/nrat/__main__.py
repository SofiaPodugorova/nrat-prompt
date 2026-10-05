import argparse
from dataclasses import asdict
from datetime import date
import json
import sys

from .http import HttpClient
from .search import fetch_page


def main() -> int:
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


if __name__ == "__main__":
    sys.exit(main())
