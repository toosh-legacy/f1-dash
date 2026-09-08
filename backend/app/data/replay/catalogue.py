"""What races exist to replay, and which have been built.

Listing is a question for OpenF1; whether a bundle exists is a question for
this server. The entry carries both so a dashboard can show the whole calendar
and mark the part of it that is ready to play.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.data.openf1_client import OpenF1Client
from app.data.replay.shared import _parse_date
from app.data.replay.store import bundle_path


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------
@dataclass
class ReplayCatalogueEntry:
    session_key: int
    meeting_key: int | None
    name: str
    session_type: str
    circuit: str | None
    country: str | None
    location: str | None
    date_start: str | None
    year: int | None
    cached: bool = False
    size_bytes: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "meeting_key": self.meeting_key,
            "name": self.name,
            "session_type": self.session_type,
            "circuit": self.circuit,
            "country": self.country,
            "location": self.location,
            "date_start": self.date_start,
            "year": self.year,
            "cached": self.cached,
            "size_bytes": self.size_bytes,
        }


def catalogue(
    year: int | None = None,
    *,
    client: OpenF1Client | None = None,
    include_sprints: bool = True,
    now: datetime | None = None,
) -> list[ReplayCatalogueEntry]:
    """Every race that has already run this season, newest first.

    A session only appears once it has actually started; a scheduled round has
    nothing to replay.
    """
    year = year or settings.CURRENT_SEASON
    now = now or datetime.now(timezone.utc)
    owned = client is None
    client = client or OpenF1Client()
    try:
        rows = client.sessions(year=year)
    finally:
        if owned:
            client.close()

    entries: list[ReplayCatalogueEntry] = []
    for row in rows:
        # OpenF1 types both the grand prix and the sprint as "Race"; they are
        # told apart by name.
        if row.get("session_type") != "Race":
            continue
        name = str(row.get("session_name") or "Race")
        if not include_sprints and name.lower().startswith("sprint"):
            continue
        started = _parse_date(row.get("date_start"))
        if started is None or started > now:
            continue
        key = int(row["session_key"])
        path = bundle_path(key)
        entries.append(
            ReplayCatalogueEntry(
                session_key=key,
                meeting_key=row.get("meeting_key"),
                name=name,
                session_type=str(row.get("session_type") or ""),
                circuit=row.get("circuit_short_name"),
                country=row.get("country_name"),
                location=row.get("location"),
                date_start=row.get("date_start"),
                year=row.get("year") or year,
                cached=path.exists(),
                size_bytes=path.stat().st_size if path.exists() else None,
            )
        )
    entries.sort(key=lambda e: e.date_start or "", reverse=True)
    return entries
