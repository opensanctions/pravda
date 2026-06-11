import hashlib
from pathlib import Path

import pytest

from pravda.storage import content_path, put_blob


@pytest.mark.asyncio
async def test_put_blob_stores_at_hash_path():
    data = b"hello pravda"
    expected_hash = hashlib.sha256(data).hexdigest()

    hash_hex = await put_blob(data)
    assert len(hash_hex) == 64
    assert hash_hex == expected_hash

    # Verify the file was written to the correct path
    path = Path(content_path(hash_hex))
    assert path.read_bytes() == data


@pytest.mark.asyncio
async def test_put_blob_deduplicates():
    data = b"same content twice"

    hash1 = await put_blob(data)
    hash2 = await put_blob(data)
    assert hash1 == hash2


def test_content_path_builds_full_path():
    path = content_path("abc123")
    assert path.endswith("/abc123")
