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


STATS = {"pts": "Points", "reb": "Rebounds", "ast": "Assists", "fg3m": "Threes",
         "stocks": "Steals + blocks", "tov": "Turnovers"}  # fmt: skip


def _has_forecast(con) -> bool:
    return bool(D.q(con, "select count(*) n from information_schema.tables "
                    "where table_schema = 'forecast'").n.iloc[0])  # fmt: skip


def _has_table(con, schema: str, table: str) -> bool:
    return bool(D.q(con, "select count(*) n from information_schema.tables where "
                    "table_schema = ? and table_name = ?", [schema, table]).n.iloc[0])  # fmt: skip


def _why(con, g, home: str, away: str, mode: str) -> None:
    """The predicted margin split into what each ingredient of the tested model adds."""
    mm = D.q(con, "select feature, beta from forecast.margin_model "
             "where model <> 'player intervals'")  # fmt: skip
    b = dict(zip(mm.feature, mm.beta, strict=True))
    parts = {"team form": b.get("form", 0.0) * g.form,
             "roster": b.get("roster", 0.0) * g.roster,
             "rest": sum(b.get(f, 0.0) * g[f] for f in ("b2b_h", "b2b_a", "rest_diff")),
             "constant": b.get("intercept", 0.0)}  # fmt: skip
    st.plotly_chart(C.diverging_bars(list(parts), list(parts.values()), mode,
                                     f"points: blue toward {home}, red toward {away}",
                                     height=170),
                    width="stretch", key="u_why")  # fmt: skip
    notes = []
    if g.b2b_h:
        notes.append(f"{home} on the second night of a back-to-back")
    if g.b2b_a:
        notes.append(f"{away} on the second night of a back-to-back")
    st.caption(
        "Why this margin: team form (opponent-adjusted, recent games weighted) and the roster "
        "(last season's per-possession impact of tonight's expected players), both including "
        "home court, plus rest, weighted as the walk-forward test found best. "
        + (" · ".join(notes) + "." if notes else "")
    )


def _coaches_line(con, home: int, away: int, season: int, short_team) -> None:
    if not _has_table(con, "coach", "h2h"):
        return
    ch = D.q(con, """select team_id, arg_max(coach_id, game_id) as coach_id,
                            arg_max(coach_name, game_id) as coach, bool_or(assumed) as assumed
                     from coach.assignments where team_id in (?, ?)
                       and season = (select max(season) from coach.assignments)
                     group by team_id""", [home, away]).set_index("team_id")  # fmt: skip
    if home not in ch.index or away not in ch.index:
        return
    hc, ac = ch.loc[home], ch.loc[away]
    hh = D.q(con, "select * from coach.h2h where coach_id = ? and opp_coach_id = ?",
             [hc.coach_id, ac.coach_id])  # fmt: skip
    rec = ""
    if not hh.empty:
        r = hh.iloc[0]
        rec = (f" Head to head: {hc.coach.split()[-1]} {int(r.wins)}-{int(r.games - r.wins)}, "
               f"average margin {r.margin:+.1f}, tempo control {r.tempo_control:+.1f} "
               "possessions (history alone hasn't predicted the next meeting).")  # fmt: skip
    flag = " (last season's coach; this season's records aren't published yet)" \
        if hc.assumed or ac.assumed else ""  # fmt: skip
    st.caption(f"Coaches: {ac.coach} ({short_team(away, season)}) vs {hc.coach} "
               f"({short_team(home, season)}){flag}.{rec} More in **Coaches**.")  # fmt: skip


def _stat_lines(con, gid, home, away, season, short_team, players, mode) -> None:
    if not _has_table(con, "forecast", "player_lines"):
        return
    pl = D.q(con, "select * from forecast.player_lines where game_id = ?", [gid])
    if pl.empty:
        return
    pl = pl.merge(players, on="person_id", how="left")
    t = C.THEME[mode]
    st.subheader("Stat lines")
    stat = st.radio("Stat", list(STATS), format_func=STATS.get, horizontal=True,
                    key="u_stat", label_visibility="collapsed")  # fmt: skip
    cols = st.columns(2)
    for k, (col, team) in enumerate(zip(cols, (away, home), strict=True)):
        sub = pl[(pl.team_id == team) & (pl.exp_minutes >= 8)].sort_values(stat)
        if sub.empty:
            continue
        color = t["series"][1 - k]
        fig = go.Figure(go.Scatter(
            x=sub[stat], y=sub.player, mode="markers",
            marker={"size": 10, "color": color, "line": {"width": 2, "color": t["mid"]}},
            error_x={"type": "data", "symmetric": False, "array": sub[f"{stat}_hi"] - sub[stat],
                     "arrayminus": sub[stat] - sub[f"{stat}_lo"], "color": C.rgba(color, 0.45),
                     "thickness": 6, "width": 0},
            customdata=sub[[f"{stat}_lo", f"{stat}_hi", "exp_minutes"]].to_numpy(),
            hovertemplate="%{y}: %{x:.1f} (80%: %{customdata[0]:.0f}-%{customdata[1]:.0f}), "
            "%{customdata[2]:.0f} min<extra></extra>",
        ))  # fmt: skip
        C.base_layout(fig, 26 * len(sub) + 60, xaxis={"title": STATS[stat], "rangemode": "tozero"},
                      yaxis={"fixedrange": True})  # fmt: skip
        with col:
            st.markdown(f"**{short_team(team, season)}**")
            st.plotly_chart(fig, width="stretch", key=f"u_lines_{team}")
    st.caption("Dot: expected; bar: the range that held 80% of real outcomes in seasons the "
               "model never saw. From recent per-minute production (shrunk toward last "
               "season), minutes when he plays, and the game's expected pace. Players expected "
               "to play 8+ minutes.")  # fmt: skip


def _accuracy(con, mode: str) -> None:
    """Walk-forward accuracy: what each ingredient adds, calibration, player lines."""
    t = C.THEME[mode]
    st.subheader("How good are these predictions?")
    ev = D.q(con, "select * from forecast.wf_eval where season = 0")
    order = ["home court only", "team form", "form + roster", "+ rest", "+ coach", "+ matchups"]
    ev = ev.set_index("model").reindex([m for m in order if m in set(ev.model)]).reset_index()
    best = D.q(con, "select any_value(model) m from forecast.margin_model "
               "where model <> 'player intervals'").m.iloc[0]  # fmt: skip
    n = int(ev.games.iloc[0])
    a, b = st.columns(2)
    panels = (
        (a, "accuracy", "winners picked", ".1%"),
        (b, "log_loss", "log loss (lower is better)", ".4f"),
    )
    for col, metric, title, fmt in panels:
        fig = go.Figure(go.Bar(
            x=ev.model, y=ev[metric], marker_cornerradius=4,
            marker_color=[t["good"] if m == best else t["bar"] for m in ev.model],
            text=[format(v, fmt) for v in ev[metric]], textposition="outside",
            hovertemplate="%{x}: %{text}<extra></extra>",
        ))  # fmt: skip
        lo, hi = ev[metric].min(), ev[metric].max()
        pad = (hi - lo) * 0.6 or 0.01
        C.base_layout(fig, 300, yaxis={"range": [lo - pad, hi + pad], "title": title,
                                       "tickformat": fmt, "fixedrange": True},
                      xaxis={"fixedrange": True})  # fmt: skip
        with col:
            st.plotly_chart(fig, width="stretch", key=f"u_acc_{metric}")
    st.caption(f"{n:,} games, 2021-22 to 2025-26, each predicted from earlier games only, a week "
               "at a time; each step adds one ingredient. Blue: the model in use. Coaching "
               "history and style matchups made predictions worse, so they are left "
               "out.")  # fmt: skip

    a, b = st.columns(2)
    with a:
        cal = D.q(con, "select * from forecast.wf_calibration order by bin")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", hoverinfo="skip",
                                 line={"dash": "dash", "color": t["muted"], "width": 1},
                                 showlegend=False))  # fmt: skip
        fig.add_trace(go.Scatter(
            x=cal.predicted, y=cal.actual, mode="markers+lines", showlegend=False,
            marker={"size": 10, "color": t["series"][0], "line": {"width": 2, "color": t["mid"]}},
            line={"color": t["series"][0], "width": 2}, customdata=cal[["games"]].to_numpy(),
            hovertemplate="predicted %{x:.0%} → won %{y:.0%} (%{customdata[0]} games)"
            "<extra></extra>",
        ))  # fmt: skip
        C.base_layout(fig, 320, xaxis={"title": "predicted home win chance", "range": [0, 1],
                                       "tickformat": ".0%"},
                      yaxis={"title": "how often the home team won", "range": [0, 1],
                             "tickformat": ".0%"})  # fmt: skip
        st.markdown("**Win chances mean what they say**")
        st.plotly_chart(fig, width="stretch", key="u_cal")
    with b:
        pe = D.q(con, "select * from forecast.wf_player_eval where stat in "
                 "('pts','reb','ast','fg3m','stocks','tov')")  # fmt: skip
        pe["label"] = pe.stat.map(STATS)
        fig = go.Figure()
        for k, (c, name) in enumerate((("model_mae", "courtvision"),
                                       ("season_avg_mae", "season average so far"))):  # fmt: skip
            fig.add_trace(go.Bar(
                x=pe.label, y=pe[c], name=name, marker_cornerradius=4,
                marker_color=t["series"][0] if k == 0 else t["bar"],
                hovertemplate="%{x}: off by %{y:.2f} on average<extra>" + name + "</extra>",
            ))  # fmt: skip
        C.base_layout(fig, 320, barmode="group", legend={"orientation": "h", "y": 1.1},
                      yaxis={"title": "average miss per player-game"})  # fmt: skip
        st.markdown("**Player stat lines vs the season average**")
        st.plotly_chart(fig, width="stretch", key="u_pl_acc")
    cov = D.q(con, "select avg(coverage_80) c from forecast.wf_player_eval "
              "where coverage_80 is not null").c.iloc[0]  # fmt: skip
    better = pe[pe.model_mae < pe.season_avg_mae - 0.005].label.str.lower().tolist()
    tied = pe[(pe.model_mae - pe.season_avg_mae).abs() <= 0.005].label.str.lower().tolist()
    st.caption(
        (
            f"Player lines beat the season-to-date average on {', '.join(better)}"
            if better
            else "Player lines match the season-to-date average"
        )
        + (f" and tie it on {', '.join(tied)}" if tied else "")
        + f"; the 80% ranges held {cov:.0%} of outcomes in 2023-24 to 2025-26 (fitted on "
        "the two seasons before). "
        "Matchup adjustments for player lines (opponent's defense by position, pace) were "
        "tested and add nothing measurable."
    )


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
    has_wf = "rest_h" in games.columns and pd.notna(g.get("rest_h"))
    c = st.columns(4)
    fav = home if g.pred_margin >= 0 else away
    c[0].metric("Favorite", short_team(fav, season), f"{abs(g.pred_margin):.1f} points")
    c[1].metric("Home win chance", f"{g.home_win_prob:.0%}")
    c[2].metric("Expected possessions", f"{g.pace:.0f}")
    if has_wf:
        c[3].metric("Days of rest", f"{int(g.rest_a)} / {int(g.rest_h)}",
                    help=f"{short_team(away, season)} / {short_team(home, season)}: days since "
                    "each team's last game (3 = three or more). 1 = second night of a "
                    "back-to-back.")  # fmt: skip
        _why(con, g, short_team(home, season), short_team(away, season), mode)
    else:
        c[3].metric("Form vs roster view", f"{g.net_form:+.1f} / {g.net_roster:+.1f}",
                    help="Home net rating edge per 100 from team form and from the roster's "
                    "per-possession impact over expected minutes.")  # fmt: skip
    _coaches_line(con, home, away, season, short_team)
    _stat_lines(con, gid, home, away, season, short_team, players, mode)

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

    if _has_table(con, "forecast", "wf_eval"):
        _accuracy(con, mode)
        return
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
