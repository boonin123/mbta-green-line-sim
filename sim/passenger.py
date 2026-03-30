"""
sim/passenger.py
----------------
SimPy process for passenger arrivals at each station.

Passengers arrive according to a Poisson process with rate λ (arrivals/min),
which varies by:
  - station tier  (major_hub > trunk > branch_main > branch_outer)
  - time block    (am_peak / pm_peak have 2.5-2.8× midday rate)
  - day type      (weekday / saturday / sunday)

The inter-arrival time between successive passengers is Exponential(1/λ),
which is the continuous-time equivalent of a Poisson count process.

Usage (called from runner.py)
------
    import simpy
    from sim.passenger import arrival_process, get_arrival_rate

    env = simpy.Environment(initial_time=start_time_seconds)
    rate = get_arrival_rate(station_id, passenger_arrivals_data, day_type, time_block)
    env.process(arrival_process(env, station, rate, sim_end_time))
"""

from __future__ import annotations

import json
import os
import random

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# Seconds per hour, used for time block transitions
SECONDS_PER_HOUR = 3600

# Time block boundaries in seconds since midnight
# Matches fit_distributions.py TIME_BLOCKS
TIME_BLOCK_BOUNDARIES = [
    ("early_morning", 4 * 3600, 6 * 3600),
    ("am_peak",       6 * 3600, 9 * 3600),
    ("midday",        9 * 3600, 15 * 3600),
    ("pm_peak",       15 * 3600, 19 * 3600),
    ("evening",       19 * 3600, 22 * 3600),
    ("late_night",    22 * 3600, 25 * 3600),
    ("overnight",     0,         4 * 3600),
]


def current_time_block(sim_time_seconds: float) -> str:
    """Return the time block label for a simulation time (seconds since midnight)."""
    for label, start, end in TIME_BLOCK_BOUNDARIES:
        if start <= sim_time_seconds < end:
            return label
    return "overnight"


def load_passenger_arrivals() -> dict:
    path = os.path.join(DATA_DIR, "distributions", "passenger_arrivals.json")
    with open(path) as f:
        return json.load(f)


def get_arrival_rate(
    station_id: str,
    arrivals_data: dict,
    day_type: str,
    time_block: str,
    pax_scale: float = 1.0,
) -> float:
    """
    Return λ (passengers per minute) for a station at a given time.

    Parameters
    ----------
    station_id    : parent station ID (e.g. "place-kencl")
    arrivals_data : loaded passenger_arrivals.json dict
    day_type      : "weekday", "saturday", or "sunday"
    time_block    : one of the TIME_BLOCK labels
    pax_scale     : multiplier applied to the raw rate. Use 1/n_branches when
                    simulating a single branch — base rates are system-wide
                    (all branches share each platform), so a D-branch-only run
                    should use pax_scale=0.25 to avoid over-accumulation.

    Returns
    -------
    float : arrival rate in passengers per minute (>= 0)
    """
    tiers = arrivals_data["station_tiers"]
    base_rates = arrivals_data["base_rates_per_min"]
    multipliers = arrivals_data["time_multipliers"]
    default_tier = arrivals_data.get("default_tier", "branch_outer")

    tier = tiers.get(station_id, default_tier)
    base = base_rates[tier]
    mult = multipliers.get(day_type, multipliers["weekday"]).get(time_block, 1.0)
    return base * mult * pax_scale


def arrival_process(
    env, station, arrivals_data: dict, day_type: str, sim_end: float,
    pax_scale: float = 1.0,
):
    """
    SimPy generator process: continuously generates passenger arrivals
    at `station` until sim_end.

    The arrival rate is re-evaluated at each arrival to track time block
    transitions (e.g. switching from midday to pm_peak mid-simulation).

    Parameters
    ----------
    env           : simpy.Environment
    station       : SimulatedStation instance
    arrivals_data : loaded passenger_arrivals.json
    day_type      : "weekday", "saturday", or "sunday"
    sim_end       : simulation end time in seconds since midnight
    pax_scale     : branch fraction scale factor (see get_arrival_rate)
    """
    while env.now < sim_end:
        # Re-evaluate rate at current simulation time (captures peak transitions)
        block = current_time_block(env.now)
        rate_per_min = get_arrival_rate(
            station.id, arrivals_data, day_type, block, pax_scale
        )

        if rate_per_min <= 0:
            # No arrivals during this period — advance by 1 minute and recheck
            yield env.timeout(60.0)
            continue

        # Inter-arrival time ~ Exponential(rate_per_min / 60) seconds
        rate_per_sec = rate_per_min / 60.0
        interarrival = random.expovariate(rate_per_sec)

        yield env.timeout(interarrival)

        if env.now < sim_end:
            station.add_passenger(env.now)


def seed_initial_passengers(
    station, arrivals_data: dict, day_type: str, sim_start: float,
    pax_scale: float = 1.0,
):
    """
    Seed platforms with passengers who would have accumulated before the
    simulation window begins (avoids cold-start bias on first few trains).

    Parameters
    ----------
    station       : SimulatedStation
    arrivals_data : loaded passenger_arrivals.json
    day_type      : "weekday", "saturday", or "sunday"
    sim_start     : simulation start time (seconds since midnight)
    pax_scale     : branch fraction scale factor (see get_arrival_rate)
    """
    block = current_time_block(sim_start)
    rate_per_min = get_arrival_rate(
        station.id, arrivals_data, day_type, block, pax_scale
    )

    # Assume half a headway of accumulation before sim starts.
    assumed_headway_min = 7.0
    expected_waiting = min(rate_per_min * (assumed_headway_min / 2.0), 30)

    # Add Poisson-distributed initial count
    initial = max(0, int(random.gauss(expected_waiting, max(1.0, expected_waiting ** 0.5))))
    if initial > 0:
        station.add_passengers_bulk(initial, sim_start)
