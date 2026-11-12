"""Half-court drawing and zone heat maps (Plotly).

Coordinates are the play-by-play ``x_legacy`` / ``y_legacy`` system: tenths of a foot,
rim at the origin, +y toward half court. Zone areas are rasterized with the same rule the
metrics use (``cv_zone`` in :mod:`courtvision.db`), so the picture can't disagree with the
numbers.
"""

from __future__ import annotations

import math

import numpy as np
import plotly.graph_objects as go

ZONE_LABELS = {
    "RA": "Restricted area",
    "PAINT": "Paint (non-RA)",
    "MID": "Mid-range",
    "C3": "Corner 3",
    "ATB3": "Above-the-break 3",
}
ZONE_KEY = {"RA": "ra", "PAINT": "pnt", "MID": "mid", "C3": "c3", "ATB3": "atb3"}

X_MIN, X_MAX, Y_MIN, Y_MAX = -250, 250, -47.5, 300
STEP = 5


def zone_of(x: float, y: float) -> str | None:
    """Zone of a court point, with 2 vs 3 from the court geometry (not a recorded shot)."""
    three = (abs(x) >= 220 and y <= 87) or (math.hypot(x, y) >= 237.5 and y > 87)
    if three:
        if y > 417.5:
            return None
        return "C3" if abs(x) >= 220 and y <= 87 else "ATB3"
    if math.hypot(x, y) < 40:
        return "RA"
    if abs(x) <= 80 and y <= 138:
        return "PAINT"
    return "MID"


_XS = np.arange(X_MIN + STEP / 2, X_MAX, STEP)
_YS = np.arange(Y_MIN + STEP / 2, Y_MAX, STEP)
_GRID = [[zone_of(x, y) for x in _XS] for y in _YS]


def court_shapes(color: str = "rgba(120,120,120,0.9)") -> list[dict]:
    line = {"color": color, "width": 1.5}
    shapes = [
        # hoop, backboard
        {"type": "circle", "x0": -7.5, "y0": -7.5, "x1": 7.5, "y1": 7.5, "line": line},
        {"type": "line", "x0": -30, "y0": -7.5, "x1": 30, "y1": -7.5, "line": line},
        # lane and free-throw circle
        {"type": "rect", "x0": -80, "y0": -47.5, "x1": 80, "y1": 142.5, "line": line},
        {"type": "circle", "x0": -60, "y0": 82.5, "x1": 60, "y1": 202.5, "line": line},
        # restricted area arc
        {"type": "path", "path": _arc(40, 0, 180), "line": line},
        # three-point line: corners and arc
        {"type": "line", "x0": -220, "y0": -47.5, "x1": -220, "y1": 92.5, "line": line},
        {"type": "line", "x0": 220, "y0": -47.5, "x1": 220, "y1": 92.5, "line": line},
        {"type": "path", "path": _arc(237.5, 22.1, 157.9), "line": line},
        # baseline
        {"type": "rect", "x0": -250, "y0": -47.5, "x1": 250, "y1": 422.5, "line": line},
    ]
    return shapes


def _arc(r: float, a0: float, a1: float, n: int = 60) -> str:
    pts = [
        (r * math.cos(math.radians(a)), r * math.sin(math.radians(a)))
        for a in np.linspace(a0, a1, n)
    ]
    return "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def zone_heatmap(
    values: dict[str, float],
    hover: dict[str, str],
    title: str,
    zmax: float | None = None,
    colorbar_title: str = "",
) -> go.Figure:
    """Color each zone by ``values[zone]`` (diverging around 0)."""
    z = [[values.get(c) if c else None for c in row] for row in _GRID]
    text = [[hover.get(c, "") if c else "" for c in row] for row in _GRID]
    finite = [abs(v) for v in values.values() if v is not None and not math.isnan(v)]
    zmax = zmax or (max(finite) if finite else 1.0) or 1.0
    fig = go.Figure(
        go.Heatmap(
            x=_XS,
            y=_YS,
            z=z,
            text=text,
            hoverinfo="text",
            zmid=0,
            zmin=-zmax,
            zmax=zmax,
            colorscale="RdBu_r",
            showscale=True,
            colorbar={"title": colorbar_title, "thickness": 12},
        )  # fmt: skip
    )
    fig.update_layout(
        title=title,
        shapes=court_shapes(),
        xaxis={"visible": False, "range": [X_MIN, X_MAX], "scaleanchor": "y"},
        yaxis={"visible": False, "range": [Y_MIN, Y_MAX]},
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        height=420,
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def radar(axes: list[str], series: dict[str, list[float | None]]) -> go.Figure:
    """Ratings on 0-100 axes; a dashed ring marks league average (50)."""
    fig = go.Figure()
    closed = axes + axes[:1]
    fig.add_trace(
        go.Scatterpolar(
            r=[50] * len(closed),
            theta=closed,
            mode="lines",
            name="League average",
            line={"dash": "dash", "color": "gray"},
            hoverinfo="skip",
        )  # fmt: skip
    )
    for name, vals in series.items():
        v = [x if x is not None and not (isinstance(x, float) and math.isnan(x)) else None
             for x in vals]  # fmt: skip
        fig.add_trace(
            go.Scatterpolar(
                r=v + v[:1],
                theta=closed,
                fill="toself",
                name=name,
                connectgaps=False,
                hovertemplate="%{theta}: %{r:.0f}<extra>" + name + "</extra>",
            )  # fmt: skip
        )
    fig.update_layout(
        polar={"radialaxis": {"range": [0, 100], "tickvals": [25, 50, 75]}},
        margin={"l": 40, "r": 40, "t": 30, "b": 30},
        height=380,
        legend={"orientation": "h"},
    )
    return fig
