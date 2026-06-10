import hashlib

import pytest

from pravda.storage import get_blob, put_blob


@pytest.mark.asyncio
async def test_put_and_get_blob(storage_dir):
    data = b"hello pravda"
    expected_hash = hashlib.sha256(data).hexdigest()

    result = await put_blob(data)
    assert result == expected_hash

    retrieved = await get_blob(expected_hash)
    assert retrieved == data


@pytest.mark.asyncio
async def test_put_blob_deduplicates(storage_dir):
    data = b"same content twice"

    hash1 = await put_blob(data)
    hash2 = await put_blob(data)
    assert hash1 == hash2


@pytest.mark.asyncio
async def test_get_blob_missing(storage_dir):
    with pytest.raises(FileNotFoundError):
        await get_blob("0" * 64)
