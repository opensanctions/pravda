"""Downloaded response bodies are folded back into the HAR manifest."""

import json
import zipfile
from pathlib import Path

import pytest
from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper
from fsspec.implementations.local import LocalFileSystem

import pravda.storage as storage
from pravda.capture import DownloadedBody
from pravda.http_archive import capture_http_archive
from pravda.storage import content_prefix

PAGE_URL = "https://example.com/doc.pdf"


@pytest.fixture()
def storage_tmp(tmp_path, monkeypatch):
    # Point the storage backend at a throwaway local dir.
    monkeypatch.setattr(storage, "fs", AsyncFileSystemWrapper(LocalFileSystem()))
    monkeypatch.setattr(storage, "_base_path", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_download_body_folded_into_har(storage_tmp):
    """A downloaded body patches its manifest entry into a real content._file."""
    pdf_bytes = b"%PDF-1.7 real-ish bytes\n%EOF"
    # One normal entry (body present in the zip) and one download entry whose
    # body Playwright never recorded (size -1, no _file) — mirroring reality.
    manifest = {
        "log": {
            "entries": [
                {
                    "request": {"method": "GET", "url": "https://example.com/"},
                    "response": {
                        "status": 200,
                        "content": {
                            "mimeType": "text/html",
                            "size": 5,
                            "_file": "abc.html",
                        },
                    },
                },
                {
                    "request": {"method": "GET", "url": PAGE_URL},
                    "response": {
                        "status": 200,
                        "content": {"mimeType": "application/pdf", "size": -1},
                    },
                },
            ]
        }
    }

    zip_path = storage_tmp / "record.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("har.har", json.dumps(manifest))
        archive.writestr("abc.html", b"hello")

    capture = await capture_http_archive(
        zip_path,
        PAGE_URL,
        download=DownloadedBody(url=PAGE_URL, data=pdf_bytes),
    )

    prefix = Path(content_prefix(PAGE_URL))
    stored = json.loads((prefix / capture.http_archive).read_bytes())
    entries = stored["log"]["entries"]

    # The normal entry is untouched.
    assert entries[0]["response"]["content"]["_file"] == "abc.html"

    # The download entry now references its body like any other entry.
    pdf_entry = entries[1]["response"]["content"]
    assert pdf_entry["mimeType"] == "application/pdf"
    assert pdf_entry["size"] == len(pdf_bytes)
    assert pdf_entry["_file"].endswith(".pdf")
    assert (prefix / pdf_entry["_file"]).read_bytes() == pdf_bytes


@pytest.mark.asyncio
async def test_download_without_match_stored_as_orphan(storage_tmp):
    """When no bodyless entry matches, the bytes are still preserved."""
    pdf_bytes = b"%PDF-1.7 orphan\n%EOF"
    manifest = {"log": {"entries": []}}

    zip_path = storage_tmp / "record.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("har.har", json.dumps(manifest))

    await capture_http_archive(
        zip_path,
        PAGE_URL,
        download=DownloadedBody(url=PAGE_URL, data=pdf_bytes),
    )

    prefix = Path(content_prefix(PAGE_URL))
    orphan = next(prefix.glob("*.pdf"))
    assert orphan.read_bytes() == pdf_bytes
