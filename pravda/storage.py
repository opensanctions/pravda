import hashlib
import logging
import os

import fsspec

logger = logging.getLogger(__name__)

fs, _base_path = fsspec.core.url_to_fs(os.environ["STORAGE_BASE_PATH"])


def _content_path(hash_hex: str) -> str:
    return f"{_base_path.rstrip('/')}/{hash_hex}"


async def put_blob(data: bytes) -> str:
    hash_hex = hashlib.sha256(data).hexdigest()
    path = _content_path(hash_hex)

    if fs.exists(path):
        logger.info("Blob already exists: %s", hash_hex)
        return hash_hex

    fs.makedirs(_base_path, exist_ok=True)
    with fs.open(path, "wb") as f:
        f.write(data)

    logger.info("Stored blob: %s (%d bytes)", hash_hex, len(data))
    return hash_hex


async def get_blob(hash_hex: str) -> bytes:
    path = _content_path(hash_hex)
    with fs.open(path, "rb") as f:
        return f.read()
