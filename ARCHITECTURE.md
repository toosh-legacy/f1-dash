# Architecture

What this system is, how the parts fit, and why each one is shaped the way it is
— with the video you'd watch to understand each piece, next to the piece itself.

- The specification is `guide.md`. It is the authority on *what* is built.
- `README.md` covers running it. `DEPLOY.md` covers shipping it.
- This file covers how it is put together, and how to learn it.

Every section ends with a **▶ Learn it** block: videos for the technology that
section uses, plus the files in this repo they unlock. Watch one, then open the
file it names and read it against the video. The code is heavily commented on
the *why*; the videos supply the *what*.

If you'd rather have a route than a tour, jump to
[Reading orders](#reading-orders) at the end and come back.

---

## 1. What it does, in one paragraph

Watch a finished Grand Prix play back on a circuit that was reconstructed from
the cars' own position data, and at any lap see who the system thinks finishes
where — twice over, once from arithmetic and once from a trained model — next
to a scoreboard saying how often each has actually been right. During a live
session the same panels update from a WebSocket instead of a scrubber.

## 2. The one invariant everything bends around

**Two clocks, never touching.**

| Clock | When it runs | What it does | Where |
|---|---|---|---|
| **Retraining** | Once, *after* a session ends | Labels the session's snapshots with real results, refits, gates, maybe promotes | `training/`, `models/registry.py` |
| **Live inference** | Continuously *during* a session | Feeds current state through the *already-trained* active model | `live/` |

A request path may **enqueue** a retrain and must return immediately. It may
never wait on one. This is enforced structurally: `POST /sessions/{id}/retrain`
hands work to a `ThreadPoolExecutor` in `training/jobs.py` and returns a job
record (`backend/app/api/training.py:19`). Nothing in `live/` imports `training/`.

Everything else in the codebase is downstream of this split.

## 3. The picture

```
        ┌───────────────────────────── browser (no build step) ────────────────────────────┐
        │  boot.js → board.js (grid) → map.js (SVG circuit + cars)                        │
        │            search.js (race finder)   prediction.js (projection + accuracy)      │
        │            live.js (WebSocket)       notices.js (race control, overtakes)       │
        └───────▲──────────────────────────────────────▲──────────────────────────┬───────┘
                │ REST: gzipped JSON bundles           │ WS: one msg per lap      │
     ┌──────────┴──────────────────────────────────────┴──────────────────────────▼──────┐
     │                    FastAPI  ·  backend/app/main.py  (assembly only)                │
     │        api/{meta,reference,sessions,training,predictions,models,live,replays}      │
     │        never trains in-request · never blocks on a build · hands work to the pool  │
     └──┬──────────────┬────────────────┬─────────────────┬───────────────┬───────────────┘
        │              │                │                 │               │
        ▼              ▼                ▼                 ▼               ▼
   live loops      job pool         registry         projection        replay
   (daemon        (1 worker,       + validation     + calibration    reconstruction
    threads)       CPU-heavy)          gate                          (build → bundle)
        │              │                │                 │               │
        └──────┬───────┴────────┬───────┴────────┬────────┴───────┬───────┘
               ▼                ▼                ▼                ▼
          OpenF1 REST        fastf1        model artifacts     SQLAlchemy 2
        live state + X/Y   completed        (.joblib)        SQLite → Postgres
                            sessions       via storage/blobs.py → S3
```

Threads publish to the browser through one crossing only:
`broadcast.publish_threadsafe` → `asyncio.run_coroutine_threadsafe` → a bounded
queue per subscriber. Bounded on purpose: a stalled client drops its oldest
messages instead of growing memory, because a stale live prediction is worthless
anyway (`backend/app/live/broadcast.py`).

> **▶ Learn it — Python concurrency**
> Why the loops are daemon *threads* while the API is an event loop, and why the
> single crossing between them is `run_coroutine_threadsafe`. This is also why
> route handlers touching `fastf1` are `def`, not `async def`: FastAPI runs sync
> handlers in a threadpool, which is exactly right for blocking I/O.
> - [AsyncIO vs Threading vs Multiprocessing in Python](https://www.youtube.com/watch?v=QlkXji08lno)
> - [Multithreading vs Multiprocessing vs Asyncio in 10 Minutes](https://www.youtube.com/watch?v=QXwefd3z8IU)
> - [Threads vs Async: Has Asyncio Solved Concurrency?](https://www.youtube.com/watch?v=NZq31Sg8R9E) — the opinionated take; watch after the other two
>
> **Unlocks:** `live/base.py`, `live/broadcast.py`, `training/jobs.py`

---

## 4. The HTTP surface — `app/main.py`, `app/api/`

`main.py` is assembly only: startup, shutdown, CORS, where the dashboard is
served from. The routes live in `api/`, grouped by what they are about — `meta`,
`reference`, `sessions`, `training`, `predictions`, `models`, `live`, `replays`
— and registered in one visible list in `api/__init__.py`, because FastAPI
matches in registration order and a list you can see is a list you can check.

`deps.py` holds only helpers used by *more than one* router; a helper needed by
exactly one lives next to its caller, where it reads better than it would behind
an import.

> **▶ Learn it — FastAPI**
> Routers, dependency injection, Pydantic response models, lifespan, and the
> threadpool behaviour above.
> - [FastAPI Tutorial for Beginners – Full Course](https://www.youtube.com/watch?v=VirndPTeRaw)
> - [FastAPI Beyond CRUD Full Course](https://www.youtube.com/watch?v=TO4aQ3ghFOc) — routers, structure, background tasks; closest to this codebase's shape
> - [FastAPI Full Crash Course](https://www.youtube.com/watch?v=rvFsGRvj9jo) — the fast version
>
> **Unlocks:** all of `api/`, `main.py`, `schemas.py`, `deps.py`

## 5. Data sources — `app/data/`

- `fastf1_client.py` — completed sessions (results, laps, stints, weather).
  Blocking, with its own on-disk cache.
- `openf1_client.py` — live state and raw track X/Y. No API key, and the same
  endpoints replay a finished session — which is how the live path gets exercised
  out of season.

> **▶ Learn it — F1 data and telemetry**
> Domain, not technique, and the fastest way to sanity-check whether a feature is
> sensible before you build it.
> - [Data Science applied to F1: the FastF1 library](https://www.youtube.com/watch?v=U3NQbOibbr8)
> - [F1 Data Analysis with Python](https://www.youtube.com/watch?v=INIyIpBXQRg)
> - [Visualizing F1 Performance from Telemetry Data](https://www.youtube.com/watch?v=HSV2FhPtRVI)
> - [I built this VIRAL F1 Python Project (open-sourced)](https://www.youtube.com/watch?v=TiQEElXyY2w) — a comparable project end to end
>
> **Unlocks:** `data/fastf1_client.py`, `data/openf1_client.py`, `data/seed.py`, and what every feature in `features/engineering.py` actually means

## 6. Replay reconstruction — `app/data/replay/`

The most distinctive subsystem. The position feed samples each car every ~2.7s
(~30 points a lap), which alone draws a polygon with the corners cut off.

- `geometry.py` — folds every green lap onto one lap so corners are sampled from
  many independent clock offsets, then re-places every fix along the resulting
  loop twice to pull the line onto the real geometry. The pit lane is traced
  separately, from fixes around *timed stops*: distance-from-racing-line can't
  find it, because a car running wide at a fast corner sits further off line than
  a car in the pits.
- `frames.py` — cars are carried as **progress along a path**, not as raw x/y.
  Interpolating then moves a car *through* the corner it drove instead of cutting
  the chord.
- `narrative.py` — laps, stints, pit windows, flags, overtakes, final order.
- `build.py` — the only module that talks to OpenF1 during a build; owns the
  order (geometry → frames → narrative) and reports progress per stage.
- `store.py` — bundles are immutable once built, so they're cached three ways:
  disk, an in-process parsed cache, and object storage in production.

Consequence: **a new circuit needs no new asset.** Nothing is shipped; everything
is derived.

## 7. Features — `app/features/`

- `engineering.py` — pure functions over an already-assembled `FeatureContext`.
  No network or database access, so the whole catalog is unit-testable without
  FastF1 or OpenF1. Every feature has a declared fallback; a gap becomes a
  documented neutral value plus an entry in `fallbacks_used`, never a silent
  `None` reaching XGBoost.
- `transfer.py` — the 2026 regulation-reset policy **as executable data**, not as
  a comment. `assert_policy_covered` runs at import and fails loudly if a feature
  is added without a declared policy. `row_sample_weight` drops pre-reset rows
  when the matrix contains regime-only features, down-weights partial carry-over,
  and those weights are handed to `fit()` — so the policy reaches the model rather
  than stopping at a docstring.

This is the single most important piece of judgment in the project.

## 8. Training — `app/training/`

- `dataset.py` — labelling (a finished session writes its real results back onto
  the snapshots that predicted it: today's prediction inputs are tomorrow's
  training data) and assembly of `(rows, labels, weights)` under the transfer table.
- `jobs.py` — one worker. Retraining is CPU-heavy and naturally serialised per
  session; more concurrency would just contend with inference for cores.
- `retrain_race.py`, `retrain_qualifying.py` — the two pipelines.

## 9. Models, registry, calibration — `app/models/`

- `base.py` — the shared XGBoost wrapper. Deliberately conservative: shallow
  trees, strong regularisation, few estimators. A season is a few hundred rows; a
  model that *cannot* overfit them is the goal, not a limitation.
- `registry.py` — versioning, the validation gate, promotion. A new version is
  always written and recorded, but only becomes active if it beats the active one
  on a held-out recent session by `VALIDATION_MIN_IMPROVEMENT`. Scores above
  `LEAKAGE_SUSPICION_SCORE` are treated as a leakage smell, not a win.
  `load_active` refuses a model trained under a different `regs_regime` than the
  session it is about to serve (`RegimeMismatch`).
- `calibration.py` — scores three answers side by side on finished races: **track
  position** (the baseline any prediction must beat), the projection, and the
  model. Mean absolute error in places, grouped by how far through the race the
  prediction was made. Reads the cache; never runs a model.
- `replay_predictor.py` — one background pass evaluating every lap of a replay
  into `projection_store`, keyed by model version. Promoting a model doesn't
  invalidate anything: the old rows simply stop matching and stay for comparison.

> **▶ Learn it — gradient boosting**
> The theory matters here. Knowing *why* boosting overfits small corpora is what
> makes the conservative hyperparameters in `base.py` read as a decision rather
> than as timidity.
> - [StatQuest: Gradient Boost & XGBoost playlist](https://www.youtube.com/playlist?list=PLZ5DHV9_5h9vQwAImmNi1RfoTtSuOUjwM) — the canonical explanation; watch Gradient Boost first
> - [Gradient Boost Part 1: Regression Main Ideas](https://www.youtube.com/watch?v=3CC4N4z3GJc)
> - [XGBoost Part 1: Regression](https://www.youtube.com/watch?v=OtD8wVaFm6E)
> - [XGBoost in Python from Start to Finish](https://www.youtube.com/watch?v=GrJP9FLV3FE) — the practical follow-up
>
> **Unlocks:** `models/base.py`, `models/race_model.py`, `models/qualifying_model.py`, and the sample-weight machinery in `features/transfer.py` + `training/dataset.py`

> **▶ Learn it — model registries and promotion gates**
> This repo hand-rolls what MLflow's registry does: versions, stages, a promotion
> decision. Learn the standard vocabulary, then read `registry.py` as one
> opinionated implementation of it — with a validation gate MLflow leaves to you.
> - [MLflow Crash Course — Model Registry and Deployment](https://www.youtube.com/watch?v=bDflB17YUNc)
> - [MLflow Model Registry: A Complete Guide](https://www.youtube.com/watch?v=cUxRj3Jc0lQ)
> - [Simplifying MLOps with Model Registry](https://www.youtube.com/watch?v=WrieKPgXZyo) — the concepts, tool-agnostic
>
> **Unlocks:** `models/registry.py`, `models/calibration.py`, `db/projection_store.py`

## 10. Serving — `app/live/`

- `base.py` — daemon-thread loop scaffolding: poll, decide whether anything worth
  predicting changed, run the active model, broadcast. A failed poll is logged and
  retried with backoff to 60s — an F1 session is 90 minutes and the upstream
  hiccups. `LoopStatus` tracks `last_poll_at`/`last_publish_at`, because the
  interesting failure isn't a stopped loop (obvious) but one still turning and
  silent for ten minutes.
- `race_loop.py` — race control is checked first on a faster cadence (a safety car
  can't wait for the next lap boundary); a new lap triggers inference; predictions
  under a non-green state are still produced but flagged `is_gated`.
- `qualifying_loop.py` — one update per period boundary.
- `broadcast.py` — session-scoped pub/sub, one bounded queue per subscriber.
- `projection.py` — the arithmetic answer, and why there are two:

  ```
  delta = gap_to_leader − pace_advantage × laps_remaining × conversion
                        + pit_loss × stops_owed
  order = track position, weighted toward the projection by how much race is left
  ```

  Damped by how hard the circuit is to overtake at — a pace advantage that can't
  be used isn't an advantage. Every term is shown in the panel, so a surprising
  order can be *read* rather than trusted. The projection and the model are never
  blended: where they disagree is the interesting part, usually a strategy about
  to pay off or fail.

> **▶ Learn it — WebSockets**
> Watch for the fan-out pattern: one queue per subscriber, bounded, drained
> independently so one slow client can't stall the loop.
> - [How WebSockets Work — Build a Chat with FastAPI & Python](https://www.youtube.com/watch?v=jQMl8E9hUp8)
> - [Building a WebSocket Chat with FastAPI and JavaScript](https://www.youtube.com/watch?v=5o__C9wJHZA) — both ends, which is what you need here
> - [Real-Time Chat with FastAPI and WebSockets](https://www.youtube.com/watch?v=QlDvbExTzcw)
>
> **Unlocks:** `api/live.py`, `live/broadcast.py`, `static/js/live.js`, and the upgrade proxy in `frontend/server.js`

## 11. Storage — `app/db/`, `app/storage/`

- `database.py` is the only module that knows which engine it is. No SQLite-only
  features anywhere, so Postgres is a connection-string change — and `config.py`
  rewrites `postgres://` / `postgresql://` to `postgresql+psycopg://`, because
  managed hosts hand out a scheme pointing at a driver that isn't installed.
- `models.py` — every `Session` carries a `regs_regime`; every `PredictionModel`
  records the regime it was trained under. Structural, not conventional.
- `projection_store.py`, `replay_store.py`, `session_index.py` — caches and indexes.
- `storage/blobs.py` — a **mirror**, not a filesystem abstraction. Callers keep
  writing and reading local paths and bracket them with `publish()` after a write
  and `materialize()` before a read. `local` makes both no-ops; `s3` turns local
  disk into a cache in front of a bucket that a cold container refills on first use.

> **▶ Learn it — SQLAlchemy 2.0**
> Specifically **2.0 style**: `Mapped[]` / `mapped_column`, `select()`,
> `session_scope`. 1.x tutorials will teach you the wrong syntax.
> - [SQLAlchemy ORM — Modern 2.0-style Model Classes](https://www.youtube.com/watch?v=T5n1UMD2c1w) — start here; it's the exact style used
> - [SQLAlchemy 2.0 ORM Crash Course](https://www.youtube.com/watch?v=XWtj4zLl_tg)
> - [SQLAlchemy ORM crash course part 2 — advanced](https://www.youtube.com/watch?v=mCMhzZLH7XE) — relationships, loading strategies
>
> **Unlocks:** `db/models.py`, `db/database.py`, all the `*_store.py` modules

## 12. Frontend — `backend/app/static/`

Plain ES modules, SVG, no build step. The map is a few hundred SVG nodes updated
on a rAF loop; a bundler would earn nothing.

| Module | Owns |
|---|---|
| `core.js` | API discovery + shared helpers. The one module with **no imports** — anything opinionated here would reach the whole page |
| `board.js` | The grid. Panels occupy whole cells, so two panels side by side are *exactly* side by side |
| `map.js` | Circuit drawn once on open; cars moved per frame via transforms, never by rebuilding paths |
| `prediction.js` | Projection panel + accuracy, reading the per-lap cache |
| `live.js` | One socket per session |
| `notices.js` | Race control calls and overtakes — the two things worth interrupting a viewer for |
| `search.js` | One list of races, past and future |
| `chrome.js` | Static field + boot sequence; stops when the tab is hidden, never animates under `prefers-reduced-motion` |
| `boot.js` | Order of operations: find API → board → open something → *then* wire the socket |

`frontend/server.js` is Node stdlib only: serves the page on its own origin (as a
CDN would) and proxies `/api/*` plus the WebSocket upgrade in development.

> **▶ Learn it — SVG and requestAnimationFrame**
> The key idea to carry in: build the DOM once, then animate **transforms**.
> `map.js` never rebuilds a path during playback, which is the whole reason a few
> hundred nodes stay smooth.
> - [requestAnimationFrame() — Beau teaches JavaScript](https://www.youtube.com/watch?v=tS6oP1NveoI)
> - [SVG + JavaScript: code an animated watch](https://www.youtube.com/watch?v=ULomsOSk4JA) — manipulating inline SVG from JS, exactly this pattern
> - [Complex SVG Animations Made Simple with JavaScript](https://www.youtube.com/watch?v=FVd5arHq0Zk)
> - [Frame-by-frame animation with requestAnimationFrame](https://www.youtube.com/watch?v=hzIPT9IvWJE) — interpolating between keyframes, i.e. what playback does between 1 Hz frames
>
> **Unlocks:** `static/js/map.js`, `board.js`, `chrome.js`

## 13. Two lifecycles worth tracing by hand

**Opening a race that has never been built**

```
search.js → GET /replays                       (catalogue: built? / buildable? / upcoming?)
         → POST /replays/{key}/build           → JobOut, returns immediately
            job pool → build.py → OpenF1 → geometry → frames → narrative
                     → store.py: gzip to disk + blobs.publish() → DB row
         → GET /replays/{key}/meta + /frames   (served gzipped, cache 1 day — a built bundle never changes)
         → map.js draws circuit once, then transforms per rAF tick
         → POST /replays/{key}/predict         → replay_predictor evaluates every lap into projection_store
         → GET /replays/{key}/projection?lap=N (rows, not a model run)
         → GET /replays/{key}/accuracy         (calibration vs track-position baseline)
```

**A live race lap**

```
race_loop thread: poll race control (fast cadence) ──► broadcast immediately
                  poll state → new lap? ──► features → registry.load_active(regime) → predict
                                          ──► store snapshot ──► broadcast
broadcast: publish_threadsafe → run_coroutine_threadsafe → bounded queue per client
browser:  live.js socket → renderOrder + notices
after the session ends: POST /sessions/{id}/retrain → job pool → label → assemble → fit → gate
```

## 14. Tests

`backend/tests/` mirrors the layers: `test_transfer_and_features.py`,
`test_registry_and_gate.py`, `test_projection.py`, `test_replay.py`,
`test_calibration.py`, `test_live.py`, `test_api.py`. End-to-end lives in
`tests/e2e/` under Playwright.

> **▶ Learn it — pytest and Playwright**
> Note how the pure-function design in `features/engineering.py` is what makes
> the tests possible without a network — that's the payoff of the design, seen
> from the test side.
> - [Pytest Tutorial – How to Test Python Code](https://www.youtube.com/watch?v=cHYq1MRoyI0)
> - [Pytest Full Course — Master Pytest in 2 Hours](https://www.youtube.com/watch?v=hoWPZMJ03UA)
> - [Please Learn How To Write Tests in Python • Pytest Tutorial](https://www.youtube.com/watch?v=EgpLj86ZHFQ) — the short motivating one
>
> **Unlocks:** `backend/tests/`, `tests/e2e/`, the `conftest.py` fixtures

## 15. Deployment

Split across two hosts, forced by what the backend does rather than by taste:
the static page on **Vercel**, the API container on **Render** (WebSockets held
open, background training threads, ~1 GB of ML wheels — past Vercel's 250 MB
function cap), rows in **Render Postgres**, artifacts and bundles in **S3** (or
any S3-compatible endpoint via `F1_S3_ENDPOINT_URL`). The Render disk is a warm
cache; losing it costs a download, not data. Full walkthrough in `DEPLOY.md`.

> **▶ Learn it — Docker and deployment**
> Layer caching matters here: ~1 GB of ML wheels means a badly ordered Dockerfile
> costs minutes on every push.
> - [This Is How You Write an Efficient Python Dockerfile](https://www.youtube.com/watch?v=tc713anE3UY) — layer ordering, exactly the constraint here
> - [Docker Tutorial for Beginners — Containerize Python Applications](https://www.youtube.com/watch?v=bi0cKgmRuiA)
> - [Docker Tutorial for Beginners — Full DevOps Course](https://www.youtube.com/watch?v=fqMOX6JJhGo) — the long one, if Docker is new
>
> **Unlocks:** `backend/Dockerfile`, `render.yaml`, `vercel.json`, `scripts/build-static.js`, `storage/blobs.py`, `DEPLOY.md`

---

## Reading orders

**The whole thing, in dependency order.** Sections 2–3 (concurrency) and 4
(FastAPI) are the backbone; 5–9 are the prediction half; 10–11 the serving and
storage; 12 the browser; 14–15 make it shippable.

**Just changing a feature or a model?** Sections 7 → 8 → 9, then read
`features/transfer.py` and `models/registry.py` directly. The gradient-boosting
and registry videos are the two that pay off fastest.

**Just changing the dashboard?** Section 12, then `boot.js` → `board.js` →
`map.js`. Nothing on the Python side is required.

**Reading the code cold, file by file:**

1. `guide.md` §§1–3 — the rules the code is obeying
2. `backend/app/config.py` — every tunable, in one place
3. `backend/app/main.py` → `api/__init__.py` — the whole HTTP surface in two files
4. `backend/app/db/models.py` — the data model
5. `backend/app/features/transfer.py` — the single most important piece of judgment
6. `backend/app/live/projection.py` — the arithmetic answer
7. `backend/app/models/registry.py` — the gate
8. `backend/app/data/replay/geometry.py` — the cleverest code here
9. `backend/app/static/js/boot.js` → `map.js` — the browser
