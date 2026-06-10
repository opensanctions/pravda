import hashlib
import logging
from pathlib import Path

import fsspec

from pravda.constants import STORAGE_BASE_PATH

logger = logging.getLogger(__name__)


def _get_fs() -> fsspec.AbstractFileSystem:
    if STORAGE_BASE_PATH.startswith("gs://"):
        return fsspec.filesystem("gcs")
    return fsspec.filesystem("file")


def _content_path(hash_hex: str) -> str:
    if STORAGE_BASE_PATH.startswith("gs://"):
        return f"{STORAGE_BASE_PATH.rstrip('/')}/{hash_hex}"
    return str(Path(STORAGE_BASE_PATH) / hash_hex)


async def put_blob(data: bytes) -> str:
    hash_hex = hashlib.sha256(data).hexdigest()
    path = _content_path(hash_hex)
    fs = _get_fs()

    if fs.exists(path):
        logger.info("Blob already exists: %s", hash_hex)
        return hash_hex

    # Ensure parent directory exists (local filesystem only)
    if not STORAGE_BASE_PATH.startswith("gs://"):
        Path(STORAGE_BASE_PATH).mkdir(parents=True, exist_ok=True)

    with fs.open(path, "wb") as f:
        f.write(data)

    logger.info("Stored blob: %s (%d bytes)", hash_hex, len(data))
    return hash_hex


async def get_blob(hash_hex: str) -> bytes:
    path = _content_path(hash_hex)
    fs = _get_fs()
    with fs.open(path, "rb") as f:
        return f.read()
