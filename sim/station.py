"""
sim/station.py
--------------
SimulatedStation holds per-station runtime state during a simulation run:
  - Platform passenger queue (count of waiting passengers)
  - Metrics: arrivals, boardings, missed (capacity overflow), wait times

This is the runtime counterpart to network.StationRecord (topology).
Station instances are created fresh for each simulation run.

Boarding model
--------------
When a train arrives, the station transfers passengers to the train:

    board_count = min(capacity_remaining, waiting_passengers)

Alighting model
---------------
A fixed fraction of on-board passengers alight at each stop. The fraction
is higher at major hubs and near the terminus, lower on outer branch stops.
Default: ALIGHT_FRACTION = 0.18 (roughly 1-in-5 passengers exit each stop).
At the final stop in a route, all remaining passengers alight.

Dwell time coupling
-------------------
Dwell time is NOT a fixed sample — it depends on boarding and alighting count:

    dwell = base_dwell_sample + BOARD_TIME * board_count + ALIGHT_TIME * alight_count

This creates the bunching cascade: late train → more passengers accumulated
→ longer dwell → train falls further behind → next stop has even more passengers.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sim.network import Network, StationRecord

# Fraction of on-board passengers that alight at each non-terminus stop.
# Varies by station tier (major hubs see more exits).
ALIGHT_FRACTIONS = {
    "major_hub": 0.28,
    "trunk": 0.20,
    "branch_main": 0.15,
    "branch_outer": 0.10,
    "terminus": 1.00,  # everyone exits at end of line
}
DEFAULT_ALIGHT_FRACTION = 0.18


@dataclass
class BoardingResult:
    """Returned by SimulatedStation.process_train_stop()."""
    boarded: int
    alighted: int
    dwell_time: float        # seconds (base + per-passenger)
    passengers_remaining_on_platform: int
    overflow: int            # passengers who couldn't board (train full)


class SimulatedStation:
    """
    Runtime state for a single station during one simulation run.

    Parameters
    ----------
    record : StationRecord
        Read-only topology info (name, lat/lon, is_surface, etc.)
    tier : str
        Station tier for alighting fraction lookup
        ("major_hub", "trunk", "branch_main", "branch_outer", "terminus")
    """

    def __init__(self, record: "StationRecord", tier: str = "branch_outer"):
        self.id = record.id
        self.name = record.name
        self.is_surface = record.is_surface
        self.tier = tier

        # Runtime state
        self.waiting_passengers: int = 0

        # Metrics accumulated over the run
        self.total_arrived: int = 0           # passengers who arrived at this platform
        self.total_boarded: int = 0           # passengers who successfully boarded a train
        self.total_alighted: int = 0          # passengers who alighted here
        self.total_missed_boardings: int = 0  # cumulative per-train-stop misses
                                              # (a passenger stranded across N trains counts N times;
                                              # useful for dwell pressure analysis but not headcount)
        self.wait_times: list[float] = []     # per-passenger wait time (seconds) for boarded pax
        self._arrival_times: list[float] = [] # timestamps for wait time computation

    # ------------------------------------------------------------------
    # Passenger arrival (called by passenger.py arrival process)
    # ------------------------------------------------------------------

    def add_passenger(self, sim_time: float):
        """Record one passenger arriving on the platform."""
        self.waiting_passengers += 1
        self.total_arrived += 1
        self._arrival_times.append(sim_time)

    def add_passengers_bulk(self, count: int, sim_time: float):
        """Add multiple passengers at once (used for initial platform seeding)."""
        self.waiting_passengers += count
        self.total_arrived += count
        self._arrival_times.extend([sim_time] * count)

    # ------------------------------------------------------------------
    # Train stop processing
    # ------------------------------------------------------------------

    def process_train_stop(
        self,
        network: "Network",
        passengers_on_board: int,
        train_capacity: int,
        time_block: str,
        sim_time: float,
        is_terminus: bool = False,
    ) -> BoardingResult:
        """
        Process one train stop: alighting, boarding, dwell time calculation.

        Parameters
        ----------
        network      : Network (for dwell time sampling)
        passengers_on_board : current passenger load on the train
        train_capacity : maximum passengers the train can hold
        time_block   : current time block label (for dwell sampling)
        sim_time     : current simulation time (seconds since midnight)
        is_terminus  : True if this is the last stop on this train's route

        Returns
        -------
        BoardingResult with boarding count, alighting count, dwell time, overflow
        """
        # --- Alighting ---
        if is_terminus:
            alighted = passengers_on_board
        else:
            frac = ALIGHT_FRACTIONS.get(self.tier, DEFAULT_ALIGHT_FRACTION)
            # Add small randomness: ±20% of fraction
            frac = max(0.0, frac * random.uniform(0.8, 1.2))
            alighted = int(round(passengers_on_board * frac))
        alighted = min(alighted, passengers_on_board)
        self.total_alighted += alighted

        # --- Boarding ---
        capacity_remaining = train_capacity - (passengers_on_board - alighted)
        capacity_remaining = max(0, capacity_remaining)
        boarded = min(self.waiting_passengers, capacity_remaining)
        overflow = self.waiting_passengers - boarded

        # Record wait times for passengers who board now
        if boarded > 0 and self._arrival_times:
            # Approximate: assign earliest arrival times to boarding passengers
            boarding_arrivals = self._arrival_times[:boarded]
            for arr_t in boarding_arrivals:
                self.wait_times.append(sim_time - arr_t)
            self._arrival_times = self._arrival_times[boarded:]

        self.waiting_passengers -= boarded
        self.total_boarded += boarded
        self.total_missed_boardings += overflow  # cumulative; see note above

        # --- Dwell time ---
        base_dwell = network.sample_dwell(self.is_surface, time_block)
        from sim.network import BOARD_TIME_PER_PAX, ALIGHT_TIME_PER_PAX
        dwell = (
            base_dwell
            + BOARD_TIME_PER_PAX * boarded
            + ALIGHT_TIME_PER_PAX * alighted
        )
        # Clamp: minimum 5s (doors must open), maximum 4 min (exceptional event)
        dwell = max(5.0, min(dwell, 240.0))

        return BoardingResult(
            boarded=boarded,
            alighted=alighted,
            dwell_time=dwell,
            passengers_remaining_on_platform=self.waiting_passengers,
            overflow=overflow,
        )

    # ------------------------------------------------------------------
    # Metrics helpers
    # ------------------------------------------------------------------

    def mean_wait_time(self) -> float | None:
        if not self.wait_times:
            return None
        return sum(self.wait_times) / len(self.wait_times)

    def total_stranded(self) -> int:
        """Unique passengers still on platform at end of run (never boarded)."""
        return self.waiting_passengers

    def board_rate(self) -> float:
        """Fraction of arrived passengers who successfully boarded."""
        if self.total_arrived == 0:
            return 1.0
        return self.total_boarded / self.total_arrived

    def to_metrics_dict(self) -> dict:
        stranded = self.total_stranded()
        return {
            "station_id": self.id,
            "station_name": self.name,
            "total_arrived": self.total_arrived,
            "total_boarded": self.total_boarded,
            "total_alighted": self.total_alighted,
            "total_stranded": stranded,              # unique passengers, never boarded
            "total_missed_boardings": self.total_missed_boardings,  # cumulative per-stop misses
            "board_rate": self.board_rate(),
            "mean_wait_sec": self.mean_wait_time(),
        }

    def __repr__(self):
        return (
            f"SimulatedStation({self.name!r}, waiting={self.waiting_passengers}, "
            f"boarded={self.total_boarded})"
        )
