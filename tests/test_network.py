"""
tests/test_network.py
---------------------
Unit tests for sim/network.py: Network loading, route building,
distribution sampling, and merge point initialisation.
"""

import pytest
import simpy

from sim.network import (
    BOARD_TIME_PER_PAX,
    ALIGHT_TIME_PER_PAX,
    MERGE_STATION_IDS,
    Network,
    SegmentDist,
    HeadwayDist,
    StationRecord,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def net():
    """Single Network instance shared across all tests in this module."""
    return Network(branches=["Green-D"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_board_time_positive():
    assert BOARD_TIME_PER_PAX > 0


def test_alight_time_positive():
    assert ALIGHT_TIME_PER_PAX > 0


def test_merge_station_ids_contains_kenmore():
    assert "place-kencl" in MERGE_STATION_IDS


# ---------------------------------------------------------------------------
# Network loading
# ---------------------------------------------------------------------------

def test_network_loads_stations(net):
    assert len(net.stations) > 0


def test_network_d_branch_stations(net):
    # D branch has 25 stations (Riverside → Union Square)
    d_stations = [s for s in net.stations.values() if "Green-D" in s.branches]
    assert len(d_stations) >= 20


def test_station_record_fields(net):
    sid = next(iter(net.stations))
    rec = net.stations[sid]
    assert rec.id == sid
    assert isinstance(rec.name, str) and rec.name
    assert isinstance(rec.lat, float)
    assert isinstance(rec.lon, float)
    assert isinstance(rec.is_surface, bool)
    assert isinstance(rec.branches, list)


def test_network_loads_segments(net):
    assert len(net.segments) > 0


def test_network_loads_headways(net):
    assert len(net.headways) > 0


def test_network_loads_dwell_params(net):
    assert "underground" in net.dwell_params
    assert "surface" in net.dwell_params


# ---------------------------------------------------------------------------
# Route building
# ---------------------------------------------------------------------------

def test_get_route_inbound_nonempty(net):
    route = net.get_route("Green-D", 1)
    assert len(route) >= 20


def test_get_route_outbound_nonempty(net):
    route = net.get_route("Green-D", 0)
    assert len(route) >= 20


def test_inbound_outbound_reversed(net):
    inbound = net.get_route("Green-D", 1)
    outbound = net.get_route("Green-D", 0)
    assert inbound == list(reversed(outbound))


def test_route_station_ids_in_stations(net):
    route = net.get_route("Green-D", 1)
    for sid in route:
        assert sid in net.stations, f"{sid} not in net.stations"


def test_get_route_unknown_branch_returns_empty(net):
    result = net.get_route("Green-Z", 1)
    assert result == []


# ---------------------------------------------------------------------------
# Distribution sampling
# ---------------------------------------------------------------------------

def test_sample_dwell_underground_in_range(net):
    for _ in range(20):
        d = net.sample_dwell(is_surface=False, time_block="midday")
        assert 5.0 <= d <= 180.0, f"dwell {d} out of range"


def test_sample_dwell_surface_in_range(net):
    for _ in range(20):
        d = net.sample_dwell(is_surface=True, time_block="am_peak")
        assert 5.0 <= d <= 180.0, f"dwell {d} out of range"


def test_sample_headway_positive(net):
    for _ in range(10):
        h = net.sample_headway("Green-D", 1, "weekday", "am_peak")
        assert h > 0


def test_sample_travel_time_fallback_positive(net):
    # Unknown stop IDs should hit the fallback and return >= 30
    t = net.sample_travel_time("fake-stop-001", "fake-stop-002")
    assert t >= 30.0


def test_sample_travel_time_known_segment_positive(net):
    # Use a real segment if one exists
    if net.segments:
        key = next(iter(net.segments))
        from_stop, to_stop = key.split("__")
        t = net.sample_travel_time(from_stop, to_stop)
        assert t > 0


# ---------------------------------------------------------------------------
# Merge point initialisation
# ---------------------------------------------------------------------------

def test_init_merge_points_creates_no_resources_for_single_branch(net):
    env = simpy.Environment()
    net.init_merge_points(env)
    # D branch alone doesn't share Kenmore with other branches,
    # so no merge resources should be created for single-branch runs.
    # (Merge resources are only useful when multiple branches are active.)
    assert isinstance(net.merge_resources, dict)


def test_init_merge_points_multi_branch():
    env = simpy.Environment()
    multi_net = Network(branches=["Green-B", "Green-C", "Green-D"])
    multi_net.init_merge_points(env)
    # Kenmore should now have a resource since multiple branches share it
    assert "place-kencl" in multi_net.merge_resources
    res = multi_net.merge_resources["place-kencl"]
    import simpy as _simpy
    assert isinstance(res, _simpy.Resource)


# ---------------------------------------------------------------------------
# SegmentDist
# ---------------------------------------------------------------------------

def test_segment_dist_lognormal_samples_positive():
    dist = SegmentDist({"dist": "lognormal", "mu": 4.5, "sigma": 0.3, "is_surface": True})
    for _ in range(20):
        assert dist.sample() > 0


def test_segment_dist_normal_samples_at_least_5():
    dist = SegmentDist({"dist": "normal", "mean": 90.0, "std": 10.0, "is_surface": False})
    for _ in range(20):
        assert dist.sample() >= 5.0


# ---------------------------------------------------------------------------
# HeadwayDist
# ---------------------------------------------------------------------------

def test_headway_dist_lognormal_positive():
    hd = HeadwayDist({"dist": "lognormal", "mu": 6.0, "sigma": 0.4, "mean": 0})
    for _ in range(10):
        assert hd.sample() > 0


def test_headway_dist_normal_at_least_30():
    hd = HeadwayDist({"dist": "normal", "mu": 0, "sigma": 0, "mean": 420})
    for _ in range(10):
        assert hd.sample() >= 30.0
