#!/usr/bin/env python
"""Copy the local SQLite database into Postgres (Neon), table by table.

    backend/.venv/Scripts/python scripts/migrate_to_postgres.py --to "postgres://..." --dry-run
    backend/.venv/Scripts/python scripts/migrate_to_postgres.py --to "postgres://..."
    backend/.venv/Scripts/python scripts/migrate_to_postgres.py --replace --push-files

The target defaults to ``DATABASE_URL`` from the environment (what ``vercel env
pull`` writes). Tables are created from the models (``Base.metadata``), so the
schema is the code's, not a dump of SQLite's. Rows go in dependency order --
parents before the rows that point at them -- in batches.

Running it twice is safe: a table that already has rows in Postgres is left
alone, unless ``--replace``, which empties every target table first (children
before parents) and copies everything again. Afterwards each integer primary
key's sequence is moved past the highest id copied, so the next row Postgres
creates does not collide with one that came from SQLite.

``--push-files`` also stores, in the database's file store, the files a
function cannot find on its own disk: the model artifact of every row in
``prediction_models`` and the kept telemetry windows.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
DEFAULT_SOURCE = f"sqlite:///{BACKEND / 'f1_dashboard.db'}"
BATCH = 500


def normalise(url: str) -> str:
    """``postgres://`` as providers hand it out, rewritten for psycopg 3."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", default=DEFAULT_SOURCE,
                        help="source database URL (default: backend/f1_dashboard.db)")
    parser.add_argument("--to", dest="target", default=os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL"),
                        help="target Postgres URL (default: $DATABASE_URL)")
    parser.add_argument("--replace", action="store_true",
                        help="empty every target table first and copy everything again")
    parser.add_argument("--dry-run", action="store_true", help="print row counts, change nothing")
    parser.add_argument("--push-files", action="store_true",
                        help="also store model artifacts and telemetry in the database's file store")
    parser.add_argument("--batch", type=int, default=BATCH, help=f"rows per insert (default {BATCH})")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.target:
        print("no target: pass --to or set DATABASE_URL", file=sys.stderr)
        return 2
    target_url = normalise(args.target)
    # The app's own engine (used by the file store) must point at the target,
    # so this is set before anything from the app is imported.
    os.environ["F1_DATABASE_URL"] = target_url
    os.environ.setdefault("F1_LIVE_AUTOSTART", "false")
    sys.path.insert(0, str(BACKEND))

    from sqlalchemy import create_engine, func, inspect, select, text

    from app.db import models  # noqa: F401  (registers the tables)
    from app.db.database import Base

    # Tables other parts of the app add. Each guarded: a checkout that
    # predates one still migrates what it has.
    for module in ("app.db.stored_file", "app.db.job_record"):
        try:
            __import__(module)
        except ImportError:
            print(f"note: {module} not present; its table is skipped")

    source = create_engine(normalise(args.source), future=True)
    target = create_engine(target_url, future=True, pool_pre_ping=True)
    tables = Base.metadata.sorted_tables
    source_tables = set(inspect(source).get_table_names())

    def count(engine, table) -> int | None:
        try:
            with engine.connect() as conn:
                return conn.execute(select(func.count()).select_from(table)).scalar_one()
        except Exception:  # noqa: BLE001 - table missing on that side
            return None

    if args.dry_run:
        print(f"{'table':32} {'sqlite':>10} {'postgres':>10}")
        for table in tables:
            here = count(source, table) if table.name in source_tables else None
            there = count(target, table)
            print(f"{table.name:32} {'-' if here is None else here:>10} {'-' if there is None else there:>10}")
        return 0

    Base.metadata.create_all(bind=target)

    if args.replace:
        with target.begin() as conn:
            for table in reversed(tables):
                conn.execute(table.delete())
        print("emptied every target table")

    for table in tables:
        if table.name not in source_tables:
            print(f"{table.name:32} not in source, skipped")
            continue
        existing = count(target, table) or 0
        if existing:
            print(f"{table.name:32} already has {existing} rows, skipped (--replace to copy again)")
            continue
        # Only the columns both sides have: an older SQLite file may predate one.
        have = {c["name"] for c in inspect(source).get_columns(table.name)}
        columns = [c for c in table.columns if c.name in have]
        copied = 0
        with source.connect() as read, target.begin() as write:
            result = read.execution_options(stream_results=True).execute(select(*columns))
            while True:
                rows = result.fetchmany(args.batch)
                if not rows:
                    break
                write.execute(table.insert(), [dict(row._mapping) for row in rows])
                copied += len(rows)
        print(f"{table.name:32} {copied} rows")

    if target.dialect.name == "postgresql":
        with target.begin() as conn:
            for table in tables:
                pk = list(table.primary_key.columns)
                try:
                    integer = len(pk) == 1 and pk[0].type.python_type is int
                except NotImplementedError:
                    integer = False
                if not integer:
                    continue
                column = pk[0].name
                sequence = conn.execute(
                    text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": table.name, "c": column}
                ).scalar()
                if not sequence:
                    continue
                conn.execute(text(
                    f'SELECT setval(:s, COALESCE((SELECT MAX("{column}") FROM "{table.name}"), 1), '
                    f'(SELECT MAX("{column}") FROM "{table.name}") IS NOT NULL)'
                ), {"s": sequence})
        print("sequences moved past the copied ids")

    if args.push_files:
        push_files(target)
    return 0


def push_files(target) -> None:
    """Model artifacts and telemetry windows into the durable file store."""
    try:
        from app.storage import files
    except ImportError:
        print("note: app.storage.files not present; --push-files skipped")
        return
    from sqlalchemy import select

    from app.config import settings
    from app.db.models import PredictionModel

    pushed = missing = 0
    with target.connect() as conn:
        rows = conn.execute(select(
            PredictionModel.model_type, PredictionModel.version, PredictionModel.artifact_path
        )).all()
    for model_type, version, artifact_path in rows:
        name = f"{model_type}_v{version}.joblib"
        candidates = [settings.MODEL_ARTIFACT_DIR / name]
        if artifact_path:
            candidates.append(settings.MODEL_ARTIFACT_DIR / Path(artifact_path).name)
            candidates.append(Path(artifact_path))
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            missing += 1
            continue
        files.publish(path, f"models/{name}")
        pushed += 1
    print(f"models: {pushed} stored, {missing} with no file on this machine")

    try:
        from app.data.replay.telemetry import CACHE_VERSION
    except ImportError:
        CACHE_VERSION = None
    base = settings.REPLAY_DIR / "telemetry"
    if CACHE_VERSION:
        base = base / CACHE_VERSION
    windows = 0
    if base.is_dir():
        for path in sorted(base.rglob("*.json.gz")):
            files.publish(path, path.relative_to(settings.REPLAY_DIR).as_posix())
            windows += 1
    print(f"telemetry: {windows} windows stored from {base}")


if __name__ == "__main__":
    sys.exit(main())
