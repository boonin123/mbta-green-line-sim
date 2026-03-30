"""
dashboard/batch_view.py
-----------------------
Batch I/O analysis view for the MBTA Green Line simulation dashboard.

Layout:
  - Sidebar: configuration controls (branch, direction, day, time, runs)
  - Main panel:
      Row 1: Summary stat cards (mean trip, p90 trip, breakdowns/run, board rate)
      Row 2: Trip duration distribution histogram + delay CDF
      Row 3: Bunching chart (headway gap scatter) + station heatmap (boardings)
      Row 4: Results table (per-run summary)

Runs execute in a background thread so the UI can show a live progress bar.
"""

from __future__ import annotations

import threading

import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html, no_update

from analysis.metrics import (
    batch_summary_table,
    station_stats,
)
from sim.runner import BatchResult, SimConfig, batch_run, single_run
# Re-use route helpers from map_view
from dashboard.map_view import (
    _ALL_STATION_OPTIONS,
    _infer_route,
    _reachable_from,
)

# ---------------------------------------------------------------------------
# Branch / direction options
# ---------------------------------------------------------------------------

BRANCH_OPTIONS = [
    {"label": "Green-B (Boston College)", "value": "Green-B"},
    {"label": "Green-C (Cleveland Circle)", "value": "Green-C"},
    {"label": "Green-D (Riverside) — default", "value": "Green-D"},
    {"label": "Green-E (Heath Street)", "value": "Green-E"},
    {"label": "All branches (B+C+D+E)", "value": "ALL"},
]

DAY_OPTIONS = [
    {"label": "Weekday", "value": "weekday"},
    {"label": "Saturday", "value": "saturday"},
    {"label": "Sunday", "value": "sunday"},
]

TIME_OPTIONS = [
    {"label": "Early morning (5:00)", "value": "05:00"},
    {"label": "AM peak (7:00)", "value": "07:00"},
    {"label": "Midday (11:00)", "value": "11:00"},
    {"label": "PM peak (16:30)", "value": "16:30"},
    {"label": "Evening (19:00)", "value": "19:00"},
]


# ---------------------------------------------------------------------------
# Background-run state  (shared between callbacks via module-level dict)
# ---------------------------------------------------------------------------

_BATCH_LOCK = threading.Lock()
_BATCH_STATE: dict = {
    "running": False,
    "progress": 0,
    "total": 0,
    "result": None,   # BatchResult once done
    "error": None,
}


def _run_batch_thread(cfg: SimConfig, n_runs: int) -> None:
    """Worker that runs batch_run and updates _BATCH_STATE as it goes."""
    global _BATCH_STATE

    def _on_progress(completed: int, total: int) -> None:
        with _BATCH_LOCK:
            _BATCH_STATE["progress"] = completed

    try:
        result = batch_run(cfg, n_runs, verbose=False, progress_fn=_on_progress)
        with _BATCH_LOCK:
            _BATCH_STATE["result"] = result
            _BATCH_STATE["running"] = False
    except Exception as exc:
        with _BATCH_LOCK:
            _BATCH_STATE["error"] = str(exc)
            _BATCH_STATE["running"] = False


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def layout() -> html.Div:
    return html.Div([
        dbc.Row([
            # --- Sidebar ---
            dbc.Col([
                html.H5("Simulation Config", className="mt-2 mb-3 fw-bold"),

                html.Label("Origin station", className="form-label"),
                dcc.Dropdown(
                    id="batch-origin",
                    options=_ALL_STATION_OPTIONS,
                    value=None,
                    clearable=True,
                    placeholder="Full branch (default)",
                    className="mb-2",
                ),

                html.Label("Destination station", className="form-label"),
                dcc.Dropdown(
                    id="batch-dest",
                    options=_ALL_STATION_OPTIONS,
                    value=None,
                    clearable=True,
                    placeholder="End of branch (default)",
                    className="mb-2",
                ),

                html.Div(id="batch-route-badge", className="mb-3"),

                html.Label("Branch(es)", className="form-label"),
                dcc.Dropdown(
                    id="batch-branch",
                    options=BRANCH_OPTIONS,
                    value="Green-D",
                    clearable=False,
                    className="mb-3",
                ),

                html.Label("Direction", className="form-label"),
                dbc.RadioItems(
                    id="batch-direction",
                    options=[
                        {"label": "Inbound (→ downtown)", "value": 1},
                        {"label": "Outbound (← terminus)", "value": 0},
                    ],
                    value=1,
                    className="mb-3",
                ),

                html.P(
                    "When origin + destination are set, branch and direction "
                    "are inferred automatically.",
                    className="text-muted",
                    style={"fontSize": "0.75rem"},
                ),

                html.Label("Day type", className="form-label"),
                dcc.Dropdown(
                    id="batch-day",
                    options=DAY_OPTIONS,
                    value="weekday",
                    clearable=False,
                    className="mb-3",
                ),

                html.Label("Start time", className="form-label"),
                dcc.Dropdown(
                    id="batch-start-time",
                    options=TIME_OPTIONS,
                    value="07:00",
                    clearable=False,
                    className="mb-3",
                ),

                html.Label("Window (minutes)", className="form-label"),
                dbc.Input(
                    id="batch-duration",
                    type="number",
                    value=120,
                    min=30,
                    max=480,
                    step=30,
                    className="mb-3",
                ),

                html.Label("Number of runs", className="form-label"),
                dbc.Input(
                    id="batch-runs",
                    type="number",
                    value=100,
                    min=10,
                    max=2000,
                    step=10,
                    className="mb-3",
                ),

                dbc.Button(
                    "Run Simulation",
                    id="batch-run-btn",
                    color="success",
                    className="w-100 mt-2",
                    n_clicks=0,
                ),

                # Progress bar (hidden until a run starts)
                html.Div(id="batch-progress-container", children=[
                    html.Div(className="mt-3 mb-1 small text-muted",
                             id="batch-progress-label",
                             children="Starting…"),
                    dbc.Progress(
                        id="batch-progress-bar",
                        value=0,
                        striped=True,
                        animated=True,
                        color="success",
                        style={"height": "10px"},
                    ),
                ], style={"display": "none"}),

                html.Div(id="batch-status", className="mt-3 text-muted small"),

                # Interval for polling thread progress (disabled when idle)
                dcc.Interval(
                    id="batch-poll-interval",
                    interval=300,
                    n_intervals=0,
                    disabled=True,
                ),

            ], width=2, className="bg-light border-end py-3 px-3",
               style={"minHeight": "100vh"}),

            # --- Main panel ---
            dbc.Col([
                # Summary cards
                dbc.Row(id="batch-summary-cards", className="mb-3 mt-3"),

                # Charts row 1: histogram + delay CDF
                dbc.Row([
                    dbc.Col(dcc.Graph(id="batch-duration-hist"), width=6),
                    dbc.Col(dcc.Graph(id="batch-delay-cdf"), width=6),
                ], className="mb-3"),

                # Charts row 2: bunching + station boardings
                dbc.Row([
                    dbc.Col(dcc.Graph(id="batch-bunching-chart"), width=6),
                    dbc.Col(dcc.Graph(id="batch-station-bar"), width=6),
                ], className="mb-3"),

                # Per-run table
                html.H6("Per-run results", className="fw-bold"),
                html.Div(id="batch-results-table"),

            ], width=10, className="px-4"),
        ], className="g-0"),
    ])


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

def _resolve_branches(branch_val: str) -> list[str]:
    if branch_val == "ALL":
        return ["Green-B", "Green-C", "Green-D", "Green-E"]
    return [branch_val]


def register_callbacks(app):
    """Register all batch view callbacks against the provided Dash app."""

    # --- Cascade: update destination options when origin changes ---
    @app.callback(
        Output("batch-dest", "options"),
        Output("batch-dest", "value"),
        Output("batch-route-badge", "children"),
        Input("batch-origin", "value"),
        State("batch-dest", "value"),
    )
    def update_batch_dest(origin_id, current_dest):
        if not origin_id:
            return _ALL_STATION_OPTIONS, current_dest, ""
        options = _reachable_from(origin_id)
        reachable_ids = {o["value"] for o in options}
        new_dest = current_dest if current_dest in reachable_ids else None
        return options, new_dest, _batch_route_badge(origin_id, new_dest)

    @app.callback(
        Output("batch-route-badge", "children", allow_duplicate=True),
        Input("batch-dest", "value"),
        State("batch-origin", "value"),
        prevent_initial_call=True,
    )
    def update_batch_route_badge(dest_id, origin_id):
        return _batch_route_badge(origin_id, dest_id)

    # --- Button click: validate, kick off background thread, show progress ---
    @app.callback(
        Output("batch-run-btn", "disabled"),
        Output("batch-progress-container", "style"),
        Output("batch-progress-bar", "value"),
        Output("batch-progress-label", "children"),
        Output("batch-poll-interval", "disabled"),
        Output("batch-status", "children"),
        Input("batch-run-btn", "n_clicks"),
        State("batch-origin", "value"),
        State("batch-dest", "value"),
        State("batch-branch", "value"),
        State("batch-direction", "value"),
        State("batch-day", "value"),
        State("batch-start-time", "value"),
        State("batch-duration", "value"),
        State("batch-runs", "value"),
        prevent_initial_call=True,
    )
    def start_batch(n_clicks, origin_id, dest_id, branch_val, direction,
                    day_type, start_time, duration, n_runs):
        global _BATCH_STATE

        n_runs = int(n_runs or 100)

        if origin_id and dest_id:
            route_info = _infer_route(origin_id, dest_id)
            if route_info:
                branches = [route_info[0]]
                direction = route_info[1]
                end_station_id = dest_id
            else:
                branches = _resolve_branches(branch_val)
                end_station_id = dest_id
        else:
            branches = _resolve_branches(branch_val)
            end_station_id = dest_id

        cfg = SimConfig(
            branches=branches,
            directions=[direction],
            day_type=day_type,
            start_time=start_time,
            duration_min=float(duration or 120),
            seed=42,
            end_station_id=end_station_id,
        )

        with _BATCH_LOCK:
            _BATCH_STATE.update({
                "running": True,
                "progress": 0,
                "total": n_runs,
                "result": None,
                "error": None,
            })

        t = threading.Thread(
            target=_run_batch_thread,
            args=(cfg, n_runs),
            daemon=True,
        )
        t.start()

        return (
            True,                           # button disabled
            {"display": "block"},           # progress bar visible
            0,                              # progress value
            f"Running 0 / {n_runs} simulations…",
            False,                          # interval enabled
            "",                             # clear status
        )

    # --- Interval polling: update progress bar; render results when done ---
    @app.callback(
        Output("batch-summary-cards", "children"),
        Output("batch-duration-hist", "figure"),
        Output("batch-delay-cdf", "figure"),
        Output("batch-bunching-chart", "figure"),
        Output("batch-station-bar", "figure"),
        Output("batch-results-table", "children"),
        Output("batch-run-btn", "disabled", allow_duplicate=True),
        Output("batch-progress-container", "style", allow_duplicate=True),
        Output("batch-progress-bar", "value", allow_duplicate=True),
        Output("batch-progress-label", "children", allow_duplicate=True),
        Output("batch-poll-interval", "disabled", allow_duplicate=True),
        Output("batch-status", "children", allow_duplicate=True),
        Input("batch-poll-interval", "n_intervals"),
        prevent_initial_call=True,
    )
    def poll_progress(n_intervals):
        with _BATCH_LOCK:
            state = dict(_BATCH_STATE)

        n_runs = state["total"] or 1
        completed = state["progress"]
        pct = round(completed / n_runs * 100)

        if state["error"]:
            return (
                no_update, no_update, no_update, no_update, no_update, no_update,
                False, {"display": "none"}, 0, "",
                True,
                f"Error: {state['error']}",
            )

        if state["running"] or state["result"] is None:
            return (
                no_update, no_update, no_update, no_update, no_update, no_update,
                no_update, no_update, pct,
                f"Running {completed} / {n_runs} simulations…",
                no_update, no_update,
            )

        # ---- Results ready — render everything ----
        return _render_results(state["result"])

    # ---- Helper: render all chart outputs from a completed BatchResult ----
    def _render_results(batch: BatchResult):
        from analysis.metrics import SCHEDULED_TRIP_SEC
        import statistics as _stat

        agg = batch.aggregate()
        branches = batch.config.branches

        all_mean_mins = [
            r["trip_duration_mean_sec"] / 60
            for r in batch.run_summaries
            if r.get("trip_duration_mean_sec")
        ]
        all_p90_mins = [
            r["trip_duration_p90_sec"] / 60
            for r in batch.run_summaries
            if r.get("trip_duration_p90_sec")
        ]

        td = agg["trip_duration"]
        bk = agg["breakdowns"]

        def _card(title, value, color="primary"):
            return dbc.Col(
                dbc.Card([
                    dbc.CardBody([
                        html.P(title, className="card-text text-muted small mb-1"),
                        html.H4(value, className=f"text-{color} mb-0"),
                    ])
                ], className="h-100"),
                width=3,
            )

        mean_min = f"{td['mean_of_means_sec']/60:.1f} min" if td.get("mean_of_means_sec") else "—"
        p90_min  = f"{td['p90_of_p90s_sec']/60:.1f} min"  if td.get("p90_of_p90s_sec")  else "—"
        worst_min = f"{td['worst_trip_sec']/60:.1f} min"   if td.get("worst_trip_sec")    else "—"
        bd_per_run = f"{bk['mean_per_run']:.1f}"

        cards = [
            _card("Mean trip time (p50 of means)", mean_min, "primary"),
            _card("p90 trip time", p90_min, "warning"),
            _card("Worst single trip", worst_min, "danger"),
            _card("Breakdowns / run", bd_per_run, "secondary"),
        ]

        # Trip duration histogram
        hist_fig = go.Figure()
        hist_fig.add_trace(go.Histogram(
            x=all_mean_mins, nbinsx=30, name="Mean trip (min)",
            marker_color="#3b82f6", opacity=0.8,
        ))
        hist_fig.add_trace(go.Histogram(
            x=all_p90_mins, nbinsx=30, name="p90 trip (min)",
            marker_color="#f97316", opacity=0.6,
        ))
        hist_fig.update_layout(
            title="Trip duration distribution", xaxis_title="Minutes",
            yaxis_title="Runs", barmode="overlay",
            legend=dict(orientation="h", y=1.02, x=0),
            margin=dict(t=50, b=40, l=40, r=20), height=320,
        )

        # Delay CDF
        sched_sec = _stat.mean(SCHEDULED_TRIP_SEC.get(b, 4080) for b in branches)
        delays_min = [
            (r["trip_duration_mean_sec"] - sched_sec) / 60
            for r in batch.run_summaries
            if r.get("trip_duration_mean_sec")
        ]
        delays_sorted = sorted(delays_min)
        n_d = len(delays_sorted)
        cdf_y = [(i + 1) / n_d * 100 for i in range(n_d)]

        cdf_fig = go.Figure()
        cdf_fig.add_trace(go.Scatter(
            x=delays_sorted, y=cdf_y, mode="lines",
            line=dict(color="#10b981", width=2),
            fill="tozeroy", fillcolor="rgba(16,185,129,0.1)", name="CDF",
        ))
        cdf_fig.add_vline(x=0, line_dash="dash", line_color="gray",
                          annotation_text="Schedule", annotation_position="top right")
        cdf_fig.add_vline(x=5, line_dash="dot", line_color="#f97316",
                          annotation_text="5 min late", annotation_position="top right")
        cdf_fig.update_layout(
            title="Delay vs schedule — cumulative",
            xaxis_title="Delay (min, + = late)", yaxis_title="% of runs",
            margin=dict(t=50, b=40, l=40, r=20), height=320,
        )

        # Bunching chart
        bunching_counts = [r.get("bunching_events", 0) for r in batch.run_summaries]
        run_nums = list(range(1, len(bunching_counts) + 1))

        bunch_fig = go.Figure()
        bunch_fig.add_trace(go.Bar(
            x=run_nums, y=bunching_counts,
            marker_color="#8b5cf6", opacity=0.8, name="Bunching events",
        ))
        bunch_fig.update_layout(
            title="Bunching events per run", xaxis_title="Run #",
            yaxis_title="Events", margin=dict(t=50, b=40, l=40, r=20), height=320,
        )

        # Station boardings
        last_result = _run_single_for_station_stats(batch.config)
        s_stats = station_stats(last_result, top_n=10)
        top = s_stats["top_boardings"]
        s_names = [m["station_name"] for m in top]
        board_counts = [m["total_boarded"] for m in top]
        wait_secs = [m["mean_wait_sec"] or 0 for m in top]

        station_fig = go.Figure()
        station_fig.add_trace(go.Bar(
            x=s_names, y=board_counts, name="Total boarded",
            marker_color="#3b82f6", yaxis="y",
        ))
        station_fig.add_trace(go.Scatter(
            x=s_names, y=[w / 60 for w in wait_secs], name="Mean wait (min)",
            mode="lines+markers", marker=dict(color="#ef4444"), yaxis="y2",
        ))
        station_fig.update_layout(
            title="Top 10 stations — boardings & wait time",
            xaxis_tickangle=-35,
            yaxis=dict(title="Boardings"),
            yaxis2=dict(title="Mean wait (min)", overlaying="y", side="right"),
            legend=dict(orientation="h", y=1.02),
            margin=dict(t=60, b=100, l=50, r=60), height=350,
        )

        # Per-run results table
        rows_data = batch_summary_table(batch)
        table = dbc.Table(
            [
                html.Thead(html.Tr([
                    html.Th("Run"), html.Th("Trains"),
                    html.Th("Mean (min)"), html.Th("p50 (min)"),
                    html.Th("p90 (min)"), html.Th("Worst (min)"),
                    html.Th("Breakdowns"), html.Th("Boarded"), html.Th("Stranded"),
                ])),
                html.Tbody([
                    html.Tr([
                        html.Td(r["run"]), html.Td(r["n_trains"]),
                        html.Td(f"{r['mean_trip_min']:.1f}" if r["mean_trip_min"] else "—"),
                        html.Td(f"{r['p50_trip_min']:.1f}" if r["p50_trip_min"] else "—"),
                        html.Td(f"{r['p90_trip_min']:.1f}" if r["p90_trip_min"] else "—"),
                        html.Td(f"{r['worst_trip_min']:.1f}" if r["worst_trip_min"] else "—"),
                        html.Td(r["breakdowns"]),
                        html.Td(r["total_boarded"]),
                        html.Td(r["total_stranded"]),
                    ])
                    for r in rows_data
                ]),
            ],
            bordered=True, hover=True, responsive=True, size="sm",
            className="mt-2",
            style={"maxHeight": "340px", "overflowY": "auto", "display": "block"},
        )

        status = (
            f"Completed {batch.n_runs} runs in {batch.wall_time_sec:.1f}s "
            f"({batch.wall_time_sec / batch.n_runs * 1000:.0f}ms/run)"
        )

        return (
            cards, hist_fig, cdf_fig, bunch_fig, station_fig, table,
            False,               # button re-enabled
            {"display": "none"}, # progress bar hidden
            100,                 # progress value
            "Done",
            True,                # interval disabled
            status,
        )


def _run_single_for_station_stats(cfg: SimConfig):
    """Run one simulation to get station-level stats for the chart."""
    single_cfg = SimConfig(
        branches=cfg.branches,
        directions=cfg.directions,
        day_type=cfg.day_type,
        start_time=cfg.start_time,
        duration_min=cfg.duration_min,
        end_station_id=cfg.end_station_id,
        seed=42,
    )
    return single_run(single_cfg)


def _batch_route_badge(origin_id: str | None, dest_id: str | None) -> html.Div:
    """Show inferred branch/direction badge when both origin and dest are set."""
    if not origin_id or not dest_id:
        return html.Div()
    route_info = _infer_route(origin_id, dest_id)
    if route_info is None:
        return dbc.Alert("No direct route found.", color="danger",
                         className="py-1 small")
    branch, direction = route_info
    dir_label = "Inbound" if direction == 1 else "Outbound"
    return html.Div([
        html.Span("Route: ", className="text-muted small"),
        dbc.Badge(branch, color="success", className="me-1"),
        dbc.Badge(dir_label, color="secondary"),
    ], className="mb-1")
