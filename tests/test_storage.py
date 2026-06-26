from pathlib import Path

import pytest

from pravda.storage import (
    _base_path,
    content_hash,
    content_prefix,
    normalize_hostname,
    put_blob,
)


@pytest.mark.asyncio
async def test_put_blob_stores_at_content_address():
    data = b"hello pravda"
    name = f"{content_hash(data)}.txt"
    url = "https://www.example.com/path"

    stored = await put_blob(name, data, url)
    assert stored == name

    # Written under the normalized hostname prefix within the storage backend
    path = Path(content_prefix(url)) / name
    assert path.read_bytes() == data


@pytest.mark.asyncio
async def test_put_blob_deduplicates():
    data = b"same content twice"
    name = f"{content_hash(data)}.mhtml"

    name1 = await put_blob(name, data, "https://example.com")
    name2 = await put_blob(name, data, "https://example.com")
    assert name1 == name2


def test_content_prefix_joins_base_and_hostname():
    assert content_prefix("https://www.example.com:443/p") == (
        f"{_base_path}/example.com"
    )


def test_normalize_hostname_lowercases_strips_www():
    assert normalize_hostname("https://WWW.Example.com:443/p?q=1") == "example.com"


def test_normalize_hostname_strips_www_with_digits():
    assert normalize_hostname("https://www2.example.com/") == "example.com"
    assert normalize_hostname("https://www12.Example.com/") == "example.com"


def test_normalize_hostname_drops_port():
    assert normalize_hostname("https://example.com:8080/") == "example.com"


def test_normalize_hostname_ipv6_passthrough():
    assert normalize_hostname("http://[2001:db8::1]:8080/") == "2001:db8::1"


def test_normalize_hostname_idn_punycode():
    assert normalize_hostname("https://bücher.de") == "xn--bcher-kva.de"


def test_normalize_hostname_ip_passthrough():
    assert normalize_hostname("http://192.0.2.1:8080/") == "192.0.2.1"
