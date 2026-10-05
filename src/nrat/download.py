"""Initial PDF checks only; reading every page belongs to the extraction stage."""

from dataclasses import dataclass
from datetime import date
import hashlib
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .http import AttemptFailure, HttpClient, ResponseValidationError

SOURCE_HOSTS = {"nddkr.ukrintei.ua", "dir.ukrintei.ua"}


def allowed_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.netloc.lower() in SOURCE_HOSTS
                and not p.query and not p.fragment and not p.username and not p.password
                and not any(c.isspace() for c in url))
    except (ValueError, TypeError):
        return False


def source_matches(url: str, doc_id: str) -> bool:
    if not allowed_url(url):
        return False
    match = re.fullmatch(r"/view/ok/([0-9a-fA-F]{32})/?", urlsplit(url).path)
    return match is not None and match.group(1).lower() == doc_id


@dataclass(frozen=True)
class FileInfo:
    size: int
    sha256: str


@dataclass(frozen=True)
class DownloadResult:
    status: str
    attempts: int
    file: FileInfo | None
    final_url: str | None
    errors: tuple[AttemptFailure, ...] = ()


def inspect_pdf(path: Path) -> FileInfo:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        first = stream.read(65536)
        if not first:
            raise ResponseValidationError("empty", "Empty PDF response/file")
        if not first.startswith(b"%PDF"):
            category = "html_instead_pdf" if first.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<")) else "invalid_pdf"
            raise ResponseValidationError(category, "Non-PDF content; missing %PDF signature")
        digest.update(first)
        size += len(first)
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
            size += len(chunk)
    return FileInfo(size, digest.hexdigest())


def verified_file(path: Path, expected: dict | None) -> FileInfo | None:
    if not expected:
        return None
    try:
        info = inspect_pdf(path)
    except (OSError, ResponseValidationError):
        return None
    return info if info.size == expected.get("size") and info.sha256 == expected.get("sha256") else None


def download_pdf(client: HttpClient, query_date: date, doc_id: str, source_url: str,
                 path: Path, *, expected=None, on_attempt=None, on_failure=None) -> DownloadResult:
    if not re.fullmatch(r"[0-9a-f]{32}", doc_id) or not source_matches(source_url, doc_id):
        raise ValueError("Invalid doc_id or source URL")
    path = Path(path)
    existing = verified_file(path, expected)
    if existing:
        return DownloadResult("skip", 0, existing, expected.get("final_url", source_url))
    part = path.with_suffix(".part")

    def writer(response, final_url):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with part.open("wb") as stream:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            info = inspect_pdf(part)
            os.replace(part, path)
            return info
        finally:
            part.unlink(missing_ok=True)

    try:
        return client.fetch_pdf(query_date, source_url, writer,
                                on_attempt=on_attempt, on_failure=on_failure)
    finally:
        part.unlink(missing_ok=True)
