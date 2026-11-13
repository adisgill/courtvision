"""Shared chart builders for the app (Plotly), one visual language for every view.

Color rule everywhere: **blue = good for the team or player being looked at, red = bad,
gray = league average**. Identity (one team vs another) uses the categorical slots.
Every figure is themed for Streamlit's light or dark mode via ``mode``.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import plotly.graph_objects as go

THEME = {
    "light": {"good": "#2a78d6", "bad": "#e34948", "mid": "#f0efec", "ink": "#0b0b0b",
              "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9", "bar": "#a8a69e",
              "series": ["#2a78d6", "#eb6834", "#1baf7a"]},
    "dark": {"good": "#3987e5", "bad": "#e66767", "mid": "#383835", "ink": "#f0efec",
             "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a", "bar": "#6b6a64",
             "series": ["#3987e5", "#d95926", "#199e70"]},
}  # fmt: skip
RISK = {"low": "#0ca30c", "medium": "#fab219", "high": "#d03b3b"}


def count_range(lo, hi):
    """The whole numbers an 80% range covers, as (low, high): stats are counts, so a range
    of 0.4-3.8 means 1 to 3, the set its coverage was calibrated and is scored on."""
    lo, hi = np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)
    return np.maximum(np.ceil(lo - 1e-9), 0.0), np.floor(hi + 1e-9)


def range_text(lo, hi) -> str:
    a, b = count_range(lo, hi)
    return f"{a:.0f}-{b:.0f}"


def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgba(h: str, alpha: float) -> str:
    r, g, b = _rgb(h)
    return f"rgba({r},{g},{b},{alpha})"


def mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    r, g, bl = (round(x + (y - x) * t) for x, y in zip(ra, rb, strict=True))
    return f"#{r:02x}{g:02x}{bl:02x}"


def good_bad(value: float | None, scale: float, mode: str) -> str:
    """Diverging fill: blue above zero, red below, gray at zero or unknown."""
    t = THEME[mode]
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return t["mid"]
    s = max(-1.0, min(1.0, value / scale))
    return mix(t["mid"], t["good"] if s >= 0 else t["bad"], abs(s))


def scale(mode: str) -> list[list]:
    """Diverging scale: red - gray - blue, with intermediate steps so mid values stay
    readable (the gray band is wide enough that 'about average' looks it)."""
    t = THEME[mode]
    return [[0, t["bad"]], [0.2, mix(t["mid"], t["bad"], 0.65)], [0.42, t["mid"]],
            [0.58, t["mid"]], [0.8, mix(t["mid"], t["good"], 0.65)], [1, t["good"]]]  # fmt: skip


def heat_grid(z: pd.DataFrame, text: pd.DataFrame, mode: str, hover: pd.DataFrame | None = None,
              height: int | None = None, row_px: int = 30) -> go.Figure:  # fmt: skip
    """Rows x columns grid colored by ``z`` (already scaled to -1..1: blue good, red bad,
    NaN gray), printing ``text``; columns can carry different units because each is
    scaled on its own before it gets here."""
    t = THEME[mode]
    zz = z.to_numpy(float)
    fig = go.Figure(go.Heatmap(
        z=np.nan_to_num(zz, nan=0.0), x=list(z.columns), y=list(z.index),
        text=text.to_numpy(), texttemplate="%{text}", textfont={"size": 12},
        customdata=(hover if hover is not None else text).to_numpy(),
        colorscale=scale(mode), zmin=-1, zmax=1, zmid=0, xgap=3, ygap=3, showscale=False,
        hovertemplate="%{y} · %{x}<br>%{customdata}<extra></extra>",
    ))  # fmt: skip
    base_layout(fig, height or row_px * len(z) + 70,
                xaxis={"side": "top", "fixedrange": True, "tickfont": {"color": t["ink2"]}},
                yaxis={"autorange": "reversed", "fixedrange": True,
                       "tickfont": {"color": t["ink"]}})  # fmt: skip
    return fig


def base_layout(fig: go.Figure, height: int, **kw) -> go.Figure:
    fig.update_layout(
        height=height,
        margin={"l": 0, "r": 10, "t": 10, "b": 0},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        **kw,
    )
    return fig


def diverging_bars(labels: list[str], values: list[float], mode: str, unit: str = "",
                   ratings: list[float | None] | None = None,
                   height: int | None = None) -> go.Figure:  # fmt: skip
    """Horizontal bars from zero, blue when good and red when bad, value labels outside."""
    vals = [0.0 if v is None or pd.isna(v) else float(v) for v in values]
    top = max([abs(v) for v in vals] + [0.5])
    colors = [good_bad(v, top, mode) if v else THEME[mode]["mid"] for v in vals]
    colors = [THEME[mode]["good"] if v > 0 else THEME[mode]["bad"] if v < 0 else c
              for v, c in zip(vals, colors, strict=True)]  # fmt: skip
    text = [f"{v:+.1f}" + (f"  ·  {r:.0f}" if ratings and r is not None and not pd.isna(r)
                           else "") for v, r in zip(vals, ratings or [None] * len(vals),
                                                    strict=True)]  # fmt: skip
    fig = go.Figure(go.Bar(
        y=labels, x=vals, orientation="h", marker_color=colors, marker_cornerradius=4,
        text=text, textposition="outside", cliponaxis=False,
        textfont={"color": THEME[mode]["ink2"], "size": 12},
        hovertemplate="%{y}: %{x:+.2f} " + unit + "<extra></extra>",
    ))  # fmt: skip
    fig.add_vline(x=0, line_color=THEME[mode]["muted"], line_width=1)
    base_layout(fig, height or max(180, 30 * len(labels) + 40),
                xaxis={"range": [-top * 1.45, top * 1.45], "zeroline": False,
                       "showgrid": False, "title": unit, "fixedrange": True},
                yaxis={"autorange": "reversed", "fixedrange": True},
                bargap=0.35, showlegend=False)  # fmt: skip
    return fig


def rating_heatmap(df: pd.DataFrame, rows: str, cols: list[str], col_labels: list[str],
                   mode: str, height: int | None = None) -> go.Figure:  # fmt: skip
    """Rows x dimensions grid of 0-100 ratings, diverging around 50, values printed."""
    z = df[cols].to_numpy(dtype=float)
    text = [["–" if np.isnan(v) else f"{v:.0f}" for v in row] for row in z]
    t = THEME[mode]
    fig = go.Figure(go.Heatmap(
        z=np.where(np.isnan(z), 50, z), x=col_labels, y=df[rows].tolist(), text=text,
        texttemplate="%{text}", textfont={"size": 12}, colorscale=scale(mode), zmin=20,
        zmax=80, zmid=50, xgap=3, ygap=3, showscale=False,
        hovertemplate="%{y} · %{x}: %{text} (50 = league average)<extra></extra>",
    ))  # fmt: skip
    base_layout(fig, height or 30 * len(df) + 60,
                xaxis={"side": "top", "fixedrange": True, "tickfont": {"color": t["ink2"]}},
                yaxis={"autorange": "reversed", "fixedrange": True,
                       "tickfont": {"color": t["ink"]}})  # fmt: skip
    return fig


def radar(axes: list[str], series: dict[str, list[float | None]], mode: str) -> go.Figure:
    """Ratings on 0-100 axes; a dashed ring marks league average (50)."""
    t = THEME[mode]
    fig = go.Figure()
    closed = axes + axes[:1]
    fig.add_trace(go.Scatterpolar(r=[50] * len(closed), theta=closed, mode="lines",
                                  name="league average", hoverinfo="skip",
                                  line={"dash": "dash", "color": t["muted"]}))  # fmt: skip
    for k, (name, vals) in enumerate(series.items()):
        v = [x if x is not None and not (isinstance(x, float) and math.isnan(x)) else None
             for x in vals]  # fmt: skip
        color = t["series"][k % len(t["series"])]
        fig.add_trace(go.Scatterpolar(
            r=v + v[:1], theta=closed, fill="toself", name=name, connectgaps=False,
            line={"color": color, "width": 2}, fillcolor=mix(color, t["mid"], 0.55) + "80",
            hovertemplate="%{theta}: %{r:.0f}<extra>" + name + "</extra>",
        ))  # fmt: skip
    fig.update_layout(
        polar={"radialaxis": {"range": [0, 100], "tickvals": [25, 50, 75],
                              "gridcolor": t["grid"]},
               "angularaxis": {"gridcolor": t["grid"]}, "bgcolor": "rgba(0,0,0,0)"},
        margin={"l": 40, "r": 40, "t": 30, "b": 30}, height=380,
        legend={"orientation": "h"}, paper_bgcolor="rgba(0,0,0,0)",
    )  # fmt: skip
    return fig


def bullet(value: float, marks: dict[str, float], mode: str, prefix: str = "$",
           suffix: str = "M", band: tuple[float, float] | None = None,
           label: str = "", x0: float = 0.0) -> go.Figure:  # fmt: skip
    """One bar against labelled vertical thresholds, with an optional uncertainty band.

    ``x0`` starts the axis above zero to spread out thresholds that sit close together
    (payroll against the cap lines); labels alternate between two rows so they never
    overlap."""
    t = THEME[mode]
    top = max([value, *marks.values(), *(band or ())]) * 1.06
    fig = go.Figure()
    if band:
        fig.add_shape(
            type="rect",
            x0=band[0],
            x1=band[1],
            y0=-0.42,
            y1=0.42,
            fillcolor=rgba(t["good"], 0.22),
            line_width=0,
            layer="below",
        )
    hover = f"{label or 'value'} {prefix}%{{x:.1f}}{suffix}<extra></extra>"
    if x0 > 0:  # a bar on a truncated axis would misstate its length: mark the value instead
        fig.add_trace(go.Scatter(x=[value], y=[label or "value"], mode="markers+text",
                                 marker={"symbol": "diamond", "size": 18, "color": t["ink"]},
                                 text=[f"{label} {prefix}{value:.1f}{suffix}"],
                                 textposition="bottom center", hovertemplate=hover,
                                 textfont={"color": t["ink2"], "size": 12}))  # fmt: skip
    else:
        fig.add_trace(go.Bar(x=[value], y=[label or "value"], orientation="h", width=0.3,
                             marker_color=t["bar"], marker_cornerradius=4,
                             hovertemplate=hover))  # fmt: skip
    for k, (name, x) in enumerate(sorted(marks.items(), key=lambda kv: kv[1])):
        fig.add_vline(x=x, line_color=t["ink"], line_width=2)
        fig.add_annotation(x=x, y=1.02 + 0.3 * (k % 2), yref="paper", yanchor="bottom",
                           showarrow=False, text=f"{name} {prefix}{x:.0f}{suffix}",
                           font={"size": 11, "color": t["muted"]})  # fmt: skip
    base_layout(fig, 170, xaxis={"range": [x0, top], "tickprefix": prefix,
                                 "ticksuffix": suffix, "showgrid": False, "fixedrange": True},
                yaxis={"visible": False, "fixedrange": True}, showlegend=False)  # fmt: skip
    fig.update_layout(margin={"l": 0, "r": 10, "t": 60, "b": 0})
    return fig


def selected(event, field: int = 0):
    """customdata[field] of the first selected point of a plotly_chart event, or None."""
    try:
        pts = event.selection["points"]
    except (AttributeError, KeyError, TypeError):
        return None
    if not pts:
        return None
    cd = pts[0].get("customdata")
    if cd is None:
        return None
    if isinstance(cd, dict):  # Streamlit hands array customdata back as {"0": v0, ...}
        cd = [cd[k] for k in sorted(cd, key=lambda k: int(k))]
    return cd[field] if isinstance(cd, list | tuple) else cd
