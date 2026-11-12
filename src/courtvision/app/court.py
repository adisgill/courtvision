"""Half-court zone chart, shot-mix bars and the radar (Plotly).

Coordinates are the play-by-play ``x_legacy`` / ``y_legacy`` system: tenths of a foot,
rim at the origin, +y toward half court. Zone shapes follow the rule the metrics use
(``cv_zone`` in :mod:`courtvision.db`; :func:`zone_of` here), except that the paint is
drawn up to the painted lane line (14.25 ft) where the rule cuts at 13.8 ft.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import plotly.graph_objects as go

ZONES = ["RA", "PAINT", "MID", "C3", "ATB3"]
ZONE_LABELS = {
    "RA": "Restricted area",
    "PAINT": "Paint (non-RA)",
    "MID": "Mid-range",
    "C3": "Corner 3",
    "ATB3": "Above-the-break 3",
}
ZONE_KEY = {"RA": "ra", "PAINT": "pnt", "MID": "mid", "C3": "c3", "ATB3": "atb3"}

X_MIN, X_MAX, Y_MIN, Y_TOP = -250, 250, -47.5, 300
FG_SCALE = 8.0  # percentage points of FG% at full color
MIN_FGA = 15  # zones with fewer shots are drawn neutral and say so

# Diverging pair (cold blue, neutral gray, hot red) and ink, per theme.
THEME = {
    "light": {
        "cold": "#2a78d6", "mid": "#f0efec", "hot": "#e34948",
        "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "lines": "rgba(11,11,11,0.55)", "bar": "#a8a69e",
    },
    "dark": {
        "cold": "#3987e5", "mid": "#383835", "hot": "#e66767",
        "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
        "lines": "rgba(240,239,236,0.55)", "bar": "#6b6a64",
    },
}  # fmt: skip


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


# ---- geometry -----------------------------------------------------------------------------

_ARC_R = 237.5
_ARC_A = math.degrees(math.asin(87 / _ARC_R))  # where the arc meets the corner lines


def _arc(r: float, a0: float, a1: float, n: int = 60) -> list[tuple[float, float]]:
    return [
        (r * math.cos(math.radians(a)), r * math.sin(math.radians(a)))
        for a in np.linspace(a0, a1, n)
    ]


def _path(pts: list[tuple[float, float]]) -> str:
    return "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def zone_polygons() -> dict[str, list[list[tuple[float, float]]]]:
    """Non-overlapping outlines of the five zones (holes cut with zero-width slits)."""
    inner_arc = _arc(_ARC_R, 180 - _ARC_A, _ARC_A)  # left to right over the top
    x87 = _ARC_R * math.cos(math.radians(_ARC_A))
    ra = _arc(40, -90, 270, 72)  # full circle starting at its bottom
    return {
        "ATB3": [
            [(X_MIN, 87), (X_MIN, Y_TOP), (X_MAX, Y_TOP), (X_MAX, 87), (x87, 87)]
            + inner_arc[::-1]
            + [(-x87, 87)]
        ],
        "C3": [
            [(X_MIN, Y_MIN), (-220, Y_MIN), (-220, 87), (X_MIN, 87)],
            [(220, Y_MIN), (X_MAX, Y_MIN), (X_MAX, 87), (220, 87)],
        ],
        "MID": [
            [(-220, Y_MIN), (-220, 87)]
            + inner_arc
            + [(220, 87), (220, Y_MIN), (80, Y_MIN), (80, 142.5), (-80, 142.5), (-80, Y_MIN)]
        ],
        "PAINT": [
            [(-80, Y_MIN), (0, Y_MIN), *ra, (0, Y_MIN), (80, Y_MIN), (80, 142.5), (-80, 142.5)]
        ],
        "RA": [ra],
    }


def court_lines(color: str) -> list[dict]:
    line = {"color": color, "width": 1.5}
    top_arc = _arc(_ARC_R, _ARC_A, 180 - _ARC_A)
    return [
        {"type": "circle", "x0": -7.5, "y0": -7.5, "x1": 7.5, "y1": 7.5, "line": line},
        {"type": "line", "x0": -30, "y0": -7.5, "x1": 30, "y1": -7.5, "line": line},
        {"type": "rect", "x0": -80, "y0": Y_MIN, "x1": 80, "y1": 142.5, "line": line},
        {"type": "circle", "x0": -60, "y0": 82.5, "x1": 60, "y1": 202.5, "line": line},
        {"type": "path", "path": _path(_arc(40, 0, 180)), "line": line},
        {"type": "line", "x0": -220, "y0": Y_MIN, "x1": -220, "y1": 87, "line": line},
        {"type": "line", "x0": 220, "y0": Y_MIN, "x1": 220, "y1": 87, "line": line},
        {"type": "path", "path": _path(top_arc), "line": line},
        {"type": "rect", "x0": X_MIN, "y0": Y_MIN, "x1": X_MAX, "y1": Y_TOP + 5, "line": line},
    ]


# ---- color --------------------------------------------------------------------------------


def _rgb(hex_: str) -> tuple[int, int, int]:
    h = hex_.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    r, g, bl = (round(x + (y - x) * t) for x, y in zip(ra, rb, strict=True))
    return f"#{r:02x}{g:02x}{bl:02x}"


def heat_color(diff_pp: float | None, mode: str) -> str:
    """Diverging fill for an FG% difference in percentage points (gray when unknown)."""
    t = THEME[mode]
    if diff_pp is None or math.isnan(diff_pp):
        return t["mid"]
    s = max(-1.0, min(1.0, diff_pp / FG_SCALE))
    return _mix(t["mid"], t["hot"] if s >= 0 else t["cold"], abs(s))


def _text_on(fill: str) -> str:
    def lin(c: int) -> float:
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in _rgb(fill))
    return "#0b0b0b" if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.3 else "#ffffff"


# ---- charts -------------------------------------------------------------------------------

_LABEL_AT = {"RA": (0, 24), "PAINT": (0, 112), "MID": (-150, 112), "ATB3": (0, 268)}


def zone_court(prof: pd.DataFrame, mode: str = "light", allowed: bool = False) -> go.Figure:
    """Each zone filled by FG% vs league (red hotter, blue colder) and labelled with it.

    ``prof`` is :func:`courtvision.app.data.zone_profile` output: one row per zone with
    ``fga``, ``unit_fg_pct``, ``fg_pct`` (league), ``unit_share``, ``share`` (league).
    """
    t = THEME[mode]
    p = prof.set_index("zone")
    fig = go.Figure()
    shots = "opponent shots" if allowed else "shots"
    for zone, polys in zone_polygons().items():
        r = p.loc[zone] if zone in p.index else None
        enough = r is not None and r.fga >= MIN_FGA
        diff = 100 * (r.unit_fg_pct - r.fg_pct) if enough else None
        fill = heat_color(diff, mode)
        xs: list[float | None] = []
        ys: list[float | None] = []
        for poly in polys:
            xs += [x for x, _ in poly] + [poly[0][0], None]
            ys += [y for _, y in poly] + [poly[0][1], None]
        hover = (
            f"<b>{ZONE_LABELS[zone]}</b><br>{int(r.fga)} {shots}, "
            f"{r.unit_share:.0%} of all (league {r.share:.0%})<br>"
            f"FG {r.unit_fg_pct:.1%} vs league {r.fg_pct:.1%}"
            if r is not None and r.fga > 0
            else f"<b>{ZONE_LABELS[zone]}</b><br>no {shots}"
        )
        fig.add_trace(
            go.Scatter(
                x=xs, y=ys, mode="lines", fill="toself", fillcolor=fill,
                line={"width": 0}, hoveron="fills", hoverinfo="text", text=hover,
                name=ZONE_LABELS[zone], showlegend=False,
            )
        )  # fmt: skip
        label = (
            f"<b>{r.unit_fg_pct:.0%}</b><br>{diff:+.1f}"
            if enough
            else f"<b>–</b><br>{int(r.fga) if r is not None else 0} {shots}"
        )
        if zone == "C3":
            fig.add_annotation(
                x=0, y=-72, showarrow=False, font={"color": t["ink2"], "size": 13},
                text=(f"Corner 3s (both corners): <b>{r.unit_fg_pct:.0%}</b>, "
                      f"{diff:+.1f}" if enough
                      else f"Corner 3s: {int(r.fga) if r is not None else 0} {shots}"),
            )  # fmt: skip
        else:
            x, y = _LABEL_AT[zone]
            fig.add_trace(
                go.Scatter(
                    x=[x], y=[y], mode="text", text=[label], hoverinfo="skip",
                    textfont={"color": _text_on(fill), "size": 13}, showlegend=False,
                )
            )  # fmt: skip
    fig.update_layout(
        shapes=court_lines(t["lines"]),
        xaxis={"visible": False, "range": [X_MIN - 5, X_MAX + 5], "fixedrange": True},
        yaxis={"visible": False, "range": [-95, Y_TOP + 8], "fixedrange": True,
               "scaleanchor": "x"},
        margin={"l": 0, "r": 0, "t": 10, "b": 0},
        height=470,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )  # fmt: skip
    return fig


def shot_mix(prof: pd.DataFrame, who: str, mode: str = "light") -> go.Figure:
    """Share of the unit's shots by zone (bars) against league average (tick)."""
    t = THEME[mode]
    p = prof.set_index("zone").reindex(ZONES)
    unit = 100 * p.unit_share.fillna(0)
    league = 100 * p.share
    # numbers live in the row labels, so they never collide with the league tick
    names = [
        f"{ZONE_LABELS[z]}<br><span style='color:{t['muted']}'>{u:.0f}% · league {lg:.0f}%</span>"
        for z, u, lg in zip(ZONES, unit, league, strict=True)
    ]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            y=names, x=unit, orientation="h", name=who, marker_color=t["bar"],
            marker_cornerradius=4,
            hovertemplate=f"%{{x:.1f}}% of shots<extra>{who}</extra>",
        )
    )  # fmt: skip
    fig.add_trace(
        go.Scatter(
            y=names, x=league, mode="markers", name="League average",
            marker={"symbol": "line-ns", "size": 26, "line": {"width": 3, "color": t["ink"]}},
            hovertemplate="league %{x:.1f}%<extra></extra>",
        )
    )  # fmt: skip
    top = max(unit.max(), league.max()) * 1.08
    fig.update_layout(
        xaxis={"range": [0, top], "ticksuffix": "%", "showgrid": False, "fixedrange": True},
        yaxis={"autorange": "reversed", "fixedrange": True, "ticklabelstandoff": 6},
        bargap=0.5,
        legend={"orientation": "h", "y": -0.12},
        margin={"l": 0, "r": 10, "t": 10, "b": 0},
        height=330,
    )  # fmt: skip
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
        polar={
            "radialaxis": {"range": [0, 100], "tickvals": [25, 50, 75]},
            "bgcolor": "rgba(0,0,0,0)",
        },  # fmt: skip
        margin={"l": 40, "r": 40, "t": 30, "b": 30},
        height=380,
        legend={"orientation": "h"},
    )
    return fig
