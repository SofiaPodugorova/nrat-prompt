"""Sequential HTTP transport with explicit, bounded retries."""

from dataclasses import dataclass
from datetime import date
from threading import Lock
import time
from typing import Callable, Generic, TypeVar
from urllib.parse import urljoin

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
    exception_type: str | None = None


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
        self._kind_end = {}
        self._lock = Lock()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._session.close()

    def _wait(self, query_date: date, retry_delay: int, kind="search"):
        remaining = 0.0
        if self._last_end is not None:
            if query_date != self._last_date:
                remaining = max(0.0, 5 - (self._clock() - self._last_end))
            else:
                remaining = max(0.0, (3 if kind == "search" else 1)
                                - (self._clock() - self._last_end))
        if kind in self._kind_end:
            remaining = max(remaining, (3 if kind == "search" else 1)
                            - (self._clock() - self._kind_end[kind]))
        delay = max(remaining, retry_delay)
        if delay:
            self._sleep(delay)

    def fetch(self, query_date: date, page: int, validator: Callable[[str], T], *,
              on_failure: Callable[[AttemptFailure], None] | None = None) -> HttpResult[T]:
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
                    errors.append(AttemptFailure(attempt, "page_fail", reason, code, type(exc).__name__))
                finally:
                    self._last_end = self._clock()
                    self._last_date = query_date
                    self._kind_end["search"] = self._last_end
                    if response is not None:
                        response.close()
                if on_failure is not None:
                    on_failure(errors[-1])
                if not retryable or attempt == MAX_ATTEMPTS:
                    return HttpResult(None, attempt, tuple(errors))
                retry_delay = retry_wait(15 if attempt == 1 else 30, retry_after)
        raise AssertionError("unreachable")

    def fetch_pdf(self, query_date: date, source_url: str, writer, *,
                  on_attempt=None, on_failure=None):
        """One retry budget; each attempt includes at most three safe redirects."""
        from .download import DownloadResult, allowed_url
        if not allowed_url(source_url):
            raise ValueError("PDF source URL is not an allowed HTTPS URL")
        with self._lock:
            errors = []
            retry_delay = 0
            for attempt in range(1, MAX_ATTEMPTS + 1):
                self._wait(query_date, retry_delay, "pdf")
                if on_attempt is not None:
                    on_attempt(attempt)
                url = source_url
                code = None
                retryable = True
                retry_after = None
                try:
                    for hop in range(4):
                        if hop:
                            self._wait(query_date, 0, "pdf")
                        response = None
                        try:
                            response = self._session.get(url, timeout=(30, 120),
                                                         allow_redirects=False, stream=True)
                            code = response.status_code
                            retry_after = response.headers.get("Retry-After")
                            if code in {301, 302, 303, 307, 308}:
                                retryable = False
                                location = response.headers.get("Location")
                                try:
                                    destination = urljoin(url, location or "")
                                except ValueError:
                                    raise ResponseValidationError("redirect_invalid", "Malformed PDF redirect") from None
                                if hop == 3 or not location or not allowed_url(destination):
                                    raise ResponseValidationError("redirect_invalid", "Unsafe or excessive PDF redirect")
                                url = destination
                                retryable = True
                                continue
                            if code != 200:
                                retryable = code in {408, 425, 429} or 500 <= code <= 599
                                category = "notpdf" if code in {404, 410} else ("server500" if code == 500 else "http_error")
                                raise ResponseValidationError(category, f"PDF source returned HTTP {code}")
                            value = writer(response, url)
                            return DownloadResult("ok", attempt, value, url, tuple(errors))
                        finally:
                            self._last_end = self._clock()
                            self._last_date = query_date
                            self._kind_end["pdf"] = self._last_end
                            if response is not None:
                                response.close()
                except ResponseValidationError as exc:
                    error = AttemptFailure(attempt, exc.category, exc.reason, code)
                except requests.exceptions.RequestException as exc:
                    error = AttemptFailure(attempt, "network_error", f"PDF request failed ({type(exc).__name__})",
                                           code, type(exc).__name__)
                except OSError as exc:
                    retryable = False
                    error = AttemptFailure(attempt, "disk_error", f"PDF file operation failed ({type(exc).__name__})",
                                           code, type(exc).__name__)
                errors.append(error)
                if on_failure is not None:
                    on_failure(error, url)
                if not retryable or attempt == MAX_ATTEMPTS:
                    status = error.category if error.category in {"notpdf", "server500", "empty"} else "fail"
                    return DownloadResult(status, attempt, None, url, tuple(errors))
                retry_delay = retry_wait(15 if attempt == 1 else 30, retry_after)
        raise AssertionError("unreachable")
