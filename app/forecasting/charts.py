"""Chart rendering for the engine.

Renders Prophet's trend decomposition as a Plotly figure (interactive zoom/pan/hover,
not a static image) rather than matplotlib. The figure is returned as a plain,
JSON-safe dict -- {"data": [...], "layout": {...}} -- so the API layer can put it
straight in a Pydantic response, and the dashboard renders it client-side with
`Plotly.newPlot(container, fig.data, fig.layout)` using the same self-hosted
`plotly-cartesian.min.js` bundle the funding-gap chart uses (see
`app/static/dashboard/index.html`) -- no server-side image rendering, no matplotlib
output at all here anymore. (The funding-gap chart itself is built entirely
client-side straight from `ForecastResponse.forecast`, so there is nothing to render
for it here -- see notes.txt for why this replaced the original hand-rolled-SVG and
matplotlib versions.)
"""

from __future__ import annotations

import json

import pandas as pd
from prophet import Prophet
from prophet.plot import plot_components_plotly


def render_trend_decomposition(model: Prophet, forecast: pd.DataFrame) -> dict:
    """Prophet's own component decomposition: trend, weekly/yearly seasonality, holidays.

    `Figure.to_json()`/`to_plotly_json()` embed numpy arrays (and, depending on plotly
    version, a compact base64 "typed array" encoding for numeric series) -- routing
    through `to_json()` then `json.loads()` guarantees the result is plain JSON-safe
    Python (str/int/float/list/dict) before it ever reaches Pydantic/FastAPI, rather
    than relying on FastAPI's default encoder to happen to understand whatever numpy/
    plotly-internal types `to_plotly_json()` would hand back directly.
    """
    fig = plot_components_plotly(model, forecast)
    return json.loads(fig.to_json())
