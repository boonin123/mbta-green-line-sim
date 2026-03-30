"""
tests/test_train.py
-------------------
Unit tests for sim/train.py: Train SimPy process, event logging,
trip duration, breakdown tracking, and partial-route behaviour.
"""

import pytest
import simpy

from sim.network import Network
from sim.station import SimulatedStation
from sim.train import Train, load_breakdown_rates, TRAIN_CAPACITY


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def net():
    return Network(branches=["Green-D"])


@pytest.fixture(scope="module")
def breakdown_rates():
    return load_breakdown_rates()


def _build_stations(net, route):
    """Create a SimulatedStation for every stop in route."""
    stations = {}
    for sid in route:
        if sid in net.stations:
            stations[sid] = SimulatedStation(net.stations[sid])
    return stations


def _run_train(net, breakdown_rates, route=None, breakdown_scale=0.0,
               start_seconds=25200.0):
    """
    Run a single Train through `route` in a fresh SimPy environment.
    Returns (train, env) after simulation completes.
    """
    env = simpy.Environment(initial_time=start_seconds)
    full_route = route or net.get_route("Green-D", 1)
    stations = _build_stations(net, full_route)

    train = Train(
        train_id="test-001",
        branch="Green-D",
        direction=1,
        start_time=start_seconds,
        network=net,
        stations=stations,
        breakdown_rates=breakdown_rates,
        day_type="weekday",
        route_override=full_route,
        breakdown_scale=breakdown_scale,
    )
    env.process(train.run(env))
    env.run()
    return train, env


# ---------------------------------------------------------------------------
# Basic process completion
# ---------------------------------------------------------------------------

def test_train_completes_full_route(net, breakdown_rates):
    train, _ = _run_train(net, breakdown_rates, breakdown_scale=0.0)
    assert train.trip_end_time is not None
    assert train.trip_start_time is not None


def test_trip_duration_positive(net, breakdown_rates):
    train, _ = _run_train(net, breakdown_rates, breakdown_scale=0.0)
    dur = train.trip_duration()
    assert dur is not None
    assert dur > 0


def test_trip_duration_reasonable(net, breakdown_rates):
    # D branch ~68 min scheduled; even with zero pax, should be within 30–180 min
    train, _ = _run_train(net, breakdown_rates, breakdown_scale=0.0)
    dur_min = train.trip_duration() / 60
    assert 30 <= dur_min <= 180, f"Trip duration {dur_min:.1f} min is outside expected range"


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------

def test_event_log_nonempty(net, breakdown_rates):
    train, _ = _run_train(net, breakdown_rates)
    assert len(train.event_log) > 0


def test_event_log_has_departed_terminus(net, breakdown_rates):
    train, _ = _run_train(net, breakdown_rates)
    types = [e["event"] for e in train.event_log]
    assert "departed_terminus" in types


def test_event_log_has_arrived_and_departed(net, breakdown_rates):
    train, _ = _run_train(net, breakdown_rates)
    types = {e["event"] for e in train.event_log}
    assert "arrived" in types
    assert "departed" in types


def test_event_log_fields(net, breakdown_rates):
    train, _ = _run_train(net, breakdown_rates)
    required = {"time", "event", "train_id", "branch", "station_id",
                "station_name", "passengers", "dwell_sec"}
    for evt in train.event_log:
        for key in required:
            assert key in evt, f"Event missing key '{key}': {evt}"


def test_event_log_times_nondecreasing(net, breakdown_rates):
    train, _ = _run_train(net, breakdown_rates)
    times = [e["time"] for e in train.event_log]
    for i in range(1, len(times)):
        assert times[i] >= times[i - 1], (
            f"Event log time went backwards at index {i}: {times[i-1]} → {times[i]}"
        )


def test_event_log_passengers_within_capacity(net, breakdown_rates):
    train, _ = _run_train(net, breakdown_rates)
    for evt in train.event_log:
        assert 0 <= evt["passengers"] <= TRAIN_CAPACITY


# ---------------------------------------------------------------------------
# Breakdown mechanics
# ---------------------------------------------------------------------------

def test_no_breakdowns_when_scale_zero(net, breakdown_rates):
    train, _ = _run_train(net, breakdown_rates, breakdown_scale=0.0)
    assert train.breakdown_count == 0
    assert train.total_breakdown_delay == 0.0


def test_breakdowns_possible_when_scale_high(net, breakdown_rates):
    # Run 10 trains with very high breakdown scale; expect at least one breakdown
    total_breakdowns = 0
    for seed in range(10):
        import random
        random.seed(seed)
        train, _ = _run_train(net, breakdown_rates, breakdown_scale=100.0)
        total_breakdowns += train.breakdown_count
    assert total_breakdowns > 0


def test_breakdown_delay_nonnegative(net, breakdown_rates):
    train, _ = _run_train(net, breakdown_rates, breakdown_scale=1.0)
    assert train.total_breakdown_delay >= 0.0


def test_breakdown_events_in_log_when_breakdowns_occur(net, breakdown_rates):
    import random
    random.seed(42)
    # Use high scale to force breakdowns
    train, _ = _run_train(net, breakdown_rates, breakdown_scale=50.0)
    if train.breakdown_count > 0:
        bd_events = [e for e in train.event_log if e["event"] == "breakdown_start"]
        assert len(bd_events) == train.breakdown_count


# ---------------------------------------------------------------------------
# Partial route (route_override with truncated route)
# ---------------------------------------------------------------------------

def test_partial_route_shorter_duration(net, breakdown_rates):
    full_route = net.get_route("Green-D", 1)
    half_route = full_route[:len(full_route) // 2]

    train_full, _ = _run_train(net, breakdown_rates, route=full_route, breakdown_scale=0.0)
    train_half, _ = _run_train(net, breakdown_rates, route=half_route, breakdown_scale=0.0)

    assert train_half.trip_duration() < train_full.trip_duration()


def test_partial_route_last_event_at_truncated_terminus(net, breakdown_rates):
    full_route = net.get_route("Green-D", 1)
    short_route = full_route[:5]

    train, _ = _run_train(net, breakdown_rates, route=short_route, breakdown_scale=0.0)
    # The last non-terminus arrival should be at the 5th stop
    arrived_stations = [
        e["station_id"] for e in train.event_log if e["event"] == "arrived"
    ]
    assert arrived_stations[-1] == short_route[-1]


# ---------------------------------------------------------------------------
# to_metrics_dict
# ---------------------------------------------------------------------------

def test_metrics_dict_keys(net, breakdown_rates):
    train, _ = _run_train(net, breakdown_rates)
    m = train.to_metrics_dict()
    for key in ("train_id", "branch", "direction", "start_time",
                "end_time", "trip_duration_sec", "breakdown_count",
                "breakdown_delay_sec"):
        assert key in m, f"Missing key: {key}"


def test_metrics_dict_trip_duration_matches(net, breakdown_rates):
    train, _ = _run_train(net, breakdown_rates)
    m = train.to_metrics_dict()
    assert m["trip_duration_sec"] == train.trip_duration()
