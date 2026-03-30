"""
build_stations.py
-----------------
Parses MBTA GTFS data to produce data/stations.json — the canonical
station list used by the simulation and dashboard.

Each entry represents a physical station (GTFS parent_station), with:
  - id:           parent_station ID (e.g. "place-kencl")
  - name:         human-readable name (e.g. "Kenmore")
  - lat / lon:    coordinates from the parent station record
  - branches:     list of Green Line branches that serve this station
  - stop_ids:     {branch: {inbound: stop_id, outbound: stop_id}}
  - is_surface:   True if the stop is at-grade / street-running
  - stop_sequence: {branch: sequence_position} for ordering stops on each branch

Run from the project root:
    python data/build_stations.py
"""

import csv
import json
import os
from collections import defaultdict

GTFS_DIR = os.path.join(os.path.dirname(__file__), "gtfs")
OUT_FILE = os.path.join(os.path.dirname(__file__), "stations.json")

GREEN_ROUTES = {"Green-B", "Green-C", "Green-D", "Green-E"}

# level_id values that indicate surface / street-running operation
SURFACE_LEVELS = {
    "level_median",       # B/C branch street-median stops
    "level_in_street",    # street-running
    "level_ground",       # at-grade (GLX surface stations, D branch some stops)
    "level_0_platform",   # ground-level platform
}


def load_csv(filename):
    path = os.path.join(GTFS_DIR, filename)
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def parse_trips():
    """Return {trip_id: {route_id, direction_id, service_id}}."""
    trips = {}
    for row in load_csv("trips.txt"):
        if row["route_id"] in GREEN_ROUTES:
            trips[row["trip_id"]] = {
                "route_id": row["route_id"],
                "direction_id": int(row["direction_id"]),
                "service_id": row["service_id"],
            }
    return trips


def parse_stop_times(trips):
    """
    Return {trip_id: [(stop_sequence, stop_id), ...]} sorted by sequence.
    Only includes Green Line trips.
    """
    trip_stops = defaultdict(list)
    for row in load_csv("stop_times.txt"):
        if row["trip_id"] in trips:
            trip_stops[row["trip_id"]].append(
                (int(row["stop_sequence"]), row["stop_id"])
            )
    # Sort each trip's stops by sequence
    for tid in trip_stops:
        trip_stops[tid].sort(key=lambda x: x[0])
    return trip_stops


def parse_stops():
    """Return {stop_id: stop_dict}."""
    return {row["stop_id"]: row for row in load_csv("stops.txt")}


def derive_is_surface(stop_ids_for_parent, stops_by_id):
    """
    A station is surface if ANY of its platform stops has a surface level_id.
    Underground stations have all platforms underground.
    """
    for sid in stop_ids_for_parent:
        level = stops_by_id.get(sid, {}).get("level_id", "")
        if level in SURFACE_LEVELS:
            return True
    return False


def build_branch_stop_order(trips, trip_stops, stops_by_id):
    """
    For each (branch, direction), find the canonical stop order using the
    most-common trip pattern (longest trip = most stops = full route).

    Returns:
        {branch: {direction: [stop_id, ...]}}
    """
    # Collect stop sequences per (branch, direction): count occurrence
    pattern_counts = defaultdict(lambda: defaultdict(int))
    for tid, meta in trips.items():
        key = (meta["route_id"], meta["direction_id"])
        stops_seq = tuple(sid for _, sid in trip_stops.get(tid, []))
        if stops_seq:
            pattern_counts[key][stops_seq] += 1

    canonical = {}
    for (branch, direction), patterns in pattern_counts.items():
        # Use the most common full-length pattern
        best = max(patterns, key=lambda p: (len(p), patterns[p]))
        canonical.setdefault(branch, {})[direction] = list(best)

    return canonical


def main():
    print("Loading GTFS data...")
    trips = parse_trips()
    print(f"  {len(trips)} Green Line trips loaded")

    trip_stops = parse_stop_times(trips)
    stops_by_id = parse_stops()

    # Map stop_id -> parent_station
    stop_to_parent = {
        sid: s["parent_station"]
        for sid, s in stops_by_id.items()
        if s.get("parent_station")
    }

    # Collect which branches + directions serve each parent station
    parent_branches = defaultdict(set)       # parent_id -> set of route_ids
    parent_stop_ids = defaultdict(set)       # parent_id -> set of child stop_ids (Green Line only)
    parent_branch_stops = defaultdict(lambda: defaultdict(lambda: defaultdict(str)))
    # parent_id -> branch -> direction -> stop_id (pick first found; direction 0=outbound,1=inbound)

    for tid, meta in trips.items():
        branch = meta["route_id"]
        direction = meta["direction_id"]
        for _, sid in trip_stops.get(tid, []):
            parent = stop_to_parent.get(sid)
            if not parent:
                continue
            parent_branches[parent].add(branch)
            parent_stop_ids[parent].add(sid)
            # Record the stop_id used for this branch+direction at this parent
            # (last one wins — consistent because same branch/dir always uses same platform)
            parent_branch_stops[parent][branch][direction] = sid

    # Build canonical branch stop orders
    canonical_order = build_branch_stop_order(trips, trip_stops, stops_by_id)

    # Build stop_sequence position maps: parent_id -> branch -> sequence_index
    parent_sequence = defaultdict(dict)
    for branch, dirs in canonical_order.items():
        # Use direction=1 (inbound, toward downtown) as the canonical ordering
        # so sequence 0 = outermost terminus, last = Government Center / downtown
        inbound_stops = dirs.get(1, dirs.get(0, []))
        for seq_idx, sid in enumerate(inbound_stops):
            parent = stop_to_parent.get(sid)
            if parent:
                parent_sequence[parent][branch] = seq_idx

    # Load parent station records (location_type == 1)
    parent_records = {
        sid: s
        for sid, s in stops_by_id.items()
        if s.get("location_type") == "1"
    }

    # Build output
    stations = []
    for parent_id in sorted(parent_branches.keys()):
        parent = parent_records.get(parent_id, {})

        # Fall back to averaging child stop coords if parent record missing
        if parent.get("stop_lat"):
            lat = float(parent["stop_lat"])
            lon = float(parent["stop_lon"])
            name = parent["stop_name"]
        else:
            child_stops = [stops_by_id[sid] for sid in parent_stop_ids[parent_id]
                           if sid in stops_by_id and stops_by_id[sid].get("stop_lat")]
            if not child_stops:
                print(f"  WARNING: no coordinates for {parent_id}, skipping")
                continue
            lat = sum(float(s["stop_lat"]) for s in child_stops) / len(child_stops)
            lon = sum(float(s["stop_lon"]) for s in child_stops) / len(child_stops)
            name = child_stops[0]["stop_name"].split(" - ")[0]

        branches = sorted(parent_branches[parent_id])
        is_surface = derive_is_surface(parent_stop_ids[parent_id], stops_by_id)

        # Build stop_ids dict: branch -> {inbound: sid, outbound: sid}
        stop_ids = {}
        for branch, dir_stops in parent_branch_stops[parent_id].items():
            stop_ids[branch] = {
                "outbound": dir_stops.get(0, ""),  # direction 0 = away from downtown
                "inbound": dir_stops.get(1, ""),   # direction 1 = toward downtown
            }

        station = {
            "id": parent_id,
            "name": name,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "branches": branches,
            "is_surface": is_surface,
            "stop_ids": stop_ids,
            "stop_sequence": parent_sequence.get(parent_id, {}),
        }
        stations.append(station)

    # Sort by branch then sequence for readable output
    # Primary sort: first branch alphabetically; secondary: sequence on that branch
    def sort_key(s):
        b = s["branches"][0]
        seq = s["stop_sequence"].get(b, 9999)
        return (b, seq)

    stations.sort(key=sort_key)

    with open(OUT_FILE, "w") as f:
        json.dump(stations, f, indent=2)

    print(f"\nWrote {len(stations)} stations to {OUT_FILE}")

    # Print summary
    branch_counts = defaultdict(int)
    surface_counts = defaultdict(int)
    for s in stations:
        for b in s["branches"]:
            branch_counts[b] += 1
        if s["is_surface"]:
            for b in s["branches"]:
                surface_counts[b] += 1

    print("\nStation counts by branch:")
    for b in sorted(branch_counts):
        print(f"  {b}: {branch_counts[b]} stations ({surface_counts[b]} surface)")


if __name__ == "__main__":
    main()
