# F1 Live Prediction Dashboard — 2026 season

Per-car live predictions for the current Formula 1 season, on two separate clocks:

- **Retraining** runs once, after a session ends, using that session's real results as new
  training data.
- **Live inference** runs continuously during a session — every lap in a race, every period in
  qualifying — feeding current live state through the already-trained model.

These are architecturally separate. A live request path never triggers retraining.

Built to `guide.md`, which remains the specification; this README covers how to run it.

## Stack

| Layer | Choice |
|---|---|
| API | Python 3.13, FastAPI, native WebSockets |
| Persistence | SQLAlchemy 2 + SQLite (Postgres is a connection-string change) |
| Models | XGBoost (all four prediction models) |
| Completed-session data | `fastf1` |
| Live session data | OpenF1 REST (no API key) |
| Frontend | Static HTML/JS, no build step, served by a zero-dependency Node server |

## Quick start

```bash
# 1. Backend
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux

# 2. Seed real season data (circuits, calendar, teams, drivers)
.venv/Scripts/python -m app.cli seed --events 5

# 3. Build the training corpus: walks the season in order, retraining as it goes
.venv/Scripts/python -m app.cli backfill

# 4. Review scores and activate a version (promotion is deliberate, never automatic)
.venv/Scripts/python -m app.cli status
.venv/Scripts/python -m app.cli promote --model-id 7

# 5. Run the API
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

```bash
# 6. Dashboard (separate terminal, from the repo root)
npm start          # http://localhost:3000, proxies /api to the backend
npm run smoke      # end-to-end check: API, dashboard, WebSocket
```

The API also serves the dashboard directly at <http://localhost:8000/>. The Node server exists so
the frontend can live on its own origin — as it would behind a CDN — and to proxy the WebSocket
upgrade in development.

Docker: `docker compose up --build` brings up both services.

## Watching a session live

```bash
# Start the polling loop. Passing a *completed* OpenF1 session key replays that
# session, which is how the live path is exercised out of season.
curl -X POST http://localhost:8000/sessions/25/live/start \
     -H 'content-type: application/json' \
     -d '{"openf1_session_key": 11342, "total_laps": 57}'

curl http://localhost:8000/live            # loop status
curl -X POST http://localhost:8000/sessions/25/live/stop
```

Open the dashboard, pick the session, hit **Connect**. Qualifying updates arrive at each period
boundary, race updates every lap, and race-control changes arrive immediately.

## The four rules this codebase is built around

1. **Retraining is a batch job.** `POST /sessions/{id}/retrain` enqueues work on a background
   thread (`app/training/jobs.py`) and returns a job record immediately. Progress crosses back
   into the event loop through `asyncio.run_coroutine_threadsafe`.
2. **Every model version passes a validation gate before it can go live.** `app/models/registry.py`
   compares a candidate against the active version on a held-out recent session, refuses to promote
   a worse one, and treats an implausibly good score as suspected leakage rather than success.
   Even a passing model waits for an explicit `POST /models/{id}/promote`.
3. **The 2026 transfer table is executable policy, not documentation.** `app/features/transfer.py`
   declares a policy per feature — car/team/tyre features train on 2026 data only, driver and
   track-geometry features may use the full record, power-unit and pit-loss carry over weakly.
   Training pipelines query it; a feature without a declared policy fails at import.
4. **Live race predictions are gated outside green-flag conditions.** Safety car, VSC, and red
   flag are detected from race control, stored as `RaceControlEvent`, flagged on the prediction as
   `is_gated`, and surfaced in the dashboard as low-confidence rather than a confident number.

`regs_regime` is a field on every session, snapshot and model — never a hardcoded `"2026"` — so the
next regulation reset does not require a rewrite. A model trained under one regime is refused at
serve time under another.

## Layout

```
backend/app/
  data/       fastf1_client.py (completed sessions) · openf1_client.py (live) · seed.py
  features/   transfer.py (the §2 table as code) · engineering.py (pure features) · builder.py
  models/     base.py · qualifying_model.py · race_model.py · registry.py (versioning + gate)
  training/   dataset.py (labelling + matrix) · retrain_qualifying.py · retrain_race.py · jobs.py
  live/       base.py (polling threads) · qualifying_loop.py · race_loop.py · race_control.py
              broadcast.py (per-session subscriber queues)
  db/         models.py · database.py
  main.py     REST + WebSocket routes
  cli.py      seed / backfill / status / promote
frontend/     server.js (static host + dev proxy, stdlib only)
scripts/      smoke.js (end-to-end check)
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Regime, season, active models, running loops |
| GET | `/circuits`, `/drivers` | Seeded reference data |
| POST | `/seed` | Seed calendar and entries from real season data |
| GET | `/sessions?year=&circuit_id=&session_type=` | List/filter sessions |
| GET | `/sessions/{id}` | Session detail |
| POST | `/sessions/{id}/retrain` | Queue retraining; returns a job immediately |
| GET | `/jobs`, `/jobs/{id}` | Job status and per-stage progress |
| GET | `/sessions/{id}/practice` | Current-form summary — not a forward prediction |
| GET | `/sessions/{id}/qualifying/predictions?period=Q1\|Q2\|Q3` | Latest qualifying predictions |
| GET | `/sessions/{id}/race/predictions?lap=` | Latest race predictions |
| GET | `/sessions/{id}/race_control` | Recorded race-control events |
| GET | `/models` · POST `/models/{id}/promote` | Registry and manual activation |
| POST | `/sessions/{id}/live/start` · `/live/stop` · GET `/live` | Live loop control |
| WS | `/sessions/{id}/live` | `qualifying_update` / `race_update` / `race_control` |

## Tests

```bash
cd backend && .venv/Scripts/python -m pytest        # 84 tests, no network required
```

Coverage focuses on the parts that are expensive to get wrong: the transfer table, feature
completeness and fallbacks, the validation gate and promotion invariants, race-control
classification and gating, broadcast fan-out under a slow subscriber, loop resilience to a failing
poll, and the leakage guards around training-data assembly.

## Notes from running it against real 2026 data

- **FastF1 classification is unavailable for the current season.** Position, grid and status reach
  FastF1 via Ergast, which does not cover 2026, so the finishing order is reconstructed from lap
  timing (`_augment_results_from_laps`). Without that, every driver reads as a DNF.
- **Feature aggregates are computed `as_of` the session being predicted.** Otherwise a race's own
  results feed the features used to predict it — with a season this short, that leaks the answer.
- **OpenF1 rate-limits and 404s empty feeds.** The client throttles requests, honours `Retry-After`,
  and treats a 404 (`intervals` during qualifying, for example) as an empty result. Per-car
  telemetry is opt-in (`include_telemetry=True`) because it costs one request per car per tick.
- **The Manual Override field name is probed, not hardcoded.** 2026 replaces DRS with Manual
  Override; `detect_override_field` checks candidate names against the live payload and falls back
  to neutral when none is present. Against current OpenF1 data it still resolves to `drs`.
- **Scores from a five-event backfill:** qualifying-advancement accuracy 0.57–0.89, finishing
  position MAE 2.6–5.3 places. Race-strategy accuracy swings widely (0.0–0.95) because a held-out
  race can run strategies absent from a corpus this small; the validation detail reports
  `unseen_labels` so the number is read in context.

## Configuration

Copy `backend/.env.example` to `.env`. Everything is environment-driven: database URL, regime and
season, poll cadences, model directory, validation margin, leakage threshold, and whether a passing
model auto-promotes (off by default).
