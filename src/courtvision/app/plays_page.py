"""Plays view (``courtvision plays``): what teams run, how well, who runs it, who passes to
whom, and how they turn it over; offense or defense. Play types come from the
play-by-play (2015-16 on), not tracking data; see ``courtvision.plays``.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from courtvision import plays as P
from courtvision.app import charts as C
from courtvision.app import data as D


def render(con, team_label, players: pd.DataFrame, mode: str, goto) -> None:
    if not D.has_schema(con, "plays"):
        st.warning("No play types yet. Run `courtvision plays`.")
        return
    t = C.THEME[mode]
    seasons = D.q(con, "select distinct season from plays.team order by 1 desc").season.tolist()
    c1, c2, c3 = st.columns([1, 2, 1])
    season = c1.selectbox("Season", seasons, key="pl_season",
                          format_func=lambda s: f"{s}-{(s + 1) % 100:02d}")  # fmt: skip
    side = c3.segmented_control("Side", ["Offense", "Defense"], default="Offense",
                                key="pl_side") or "Offense"  # fmt: skip
    sd = side.lower()
    tm = D.q(con, "select * from plays.team where season = ? and side = ?", [season, sd])
    tm["team"] = tm.team_id.map(lambda i: team_label(i, season))
    teams = sorted(tm.team.unique())
    if st.session_state.get("pl_team") not in teams:
        st.session_state["pl_team"] = teams[0]
    name = c2.selectbox("Team", teams, key="pl_team")
    team = int(tm[tm.team == name].team_id.iloc[0])
    sign = 1 if sd == "offense" else -1  # blue = good for the team looked at
    rel = D.q(con, "select * from plays.reliability where side = ?", [sd]).set_index("play")

    # ---- league fingerprint ------------------------------------------------------------
    st.subheader("Every team's play mix" + (" allowed" if sd == "defense" else ""))
    order = [p for p in P.PLAYS if p in set(tm.play)]
    share = tm.pivot(index="team", columns="play", values="share").reindex(columns=order)
    eff = tm.pivot(index="team", columns="play", values="pp100_rel_shrunk").reindex(columns=order)
    share = share.loc[share.index.sort_values()]
    eff = eff.loc[share.index]
    z = np.clip(sign * eff / 8, -1, 1)
    text = share.map(lambda v: "" if pd.isna(v) else f"{v:.0%}")
    hover = pd.DataFrame(
        [[f"{share.iloc[i, j]:.1%} of {'opponent ' if sd == 'defense' else ''}shots · "
          f"{eff.iloc[i, j]:+.1f} per 100 vs league" for j in range(len(order))]
         for i in range(len(share))], index=share.index, columns=order)  # fmt: skip
    st.plotly_chart(C.heat_grid(z, text, mode, hover, row_px=24), width="stretch",
                    key="pl_league")  # fmt: skip
    st.caption(
        "Number: share of "
        + ("opponents' " if sd == "defense" else "")
        + "shots on each play. Color: points per 100 shots on it against league average "
        "(shrunk by its measured reliability); blue is good for the "
        + ("defense (opponents scored less)." if sd == "defense" else "offense.")
        + " Play mix is a team's fingerprint (split-half r 0.7-0.99); efficiency on cuts, "
        "lobs, step-backs and in transition is mostly noise within a season."
    )

    # ---- one team -------------------------------------------------------------------------
    st.subheader(f"{name}: what they {'allow' if sd == 'defense' else 'run'}")
    me = tm[tm.team_id == team].set_index("play").reindex(order).dropna(subset=["shots"])
    a, b = st.columns(2)
    with a:
        st.markdown("**How often**, against league (tick)")
        fig = go.Figure()
        fig.add_trace(go.Bar(y=me.index, x=100 * me.share, orientation="h", name=name,
                             marker_color=t["series"][0], marker_cornerradius=4,
                             hovertemplate="%{y}: %{x:.1f}% of shots<extra></extra>"))  # fmt: skip
        fig.add_trace(go.Scatter(y=me.index, x=100 * me.lg_share, mode="markers",
                                 name="league", marker={"symbol": "line-ns", "size": 18,
                                                        "line": {"width": 3, "color": t["ink"]}},
                                 hovertemplate="league %{x:.1f}%<extra></extra>"))  # fmt: skip
        C.base_layout(fig, 30 * len(me) + 60, yaxis={"autorange": "reversed", "fixedrange": True},
                      xaxis={"ticksuffix": "%", "fixedrange": True},
                      legend={"orientation": "h", "y": -0.08}, bargap=0.35)  # fmt: skip
        st.plotly_chart(fig, width="stretch", key="pl_share")
    with b:
        st.markdown("**How well**: points per 100 shots against league")
        st.plotly_chart(C.diverging_bars(list(me.index), list(sign * me.pp100_rel_shrunk), mode,
                                         "points per 100 shots vs league (blue good)",
                                         height=30 * len(me) + 60),
                        width="stretch", key="pl_eff")  # fmt: skip
    best = me.assign(v=sign * me.share * me.pp100_rel_shrunk).sort_values("v")
    st.caption(
        f"Most valuable: **{best.index[-1]}** ({best.share.iloc[-1]:.0%} of shots, "
        f"{sign * best.pp100_rel_shrunk.iloc[-1]:+.1f}). Costliest: **{best.index[0]}** "
        f"({best.share.iloc[0]:.0%}, {sign * best.pp100_rel_shrunk.iloc[0]:+.1f}). "
        f"{P.DESCRIBE.get(best.index[-1], '')}."
    )

    if sd == "offense":
        _who_runs_it(con, team, season, order, players, mode, goto)
        _passing(con, team, season, players, mode)
    _turnovers(con, team, season, sd, mode)
    with st.expander("How the plays are defined, and how far to trust them"):
        st.dataframe(pd.DataFrame({"play": list(P.DESCRIBE), "what": list(P.DESCRIBE.values())}),
                     hide_index=True)  # fmt: skip
        r = rel.reindex(order)
        st.dataframe(pd.DataFrame({
            "play": order, "mix repeats (split-half r)": r.share_r.round(2).to_numpy(),
            "efficiency repeats (r)": r.efficiency_r.round(2).to_numpy(),
            "shrinkage (shots)": r.k.round(0).to_numpy()}), hide_index=True)  # fmt: skip
        mt = D.q(con, "select * from plays.matchup_test")
        allp = mt[mt.play == "all plays"].iloc[0]
        st.caption(
            "Each shot is classified from what the play-by-play says about it (driving, "
            "pull-up, step-back, cutting, alley-oop, putback, floater, turnaround, hook) and "
            "how its possession started; no tracking data. Tagging got more detailed over the "
            "years, so compare teams within a season. Predicting each season's second-half "
            f"games from its first half, a team's own play numbers cut the error by "
            f"{allp.gain_offense_pct:.1f}% and adding the opponent defense's numbers by "
            f"{allp.gain_offense_defense_pct:.1f}% (single games are noisy; drives and "
            "pull-ups gain the most)."
        )


def _who_runs_it(con, team, season, order, players, mode, goto) -> None:
    t = C.THEME[mode]
    play = st.selectbox("Who runs it", order, key="pl_play")
    pl = D.q(con, """select * from plays.player where season = ? and team_id = ? and play = ?
                     order by shots desc limit 10""", [season, team, play])  # fmt: skip
    if pl.empty:
        return
    pl = pl.merge(players, on="person_id", how="left")
    fig = go.Figure(go.Bar(
        y=pl.player, x=pl.shots, orientation="h", marker_cornerradius=4,
        marker_color=[C.good_bad(v, 15, mode) for v in pl.pp100_rel_shrunk],
        text=[f"{n:.0f} shots · {v:+.1f}" for n, v in zip(pl.shots, pl.pp100_rel_shrunk,
                                                           strict=True)],
        textposition="outside", textfont={"color": t["ink2"], "size": 11}, cliponaxis=False,
        customdata=pl[["player", "share"]].to_numpy(),
        hovertemplate="%{y}: %{x:.0f} shots on this play (%{customdata[1]:.0%} of his "
        "shots)<extra></extra>",
    ))  # fmt: skip
    C.base_layout(fig, 28 * len(pl) + 60, yaxis={"autorange": "reversed", "fixedrange": True},
                  xaxis={"title": f"{play} shots", "fixedrange": True,
                         "range": [0, pl.shots.max() * 1.3]})  # fmt: skip
    ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                         key="pl_who")  # fmt: skip
    if (p := C.selected(ev)) is not None:
        goto("Player", clear="pl_who", p_name=p, p_season=season)
    st.caption(
        "Bar: his shots on this play. Color and number: his points per 100 shots on "
        "it against league, shrunk. **Click a player** to open him."
    )


def _passing(con, team, season, players, mode) -> None:
    t = C.THEME[mode]
    st.markdown("**Passing connections**: who assists whom, and on what")
    a = D.q(con, """select passer_id, shooter_id, play, assists, pts from plays.assists
                    where season = ? and team_id = ?""", [season, team])  # fmt: skip
    if a.empty:
        return
    names = players.set_index("person_id").player
    pair = a.groupby(["passer_id", "shooter_id"]).agg(assists=("assists", "sum"),
                                                      pts=("pts", "sum")).reset_index()  # fmt: skip
    pair = pair.sort_values("assists", ascending=False).head(18)
    who = pd.Index(pd.unique(pd.concat([pair.passer_id, pair.shooter_id])))
    vol = (pair.groupby("passer_id").assists.sum().reindex(who).fillna(0)
           + pair.groupby("shooter_id").assists.sum().reindex(who).fillna(0))  # fmt: skip
    who = vol.sort_values(ascending=False).index
    ang = {p: math.pi / 2 - 2 * math.pi * i / len(who) for i, p in enumerate(who)}
    pos = {p: (math.cos(v), math.sin(v)) for p, v in ang.items()}
    fig = go.Figure()
    top = pair.assists.max()
    size = {p: 10 + 22 * math.sqrt(vol[p] / vol.max()) for p in who}
    for r in pair.itertuples():
        (x0, y0), (x1, y1) = pos[r.passer_id], pos[r.shooter_id]
        # bend each edge to its own side, so A -> B and B -> A don't overlap
        cx, cy = (x0 + x1) / 2 * 0.6 + (y1 - y0) * 0.12, (y0 + y1) / 2 * 0.6 - (x1 - x0) * 0.12
        color = C.rgba(t["series"][0], 0.55)
        fig.add_trace(go.Scatter(
            x=[x0, cx, x1], y=[y0, cy, y1], mode="lines",
            line={"width": 1 + 7 * r.assists / top, "color": color, "shape": "spline"},
            hoverinfo="skip", showlegend=False))  # fmt: skip
        fig.add_annotation(x=x1, y=y1, ax=cx, ay=cy, xref="x", yref="y", axref="x", ayref="y",
                           text="", showarrow=True, arrowhead=3, arrowsize=1.2,
                           arrowwidth=1.6, arrowcolor=t["series"][0],
                           standoff=size[r.shooter_id] / 2 + 2)  # fmt: skip
    mix = a.groupby(["passer_id", "shooter_id", "play"]).assists.sum().reset_index()
    labels = {}
    for r in pair.itertuples():
        m = mix[(mix.passer_id == r.passer_id) & (mix.shooter_id == r.shooter_id)]
        m = m.sort_values("assists", ascending=False).head(2)
        labels[(r.passer_id, r.shooter_id)] = ", ".join(
            f"{p} {n / r.assists:.0%}" for p, n in zip(m.play, m.assists, strict=True)
        )
    fig.add_trace(go.Scatter(
        x=[pos[p][0] for p in who], y=[pos[p][1] for p in who], mode="markers+text",
        text=[names.get(p, str(p)).split()[-1] for p in who], textposition="top center",
        textfont={"size": 11, "color": t["ink"]},
        marker={"size": [size[p] for p in who],
                "color": t["series"][0], "line": {"width": 2, "color": t["mid"]}},
        customdata=[[names.get(p, str(p)), int(vol[p])] for p in who],
        hovertemplate="%{customdata[0]}: %{customdata[1]} assists given + received (top "
        "connections)<extra></extra>", showlegend=False,
    ))  # fmt: skip
    C.base_layout(fig, 460, xaxis={"visible": False, "range": [-1.35, 1.35], "fixedrange": True},
                  yaxis={"visible": False, "range": [-1.25, 1.3], "fixedrange": True,
                         "scaleanchor": "x"})  # fmt: skip
    left, right = st.columns([3, 2])
    with left:
        st.plotly_chart(fig, width="stretch", key="pl_net")
    with right:
        rows = pair.head(10).copy()
        ps = list(zip(rows.passer_id, rows.shooter_id, strict=True))
        rows["connection"] = [f"{names.get(p, p)} → {names.get(s, s)}" for p, s in ps]
        rows["mostly"] = [labels[(p, s)] for p, s in ps]
        st.dataframe(rows[["connection", "assists", "pts", "mostly"]], hide_index=True,
                     height=390)  # fmt: skip
    st.caption(
        "The team's 18 most frequent passer → scorer pairs: arrows point from passer to "
        "scorer, line width = assists, node size = assists given and received among them. "
        "'Mostly': the plays the assisted shots came from."
    )


def _turnovers(con, team, season, sd, mode) -> None:
    tv = D.q(con, """select kind, per100, lg_per100, per100_rel from plays.turnovers
                     where season = ? and team_id = ? and side = ?""",
             [season, team, sd])  # fmt: skip
    if tv.empty:
        return
    tv = tv.set_index("kind").reindex(P.TOV_KINDS).dropna()
    st.markdown("**Turnovers by kind**, per 100 possessions against league"
                + (" (forced)" if sd == "defense" else ""))  # fmt: skip
    vals = list(-tv.per100_rel if sd == "offense" else tv.per100_rel)
    st.plotly_chart(C.diverging_bars(list(tv.index), vals, mode,
                                     "per 100 possessions vs league (blue good)",
                                     height=30 * len(tv) + 60),
                    width="stretch", key="pl_tov")  # fmt: skip
    st.caption(
        "Blue: fewer turnovers of that kind than league average"
        + (" (offense)." if sd == "offense" else "; here, more forced (defense).")
    )
