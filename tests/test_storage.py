import hashlib
from pathlib import Path

import pytest

from pravda.storage import content_path, put_blob


@pytest.mark.asyncio
async def test_put_blob_stores_at_hash_path():
    data = b"hello pravda"
    expected_hash = hashlib.sha256(data).hexdigest()

    hash_hex = await put_blob(data, "https://www.example.com/path")
    assert len(hash_hex) == 64
    assert hash_hex == expected_hash

    # Verify the file was written under the normalized hostname prefix
    path = Path(content_path("https://www.example.com", hash_hex))
    assert path.read_bytes() == data


@pytest.mark.asyncio
async def test_put_blob_deduplicates():
    data = b"same content twice"

    hash1 = await put_blob(data, "https://example.com")
    hash2 = await put_blob(data, "https://example.com")
    assert hash1 == hash2


def test_content_path_builds_full_path():
    path = content_path("https://example.com", "abc123")
    assert path.endswith("/example.com/abc123")


def test_normalize_hostname_lowercases_strips_www():
    from pravda.storage import normalize_hostname

    assert normalize_hostname("https://WWW.Example.com:443/p?q=1") == "example.com"


def test_normalize_hostname_idn_punycode():
    from pravda.storage import normalize_hostname

    assert normalize_hostname("https://bücher.de") == "xn--bcher-kva.de"


def test_normalize_hostname_ip_passthrough():
    from pravda.storage import normalize_hostname

    assert normalize_hostname("http://192.0.2.1:8080/") == "192.0.2.1"
