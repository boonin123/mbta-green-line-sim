# MBTA Green Line Simulation — Project Guide for Claude

## Project Overview

This project is a discrete-event simulation (DES) of the MBTA Green Line — the most delay-prone line in the Boston transit system. The Green Line runs 4 branches (B, C, D, E) that merge into a single shared trunk, creating cascading delays from bunching, dwell time variance, and surface-street interference.

**Goal:** Build a project dashboard with two primary features:
1. **Batch I/O Analysis** — run 1000+ simulations, surface findings around expected vs. actual travel time, delay distributions, bunching frequency, etc.
2. **Interactive Map ("Ride the Train")** — simulate a single ride: user picks time, origin, and destination station; watch the train move in real time on the map.

---

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Simulation engine | **SimPy** (Python DES) | Industry-standard discrete-event simulation; natural fit for process-based train modeling |
| Dashboard / UI | **Dash** (Plotly) | Event-driven callbacks handle async simulation updates; better than Streamlit for interactive maps |
| Map rendering | **Plotly scatter_mapbox** | Native Dash integration; GTFS stop coordinates provide exact lat/lon |
| Data sources | **MBTA GTFS + V3 API** | Free, public, authoritative — used to fit real distributions rather than estimating |
| Testing | **pytest** | Standard Python testing |

**Note on stack choices:**
- "Solarium" is not a simulation library (it's an LED controller on PyPI). SimPy was chosen over alternatives.
- Mesa is an agent-based modeling framework, not suitable for DES transit simulation.
- Streamlit was considered but rejected: it re-runs the entire script on every interaction, which breaks animated single-run maps and long simulation runs.

---

## Green Line Network Structure

```
GLX (Medford/Tufts) ──┐
GLX (Union Square) ────┤
B (Boston College) ────┤
C (Cleveland Circle) ──┤─── Kenmore ─── Copley ─── Boylston ─── Park St ─── Govt Center ─── North Station ─── ...
D (Riverside) ─────────┘
E (Heath Street) ──────────────────── Copley ───┘
```

**Critical merge points:**
- **Kenmore**: B, C, D branches converge westbound. Primary congestion source.
- **Copley**: E branch joins the trunk.

**Track types:**
- Underground (D branch, trunk): predictable travel times
- Surface / street-running (B, C, E): subject to traffic signal delays, car interference
- B branch is worst: 24 surface stops from Kenmore to Boston College

---

## Repository Structure

```
mbta-green-line-sim/
├── CLAUDE.md                    # This file — project guide for Claude
├── README.md                    # User-facing project documentation
├── requirements.txt             # Python dependencies
├── .gitignore
│
├── data/
│   ├── gtfs/                    # Raw MBTA GTFS schedule files (downloaded)
│   │   ├── stops.txt            # All stop names + lat/lon coordinates
│   │   ├── stop_times.txt       # Scheduled arrival/departure times per trip
│   │   ├── trips.txt            # Trip → route mapping
│   │   └── routes.txt           # Route metadata
│   ├── distributions/           # Fitted distributions (JSON) for simulation inputs
│   │   ├── headways.json        # Inter-train arrival distributions by branch + time period
│   │   ├── dwell_times.json     # Dwell time distributions by stop type (surface vs underground)
│   │   ├── travel_times.json    # Segment travel time distributions (base + variance)
│   │   ├── passenger_arrivals.json  # Passenger arrival rates by station, day of week, time block
│   │   └── breakdown_rates.json # Train breakdown probability by branch
│   └── stations.json            # Canonical station list: name, coords, branch, stop_id, surface/underground
│
├── sim/
│   ├── __init__.py
│   ├── network.py               # Green Line graph: stations, segments, branch definitions, merge points
│   ├── train.py                 # SimPy Train process: movement, dwell, breakdown logic
│   ├── station.py               # SimPy Station: passenger queue, boarding/alighting logic
│   ├── passenger.py             # Passenger arrival process (Poisson arrivals from fitted rates)
│   └── runner.py                # Batch runner (N runs) + single-run runner with event log
│
├── analysis/
│   ├── __init__.py
│   ├── metrics.py               # Stats: travel time, delay, headway regularity, bunching index
│   └── fit_distributions.py    # Scripts to fit distributions from GTFS/API data
│
├── dashboard/
│   ├── __init__.py
│   ├── app.py                   # Dash app entry point
│   ├── batch_view.py            # I/O analysis layout: charts, stats, findings
│   └── map_view.py              # Interactive single-run map with train animation
│
└── tests/
    ├── test_network.py
    ├── test_train.py
    ├── test_station.py
    └── test_metrics.py
```

---

## Data Layer

### MBTA GTFS
- Downloaded from: https://www.mbta.com/developers/gtfs
- Updated several times per month; re-download periodically
- Key files: `stops.txt`, `stop_times.txt`, `trips.txt`, `routes.txt`
- Green Line route IDs: `Green-B`, `Green-C`, `Green-D`, `Green-E` (and GLX branches)

### MBTA V3 API
- Base URL: `https://api-v3.mbta.com`
- No key required for testing; register for key to get 1000 req/min
- Key endpoints used:
  - `/predictions?filter[route]=Green-B,...` — real-time predictions
  - `/vehicles?filter[route]=Green-B,...` — live vehicle positions
  - `/schedules?filter[route]=...` — scheduled times
- API key stored in `.env` (never committed)

### Distribution Fitting (`analysis/fit_distributions.py`)
Distributions are fit from GTFS scheduled times and (where available) historical performance data:
- **Headways**: computed from consecutive scheduled departure times at terminus stops; fit to lognormal
- **Dwell times**: estimated from stop_times gap between arrival and departure; fit to gamma
- **Travel times**: segment-by-segment from stop_times; fit to lognormal (surface) or normal (underground)
- **Passenger arrivals**: Poisson process; λ varies by station × day-of-week × time block (7 time blocks: early morning, AM peak, midday, PM peak, evening, late night, overnight)

---

## Simulation Design

### Core SimPy Processes

**Train process** (`sim/train.py`):
```
for each segment in route:
    travel(segment)  → timeout(travel_time_sample)
    arrive_at_station()
    dwell()          → timeout(dwell_time_sample(boarding + alighting count))
    possibly_break_down()  → timeout(repair_time_sample)
```

**Station process** (`sim/station.py`):
- Maintains a passenger queue (SimPy Store or Container)
- Passengers arrive via Poisson process (rate from fitted λ)
- When train arrives: transfer min(capacity_remaining, queue_size) passengers
- Track: waiting time per passenger, overflow (missed trains)

**Merge point logic** (`sim/network.py`):
- Kenmore modeled as a SimPy Resource with capacity=1 (one train through at a time)
- Train requests the resource → held until resource free → releases after passing
- First-come-first-served for V1; priority scheduling a future enhancement

### Key Simulation Parameters

| Parameter | Source | Notes |
|-----------|--------|-------|
| Train capacity | 176 passengers (Type 8 LRV) | Standard Green Line car |
| Peak headway (per branch) | ~7 min | Fit from GTFS |
| Trunk headway | ~2 min | Computed from merged schedule |
| Breakdown rate | ~2-3% of trips | From MBTA performance reports |
| Surface segment variance | ±30-60s | Higher on B branch |

### Short-Turns
Some Green Line trains terminate early (e.g., a B branch train ends at Kenmore rather than Boston College). This is modeled in the schedule data and must be reflected in the simulation — a significant real-world delay source.

---

## Output / Dashboard Design

### Batch I/O Analysis View
Inputs:
- Number of runs (default: 1000)
- Day of week
- Time window (e.g., 7am–9am)
- Branch(es) to include
- Special event toggle (passenger surge injection)

Key output metrics:
- Expected vs. actual median travel time (by branch, by trip)
- Delay distribution (p50, p75, p90, p99)
- Longest / shortest simulated ride
- Bunching frequency (headway CV > threshold)
- Average passenger wait time by station
- Train capacity exceedance events

### Interactive Map View ("Ride the Train")
Inputs:
- Start station
- End station
- Departure time
- Day of week

Output:
- Animated Plotly map showing train position updating in real time (driven by simulation event log)
- Sidebar showing: current stop, next stop, estimated arrival, crowding level
- Trip summary on completion: total time, delay vs. schedule, stops made

---

## Implementation Phases

### Phase 1: Data Layer ✅ COMPLETE
- [x] Download and parse MBTA GTFS files
- [x] Build `stations.json` — 70 physical stations, lat/lon, branches, surface flag, stop_ids
- [x] Write `analysis/fit_distributions.py` — headways (24 series), travel times (146 segments)
- [x] Produce all `data/distributions/*.json` files
- [x] Write `data/stations.json`

### Phase 2: SimPy Simulation Core ✅ COMPLETE
- [x] `sim/network.py` — Network graph: loads stations, routes, segment dists, headways, dwell params; exposes `sample_travel_time`, `sample_dwell`, `sample_headway`, `init_merge_points`
- [x] `sim/station.py` — SimulatedStation: platform queue, boarding/alighting with dwell coupling (`dwell = base + 0.5s×board + 0.3s×alight`), per-station metrics
- [x] `sim/passenger.py` — Poisson arrival process keyed to time block; `seed_initial_passengers` for warm-start
- [x] `sim/train.py` — Train SimPy process: per-stop alight/board/dwell, segment travel, breakdown injection, merge Resource request; `train_dispatcher` spawns trains at headway intervals
- [x] `sim/runner.py` — `single_run(SimConfig)` and `batch_run(SimConfig, n)` with event log, trip metrics, headway gap collection; CLI entry point
- [ ] Unit tests for all sim components (Phase 3 prerequisite)

**Verified smoke test (D branch, 120 min, weekday AM):**
- 100 runs complete in ~3 seconds
- Trip duration: mean ~100 min, p90 ~102 min, best ~91 min, worst ~110 min
- Breakdowns: ~5/run, 99% of runs affected
- Trains/run: ~30

**Known issues to address in calibration pass (Phase 5):**
- Trip duration ~100 min vs ~60 min scheduled — passenger arrival rates need scaling down for single-branch sim (rates are system-wide, not per-branch)
- `total_overflow` in station metrics double-counts passengers who miss multiple consecutive trains; needs to become a per-unique-passenger metric

### Phase 3: Analysis Layer (next)
- [ ] Implement `analysis/metrics.py` — delay, travel time, bunching stats richer than runner summary
- [ ] Unit tests for sim components (`tests/`)
- [ ] Validate: p50 travel time should match MBTA schedule ± reasonable variance
- [ ] Extend to full Green Line (add B, C, E branches + merge Resource logic at Kenmore/Copley)

### Phase 4: Dash Dashboard
- [ ] `dashboard/app.py` — app scaffold, layout router
- [ ] `dashboard/batch_view.py` — batch I/O charts and stats
- [ ] `dashboard/map_view.py` — single-run animated map

### Phase 5: Polish & Calibration
- [ ] Calibration pass: scale passenger arrival rates for single-branch context; validate p50 trip time against GTFS schedule
- [ ] Fix overflow metric to track unique stranded passengers
- [ ] Special event injection (passenger surge via SimConfig)
- [ ] Weather factor (surface segment variance multiplier)
- [ ] GLX branches (Union Square, Medford/Tufts — already in GTFS data)

---

## Development Notes

### Environment Setup
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Running the Simulation (CLI)
```bash
# Single run — D branch inbound, weekday AM peak, 120 min window
python -m sim.runner --mode single --branch Green-D --direction 1 --day weekday --start-time 07:00 --duration 120

# Batch run — 1000 simulations
python -m sim.runner --mode batch --branch Green-D --direction 1 --day weekday --start-time 07:00 --duration 120 --runs 1000
```

### Running the Dashboard
```bash
python dashboard/app.py
# Opens at http://localhost:8050
```

### Running Tests
```bash
pytest tests/ -v
```

### MBTA API Key
Store as `MBTA_API_KEY` in a `.env` file at project root. Never commit `.env`.

---

## Key Design Decisions & Rationale

| Decision | Rationale |
|----------|-----------|
| SimPy over Salabim | Larger community, more transit simulation examples, better documentation |
| Dash over Streamlit | Streamlit re-runs entire script on every interaction; breaks long simulation runs and animated maps |
| D branch for MVP | Grade-separated, no surface interference, simplest topology — isolates SimPy core logic |
| GTFS for distribution fitting | Real data over estimates — dramatically increases simulation credibility |
| Trunk as SimPy Resource | Models actual physical constraint: one train at a time through the shared underground section |
| Lognormal for surface segments | Right-skewed: occasional very long delays from traffic, but a floor at minimum travel time |
| Dwell time coupled to pax count | `dwell = base_sample + 0.5s×board + 0.3s×alight` captures bunching cascade: late train → more pax → longer dwell → later still |
| Effective door rate 0.5s/pax | Type 8 LRV has 4 sets of double doors (8 streams); TCRP single-door rate of 2.5s / 8 ≈ 0.31s; 0.5s adds crowding margin |
| Passengers modeled, not ignored | Without passenger coupling, simulation outputs delay distributions but not *why* delays compound — misses the core Green Line phenomenon |

---

## Known Limitations & Future Work

- **Merge scheduling**: V1 uses FCFS at Kenmore. Real MBTA uses schedule-based dispatching — a future improvement would implement headway-based hold/release logic.
- **Passenger alighting**: V1 assumes constant fraction alight at each stop. Future: OD matrix from AFC tap data.
- **Signal priority**: Green Line has transit signal priority (TSP) at some intersections. Not modeled in V1.
- **Two-car trains**: Some peak runs use coupled LRVs (352 passenger capacity). V1 uses single-car only.
- **GLX branches**: Union Square and Medford/Tufts added in 2022. Not in V1 scope.
