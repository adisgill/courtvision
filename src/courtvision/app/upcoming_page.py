"""Upcoming view: predictions for each team's next game (``courtvision forecast``).

A strip of the next games (home win chance), and for the chosen game: the margin, each
team's predicted dimension ratings against its season average, where each side has the
edge, and every expected player's predicted ratings and minutes.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from courtvision.app import charts as C
from courtvision.app import data as D

DIMS = ["shooting", "defense", "ball_control", "backcourt", "frontcourt"]


def _has_forecast(con) -> bool:
    return bool(D.q(con, "select count(*) n from information_schema.tables "
                    "where table_schema = 'forecast'").n.iloc[0])  # fmt: skip


def render(con, team_label, short_team, players: pd.DataFrame, mode: str, goto) -> None:
    if not _has_forecast(con):
        st.warning("No forecasts yet. Run `courtvision forecast`.")
        return
    games = D.q(con, "select * from forecast.games order by tipoff")
    if games.empty:
        st.info("No upcoming games in the schedule.")
        return
    season = int(D.q(con, "select max(season) as s from metrics.team_season").s.iloc[0])
    games["label"] = [
        f"{short_team(a, season)} @ {short_team(h, season)}"
        for a, h in zip(games.away_team_id, games.home_team_id, strict=True)
    ]
    games["when"] = pd.to_datetime(games.tipoff).dt.strftime("%a %b %d %H:%M")
    t = C.THEME[mode]

    last = D.q(con, "select max(game_date) as d from stg.games where home_score is not null")
    st.subheader("Next game for every team")
    st.caption(f"Predicted from games through {pd.to_datetime(last.d.iloc[0]):%b %d, %Y} "
               "(refresh with `courtvision build` after the nightly Kaggle update).")  # fmt: skip
    fig = go.Figure(go.Bar(
        y=games.label + "  ·  " + games.when, x=games.home_win_prob - 0.5, orientation="h",
        base=0.5, marker_cornerradius=4,
        marker_color=[t["series"][0] if v >= 0.5 else t["series"][1]
                      for v in games.home_win_prob],
        text=[f"home {p:.0%} · {m:+.1f}" for p, m in zip(games.home_win_prob,
                                                         games.pred_margin, strict=True)],
        textposition="outside", textfont={"size": 11, "color": t["ink2"]}, cliponaxis=False,
        customdata=games[["game_id"]].to_numpy(),
        hovertemplate="%{y}<br>home win chance %{text}<extra></extra>",
    ))  # fmt: skip
    fig.add_vline(x=0.5, line_color=t["muted"], line_width=1)
    C.base_layout(fig, 30 * len(games) + 70, yaxis={"autorange": "reversed"},
                  xaxis={"range": [0, 1], "tickformat": ".0%",
                         "title": "home team's chance to win"})  # fmt: skip
    ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                         key="u_strip")  # fmt: skip
    st.caption("Blue: home team favored; orange: away team favored. Number after the dot: "
               "predicted home margin. **Click a game** to open its preview.")  # fmt: skip
    pick = C.selected(ev)
    gid = (
        pick
        if pick in set(games.game_id)
        else st.session_state.get("u_game", games.game_id.iloc[0])
    )
    st.session_state["u_game"] = gid
    g = games[games.game_id == gid].iloc[0]
    home, away = int(g.home_team_id), int(g.away_team_id)

    st.subheader(f"{team_label(away, season)} at {team_label(home, season)}, {g.when}")
    c = st.columns(4)
    fav = home if g.pred_margin >= 0 else away
    c[0].metric("Favorite", short_team(fav, season), f"{abs(g.pred_margin):.1f} points")
    c[1].metric("Home win chance", f"{g.home_win_prob:.0%}")
    c[2].metric("Expected possessions", f"{g.pace:.0f}")
    c[3].metric("Form vs roster view", f"{g.net_form:+.1f} / {g.net_roster:+.1f}",
                help="Home net rating edge per 100 from team form (opponent-adjusted, recent "
                "games weighted) and from the roster's per-possession impact over expected "
                "minutes. The prediction blends them.")  # fmt: skip

    tm = D.q(con, "select * from forecast.team where game_id = ?", [gid])
    cols = st.columns(2)
    for col, team in zip(cols, (away, home), strict=True):
        d = tm[tm.team_id == team].set_index("measure")
        with col:
            st.markdown(f"**{team_label(team, season)}**")
            axes = [m for m in DIMS if m in d.index]
            st.plotly_chart(C.radar([D.LABELS[m] for m in axes],
                                    {"predicted": [d.pred_rating.get(m) for m in axes],
                                     "season average": [d.season_avg.get(m) for m in axes]},
                                    mode),
                            width="stretch", key=f"u_radar_{team}")  # fmt: skip
    a, h = tm[tm.team_id == away].set_index("measure"), tm[tm.team_id == home].set_index("measure")
    parts = [m for m in D.ordered(h.index) if m in a.index]
    st.markdown("**Where each side has the edge** (predicted rating, home minus away)")
    st.plotly_chart(C.diverging_bars([D.LABELS.get(m) or D.COMPONENT_LABELS.get(m, m)
                                      for m in parts],
                                     [h.pred_rating.get(m) - a.pred_rating.get(m) for m in parts],
                                     mode, f"blue: {short_team(home, season)} better; red: "
                                     f"{short_team(away, season)} better"),
                    width="stretch", key="u_edges")  # fmt: skip
    st.caption(
        "Each team's prediction combines its recent form (recent games weighted more) "
        "with what this opponent does to teams on that measure, and home court."
    )

    st.subheader("Players")
    pl = D.q(con, "select * from forecast.players where game_id = ? order by exp_minutes desc",
             [gid]).merge(players, on="person_id", how="left")  # fmt: skip
    pcols = st.columns(2)
    for col, team in zip(pcols, (away, home), strict=True):
        sub = pl[pl.team_id == team].copy()
        if sub.empty:
            continue
        sub["row"] = [f"{n}  ({m:.0f} min)" for n, m in zip(sub.player, sub.exp_minutes,
                                                             strict=True)]  # fmt: skip
        cols_r = [f"{m}_rating" for m in DIMS if f"{m}_rating" in sub.columns]
        with col:
            st.markdown(f"**{short_team(team, season)}**: expected minutes and ratings")
            st.plotly_chart(C.rating_heatmap(sub, "row", cols_r,
                                             [D.LABELS[c[:-7]] for c in cols_r], mode),
                            width="stretch", key=f"u_heat_{team}")  # fmt: skip
            imp = sub.dropna(subset=["impact"]).head(10)
            fig = go.Figure(go.Bar(
                y=imp.player, x=imp.impact * imp.exp_minutes / 48, orientation="h",
                marker_color=[t["good"] if v > 0 else t["bad"] for v in imp.impact],
                marker_cornerradius=4, customdata=imp[["player"]].to_numpy(),
                hovertemplate="%{y}: %{x:+.2f} points per 100 to the team<extra></extra>",
            ))  # fmt: skip
            C.base_layout(fig, 24 * len(imp) + 60, yaxis={"autorange": "reversed"},
                          xaxis={"title": "expected contribution to team net per 100"})  # fmt: skip
            ev = st.plotly_chart(fig, width="stretch", on_select="rerun",
                                 selection_mode="points", key=f"u_imp_{team}")  # fmt: skip
            if (p := C.selected(ev)) is not None:
                goto("Player", clear=f"u_imp_{team}", p_name=p, p_season=season)
    st.caption("Expected minutes: recent games, weighted toward the latest, for players who "
               "played in one of the team's last three games (no injury feed). Ratings: recent "
               "form shrunk toward his season level, plus the opponent's effect. Bars: impact "
               "x share of minutes. **Click a player** to open him.")  # fmt: skip

    with st.expander("How good are these predictions? (backtest)"):
        bt = D.q(con, "select * from forecast.backtest_games")
        bt["model"] = bt.blend_form.map(
            lambda b: (
                "always pick the home team"
                if pd.isna(b)
                else f"{b:.0%} team form + {1 - b:.0%} roster"
            )
        )
        st.dataframe(bt[["model", "margin_mae", "accuracy", "brier", "games"]].round(3),
                     hide_index=True)  # fmt: skip
        tb = D.q(con, "select * from forecast.backtest_team order by measure")
        tb["measure"] = tb.measure.map(lambda m: D.LABELS.get(m) or D.COMPONENT_LABELS.get(m, m))
        st.dataframe(tb.round(2), hide_index=True)
        st.caption(
            "Last season re-predicted week by week from earlier games only. Margin "
            "error in points; Brier: lower is better (0.25 = coin flip). Team measures: "
            "average error of the prediction vs the team's season-to-date average."
        )
