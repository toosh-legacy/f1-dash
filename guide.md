# GUIDE.md — F1 Live Prediction Dashboard (2026 Season)

## 1. What you're building

A live, F1-only dashboard with per-car predictions for the current 2026 season, updating on two
different clocks:

- **Retraining** happens once, after a session ends, using that session's real results as new
  training data.
- **Live inference** happens constantly during a session — every lap during a race, every period
  (Q1/Q2/Q3) during qualifying — feeding current live state through the already-trained model.

These are architecturally separate systems. A live request path must never trigger model
retraining. This is the single most important constraint in this document, and it applies
regardless of season — but 2026 specifically is why the rest of this guide looks the way it does,
covered next.

## 2. The 2026 regulation reset — why this shapes everything below

2026 is a ground-up technical reset, not an incremental rule change. This matters for a prediction
system more than almost any other kind of change F1 makes, because it resets which historical data
is still useful and which isn't. The actual changes:

- **Active aerodynamics replace DRS.** Movable front and rear wings switch between Z-mode (high
  downforce, deployed in corners) and X-mode (low drag, for straights).
- **"Manual Override" replaces DRS as the overtaking aid** — an on-demand burst of electrical power
  when within one second of the car ahead, deployable in one go or multiple shorter bursts. This
  makes energy deployment a genuine strategic resource to manage, not just a fixed drag-reduction
  zone.
- **Simplified hybrid power unit** — MGU-H is removed entirely; MGU-K power nearly triples (120kW to
  350kW), with a near-50/50 split between combustion and electric power, running on sustainable
  fuel.
- **Lighter, smaller cars** — minimum weight down 30kg to 768kg, wheelbase shortened 200mm, width
  cut 100mm, downforce down ~30%, drag down ~55%.
- **Narrower tires** — still 18-inch wheels, but fronts 25mm narrower and rears 30mm narrower.

**The practical consequence: don't treat 2026 as a flag to remember, treat it as the primary
regime this whole system is built for.** Pre-2026 seasons exist and have data, but only some of
that data is still relevant — the table below is the single most important piece of judgment this
project depends on, because getting it wrong means the model confidently learns patterns that no
longer exist.

### What transfers across the regulation reset, and what doesn't

| Feature category | Transfers from pre-2026 data? | Why |
|---|---|---|
| Car/team current pace ranking | **No** — 2026 data only | Every car is a new design under new rules; last year's competitive order is not informative |
| Team power-unit reliability history | **Weak/partial** — use cautiously | Engineering culture may partially carry over, but MGU-H removal and the new 50/50 split make the actual unit a new design |
| Tire degradation profile per circuit | **No** — 2026 data only | New tire dimensions and construction reset the degradation curve's shape, not just its scale |
| Track-specific "car suitability" (aero philosophy fit) | **No** | Aero rules changed too completely for old downforce/drag characteristics to map onto new cars |
| Driver historical baseline / skill rating | **Yes** | Driver skill is a human attribute, largely independent of the car |
| Driver track-specific history | **Yes** | Circuit layout knowledge and personal history at a track carry over |
| Driver wet-weather performance rating | **Yes** | Same reasoning — a human skill signal |
| Circuit characteristics (type, altitude, layout-driven historical SC rate) | **Yes**, if the layout is unchanged | Physical track geometry doesn't change with car regulations |
| Historical weather patterns at a circuit | **Yes** | Weather is independent of car regulations entirely |
| Pit lane time loss | **Mostly yes**, revalidate early in the season | Geometry is usually unchanged, but new car dimensions or pit lane regulation tweaks could shift it slightly — treat 2026 pit stop data as the source of truth once enough exists, and pre-2026 as a starting prior only |

Build every training pipeline in this project to read that table as a hard rule, not a suggestion:
car/team/tire features are trained on 2026 data only, growing as the season does; driver- and
track-geometry features may draw on the full historical record.

## 3. Tech stack (decided — do not re-litigate)

- **Backend:** Python, FastAPI, SQLAlchemy + SQLite for development (avoid SQLite-only features so
  a Postgres swap later is a connection-string change).
- **Prediction models:** XGBoost for all tabular predictions — the consistently proven approach
  across every reference F1 prediction project surveyed. Don't reach for deep learning without a
  specific, demonstrated reason, and remember the 2026 data volume is inherently small this season —
  a simpler model with good features will outperform a complex one with too little data to fit it.
- **Data sources:** `fastf1` (pip package) for historical/completed session data; OpenF1's
  REST/WebSocket API for live session data. Neither requires an API key.
- **Live updates:** native FastAPI WebSocket support, one endpoint per session.
- **Frontend:** a single static HTML/JS dashboard, no framework build step.

## 4. Reference implementation to follow

A working sibling project (`../lapcoach_app/backend/`) already implements the offline/online split
and the WebSocket broadcast pattern this project needs, tested end-to-end:

- `app/training.py` — the pattern for running a long job on a background thread without blocking
  the API's event loop, and safely crossing back into the event loop via
  `asyncio.run_coroutine_threadsafe` to broadcast progress. Apply the same pattern here: "PPO
  training checkpoint" becomes "new lap / new qualifying period," and "training thread" becomes
  "OpenF1 polling loop."
- `app/main.py` — the FastAPI + WebSocket route structure to mirror.
- `app/models.py` / `app/database.py` — the SQLAlchemy + SQLite setup pattern.

Read that project's code before starting — it answers "what does this actually look like in
working code" for the hardest architectural piece.

## 5. Repository layout to create

```
f1_prediction_dashboard/
  backend/
    app/
      data/
        fastf1_client.py       # historical/completed session data
        openf1_client.py       # live session data
      features/
        engineering.py          # feature computation — see §7, respect the transfer table in §2
      models/
        registry.py             # model registry: versioning, is_active, validation gate
        qualifying_model.py
        race_model.py
      training/
        retrain_qualifying.py    # offline job, triggered after a qualifying session ends
        retrain_race.py
      live/
        qualifying_loop.py       # live inference, per-period cadence
        race_loop.py              # live inference, per-lap cadence
        broadcast.py              # WebSocket subscriber/broadcast, mirrors reference training.py
      db/
        models.py                 # SQLAlchemy models, per §6
        database.py
      main.py                     # FastAPI app: REST + WebSocket routes
      static/
        dashboard.html
    requirements.txt
  guide.md                        # this file
```

## 6. Non-negotiable architectural rules

1. **Retraining is a batch job, never part of a live request path.**
2. **Every new model version must pass a validation gate before going live** — compare accuracy on
   a held-out recent session against the currently active version; never auto-promote a worse model.
3. **Respect the transfer table in §2 in every training pipeline.** Car/team/tire features train on
   2026 data only; driver- and track-geometry features may use the full historical record.
4. **Live race predictions must be gated during non-green-flag states.** Detect safety car / VSC /
   red flag from OpenF1 race control messages and suppress or clearly flag predictions as
   low-confidence rather than emitting a confident number that assumes normal racing.
5. **Build every feature in §7 marked "core" before any marked "extended."** Don't build
   speculative proxy features before there's evidence a core-only model actually needs them.
6. **Track a `regs_regime` field on every session** (currently always `"2026"` — keep it a field
   rather than a hardcoded assumption, since this same system will need to handle the *next*
   regulation reset someday without a rewrite).

## 7. Feature catalog

Organized by category. "Core" = build first, high-value and readily available. "Extended" =
valuable but needs more work to source or derive, or is lower-confidence — don't block on these.
Every entry already reflects the 2026 car and rules directly, per §2, not as a footnote.

### Car & team performance
| Feature | Source | Priority |
|---|---|---|
| Current-season (2026) qualifying/race pace percentile | FastF1, derived | Core |
| Recent form trend (last 3-5 race delta, 2026 only) | FastF1, derived | Core |
| Power unit manufacturer & spec (note: MGU-H removed, ~350kW MGU-K for all units this year) | FastF1 car metadata | Core |
| Reliability/DNF history, 2026 only | FastF1 historical results | Core |
| Fuel-corrected pace | Derived: ~0.03-0.05s/kg/lap correction against race distance completed | Extended |
| Upgrade package timing | Not in FastF1/OpenF1 — track manually, or infer as a pace changepoint | Extended |
| **Energy deployment efficiency (Manual Override usage)** | Proxy via speed-trap gains in Manual Override zones from OpenF1 telemetry | Core — new this year, don't skip |
| **Active aero mode usage (Z-mode vs. X-mode time split)** | OpenF1 telemetry if mode-state is exposed in the current season's schema — verify field availability first | Extended |

### Driver-specific (transfers from historical data per §2)
| Feature | Source | Priority |
|---|---|---|
| Driver historical baseline performance | FastF1 historical, full record | Core |
| Qualifying/race pace gap to teammate (2026 only, since this compares current cars) | FastF1, derived | Core |
| Track-specific driver history | FastF1 historical, full record | Core |
| Wet-weather performance rating | FastF1 historical, full record, filtered to wet sessions | Core |
| Consistency metric (lap time variance, incident rate) | FastF1, derived | Extended |
| Rookie/experience flag | FastF1 driver metadata | Core |
| New-to-team flag | Season metadata | Core |
| Historical grid-to-finish position delta | FastF1 historical, full record | Extended |

### Track/circuit characteristics (transfers per §2, assuming layout unchanged)
| Feature | Source | Priority |
|---|---|---|
| Circuit type (street/permanent/hybrid) | Static reference table | Core |
| Historical overtaking difficulty | FastF1/historical results, derived | Core |
| Historical safety car / VSC rate at this circuit | FastF1 race control historical logs | Core |
| Tire degradation profile of the circuit | **2026 data only** — FastF1 historical stint data, derived fresh this season | Core |
| Pit lane time loss | FastF1 historical; revalidate against 2026 data as it accumulates | Core |
| Corner-speed profile / % time at full throttle | Derivable from FastF1 telemetry | Extended |
| Elevation / altitude | Static geographic reference | Extended |

### Weather & track conditions (transfers per §2, independent of car regs)
| Feature | Source | Priority |
|---|---|---|
| Air temperature, track temperature, humidity | FastF1 / OpenF1 weather data | Core |
| Wind speed & direction | FastF1 / OpenF1 weather data | Extended |
| Rainfall / precipitation | FastF1 rain flag; a forecast needs an external weather API | Core |
| Track evolution (grip increase through a session) | Derived: fuel-corrected lap time improvement across a session, net of tire changes | Extended |
| Time of day / day vs. night race | Session schedule metadata | Core |

### Tire data (2026 data only, per §2 — new dimensions reset the degradation curve)
| Feature | Source | Priority |
|---|---|---|
| Compound per stint | OpenF1 (live) / FastF1 (historical) | Core |
| Tire age (laps on current set) | OpenF1 (live) / FastF1 | Core |
| Degradation rate per compound per circuit, 2026 only | FastF1 historical stint data, derived | Core |
| Tire allocation remaining for the weekend | Not in FastF1/OpenF1 — FIA publishes allocation sheets; track separately | Extended |
| Tire age × track temperature interaction | Derived | Extended |

### Race control / incidents
| Feature | Source | Priority |
|---|---|---|
| Safety car / VSC deployment (live) | OpenF1 race control (live), FastF1 (historical) | Core |
| Red flag | OpenF1 / FastF1 | Core |
| Yellow flag sectors | OpenF1 race control / track status | Extended |
| Grid penalties (power unit/gearbox changes) | Partially in FastF1/OpenF1; component-change penalties often need FIA steward document tracking | Extended |
| **Manual Override enabled status** | OpenF1 car data flag — verify field name against the current season's schema, since this replaces the old DRS field | Core |

### Strategic/regulatory context
| Feature | Source | Priority |
|---|---|---|
| Championship points situation | Derived from cumulative 2026 results | Extended |
| Constructors' championship proximity (team orders likelihood) | Derived from cumulative results | Extended |
| Sprint weekend format flag | Season calendar metadata | Core |

### Session & relative-position metadata
| Feature | Source | Priority |
|---|---|---|
| Session type (FP1/FP2/FP3/SQ/Sprint/Q1/Q2/Q3/Race) | Session metadata | Core |
| Current lap number / laps remaining | OpenF1 (live) | Core |
| Live gap to car ahead/behind | OpenF1 (live) | Core |
| On-track battle context (attacking/defending) | Derived from sustained sub-1-second gaps | Extended |

## 8. Data models

Field-level specification — translate to SQLAlchemy models following the pattern in the reference
implementation's `app/models.py`.

**Circuit** (static reference, seeded once): `id`, `name`, `type` (street/permanent/hybrid),
`altitude_m`, `avg_pit_loss_s`, `historical_sc_rate`, `historical_overtaking_difficulty`.

**Session**: `id`, `year`, `circuit_id` (FK), `session_type` (FP1/FP2/FP3/SQ/Sprint/Q1/Q2/Q3/Race),
`regs_regime` (string, currently always `"2026"` — see rule 6), `is_sprint_weekend`, `start_time`,
`status` (scheduled/live/completed), `weather_snapshot` (JSON: air_temp, track_temp, humidity,
wind_speed, wind_direction, rainfall).

**Driver / Team**: standard reference tables — `id`, `name`, `team_id` (Driver), `power_unit`
(Team), `is_rookie` (Driver), `joined_team_date` (Driver). Seed from FastF1 metadata.

**FeatureSnapshot**: the engineered feature vector actually used for a specific prediction —
`id`, `session_id` (FK), `driver_id` (FK), `context` (e.g. "Q2", "lap_23"), `features` (JSON, keyed
by feature name from §7), `created_at`. Store this — it's what makes predictions auditable and
becomes training data once results are known.

**PredictionModel** (the registry): `id`, `model_type` (qualifying_time / qualifying_advancement /
race_strategy / race_finish_position), `version`, `artifact_path`, `trained_at`,
`validation_score`, `is_active` (exactly one true per `model_type`), `regs_regime` (which regime
this model was trained under — never let a model trained under one regime silently serve
predictions for another).

**QualifyingPrediction**: `id`, `session_id` (FK), `driver_id` (FK), `period` (Q1/Q2/Q3),
`predicted_time_s`, `advancement_probability` (null for Q3), `model_version` (FK), `created_at`.

**RacePrediction**: `id`, `session_id` (FK), `driver_id` (FK), `lap_number`,
`predicted_finish_position`, `strategy_probabilities` (JSON, e.g.
`{"1-stop-medium-hard": 0.6, "2-stop-soft-medium-soft": 0.3}`), `is_gated` (bool, per rule 4),
`model_version` (FK), `created_at`.

**RaceControlEvent**: `id`, `session_id` (FK), `event_type` (green/yellow/safety_car/vsc/red_flag),
`lap_number`, `created_at`.

## 9. API specification

### REST

| Method | Path | Purpose |
|---|---|---|
| GET | `/circuits` | List all seeded circuits |
| GET | `/sessions?year=&circuit_id=&session_type=` | List/filter sessions |
| GET | `/sessions/{id}` | Session detail |
| POST | `/sessions/{id}/retrain` | Trigger the offline retraining pipeline for this session's now-complete results. Returns immediately (background thread) |
| GET | `/sessions/{id}/practice` | Current-form summary for a practice session — not a forward prediction |
| GET | `/sessions/{id}/qualifying/predictions?period=Q1\|Q2\|Q3` | Latest qualifying predictions |
| GET | `/sessions/{id}/race/predictions?lap=` | Latest race predictions, optionally at a specific lap |
| GET | `/models` | List the prediction model registry |
| POST | `/models/{id}/promote` | Flip `is_active` after manual review of a validation score — keep this a deliberate, separate action from retraining itself |

### WebSocket

`/sessions/{id}/live` — one message per period boundary (qualifying) or per lap (race), plus an
immediate `race_control` message on any state change (don't wait for the next scheduled update to
report a safety car).

```json
// qualifying
{"type": "qualifying_update", "period": "Q2",
 "predictions": [{"driver_id": "...", "predicted_time_s": 78.2, "advancement_probability": 0.71}]}

// race
{"type": "race_update", "lap_number": 23, "is_gated": false,
 "predictions": [{"driver_id": "...", "predicted_finish_position": 3.2,
                  "strategy_probabilities": {"1-stop-medium-hard": 0.6}}]}

// race control, pushed immediately
{"type": "race_control", "event_type": "safety_car", "lap_number": 24}
```

Follow the reference implementation's subscriber-queue pattern: one queue per session id, broadcast
via `asyncio.run_coroutine_threadsafe` from whichever thread/process is polling OpenF1.

## 10. Milestones

Work through these in order — each depends on the previous one actually working, not just
existing.

**M0 — Scaffolding.** Repository layout, empty FastAPI app that boots, database models from §8
with a working create-all step. *Done when:* the app boots and tables exist and can be queried.

**M1 — Data ingestion.** Build `fastf1_client.py` and `openf1_client.py`. Seed `Circuit`, `Driver`,
`Team` from real 2026 season data — this season is the primary corpus per §2, not historical
seasons. *Done when:* you can pull a real 2026 session's results via FastF1 and separately confirm
the OpenF1 client works against a completed session's historical window.

**M2 — Feature engineering (core features only).** Build every "Core" feature from §7, respecting
the transfer table in §2 — car/team/tire features from 2026 data only, driver/track features from
the full historical record. Store output as `FeatureSnapshot` rows. *Done when:* a real session
produces a complete core feature vector per driver with no missing values (implement the fallback
approach referenced for missing data rather than leaving gaps).

**M3 — Offline training + registry + validation gate (qualifying model first).** Build the
qualifying models, the registry, and the retraining job. Train primarily on 2026 sessions per §2.
Implement the validation gate. *Done when:* a real training run produces a model with accuracy in a
plausible range (reference targets from surveyed projects: ~70-75% for Q3-advancement-style
classification, high-80s% for podium-style classification — sanity checks, not requirements). A
score near 99% is much more likely a data-leakage bug than a good model — check for it before
celebrating.

**M4 — Live inference for qualifying + broadcast.** Poll OpenF1 (or replay historical 2026 data for
testing), detect period boundaries, re-run the active model, broadcast via WebSocket. *Done when:*
a WebSocket client receives a `qualifying_update` at each period boundary with predictions that
visibly move based on that period's actual results.

**M5 — Race models + live loop.** Same pattern as M3/M4 for race strategy and finish-position,
per-lap cadence. *Done when:* predicted finishing position visibly updates lap by lap in a sensible
direction, and strategy probabilities shift as tire age accumulates.

**M6 — Race control gating.** Build the race-control listener, populate `RaceControlEvent`,
implement rule 4. *Done when:* replaying a real session with a safety car period shows `is_gated:
true` during that window, and the `race_control` message arrives immediately, not at the next
scheduled update.

**M7 — Dashboard.** Three sections (practice/qualifying/race) per §9, following the reference
implementation's dashboard structure. *Done when:* predictions update in the browser without a page
refresh, in the correct section, with gating visibly reflected during a safety car window.

**M8 — Regime handling check.** Not new functionality — confirm `regs_regime` is populated
correctly everywhere (features, training data, predictions), and confirm the training weighting
from §2's transfer table is actually implemented in code, not just documented. Confirm Manual
Override tracking is wired in for at least the speed-trap-proxy version.

## 11. What "done" looks like

Point this at a real 2026 qualifying or race session, and the dashboard shows live-updating,
per-car predictions in the correct section, sourced from a model trained primarily on 2026 data and
validated against held-out results before promotion, with visible, correct handling of safety car
states rather than a confident-looking wrong number during one.
