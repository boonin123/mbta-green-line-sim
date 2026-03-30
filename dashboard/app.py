"""
dashboard/app.py
----------------
MBTA Green Line Simulation — Dash application entry point.

Two views:
  /batch  — Batch I/O analysis (run 10–2000 simulations, view distributions)
  /map    — Interactive single-run animated map ("Ride the Train")

Run:
    python dashboard/app.py
    # Opens at http://localhost:8050
"""

from __future__ import annotations

import os
import sys

# Ensure project root is on the path when running as `python dashboard/app.py`
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, dcc, html

# ---------------------------------------------------------------------------
# App initialisation
# IMPORTANT: app must be created BEFORE importing view modules, so that
# @callback decorators in those modules register against this app instance.
# ---------------------------------------------------------------------------

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    suppress_callback_exceptions=True,
    title="MBTA Green Line Sim",
)
server = app.server  # expose for production WSGI servers

# Import views AFTER app is created, then register their callbacks
from dashboard import batch_view, landing_view, map_view  # noqa: E402

batch_view.register_callbacks(app)
landing_view.register_callbacks(app)
map_view.register_callbacks(app)


# ---------------------------------------------------------------------------
# Top navigation
# ---------------------------------------------------------------------------

NAVBAR = dbc.Navbar(
    dbc.Container([
        dbc.NavbarBrand(
            [
                html.Span("🚃 ", style={"fontSize": "1.2rem"}),
                "MBTA Green Line Simulation",
            ],
            className="fw-bold",
        ),
        dbc.Nav([
            dbc.NavItem(dbc.NavLink("Home", href="/", active="exact")),
            dbc.NavItem(dbc.NavLink("Batch Analysis", href="/batch", active="exact")),
            dbc.NavItem(dbc.NavLink("Ride the Train", href="/map", active="exact")),
        ], navbar=True, className="ms-auto"),
    ], fluid=True),
    color="success",
    dark=True,
    className="mb-0",
)


# ---------------------------------------------------------------------------
# Root layout
# ---------------------------------------------------------------------------

app.layout = html.Div([
    dcc.Location(id="url", refresh=False),
    NAVBAR,
    html.Div(id="page-content"),
])


# ---------------------------------------------------------------------------
# Page routing
# ---------------------------------------------------------------------------

@app.callback(
    Output("page-content", "children"),
    Input("url", "pathname"),
)
def render_page(pathname: str):
    if pathname in ("/", ""):
        return landing_view.layout()
    if pathname == "/batch":
        return batch_view.layout()
    if pathname == "/map":
        return map_view.layout()
    return dbc.Container([
        html.H3("404 — Page not found", className="mt-5"),
        html.P(f"No page at {pathname!r}"),
        dbc.Button("Go Home", href="/", color="primary"),
    ], className="text-center")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8050))
    debug = os.environ.get("DASH_DEBUG", "true").lower() == "true"
    print(f"Starting MBTA Green Line dashboard at http://localhost:{port}")
    app.run(debug=debug, port=port)
