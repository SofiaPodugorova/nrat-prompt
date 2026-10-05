"""Build and independently verify a temporary ZIP before publishing it."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile

from .download import verified_file
from .reports import REPORT_FILES, manifest_for, write_reports


def verify_archive(path: Path, expected_manifest: dict):
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            expected_files = {f["path"]: f for f in expected_manifest["files"]}
            if (len(names) != len(set(names)) or set(names) != set(REPORT_FILES) | set(expected_files)
                    or archive.testzip() is not None):
                raise ValueError("ZIP members/CRC do not match the manifest and reports")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            summary = json.loads(archive.read("summary.json").decode("utf-8"))
            if manifest != expected_manifest or summary != expected_manifest["summary"]:
                raise ValueError("ZIP manifest/summary differs from the intended snapshot")
            for name, info in expected_files.items():
                size = 0
                digest = hashlib.sha256()
                with archive.open(name) as stream:
                    first = stream.read(65536)
                    if not first.startswith(b"%PDF"):
                        raise ValueError("ZIP contains an invalid PDF")
                    digest.update(first)
                    size += len(first)
                    for chunk in iter(lambda: stream.read(65536), b""):
                        digest.update(chunk)
                        size += len(chunk)
                if size != info["size"] or digest.hexdigest() != info["sha256"]:
                    raise ValueError("ZIP PDF size/SHA-256 mismatch")
    except (zipfile.BadZipFile, KeyError, UnicodeError) as exc:
        raise ValueError("ZIP cannot be verified") from exc


def build_archive(work_dir: Path, state: dict) -> Path:
    work_dir = Path(work_dir)
    manifest = manifest_for(state)
    for info in manifest["files"]:
        if not verified_file(work_dir / info["path"], info):
            doc = state["documents"][info["doc_id"]]
            doc["status"] = "pending"
            raise ValueError("Working PDF failed verification before archiving")
    write_reports(work_dir, state)
    temporary = None
    target = work_dir / f'nrat-{state["year"]}-{state["summary"]["archive_status"]}.zip'
    try:
        with tempfile.NamedTemporaryFile(dir=work_dir, prefix=".nrat-", suffix=".zip.tmp", delete=False) as stream:
            temporary = Path(stream.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for name in REPORT_FILES:
                archive.write(work_dir / name, name)
            for info in manifest["files"]:
                archive.write(work_dir / info["path"], info["path"])
        verify_archive(temporary, manifest)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
