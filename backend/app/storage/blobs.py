"""Blob storage for model artifacts and built replay bundles.

Two files in this project outlive the container that wrote them: a trained
model's ``.joblib`` and a replay's gzip. On a laptop both live on disk and
nothing else is needed. On a host with an ephemeral filesystem -- Render, a
rebuilt container, a second replica -- disk is a cache, not a record, and the
record belongs in object storage.

So this is deliberately a *mirror*, not a filesystem abstraction: callers keep
writing and reading local paths exactly as they did, and bracket those with
:meth:`publish` after a write and :meth:`materialize` before a read. When the
backend is ``local`` both are no-ops and the code path is the one that has
always run; when it is ``s3`` the local file becomes a cache in front of the
bucket, and a cold container refills it on first use.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: How long a bucket listing is trusted before it is fetched again.
INDEX_TTL_S = float(os.getenv("F1_S3_INDEX_TTL", "30"))


class BlobStore:
    """Local disk only: the file that was written *is* the record."""

    backend = "local"

    def publish(self, path: Path, key: str) -> None:
        """Record a file that has just been written locally."""

    def exists(self, key: str, path: Path) -> bool:
        """Whether the blob is available, without fetching it.

        Kept separate from :meth:`materialize` because catalogue listings ask
        this once per row: it has to stay cheap.
        """
        return path.exists()

    def materialize(self, key: str, path: Path) -> bool:
        """Ensure ``path`` exists locally, returning whether it does."""
        return path.exists()

    def delete(self, key: str) -> None:
        """Forget a blob."""

    def describe(self) -> str:
        return "local disk"


class S3BlobStore(BlobStore):
    """S3 (or any S3-compatible endpoint) as the record, disk as the cache.

    Credentials are left entirely to boto3's own chain -- environment, instance
    role, or IAM role for service accounts -- so nothing secret passes through
    settings or this module.
    """

    backend = "s3"

    def __init__(self, bucket: str, prefix: str = "", endpoint_url: str | None = None) -> None:
        import boto3  # imported lazily: the local backend must not need it

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client("s3", endpoint_url=endpoint_url or None)
        # A cold container can be asked for the same artifact by several
        # requests at once; one download is enough.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # One listing answers every existence question a catalogue asks, and
        # goes stale fast enough that a bundle built elsewhere shows up.
        self._index: set[str] | None = None
        self._index_at = 0.0
        self._index_guard = threading.Lock()

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def _keys(self) -> set[str]:
        """Every key under the prefix, cached for :data:`INDEX_TTL_S`."""
        with self._index_guard:
            if self._index is not None and time.monotonic() - self._index_at < INDEX_TTL_S:
                return self._index
            keys: set[str] = set()
            try:
                paginator = self._client.get_paginator("list_objects_v2")
                kwargs = {"Bucket": self.bucket}
                if self.prefix:
                    kwargs["Prefix"] = self.prefix + "/"
                for page in paginator.paginate(**kwargs):
                    for item in page.get("Contents", ()):
                        keys.add(item["Key"])
            except Exception as error:
                log.warning("could not list s3://%s: %s", self.bucket, error)
                # An unreadable bucket is reported as empty rather than raising:
                # a catalogue with nothing marked built is still a catalogue.
                return set()
            self._index = keys
            self._index_at = time.monotonic()
            return keys

    def publish(self, path: Path, key: str) -> None:
        if not path.exists():
            raise FileNotFoundError(f"cannot publish missing file {path}")
        self._client.upload_file(str(path), self.bucket, self._key(key))
        with self._index_guard:
            if self._index is not None:
                self._index.add(self._key(key))
        log.info("published %s to s3://%s/%s", path.name, self.bucket, self._key(key))

    def exists(self, key: str, path: Path) -> bool:
        return path.exists() or self._key(key) in self._keys()

    def materialize(self, key: str, path: Path) -> bool:
        if path.exists():
            return True
        with self._lock_for(key):
            if path.exists():  # filled in while waiting on the lock
                return True
            path.parent.mkdir(parents=True, exist_ok=True)
            # Download beside the target and rename, so a reader never opens a
            # half-written artifact.
            tmp = path.with_name(path.name + f".{os.getpid()}.part")
            try:
                self._client.download_file(self.bucket, self._key(key), str(tmp))
            except Exception as error:  # botocore's 404 is a ClientError subclass
                tmp.unlink(missing_ok=True)
                log.warning("s3://%s/%s unavailable: %s", self.bucket, self._key(key), error)
                return False
            tmp.replace(path)
            log.info("cached s3://%s/%s at %s", self.bucket, self._key(key), path)
            return True

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._key(key))
        with self._index_guard:
            if self._index is not None:
                self._index.discard(self._key(key))

    def describe(self) -> str:
        location = f"s3://{self.bucket}"
        return f"{location}/{self.prefix}" if self.prefix else location


def _build() -> BlobStore:
    from app.config import settings

    if settings.BLOB_BACKEND != "s3":
        return BlobStore()
    if not settings.S3_BUCKET:
        raise RuntimeError("F1_BLOB_BACKEND=s3 requires F1_S3_BUCKET to be set")
    return S3BlobStore(settings.S3_BUCKET, settings.S3_PREFIX, settings.S3_ENDPOINT_URL)


blobs: BlobStore = _build()
