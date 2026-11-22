"""A file kept in the database, so it outlives the machine that wrote it.

On a Vercel function the filesystem is ``/tmp`` only, per instance, and gone
when the instance is recycled. Anything that used to be a durable record on
local disk -- a trained model, a telemetry window, a circuit map -- is
therefore stored here as well, and the file on disk is demoted to a cache that
:mod:`app.storage.files` rebuilds on demand.

Deliberately one generic table rather than a column per kind of file: the key
is the file's path relative to its cache directory, so adding a new kind of
durable file is a choice of key prefix, not a migration. Portable columns only,
so it is the same table on SQLite and on Postgres.

Replay bundles are *not* stored here: they already have ``replay_bundles``,
which carries catalogue metadata beside the blob.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StoredFile(Base):
    """One file's bytes under a slash-separated key, e.g. ``models/race_v3.joblib``."""

    __tablename__ = "stored_files"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: Kept beside the blob so a listing can report sizes without reading it.
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
