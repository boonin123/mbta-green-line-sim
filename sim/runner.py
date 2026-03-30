"""
sim/runner.py
-------------
Orchestrates simulation runs — both single-run (for the interactive map)
and batch (for I/O analysis).

Single run
----------
Returns a RunResult containing:
  - All train event logs (list of dicts) — drives the map animation
  - Per-station metrics (boardings, wait times, overflow)
  - Per-train metrics (trip duration, breakdowns)
  - Aggregate metrics (delay distribution, bunching index)

Batch run
---------
Runs N independent single runs with identical config but different random seeds.
Returns a BatchResult with aggregated statistics across all runs.

CLI usage
---------
    # Single run
    python -m sim.runner --mode single --branch Green-D --direction 1 \
        --day weekday --start-time 08:00 --duration 120

    # Batch run
    python -m sim.runner --mode batch --branch Green-D --direction 1 \
        --day weekday --start-time 07:00 --duration 180 --runs 500
"""

from __future__ import annotations

import json
import os
import random
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

import simpy

from sim.network import Network
from sim.passenger import (
    arrival_process,
    load_passenger_arrivals,
    seed_initial_passengers,
)
from sim.station import SimulatedStation
from sim.train import Train, load_breakdown_rates, train_dispatcher

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# Station tier lookup — loaded once from passenger_arrivals.json
_ARRIVALS_DATA: dict | None = None


def _get_arrivals_data() -> dict:
    global _ARRIVALS_DATA
    if _ARRIVALS_DATA is None:
        _ARRIVALS_DATA = load_passenger_arrivals()
    return _ARRIVALS_DATA


def _time_str_to_seconds(t: str) -> float:
    """Convert "HH:MM" or "HH:MM:SS" string to seconds since midnight."""
    parts = t.split(":")
    h, m = int(parts[0]), int(parts[1])
    s = int(parts[2]) if len(parts) > 2 else 0
    return h * 3600 + m * 60 + s


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class SimConfig:
    """
    Configuration for a single simulation run or batch.

    Parameters
    ----------
    branches    : list of branch IDs to simulate (default: ["Green-D"])
    directions  : list of directions (0=outbound, 1=inbound; default: [1])
    day_type    : "weekday", "saturday", or "sunday"
    start_time  : simulation start as "HH:MM" string or seconds since midnight
    duration_min: simulation window in minutes
    seed        : random seed (None = non-deterministic)
    """
    branches: list[str] = field(default_factory=lambda: ["Green-D"])
    directions: list[int] = field(default_factory=lambda: [1])
    day_type: str = "weekday"
    start_time: str | float = "07:00"
    duration_min: float = 120.0
    seed: int | None = None

    @property
    def start_seconds(self) -> float:
        if isinstance(self.start_time, (int, float)):
            return float(self.start_time)
        return _time_str_to_seconds(self.start_time)

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_min * 60


# ---------------------------------------------------------------------------
# Run result containers
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Output of a single simulation run."""
    config: SimConfig
    trains: list[Train]
    station_metrics: list[dict]
    event_log: list[dict]           # all events across all trains, sorted by time
    wall_time_sec: float            # real-world seconds the sim took to run

    # Computed aggregate metrics
    trip_durations: list[float] = field(default_factory=list)
    delay_seconds: list[float] = field(default_factory=list)
    headway_gaps: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        td = self.trip_durations
        return {
            "n_trains": len(self.trains),
            "trip_duration_mean_sec": statistics.mean(td) if td else None,
            "trip_duration_p50_sec": statistics.median(td) if td else None,
            "trip_duration_p90_sec": _percentile(td, 90) if td else None,
            "trip_duration_min_sec": min(td) if td else None,
            "trip_duration_max_sec": max(td) if td else None,
            "total_breakdowns": sum(t.breakdown_count for t in self.trains),
            "total_boarded": sum(
                m["total_boarded"] for m in self.station_metrics
            ),
            "total_overflow": sum(
                m["total_overflow"] for m in self.station_metrics
            ),
        }


@dataclass
class BatchResult:
    """Aggregated output across N simulation runs."""
    config: SimConfig
    n_runs: int
    run_summaries: list[dict]
    wall_time_sec: float

    def aggregate(self) -> dict:
        def _collect(key):
            return [r[key] for r in self.run_summaries if r.get(key) is not None]

        td_means = _collect("trip_duration_mean_sec")
        td_p90s = _collect("trip_duration_p90_sec")
        td_maxes = _collect("trip_duration_max_sec")
        td_mins = _collect("trip_duration_min_sec")
        breakdowns = _collect("total_breakdowns")
        overflows = _collect("total_overflow")

        return {
            "n_runs": self.n_runs,
            "branches": self.config.branches,
            "day_type": self.config.day_type,
            "start_time": self.config.start_time,
            "duration_min": self.config.duration_min,
            # Trip duration distributions (across all runs)
            "trip_duration": {
                "mean_of_means_sec": _safe_mean(td_means),
                "p50_of_means_sec": _safe_median(td_means),
                "p90_of_p90s_sec": _safe_median(td_p90s),
                "worst_trip_sec": max(td_maxes) if td_maxes else None,
                "best_trip_sec": min(td_mins) if td_mins else None,
                "std_of_means_sec": _safe_std(td_means),
            },
            # Reliability
            "breakdowns": {
                "mean_per_run": _safe_mean(breakdowns),
                "max_per_run": max(breakdowns) if breakdowns else None,
                "runs_with_breakdown": sum(1 for b in breakdowns if b > 0),
            },
            # Capacity
            "overflow": {
                "mean_per_run": _safe_mean(overflows),
                "max_per_run": max(overflows) if overflows else None,
                "runs_with_overflow": sum(1 for o in overflows if o > 0),
            },
            "wall_time_sec": self.wall_time_sec,
        }


# ---------------------------------------------------------------------------
# Core simulation setup
# ---------------------------------------------------------------------------

def _build_simulated_stations(network: Network, arrivals_data: dict) -> dict[str, SimulatedStation]:
    """Create SimulatedStation instances for all stations in the network."""
    tiers = arrivals_data["station_tiers"]
    default_tier = arrivals_data.get("default_tier", "branch_outer")

    stations = {}
    for station_id, record in network.stations.items():
        tier = tiers.get(station_id, default_tier)
        stations[station_id] = SimulatedStation(record, tier)
    return stations


def _compute_headway_gaps(trains: list[Train], route: list[str]) -> list[float]:
    """
    Compute inter-train arrival gaps at the midpoint of the route.
    Used for bunching analysis.
    """
    if len(route) < 2:
        return []
    midpoint = route[len(route) // 2]
    arrivals = []
    for train in trains:
        for event in train.event_log:
            if event["station_id"] == midpoint and event["event"] == "arrived":
                arrivals.append(event["time"])
    arrivals.sort()
    return [arrivals[i] - arrivals[i - 1] for i in range(1, len(arrivals))]


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------

def single_run(config: SimConfig) -> RunResult:
    """
    Execute one simulation run and return a RunResult.

    The SimPy environment runs from config.start_seconds to config.end_seconds.
    Passenger processes and train dispatchers are started at sim_start.
    """
    if config.seed is not None:
        random.seed(config.seed)

    wall_start = time.perf_counter()

    network = Network(branches=config.branches)
    arrivals_data = _get_arrivals_data()
    breakdown_rates = load_breakdown_rates()

    env = simpy.Environment(initial_time=config.start_seconds)
    network.init_merge_points(env)

    stations = _build_simulated_stations(network, arrivals_data)

    # Seed platforms with pre-existing passengers (warm-start)
    for station in stations.values():
        seed_initial_passengers(station, arrivals_data, config.day_type, config.start_seconds)

    # Start passenger arrival processes at every station
    for station in stations.values():
        env.process(
            arrival_process(env, station, arrivals_data, config.day_type, config.end_seconds)
        )

    # Track all spawned trains via a shared list
    all_trains: list[Train] = []

    def _dispatching_process(branch: str, direction: int):
        """Wrapper that collects Train objects spawned by train_dispatcher."""
        from sim.passenger import current_time_block

        train_counter = 0
        while env.now < config.end_seconds:
            time_block = current_time_block(env.now)
            headway = network.sample_headway(branch, direction, config.day_type, time_block)
            yield env.timeout(headway)

            if env.now >= config.end_seconds:
                break

            train_counter += 1
            train_id = f"{branch}-d{direction}-{train_counter:04d}"
            train = Train(
                train_id=train_id,
                branch=branch,
                direction=direction,
                start_time=env.now,
                network=network,
                stations=stations,
                breakdown_rates=breakdown_rates,
                day_type=config.day_type,
            )
            all_trains.append(train)
            env.process(train.run(env))

    for branch in config.branches:
        for direction in config.directions:
            env.process(_dispatching_process(branch, direction))

    env.run(until=config.end_seconds)

    # Collect and sort all events
    event_log = []
    for train in all_trains:
        event_log.extend(train.event_log)
    event_log.sort(key=lambda e: (e["time"], e["train_id"]))

    # Compute trip durations
    trip_durations = [
        t.trip_duration()
        for t in all_trains
        if t.trip_duration() is not None
    ]

    # Compute headway gaps for bunching analysis
    if config.branches and config.directions:
        route = network.get_route(config.branches[0], config.directions[0])
        headway_gaps = _compute_headway_gaps(all_trains, route)
    else:
        headway_gaps = []

    station_metrics = [s.to_metrics_dict() for s in stations.values()]

    wall_time = time.perf_counter() - wall_start

    result = RunResult(
        config=config,
        trains=all_trains,
        station_metrics=station_metrics,
        event_log=event_log,
        wall_time_sec=wall_time,
        trip_durations=trip_durations,
        headway_gaps=headway_gaps,
    )
    return result


# ---------------------------------------------------------------------------
# Batch run
# ---------------------------------------------------------------------------

def batch_run(config: SimConfig, n_runs: int, verbose: bool = True) -> BatchResult:
    """
    Run N independent simulations and aggregate results.

    Each run uses a different random seed derived from the base config seed.
    """
    wall_start = time.perf_counter()
    summaries = []

    for i in range(n_runs):
        run_config = SimConfig(
            branches=config.branches,
            directions=config.directions,
            day_type=config.day_type,
            start_time=config.start_time,
            duration_min=config.duration_min,
            seed=config.seed + i if config.seed is not None else None,
        )
        result = single_run(run_config)
        summaries.append(result.summary())

        if verbose and (i + 1) % max(1, n_runs // 10) == 0:
            print(f"  Completed {i + 1}/{n_runs} runs...")

    wall_time = time.perf_counter() - wall_start

    return BatchResult(
        config=config,
        n_runs=n_runs,
        run_summaries=summaries,
        wall_time_sec=wall_time,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(data: list[float], p: int) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (k - lo)


def _safe_mean(data):
    return statistics.mean(data) if data else None


def _safe_median(data):
    return statistics.median(data) if data else None


def _safe_std(data):
    return statistics.stdev(data) if len(data) > 1 else None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run MBTA Green Line simulation")
    parser.add_argument("--mode", choices=["single", "batch"], default="single")
    parser.add_argument("--branch", default="Green-D")
    parser.add_argument("--direction", type=int, default=1, choices=[0, 1])
    parser.add_argument("--day", default="weekday", choices=["weekday", "saturday", "sunday"])
    parser.add_argument("--start-time", default="07:00")
    parser.add_argument("--duration", type=float, default=120.0, help="Minutes")
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    cfg = SimConfig(
        branches=[args.branch],
        directions=[args.direction],
        day_type=args.day,
        start_time=args.start_time,
        duration_min=args.duration,
        seed=args.seed,
    )

    if args.mode == "single":
        print(f"Running single simulation: {args.branch} dir={args.direction} "
              f"{args.day} {args.start_time} for {args.duration:.0f} min...")
        result = single_run(cfg)
        s = result.summary()
        print(f"\nResult:")
        print(f"  Trains completed: {s['n_trains']}")
        if s["trip_duration_mean_sec"]:
            print(f"  Trip duration: mean={s['trip_duration_mean_sec']/60:.1f}min "
                  f"p50={s['trip_duration_p50_sec']/60:.1f}min "
                  f"p90={s['trip_duration_p90_sec']/60:.1f}min "
                  f"min={s['trip_duration_min_sec']/60:.1f}min "
                  f"max={s['trip_duration_max_sec']/60:.1f}min")
        print(f"  Breakdowns: {s['total_breakdowns']}")
        print(f"  Boarded: {s['total_boarded']} | Overflow: {s['total_overflow']}")
        print(f"  Wall time: {result.wall_time_sec:.2f}s")

    else:
        print(f"Running {args.runs} simulations: {args.branch} dir={args.direction} "
              f"{args.day} {args.start_time} for {args.duration:.0f} min...")
        batch = batch_run(cfg, args.runs)
        agg = batch.aggregate()
        td = agg["trip_duration"]
        print(f"\nBatch results ({agg['n_runs']} runs):")
        if td["mean_of_means_sec"]:
            print(f"  Mean trip duration:  {td['mean_of_means_sec']/60:.1f} min")
            print(f"  p50 of means:        {td['p50_of_means_sec']/60:.1f} min")
            print(f"  p90 of p90s:         {td['p90_of_p90s_sec']/60:.1f} min")
            print(f"  Worst trip ever:     {td['worst_trip_sec']/60:.1f} min")
            print(f"  Best trip ever:      {td['best_trip_sec']/60:.1f} min")
            print(f"  Std of means:        {td['std_of_means_sec']/60:.1f} min")
        bk = agg["breakdowns"]
        print(f"  Breakdowns/run:      {bk['mean_per_run']:.2f} avg, "
              f"{bk['runs_with_breakdown']}/{agg['n_runs']} runs affected")
        ov = agg["overflow"]
        print(f"  Overflow/run:        {ov['mean_per_run']:.1f} avg passengers stranded")
        print(f"  Wall time:           {agg['wall_time_sec']:.1f}s total")
