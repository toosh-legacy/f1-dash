#!/usr/bin/env python
"""Upload the artifacts and replays on this machine into the bucket.

The registry rows travel with the database, but the ``.joblib`` files they point
at were written on a laptop and the bucket has never seen them. This walks the
rows, finds each file locally, and publishes it under the key the deployed API
will look for -- so the first request after a deploy finds a model instead of a
missing path. Replay gzips get the same treatment.

    F1_BLOB_BACKEND=s3 F1_S3_BUCKET=... python scripts/push_artifacts.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.config import settings  # noqa: E402
from app.data import replay  # noqa: E402
from app.db import models as m  # noqa: E402
from app.db.database import session_scope  # noqa: E402
from app.models import registry  # noqa: E402
from app.storage import blobs  # noqa: E402


def main() -> int:
    if blobs.backend == "local":
        print("F1_BLOB_BACKEND is 'local' -- nothing to upload to. Set it to 's3'.")
        return 1

    print(f"publishing into {blobs.describe()}")
    sent = missing = 0

    with session_scope() as db:
        for record in registry.list_models(db):
            key = registry.artifact_key(record.model_type, record.version)
            path = Path(record.artifact_path)
            if not path.exists():
                path = registry.artifact_path(record.model_type, record.version)
            if not path.exists():
                print(f"  missing  {record.model_type} v{record.version} ({record.artifact_path})")
                missing += 1
                continue
            blobs.publish(path, key)
            print(f"  sent     {key}  ({path.stat().st_size / 1e6:.1f} MB)")
            sent += 1

    for path in sorted(settings.REPLAY_DIR.glob("session_*.json.gz")):
        session_key = int(path.name.split("_")[1])
        blobs.publish(path, replay.bundle_key(session_key))
        print(f"  sent     {replay.bundle_key(session_key)}  ({path.stat().st_size / 1e6:.1f} MB)")
        sent += 1

    print(f"\n{sent} uploaded, {missing} missing")
    return 0 if missing == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
