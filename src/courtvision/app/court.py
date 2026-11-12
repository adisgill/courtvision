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

# Diverging pair (blue = good for the unit, gray = average, red = bad) and ink, per theme.
THEME = {
    "light": {
        "good": "#2a78d6", "mid": "#f0efec", "bad": "#e34948",
        "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "lines": "rgba(11,11,11,0.55)", "bar": "#a8a69e",
    },
    "dark": {
        "good": "#3987e5", "mid": "#383835", "bad": "#e66767",
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
    """Diverging fill for a good-for-the-unit difference in points of % (gray if unknown)."""
    t = THEME[mode]
    if diff_pp is None or math.isnan(diff_pp):
        return t["mid"]
    s = max(-1.0, min(1.0, diff_pp / FG_SCALE))
    return _mix(t["mid"], t["good"] if s >= 0 else t["bad"], abs(s))


def _text_on(fill: str) -> str:
    def lin(c: int) -> float:
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in _rgb(fill))
    return "#0b0b0b" if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.3 else "#ffffff"


# ---- charts -------------------------------------------------------------------------------

_LABEL_AT = {"RA": (0, 24), "PAINT": (0, 112), "MID": (-150, 112), "ATB3": (0, 268)}


def zone_court(prof: pd.DataFrame, mode: str = "light", allowed: bool = False) -> go.Figure:
    """Each zone filled by FG% vs league and labelled with it. Blue is good for the unit:
    above-average shooting on offense, opponents below average on defense.

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
        fill = heat_color(None if diff is None else (-diff if allowed else diff), mode)
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


# ---- hexagon shot maps -------------------------------------------------------------------

HEX_R = 17.0  # hexagon radius, tenths of a foot (~1.7 ft)
K_ZONE, K_HEX = 150.0, 60.0  # shots of prior: zone toward league, hexagon toward its zone
PP100_SCALE = 20.0  # points per 100 shots vs league at full color
FULL_SHARE = 0.016  # a hexagon with this share of the shots is drawn full size
_SQ3 = math.sqrt(3)


def hex_of(x: np.ndarray, y: np.ndarray, r: float = HEX_R) -> tuple[np.ndarray, np.ndarray]:
    """Axial coordinates (q, r) of the pointy-top hexagon containing each point."""
    q = (_SQ3 / 3 * x - y / 3) / r
    rr = (2 / 3 * y) / r
    cx, cz = q, rr
    cy = -cx - cz
    rx, ry, rz = np.round(cx), np.round(cy), np.round(cz)
    dx, dy, dz = np.abs(rx - cx), np.abs(ry - cy), np.abs(rz - cz)
    fix_x = (dx > dy) & (dx > dz)
    fix_z = ~fix_x & (dz >= dy)
    rx = np.where(fix_x, -ry - rz, rx)
    rz = np.where(fix_z, -rx - ry, rz)
    return rx.astype(int), rz.astype(int)


def hex_center(q, rr, r: float = HEX_R):
    return r * _SQ3 * (q + np.asarray(rr) / 2), r * 1.5 * np.asarray(rr)


def _zone_arr(v: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    d = np.hypot(x, y)
    return np.select(
        [(v == 3) & (np.abs(x) >= 220) & (y <= 87), v == 3, d < 40, (np.abs(x) <= 80) & (y <= 138)],
        ["C3", "ATB3", "RA", "PAINT"], "MID")  # fmt: skip


def league_hex(shots: pd.DataFrame) -> pd.DataFrame:
    """League baseline per hexagon from all of a season's shots (x, y, made, shot_value):
    attempts, FG%, points per shot, and the zone most of its shots belong to."""
    s = shots.dropna(subset=["x", "y"])
    s = s[(s.y <= 330) & (s.y >= -50)]
    q, rr = hex_of(s.x.to_numpy(float), s.y.to_numpy(float))
    d = pd.DataFrame({"q": q, "rr": rr, "made": s.made.astype(float).to_numpy(),
                      "pts": (s.made.astype(float) * s.shot_value).to_numpy(),
                      "zone": _zone_arr(s.shot_value.to_numpy(), s.x.to_numpy(float),
                                        s.y.to_numpy(float))})  # fmt: skip
    g = (
        d.groupby(["q", "rr"])
        .agg(
            lg_n=("made", "size"),
            lg_fg=("made", "mean"),
            lg_pps=("pts", "mean"),
            zone=("zone", lambda z: z.mode().iloc[0]),
        )
        .reset_index()
    )
    return g


def unit_hex(shots: pd.DataFrame, league: dict[int, pd.DataFrame]) -> pd.DataFrame:
    """One unit's shots (season, x, y, made, shot_value, optional weight w) against the
    league from the same spots. Per hexagon: weighted attempts, share, FG%, and points
    per 100 shots vs league (``diff``), shrunk toward the unit's zone and the zone toward
    league average, so a hexagon with three shots doesn't paint itself dark."""
    s = shots.dropna(subset=["x", "y"]).copy()
    s = s[(s.y <= 330) & (s.y >= -50)]
    if s.empty:
        return pd.DataFrame()
    s["w"] = s.get("w", 1.0)
    s["q"], s["rr"] = hex_of(s.x.to_numpy(float), s.y.to_numpy(float))
    base = pd.concat([lg.assign(season=se) for se, lg in league.items()])
    s = s.merge(base, on=["season", "q", "rr"], how="left")
    s["lg_pps"] = s.lg_pps.fillna(s.made.mean() * s.shot_value)
    s["lg_fg"] = s.lg_fg.fillna(s.made.mean())
    s["zone"] = s.zone.fillna(pd.Series(_zone_arr(s.shot_value.to_numpy(), s.x.to_numpy(float),
                                                  s.y.to_numpy(float)), index=s.index))  # fmt: skip
    s["pts"] = s.made.astype(float) * s.shot_value
    s["ex"] = s.w * (s.pts - s.lg_pps)
    s["wm"], s["wl"] = s.w * s.made.astype(float), s.w * s.lg_fg
    z = s.groupby("zone").agg(zn=("w", "sum"), zex=("ex", "sum"))
    zdiff = z.zex / (z.zn + K_ZONE)
    h = s.groupby(["q", "rr"]).agg(n=("w", "sum"), ex=("ex", "sum"), made=("wm", "sum"),
                                   lgm=("wl", "sum"), zone=("zone", lambda v: v.mode().iloc[0])
                                   ).reset_index()  # fmt: skip
    h["zone_diff"] = h.zone.map(zdiff).fillna(0.0)
    h["diff"] = 100 * (h.ex + K_HEX * h.zone_diff) / (h.n + K_HEX)
    h["share"] = h.n / h.n.sum()
    h["fg"] = h.made / h.n
    h["lg_fg"] = h.lgm / h.n
    h["cx"], h["cy"] = hex_center(h.q.to_numpy(), h.rr.to_numpy())
    return h


def _hex_poly(cx: float, cy: float, scale: float) -> list[tuple[float, float]]:
    r = HEX_R * scale
    return [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
            for a in (30, 90, 150, 210, 270, 330, 30)]  # fmt: skip


def hex_court(h: pd.DataFrame, mode: str = "light", value: str = "diff", size: str = "share",
              hover: list[str] | None = None, min_share: float = 0.0015,
              scale: float = PP100_SCALE) -> go.Figure:  # fmt: skip
    """Hexagon shot map: each hexagon's area follows ``size`` (share of shots), its color
    ``value`` (points per 100 shots vs league from the same spots; blue good for the unit,
    red bad, gray average). ``hover``: one text line per hexagon."""
    t = THEME[mode]
    fig = go.Figure()
    h = h[(h[size] >= min_share) & (h.cy <= Y_TOP - 8)].copy() if not h.empty else h
    if not h.empty:
        h["k"] = np.clip(np.sqrt(h[size] / FULL_SHARE), 0.32, 1.0) * 0.96
        steps = np.linspace(-1, 1, 11)
        h["bin"] = np.digitize(np.clip(h[value] / scale, -1, 1), (steps[:-1] + steps[1:]) / 2)
        for b, g in h.groupby("bin"):
            s = steps[int(b)]
            fill = _mix(t["mid"], t["good"] if s >= 0 else t["bad"], abs(s))
            xs: list[float | None] = []
            ys: list[float | None] = []
            for r in g.itertuples():
                poly = _hex_poly(r.cx, r.cy, r.k)
                xs += [p[0] for p in poly] + [None]
                ys += [p[1] for p in poly] + [None]
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", fill="toself", fillcolor=fill,
                                     line={"width": 0.6, "color": fill}, hoverinfo="skip",
                                     showlegend=False))  # fmt: skip
        fig.add_trace(go.Scatter(
            x=h.cx, y=h.cy, mode="markers", showlegend=False,
            marker={"size": 16, "color": "rgba(0,0,0,0)"},
            text=hover if hover is not None else [
                f"{r.share:.1%} of shots · FG {r.fg:.0%} (league {r.lg_fg:.0%})<br>"
                f"{r.diff:+.1f} points per 100 shots vs league" for r in h.itertuples()],
            hovertemplate="%{text}<extra></extra>",
        ))  # fmt: skip
    fig.update_layout(
        shapes=court_lines(t["lines"]),
        xaxis={"visible": False, "range": [X_MIN - 5, X_MAX + 5], "fixedrange": True},
        yaxis={"visible": False, "range": [Y_MIN - 8, Y_TOP + 8], "fixedrange": True,
               "scaleanchor": "x"},
        margin={"l": 0, "r": 0, "t": 6, "b": 0}, height=430,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        hoverlabel={"bgcolor": t["mid"], "font": {"color": t["ink"]}},
    )  # fmt: skip
    return fig


def hex_key(mode: str = "light", scale: float = PP100_SCALE, unit: str = "") -> go.Figure:
    """A slim legend for the hexagon maps: color steps and two sizes."""
    t = THEME[mode]
    fig = go.Figure()
    steps = np.linspace(-1, 1, 11)
    for i, s in enumerate(steps):
        fill = _mix(t["mid"], t["good"] if s >= 0 else t["bad"], abs(s))
        poly = _hex_poly(i * 34.0, 0, 0.96)
        fig.add_trace(go.Scatter(x=[p[0] for p in poly], y=[p[1] for p in poly], fill="toself",
                                 fillcolor=fill, mode="lines", line={"width": 0},
                                 hoverinfo="skip", showlegend=False))  # fmt: skip
    for x, txt, anchor in ((-20, f"−{scale:.0f}", "right"), (360, f"+{scale:.0f}", "left")):
        fig.add_annotation(x=x, y=0, text=txt, showarrow=False, xanchor=anchor,
                           font={"size": 11, "color": t["ink2"]})  # fmt: skip
    fig.add_annotation(x=170, y=-24, text=unit, showarrow=False, yanchor="top",
                       font={"size": 11, "color": t["muted"]})  # fmt: skip
    for x, k, txt in ((470, 0.4, "rare"), (530, 1.0, "frequent")):
        poly = _hex_poly(x, 0, k)
        fig.add_trace(go.Scatter(x=[p[0] for p in poly], y=[p[1] for p in poly], fill="toself",
                                 fillcolor=t["bar"], mode="lines", line={"width": 0},
                                 hoverinfo="skip", showlegend=False))  # fmt: skip
        fig.add_annotation(x=x, y=-24, text=txt, showarrow=False, yanchor="top",
                           font={"size": 11, "color": t["muted"]})  # fmt: skip
    fig.update_layout(xaxis={"visible": False, "range": [-80, 580], "fixedrange": True},
                      yaxis={"visible": False, "range": [-48, 22], "fixedrange": True,
                             "scaleanchor": "x"},
                      height=70, margin={"l": 0, "r": 0, "t": 0, "b": 0},
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")  # fmt: skip
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
