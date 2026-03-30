"""
sim/train.py
------------
SimPy process for a single Green Line train.

A train follows a fixed route (list of station IDs) and, at each stop:
  1. Arrives  → passengers alight, passengers board (dwell time coupled to counts)
  2. Departs  → travels to next stop (travel time sampled from fitted distribution)
  3. Possibly breaks down between stops (Poisson breakdown process)

At merge points (Kenmore, Copley), the train requests a SimPy Resource to
model the physical constraint of one train at a time through the junction.
This is what creates the trunk congestion in multi-branch simulations.

Event log
---------
Every significant event is appended to self.event_log as a dict:
  {
    "time":        float,   # seconds since midnight
    "event":       str,     # "departed_terminus" | "arrived" | "departed"
                            # | "breakdown_start" | "breakdown_end"
    "train_id":    str,
    "branch":      str,
    "station_id":  str,
    "station_name": str,
    "passengers":  int,     # on-board count after stop processing
    "platform_waiting": int,
    "dwell_sec":   float,   # 0 if not a stop event
    "scheduled_time": float | None,  # for delay computation
  }

This log is the primary output of a single simulation run and drives
the interactive map animation in the dashboard.
"""

from __future__ import annotations

import json
import os
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import simpy
    from sim.network import Network
    from sim.station import SimulatedStation

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

TRAIN_CAPACITY = 176   # Type 8 LRV single-car capacity (seated + standing)
MIN_TURNAROUND = 120   # seconds held at terminus before departing outbound


def load_breakdown_rates() -> dict:
    path = os.path.join(DATA_DIR, "distributions", "breakdown_rates.json")
    with open(path) as f:
        return json.load(f)


class Train:
    """
    A single Green Line train.

    Parameters
    ----------
    train_id      : unique identifier string (e.g. "Green-D-001")
    branch        : route ID (e.g. "Green-D")
    direction     : 0=outbound, 1=inbound
    start_time    : simulation time (seconds) when this train departs terminus
    network       : Network object (topology + distribution sampling)
    stations      : dict[station_id -> SimulatedStation] (runtime state)
    breakdown_rates : loaded breakdown_rates.json
    day_type      : "weekday", "saturday", or "sunday"
    """

    def __init__(
        self,
        train_id: str,
        branch: str,
        direction: int,
        start_time: float,
        network: "Network",
        stations: dict[str, "SimulatedStation"],
        breakdown_rates: dict,
        day_type: str = "weekday",
    ):
        self.train_id = train_id
        self.branch = branch
        self.direction = direction
        self.start_time = start_time
        self.network = network
        self.stations = stations
        self.day_type = day_type

        self.passengers = 0
        self.capacity = TRAIN_CAPACITY
        self.event_log: list[dict] = []

        # Breakdown parameters for this branch
        br = breakdown_rates.get(branch, breakdown_rates.get("Green-D", {}))
        self._breakdown_prob = br.get("prob_per_trip", 0.02)
        self._breakdown_mu = br.get("delay_mu", 5.0)
        self._breakdown_sigma = br.get("delay_sigma", 0.7)

        # Metrics
        self.trip_start_time: float | None = None
        self.trip_end_time: float | None = None
        self.total_breakdown_delay: float = 0.0
        self.breakdown_count: int = 0

    # ------------------------------------------------------------------
    # SimPy process entry point
    # ------------------------------------------------------------------

    def run(self, env: "simpy.Environment"):
        """SimPy generator process. Yield from env.process(train.run(env))."""
        from sim.passenger import current_time_block

        route = self.network.get_route(self.branch, self.direction)
        if not route:
            return

        self.trip_start_time = env.now
        self._log_event(env.now, "departed_terminus", route[0], 0.0)

        for stop_index, station_id in enumerate(route):
            station = self.stations.get(station_id)
            if station is None:
                continue

            is_terminus = (stop_index == len(route) - 1)
            time_block = current_time_block(env.now)

            # --- Process stop (alight + board + dwell) ---
            result = station.process_train_stop(
                network=self.network,
                passengers_on_board=self.passengers,
                train_capacity=self.capacity,
                time_block=time_block,
                sim_time=env.now,
                is_terminus=is_terminus,
            )

            self.passengers = (
                self.passengers - result.alighted + result.boarded
            )
            self.passengers = max(0, min(self.passengers, self.capacity))

            self._log_event(
                env.now, "arrived", station_id, result.dwell_time,
                platform_waiting=result.passengers_remaining_on_platform,
            )

            # Dwell at station
            yield env.timeout(result.dwell_time)

            self._log_event(
                env.now, "departed", station_id, 0.0,
                platform_waiting=result.passengers_remaining_on_platform,
            )

            if is_terminus:
                break

            # --- Travel to next stop ---
            next_station_id = route[stop_index + 1]
            from_stop = self.network.get_stop_id(station_id, self.branch, self.direction)
            to_stop = self.network.get_stop_id(next_station_id, self.branch, self.direction)
            travel_time = self.network.sample_travel_time(from_stop, to_stop)

            # --- Merge point: request resource if entering trunk junction ---
            merge_res = self.network.merge_resources.get(next_station_id)
            if merge_res is not None:
                with merge_res.request() as req:
                    yield req
                    yield env.timeout(travel_time)
            else:
                yield env.timeout(travel_time)

            # --- Possible breakdown between stops ---
            if random.random() < self._breakdown_prob:
                delay = random.lognormvariate(self._breakdown_mu, self._breakdown_sigma)
                self.breakdown_count += 1
                self.total_breakdown_delay += delay
                self._log_event(env.now, "breakdown_start", next_station_id, delay)
                yield env.timeout(delay)
                self._log_event(env.now, "breakdown_end", next_station_id, 0.0)

        self.trip_end_time = env.now

    # ------------------------------------------------------------------
    # Event logging
    # ------------------------------------------------------------------

    def _log_event(
        self,
        sim_time: float,
        event: str,
        station_id: str,
        dwell_sec: float,
        platform_waiting: int = 0,
    ):
        station = self.stations.get(station_id)
        self.event_log.append({
            "time": round(sim_time, 1),
            "event": event,
            "train_id": self.train_id,
            "branch": self.branch,
            "direction": self.direction,
            "station_id": station_id,
            "station_name": station.name if station else station_id,
            "passengers": self.passengers,
            "platform_waiting": platform_waiting,
            "dwell_sec": round(dwell_sec, 1),
        })

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def trip_duration(self) -> float | None:
        if self.trip_start_time is not None and self.trip_end_time is not None:
            return self.trip_end_time - self.trip_start_time
        return None

    def to_metrics_dict(self) -> dict:
        return {
            "train_id": self.train_id,
            "branch": self.branch,
            "direction": self.direction,
            "start_time": self.trip_start_time,
            "end_time": self.trip_end_time,
            "trip_duration_sec": self.trip_duration(),
            "breakdown_count": self.breakdown_count,
            "breakdown_delay_sec": round(self.total_breakdown_delay, 1),
        }

    def __repr__(self):
        return (
            f"Train({self.train_id}, branch={self.branch}, "
            f"dir={self.direction}, pax={self.passengers}/{self.capacity})"
        )


def train_dispatcher(
    env: "simpy.Environment",
    branch: str,
    direction: int,
    network: "Network",
    stations: dict[str, "SimulatedStation"],
    breakdown_rates: dict,
    day_type: str,
    sim_end: float,
) -> "simpy.events.Process":
    """
    SimPy generator that spawns trains at headway intervals for one branch+direction.

    Yields to env at each headway interval, spawning a new Train process.
    Runs until sim_end is reached.
    """
    from sim.passenger import current_time_block

    train_counter = 0

    while env.now < sim_end:
        time_block = current_time_block(env.now)
        headway = network.sample_headway(branch, direction, day_type, time_block)

        yield env.timeout(headway)

        if env.now >= sim_end:
            break

        train_counter += 1
        train_id = f"{branch}-{direction}-{train_counter:04d}"

        train = Train(
            train_id=train_id,
            branch=branch,
            direction=direction,
            start_time=env.now,
            network=network,
            stations=stations,
            breakdown_rates=breakdown_rates,
            day_type=day_type,
        )
        env.process(train.run(env))

        # Yield the Train object via a shared list (runner collects it)
        # Runner passes in a list reference; we append here for metrics collection
        if hasattr(train_dispatcher, "_train_registry"):
            train_dispatcher._train_registry.append(train)
