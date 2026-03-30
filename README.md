# MBTA Green Line Simulation

A discrete-event simulation of the MBTA Green Line — the most delay-prone transit line in Boston — built with SimPy and visualized through an interactive Dash dashboard.

## Features

- **Batch Analysis**: Run 1000+ simulations and explore delay distributions, expected vs. actual travel times, bunching frequency, and more
- **Interactive Map ("Ride the Train")**: Simulate a single trip — pick your origin, destination, and departure time, then watch your train move in real time

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # add your MBTA API key
```

## Running

```bash
# Launch dashboard
python dashboard/app.py

# CLI batch run
python -m sim.runner --mode batch --runs 1000 --branch B --day monday

# CLI single run
python -m sim.runner --mode single --origin "Park Street" --destination "Boston College" --time "08:00" --day monday
```

## Data Sources

- [MBTA GTFS](https://www.mbta.com/developers/gtfs) — scheduled times, stop coordinates
- [MBTA V3 API](https://www.mbta.com/developers/v3-api) — real-time data, used for distribution fitting

## Tech Stack

- **Simulation**: Python + SimPy
- **Dashboard**: Dash (Plotly)
- **Data**: MBTA GTFS + V3 API
