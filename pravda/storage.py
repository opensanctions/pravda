import hashlib
import logging
import os

import fsspec

logger = logging.getLogger(__name__)

fs, _base_path = fsspec.core.url_to_fs(os.environ["STORAGE_BASE_PATH"])


def content_path(hash_hex: str) -> str:
    return os.path.join(_base_path, hash_hex)


async def put_blob(data: bytes) -> str:
    """Store *data* and return the content hash."""
    hash_hex = hashlib.sha256(data).hexdigest()
    path = content_path(hash_hex)

    if fs.exists(path):
        logger.info("Blob already exists: %s", hash_hex)
        return hash_hex

    fs.makedirs(_base_path, exist_ok=True)
    with fs.open(path, "wb") as f:
        f.write(data)

    logger.info("Stored blob: %s (%d bytes)", hash_hex, len(data))
    return hash_hex
