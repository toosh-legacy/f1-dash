"""Back up F1's official timing archive for a season, while the F1 TV token is valid.

    .venv/Scripts/python scripts/backup_f1_archive.py                 # this season
    .venv/Scripts/python scripts/backup_f1_archive.py --year 2026 --only Race
    .venv/Scripts/python scripts/backup_f1_archive.py --check         # count, no download

``livetiming.formula1.com/static`` holds every session's raw feeds -- car
telemetry, positions, timing, tyres, race control, weather -- as the same
``.jsonStream`` files the live hub streams. It answers 401 without an F1 TV
subscriber's token (see ``app.data.f1live``), and the token lasts only days,
so this copies the files as they are into ``backend/archive/<path>`` to keep.

Resumable: a file already on disk at its published size is skipped. Files are
fetched one at a time with a pause between, to go easy on F1's servers.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.data.f1live import subscription_token, token_claims  # noqa: E402

BASE = "https://livetiming.formula1.com/static/"
PAUSE_S = 0.25

log = logging.getLogger("backup")


def fetch_json(client: httpx.Client, path: str) -> dict:
    response = client.get(BASE + path)
    response.raise_for_status()
    return json.loads(response.content.decode("utf-8-sig"))


def download(client: httpx.Client, path: str, out: Path) -> int:
    """Fetch ``path`` to ``out``; returns bytes written, 0 if already complete."""
    if out.exists():
        head = client.head(BASE + path)
        size = int(head.headers.get("content-length") or -1)
        if head.status_code == 200 and size == out.stat().st_size:
            return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(out.name + ".part")
    with client.stream("GET", BASE + path) as response:
        if response.status_code == 404:
            return 0
        response.raise_for_status()
        with open(temporary, "wb") as handle:
            for chunk in response.iter_bytes(1 << 16):
                handle.write(chunk)
    temporary.replace(out)
    return out.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--year", type=int, default=settings.CURRENT_SEASON)
    parser.add_argument("--only", help="session name to keep, e.g. Race or Qualifying")
    parser.add_argument("--check", action="store_true", help="list what would be fetched")
    parser.add_argument("--out", type=Path, default=settings.F1_ARCHIVE_DIR)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    token = subscription_token()
    if not token:
        log.error("no F1 TV token: put the login-session cookie in backend/.env as F1TV_LOGIN_SESSION")
        return 2
    claims = token_claims(token)
    if claims.get("expired"):
        log.error("F1 TV token expired at %s: copy the cookie again", claims.get("expires_at"))
        return 2
    log.info("F1 TV: %s, valid until %s", claims.get("product"), claims.get("expires_at"))

    client = httpx.Client(
        headers={"Authorization": f"Bearer {token}"}, timeout=60, follow_redirects=True
    )
    index = fetch_json(client, f"{args.year}/Index.json")
    root = args.out
    (root / str(args.year)).mkdir(parents=True, exist_ok=True)
    (root / str(args.year) / "Index.json").write_text(json.dumps(index), encoding="utf-8")

    sessions = [
        (meeting, session)
        for meeting in index.get("Meetings", [])
        for session in meeting.get("Sessions", [])
        if session.get("Path") and (not args.only or session.get("Name") == args.only)
    ]
    log.info("%d sessions in %d", len(sessions), args.year)
    total = 0
    failed: list[str] = []
    for number, (meeting, session) in enumerate(sessions, start=1):
        path = session["Path"]
        label = f"[{number}/{len(sessions)}] {meeting.get('Name')} {session.get('Name')}"
        try:
            feeds = fetch_json(client, path + "Index.json")
        except httpx.HTTPError as exc:
            log.warning("%s: no index (%s)", label, exc)
            failed.append(path)
            continue
        (root / path).mkdir(parents=True, exist_ok=True)
        (root / path / "Index.json").write_text(json.dumps(feeds), encoding="utf-8")
        streams = [f["StreamPath"] for f in feeds.get("Feeds", {}).values() if f.get("StreamPath")]
        if args.check:
            log.info("%s: %d feeds", label, len(streams))
            continue
        written = 0
        for stream in streams:
            try:
                written += download(client, path + stream, root / path / stream)
            except httpx.HTTPError as exc:
                log.warning("%s: %s failed (%s)", label, stream, exc)
                failed.append(path + stream)
            time.sleep(PAUSE_S)
        total += written
        log.info("%s: %d feeds, %.1f MB new", label, len(streams), written / 1e6)

    log.info("done: %.1f MB new in %s; %d failed", total / 1e6, root, len(failed))
    for path in failed:
        log.info("  failed: %s", path)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
