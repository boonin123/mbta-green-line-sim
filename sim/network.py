"""
sim/network.py
--------------
Defines the Green Line network graph: stations, ordered routes per branch,
segment travel-time distributions, and merge point resources.

The Network is the read-only topology layer consumed by Train and Station.
SimPy Resources for merge points are attached at simulation init time
(requires a live SimPy Environment), so they are created via Network.init_merge_points(env).

Branch directions follow MBTA convention:
  direction 0 = outbound (away from downtown / Government Center)
  direction 1 = inbound  (toward downtown / Government Center)
"""

import json
import os
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import simpy

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# Effective seconds per passenger boarding / alighting — used in dwell time coupling.
# Green Line Type 8 LRVs have 4 sets of double doors = 8 boarding streams.
# TCRP Report 165 quotes 2.5s per single door channel; effective rate across all
# doors = 2.5 / 8 ≈ 0.31s. We use 0.5s to add a margin for crowding.
BOARD_TIME_PER_PAX = 0.5   # seconds (effective, across all door channels)
ALIGHT_TIME_PER_PAX = 0.3  # seconds (effective, across all door channels)

# IDs of merge-point parent stations (require SimPy Resource in multi-branch runs)
MERGE_STATION_IDS = {
    "place-kencl",  # Kenmore  — B, C, D branches converge
    "place-coecl",  # Copley   — E branch joins trunk
}


class SegmentDist:
    """Holds distribution parameters for a single between-stop segment."""

    def __init__(self, data: dict):
        self.dist = data["dist"]           # "lognormal" or "normal"
        self.is_surface = data["is_surface"]
        self.from_name = data.get("from_name", "")
        self.to_name = data.get("to_name", "")

        if self.dist == "lognormal":
            self.mu = data["mu"]
            self.sigma = data["sigma"]
        else:  # normal
            self.mean = data["mean"]
            self.std = data["std"]

    def sample(self) -> float:
        """Draw a travel time in seconds."""
        if self.dist == "lognormal":
            return random.lognormvariate(self.mu, self.sigma)
        else:
            return max(5.0, random.gauss(self.mean, self.std))


class HeadwayDist:
    """Headway distribution for a (branch, direction, day_type, time_block) key."""

    def __init__(self, data: dict):
        self.dist = data["dist"]
        self.mu = data.get("mu", 0)
        self.sigma = data.get("sigma", 0)
        self.mean = data.get("mean", 0)

    def sample(self) -> float:
        if self.dist == "lognormal":
            return random.lognormvariate(self.mu, self.sigma)
        return max(30.0, random.gauss(self.mean, self.mean * 0.2))


class StationRecord:
    """Lightweight topology record for a station node in the network graph."""

    def __init__(self, data: dict):
        self.id = data["id"]
        self.name = data["name"]
        self.lat = data["lat"]
        self.lon = data["lon"]
        self.branches = data["branches"]
        self.is_surface = data["is_surface"]
        self.stop_ids = data["stop_ids"]            # {branch: {inbound: sid, outbound: sid}}
        self.stop_sequence = data["stop_sequence"]  # {branch: int}

    def stop_id_for(self, branch: str, direction: int) -> str:
        dir_label = "outbound" if direction == 0 else "inbound"
        return self.stop_ids.get(branch, {}).get(dir_label, "")


class Network:
    """
    Central topology object for the Green Line simulation.

    Attributes
    ----------
    stations : dict[station_id -> StationRecord]
    routes   : dict[branch -> dict[direction -> list[station_id]]]
        Ordered list of parent station IDs for each branch+direction.
    segments : dict["from_sid__to_sid" -> SegmentDist]
        Keyed by GTFS child stop_id pairs (as in travel_times.json).
    headways : dict[series_key -> dict[time_block -> HeadwayDist]]
        series_key = "{branch}__{direction_label}__{day_type}"
    dwell_params : dict  (from dwell_times.json)
    """

    def __init__(self, branches: list[str] | None = None):
        """
        Parameters
        ----------
        branches : list of branch IDs to load, e.g. ["Green-D"].
                   None = load all four branches.
        """
        self.active_branches = branches or ["Green-B", "Green-C", "Green-D", "Green-E"]
        self.merge_resources = {}  # station_id -> simpy.Resource (set by init_merge_points)

        self._load_stations()
        self._load_travel_times()
        self._load_headways()
        self._load_dwell_params()
        self._build_routes()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_stations(self):
        path = os.path.join(DATA_DIR, "stations.json")
        with open(path) as f:
            raw = json.load(f)
        self.stations: dict[str, StationRecord] = {}
        for s in raw:
            if any(b in self.active_branches for b in s["branches"]):
                self.stations[s["id"]] = StationRecord(s)

    def _load_travel_times(self):
        path = os.path.join(DATA_DIR, "distributions", "travel_times.json")
        with open(path) as f:
            raw = json.load(f)
        self.segments: dict[str, SegmentDist] = {
            k: SegmentDist(v) for k, v in raw.items()
        }

    def _load_headways(self):
        path = os.path.join(DATA_DIR, "distributions", "headways.json")
        with open(path) as f:
            raw = json.load(f)
        self.headways: dict[str, dict[str, HeadwayDist]] = {}
        for series_key, blocks in raw.items():
            self.headways[series_key] = {
                block: HeadwayDist(params) for block, params in blocks.items()
            }

    def _load_dwell_params(self):
        path = os.path.join(DATA_DIR, "distributions", "dwell_times.json")
        with open(path) as f:
            self.dwell_params = json.load(f)

    # ------------------------------------------------------------------
    # Route building
    # ------------------------------------------------------------------

    def _build_routes(self):
        """
        Build ordered lists of station IDs per (branch, direction) using
        stop_sequence values from stations.json.

        routes[branch][direction] = [station_id, station_id, ...]
        direction 0 = outbound (descending sequence), 1 = inbound (ascending)
        """
        self.routes: dict[str, dict[int, list[str]]] = {}

        for branch in self.active_branches:
            branch_stations = [
                s for s in self.stations.values() if branch in s.branches
            ]
            if not branch_stations:
                continue

            # Sort by stop_sequence for this branch (ascending = inbound order)
            branch_stations.sort(key=lambda s: s.stop_sequence.get(branch, 9999))
            inbound_ids = [s.id for s in branch_stations]    # low seq → terminus, high → downtown
            outbound_ids = list(reversed(inbound_ids))

            self.routes[branch] = {
                0: outbound_ids,  # downtown → terminus
                1: inbound_ids,   # terminus → downtown
            }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_route(self, branch: str, direction: int) -> list[str]:
        """Return ordered list of parent station IDs for branch+direction."""
        return self.routes.get(branch, {}).get(direction, [])

    def sample_travel_time(self, from_stop_id: str, to_stop_id: str) -> float:
        """
        Sample travel time (seconds) for a GTFS child stop_id segment.
        Falls back to a simple estimate if segment not in travel_times.json.
        """
        key = f"{from_stop_id}__{to_stop_id}"
        seg = self.segments.get(key)
        if seg:
            return seg.sample()
        # Fallback: 90s average (rough Green Line segment estimate)
        return max(30.0, random.gauss(90, 20))

    def sample_dwell(self, is_surface: bool, time_block: str) -> float:
        """
        Sample a base dwell time (seconds) for a stop.
        Caller adds per-passenger boarding/alighting time on top.
        """
        key = "surface" if is_surface else "underground"
        params = self.dwell_params[key]
        base = random.lognormvariate(params["mu"], params["sigma"])

        multipliers = self.dwell_params["peak_multiplier"]
        # Apply peak multiplier to variance (not mean) by scaling sigma effect
        mult = multipliers.get(time_block, 1.0)
        # Clamp base dwell: minimum 5s (doors must open), maximum 3 min
        return max(5.0, min(base * mult, 180.0))

    def sample_headway(self, branch: str, direction: int,
                       day_type: str, time_block: str) -> float:
        """
        Sample a headway (seconds between train dispatches) for the given context.
        Falls back through progressively broader keys if exact match not found.
        """
        dir_label = "outbound" if direction == 0 else "inbound"
        series_key = f"{branch}__{dir_label}__{day_type}"
        blocks = self.headways.get(series_key, {})

        dist = blocks.get(time_block) or blocks.get("midday")
        if dist:
            return dist.sample()
        # Ultimate fallback: 7-minute headway with some variance
        return max(60.0, random.gauss(420, 60))

    def get_stop_id(self, station_id: str, branch: str, direction: int) -> str:
        """Return the GTFS child stop_id for a station on a given branch+direction."""
        station = self.stations.get(station_id)
        if not station:
            return ""
        return station.stop_id_for(branch, direction)

    def init_merge_points(self, env: "simpy.Environment"):
        """
        Create SimPy Resources for merge-point stations.
        Must be called after a SimPy Environment exists.
        Only creates resources for branches that are active.
        """
        import simpy as _simpy
        active_merges = MERGE_STATION_IDS & set(self.stations.keys())
        for station_id in active_merges:
            self.merge_resources[station_id] = _simpy.Resource(env, capacity=1)

    def is_merge_point(self, station_id: str) -> bool:
        return station_id in self.merge_resources

    def __repr__(self):
        branch_summary = ", ".join(
            f"{b}({len(self.routes.get(b, {}).get(1, []))} stops)"
            for b in self.active_branches
        )
        return f"Network({branch_summary}, {len(self.segments)} segments)"
