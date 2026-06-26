import hashlib
import ipaddress
import logging
import os
import re
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

    Lowercases, drops a leading ``www`` optionally followed by digits
    (e.g. ``www.``, ``www2.``), excludes the port, and encodes
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
    host = re.sub(r"^www\d*\.", "", host)
    return host.encode("idna").decode("ascii")


def content_prefix(url: str) -> str:
    """Full storage prefix for artifacts captured from *url*.

    The base path of the storage backend joined with *url*'s normalized
    hostname, so that ``content_prefix(url) + "/" + filename`` is the
    location a downstream service reads the file from.
    """
    return os.path.join(_base_path, normalize_hostname(url))


def content_hash(data: bytes) -> str:
    """SHA-1 hex digest of *data*.

    Content address following the same scheme Playwright uses for its HAR
    resources: a file is named ``<sha1>.<extension>``. The caller appends the
    extension (derived from the MIME type) — see ``put_blob``.
    """
    return hashlib.sha1(data).hexdigest()


async def put_blob(name: str, data: bytes, url: str) -> str:
    """Store *data* under the hostname prefix of *url* as *name*.

    *name* is a content-addressed filename (``<sha1>.<extension>``); the
    caller computes the hash via ``content_hash`` and appends the extension.
    Returns *name*.
    """
    host_dir = content_prefix(url)
    path = os.path.join(host_dir, name)

    if await fs._exists(path):
        logger.debug("Blob already exists: %s", name)
        return name

    await fs._makedirs(host_dir, exist_ok=True)
    await fs._pipe_file(path, data)

    logger.debug("Stored blob: %s (%d bytes)", name, len(data))
    return name
