from pathlib import Path

import pytest

from pravda.storage import Storage, cas_name, normalize_hostname


@pytest.mark.asyncio
async def test_put_artifact_returns_prefixed_content_address(storage: Storage):
    data = b"<html>hello</html>"
    url = "https://www.example.com/page"

    path = await storage.put_artifact(data, "html", url)

    assert path == f"example.com/{cas_name(data, 'html')}"
    assert (Path(storage.base_path) / path).read_bytes() == data


@pytest.mark.asyncio
async def test_put_artifact_deduplicates(storage: Storage):
    data = b"same content twice"

    name1 = await storage.put_artifact(data, "mhtml", "https://example.com")
    name2 = await storage.put_artifact(data, "mhtml", "https://example.com")
    assert name1 == name2


@pytest.mark.asyncio
async def test_read_artifact_returns_stored_bytes(storage: Storage):
    data = b"hello pravda"
    path = await storage.put_artifact(data, "txt", "https://example.com/path")

    assert await storage.read_artifact(path) == data


@pytest.mark.asyncio
async def test_read_artifact_missing_artifact_raises(storage: Storage):
    with pytest.raises(FileNotFoundError):
        await storage.read_artifact("example.com/missing.txt")


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
