"""
dashboard/map_view.py
---------------------
Interactive single-run "Ride the Train" view.

The user picks an origin station, destination station, target arrival time,
and day type, then clicks "Simulate Ride". The dashboard infers the correct
branch and direction, runs a simulation with enough lead time to capture a
train covering that journey, and plays back the origin→destination portion
of that train's trip on an animated Plotly map.

Layout:
  - Left panel: origin/destination pickers, arrival time, day type
  - Right panel: Plotly map + status bar + stop timeline
"""

from __future__ import annotations

import json
import math
import random

import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html, no_update

from sim.runner import SimConfig, RunResult, single_run
from sim.network import Network

MAPBOX_STYLE = "open-street-map"

# Load full network once at import time for station options + route inference
_NET = Network(branches=["Green-B", "Green-C", "Green-D", "Green-E"])

# Bounding box computed from actual station coordinates
_ALL_LATS = [s.lat for s in _NET.stations.values()]
_ALL_LONS = [s.lon for s in _NET.stations.values()]
MAP_CENTER = {
    "lat": (min(_ALL_LATS) + max(_ALL_LATS)) / 2,
    "lon": (min(_ALL_LONS) + max(_ALL_LONS)) / 2,
}
# Zoom to fit the full Green Line (~0.19° lon span → zoom 11 fits well)
MAP_ZOOM = 11


# ---------------------------------------------------------------------------
# Helpers: route inference and station options
# ---------------------------------------------------------------------------

def _build_all_station_options() -> list[dict]:
    """All unique stations across all branches, alpha-sorted by name."""
    options = {
        sid: {"label": rec.name, "value": sid}
        for sid, rec in _NET.stations.items()
    }
    return sorted(options.values(), key=lambda o: o["label"])


def _reachable_from(origin_id: str) -> list[dict]:
    """
    Return destination options for stations reachable from origin_id on at
    least one branch/direction, excluding origin itself.
    """
    reachable = set()
    for branch in _NET.active_branches:
        for direction in [0, 1]:
            route = _NET.get_route(branch, direction)
            if origin_id in route:
                o_idx = route.index(origin_id)
                # All stations after origin on this route are reachable
                reachable.update(route[o_idx + 1:])
    reachable.discard(origin_id)
    options = [
        {"label": _NET.stations[sid].name, "value": sid}
        for sid in reachable
        if sid in _NET.stations
    ]
    return sorted(options, key=lambda o: o["label"])


def _infer_route(origin_id: str, dest_id: str) -> tuple[str, int] | None:
    """
    Return (branch, direction) such that origin appears before dest on that
    branch's route. Prefers inbound (direction=1) when multiple options exist.
    Returns None if no valid route found.
    """
    for direction in [1, 0]:  # prefer inbound
        for branch in ["Green-B", "Green-C", "Green-D", "Green-E"]:
            route = _NET.get_route(branch, direction)
            if origin_id in route and dest_id in route:
                if route.index(origin_id) < route.index(dest_id):
                    return branch, direction
    return None


def _estimate_stop_count(branch: str, direction: int,
                         origin_id: str, dest_id: str) -> int:
    """Number of stops between origin and dest (inclusive of both)."""
    route = _NET.get_route(branch, direction)
    try:
        return route.index(dest_id) - route.index(origin_id) + 1
    except ValueError:
        return 10


def _inject_transit_frames(event_log: list[dict]) -> list[dict]:
    """
    Insert synthetic 'moving' frames between each 'departed' and the following
    'arrived' event so the train marker glides smoothly between stations.

    Two intermediate frames are inserted at 1/3 and 2/3 of the way along
    the straight-line path between consecutive stations.
    """
    result: list[dict] = []
    for i, evt in enumerate(event_log):
        result.append(evt)
        if evt["event"] != "departed":
            continue

        next_arrived = next(
            (e for e in event_log[i + 1:] if e["event"] == "arrived"),
            None,
        )
        if next_arrived is None:
            continue

        from_id = evt["station_id"]
        to_id = next_arrived["station_id"]
        if from_id not in _NET.stations or to_id not in _NET.stations:
            continue

        from_rec = _NET.stations[from_id]
        to_rec = _NET.stations[to_id]
        t_dep = evt["time"]
        t_arr = next_arrived["time"]
        pax = evt.get("passengers", 0)

        for frac in (0.33, 0.67):
            result.append({
                "event": "moving",
                "lat": from_rec.lat + frac * (to_rec.lat - from_rec.lat),
                "lon": from_rec.lon + frac * (to_rec.lon - from_rec.lon),
                "time": t_dep + frac * (t_arr - t_dep),
                "station_id": to_id,
                "station_name": next_arrived["station_name"],
                "passengers": pax,
                "dwell_sec": 0,
            })
    return result


def _trim_to_journey(event_log: list[dict],
                     origin_id: str, dest_id: str) -> list[dict]:
    """
    Slice event log to the portion from first arrival at origin through
    arrival at destination. Returns the full log if either stop is missing.
    """
    origin_idx = next(
        (i for i, e in enumerate(event_log)
         if e["event"] == "arrived" and e["station_id"] == origin_id),
        None,
    )
    if origin_idx is None:
        return event_log

    dest_idx = next(
        (i for i in range(origin_idx, len(event_log))
         if event_log[i]["event"] == "arrived"
         and event_log[i]["station_id"] == dest_id),
        None,
    )
    if dest_idx is None:
        return event_log[origin_idx:]

    return event_log[origin_idx: dest_idx + 1]


def _time_str_to_sec(t: str) -> float:
    """'HH:MM' → seconds since midnight."""
    h, m = int(t[:2]), int(t[3:5])
    return h * 3600 + m * 60


def _sec_to_time_str(s: float) -> str:
    """Seconds since midnight → 'H:MM AM/PM'."""
    h = int(s // 3600) % 24
    m = int((s % 3600) // 60)
    period = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {period}"


_ALL_STATION_OPTIONS = _build_all_station_options()

# Sensible defaults: Riverside → Kenmore on D branch
_DEFAULT_ORIGIN = "place-river"
_DEFAULT_DEST = "place-kencl"


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def layout() -> html.Div:
    return html.Div([
        dbc.Row([
            # --- Left panel ---
            dbc.Col([
                html.H5("Ride the Train", className="mt-2 mb-1 fw-bold"),
                html.P("Pick your journey and see how the sim plays out.",
                       className="text-muted small mb-3"),

                html.Label("Origin station", className="form-label"),
                dcc.Dropdown(
                    id="map-origin",
                    options=_ALL_STATION_OPTIONS,
                    value=_DEFAULT_ORIGIN,
                    clearable=False,
                    placeholder="Select origin…",
                    className="mb-3",
                ),

                html.Label("Destination station", className="form-label"),
                dcc.Dropdown(
                    id="map-dest",
                    options=_reachable_from(_DEFAULT_ORIGIN),
                    value=_DEFAULT_DEST,
                    clearable=False,
                    placeholder="Select destination…",
                    className="mb-3",
                ),

                # Inferred route badge
                html.Div(id="map-route-badge", className="mb-3"),

                html.Label("Departure time", className="form-label"),
                dcc.Dropdown(
                    id="map-depart-time",
                    options=[
                        {"label": "5:30 AM", "value": "05:30"},
                        {"label": "7:00 AM (AM peak)", "value": "07:00"},
                        {"label": "7:30 AM (AM peak)", "value": "07:30"},
                        {"label": "8:00 AM (AM peak)", "value": "08:00"},
                        {"label": "9:00 AM", "value": "09:00"},
                        {"label": "12:00 PM (midday)", "value": "12:00"},
                        {"label": "5:00 PM (PM peak)", "value": "17:00"},
                        {"label": "5:30 PM (PM peak)", "value": "17:30"},
                        {"label": "6:00 PM", "value": "18:00"},
                        {"label": "8:00 PM (evening)", "value": "20:00"},
                    ],
                    value="08:00",
                    clearable=False,
                    className="mb-3",
                ),

                html.Label("Day type", className="form-label"),
                dcc.Dropdown(
                    id="map-day",
                    options=[
                        {"label": "Weekday", "value": "weekday"},
                        {"label": "Saturday", "value": "saturday"},
                        {"label": "Sunday", "value": "sunday"},
                    ],
                    value="weekday",
                    clearable=False,
                    className="mb-3",
                ),

                html.Hr(),

                dbc.Button(
                    "Simulate Ride",
                    id="map-run-btn",
                    color="primary",
                    className="w-100 mb-3",
                    n_clicks=0,
                ),

                html.Div(id="map-playback-controls", children=[
                    dbc.Button("▶ Play", id="map-play-btn", color="success",
                               outline=True, size="sm", className="me-2",
                               n_clicks=0),
                    dbc.Button("⏸ Pause", id="map-pause-btn", color="secondary",
                               outline=True, size="sm", className="me-2",
                               n_clicks=0),
                    dbc.Button("⏮ Reset", id="map-reset-btn", color="secondary",
                               outline=True, size="sm",
                               n_clicks=0),
                ], style={"display": "none"}),

                html.Div(id="map-trip-summary", className="mt-3"),

            ], width=2, className="bg-light border-end py-3 px-3",
               style={"minHeight": "100vh"}),

            # --- Right panel ---
            dbc.Col([
                dbc.Row([
                    dbc.Col(html.Div(id="map-current-stop", className="fw-bold"), width=3),
                    dbc.Col(html.Div(id="map-next-stop", className="text-muted"), width=3),
                    dbc.Col(html.Div(id="map-eta"), width=3),
                    dbc.Col(html.Div(id="map-crowding"), width=3),
                ], className="border-bottom py-2 px-2 mb-2 bg-white",
                   style={"fontSize": "0.85rem"}),

                dcc.Graph(
                    id="map-figure",
                    figure=_empty_map(),
                    style={"height": "520px"},
                    config={"scrollZoom": True},
                ),

                html.H6("Stop timeline", className="mt-3 mb-1 fw-bold px-2"),
                html.Div(id="map-timeline-table", className="px-2"),

                dcc.Store(id="map-event-log-store"),
                dcc.Store(id="map-frame-index", data=0),
                dcc.Interval(id="map-interval", interval=500, n_intervals=0,
                             disabled=True),
            ], width=10, className="px-3 py-2"),
        ], className="g-0"),

        # --- End-of-ride stats modal ---
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle("🏁 Ride Complete"), close_button=True),
            dbc.ModalBody(id="map-end-modal-body"),
            dbc.ModalFooter(
                dbc.Button("Close", id="map-end-modal-close", color="secondary",
                           n_clicks=0),
            ),
        ], id="map-end-modal", size="lg", is_open=False),
    ])


# ---------------------------------------------------------------------------
# Map figure helpers
# ---------------------------------------------------------------------------

def _empty_map() -> go.Figure:
    fig = go.Figure(go.Scattermapbox())
    fig.update_layout(
        mapbox=dict(style=MAPBOX_STYLE, center=MAP_CENTER, zoom=MAP_ZOOM),
        margin=dict(l=0, r=0, t=0, b=0),
        showlegend=False,
    )
    return fig


def _build_map_figure(event_log: list[dict], frame_idx: int,
                      branch: str, direction: int,
                      origin_id: str, dest_id: str) -> go.Figure:
    """
    Build map for the current playback frame.
    Shows the route segment between origin and dest; highlights origin (blue),
    destination (gold), visited stops (green), train (red).
    """
    if not event_log:
        return _empty_map()

    full_route = _NET.get_route(branch, direction)

    # Clip route display to origin→dest segment
    try:
        o_idx = full_route.index(origin_id)
        d_idx = full_route.index(dest_id)
        display_route = full_route[o_idx: d_idx + 1]
    except ValueError:
        display_route = full_route

    route_lats = [_NET.stations[s].lat for s in display_route if s in _NET.stations]
    route_lons = [_NET.stations[s].lon for s in display_route if s in _NET.stations]

    visited_ids = {
        e["station_id"] for e in event_log[:frame_idx + 1]
        if e["event"] == "arrived"
    }

    # Bucket stations by role for styling
    normal_lats, normal_lons, normal_names = [], [], []
    visited_lats, visited_lons, visited_names = [], [], []

    for sid in display_route:
        if sid not in _NET.stations:
            continue
        if sid in (origin_id, dest_id):
            continue  # drawn separately
        rec = _NET.stations[sid]
        if sid in visited_ids:
            visited_lats.append(rec.lat)
            visited_lons.append(rec.lon)
            visited_names.append(rec.name)
        else:
            normal_lats.append(rec.lat)
            normal_lons.append(rec.lon)
            normal_names.append(rec.name)

    fig = go.Figure()

    # Route line
    fig.add_trace(go.Scattermapbox(
        lat=route_lats, lon=route_lons, mode="lines",
        line=dict(color="#94a3b8", width=3),
        hoverinfo="skip", showlegend=False,
    ))

    # Unvisited intermediate stops
    if normal_lats:
        fig.add_trace(go.Scattermapbox(
            lat=normal_lats, lon=normal_lons, mode="markers",
            marker=dict(size=9, color="#cbd5e1"),
            text=normal_names,
            hovertemplate="%{text}<extra></extra>",
            showlegend=False,
        ))

    # Visited intermediate stops
    if visited_lats:
        fig.add_trace(go.Scattermapbox(
            lat=visited_lats, lon=visited_lons, mode="markers",
            marker=dict(size=11, color="#22c55e"),
            text=visited_names,
            hovertemplate="%{text}<extra></extra>",
            showlegend=False,
        ))

    # Origin marker (blue)
    if origin_id in _NET.stations:
        rec = _NET.stations[origin_id]
        fig.add_trace(go.Scattermapbox(
            lat=[rec.lat], lon=[rec.lon], mode="markers",
            marker=dict(size=15, color="#3b82f6"),
            text=[f"⬆ {rec.name} (Origin)"],
            hovertemplate="%{text}<extra></extra>",
            showlegend=False,
        ))

    # Destination marker (gold)
    if dest_id in _NET.stations:
        rec = _NET.stations[dest_id]
        fig.add_trace(go.Scattermapbox(
            lat=[rec.lat], lon=[rec.lon], mode="markers",
            marker=dict(size=15, color="#f59e0b"),
            text=[f"★ {rec.name} (Destination)"],
            hovertemplate="%{text}<extra></extra>",
            showlegend=False,
        ))

    # Train marker — use interpolated lat/lon for "moving" frames
    current_evt = event_log[frame_idx] if frame_idx < len(event_log) else None
    if not current_evt:
        for e in reversed(event_log[:frame_idx + 1]):
            if e["event"] in ("arrived", "departed", "moving"):
                current_evt = e
                break

    if current_evt:
        if current_evt["event"] == "moving":
            train_lat = current_evt["lat"]
            train_lon = current_evt["lon"]
            pax = current_evt.get("passengers", 0)
            hover = f"→ {current_evt['station_name']}<br>{pax} pax on board"
        elif current_evt["station_id"] in _NET.stations:
            rec = _NET.stations[current_evt["station_id"]]
            train_lat, train_lon = rec.lat, rec.lon
            pax = current_evt.get("passengers", 0)
            hover = f"{rec.name}<br>{pax} pax on board"
        else:
            train_lat = train_lon = None

        if train_lat is not None:
            fig.add_trace(go.Scattermapbox(
                lat=[train_lat], lon=[train_lon], mode="markers",
                marker=dict(size=18, color="#ef4444"),
                hovertext=hover,
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=False,
            ))

    # Auto-zoom to fit the displayed route segment with padding
    if route_lats and route_lons:
        lat_min, lat_max = min(route_lats), max(route_lats)
        lon_min, lon_max = min(route_lons), max(route_lons)
        # Pad by 10% each side
        lat_pad = max((lat_max - lat_min) * 0.10, 0.005)
        lon_pad = max((lon_max - lon_min) * 0.10, 0.005)
        lat_center = (lat_min + lat_max) / 2
        lon_center = (lon_min + lon_max) / 2
        # Mapbox zoom: each level halves the visible area.
        # ~0.19° lon span ≈ zoom 11; use max span to ensure both axes fit.
        span = max(lat_max - lat_min + 2 * lat_pad,
                   (lon_max - lon_min + 2 * lon_pad) * 0.6)  # lat-equivalent
        zoom = max(9, min(14, round(11.0 - math.log2(span / 0.09), 1)))
        center = {"lat": lat_center, "lon": lon_center}
    else:
        zoom = MAP_ZOOM
        center = MAP_CENTER

    fig.update_layout(
        mapbox=dict(style=MAPBOX_STYLE, center=center, zoom=zoom),
        margin=dict(l=0, r=0, t=0, b=0),
        showlegend=False,
        uirevision="map",
    )
    return fig


def _build_timeline_table(event_log: list[dict], frame_idx: int,
                          dest_id: str) -> dbc.Table:
    arrivals = [e for e in event_log if e["event"] == "arrived"]
    current_station = next(
        (e["station_id"] for e in reversed(event_log[:frame_idx + 1])
         if e["event"] == "arrived"),
        None,
    )

    rows = []
    for e in arrivals:
        t_sec_abs = e["time"]
        t_h = int(t_sec_abs // 3600) % 24
        t_m = int((t_sec_abs % 3600) // 60)
        t_s = int(t_sec_abs % 60)
        period = "AM" if t_h < 12 else "PM"
        t_h12 = t_h % 12 or 12
        time_str = f"{t_h12}:{t_m:02d}:{t_s:02d} {period}"

        is_current = e["station_id"] == current_station
        is_dest = e["station_id"] == dest_id
        if is_dest:
            row_style = {"background": "#fef3c7", "fontWeight": "bold"}
        elif is_current:
            row_style = {"background": "#dcfce7", "fontWeight": "bold"}
        else:
            row_style = {}

        rows.append(html.Tr([
            html.Td(
                [e["station_name"], dbc.Badge("dest", color="warning", className="ms-1")
                 if is_dest else ""],
                style={"fontSize": "0.8rem"},
            ),
            html.Td(time_str, style={"fontSize": "0.8rem"}),
            html.Td(f"{e['dwell_sec']:.0f}s", style={"fontSize": "0.8rem"}),
            html.Td(str(e.get("passengers", "—")), style={"fontSize": "0.8rem"}),
        ], style=row_style))

    return dbc.Table(
        [
            html.Thead(html.Tr([
                html.Th("Station", style={"fontSize": "0.8rem"}),
                html.Th("Arrival", style={"fontSize": "0.8rem"}),
                html.Th("Dwell", style={"fontSize": "0.8rem"}),
                html.Th("On-board", style={"fontSize": "0.8rem"}),
            ])),
            html.Tbody(rows),
        ],
        bordered=True, hover=True, size="sm", responsive=True,
        style={"maxHeight": "220px", "overflowY": "auto", "display": "block"},
    )


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

def register_callbacks(app):

    # --- Update destination options when origin changes ---
    @app.callback(
        Output("map-dest", "options"),
        Output("map-dest", "value"),
        Output("map-route-badge", "children"),
        Input("map-origin", "value"),
        State("map-dest", "value"),
    )
    def update_dest_options(origin_id, current_dest):
        if not origin_id:
            return _ALL_STATION_OPTIONS, current_dest, ""

        options = _reachable_from(origin_id)
        reachable_ids = {o["value"] for o in options}

        # Keep current dest if still reachable, else pick first option
        new_dest = current_dest if current_dest in reachable_ids else (
            options[0]["value"] if options else None
        )

        # Show inferred route badge
        badge = _route_badge(origin_id, new_dest)
        return options, new_dest, badge

    # --- Update route badge when destination changes ---
    @app.callback(
        Output("map-route-badge", "children", allow_duplicate=True),
        Input("map-dest", "value"),
        State("map-origin", "value"),
        prevent_initial_call=True,
    )
    def update_route_badge(dest_id, origin_id):
        return _route_badge(origin_id, dest_id)

    # --- Run simulation ---
    @app.callback(
        Output("map-event-log-store", "data"),
        Output("map-frame-index", "data"),
        Output("map-interval", "disabled"),
        Output("map-playback-controls", "style"),
        Output("map-trip-summary", "children"),
        Input("map-run-btn", "n_clicks"),
        State("map-origin", "value"),
        State("map-dest", "value"),
        State("map-depart-time", "value"),
        State("map-day", "value"),
        prevent_initial_call=True,
    )
    def run_simulation(n_clicks, origin_id, dest_id, depart_time_str, day_type):
        if not origin_id or not dest_id:
            return no_update, no_update, no_update, no_update, \
                dbc.Alert("Select an origin and destination.", color="warning")

        route_info = _infer_route(origin_id, dest_id)
        if route_info is None:
            return no_update, no_update, no_update, no_update, \
                dbc.Alert("No direct route found between these stations.", color="danger")

        branch, direction = route_info
        depart_sec = _time_str_to_sec(depart_time_str)

        # Estimate travel time: ~100s per stop between origin and dest
        n_stops = _estimate_stop_count(branch, direction, origin_id, dest_id)
        est_travel_sec = n_stops * 100

        # Back-calculate sim_start: a train must leave the terminus early enough
        # to reach the origin station by depart_sec.
        full_route = _NET.get_route(branch, direction)
        try:
            stops_before_origin = full_route.index(origin_id)
        except ValueError:
            stops_before_origin = 0
        HEADWAY_BUFFER_SEC = 840  # one full headway of slack
        pre_buffer = stops_before_origin * 100 + HEADWAY_BUFFER_SEC
        sim_start = max(0, depart_sec - pre_buffer)
        duration_min = (pre_buffer + est_travel_sec + HEADWAY_BUFFER_SEC) / 60

        cfg = SimConfig(
            branches=[branch],
            directions=[direction],
            day_type=day_type,
            start_time=sim_start,
            duration_min=duration_min,
            end_station_id=dest_id,
            seed=random.randint(1, 999_999),
        )
        result: RunResult = single_run(cfg)

        # Find the next train departing at or after depart_sec, then fall back
        # to closest if none qualify.
        candidates_after, candidates_any = [], []
        for t in result.trains:
            arrivals = {e["station_id"]: e["time"]
                        for e in t.event_log if e["event"] == "arrived"}
            if origin_id in arrivals and dest_id in arrivals:
                origin_time = arrivals[origin_id]
                dest_time = arrivals[dest_id]
                wait = origin_time - depart_sec
                if wait >= 0:
                    candidates_after.append((wait, t, origin_time, dest_time))
                candidates_any.append((abs(wait), t, origin_time, dest_time))

        candidates = candidates_after if candidates_after else candidates_any
        if not candidates:
            return no_update, no_update, no_update, no_update, \
                dbc.Alert(
                    "No train found covering this journey in the sim window. "
                    "Try a different departure time or origin/destination.",
                    color="warning",
                )

        candidates.sort(key=lambda x: x[0])
        _, best_train, origin_arrival_time, actual_dest_arrival = candidates[0]

        journey_log = _inject_transit_frames(
            _trim_to_journey(best_train.event_log, origin_id, dest_id)
        )

        journey_sec = actual_dest_arrival - origin_arrival_time
        wait_sec = max(0.0, origin_arrival_time - depart_sec)
        stops_made = sum(1 for e in journey_log if e["event"] == "arrived")

        origin_name = _NET.stations[origin_id].name if origin_id in _NET.stations else origin_id
        dest_name = _NET.stations[dest_id].name if dest_id in _NET.stations else dest_id
        dir_label = "Inbound" if direction == 1 else "Outbound"

        summary = dbc.Card([
            dbc.CardHeader(
                f"{origin_name} → {dest_name}",
                className="fw-bold small",
            ),
            dbc.CardBody([
                html.P([
                    html.Span("Route: ", className="text-muted"),
                    dbc.Badge(f"{branch} {dir_label}", color="success", className="me-1"),
                ], className="mb-1 small"),
                html.P([
                    html.Span("You arrive: ", className="text-muted"),
                    _sec_to_time_str(depart_sec),
                ], className="mb-1 small"),
                html.P([
                    html.Span("Train departs: ", className="text-muted"),
                    _sec_to_time_str(origin_arrival_time),
                    html.Span(
                        f"  (+{wait_sec/60:.0f} min wait)",
                        className="text-muted",
                    ),
                ], className="mb-1 small"),
                html.P([
                    html.Span("Arrives at dest: ", className="text-muted"),
                    html.Span(_sec_to_time_str(actual_dest_arrival), className="fw-bold"),
                ], className="mb-1 small"),
                html.P([
                    html.Span("Travel time: ", className="text-muted"),
                    f"{journey_sec/60:.1f} min",
                ], className="mb-1 small"),
                html.P([
                    html.Span("Stops: ", className="text-muted"),
                    f"{stops_made}",
                ], className="mb-1 small"),
                html.P([
                    html.Span("Breakdowns: ", className="text-muted"),
                    f"{best_train.breakdown_count}",
                ], className="mb-0 small"),
            ]),
        ], className="mt-2")

        # --- Quick comparison: 30 independent single runs with varied seeds ---
        journey_times = []
        for _ in range(30):
            cmp_cfg = SimConfig(
                branches=[branch],
                directions=[direction],
                day_type=day_type,
                start_time=sim_start,
                duration_min=duration_min,
                end_station_id=dest_id,
                seed=random.randint(1, 999_999),
            )
            cmp_result: RunResult = single_run(cmp_cfg)
            for t in cmp_result.trains:
                t_arrivals = {e["station_id"]: e["time"]
                              for e in t.event_log if e["event"] == "arrived"}
                if origin_id in t_arrivals and dest_id in t_arrivals:
                    journey_times.append(t_arrivals[dest_id] - t_arrivals[origin_id])
                    break  # one train per run

        batch_stats: dict | None = None
        if len(journey_times) >= 5:
            jt_sorted = sorted(journey_times)
            n_jt = len(jt_sorted)
            p25 = jt_sorted[max(0, int(n_jt * 0.25) - 1)]
            p50 = jt_sorted[max(0, int(n_jt * 0.50) - 1)]
            p90 = jt_sorted[max(0, int(n_jt * 0.90) - 1)]
            n_faster = sum(1 for t in journey_times if t > journey_sec)
            pct_rank = round(n_faster / n_jt * 100)
            batch_stats = {
                "p25": p25, "p50": p50, "p90": p90,
                "pct_rank": pct_rank, "n": n_jt,
            }

        store_data = json.dumps({
            "events": journey_log,
            "origin_id": origin_id,
            "dest_id": dest_id,
            "branch": branch,
            "direction": direction,
            "breakdown_count": best_train.breakdown_count,
            "journey_sec": journey_sec,
            "batch_stats": batch_stats,
        })
        return store_data, 0, False, {"display": "block"}, summary

    # --- Advance playback frame ---
    @app.callback(
        Output("map-frame-index", "data", allow_duplicate=True),
        Output("map-interval", "disabled", allow_duplicate=True),
        Input("map-interval", "n_intervals"),
        State("map-event-log-store", "data"),
        State("map-frame-index", "data"),
        prevent_initial_call=True,
    )
    def advance_frame(n_intervals, store_json, frame_idx):
        if not store_json:
            return no_update, True
        store = json.loads(store_json)
        event_log = store["events"]
        next_idx = frame_idx + 1
        if next_idx >= len(event_log):
            return frame_idx, True
        return next_idx, False

    # --- Update map + status bar ---
    @app.callback(
        Output("map-figure", "figure"),
        Output("map-current-stop", "children"),
        Output("map-next-stop", "children"),
        Output("map-eta", "children"),
        Output("map-crowding", "children"),
        Output("map-timeline-table", "children"),
        Input("map-frame-index", "data"),
        State("map-event-log-store", "data"),
        prevent_initial_call=True,
    )
    def update_map(frame_idx, store_json):
        if not store_json:
            return _empty_map(), "", "", "", "", ""

        store = json.loads(store_json)
        event_log = store["events"]
        origin_id = store["origin_id"]
        dest_id = store["dest_id"]
        branch = store["branch"]
        direction = store["direction"]

        fig = _build_map_figure(event_log, frame_idx, branch, direction,
                                origin_id, dest_id)
        timeline = _build_timeline_table(event_log, frame_idx, dest_id)

        current_evt = event_log[frame_idx] if frame_idx < len(event_log) else None
        if not current_evt:
            return fig, "—", "—", "—", "—", timeline

        is_moving = current_evt["event"] == "moving"
        arrivals = [e for e in event_log if e["event"] == "arrived"]

        if is_moving:
            # Heading toward next station — find the last arrived for timeline position
            last_arrived = next(
                (e for e in reversed(event_log[:frame_idx])
                 if e["event"] == "arrived"),
                None,
            )
            cur_arr_idx = next(
                (i for i, e in enumerate(arrivals)
                 if e["station_id"] == (last_arrived["station_id"] if last_arrived else None)),
                None,
            )
            current_stop_label = [
                html.Span("→ ", className="text-muted"),
                current_evt["station_name"],
            ]
        else:
            cur_arr_idx = next(
                (i for i, e in enumerate(arrivals)
                 if e["station_id"] == current_evt["station_id"]),
                None,
            )
            current_stop_label = [
                html.Span("Now: ", className="text-muted"),
                current_evt["station_name"],
            ]

        next_stop_name, eta_str = "—", "—"
        if cur_arr_idx is not None and cur_arr_idx + 1 < len(arrivals):
            nxt = arrivals[cur_arr_idx + 1]
            next_stop_name = nxt["station_name"]
            gap = nxt["time"] - current_evt["time"]
            eta_str = f"~{int(gap//60)}m {int(gap%60):02d}s"

        # ETA to destination
        dest_evt = next(
            (e for e in arrivals if e["station_id"] == dest_id), None
        )
        if dest_evt and current_evt["station_id"] != dest_id:
            gap_to_dest = dest_evt["time"] - current_evt["time"]
            dest_name = _NET.stations[dest_id].name if dest_id in _NET.stations else dest_id
            eta_dest = f"~{int(gap_to_dest//60)}m to {dest_name}"
        else:
            eta_dest = eta_str

        pax = current_evt.get("passengers", 0)
        capacity = 176
        pct = pax / capacity * 100
        if pct < 50:
            label, color = "Light", "success"
        elif pct < 80:
            label, color = "Moderate", "warning"
        else:
            label, color = "Crowded", "danger"

        return (
            fig,
            current_stop_label,
            [html.Span("Next: ", className="text-muted"), next_stop_name],
            [html.Span("ETA: ", className="text-muted"), eta_dest],
            dbc.Badge(f"{label} ({pax}/{capacity})", color=color, className="ms-1"),
            timeline,
        )

    @app.callback(
        Output("map-interval", "disabled", allow_duplicate=True),
        Input("map-play-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def play(_):
        return False

    @app.callback(
        Output("map-interval", "disabled", allow_duplicate=True),
        Input("map-pause-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def pause(_):
        return True

    @app.callback(
        Output("map-frame-index", "data", allow_duplicate=True),
        Output("map-interval", "disabled", allow_duplicate=True),
        Input("map-reset-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def reset(_):
        return 0, True

    # --- Show end-of-ride modal when playback reaches last frame ---
    @app.callback(
        Output("map-end-modal", "is_open"),
        Output("map-end-modal-body", "children"),
        Input("map-frame-index", "data"),
        State("map-event-log-store", "data"),
        State("map-end-modal", "is_open"),
        prevent_initial_call=True,
    )
    def show_end_modal(frame_idx, store_json, is_open):
        if not store_json or is_open:
            return no_update, no_update
        store = json.loads(store_json)
        event_log = store["events"]
        last_evt = event_log[-1] if event_log else {}
        if frame_idx < len(event_log) - 1 or last_evt.get("event") != "arrived":
            return no_update, no_update
        # Reached final arrived frame — build modal
        return True, _build_end_modal_body(store)

    # --- Close modal ---
    @app.callback(
        Output("map-end-modal", "is_open", allow_duplicate=True),
        Input("map-end-modal-close", "n_clicks"),
        prevent_initial_call=True,
    )
    def close_end_modal(_):
        return False


# ---------------------------------------------------------------------------
# Helper: end-of-ride stats modal body
# ---------------------------------------------------------------------------

def _build_end_modal_body(store: dict) -> html.Div:
    event_log = store["events"]
    origin_id = store["origin_id"]
    dest_id = store["dest_id"]
    journey_sec = store.get("journey_sec", 0)
    breakdown_count = store.get("breakdown_count", 0)
    batch_stats = store.get("batch_stats")

    journey_min = journey_sec / 60
    origin_name = _NET.stations[origin_id].name if origin_id in _NET.stations else origin_id
    dest_name = _NET.stations[dest_id].name if dest_id in _NET.stations else dest_id

    # Crowding profile: average load factor across journey
    pax_readings = [e.get("passengers", 0) for e in event_log if e["event"] == "arrived"]
    avg_pax = sum(pax_readings) / len(pax_readings) if pax_readings else 0
    capacity = 176
    avg_pct = avg_pax / capacity * 100
    if avg_pct < 50:
        crowd_label, crowd_color = "Light", "success"
    elif avg_pct < 80:
        crowd_label, crowd_color = "Moderate", "warning"
    else:
        crowd_label, crowd_color = "Crowded", "danger"

    # Stat cards
    your_card = dbc.Col(dbc.Card([
        dbc.CardBody([
            html.H3(f"{journey_min:.1f}", className="card-title text-primary mb-0"),
            html.Small("min — your ride", className="text-muted"),
        ])
    ], className="text-center h-100"), width=3)

    if batch_stats:
        p50_min = batch_stats["p50"] / 60
        p90_min = batch_stats["p90"] / 60
        pct_rank = batch_stats["pct_rank"]
        vs_p50 = journey_sec - batch_stats["p50"]
        vs_str = f"{'+'if vs_p50 >= 0 else ''}{vs_p50/60:.1f} min vs typical"
        vs_color = "danger" if vs_p50 > 120 else ("warning" if vs_p50 > 0 else "success")
        beat_str = f"Top {pct_rank}%" if pct_rank > 0 else "Slowest run"
        beat_color = "success" if pct_rank >= 50 else ("warning" if pct_rank >= 20 else "danger")

        comparison_cards = [
            dbc.Col(dbc.Card([
                dbc.CardBody([
                    html.H3(f"{p50_min:.1f}", className="card-title text-success mb-0"),
                    html.Small("min — typical (p50)", className="text-muted"),
                ])
            ], className="text-center h-100"), width=3),
            dbc.Col(dbc.Card([
                dbc.CardBody([
                    html.H3(f"{p90_min:.1f}", className="card-title text-warning mb-0"),
                    html.Small("min — busy day (p90)", className="text-muted"),
                ])
            ], className="text-center h-100"), width=3),
            dbc.Col(dbc.Card([
                dbc.CardBody([
                    html.H3(beat_str, className=f"card-title text-{beat_color} mb-0"),
                    html.Small(f"of {batch_stats['n']} simulated rides", className="text-muted"),
                ])
            ], className="text-center h-100"), width=3),
        ]
        vs_badge = dbc.Alert(vs_str, color=vs_color, className="py-1 px-3 mb-0 small")
    else:
        comparison_cards = []
        vs_badge = html.Span()

    return html.Div([
        html.H6(f"{origin_name}  →  {dest_name}",
                className="text-muted mb-3"),
        dbc.Row([your_card] + comparison_cards, className="g-2 mb-3"),
        dbc.Row([
            dbc.Col(vs_badge, width="auto"),
        ], className="mb-3"),
        html.Hr(className="my-2"),
        dbc.Row([
            dbc.Col([
                html.Span("Breakdowns en route: ", className="text-muted small"),
                dbc.Badge(str(breakdown_count),
                          color="danger" if breakdown_count else "success",
                          className="ms-1"),
            ], width="auto"),
            dbc.Col([
                html.Span("Average crowding: ", className="text-muted small"),
                dbc.Badge(f"{crowd_label} ({avg_pct:.0f}%)", color=crowd_color,
                          className="ms-1"),
            ], width="auto"),
            dbc.Col([
                html.Span("Stops made: ", className="text-muted small"),
                html.Strong(str(sum(1 for e in event_log if e["event"] == "arrived"))),
            ], width="auto"),
        ], className="g-3"),
        html.P(
            f"Comparison based on {batch_stats['n']} simulated trips for the same route and time." if batch_stats else
            "Not enough batch data for comparison.",
            className="text-muted small mt-3 mb-0",
        ),
    ])


# ---------------------------------------------------------------------------
# Helper: route badge
# ---------------------------------------------------------------------------

def _route_badge(origin_id: str | None, dest_id: str | None) -> html.Div:
    if not origin_id or not dest_id:
        return html.Div()
    route_info = _infer_route(origin_id, dest_id)
    if route_info is None:
        return dbc.Alert("No direct route between these stations.",
                         color="danger", className="py-1 small")
    branch, direction = route_info
    dir_label = "Inbound" if direction == 1 else "Outbound"
    return html.Div([
        html.Span("Route: ", className="text-muted small"),
        dbc.Badge(f"{branch}", color="success", className="me-1"),
        dbc.Badge(dir_label, color="secondary"),
    ])
