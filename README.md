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
| Replay geometry | OpenF1 `location` feed, reconstructed server-side |
| Frontend | Static HTML/JS/SVG, no build step, served by a zero-dependency Node server |

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

## The race map

The dashboard opens on the most recent race with a replay built: the circuit
drawn as a glowing mustard outline, every car a dot in its team's colour, moving
around the lap it actually drove, peeling into the pit lane when it stopped.
Scrub, play at up to 60x, and the running order, flag state and race-control log
follow the playhead.

None of that geometry is shipped with the project. OpenF1's `location` feed
gives raw track X/Y per car, and `app/data/replay.py` turns a finished session
into a cached bundle:

```bash
curl -X POST http://localhost:8000/replays/11353/build   # one job per session
curl http://localhost:8000/replays                       # what is built
npm run check:replay                                     # verify one end to end
```

The **Replays** tab lists every race that has run this season and builds any of
them on demand — one background job, a few minutes, then it plays instantly from
cache.

## The prediction panel

The tab pinned to the right edge answers "who finishes where from here?" at the
lap under the playhead, two ways that are deliberately not blended.

**The projection** starts from where the cars actually are — track position is
the strongest single predictor of a finishing order — and corrects it with the
time each car is expected to gain or lose from here:

```
delta = gap_to_leader - pace_advantage x laps_remaining x conversion
        + pit_loss x stops_owed
order = track position, weighted towards the projection by how much race is
        left for its corrections to come true
```

Every term is shown next to the result. Pace is measured against the quickest
quarter of the field rather than the median (half the grid is slower than the
median by construction, so measuring against it makes everyone look fast), over
green laps only. `conversion` damps the correction by how hard the circuit is
to overtake at: half a second a lap at Monaco buys a closer view of a gearbox.

**The model** is the trained finish-position estimator, run over the same lap
through the engineered feature vector, shown in its own column wherever the
session is in the database and a model is active for the regime. Where the two
disagree, a strategy is about to pay off or fail.

### How wrong is it?

A projected finishing order is unfalsifiable while a race runs, so the panel
quotes its own track record instead. Every stored projection is scored against
the classification of the race it was made in, alongside the trained model and
the baseline both have to beat — reading the running order and leaving it alone:

```bash
curl http://localhost:8000/replays/11353/accuracy   # one race, lap by lap
curl http://localhost:8000/predictions/accuracy     # everything evaluated
```

Measured over 194 laps of three finished races, mean absolute error in
finishing positions:

| stage | track position | projection | model |
|---|---|---|---|
| opening quarter | **2.81** | 2.88 | 3.58 |
| second quarter | 2.49 | **2.39** | 3.42 |
| third quarter | 2.24 | **2.22** | 3.31 |
| final quarter | 1.40 | **1.08** | 2.64 |
| overall | 2.21 | **2.12** | 3.23 |

This measure is what the project is for, and it has already earned its keep
twice. It caught the projection's first version, which was *worse* than doing
nothing and got worse as races ran on. And it settled how far to trust the
correction: sweeping the weight showed 0.15 is the best value and anything
heavier makes the order worse — including trusting the pit-stop term at full
strength, which looked like arithmetic on a known pit loss and scored 2.76,
because *whether a car still owes a stop* is inferred from a compound-life
table rather than observed.

The trained model is currently the worst of the three. The panel says so.

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
  data/       fastf1_client.py (completed sessions) · openf1_client.py (live)
              replay.py (circuit + playback reconstruction) · seed.py
  features/   transfer.py (the §2 table as code) · engineering.py (pure features) · builder.py
  models/     base.py · qualifying_model.py · race_model.py · registry.py (versioning + gate)
              replay_predictor.py (evaluate a whole replay, once)
              calibration.py (score the predictions against what happened)
  training/   dataset.py (labelling + matrix) · retrain_qualifying.py · retrain_race.py · jobs.py
  live/       base.py (polling threads) · qualifying_loop.py · race_loop.py · race_control.py
              projection.py (the prediction panel) · broadcast.py (subscriber queues)
  db/         models.py · database.py · projection_store.py (the projection cache)
              replay_store.py (built replays, shared through the database)
  main.py     REST + WebSocket routes
  cli.py      seed / backfill / status / promote
frontend/     server.js (static host + dev proxy, stdlib only)
scripts/      smoke.js (API + websocket) · replay-check.js (replay geometry)
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Regime, season, active models, running loops |
| GET | `/now` | What Connect opens on: live session, next session, last replay |
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
| GET | `/replays` | Every race that has run, and whether its replay is built |
| POST | `/replays/{session_key}/build` | Queue a replay build; returns a job |
| GET | `/replays/{session_key}` | The replay bundle (gzipped) |
| GET | `/replays/{session_key}/projection?lap=&fresh=` | Projected finishing order at a lap |
| POST | `/replays/{session_key}/predict` | Evaluate every lap and cache it; returns a job |
| GET · DELETE | `/replays/{session_key}/predictions` | Cache coverage · drop it |
| GET | `/replays/{session_key}/accuracy` | Score one race's projections against the result |
| GET | `/predictions/accuracy` | The same measure across every evaluated replay |
| WS | `/sessions/{id}/live` | `qualifying_update` / `race_update` / `race_control` |

## Tests

```bash
cd backend && .venv/Scripts/python -m pytest        # 146 tests, no network required
```

Coverage focuses on the parts that are expensive to get wrong: the transfer table, feature
completeness and fallbacks, the validation gate and promotion invariants, race-control
classification and gating, broadcast fan-out under a slow subscriber, loop resilience to a failing
poll, the leakage guards around training-data assembly, and the replay reconstruction — which is
tested against a synthetic circuit sampled the way the real feed samples, so the question asked is
"does a sparse, jittery feed come back as the shape it was sampled from" rather than "does one
recorded race still look right".

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
- **The position feed is far coarser than it looks.** A fix arrives about every 2.7 s -- a 250 m
  jump at racing speed, roughly thirty points per lap. Drawing those directly teleports cars across
  corners, so the circuit is reconstructed first (every green lap of the race folded onto one lap,
  then refined twice by projection) and cars are carried as *progress along that path*. The pit lane
  cannot be found by distance from the racing line, because a car running wide at a fast corner is
  further off line than a car in the pits; it is traced from the fixes around timed stops instead.
- **Scores from a five-event backfill:** qualifying-advancement accuracy 0.57–0.89, finishing
  position MAE 2.6–5.3 places. Race-strategy accuracy swings widely (0.0–0.95) because a held-out
  race can run strategies absent from a corpus this small; the validation detail reports
  `unseen_labels` so the number is read in context.

## Configuration

Copy `backend/.env.example` to `.env`. Everything is environment-driven: database URL, regime and
season, poll cadences, model directory, replay cache directory and frame rate, validation margin,
leakage threshold, and whether a passing model auto-promotes (off by default).
