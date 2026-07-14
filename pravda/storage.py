"""Content-addressed blob storage over an fsspec backend.

The :class:`Storage` value bundles an async fsspec filesystem with a base
path and is constructed by :class:`pravda.pravda.Pravda` from
``storage_base_path``.
"""

import hashlib
import ipaddress
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import fsspec
from fsspec.asyn import AsyncFileSystem
from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper

logger = logging.getLogger(__name__)

# Per-write wall-clock budget applied around individual artifact writes
# (rendered HTML, plaintext, screenshot, downloaded file) so a wedged storage
# backend cannot hang a capture forever. HAR body writes are bounded as a
# group by the HAR-processing stage instead, so put_blob itself stays
# unbounded — callers wrap the specific writes that need this.
STORAGE_WRITE_TIMEOUT_S = 15


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


def content_hash(data: bytes) -> str:
    """SHA-1 hex digest of *data*.

    Content address following the same scheme Playwright uses for its HAR
    resources: a file is named ``<sha1>.<extension>``. The caller appends the
    extension (derived from the MIME type) — see ``cas_name``.
    """
    return hashlib.sha1(data).hexdigest()


def cas_name(data: bytes, ext: str = "") -> str:
    """Content-addressed filename for *data*: ``<sha1>.<ext>``.

    The extension follows the same scheme Playwright uses for its HAR
    resources. A leading dot is stripped and the rest lowercased; an empty
    extension yields a bare ``<sha1>``.
    """
    ext = ext.lstrip(".").lower()
    return f"{content_hash(data)}.{ext}" if ext else content_hash(data)


@dataclass(frozen=True)
class Storage:
    """A content-addressed blob store: an async filesystem plus a base path.

    Constructed by :class:`pravda.pravda.Pravda` from ``storage_base_path``.
    The filesystem is driven through its async (``_``-prefixed) methods so a
    slow write never blocks the event loop and stalls other in-flight
    captures.
    """

    fs: AsyncFileSystem
    base_path: str

    @classmethod
    def from_url(cls, storage_base_path: str) -> "Storage":
        """Build a :class:`Storage` from an fsspec URL.

        Remote backends (gcs, s3) are natively async; local is sync, so wrap
        it. Either way blob I/O is driven through the async (``_``-prefixed)
        methods.
        """
        fs, base_path = fsspec.core.url_to_fs(storage_base_path)
        if not fs.async_impl:
            fs = AsyncFileSystemWrapper(fs)
        return cls(fs=fs, base_path=base_path)

    def content_prefix(self, url: str) -> str:
        """Full storage prefix for artifacts captured from *url*.

        The base path of the storage backend joined with *url*'s normalized
        hostname, so that ``content_prefix(url) + "/" + filename`` is the
        location a downstream service reads the file from.
        """
        return os.path.join(self.base_path, normalize_hostname(url))

    async def put_blob(self, name: str, data: bytes, url: str) -> str:
        """Store *data* under the hostname prefix of *url* as *name*.

        *name* is the content-addressed filename (``<sha1>.<extension>``); build
        it with ``cas_name``. Returns *name*.
        """
        host_dir = self.content_prefix(url)
        path = os.path.join(host_dir, name)

        await self.fs._makedirs(host_dir, exist_ok=True)
        await self.fs._pipe_file(path, data)

        logger.debug("Stored blob: %s (%d bytes)", name, len(data))
        return name
