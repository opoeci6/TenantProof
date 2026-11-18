"""Content-addressed blob store. Key = sha256 of the bytes, so writes are idempotent and a
stored blob can always be re-verified against its key. Local-disk implementation; the
function surface (put / open_chunks / read) is what an object store would replace."""

import hashlib
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

CHUNK = 64 * 1024


@dataclass(frozen=True)
class StoredBlob:
    key: str
    sha256: str
    size_bytes: int


def _root() -> Path:
    return Path(get_settings().storage_dir)


def _path(key: str) -> Path:
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("invalid storage key")
    return _root() / key[:2] / key


def put(data: bytes) -> StoredBlob:
    digest = hashlib.sha256(data).hexdigest()
    target = _path(digest)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, target)
        finally:
            Path(tmp).unlink(missing_ok=True)
    return StoredBlob(key=digest, sha256=digest, size_bytes=len(data))


def open_chunks(key: str) -> Iterator[bytes]:
    with _path(key).open("rb") as handle:
        while chunk := handle.read(CHUNK):
            yield chunk


def read(key: str) -> bytes:
    return b"".join(open_chunks(key))


def exists(key: str) -> bool:
    return _path(key).exists()
