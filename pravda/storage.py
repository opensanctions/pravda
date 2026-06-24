import hashlib
import ipaddress
import logging
import os
from urllib.parse import urlparse

import fsspec
from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper

logger = logging.getLogger(__name__)

fs, _base_path = fsspec.core.url_to_fs(os.environ["STORAGE_BASE_PATH"])
# Remote backends (gcs, s3) are natively async; local is sync, so wrap it.
# Either way we drive blob I/O through the async (`_`-prefixed) methods so a
# slow write never blocks the event loop and stalls other in-flight captures.
if not fs.async_impl:
    fs = AsyncFileSystemWrapper(fs)


def normalize_hostname(url: str) -> str:
    """Normalize *url*'s hostname for use as a storage path prefix.

    Lowercases, drops a leading ``www.``, excludes the port, and encodes
    internationalized domains as Punycode. IP addresses (v4/v6) are valid
    prefixes and are returned as-is.
    """
    host = urlparse(url).hostname
    if not host:
        raise ValueError(f"URL has no hostname: {url}")
    try:
        ipaddress.ip_address(host)
        return host.lower()
    except ValueError:
        pass
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host.encode("idna").decode("ascii")


def content_path(url: str, hash_hex: str) -> str:
    return os.path.join(_base_path, normalize_hostname(url), hash_hex)


async def put_blob(data: bytes, url: str) -> str:
    """Store *data* under the hostname prefix of *url* and return its hash."""
    hash_hex = hashlib.sha256(data).hexdigest()
    host_dir = os.path.join(_base_path, normalize_hostname(url))
    path = os.path.join(host_dir, hash_hex)

    if await fs._exists(path):
        logger.debug("Blob already exists: %s", hash_hex)
        return hash_hex

    await fs._makedirs(host_dir, exist_ok=True)
    await fs._pipe_file(path, data)

    logger.debug("Stored blob: %s (%d bytes)", hash_hex, len(data))
    return hash_hex
