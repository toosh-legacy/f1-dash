"""The ``jobs`` table: background work, recorded where every process can see it.

A job used to be a closure on this process's thread pool and a record in its
memory. On a function that answers and stops, neither survives the response,
so a job is now a row: *what* to run (``kind`` plus JSON ``params``) and how
far it got. Whoever runs it -- a thread here, or the GitHub Actions worker --
reads the row, runs the named handler, and writes the outcome back, and the
API reports status from the same row.

Plain columns and the generic ``JSON`` type only, so it is the same table on
SQLite and on Postgres.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    session_id: Mapped[int | None] = mapped_column(Integer)
    # queued | running | succeeded | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    stages: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # "Is one of these already queued or running?" and "what is recent?"
        Index("ix_jobs_kind_status", "kind", "status"),
        Index("ix_jobs_created_at", "created_at"),
    )
