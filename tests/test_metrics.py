"""
tests/test_metrics.py
---------------------
Unit tests for analysis/metrics.py: all five metric groups,
batch_summary_table, and full_report.

Uses lightweight mock objects rather than running full simulations,
which keeps the test suite fast and deterministic.
"""

import math
import pytest

from analysis.metrics import (
    trip_duration_stats,
    delay_stats,
    bunching_stats,
    station_stats,
    time_breakdown,
    batch_summary_table,
    full_report,
    SCHEDULED_TRIP_SEC,
    BUNCH_THRESHOLD,
    _pct,
    _dist_summary,
)


# ---------------------------------------------------------------------------
# Helper: build minimal mock objects
# ---------------------------------------------------------------------------

class _MockConfig:
    def __init__(self, branches=None, directions=None, day_type="weekday",
                 start_time="07:00", duration_min=120, end_station_id=None,
                 effective_pax_scale=0.25, breakdown_scale=0.5):
        self.branches = branches or ["Green-D"]
        self.directions = directions or [1]
        self.day_type = day_type
        self.start_time = start_time
        self.duration_min = duration_min
        self.end_station_id = end_station_id
        self.effective_pax_scale = effective_pax_scale
        self.breakdown_scale = breakdown_scale


class _MockTrain:
    def __init__(self, train_id="t-001", start=25200.0, end=29280.0,
                 breakdown_delay=0.0, breakdown_count=0, event_log=None):
        self.train_id = train_id
        self.trip_start_time = start
        self.trip_end_time = end
        self.total_breakdown_delay = breakdown_delay
        self.breakdown_count = breakdown_count
        self.event_log = event_log or []

    def trip_duration(self):
        if self.trip_start_time is not None and self.trip_end_time is not None:
            return self.trip_end_time - self.trip_start_time
        return None


class _MockResult:
    def __init__(self, trains=None, station_metrics=None, headway_gaps=None,
                 config=None, wall_time_sec=1.0):
        self.trains = trains or []
        self.station_metrics = station_metrics or []
        self.headway_gaps = headway_gaps or []
        self.config = config or _MockConfig()
        self.wall_time_sec = wall_time_sec


def _make_event_log(n_stops=5, dwell_sec=15.0, start_time=25200.0):
    """Produce a minimal event log with n_stops 'arrived' events."""
    events = []
    t = start_time
    for i in range(n_stops):
        events.append({
            "event": "arrived",
            "station_id": f"place-stop{i}",
            "station_name": f"Stop {i}",
            "time": t,
            "dwell_sec": dwell_sec,
        })
        t += dwell_sec + 90  # 90s travel between stops
    return events


# ---------------------------------------------------------------------------
# _pct helper
# ---------------------------------------------------------------------------

def test_pct_empty_returns_0():
    assert _pct([], 50) == 0.0


def test_pct_single_element():
    assert _pct([42.0], 0) == 42.0
    assert _pct([42.0], 100) == 42.0


def test_pct_median():
    data = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert abs(_pct(data, 50) - 3.0) < 1e-9


def test_pct_p90():
    data = list(range(1, 11))  # 1..10
    p90 = _pct(data, 90)
    assert 9.0 <= p90 <= 10.0


# ---------------------------------------------------------------------------
# _dist_summary
# ---------------------------------------------------------------------------

def test_dist_summary_empty():
    s = _dist_summary([])
    assert s == {"n": 0}


def test_dist_summary_keys():
    s = _dist_summary([60.0, 70.0, 80.0])
    for key in ("n", "mean", "std", "min", "p25", "p50", "p75", "p90", "p95", "p99", "max"):
        assert key in s, f"Missing key: {key}"


def test_dist_summary_single():
    s = _dist_summary([100.0])
    assert s["n"] == 1
    assert s["mean"] == 100.0
    assert s["std"] == 0.0


def test_dist_summary_ordering():
    data = [10.0, 20.0, 30.0, 40.0, 50.0]
    s = _dist_summary(data)
    assert s["min"] <= s["p25"] <= s["p50"] <= s["p75"] <= s["p90"] <= s["max"]


# ---------------------------------------------------------------------------
# 1. trip_duration_stats
# ---------------------------------------------------------------------------

def test_trip_duration_stats_keys():
    result = _MockResult(trains=[_MockTrain()])
    out = trip_duration_stats(result)
    for k in ("durations_sec", "summary", "n_trains_total", "n_trains_completed", "completion_rate"):
        assert k in out


def test_trip_duration_stats_completion_rate():
    trains = [
        _MockTrain(train_id="t1", start=25200.0, end=29280.0),
        _MockTrain(train_id="t2", start=25200.0, end=None),  # incomplete
    ]
    trains[1].trip_end_time = None
    result = _MockResult(trains=trains)
    out = trip_duration_stats(result)
    assert out["n_trains_total"] == 2
    assert out["n_trains_completed"] == 1
    assert abs(out["completion_rate"] - 0.5) < 1e-9


def test_trip_duration_stats_no_trains():
    result = _MockResult(trains=[])
    out = trip_duration_stats(result)
    assert out["n_trains_total"] == 0
    assert out["completion_rate"] == 0.0


# ---------------------------------------------------------------------------
# 2. delay_stats
# ---------------------------------------------------------------------------

def test_delay_stats_keys():
    trains = [_MockTrain(start=25200.0, end=25200.0 + 4080)]  # exactly on schedule
    result = _MockResult(trains=trains)
    out = delay_stats(result)
    for k in ("delays_sec", "summary", "pct_on_time", "pct_major_delay",
              "mean_delay_min", "schedule_sec"):
        assert k in out


def test_delay_stats_on_time_when_exact():
    schedule = SCHEDULED_TRIP_SEC["Green-D"]
    trains = [_MockTrain(start=25200.0, end=25200.0 + schedule)]
    result = _MockResult(trains=trains)
    out = delay_stats(result)
    assert out["pct_on_time"] == 100.0
    assert out["pct_major_delay"] == 0.0


def test_delay_stats_major_delay():
    schedule = SCHEDULED_TRIP_SEC["Green-D"]
    # 15-minute delay
    trains = [_MockTrain(start=25200.0, end=25200.0 + schedule + 900)]
    result = _MockResult(trains=trains)
    out = delay_stats(result)
    assert out["pct_major_delay"] == 100.0
    assert out["pct_on_time"] == 0.0


def test_delay_stats_no_completed_trains():
    train = _MockTrain()
    train.trip_end_time = None
    result = _MockResult(trains=[train])
    out = delay_stats(result)
    assert out["mean_delay_min"] is None


# ---------------------------------------------------------------------------
# 3. bunching_stats
# ---------------------------------------------------------------------------

def test_bunching_stats_keys():
    result = _MockResult(headway_gaps=[300.0, 420.0, 380.0, 200.0, 500.0])
    out = bunching_stats(result)
    for k in ("headway_gaps_sec", "summary", "cv", "bunching_events",
              "bunching_rate", "mean_headway_min", "reference_station"):
        assert k in out


def test_bunching_stats_empty_gaps():
    result = _MockResult(headway_gaps=[])
    out = bunching_stats(result)
    assert out["cv"] is None
    assert out["bunching_events"] == 0


def test_bunching_stats_single_gap():
    result = _MockResult(headway_gaps=[420.0])
    out = bunching_stats(result)
    assert out["cv"] is None  # need at least 2 gaps for std


def test_bunching_stats_cv_zero_for_uniform_headways():
    gaps = [420.0] * 10
    result = _MockResult(headway_gaps=gaps)
    out = bunching_stats(result)
    assert out["cv"] == 0.0
    assert out["bunching_events"] == 0


def test_bunching_stats_detects_bunching():
    mean = 420.0
    threshold = BUNCH_THRESHOLD * mean  # 210s
    # Two bunched gaps + rest normal
    gaps = [mean] * 8 + [threshold * 0.5, threshold * 0.5]
    result = _MockResult(headway_gaps=gaps)
    out = bunching_stats(result)
    assert out["bunching_events"] >= 2


def test_bunching_stats_mean_headway_min():
    gaps = [420.0] * 5
    result = _MockResult(headway_gaps=gaps)
    out = bunching_stats(result)
    assert abs(out["mean_headway_min"] - 7.0) < 0.01


# ---------------------------------------------------------------------------
# 4. station_stats
# ---------------------------------------------------------------------------

def _make_station_metrics(n=5):
    return [
        {
            "station_id": f"place-s{i}",
            "station_name": f"Station {i}",
            "total_arrived": 100 - i * 10,
            "total_boarded": 80 - i * 8,
            "total_stranded": i * 2,
            "total_missed_boardings": i * 3,
            "board_rate": (80 - i * 8) / max(1, 100 - i * 10),
            "mean_wait_sec": 120.0 + i * 30,
        }
        for i in range(n)
    ]


def test_station_stats_keys():
    result = _MockResult(station_metrics=_make_station_metrics())
    out = station_stats(result)
    for k in ("all_stations", "top_boardings", "worst_wait", "lowest_board_rate", "totals"):
        assert k in out


def test_station_stats_totals_sum_correctly():
    metrics = _make_station_metrics(5)
    result = _MockResult(station_metrics=metrics)
    out = station_stats(result)
    total_arrived = sum(m["total_arrived"] for m in metrics)
    total_boarded = sum(m["total_boarded"] for m in metrics)
    assert out["totals"]["total_arrived"] == total_arrived
    assert out["totals"]["total_boarded"] == total_boarded


def test_station_stats_board_rate_between_0_and_1():
    result = _MockResult(station_metrics=_make_station_metrics())
    out = station_stats(result)
    rate = out["totals"]["system_board_rate"]
    assert 0.0 <= rate <= 1.0


def test_station_stats_top_n_respected():
    result = _MockResult(station_metrics=_make_station_metrics(20))
    out = station_stats(result, top_n=5)
    assert len(out["top_boardings"]) <= 5
    assert len(out["worst_wait"]) <= 5


def test_station_stats_filters_inactive():
    metrics = _make_station_metrics(3)
    # Station with 0 arrivals should be excluded
    metrics.append({
        "station_id": "place-empty",
        "station_name": "Empty",
        "total_arrived": 0,
        "total_boarded": 0,
        "total_stranded": 0,
        "total_missed_boardings": 0,
        "board_rate": 1.0,
        "mean_wait_sec": None,
    })
    result = _MockResult(station_metrics=metrics)
    out = station_stats(result)
    ids = [m["station_id"] for m in out["all_stations"]]
    assert "place-empty" not in ids


# ---------------------------------------------------------------------------
# 5. time_breakdown
# ---------------------------------------------------------------------------

def _make_train_with_events(train_id="t-001", dwell_per_stop=20.0, n_stops=5,
                             breakdown_delay=0.0):
    total_dwell = dwell_per_stop * n_stops
    total_travel = n_stops * 90.0  # rough travel
    trip_duration = total_dwell + total_travel + breakdown_delay

    events = [
        {"event": "arrived", "dwell_sec": dwell_per_stop}
        for _ in range(n_stops)
    ]
    return _MockTrain(
        train_id=train_id,
        start=25200.0,
        end=25200.0 + trip_duration,
        breakdown_delay=breakdown_delay,
        event_log=events,
    )


def test_time_breakdown_keys():
    result = _MockResult(trains=[_make_train_with_events()])
    out = time_breakdown(result)
    assert "per_train" in out
    assert "aggregate" in out


def test_time_breakdown_per_train_fields():
    result = _MockResult(trains=[_make_train_with_events()])
    out = time_breakdown(result)
    assert len(out["per_train"]) == 1
    row = out["per_train"][0]
    for key in ("train_id", "trip_sec", "travel_sec", "dwell_sec",
                "breakdown_sec", "travel_pct", "dwell_pct", "breakdown_pct"):
        assert key in row, f"Missing key: {key}"


def test_time_breakdown_percentages_sum_to_100():
    train = _make_train_with_events(dwell_per_stop=30.0, n_stops=5, breakdown_delay=60.0)
    result = _MockResult(trains=[train])
    out = time_breakdown(result)
    row = out["per_train"][0]
    total = row["travel_pct"] + row["dwell_pct"] + row["breakdown_pct"]
    assert abs(total - 100.0) < 1.0, f"Percentages sum to {total}, expected 100"


def test_time_breakdown_no_trains():
    result = _MockResult(trains=[])
    out = time_breakdown(result)
    assert out["per_train"] == []
    assert out["aggregate"] == {}


def test_time_breakdown_skips_incomplete_trains():
    incomplete = _MockTrain()
    incomplete.trip_end_time = None
    result = _MockResult(trains=[incomplete])
    out = time_breakdown(result)
    assert out["per_train"] == []


def test_time_breakdown_aggregate_keys():
    trains = [_make_train_with_events(train_id=f"t-{i}") for i in range(3)]
    result = _MockResult(trains=trains)
    out = time_breakdown(result)
    for key in ("mean_travel_pct", "mean_dwell_pct", "mean_breakdown_pct",
                "mean_travel_sec", "mean_dwell_sec", "mean_breakdown_sec"):
        assert key in out["aggregate"]


# ---------------------------------------------------------------------------
# Batch summary table
# ---------------------------------------------------------------------------

class _MockBatchResult:
    def __init__(self, n=3, config=None):
        self.config = config or _MockConfig()
        self.n_runs = n
        self.run_summaries = [
            {
                "n_trains": 10 + i,
                "trip_duration_mean_sec": 4500.0 + i * 100,
                "trip_duration_p50_sec": 4400.0 + i * 100,
                "trip_duration_p90_sec": 5000.0 + i * 100,
                "trip_duration_min_sec": 3900.0,
                "trip_duration_max_sec": 5600.0,
                "total_breakdowns": i,
                "total_boarded": 1000 + i * 50,
                "total_stranded": i * 5,
            }
            for i in range(n)
        ]


def test_batch_summary_table_row_count():
    batch = _MockBatchResult(n=5)
    rows = batch_summary_table(batch)
    assert len(rows) == 5


def test_batch_summary_table_run_numbers():
    batch = _MockBatchResult(n=3)
    rows = batch_summary_table(batch)
    assert [r["run"] for r in rows] == [1, 2, 3]


def test_batch_summary_table_keys():
    batch = _MockBatchResult(n=1)
    row = batch_summary_table(batch)[0]
    for key in ("run", "n_trains", "mean_trip_min", "p50_trip_min", "p90_trip_min",
                "worst_trip_min", "best_trip_min", "breakdowns",
                "total_boarded", "total_stranded"):
        assert key in row, f"Missing key: {key}"


def test_batch_summary_table_minutes_conversion():
    batch = _MockBatchResult(n=1)
    row = batch_summary_table(batch)[0]
    # 4500s / 60 = 75.0 min
    assert abs(row["mean_trip_min"] - 75.0) < 0.1


# ---------------------------------------------------------------------------
# full_report
# ---------------------------------------------------------------------------

def test_full_report_keys():
    trains = [_make_train_with_events()]
    station_metrics = _make_station_metrics(3)
    result = _MockResult(
        trains=trains,
        station_metrics=station_metrics,
        headway_gaps=[300.0, 420.0, 380.0],
    )
    out = full_report(result)
    for k in ("config", "trip_duration", "delay", "bunching",
              "stations", "time_breakdown", "wall_time_sec"):
        assert k in out


def test_full_report_config_keys():
    result = _MockResult()
    out = full_report(result)
    for k in ("branches", "directions", "day_type", "start_time",
              "duration_min", "end_station_id", "pax_scale", "breakdown_scale"):
        assert k in out["config"]
