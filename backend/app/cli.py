"""Operator CLI: seed reference data, backfill training data, inspect the registry.

    python -m app.cli seed --events 5
    python -m app.cli backfill --year 2026
    python -m app.cli status
    python -m app.cli promote --model-id 7

``backfill`` walks the season in chronological order and runs the same
retraining pipeline the API queues, so the corpus is rebuilt exactly the way it
accumulates during a live season -- each session trained on what came before it.
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.data import seed as seeding
from app.db import models as m
from app.db.database import init_db, session_scope
from app.models import registry
from app.training import retrain_qualifying, retrain_race
from app.training.jobs import JobRecord, ProgressReporter

log = logging.getLogger("app.cli")


def _reporter(label: str) -> ProgressReporter:
    """A progress reporter that prints instead of broadcasting."""
    record = JobRecord(id=label, kind="cli", session_id=None)
    return ProgressReporter(record)


def cmd_seed(args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as db:
        result = seeding.seed_all(db, args.year, args.events)
    print(f"seeded: {result}")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """Retrain over the season in order, ingesting each session as it comes."""
    init_db()
    year = args.year or settings.CURRENT_SEASON
    with session_scope() as db:
        sessions = db.scalars(
            select(m.Session)
            .where(m.Session.year == year)
            .order_by(m.Session.start_time.asc().nullslast(), m.Session.id)
        ).all()
        targets = [
            (s.id, s.session_type, s.circuit_id)
            for s in sessions
            if m.SessionType(s.session_type).is_qualifying or m.SessionType(s.session_type).is_race
        ]

    if not targets:
        print(f"no {year} sessions to backfill; run `seed` first")
        return 1

    trained = failed = 0
    for session_id, session_type, circuit_id in targets:
        label = f"{circuit_id}/{session_type}"
        module = (
            retrain_qualifying
            if m.SessionType(session_type).is_qualifying
            else retrain_race
        )
        try:
            result = module.run(session_id, _reporter(label))
        except Exception as exc:  # a single unavailable session must not stop the walk
            failed += 1
            print(f"  {label}: {type(exc).__name__}: {exc}")
            continue
        trained += 1
        for name, info in (result.get("models") or {}).items():
            validation = info["validation"]
            print(
                f"  {label}: {name} v{info['version']} "
                f"{validation['metric']}={validation['score']:.4f} "
                f"rows={result.get('labelled_rows')} gate={info['gate']['promoted']}"
            )

    print(f"\nbackfill complete: {trained} sessions trained, {failed} skipped")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as db:
        sessions = db.scalars(select(m.Session)).all()
        snapshots = db.scalars(select(m.FeatureSnapshot)).all()
        print(f"regime          {settings.CURRENT_REGS_REGIME} (season {settings.CURRENT_SEASON})")
        print(f"sessions        {len(sessions)} "
              f"({sum(1 for s in sessions if s.status == 'completed')} completed)")
        print(f"snapshots       {len(snapshots)} "
              f"({sum(1 for s in snapshots if s.label_finish_position or s.label_qualifying_time_s)} labelled)")
        print("\nmodel registry:")
        for record in registry.list_models(db):
            marker = "*" if record.is_active else " "
            print(
                f" {marker} {record.model_type:<24} v{record.version:<3} "
                f"{record.validation_metric or '-':<10} "
                f"{record.validation_score if record.validation_score is not None else '-':<10} "
                f"rows={record.training_rows or 0:<5} regime={record.regs_regime}"
            )
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    with session_scope() as db:
        record = registry.promote(db, args.model_id)
        print(f"promoted {record.model_type} v{record.version} (id={record.id})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    seed_parser = sub.add_parser("seed", help="seed circuits, calendar, teams and drivers")
    seed_parser.add_argument("--year", type=int, default=None)
    seed_parser.add_argument("--events", type=int, default=None, help="limit to the first N events")
    seed_parser.set_defaults(func=cmd_seed)

    backfill_parser = sub.add_parser("backfill", help="retrain over a season in chronological order")
    backfill_parser.add_argument("--year", type=int, default=None)
    backfill_parser.set_defaults(func=cmd_backfill)

    status_parser = sub.add_parser("status", help="show data and registry state")
    status_parser.set_defaults(func=cmd_status)

    promote_parser = sub.add_parser("promote", help="activate a model version")
    promote_parser.add_argument("--model-id", type=int, required=True)
    promote_parser.set_defaults(func=cmd_promote)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(levelname)-8s %(message)s")
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
