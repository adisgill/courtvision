"""Coaches view (``courtvision coaching`` + ``courtvision walkforward``).

What a head coach controls, measured from play-by-play: timeouts that stop runs, plays
out of timeouts, tempo control, rotation habits, clutch and halftime results, and the
team's margin against what its roster and the pre-game prediction expected. Every metric
is tested first: does it repeat next season, and does it follow the coach to a new team?
Then one coach's profile, and any two coaches head to head.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from courtvision.app import charts as C
from courtvision.app import data as D

# metric: (label, what it measures, direction: +1 higher is better, -1 lower, 0 style)
METRICS = {
    "run_stop_pp100": ("Timeouts that stop runs", "opponent points per 100 possessions "
                       "fewer in the 3 possessions after his timeout than before, vs league", 1),
    "ato_pp100_rel": ("After-timeout scoring", "points per 100 plays out of his timeouts, "
                      "vs league", 1),
    "ato_three_rel": ("After-timeout threes", "share of plays out of timeouts that end in a "
                      "three, vs league", 0),
    "ato_tov_rel": ("After-timeout turnovers", "share of plays out of timeouts that end in a "
                    "turnover, vs league", -1),
    "timeouts_pg": ("Timeouts per game", "timeouts called per game", 0),
    "pace": ("Pace", "possessions per game vs that season's league average", 0),
    "fg3a_rate_rel": ("3-point rate", "share of shots that are threes, vs league", 0),
    "tempo_control": ("Tempo control", "how much closer games are played to his usual pace "
                      "than the opponent's (possessions)", 1),
    "starters_min_blowout": ("Starters' minutes in blowouts", "average minutes of the top "
                             "five in games decided by 20+", 0),
    "rotation": ("Rotation depth", "players with 10+ minutes vs league", 0),
    "transition_share": ("Transition share", "share of live-ball possessions used in the "
                         "first 8 seconds", 0),
    "transition_pp100_rel": ("Transition scoring", "points per 100 early possessions after "
                             "a miss or steal, vs league", 1),
    "clutch_net100": ("Clutch net", "net points per 100 possessions in the last 5 minutes "
                      "within 5 points", 1),
    "halftime_adj": ("Halftime adjustment", "third-quarter net minus first-half net, per "
                     "100", 1),
    "bounce_back": ("Bounce-back", "net per 100 after a loss minus after a win", 1),
    "challenges_pg": ("Challenges per game", "coach's challenges per game (2019-20 on)", 0),
    "net100": ("Team net", "net points per 100 possessions", 1),
    "vs_roster": ("Beats roster expectation", "points per game above what last season's "
                  "player impact over expected minutes predicted", 1),
    "vs_pred": ("Beats the prediction", "points per game above the walk-forward pre-game "
                "prediction", 1),
}  # fmt: skip
RESULTS = ["run_stop_pp100", "ato_pp100_rel", "tempo_control", "clutch_net100",
           "halftime_adj", "vs_roster_shrunk", "net100"]  # fmt: skip


def _label(m: str) -> str:
    return METRICS.get(m.removesuffix("_shrunk"), (m,))[0]


def _has(con, table: str) -> bool:
    return bool(D.q(con, "select count(*) n from information_schema.tables where "
                    "table_schema = 'coach' and table_name = ?", [table]).n.iloc[0])  # fmt: skip


def render(con, team_label, mode: str, goto) -> None:
    if not _has(con, "profiles"):
        st.warning("No coaching data yet. Run `courtvision ingest coaches` then "
                   "`courtvision coaching`.")  # fmt: skip
        return
    t = C.THEME[mode]
    prof = D.q(con, "select * from coach.profiles")
    prof["team_ids"] = prof.team_ids.map(list)
    # pace against that season's league average (eras differ by ten possessions)
    seas = prof.season > 0
    prof.loc[seas, "pace"] = prof[seas].pace - prof[seas].groupby("season").pace.transform("mean")
    has_val = _has(con, "value")
    if has_val:
        val = D.q(con, "select coach_id, season, vs_roster, vs_pred, vs_roster_shrunk, "
                  "vs_pred_shrunk from coach.value")  # fmt: skip
        prof = prof.merge(val, on=["coach_id", "season"], how="left")
    rel = D.q(con, "select * from coach.value_reliability") if has_val else pd.DataFrame()

    # ---- 1. which coach metrics are real? ------------------------------------------------
    st.subheader("What does a coach actually control?")
    if not rel.empty:
        r = rel.dropna(subset=["same_coach_corr"]).copy()
        r = r[r.metric.isin(METRICS)].sort_values("new_team_corr", na_position="first")
        r["label"] = r.metric.map(_label)
        fig = go.Figure()
        for k, (col, n, name) in enumerate((
                ("same_coach_corr", "same_coach_pairs", "same coach, next season"),
                ("new_team_corr", "new_team_pairs", "after he changes teams"))):  # fmt: skip
            fig.add_trace(go.Bar(
                y=r.label, x=r[col], orientation="h", name=name, marker_cornerradius=4,
                marker_color=t["series"][k], customdata=r[[n]].to_numpy(),
                hovertemplate="%{y}: r = %{x:.2f} (%{customdata[0]} coach-seasons)<extra>"
                + name + "</extra>",
            ))  # fmt: skip
        fig.add_vline(x=0, line_color=t["muted"], line_width=1)
        C.base_layout(fig, 26 * len(r) * 2 + 80, barmode="group", bargap=0.25,
                      yaxis={"fixedrange": True}, legend={"orientation": "h", "y": 1.06},
                      xaxis={"range": [-0.5, 1], "title": "season-to-season correlation",
                             "fixedrange": True})  # fmt: skip
        st.plotly_chart(fig, width="stretch", key="c_persist")
        h2h = rel[rel.metric == "h2h_vs_pred"]
        h2h_txt = (f" Head-to-head history says nothing about the next meeting either: "
                   f"r = {h2h.year_to_year.iloc[0]:.3f} over {int(h2h.year_pairs.iloc[0]):,} "
                   "games." if not h2h.empty else "")  # fmt: skip
        st.caption(
            "Each metric for the same coach in consecutive seasons (40+ games each). The "
            "orange bars are the real test: when a coach moves to a new team with new players, "
            "does the number come with him? Long orange bars are coaching; bars near zero "
            "are the roster or noise. Only about 40 moves exist since 1996-97, so treat "
            "orange bars under about 0.3 as unproven." + h2h_txt
        )

    # ---- 2. the season's coaches ---------------------------------------------------------
    seasons = sorted(prof[prof.season > 0].season.unique(), reverse=True)
    # default: the latest season far enough along to read (60+ games for some coach)
    full = [s for s in seasons if prof[prof.season == s].games.max() >= 60
            and not prof[prof.season == s].assumed.any()]  # fmt: skip
    default = seasons.index(full[0]) if full else 0
    season = st.selectbox("Season", seasons, index=default, key="c_season",
                          format_func=lambda s: f"{s}-{(s + 1) % 100:02d}")  # fmt: skip
    cur = prof[(prof.season == season) & (prof.games >= 10)].copy()
    if cur.assumed.any():
        st.info(
            "This season's coaches aren't in the source records yet: each team is shown "
            "with the coach who finished last season, so a summer hire is credited to "
            "his predecessor. `courtvision ingest coaches --fetch-current` fixes it."
        )
    elif season == seasons[0] and _has(con, "current"):
        as_of = D.q(con, "select max(as_of) d from coach.current").d.iloc[0]
        if as_of:
            st.caption(f"This season's coaches from Basketball-Reference's season page of "
                       f"{as_of}. Early-season numbers rest on few games.")  # fmt: skip
    cur["pace_rel"] = cur.pace
    cur["team"] = cur.team_ids.map(lambda ids: ", ".join(team_label(i, season).split()[-1]
                                                         for i in ids))  # fmt: skip
    left, right = st.columns([1, 1])
    with left:
        st.markdown("**Style map**: pace and 3-point rate against league average")
        fig = go.Figure(go.Scatter(
            x=cur.pace_rel, y=100 * cur.fg3a_rate_rel, mode="markers+text",
            text=cur.coach_name.str.split().str[-1], textposition="top center",
            textfont={"size": 10, "color": t["ink2"]},
            marker={"size": 11, "color": t["series"][0], "line": {"width": 2, "color": t["mid"]}},
            customdata=cur[["coach_id", "coach_name", "team"]].to_numpy(),
            hovertemplate="%{customdata[1]} (%{customdata[2]})<br>pace %{x:+.1f}<br>"
            "3-point rate %{y:+.1f} points<extra></extra>",
        ))  # fmt: skip
        fig.add_hline(y=0, line_color=t["muted"], line_width=1)
        fig.add_vline(x=0, line_color=t["muted"], line_width=1)
        C.base_layout(fig, 430, xaxis={"title": "possessions per game vs league"},
                      yaxis={"title": "3-point share of shots vs league (points)"})  # fmt: skip
        ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                             key="c_style")  # fmt: skip
        # a chart selection persists across reruns: act only when it changes
        if (p := C.selected(ev)) is not None and p != st.session_state.get("_c_style_last"):
            st.session_state["c_coach"] = p
        st.session_state["_c_style_last"] = p
    with right:
        st.markdown("**Results** (blue better than league, red worse)")
        cols = [c for c in RESULTS if c in cur.columns and cur[c].notna().any()]
        z = cur[cols].copy()
        for c in cols:
            sd = z[c].std()
            z[c] = (z[c] - z[c].mean()) / sd * 10 + 50 if sd else 50
        cur_sorted = cur.assign(_z=z.mean(axis=1)).sort_values("_z", ascending=False)
        zs = z.loc[cur_sorted.index]
        fig = go.Figure(go.Heatmap(
            z=zs.to_numpy(float), x=[_label(c) for c in cols],
            y=cur_sorted.coach_name.tolist(),
            text=[["" if pd.isna(v) else f"{v:+.1f}" for v in row]
                  for row in cur_sorted[cols].to_numpy(float)], texttemplate="%{text}",
            colorscale=C.scale(mode), zmin=25, zmax=75, zmid=50, xgap=2, ygap=2,
            showscale=False, hovertemplate="%{y} · %{x}: %{text}<extra></extra>",
        ))  # fmt: skip
        C.base_layout(fig, 22 * len(cur_sorted) + 90,
                      xaxis={"side": "top", "fixedrange": True, "tickangle": -30},
                      yaxis={"autorange": "reversed", "fixedrange": True})  # fmt: skip
        st.plotly_chart(fig, width="stretch", key="c_results")
    st.caption("**Click a coach** on the style map to open his profile. Results are shrunk "
               "toward league average by sample size; 'beats roster expectation' covers "
               "2020-21 to 2025-26.")  # fmt: skip

    # ---- 3. one coach ----------------------------------------------------------------------
    career = prof[prof.season == 0].sort_values("games", ascending=False)
    names = dict(zip(career.coach_id, career.coach_name, strict=True))
    ids = list(names)
    if st.session_state.get("c_coach") not in ids:
        st.session_state["c_coach"] = cur.sort_values("games").coach_id.iloc[-1] \
            if not cur.empty else ids[0]  # fmt: skip
    cid = st.selectbox("Coach", ids, format_func=lambda i: names[i], key="c_coach")
    c = career[career.coach_id == cid].iloc[0]
    hist = prof[(prof.coach_id == cid) & (prof.season > 0)].sort_values("season")
    st.subheader(names[cid])
    m = st.columns(4)
    m[0].metric("Games coached (since 1996-97)", f"{int(c.games):,}")
    m[1].metric("Record", f"{int(c.wins)}-{int(c.games - c.wins)}")
    m[2].metric("Seasons", f"{len(hist)}")
    m[3].metric("Team net per 100", f"{c.net100:+.1f}")

    a, b = st.columns(2)
    with a:
        st.markdown(
            "**Career profile** vs every coach with 2+ seasons (10 = one standard deviation)"
        )
        pool = career[career.games >= 164]
        traits = [k for k, v in METRICS.items() if k in pool.columns and v[2] != 0
                  and pd.notna(c.get(k))]  # fmt: skip
        vals = [(c[k] - pool[k].mean()) / pool[k].std() * METRICS[k][2] * 10 for k in traits]
        st.plotly_chart(C.diverging_bars([METRICS[k][0] for k in traits], vals, mode,
                                         "rating points vs average coach"),
                        width="stretch", key="c_traits")  # fmt: skip
    with b:
        st.markdown("**Out of his timeouts**: what the play ends in, vs league")
        mix = {"three": c.ato_three_rel, "free throws": c.ato_ft_rel,
               "turnover": c.ato_tov_rel}  # fmt: skip
        fig = go.Figure(go.Bar(
            x=list(mix), y=[100 * v for v in mix.values()], marker_cornerradius=4,
            marker_color=[t["series"][0], t["series"][0],
                          t["bad"] if c.ato_tov_rel > 0 else t["good"]],
            text=[f"{100 * v:+.1f}" for v in mix.values()], textposition="outside",
            hovertemplate="%{x}: %{y:+.1f} percentage points vs league<extra></extra>",
        ))  # fmt: skip
        fig.add_hline(y=0, line_color=t["muted"], line_width=1)
        C.base_layout(fig, 300, yaxis={"title": "percentage points vs league"})
        st.plotly_chart(fig, width="stretch", key="c_ato")
        st.caption(f"{int(c.ato_plays):,} plays out of timeouts; scoring on them "
                   f"{c.ato_pp100_rel:+.1f} points per 100 vs league (shrunk).")  # fmt: skip

    pick = st.selectbox("Season by season", [k for k in METRICS if k in hist.columns],
                        format_func=_label, key="c_metric")  # fmt: skip
    h = hist.dropna(subset=[pick])
    sign = METRICS[pick][2]
    fig = go.Figure(go.Bar(
        x=[f"{s}-{(s + 1) % 100:02d}" for s in h.season], y=h[pick], marker_cornerradius=4,
        marker_color=[t["series"][0] if sign == 0 else
                      (t["good"] if v * sign > 0 else t["bad"]) for v in h[pick]],
        customdata=h[["games"]].to_numpy(),
        hovertemplate="%{x}: %{y:.2f} (%{customdata[0]} games)<extra></extra>",
    ))  # fmt: skip
    fig.add_hline(y=0, line_color=t["muted"], line_width=1)
    C.base_layout(fig, 280, yaxis={"title": _label(pick)})
    st.plotly_chart(fig, width="stretch", key="c_hist")
    st.caption(f"{_label(pick)}: {METRICS[pick][1]}.")

    # ---- 4. coach vs coach -----------------------------------------------------------------
    if not _has(con, "h2h"):
        return
    st.subheader("Coach vs coach")
    opp = D.q(con, "select opp_coach_id, games from coach.h2h where coach_id = ? "
              "order by games desc", [cid])  # fmt: skip
    if opp.empty:
        st.info("No meetings recorded.")
        return
    oid = st.selectbox("Against", opp.opp_coach_id.tolist(),
                       format_func=lambda i: f"{names.get(i, i)}", key="c_opp")  # fmt: skip
    hh = D.q(con, "select * from coach.h2h where coach_id = ? and opp_coach_id = ?",
             [cid, oid]).iloc[0]  # fmt: skip
    m = st.columns(4)
    m[0].metric("Record", f"{int(hh.wins)}-{int(hh.games - hh.wins)}")
    m[1].metric("Average margin", f"{hh.margin:+.1f}")
    m[2].metric(
        "Tempo control",
        f"{hh.tempo_control:+.1f}",
        help="Possessions closer to his usual pace than to the opponent's, per game.",
    )
    m[3].metric("Vs prediction", "n/a" if pd.isna(hh.vs_pred) else f"{hh.vs_pred:+.1f}",
                help=f"Margin against the walk-forward prediction, {int(hh.vs_pred_games)} "
                "games (2020-21 on).")  # fmt: skip
    games = D.q(con, """select g.game_date, tg.pts - tg.opp_pts as margin, t.control
                        from coach.tempo t join coach.team_games tg using (game_id, team_id)
                        join stg.games g using (game_id)
                        where t.coach_id = ? and t.opp_coach_id = ? order by g.game_date""",
                [cid, oid])  # fmt: skip
    fig = go.Figure(go.Bar(
        x=pd.to_datetime(games.game_date), y=games.margin, marker_cornerradius=2,
        marker_color=[t["good"] if v > 0 else t["bad"] for v in games.margin],
        customdata=games[["control"]].to_numpy(),
        hovertemplate="%{x|%b %d, %Y}: %{y:+d}<br>tempo control %{customdata[0]:+.1f}"
        "<extra></extra>",
    ))  # fmt: skip
    fig.add_hline(y=0, line_color=t["muted"], line_width=1)
    C.base_layout(fig, 260, yaxis={"title": f"{names[cid].split()[-1]}'s margin"})
    st.plotly_chart(fig, width="stretch", key="c_h2h")
    st.caption("Every meeting, regular season. Blue: his team won. A record between two "
               "coaches mostly reflects their rosters at the time; it did not predict the next "
               "meeting beyond the model.")  # fmt: skip
