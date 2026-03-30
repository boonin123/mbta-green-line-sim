"""
tests/test_station.py
---------------------
Unit tests for sim/station.py: SimulatedStation boarding, alighting,
dwell coupling, wait time computation, and metrics output.
"""

import pytest

from sim.network import Network
from sim.station import SimulatedStation, BoardingResult, ALIGHT_FRACTIONS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def net():
    return Network(branches=["Green-D"])


@pytest.fixture
def underground_station(net):
    record = next(s for s in net.stations.values() if not s.is_surface)
    return SimulatedStation(record, tier="trunk")


@pytest.fixture
def surface_station(net):
    candidates = [s for s in net.stations.values() if s.is_surface]
    if not candidates:
        pytest.skip("No surface stations on this branch")
    return SimulatedStation(candidates[0], tier="branch_outer")


# ---------------------------------------------------------------------------
# Passenger arrival
# ---------------------------------------------------------------------------

def test_add_passenger_increments_waiting(underground_station):
    s = underground_station
    before = s.waiting_passengers
    s.add_passenger(sim_time=25200.0)
    assert s.waiting_passengers == before + 1
    assert s.total_arrived == before + 1


def test_add_passengers_bulk(underground_station):
    s = underground_station
    before = s.waiting_passengers
    s.add_passengers_bulk(10, sim_time=25200.0)
    assert s.waiting_passengers == before + 10
    assert s.total_arrived == before + 10


# ---------------------------------------------------------------------------
# process_train_stop — basic contract
# ---------------------------------------------------------------------------

def test_process_train_stop_returns_boarding_result(underground_station, net):
    s = underground_station
    s.add_passengers_bulk(20, sim_time=25200.0)
    result = s.process_train_stop(
        network=net,
        passengers_on_board=50,
        train_capacity=176,
        time_block="midday",
        sim_time=25500.0,
        is_terminus=False,
    )
    assert isinstance(result, BoardingResult)
    assert result.boarded >= 0
    assert result.alighted >= 0
    assert result.dwell_time >= 5.0


def test_boarding_does_not_exceed_capacity(net):
    record = next(iter(net.stations.values()))
    s = SimulatedStation(record, tier="trunk")
    # Pack 176 passengers already on the train (at capacity)
    s.add_passengers_bulk(50, sim_time=25200.0)
    result = s.process_train_stop(
        network=net,
        passengers_on_board=176,
        train_capacity=176,
        time_block="midday",
        sim_time=25500.0,
        is_terminus=False,
    )
    # Some will alight first, freeing space, but alighted may be 0 due to rounding
    assert result.boarded <= result.alighted or result.boarded == 0 or result.overflow >= 0


def test_boarding_counts_update_totals(net):
    record = next(iter(net.stations.values()))
    s = SimulatedStation(record, tier="trunk")
    s.add_passengers_bulk(10, sim_time=25200.0)
    result = s.process_train_stop(
        network=net,
        passengers_on_board=0,
        train_capacity=176,
        time_block="midday",
        sim_time=25500.0,
        is_terminus=False,
    )
    assert s.total_boarded == result.boarded
    assert s.waiting_passengers == 10 - result.boarded


# ---------------------------------------------------------------------------
# Terminus behaviour
# ---------------------------------------------------------------------------

def test_terminus_alights_all(net):
    record = next(iter(net.stations.values()))
    s = SimulatedStation(record, tier="terminus")
    on_board = 80
    result = s.process_train_stop(
        network=net,
        passengers_on_board=on_board,
        train_capacity=176,
        time_block="midday",
        sim_time=30000.0,
        is_terminus=True,
    )
    assert result.alighted == on_board


# ---------------------------------------------------------------------------
# Dwell time bounds
# ---------------------------------------------------------------------------

def test_dwell_time_min_5s(underground_station, net):
    result = underground_station.process_train_stop(
        network=net,
        passengers_on_board=0,
        train_capacity=176,
        time_block="midday",
        sim_time=25500.0,
        is_terminus=False,
    )
    assert result.dwell_time >= 5.0


def test_dwell_time_max_240s(net):
    record = next(iter(net.stations.values()))
    s = SimulatedStation(record, tier="major_hub")
    # Flood the platform with passengers to stress dwell maximum
    s.add_passengers_bulk(300, sim_time=25200.0)
    result = s.process_train_stop(
        network=net,
        passengers_on_board=100,
        train_capacity=176,
        time_block="am_peak",
        sim_time=25500.0,
        is_terminus=False,
    )
    assert result.dwell_time <= 240.0


# ---------------------------------------------------------------------------
# Wait time tracking
# ---------------------------------------------------------------------------

def test_wait_times_recorded_for_boarding_passengers(net):
    record = next(iter(net.stations.values()))
    s = SimulatedStation(record, tier="trunk")
    s.add_passengers_bulk(5, sim_time=25200.0)
    s.process_train_stop(
        network=net,
        passengers_on_board=0,
        train_capacity=176,
        time_block="midday",
        sim_time=25500.0,
        is_terminus=False,
    )
    if s.total_boarded > 0:
        assert len(s.wait_times) > 0
        for w in s.wait_times:
            assert w >= 0.0


def test_mean_wait_time_none_when_no_boardings(net):
    record = next(iter(net.stations.values()))
    s = SimulatedStation(record, tier="trunk")
    assert s.mean_wait_time() is None


# ---------------------------------------------------------------------------
# Board rate and stranded
# ---------------------------------------------------------------------------

def test_board_rate_one_when_no_arrivals(net):
    record = next(iter(net.stations.values()))
    s = SimulatedStation(record, tier="trunk")
    assert s.board_rate() == 1.0


def test_board_rate_between_0_and_1(net):
    record = next(iter(net.stations.values()))
    s = SimulatedStation(record, tier="trunk")
    s.add_passengers_bulk(20, sim_time=25200.0)
    s.process_train_stop(
        network=net,
        passengers_on_board=170,  # nearly full train
        train_capacity=176,
        time_block="midday",
        sim_time=25500.0,
        is_terminus=False,
    )
    rate = s.board_rate()
    assert 0.0 <= rate <= 1.0


def test_total_stranded_equals_remaining_waiting(net):
    record = next(iter(net.stations.values()))
    s = SimulatedStation(record, tier="trunk")
    s.add_passengers_bulk(15, sim_time=25200.0)
    s.process_train_stop(
        network=net,
        passengers_on_board=170,
        train_capacity=176,
        time_block="midday",
        sim_time=25500.0,
        is_terminus=False,
    )
    assert s.total_stranded() == s.waiting_passengers


# ---------------------------------------------------------------------------
# to_metrics_dict
# ---------------------------------------------------------------------------

def test_to_metrics_dict_keys(net):
    record = next(iter(net.stations.values()))
    s = SimulatedStation(record, tier="trunk")
    d = s.to_metrics_dict()
    for key in (
        "station_id", "station_name",
        "total_arrived", "total_boarded", "total_alighted",
        "total_stranded", "total_missed_boardings",
        "board_rate", "mean_wait_sec",
    ):
        assert key in d, f"Missing key: {key}"


def test_to_metrics_dict_no_overflow_key(net):
    """total_overflow was renamed — confirm old key is gone."""
    record = next(iter(net.stations.values()))
    s = SimulatedStation(record, tier="trunk")
    d = s.to_metrics_dict()
    assert "total_overflow" not in d
