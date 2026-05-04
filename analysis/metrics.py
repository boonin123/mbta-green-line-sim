"""
analysis/metrics.py
-------------------
Richer metric computations on top of raw RunResult / BatchResult data.

Five metric groups:

1. trip_duration_stats   — full distribution of train trip durations
2. delay_stats           — actual vs scheduled; delay per train
3. bunching_stats        — headway regularity (CV, bunching events)
4. station_stats         — per-station throughput, wait times, board rates
5. time_breakdown        — fraction of trip time in travel vs dwell vs breakdown

All public functions accept either a RunResult (single run) or a list of
RunResult objects (from a batch) and return plain dicts suitable for JSON
serialisation and Dash charting.

Usage
-----
    from sim.runner import single_run, batch_run, SimConfig
    from analysis.metrics import (
        trip_duration_stats, delay_stats, bunching_stats,
        station_stats, time_breakdown, batch_summary_table,
    )

    cfg = SimConfig(branches=["Green-D"], directions=[1],
                    day_type="weekday", start_time="07:00", duration_min=120)
    result = single_run(cfg)

    print(trip_duration_stats(result))
    print(bunching_stats(result))
"""

from __future__ import annotations

import math
import statistics
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sim.runner import BatchResult, RunResult

# Scheduled full-branch trip durations (seconds) from GTFS timetables.
# Used as denominator for delay calculations.
# Source: MBTA published schedules / GTFS median trip time (inbound, weekday)
SCHEDULED_TRIP_SEC: dict[str, float] = {
    "Green-B": 4080,   # ~68 min (Boston College → Government Center)
    "Green-C": 3300,   # ~55 min (Cleveland Circle → Government Center)
    "Green-D": 4080,   # ~68 min (Riverside → Union Square)
    "Green-E": 3000,   # ~50 min (Heath Street → Lechmere)
}

# Real-world on-time performance benchmarks by branch and day type.
# Source: MBTA OPMI Performance Dashboard, FY2024 weekday/weekend averages.
# Metric: % of trips arriving within 5 min of the published schedule.
# Values are approximate — check mbta.com/performance for the latest figures.
MBTA_OTP_REFERENCE: dict[str, dict[str, float]] = {
    "Green-B": {"weekday": 73.0, "saturday": 76.0, "sunday": 76.0},
    "Green-C": {"weekday": 76.0, "saturday": 79.0, "sunday": 79.0},
    "Green-D": {"weekday": 83.0, "saturday": 86.0, "sunday": 86.0},
    "Green-E": {"weekday": 77.0, "saturday": 80.0, "sunday": 80.0},
}

# Bunching threshold: two consecutive trains are "bunched" when the gap
# between them is less than BUNCH_THRESHOLD × mean headway at that point.
BUNCH_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(data: list[float], p: float) -> float:
    """Return the p-th percentile (0–100) of data."""
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _dist_summary(data: list[float]) -> dict:
    """Return a compact distribution summary dict (all values in original units)."""
    if not data:
        return {"n": 0}
    return {
        "n": len(data),
        "mean": statistics.mean(data),
        "std": statistics.stdev(data) if len(data) > 1 else 0.0,
        "min": min(data),
        "p25": _pct(data, 25),
        "p50": _pct(data, 50),
        "p75": _pct(data, 75),
        "p90": _pct(data, 90),
        "p95": _pct(data, 95),
        "p99": _pct(data, 99),
        "max": max(data),
    }


# ---------------------------------------------------------------------------
# 1. Trip duration stats
# ---------------------------------------------------------------------------

def trip_duration_stats(result: "RunResult") -> dict:
    """
    Full distribution of completed train trip durations for a single run.

    Returns
    -------
    dict with keys:
        durations_sec  — list of raw trip durations (seconds)
        summary        — _dist_summary dict (mean, std, p50, p90, etc.)
        n_trains_total — total trains spawned (including incomplete)
        n_trains_completed — trains that reached their terminus
        completion_rate — completed / total
    """
    completed = [
        t for t in result.trains
        if t.trip_duration() is not None
    ]
    durations = [t.trip_duration() for t in completed]

    return {
        "durations_sec": durations,
        "summary": _dist_summary(durations),
        "n_trains_total": len(result.trains),
        "n_trains_completed": len(completed),
        "completion_rate": len(completed) / max(1, len(result.trains)),
    }


# ---------------------------------------------------------------------------
# 2. Delay stats (actual vs scheduled)
# ---------------------------------------------------------------------------

def delay_stats(result: "RunResult") -> dict:
    """
    Per-train delay relative to the scheduled trip duration.

    Delay = actual_trip_duration - scheduled_duration
    Negative = faster than schedule (rare), positive = delayed.

    Returns
    -------
    dict with:
        delays_sec     — list of per-train delays in seconds
        summary        — distribution summary
        pct_on_time    — % of trains within 5 min (300s) of schedule
        pct_major_delay — % of trains > 10 min (600s) late
        mean_delay_min — mean delay in minutes
        schedule_sec   — the scheduled duration used as baseline
    """
    branches = result.config.branches
    # Use first branch's scheduled time; for multi-branch, use average
    schedule_sec = statistics.mean(
        SCHEDULED_TRIP_SEC.get(b, 4080) for b in branches
    )

    # Only consider trains on tracked branches with partial-route correction
    end_station = result.config.end_station_id
    if end_station:
        # Partial route: scale scheduled time proportionally by stops ratio
        from sim.network import Network
        net = Network(branches=branches)
        full_route = net.get_route(branches[0], result.config.directions[0])
        if end_station in full_route:
            ratio = (full_route.index(end_station) + 1) / len(full_route)
            schedule_sec *= ratio

    completed = [t for t in result.trains if t.trip_duration() is not None]
    delays = [t.trip_duration() - schedule_sec for t in completed]

    on_time = sum(1 for d in delays if d <= 300)
    major = sum(1 for d in delays if d > 600)

    return {
        "delays_sec": delays,
        "summary": _dist_summary(delays),
        "pct_on_time": on_time / max(1, len(delays)) * 100,
        "pct_major_delay": major / max(1, len(delays)) * 100,
        "mean_delay_min": statistics.mean(delays) / 60 if delays else None,
        "schedule_sec": schedule_sec,
    }


# ---------------------------------------------------------------------------
# 3. Bunching stats
# ---------------------------------------------------------------------------

def bunching_stats(result: "RunResult") -> dict:
    """
    Headway regularity analysis using inter-train arrival gaps at the
    midpoint station of the primary route.

    Bunching = two trains closer than BUNCH_THRESHOLD × mean headway.
    CV (coefficient of variation) = std / mean headway; higher = more irregular.

    Returns
    -------
    dict with:
        headway_gaps_sec   — list of gap seconds at midpoint station
        summary            — distribution summary
        cv                 — coefficient of variation (dimensionless)
        bunching_events    — count of gaps below BUNCH_THRESHOLD × mean
        bunching_rate      — bunching_events / total_gaps
        mean_headway_min   — mean headway in minutes
        reference_station  — station name used for measurement
    """
    gaps = result.headway_gaps

    if not gaps or len(gaps) < 2:
        return {
            "headway_gaps_sec": gaps,
            "summary": _dist_summary(gaps),
            "cv": None,
            "bunching_events": 0,
            "bunching_rate": 0.0,
            "mean_headway_min": None,
            "reference_station": None,
        }

    mean_gap = statistics.mean(gaps)
    std_gap = statistics.stdev(gaps)
    cv = std_gap / mean_gap if mean_gap > 0 else 0.0
    threshold = BUNCH_THRESHOLD * mean_gap
    bunched = sum(1 for g in gaps if g < threshold)

    # Identify reference station name
    branches = result.config.branches
    directions = result.config.directions
    ref_station = None
    try:
        from sim.network import Network
        net = Network(branches=branches)
        route = net.get_route(branches[0], directions[0])
        if route:
            mid_id = route[len(route) // 2]
            ref_station = net.stations[mid_id].name
    except Exception:
        pass

    return {
        "headway_gaps_sec": gaps,
        "summary": _dist_summary(gaps),
        "cv": round(cv, 3),
        "bunching_events": bunched,
        "bunching_rate": round(bunched / max(1, len(gaps)), 3),
        "mean_headway_min": round(mean_gap / 60, 2),
        "reference_station": ref_station,
    }


# ---------------------------------------------------------------------------
# 4. Station stats
# ---------------------------------------------------------------------------

def station_stats(result: "RunResult", top_n: int = 10) -> dict:
    """
    Per-station throughput, wait time, and board rate for a single run.

    Returns
    -------
    dict with:
        all_stations   — list of station metric dicts, sorted by boardings desc
        top_boardings  — top_n stations by total boardings
        worst_wait     — top_n stations by mean wait time
        lowest_board_rate — top_n stations with lowest board rate (most pressure)
        totals         — system-wide totals
    """
    metrics = result.station_metrics
    # Filter to stations that saw at least 1 passenger
    active = [m for m in metrics if m["total_arrived"] > 0]
    active.sort(key=lambda m: m["total_boarded"], reverse=True)

    totals = {
        "total_arrived": sum(m["total_arrived"] for m in active),
        "total_boarded": sum(m["total_boarded"] for m in active),
        "total_stranded": sum(m["total_stranded"] for m in active),
        "system_board_rate": (
            sum(m["total_boarded"] for m in active) /
            max(1, sum(m["total_arrived"] for m in active))
        ),
    }

    worst_wait = sorted(
        [m for m in active if m["mean_wait_sec"] is not None],
        key=lambda m: m["mean_wait_sec"],
        reverse=True,
    )[:top_n]

    lowest_board = sorted(
        active,
        key=lambda m: m["board_rate"],
    )[:top_n]

    return {
        "all_stations": active,
        "top_boardings": active[:top_n],
        "worst_wait": worst_wait,
        "lowest_board_rate": lowest_board,
        "totals": totals,
    }


# ---------------------------------------------------------------------------
# 5. Time breakdown
# ---------------------------------------------------------------------------

def time_breakdown(result: "RunResult") -> dict:
    """
    Decompose each completed train's trip time into:
      - travel time (in motion between stops)
      - dwell time (stopped at stations)
      - breakdown delay (parked due to fault)

    Derived from the train event logs.

    Returns
    -------
    dict with per-train breakdowns and aggregate fractions.
    """
    per_train = []

    for train in result.trains:
        if train.trip_duration() is None or not train.event_log:
            continue

        total_dwell = sum(
            e["dwell_sec"]
            for e in train.event_log
            if e["event"] == "arrived"
        )
        total_breakdown = train.total_breakdown_delay
        total_trip = train.trip_duration()
        total_travel = max(0.0, total_trip - total_dwell - total_breakdown)

        per_train.append({
            "train_id": train.train_id,
            "trip_sec": round(total_trip, 1),
            "travel_sec": round(total_travel, 1),
            "dwell_sec": round(total_dwell, 1),
            "breakdown_sec": round(total_breakdown, 1),
            "travel_pct": round(total_travel / total_trip * 100, 1) if total_trip else 0,
            "dwell_pct": round(total_dwell / total_trip * 100, 1) if total_trip else 0,
            "breakdown_pct": round(total_breakdown / total_trip * 100, 1) if total_trip else 0,
        })

    if not per_train:
        return {"per_train": [], "aggregate": {}}

    def _avg(key):
        return statistics.mean(t[key] for t in per_train)

    aggregate = {
        "mean_travel_pct": round(_avg("travel_pct"), 1),
        "mean_dwell_pct": round(_avg("dwell_pct"), 1),
        "mean_breakdown_pct": round(_avg("breakdown_pct"), 1),
        "mean_travel_sec": round(_avg("travel_sec"), 1),
        "mean_dwell_sec": round(_avg("dwell_sec"), 1),
        "mean_breakdown_sec": round(_avg("breakdown_sec"), 1),
    }

    return {"per_train": per_train, "aggregate": aggregate}


# ---------------------------------------------------------------------------
# Batch summary table
# ---------------------------------------------------------------------------

def batch_summary_table(batch: "BatchResult") -> list[dict]:
    """
    Flatten a BatchResult into a list of per-run summary dicts suitable
    for a Pandas DataFrame or Dash DataTable.

    Each row = one simulation run.
    """
    rows = []
    for i, s in enumerate(batch.run_summaries):
        rows.append({
            "run": i + 1,
            "n_trains": s.get("n_trains"),
            "mean_trip_min": round(s["trip_duration_mean_sec"] / 60, 1)
            if s.get("trip_duration_mean_sec") else None,
            "p50_trip_min": round(s["trip_duration_p50_sec"] / 60, 1)
            if s.get("trip_duration_p50_sec") else None,
            "p90_trip_min": round(s["trip_duration_p90_sec"] / 60, 1)
            if s.get("trip_duration_p90_sec") else None,
            "worst_trip_min": round(s["trip_duration_max_sec"] / 60, 1)
            if s.get("trip_duration_max_sec") else None,
            "best_trip_min": round(s["trip_duration_min_sec"] / 60, 1)
            if s.get("trip_duration_min_sec") else None,
            "breakdowns": s.get("total_breakdowns", 0),
            "total_boarded": s.get("total_boarded", 0),
            "total_stranded": s.get("total_stranded", 0),
        })
    return rows


# ---------------------------------------------------------------------------
# Full single-run report
# ---------------------------------------------------------------------------

def full_report(result: "RunResult") -> dict:
    """
    Compute all metric groups for a single run and return as a unified dict.
    Convenience wrapper for the dashboard's single-run view.
    """
    return {
        "config": {
            "branches": result.config.branches,
            "directions": result.config.directions,
            "day_type": result.config.day_type,
            "start_time": result.config.start_time,
            "duration_min": result.config.duration_min,
            "end_station_id": result.config.end_station_id,
            "pax_scale": result.config.effective_pax_scale,
            "breakdown_scale": result.config.breakdown_scale,
        },
        "trip_duration": trip_duration_stats(result),
        "delay": delay_stats(result),
        "bunching": bunching_stats(result),
        "stations": station_stats(result),
        "time_breakdown": time_breakdown(result),
        "wall_time_sec": result.wall_time_sec,
    }
