"""Scorecard view (``courtvision scorecard``): how the logged predictions fared.

Predictions come from the ledger exactly as they were made (``forecast.ledger_*``, never
rewritten); results from the NBA's live feed or the database. Per game: predicted vs
final margin and the winner; per player: each stat line against the box score, whether
it landed inside the 80% range, and how the season average would have done.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from courtvision.app import charts as C
from courtvision.app import data as D

STATS = {"pts": "Points", "reb": "Rebounds", "ast": "Assists", "fg3m": "Threes",
         "stocks": "Steals + blocks", "tov": "Turnovers"}  # fmt: skip


def _has(con, table: str) -> bool:
    return bool(D.q(con, "select count(*) n from information_schema.tables where "
                    "table_schema = 'forecast' and table_name = ?", [table]).n.iloc[0])  # fmt: skip


def render(con, short_team, players: pd.DataFrame, mode: str, goto) -> None:
    if not _has(con, "scorecard_games"):
        st.info("Nothing scored yet. After a day's games finish, run "
                "`courtvision scorecard --date YYYY-MM-DD`.")  # fmt: skip
        return
    t = C.THEME[mode]
    days = D.q(con, "select distinct day from forecast.scorecard_games order by 1 desc").day
    day = st.selectbox("Game day", list(days), key="sc_day",
                       format_func=lambda d: pd.Timestamp(d).strftime("%a %b %d, %Y"))  # fmt: skip
    g = D.q(con, "select * from forecast.scorecard_games where day = ? order by tipoff", [day])
    pl = D.q(con, "select * from forecast.scorecard_players where day = ?", [day]) if _has(
        con, "scorecard_players") else pd.DataFrame()  # fmt: skip
    season = int(pd.Timestamp(day).year - (pd.Timestamp(day).month < 8))
    g["label"] = [f"{short_team(a, season)} @ {short_team(h, season)}"
                  for a, h in zip(g.away_team_id, g.home_team_id, strict=True)]  # fmt: skip
    played = pl[pl.played] if len(pl) else pl

    c = st.columns(4)
    c[0].metric("Winners", f"{int(g.winner_right.sum())} of {len(g)}")
    c[1].metric("Margin error", f"{g.error.abs().mean():.1f} pts",
                help="Average miss on the final margin. The model's tested average over five "
                "seasons is 10.9 points; single nights swing far more.")  # fmt: skip
    c[2].metric("Brier score", f"{g.brier.mean():.3f}",
                help="Squared error of the win chances: 0.25 = a coin flip, lower is better. "
                "Tested average: 0.214.")  # fmt: skip
    if len(played):
        c[3].metric(
            "Stat lines inside the 80% range",
            f"{played.inside.mean():.0%}",
            help="Across every stat of every player who played. Target: 80%.",
        )
    early = g.after_tipoff.any()
    st.caption(
        f"Predictions from the ledger as made ({pd.Timestamp(g.predicted_at.min()):%b %d %I:%M %p}"
        f"; games through {pd.Timestamp(g.data_through.min()):%b %d}"
        + (
            f", injury report of {pd.Timestamp(g.injury_report.min()):%I:%M %p}"
            if g.injury_report.notna().any()
            else ""
        )
        + "). "
        + (
            "Some were made after tip-off (marked ·), but forecasts read only completed games "
            "and the injury report, never the game in progress. "
            if early
            else ""
        )
        + "One night is a handful of games: read it next to the tested averages."
    )

    # ---- games ---------------------------------------------------------------------------
    st.subheader("Games: predicted vs final margin")
    fig = go.Figure()
    for r in g.itertuples():
        fig.add_trace(go.Scatter(x=[r.pred_margin, r.actual_margin], y=[r.label, r.label],
                                 mode="lines", line={"color": t["grid"], "width": 3},
                                 hoverinfo="skip", showlegend=False))  # fmt: skip
    fig.add_trace(go.Scatter(
        x=g.pred_margin, y=g.label, mode="markers", name="predicted",
        marker={"size": 13, "color": "rgba(0,0,0,0)", "line": {"width": 2.5, "color": t["muted"]}},
        customdata=np.stack([g.home_win_prob], axis=1),
        hovertemplate="%{y}: predicted home %{x:+.1f} (home win %{customdata[0]:.0%})"
        "<extra></extra>",
    ))  # fmt: skip
    fig.add_trace(go.Scatter(
        x=g.actual_margin, y=g.label, mode="markers+text", name="final",
        marker={"size": 13, "color": [t["good"] if w else t["bad"] for w in g.winner_right],
                "line": {"width": 1.5, "color": t["mid"]}},
        text=[f"{int(a)}-{int(h)}" + (" ·" if e else "") for a, h, e in
              zip(g.away_score, g.home_score, g.after_tipoff, strict=True)],
        textposition="top center", textfont={"size": 11, "color": t["ink2"]},
        hovertemplate="%{y}: final home %{x:+.0f}<extra></extra>",
    ))  # fmt: skip
    fig.add_vline(x=0, line_color=t["muted"], line_width=1)
    top = float(np.nanmax(np.abs(np.r_[g.pred_margin, g.actual_margin]))) * 1.2 + 2
    C.base_layout(fig, 70 * len(g) + 90, yaxis={"autorange": "reversed", "fixedrange": True},
                  xaxis={"range": [-top, top], "title": "home margin (right: home won)"},
                  legend={"orientation": "h", "y": 1.12})  # fmt: skip
    st.plotly_chart(fig, width="stretch", key="sc_games")
    st.caption(
        "Hollow: predicted margin. Solid: final margin, blue when the predicted "
        "winner won, red when not. The gap is the miss."
    )
    led = D.q(con, "select distinct game_id, home_team_id, away_team_id from "
              "forecast.ledger_games where game_date::date = ?::date", [day])  # fmt: skip
    wait = led[~led.game_id.isin(g.game_id)]
    if len(wait):
        st.caption(
            "Not final when last scored: "
            + ", ".join(f"{short_team(a, season)} @ {short_team(h, season)}"
                        for a, h in zip(wait.away_team_id, wait.home_team_id, strict=True))
            + f". Run `courtvision scorecard --date {pd.Timestamp(day):%Y-%m-%d}` again "
            "once it ends."
        )  # fmt: skip

    if not len(played):
        return
    # ---- players -------------------------------------------------------------------------
    st.subheader("Player stat lines vs the box score")
    stat = st.radio("Stat", list(STATS), format_func=STATS.get, horizontal=True, key="sc_stat",
                    label_visibility="collapsed")  # fmt: skip
    s = played[played.stat == stat].merge(players, on="person_id", how="left")
    a, b = st.columns([3, 2])
    with a:
        fig = go.Figure()
        hi = float(max(s.actual.max(), s.hi.max())) + 2
        fig.add_trace(go.Scatter(x=[0, hi], y=[0, hi], mode="lines", hoverinfo="skip",
                                 line={"dash": "dash", "color": t["muted"], "width": 1},
                                 showlegend=False))  # fmt: skip
        for inside, name, color in ((True, "inside the 80% range", t["good"]),
                                    (False, "outside", t["bad"])):  # fmt: skip
            x = s[s.inside == inside]
            fig.add_trace(go.Scatter(
                x=x.pred, y=x.actual, mode="markers", name=name,
                marker={"size": 9, "color": color, "line": {"width": 1.5, "color": t["mid"]}},
                error_x={"type": "data", "symmetric": False, "array": x.hi - x.pred,
                         "arrayminus": x.pred - x.lo, "color": C.rgba(color, 0.35),
                         "thickness": 1.5, "width": 0},
                customdata=np.stack([x.player, *C.count_range(x.lo, x.hi), x.minutes], axis=1),
                hovertemplate="%{customdata[0]}: predicted %{x:.1f} "
                "(%{customdata[1]:.0f}-%{customdata[2]:.0f}), got %{y:.0f} in "
                "%{customdata[3]:.0f} min<extra></extra>",
            ))  # fmt: skip
        C.base_layout(fig, 420, xaxis={"title": f"predicted {STATS[stat].lower()}",
                                       "range": [0, hi]},
                      yaxis={"title": "actual", "range": [0, hi]},
                      legend={"orientation": "h", "y": 1.08})  # fmt: skip
        st.plotly_chart(fig, width="stretch", key="sc_scatter")
    with b:
        cov = played.groupby("stat").inside.mean().reindex(list(STATS))
        mae = played.groupby("stat").apply(
            lambda d: pd.Series({"model": d.error.abs().mean(),
                                 "season average": d.avg_error.abs().mean()}),
            include_groups=False).reindex(list(STATS))  # fmt: skip
        fig = go.Figure(go.Bar(
            y=[STATS[k] for k in cov.index], x=cov.values, orientation="h",
            marker_color=t["series"][0], marker_cornerradius=4,
            text=[f"{v:.0%}" for v in cov.values], textposition="outside",
            textfont={"color": t["ink2"], "size": 11}, cliponaxis=False,
            hovertemplate="%{y}: %{x:.0%} inside the 80% range<extra></extra>",
        ))  # fmt: skip
        fig.add_vline(x=0.8, line_color=t["ink"], line_width=2)
        C.base_layout(fig, 280, xaxis={"range": [0, 1.15], "tickformat": ".0%",
                                       "title": "inside the 80% range (line: target)"},
                      yaxis={"autorange": "reversed"})  # fmt: skip
        st.plotly_chart(fig, width="stretch", key="sc_cov")
        st.dataframe(mae.round(2).rename(index=STATS).rename_axis("average miss"),
                     width="stretch")  # fmt: skip
    st.caption(f"{played.person_id.nunique()} players who played. Average miss of the stat "
               "lines against each player's season-to-date average.")  # fmt: skip

    miss = s.assign(abs_err=s.error.abs()).sort_values("abs_err", ascending=False).head(8)
    st.markdown(f"**Biggest misses** ({STATS[stat].lower()})")
    st.dataframe(pd.DataFrame({
        "player": miss.player, "predicted": miss.pred.round(1),
        "range": [C.range_text(lo, hi) for lo, hi in zip(miss.lo, miss.hi, strict=True)],
        "actual": miss.actual.astype(int), "minutes": miss.minutes.round(0),
        "expected minutes": miss.exp_minutes.round(0),
    }), hide_index=True)  # fmt: skip

    dnp = pl[~pl.played & (pl.stat == "pts")].merge(players, on="person_id", how="left")
    if len(dnp):
        st.caption("Expected to play but didn't: " + ", ".join(
            f"{p} ({m:.0f} min expected)" for p, m in zip(dnp.player, dnp.exp_minutes,
                                                          strict=True)) + ".")  # fmt: skip
