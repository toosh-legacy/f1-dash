# F1 Live Prediction Dashboard — 2026 season

Watch a Grand Prix back on a reconstructed circuit map, and see what a trained
model makes of it at any point in the race — with an honest account of how often
that model has been right.

Three things, working off the same data:

- **A race map.** Every car moving around the circuit it actually drove, drawn
  from the position feed rather than from any bundled track file, with pit stops,
  flags and the running order following the playhead.
- **A prediction, beside the map.** Who finishes where from this lap, with every
  term of the reasoning shown, plus the trained model's own answer in a column
  next to it.
- **A scoreboard for both.** Every prediction is scored against what actually
  happened, next to the baseline it has to beat — reading the running order and
  leaving it alone. When the predictions are not helping, the dashboard says so.

Underneath, per-car predictions run on **two separate clocks**:

- **Retraining** runs once, after a session ends, using that session's real
  results as new training data.
- **Live inference** runs continuously during a session — every lap in a race,
  every period in qualifying — feeding current live state through the
  already-trained model.

These are architecturally separate. A live request path never triggers
retraining.

Built to `guide.md`, which remains the specification; this README covers how it
is put together and how to run it.

## Architecture at a glance

```
            ┌───────────────────────────── the browser ─────────────────────────────┐
            │  entrance → race map + prediction tab · replay library · live view    │
            └──────▲──────────────────────────────▲────────────────────────┬────────┘
                   │ REST (JSON, gzipped bundles) │ WebSocket              │
       ┌───────────┴──────────────────────────────┴────────────────────────▼──────┐
       │                        FastAPI  ·  backend/app/main.py                   │
       │   never trains · never blocks on a build · hands long work to the pool   │
       └───┬──────────────┬───────────────┬────────────────┬───────────────┬──────┘
           │              │               │                │               │
           ▼              ▼               ▼                ▼               ▼
      live loops      job pool        registry        projection       replay
      (threads)      (1 worker)       + gate        + calibration   reconstruction
           │              │               │                │               │
           └──────┬───────┴───────┬───────┴────────┬───────┴───────┬───────┘
                  ▼               ▼                ▼               ▼
             OpenF1 REST       fastf1        model artifacts    database
           live state + X/Y   completed        (on disk)      (SQLAlchemy 2)
                                sessions
```

| Layer | What it owns | Where |
|---|---|---|
| **Data sources** | Completed sessions (results, laps, stints, weather) and live state (position, intervals, tyres, race control, raw track X/Y) | `data/fastf1_client.py`, `data/openf1_client.py` |
| **Replay reconstruction** | Turning a coarse position feed into a circuit, a pit lane, and every car's progress around them | `data/replay.py` |
| **Features** | The engineered vector, the fallbacks, and the regulation-transfer policy that decides what a 2026 model may learn from | `features/` |
| **Training** | Labelling a finished session, assembling the matrix, fitting, scoring | `training/` |
| **Registry** | Versioning, the validation gate, and deliberate promotion | `models/registry.py` |
| **Serving** | Live loops per session, the arithmetic projection, the trained model's answer | `live/` |
| **Measurement** | Scoring stored predictions against the classification of the race they were made in | `models/calibration.py` |
| **Storage** | Sessions, snapshots, models, race control, cached projections, shared replay bundles | `db/` |

## Tech stack

| Layer | Choice | Why this one |
|---|---|---|
| API | Python 3.13, FastAPI, native WebSockets | One language for the models and the service; a synchronous endpoint runs in FastAPI's threadpool, which is what the blocking `fastf1` calls need |
| Persistence | SQLAlchemy 2 + SQLite | Zero setup for a single machine, and nothing outside `database.py` knows which engine it is — Postgres is a connection-string change |
| Models | XGBoost | Gradient boosting on a few thousand rows of tabular features, which is the shape of the data; conservative hyperparameters because the corpus is small |
| Completed sessions | `fastf1` | The reference client for official timing, with its own on-disk cache |
| Live sessions | OpenF1 REST | No API key, and the same endpoints replay a finished session, which is how the live path is exercised out of season |
| Replay geometry | OpenF1 `location`, reconstructed server-side | Raw track X/Y per car; the circuit is derived from it rather than shipped, so a new track needs no new asset |
| Frontend | Static HTML/JS/SVG, no build step | The map is a few hundred SVG nodes updated on a rAF loop; a build pipeline would earn nothing |
| Frontend host | Node standard library only | Serves the page on its own origin, as it would behind a CDN, and proxies the API and the WebSocket upgrade in development |

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

## How the work flows

Four cycles, on four different clocks. Nothing in the fast ones waits on the
slow ones.

### 1. Before a weekend — seeding

`app.cli seed` pulls the season's calendar, circuits, teams and drivers from
FastF1 and writes them as rows. Circuit geometry that no feed carries — altitude,
average pit loss, historical safety-car rate, how hard it is to overtake — comes
from a small reference table in `data/seed.py`. This is the only step that
invents anything, and it is data, not code.

### 2. After a session ends — retraining

```
POST /sessions/{id}/retrain   →   job id, immediately
```

The request enqueues and returns; a background worker then ingests the session
from FastF1, labels it (a qualifying time, whether the driver advanced, a
finishing position, the strategy actually run), assembles a training matrix,
fits a new version, scores it on a held-out recent session, and records it.

It is **recorded**, not activated. The gate compares the candidate with the
active version and refuses a worse one; it also refuses an implausibly good one,
on the grounds that a model that has apparently solved motor racing has more
likely seen the answer. Even a passing model waits for `POST /models/{id}/promote`.

`app.cli backfill` runs this over a whole season in chronological order, which is
how the corpus is built from scratch.

### 3. While the cars are running — live inference

A polling loop per session, on its own thread:

```
race control  ──▶ classify ──▶ record ──▶ push immediately  (a safety car cannot
                                                             wait for the next lap)
new lap seen  ──▶ build features ──▶ active model ──▶ store ──▶ broadcast
```

Predictions during a safety car, VSC or red flag are still produced, but flagged
`is_gated` and rendered as low-confidence: a confident finishing position assumes
racing that is not happening.

### 4. After the fact — replay, evaluate, score

```
POST /replays/{key}/build     reconstruct the circuit and the race   (minutes)
POST /replays/{key}/predict   run the model over every lap, cached   (minutes)
GET  /predictions/accuracy    score all of it against what happened  (instant)
```

A finished race never changes, so all of this is done once and kept. The bundle
goes in the database, so one person's build is everyone's replay; the per-lap
projections go in a cache keyed by the model version that produced them.

## How that is possible

The interesting engineering is in what keeps those cycles from interfering.

**Long work never happens on a request path.** A single-worker thread pool owns
retraining, replay building and evaluation. Endpoints hand it a closure and
return a job record. Progress crosses back into the event loop through
`asyncio.run_coroutine_threadsafe`, so a background thread can push to a
WebSocket without either side knowing about the other.

**Features are computed as of a point in time.** Aggregates — team form, driver
profile, circuit history — are assembled with an `as_of` cutoff, so a session's
own results can never feed the features used to predict it. This is not
theoretical: without it, finishing-position error measured 0.0000, and the gate
refused the model for being suspiciously perfect. That refusal is what found the
bug.

**What a model may learn from is declared, not assumed.** 2026 reset the
regulations, so `features/transfer.py` carries a policy per feature: car, team
and tyre features train on 2026 data only; driver skill and track geometry may
use the full record; power-unit and pit-loss carry over at reduced weight. The
training pipeline queries it row by row, and a feature without a declared policy
fails at import rather than quietly learning from a car that no longer exists.

**Everything expensive is cached at the level it is expensive at.** Season
aggregates in process; built replays in the database, mirrored to disk; per-lap
projections in the database, keyed by model version so promoting a new model
makes the old answers stale rather than wrong. A lap costs about twenty seconds
cold and 150 ms warm.

**The geometry is derived, not shipped.** The position feed updates roughly every
2.7 seconds — a 250-metre jump at racing speed, about thirty points a lap — so
the circuit is reconstructed by folding every green lap of a race onto one lap
and refining it twice, and cars are then carried as *progress along that path*.
Interpolating progress moves a car through the corners it actually drove. Drawing
raw coordinates would cut every one of them.

**Predictions are measured, not asserted.** Because both the projections and the
results are stored, the whole prediction layer can be scored against the
classification, next to the baseline of doing nothing. That measurement is
wired into the dashboard, and it has already overturned two designs and one
plausible-sounding assumption. Anything here presented as an improvement was
measured to be one.

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

Everything above follows from four constraints the specification treats as
non-negotiable. Each is enforced in code rather than by convention, and each has
a file that would have to be defeated to break it.

| Rule | Enforced by | What breaks if it goes |
|---|---|---|
| **Retraining is a batch job**, never on a request path | `training/jobs.py` — endpoints enqueue and return a job record | A live viewer's request blocks for minutes behind a model fit |
| **Every model version passes a validation gate** before it can serve, and promotion stays deliberate | `models/registry.py` — refuses a worse score, and an implausibly good one as suspected leakage | A leaking or degraded model goes live silently |
| **The regulation-transfer table is executable policy** | `features/transfer.py` — queried per row; an undeclared feature fails at import | A 2026 model quietly learns from cars that no longer exist |
| **Live race predictions are gated off green** | `live/race_control.py` — classified, stored, flagged `is_gated` | A confident finishing position is shown for racing that is not happening |

`regs_regime` is a field on every session, snapshot and model — never a
hardcoded `"2026"` — so the next regulation reset does not require a rewrite. A
model trained under one regime is refused at serve time under another.

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
