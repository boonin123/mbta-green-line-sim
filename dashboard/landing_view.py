"""
dashboard/landing_view.py
-------------------------
Landing page for the MBTA Green Line Simulation.

Shows an animated schematic of the Green Line branches and provides
entry points to the two main views: Batch Analysis and Ride the Train.
"""

from __future__ import annotations

import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import dcc, html

from sim.network import Network

_NET = Network(branches=["Green-B", "Green-C", "Green-D", "Green-E"])


# ---------------------------------------------------------------------------
# Branch colour palette
# ---------------------------------------------------------------------------

_BRANCH_COLORS = {
    "Green-B": "#22c55e",
    "Green-C": "#16a34a",
    "Green-D": "#15803d",
    "Green-E": "#86efac",
}

_BRANCH_LABELS = {
    "Green-B": "B — Boston College",
    "Green-C": "C — Cleveland Circle",
    "Green-D": "D — Riverside / Union Sq",
    "Green-E": "E — Heath St / Medford",
}


# ---------------------------------------------------------------------------
# Schematic map figure (real lat/lon, styled for the landing page)
# ---------------------------------------------------------------------------

def _build_schematic() -> go.Figure:
    fig = go.Figure()

    # Draw each branch route line + stops
    for branch in ["Green-B", "Green-C", "Green-D", "Green-E"]:
        route = _NET.get_route(branch, 0)  # outbound = terminus direction
        lats = [_NET.stations[s].lat for s in route if s in _NET.stations]
        lons = [_NET.stations[s].lon for s in route if s in _NET.stations]
        names = [_NET.stations[s].name for s in route if s in _NET.stations]
        color = _BRANCH_COLORS[branch]

        fig.add_trace(go.Scattermapbox(
            lat=lats, lon=lons, mode="lines",
            line=dict(color=color, width=4),
            name=_BRANCH_LABELS[branch],
            hoverinfo="skip",
            showlegend=True,
        ))

        fig.add_trace(go.Scattermapbox(
            lat=lats, lon=lons, mode="markers",
            marker=dict(size=6, color=color, opacity=0.7),
            text=names,
            hovertemplate="%{text}<extra></extra>",
            showlegend=False,
        ))

    # Highlight key merge / trunk stations
    key_stops = {
        "place-kencl": ("Kenmore", "#f59e0b", 14),
        "place-coecl": ("Copley", "#f59e0b", 14),
        "place-pktrm": ("Park St", "#ef4444", 16),
        "place-gover": ("Govt Center", "#ef4444", 14),
    }
    for sid, (label, color, size) in key_stops.items():
        if sid in _NET.stations:
            rec = _NET.stations[sid]
            fig.add_trace(go.Scattermapbox(
                lat=[rec.lat], lon=[rec.lon], mode="markers+text",
                marker=dict(size=size, color=color),
                text=[label],
                textposition="top right",
                textfont=dict(size=10, color="white"),
                hovertemplate=f"{label}<extra></extra>",
                showlegend=False,
            ))

    all_lats = [s.lat for s in _NET.stations.values()]
    all_lons = [s.lon for s in _NET.stations.values()]
    center = {
        "lat": (min(all_lats) + max(all_lats)) / 2,
        "lon": (min(all_lons) + max(all_lons)) / 2,
    }

    fig.update_layout(
        mapbox=dict(
            style="open-street-map",
            center=center,
            zoom=11,
        ),
        margin=dict(l=0, r=0, t=0, b=0),
        showlegend=True,
        legend=dict(
            bgcolor="rgba(0,0,0,0.6)",
            font=dict(color="white", size=11),
            x=0.01, y=0.99,
            xanchor="left", yanchor="top",
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        uirevision="landing",
    )
    return fig


# ---------------------------------------------------------------------------
# Stats bar (static facts about the simulation/network)
# ---------------------------------------------------------------------------

def _stats_bar() -> dbc.Row:
    n_stations = len(_NET.stations)
    n_branches = len(_NET.active_branches)

    facts = [
        ("🚉", str(n_stations), "stations"),
        ("🌿", str(n_branches), "branches"),
        ("🚃", "176 pax", "per car"),
        ("⏱️", "~7 min", "peak headway"),
        ("📍", "~12 mi", "surface running"),
    ]
    cols = []
    for icon, value, label in facts:
        cols.append(dbc.Col(html.Div([
            html.Div(icon, style={"fontSize": "1.6rem"}),
            html.Div(value, className="fw-bold fs-5"),
            html.Div(label, className="text-muted small"),
        ], className="text-center py-2"), xs=6, sm=4, md=2))

    return dbc.Row(cols, className="justify-content-center my-4 g-2")


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def layout() -> html.Div:
    schematic = _build_schematic()

    return html.Div([
        # Hero section
        html.Div([
            dbc.Container([
                dbc.Row([
                    dbc.Col([
                        html.Div("🚃", className="train-bounce",
                                 style={"fontSize": "4rem"}),
                        html.H1("MBTA Green Line Simulation",
                                className="display-5 fw-bold text-white mt-2"),
                        html.P(
                            "Explore Boston's most delay-prone transit line through "
                            "discrete-event simulation. Run thousands of trips, ride "
                            "the train interactively, and discover where — and why — "
                            "delays compound.",
                            className="lead text-white-50 mb-4",
                        ),
                        dbc.Row([
                            dbc.Col(
                                dbc.Button([
                                    html.Span("📊  ", style={"fontSize": "1.1rem"}),
                                    "Batch Analysis",
                                ], href="/batch", color="light", size="lg",
                                   className="w-100 fw-bold"),
                                xs=12, sm=6, className="mb-2",
                            ),
                            dbc.Col(
                                dbc.Button([
                                    html.Span("🚃  ", style={"fontSize": "1.1rem"}),
                                    "Ride the Train",
                                ], href="/map", color="success", size="lg",
                                   className="w-100 fw-bold"),
                                xs=12, sm=6, className="mb-2",
                            ),
                        ]),
                    ], md=5, className="py-5"),

                    dbc.Col([
                        dcc.Graph(
                            figure=schematic,
                            style={"height": "420px", "borderRadius": "12px",
                                   "overflow": "hidden"},
                            config={"scrollZoom": False, "displayModeBar": False},
                        ),
                    ], md=7, className="py-4"),
                ], align="center"),
            ], fluid=True, className="px-4"),
        ], style={
            "background": "linear-gradient(135deg, #14532d 0%, #166534 50%, #15803d 100%)",
            "minHeight": "520px",
        }),

        # Stats bar
        dbc.Container([
            _stats_bar(),
            html.Hr(),
        ], fluid=True, className="px-4"),

        # Feature cards
        dbc.Container([
            dbc.Row([
                dbc.Col(dbc.Card([
                    dbc.CardBody([
                        html.Div("📊", style={"fontSize": "2.5rem"}),
                        html.H5("Batch Analysis", className="fw-bold mt-2"),
                        html.P(
                            "Run up to 2,000 simulations and explore delay distributions, "
                            "station wait times, bunching frequency, and on-time performance "
                            "across any origin–destination pair.",
                            className="text-muted small",
                        ),
                        dbc.Button("Open", href="/batch", color="success",
                                   outline=True, className="mt-2"),
                    ])
                ], className="h-100 text-center py-3"), md=4, className="mb-3"),

                dbc.Col(dbc.Card([
                    dbc.CardBody([
                        html.Div("🚃", style={"fontSize": "2.5rem"}),
                        html.H5("Ride the Train", className="fw-bold mt-2"),
                        html.P(
                            "Pick your origin, destination, and target arrival time. "
                            "Watch your train move stop-by-stop and see how your trip "
                            "stacks up against 30 simulated runs of the same journey.",
                            className="text-muted small",
                        ),
                        dbc.Button("Ride", href="/map", color="success",
                                   className="mt-2"),
                    ])
                ], className="h-100 text-center py-3"), md=4, className="mb-3"),

                dbc.Col(dbc.Card([
                    dbc.CardBody([
                        html.Div("⚡", style={"fontSize": "2.5rem"}),
                        html.H5("Powered by SimPy", className="fw-bold mt-2"),
                        html.P(
                            "Built on MBTA GTFS data for realistic headway, dwell, and "
                            "breakdown distributions. Trunk merge points, passenger coupling, "
                            "and branch-level calibration — not just random noise.",
                            className="text-muted small",
                        ),
                    ])
                ], className="h-100 text-center py-3"), md=4, className="mb-3"),
            ], className="g-3"),
        ], fluid=True, className="px-4 py-3"),

    ])


def register_callbacks(app):
    # No callbacks needed for the static landing page
    pass
