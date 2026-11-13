"""Lineups view (``courtvision lineups``): the fives a team uses, what to expect from them,
the best fives it could play, which pairs work together, and who closes games.

Every number that ranks lineups is a projection: what the five players do individually
(per-possession impact, against the fives they faced) plus the lineup's own result beyond
that, shrunk by how little it repeats. A lineup's raw plus-minus is shown but never
ranked on: it predicted later games worse than assuming league average.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from courtvision import lineups as L
from courtvision.app import charts as C
from courtvision.app import data as D

LEAGUE, OWN = "league average (0)", "lineup's own first-half net"
PLAYERS, PROJ = "its players' impacts (last season)", "projection: players + shrunk synergy"


def _names(players: pd.DataFrame) -> pd.Series:
    return players.set_index("person_id").player


def five_label(ids, names: pd.Series) -> str:
    return " · ".join(str(names.get(int(i), i)).split()[-1] for i in ids)


def roster(con, team: int, season: int) -> pd.DataFrame:
    """Players with minutes for the team this season: impact, position group, and
    ``current``: still with the team (played in one of its last 15 games), so a five is
    never built from players traded for each other."""
    return D.q(con, """
        with g as (select c.person_id, c.minutes, c.game_id,
                          dense_rank() over (order by c.game_id desc) as rk
                   from metrics.player_game_counts c
                   where c.season = ? and c.team_id = ? and c.season_type = 'regular'),
        m as (select person_id, sum(minutes) as minutes, min(rk) <= 15 as current
              from g group by 1)
        select m.person_id, m.minutes, m.current, coalesce(i.impact, 0) as impact,
               case when p.is_center then 'B'
                    when p.is_guard and not coalesce(p.is_forward, false) then 'G'
                    else 'W' end as grp
        from m left join metrics.player_impact i on i.person_id = m.person_id and i.season = ?
        left join stg.players p on p.person_id = m.person_id
        order by m.minutes desc""", [season, team, season])  # fmt: skip


def render(con, team_label, players: pd.DataFrame, mode: str, goto) -> None:
    if not D.has_schema(con, "lineups"):
        st.warning("No lineups yet. Run `courtvision lineups`.")
        return
    t = C.THEME[mode]
    names = _names(players)
    sz = D.q(con, "select season, sum(poss) as poss from lineups.units group by 1 order by 1 desc")
    seasons = sz.season.tolist()
    # open on the latest season far enough along to read (half a season of possessions)
    full = sz[sz.poss >= 0.5 * sz.poss.max()].season
    c1, c2 = st.columns([1, 3])
    season = c1.selectbox("Season", seasons, key="lu_season",
                          index=seasons.index(int(full.iloc[0])) if len(full) else 0,
                          format_func=lambda s: f"{s}-{(s + 1) % 100:02d}")  # fmt: skip
    teams = D.q(con, "select distinct team_id from lineups.units where season = ?", [season])
    labels = {team_label(i, season): int(i) for i in teams.team_id}
    opts = sorted(labels)
    if st.session_state.get("lu_team") not in opts:
        st.session_state["lu_team"] = opts[0]
    name = c2.selectbox("Team", opts, key="lu_team")
    team = labels[name]
    tests = D.q(con, "select * from lineups.tests")
    sh = tests[tests.test == "split-half"].iloc[0]
    u = D.q(con, "select * from lineups.units where season = ? and team_id = ? "
            "order by poss desc", [season, team])  # fmt: skip
    u["label"] = u.players.map(lambda p: five_label(p, names))

    # ---- the fives they use ---------------------------------------------------------------
    st.subheader(f"{name}: the lineups they use")
    top = u.head(12).set_index("label")
    z = pd.DataFrame({
        "Minutes": 0.0, "Raw net": np.clip(top.net100 / 15, -1, 1),
        "From its players": np.clip(top.exp_net100 / 10, -1, 1),
        "Projected": np.clip(top.proj_net100 / 10, -1, 1), "Started": 0.0,
    }, index=top.index)  # fmt: skip
    text = pd.DataFrame({
        "Minutes": top.minutes.map(lambda v: f"{v:.0f}"),
        "Raw net": top.net100.map(lambda v: f"{v:+.1f}"),
        "From its players": top.exp_net100.map(lambda v: f"{v:+.1f}"),
        "Projected": top.proj_net100.map(lambda v: f"{v:+.1f}"),
        "Started": top.starts.map(lambda v: f"{int(v)}" if v else "–"),
    }, index=top.index)  # fmt: skip
    hover = text.copy()
    hover["Raw net"] = [f"{n:+.1f} per 100 over {p:.0f} possessions (offense {o:+.1f}, "
                        f"defense allows {d:+.1f})" for n, p, o, d in
                        zip(top.net100, top.poss, top.off100, top.def100, strict=True)]  # fmt: skip
    st.plotly_chart(C.heat_grid(z, text, mode, hover, row_px=28), width="stretch",
                    key="lu_units")  # fmt: skip
    st.caption(
        "Net points per 100 possessions against league average. *From its players*: what "
        "the five players' individual impacts expect against the lineups they faced. "
        "*Projected*: that, plus the lineup's own result beyond it, pulled toward it by "
        f"{sh.k_poss:,.0f} possessions of prior (a lineup's extra result barely repeats: "
        f"split-half r {sh.synergy_r:+.2f} to {sh.synergy_prev_r:+.2f}). *Started*: games "
        "this five started."
    )

    # ---- raw vs projected ----------------------------------------------------------------
    st.markdown("**Why a lineup's plus-minus misleads**: raw net (hollow) and projected "
                "(solid) for every lineup with 50+ possessions")  # fmt: skip
    v = u[u.poss >= 50]
    fig = go.Figure()
    xs, ys = [], []
    for r in v.itertuples():
        xs += [r.poss, r.poss, None]
        ys += [r.net100, r.proj_net100, None]
    fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", hoverinfo="skip", showlegend=False,
                             line={"color": t["grid"], "width": 1.5}))  # fmt: skip
    fig.add_trace(go.Scatter(
        x=v.poss, y=v.net100, mode="markers", name="raw net",
        marker={"size": 9, "color": "rgba(0,0,0,0)", "line": {"width": 2, "color": t["muted"]}},
        customdata=v[["label"]].to_numpy(),
        hovertemplate="%{customdata[0]}<br>raw %{y:+.1f} over %{x:.0f} possessions"
        "<extra></extra>",
    ))  # fmt: skip
    fig.add_trace(go.Scatter(
        x=v.poss, y=v.proj_net100, mode="markers", name="projected",
        marker={"size": 9, "color": [t["good"] if p > 0 else t["bad"] for p in v.proj_net100],
                "line": {"width": 1.5, "color": t["mid"]}},
        customdata=v[["label"]].to_numpy(),
        hovertemplate="%{customdata[0]}<br>projected %{y:+.1f}<extra></extra>",
    ))  # fmt: skip
    fig.add_hline(y=0, line_color=t["muted"], line_width=1)
    C.base_layout(fig, 380, xaxis={"type": "log", "title": "possessions together (log)"},
                  yaxis={"title": "net per 100 vs league"},
                  legend={"orientation": "h", "y": 1.08})  # fmt: skip
    st.plotly_chart(fig, width="stretch", key="lu_scatter")
    fw = tests[tests.test == "second half from first"].set_index("predictor")
    st.caption(
        "Small samples swing wildly; the projection pulls them to what the players support. "
        "Tested on every season's second half from its first: a lineup's own first-half net "
        f"missed by {fw.mae[OWN]:.1f} points per 100 (worse than guessing league average, "
        f"{fw.mae[LEAGUE]:.1f}); its players' impacts by {fw.mae[PLAYERS]:.1f}; the "
        f"projection by {fw.mae[PROJ]:.1f}."
    )

    # ---- best fives ----------------------------------------------------------------------
    ro = roster(con, team, season)
    ro = ro[(ro.minutes >= 100) & ro.current]
    best = L.best_fives(ro, n=5)
    if best:
        st.markdown("**The best fives they could play** (by the players' impacts; at least "
                    "one guard and one center)")  # fmt: skip
        played = u.set_index("lineup").poss
        imp_of = ro.set_index("person_id").impact
        rows = []
        for ids, imp in best:
            key = "-".join(str(i) for i in sorted(ids))
            order = sorted(ids, key=lambda i: -imp_of.get(i, 0.0))
            rows.append({"five": five_label(order, names), "expected": imp,
                         "together": float(played.get(key, 0.0))})  # fmt: skip
        b = pd.DataFrame(rows)
        fig = go.Figure(go.Bar(
            y=b.five, x=b.expected, orientation="h", marker_cornerradius=4,
            marker_color=[t["good"] if v > 0 else t["bad"] for v in b.expected],
            text=[f"{e:+.1f} · {'untried' if p < 50 else f'{p:.0f} possessions together'}"
                  for e, p in zip(b.expected, b.together, strict=True)],
            textposition="outside", textfont={"color": t["ink2"], "size": 11}, cliponaxis=False,
            hovertemplate="%{y}: %{x:+.1f} per 100 expected vs an average five<extra></extra>",
        ))  # fmt: skip
        C.base_layout(fig, 34 * len(b) + 60, yaxis={"autorange": "reversed", "fixedrange": True})
        span = [min(0, b.expected.min()) * 1.3, max(0, b.expected.max()) * 1.6]
        fig.update_layout(
            xaxis={
                "title": "expected net per 100 vs an average five",
                "range": span,
                "fixedrange": True,
            }
        )
        st.plotly_chart(fig, width="stretch", key="lu_best")
        untried = b[b.together < 50]
        st.caption(
            "Sum of the five players' per-possession impacts this season, from the players "
            "still on the roster (in one of the team's last 15 games, 100+ minutes). "
            + (
                f"**{len(untried)} of these {len(b)} have barely played together** — the "
                "players' records say they should work."
                if len(untried)
                else "The coach already uses these."
            )
        )

    # ---- pairs -----------------------------------------------------------------------------
    pk = tests[tests.test == "pairs split-half"].k_poss
    pairs_grid(con, team, season, names, mode, float(pk.iloc[0]) if len(pk) else np.nan)

    # ---- closers ---------------------------------------------------------------------------
    cl = u[u.clutch_poss > 0].sort_values("clutch_poss", ascending=False).head(5)
    if not cl.empty:
        st.markdown("**Who closes**: lineups in the clutch (last 5 minutes, within 5 points)")
        fig = go.Figure(go.Bar(
            y=cl.label, x=cl.clutch_poss, orientation="h", marker_cornerradius=4,
            marker_color=t["series"][0],
            text=[f"{p:.0f} possessions · projected {n:+.1f}" for p, n in
                  zip(cl.clutch_poss, cl.proj_net100, strict=True)],
            textposition="outside", textfont={"color": t["ink2"], "size": 11}, cliponaxis=False,
            customdata=cl[["clutch_net100"]].to_numpy(),
            hovertemplate="%{y}: %{x:.0f} clutch possessions, raw clutch net "
            "%{customdata[0]:+.1f}<extra></extra>",
        ))  # fmt: skip
        C.base_layout(fig, 34 * len(cl) + 60, yaxis={"autorange": "reversed", "fixedrange": True},
                      xaxis={"title": "clutch possessions", "fixedrange": True,
                             "range": [0, cl.clutch_poss.max() * 1.7]})  # fmt: skip
        st.plotly_chart(fig, width="stretch", key="lu_close")
        st.caption(
            "Projected net is the lineup's overall projection; clutch samples are far "
            "too small to rate a lineup on their own (raw clutch net in the hover)."
        )

    with st.expander("How far to trust lineup numbers"):
        show = tests.copy()
        st.dataframe(show[["test", "predictor", "lineups", "n_half_poss", "synergy_r",
                           "synergy_prev_r", "net_r", "k_poss", "r", "mae"]].round(3),
                     hide_index=True)  # fmt: skip
        st.caption(
            "Split-half: lineups with 50+ possessions in both odd and even games of a "
            "season. Synergy (net beyond the players) is measured against this season's "
            "impacts (biased low: fitted on the same games) and last season's (biased high: "
            "they miss this season's changes); both are near zero. Second half from first: "
            "how well each predictor anticipated a lineup's later games. Pairs: two players' "
            "net together relative to their team's."
        )


def pairs_grid(con, team: int, season: int, names: pd.Series, mode: str, k: float) -> None:
    pr = D.q(con, "select * from lineups.pairs where season = ? and team_id = ?", [season, team])
    if pr.empty:
        return
    solo = pr[pr.person_a == pr.person_b].sort_values("minutes", ascending=False).head(9)
    who = solo.person_a.tolist()
    lab = [str(names.get(int(p), p)).split()[-1] for p in who]
    z = pd.DataFrame(np.nan, index=lab, columns=lab)
    text = pd.DataFrame("", index=lab, columns=lab)
    hover = pd.DataFrame("", index=lab, columns=lab)
    look = pr.set_index(["person_a", "person_b"])
    for i, a in enumerate(who):
        for j, b in enumerate(who):
            key = (min(a, b), max(a, b))
            if key not in look.index or pd.isna(look.loc[key].rel_shrunk):
                continue
            r = look.loc[key]
            z.iloc[i, j] = np.clip(r.rel_shrunk / 6, -1, 1)
            text.iloc[i, j] = f"{r.rel_shrunk:+.1f}"
            what = "on the floor" if a == b else "together"
            hover.iloc[i, j] = (f"{what}: {r.poss:.0f} possessions, raw {r.rel:+.1f} vs the team's "
                                f"net, shrunk {r.rel_shrunk:+.1f}")  # fmt: skip
    st.markdown("**Pairs**: net per 100 when two players share the floor, against the team's "
                "own net (diagonal: the player alone)")  # fmt: skip
    st.plotly_chart(C.heat_grid(z, text, mode, hover, row_px=34), width="stretch",
                    key="lu_pairs")  # fmt: skip
    st.caption(
        "The nine players with the most minutes. Pair numbers repeat weakly within a "
        f"season, so each is pulled toward the team's net by {k:,.0f} possessions; "
        "hover for the raw value."
    )
