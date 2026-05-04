# MBTA Green Line Simulation — Backlog & Changelog

---

## Changelog

### Phase 4 — Dash Dashboard ✅ (2026-03)
- `dashboard/app.py` — Dash 4 app with Bootstrap navbar, `/batch` and `/map` routing, `register_callbacks` pattern
- `dashboard/batch_view.py` — batch I/O analysis: duration histogram, delay CDF, bunching chart, top-10 station boardings/wait-time dual axis, per-run results table, 4 stat cards; origin/destination filter with cascade + route badge
- `dashboard/map_view.py` — animated train map: origin/dest pickers, target arrival time, route inference, journey trimming, play/pause/reset, crowding badge, stop timeline, trip summary card; auto-zoom to route bounds

### Phase 3 — Analysis Layer ✅ (2026-03)
- `analysis/metrics.py` — trip duration stats, delay vs schedule, bunching CV, station throughput, time breakdown (travel/dwell/breakdown), batch summary table, full report
- 95 unit tests across `tests/test_network.py`, `test_station.py`, `test_train.py`, `test_metrics.py`
- Overflow metric renamed: `total_overflow` → `total_stranded` (unique) + `total_missed_boardings` (cumulative)

### Phase 2 — SimPy Simulation Core ✅ (2026-02/03)
- `sim/network.py`, `sim/train.py`, `sim/station.py`, `sim/passenger.py`, `sim/runner.py`
- Calibrated to D branch: p50 ~72 min midday, ~78 min AM peak vs 68 min Google Maps (realistic overhead)
- Dwell recalibrated to excess-only (GTFS double-count fix); `pax_scale = n_branches/4`; `breakdown_scale = 0.5`
- Multi-branch validated: Kenmore + Copley merge Resources, trunk headway ~3.3 min with 3 branches, CV = 0.315

### Phase 1 — Data Layer ✅ (2026-02)
- 70 stations in `data/stations.json`; GTFS-fitted distributions for headways, travel times, dwell, passenger arrivals, breakdown rates

### Phase 6 — Hosting & Documentation ✅ (2026-03)
- `render.yaml` — one-click Render.com deployment; `gunicorn` added to `requirements.txt`
- `README.md` — full rewrite with live demo link, 6 screenshot embeds, setup/CLI/hosting docs, project structure table
- `docs/screenshots/` — automated Playwright screenshots: landing, batch (empty + results), map (empty + mid-animation + end modal)
- `docs/take_screenshots.py` — headless Chromium screenshot script for future re-capture
- Departure-time sim fix: removed `trip_duration() is None` guard from candidate filter; trains only need origin + dest arrivals, not full route completion

### Phase 5 — Dashboard Enhancements ✅ (2026-03)
- `dashboard/landing_view.py` — landing page with animated hero (CSS keyframe train), real-lat/lon Plotly GL schematic coloured by branch, stats bar (stations, headways, capacity), feature cards, dual CTA buttons for Batch and Ride views
- `dashboard/map_view.py` — end-of-ride stats modal: after playback reaches destination, shows 4 stat cards (your time, p50, p90, percentile rank), breakdown count, average crowding badge; comparison drawn from 30 background batch runs of the same route/time
- `dashboard/app.py` — `/` now routes to landing page; "Home" added to navbar

### Bug Fixes (2026-03)
- **Union Square / Medford/Tufts topology** — Union Square incorrectly placed as last stop of E branch (no `stop_sequence` for Green-E → sorted to 9999). Fixed by removing `Green-E` from Union Square's `branches` in `stations.json`. E branch now correctly terminates at Medford/Tufts; D branch at Union Square.
- **Map default zoom** — was hard-coded to Boston city center zoom 12. Now computed from actual GL station bounding box (42.32–42.41°N, 71.06–71.25°W) → zoom 11.
- **Batch analysis origin/dest filter** — added stop-to-stop filtering to batch view; infers branch/direction from station pair, passes `end_station_id` to SimConfig.
- **GLX branch topology** — East Somerville, Gilman Square, Magoun Square, Ball Square, and Medford/Tufts were incorrectly assigned to Green-B and Green-C branches. These are E-branch-only GLX stations. Fixed by removing Green-B/C from their `branches`, `stop_ids`, and `stop_sequence` in `stations.json`. Resolves missing routes like Prudential → Ball Square.

---

## Backlog

Items are roughly prioritised: **P1** = high value / quick win, **P2** = meaningful but more work, **P3** = nice to have / polish.

---

### Dashboard / UX

| Pri | Item | Notes |
|-----|------|-------|
| ~~P1~~ | ~~**Landing page / "Ride the Train" intro screen**~~ | ✅ Done — `landing_view.py`: animated train hero, real GL schematic, stats bar, feature cards. |
| ~~P1~~ | ~~**End-of-ride stats pop-up card**~~ | ✅ Done — modal shows your time vs p50/p90, percentile rank, breakdown count, crowding; 30 background batch runs. |
| P1 | **Loading state / progress indicator** | Batch runs block the main thread. Show a spinner and disable the button while running. For large runs (500+), consider a progress bar updated via a polling interval. |
| P2 | **Multi-train trips** | Let user pick an origin and destination that require a transfer (e.g. B branch → trunk → E branch). Detect when direct route doesn't exist; propose the best transfer station; simulate both legs and show combined travel time and transfer wait. |
| P2 | **Batch scenario comparison** | Run two configs side-by-side (e.g. "weekday AM peak" vs "weekday PM peak") and overlay their distribution plots. Useful for "what if headways improved?" analysis. |
| P2 | **Dark mode** | Bootstrap `data-bs-theme="dark"` toggle; Plotly `template="plotly_dark"`. |
| P2 | **Export to CSV** | Dash `dcc.Download` — export per-run batch table or station stats to CSV for further analysis. |
| P3 | **Mobile layout** | Sidebar collapses to a top drawer on narrow screens; map fills viewport. |
| P3 | **Keyboard shortcuts** | Space = play/pause, R = reset, left/right arrows = step frame. |

---

### Simulation Accuracy & Data

| Pri | Item | Notes |
|-----|------|-------|
| ~~P1~~ | ~~**Outer-branch boarding saturation (alight fractions)**~~ | ✅ Fixed — `ALIGHT_FRACTIONS` is now direction-aware. Inbound fractions unchanged (preserves calibration). Outbound branch fractions increased: `branch_outer` 0.10→0.18, `branch_main` 0.15→0.22. After 8 outer stops outbound, train sheds 76% of load vs 42% before. |
| P2 | **pax_scale differentiation for branch vs trunk stations** | Follow-up to the saturation investigation. Branch-only stations (outer/main/terminus) exclusively serve their branch's passengers and should use `pax_scale=1.0` regardless of how many branches are simulated. Currently they use the same `n_branches/4` scale as trunk stations, under-counting arrivals ~4× for single-branch runs. Fixing this would increase branch-station arrivals significantly and requires recalibration of trip duration benchmarks. |
| P3 | **Short-turn trains** | Decided not to model: real MBTA short-turns are mainly late-night service cut at Government Center (not mid-branch at Kenmore), making it too nuanced to have meaningful impact on AM/PM peak scenarios. |
| P2 | **Two-car trains during peak** | Type 8 LRVs can couple to 352-pax capacity. V1 uses single-car (176 pax) always. Add `coupled_train_prob` parameter keyed to time block; double capacity when active. |
| P2 | **Weather multiplier** | Surface segment travel times increase in snow/rain. Add a `weather` parameter (`clear`, `rain`, `snow`) that scales `sigma` of surface lognormal distributions. MBTA performance data shows measurable winter degradation on B/C/E. |
| P2 | **Signal priority (TSP) modelling** | Green Line has Transit Signal Priority at several intersections. Currently surface variance is uniform. Could add a reduced-variance mode for TSP-equipped segments. |
| P2 | **GLX headway calibration** | Union Square and Medford/Tufts were added in 2022. Fitted headways from GTFS may not reflect post-GLX service patterns. Re-fit using 2023–2024 GTFS data for D/E branches at GLX stations. |
| P3 | **Wheelchair/ADA dwell penalty** | ADA ramp deployment adds ~15–30s at accessible surface stops. Small overall effect but noticeable at key stops. Could be modelled as a low-probability dwell extension. |
| P3 | **Operator rest time at terminus** | MBTA operators have a minimum layover (~2–3 min) before a train re-enters service. Affects effective headway at peak. Could add `MIN_TURNAROUND` enforcement in the dispatcher. |

---

### Analysis

| Pri | Item | Notes |
|-----|------|-------|
| P1 | **Real-world benchmark comparison** | MBTA publishes monthly on-time performance by route. Add a reference line or table to the batch view showing actual GL on-time % vs sim output for the same conditions. |
| P2 | **"What if" headway scenarios** | Parameterised headway override (e.g. "reduce peak headway from 7 min to 5 min") — run batch and compare against baseline. Directly answers the "would more trains help?" question. |
| P2 | **Worst-case scenario finder** | Batch run across all combinations of time period × day type × weather, identify which combination produces the highest p99 trip time. |
| P2 | **Station dwell pressure over time** | For a single run, plot `waiting_passengers` at each station as a heatmap over simulation time — shows where and when platforms are overwhelmed. |
| P3 | **OD matrix boarding model** | Replace the constant-fraction alighting model with an origin-destination matrix from MBTA AFC tap data. More accurate crowding distribution along route. |
