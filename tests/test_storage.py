import hashlib
from pathlib import Path

import pytest

from pravda.storage import put_blob


@pytest.mark.asyncio
async def test_put_blob_stores_at_hash_path():
    data = b"hello pravda"
    expected_hash = hashlib.sha256(data).hexdigest()

    path = await put_blob(data)
    assert Path(path).is_absolute()
    assert path.endswith(expected_hash)

    # Verify the file contains the original data
    assert Path(path).read_bytes() == data


@pytest.mark.asyncio
async def test_put_blob_deduplicates():
    data = b"same content twice"

    path1 = await put_blob(data)
    path2 = await put_blob(data)
    assert path1 == path2
