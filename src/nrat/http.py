"""Sequential HTTP transport with explicit, bounded retries."""

from dataclasses import dataclass
from datetime import date
from threading import Lock
import time
from typing import Callable, Generic, TypeVar

import requests

BASE_URL = "https://nrat.ukrintei.ua/searchdb/"
TIMEOUT = (30, 90)
MAX_ATTEMPTS = 3
T = TypeVar("T")


class ResponseValidationError(ValueError):
    def __init__(self, category: str, reason: str):
        super().__init__(reason)
        self.category = category
        self.reason = reason


@dataclass(frozen=True)
class AttemptFailure:
    attempt: int
    category: str
    reason: str
    http_code: int | None


@dataclass(frozen=True)
class HttpResult(Generic[T]):
    value: T | None
    attempts: int
    errors: tuple[AttemptFailure, ...]

    @property
    def status(self) -> str:
        if self.value is None:
            return "page_fail"
        return "ok" if getattr(self.value, "parsing_complete", True) else "partial"


def search_params(query_date: date, page: int) -> dict[str, str]:
    if not isinstance(query_date, date) or type(query_date) is not date:
        raise ValueError("query_date must be a datetime.date, without a time")
    if type(page) is not int or page < 1:
        raise ValueError("page must be a positive integer")
    return {
        "typeSearch2": "ok",
        "dateFromSearch": query_date.isoformat(),
        "dateToSearch": query_date.isoformat(),
        "pa": str(page),
        "sortOrder": "registration_date",
        "sortDir": "asc",
        "tab": "big",
    }


def retry_wait(base: int, retry_after: str | None) -> int:
    """Only numeric delay-seconds; HTTP-date values are not interpreted."""
    value = (retry_after or "").strip()
    if not value or not value.isascii() or not value.isdecimal():
        return base
    digits = value.lstrip("0") or "0"
    seconds = 300 if len(digits) > 3 else int(digits)
    return min(300, max(base, seconds))


class HttpClient:
    def __init__(self, *, session=None, sleep=time.sleep, clock=time.monotonic):
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._last_end: float | None = None
        self._last_date: date | None = None
        self._lock = Lock()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._session.close()

    def _wait(self, query_date: date, retry_delay: int):
        remaining = 0.0
        if self._last_end is not None:
            minimum = 3 if query_date == self._last_date else 5
            remaining = max(0.0, minimum - (self._clock() - self._last_end))
        delay = max(remaining, retry_delay)
        if delay:
            self._sleep(delay)

    def fetch(self, query_date: date, page: int, validator: Callable[[str], T]) -> HttpResult[T]:
        params = search_params(query_date, page)
        # One client also serializes callers; it never follows discovered links.
        with self._lock:
            errors: list[AttemptFailure] = []
            retry_delay = 0
            for attempt in range(1, MAX_ATTEMPTS + 1):
                self._wait(query_date, retry_delay)
                response = None
                code = None
                retryable = True
                retry_after = None
                try:
                    response = self._session.get(
                        BASE_URL, params=params, timeout=TIMEOUT, allow_redirects=False
                    )
                    code = response.status_code
                    retry_after = response.headers.get("Retry-After")
                    if code != 200:
                        retryable = code in {408, 425, 429} or 500 <= code <= 599
                        raise ResponseValidationError("page_fail", f"Search page returned HTTP {code}")
                    media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if media not in {"text/html", "application/xhtml+xml"}:
                        raise ResponseValidationError("page_fail", "Search response is not HTML")
                    if not response.content.strip():
                        raise ResponseValidationError("page_fail", "Empty search response")
                    try:
                        html = response.content.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        raise ResponseValidationError("page_fail", "HTML is not valid UTF-8") from None
                    value = validator(html)
                    return HttpResult(value, attempt, tuple(errors))
                except ResponseValidationError as exc:
                    errors.append(AttemptFailure(attempt, exc.category, exc.reason, code))
                except requests.exceptions.RequestException as exc:
                    # Raw exception messages may contain credentials/headers/URLs.
                    if isinstance(exc, requests.exceptions.Timeout):
                        reason = f"Network timeout ({type(exc).__name__})"
                    else:
                        reason = f"Network request failed ({type(exc).__name__})"
                    errors.append(AttemptFailure(attempt, "page_fail", reason, code))
                finally:
                    self._last_end = self._clock()
                    self._last_date = query_date
                    if response is not None:
                        response.close()
                if not retryable or attempt == MAX_ATTEMPTS:
                    return HttpResult(None, attempt, tuple(errors))
                retry_delay = retry_wait(15 if attempt == 1 else 30, retry_after)
        raise AssertionError("unreachable")
