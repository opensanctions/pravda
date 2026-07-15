"""Content-addressed blob storage over fsspec."""

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

STORAGE_WRITE_TIMEOUT_S = 15


def normalize_hostname(url: str) -> str:
    """Return a lowercase, IDNA hostname without a leading ``www`` label."""
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
    """Return the SHA-1 hex digest of *data*."""
    return hashlib.sha1(data).hexdigest()


def cas_name(data: bytes, ext: str = "") -> str:
    """Return ``<sha1>.<ext>``, normalizing *ext*."""
    ext = ext.lstrip(".").lower()
    return f"{content_hash(data)}.{ext}" if ext else content_hash(data)


@dataclass(frozen=True)
class Storage:
    """An async content-addressed blob store."""

    fs: AsyncFileSystem
    base_path: str

    @classmethod
    def from_url(cls, storage_base_path: str) -> "Storage":
        """Build storage from an fsspec URL, wrapping synchronous backends."""
        fs, base_path = fsspec.core.url_to_fs(storage_base_path)
        if not fs.async_impl:
            fs = AsyncFileSystemWrapper(fs)
        return cls(fs=fs, base_path=base_path)

    def content_prefix(self, url: str) -> str:
        """Return the storage prefix for *url*."""
        return os.path.join(self.base_path, normalize_hostname(url))

    async def put_blob(self, name: str, data: bytes, url: str) -> str:
        """Store *data* under *url*'s hostname prefix and return *name*."""
        host_dir = self.content_prefix(url)
        path = os.path.join(host_dir, name)

        await self.fs._makedirs(host_dir, exist_ok=True)
        await self.fs._pipe_file(path, data)

        logger.debug("Stored blob: %s (%d bytes)", name, len(data))
        return name
