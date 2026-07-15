"""Downloaded response bodies are folded back into the HAR manifest."""

import asyncio
import json
import zipfile
from pathlib import Path

import pytest

import pravda.http_archive as har_module
from pravda.capture import DownloadedBody
from pravda.http_archive import capture_http_archive
from pravda.storage import Storage

PAGE_URL = "https://example.com/doc.pdf"


@pytest.mark.asyncio
async def test_download_body_folded_into_har(storage: Storage):
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

    zip_path = Path(storage.base_path) / "record.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("har.har", json.dumps(manifest))
        archive.writestr("abc.html", b"hello")

    manifest = await capture_http_archive(
        zip_path,
        PAGE_URL,
        storage,
        download=DownloadedBody(
            url=PAGE_URL, data=pdf_bytes, suggested_filename="doc.pdf"
        ),
    )

    prefix = Path(storage.content_prefix(PAGE_URL))
    entries = manifest["log"]["entries"]

    # The normal entry is untouched.
    assert entries[0]["response"]["content"]["_file"] == "abc.html"

    # The download entry now references its body like any other entry.
    pdf_entry = entries[1]["response"]["content"]
    assert pdf_entry["mimeType"] == "application/pdf"
    assert pdf_entry["size"] == len(pdf_bytes)
    assert pdf_entry["_file"].endswith(".pdf")
    assert (prefix / pdf_entry["_file"]).read_bytes() == pdf_bytes


@pytest.mark.asyncio
async def test_download_body_storage_timeout_propagates(storage: Storage, monkeypatch):
    """A timed-out download write fails HAR processing."""
    pdf_bytes = b"%PDF-1.7 real-ish bytes\n%EOF"
    manifest = {
        "log": {
            "entries": [
                {
                    "request": {"method": "GET", "url": PAGE_URL},
                    "response": {
                        "status": 200,
                        "content": {"mimeType": "application/pdf", "size": -1},
                    },
                }
            ]
        }
    }

    zip_path = Path(storage.base_path) / "record.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("har.har", json.dumps(manifest))

    monkeypatch.setattr(har_module, "STORAGE_WRITE_TIMEOUT_S", 0.01)

    async def slow_pipe_file(path, value, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(storage.fs, "_pipe_file", slow_pipe_file)

    with pytest.raises(asyncio.TimeoutError):
        await capture_http_archive(
            zip_path,
            PAGE_URL,
            storage,
            download=DownloadedBody(
                url=PAGE_URL, data=pdf_bytes, suggested_filename="doc.pdf"
            ),
        )
