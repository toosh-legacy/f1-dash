"""Tuning values for replay reconstruction.

Every number here was chosen against the feed's actual behaviour rather than
guessed, so each one carries the reasoning that picked it. They live together
because changing one usually means reconsidering its neighbours.
"""
from __future__ import annotations

#: Bundle format version. Bumped when the on-disk shape changes so stale caches
#: are rebuilt rather than misread.
BUNDLE_VERSION = 6

#: A sample further than this from the racing line is not on the racing line.
#: OpenF1 units are roughly decimetres, so this is ~15 m -- wide enough to keep
#: a car running off-line through a corner, tight enough to isolate pit lane.
PIT_LANE_MIN_DISTANCE = 150.0

#: Fixes arrive every ~2.7 s, so a car that has not reported for this long is
#: off track -- retired, or in the garage -- rather than frozen on the map at
#: its last known point.
SAMPLE_STALE_S = 20.0

#: A short distance in feed units (~2.5 m), used as the floor for spatial-index
#: cell sizes so a degenerate path cannot produce a zero-sized grid.
TRACK_POINT_SPACING = 25.0

#: Vertices in the reconstructed circuit outline. Around 8 m apart on a normal
#: lap: fine enough to draw the corners, coarse enough to stay small and to
#: keep a car's position along it meaningful.
TRACK_POINTS = 600

#: Fixes further than this from the racing line are not pit lane either; they
#: are garages, run-off excursions or bad fixes.
PIT_LANE_MAX_DISTANCE = 900.0

#: How much of the approach to, and escape from, a timed stop counts as being
#: in the pit lane. A stop is timestamped at the box, but the lane either side
#: of it is most of what there is to draw, so the window is generous.
PIT_APPROACH_S = 45.0

#: Within a stop window the car is known to be in the pit lane, so tracing can
#: accept fixes much closer to the racing line than :data:`PIT_LANE_MIN_DISTANCE`
#: -- the lane runs alongside the track, and at Zandvoort or Monaco it runs very
#: close indeed. The wider threshold is still what *classifies* a car as pitting
#: during playback, where there is no stop record to lean on.
PIT_TRACE_MIN_DISTANCE = 80.0

#: A lap slower than this multiple of the session's quickest is an in-lap, an
#: out-lap or a safety-car lap, and is not a picture of the racing line.
CLEAN_LAP_RATIO = 1.06

#: Refinement passes over the reconstructed outline. The first recovers the
#: corners the phase fold cut; the second is worth having; past that it only
#: chases sampling noise.
REFINE_PASSES = 2

#: Rolling window for the pace figure shown in the prediction panel.
PACE_WINDOW_LAPS = 5

#: Laps outside this band of the driver's own median are pit laps or traffic,
#: and are excluded from the pace figure.
PACE_OUTLIER_RATIO = 1.10

#: The share of the field whose pace is the benchmark. The quickest quarter is
#: what a car at the front is actually racing; the field median is not a
#: benchmark at all, since half the grid is slower than it by construction.
REFERENCE_PACE_QUANTILE = 0.25

_TEAM_FALLBACK_COLOUR = "9AA0A6"

#: The part of a bundle that is not playback data.
META_FIELDS = (
    "version", "built_at", "session", "duration_s", "frame_interval_s",
    "total_laps", "drivers", "track", "laps", "race_control", "events", "results",
)

# --------------------------------------------------------------------------
# Overtakes
# --------------------------------------------------------------------------
#: A car is not racing for this long either side of a visit to the pit lane.
#: Places handed over on the way in and taken back on the way out are the pit
#: cycle; counting those is how a quiet race produces a hundred overtakes.
PIT_SETTLE_S = 25.0

#: Being passed by this many cars on one lap is not a run of lost duels. It is
#: a car in trouble, and it is reported as that instead.
INCIDENT_MIN_PASSES = 4
