"""courtvision visualizer: ``uv run courtvision app``.

Team, game and player views over the built ratings, plus the live view. Everything is
read from ``data/courtvision.duckdb`` (read-only); run ``courtvision metrics build`` first.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from courtvision.app import court
from courtvision.app import data as D

st.set_page_config(page_title="courtvision", layout="wide")


@st.cache_resource
def _con():
    return D.connect()


@st.cache_data
def _team_names():
    return D.team_names(_con())


@st.cache_data
def _players():
    return D.player_names(_con())


@st.cache_data
def _seasons():
    return D.seasons(_con())


def season_label(s: int) -> str:
    return f"{s}-{(s + 1) % 100:02d}"


def team_label(team_id: int, season: int) -> str:
    t = _team_names()
    row = t[(t.team_id == team_id) & (t.season == season)]
    if row.empty:
        row = t[t.team_id == team_id]
    return row.team.iloc[0] if not row.empty else str(team_id)


def rating_table(df: pd.DataFrame, measures: list[str]) -> pd.DataFrame:
    """value / rating / tier rows for the given measures, in that order."""
    d = df.set_index("measure")
    rows = []
    for m in measures:
        if m in d.index:
            r = d.loc[m]
            rows.append(
                {
                    "measure": D.LABELS.get(m) or D.COMPONENT_LABELS.get(m, m),
                    "pts / 100 vs league": round(float(r.value), 2),
                    "rating": None if pd.isna(r.rating) else round(float(r.rating)),
                    "tier": r.tier,
                    "low sample": bool(r.low_sample),
                }
            )
    return pd.DataFrame(rows)


def _theme() -> str:
    theme = getattr(st.context, "theme", None)
    return "dark" if theme is not None and theme.type == "dark" else "light"


def zone_charts(prof: pd.DataFrame, who: str, allowed: bool = False, key: str = "") -> None:
    """Court (how well, by zone) beside bars (how often, by zone), plus the table."""
    if prof.empty or prof.fga.sum() == 0:
        st.info("No zone data (shot locations exist from 1996-97).")
        return
    mode = _theme()
    whose = "Opponents'" if allowed else who + ("'" if who.endswith("s") else "'s")
    c1, c2 = st.columns([3, 2], gap="large")
    with c1:
        st.markdown(f"**{whose[:1].upper() + whose[1:]} FG% by zone**, against league average")
        st.plotly_chart(court.zone_court(prof, mode, allowed), width="stretch",
                        key=f"court_{key}")  # fmt: skip
        st.caption(
            "Each zone shows FG% and, below it, the gap to league average that season in "
            "percentage points. Red: made more than average from there; blue: fewer."
            + (" For a defense, blue is good." if allowed else "")
        )
    with c2:
        st.markdown(f"**Where {'opponents' if allowed else who} shot from**, share of shots")
        legend = "Opponents" if allowed else who[:1].upper() + who[1:]
        st.plotly_chart(court.shot_mix(prof, legend, mode),
                        width="stretch", key=f"mix_{key}")  # fmt: skip
        sel, mak = prof.selection_pp100.sum(), prof.making_pp100.sum()
        if allowed:
            st.caption(
                f"Against this defense, where opponents shot from was worth **{sel:+.1f}** "
                f"points per 100 shots compared with league average, and how many they made "
                f"**{mak:+.1f}**. Positive is bad for the defense. Tick = league average."
            )
        else:
            st.caption(
                f"Where {who} shot from was worth **{sel:+.1f}** points per 100 shots "
                f"compared with league average, and how many went in **{mak:+.1f}** (the "
                "Shot quality and Shot making values). Tick = league average."
            )
    with st.expander("Zone table"):
        t = prof.set_index("zone").reindex(court.ZONES).reset_index()
        st.dataframe(
            pd.DataFrame(
                {
                    "zone": t.zone.map(court.ZONE_LABELS),
                    "shots": t.fga.astype(int),
                    "share": (100 * t.unit_share).round(1),
                    "league share": (100 * t.share).round(1),
                    "FG%": (100 * t.unit_fg_pct).round(1),
                    "league FG%": (100 * t.fg_pct).round(1),
                    "pts/100 shots from selection": t.selection_pp100.round(2),
                    "pts/100 shots from making": t.making_pp100.round(2),
                }
            ),
            hide_index=True,
        )


# ---- views --------------------------------------------------------------------------------


def team_view() -> None:
    con = _con()
    c1, c2, c3 = st.columns([1, 2, 1])
    season = c1.selectbox("Season", _seasons(), format_func=season_label)
    teams = D.q(
        con,
        "select distinct team_id from metrics.team_season where season = ? order by 1",
        [season],
    ).team_id.tolist()
    team = c2.selectbox("Team", teams, format_func=lambda t: team_label(t, season))
    stype = c3.selectbox("Games", ["regular", "playoffs"])
    r = D.ratings(
        con, "team_season", "r.season = ? and r.team_id = ? and r.season_type = ?",
        [season, team, stype],
    )  # fmt: skip
    if r.empty:
        st.warning("No ratings for this selection.")
        return
    adj = D.q(
        con,
        """select a.measure, a.rating_adj, a.value_adj, a.schedule from metrics.team_season_adj a
           join metrics.best_tier b
             on b.level = 'team_season' and b.season = a.season and b.measure = a.measure
            and b.tier = a.tier
           where a.season = ? and a.team_id = ?""",
        [season, team],
    ).set_index("measure")
    use_adj = stype == "regular" and not adj.empty and st.toggle(
        "Adjust for opponents", value=False,
        help="What the team would have posted against an average schedule at a neutral site.",
    )  # fmt: skip
    d = r.set_index("measure")
    axes = [m for m in D.HEADLINE if m in d.index]
    src = adj.rating_adj if use_adj else d.rating
    left, right = st.columns([1, 1])
    with left:
        st.plotly_chart(
            court.radar([D.LABELS[m] for m in axes],
                        {team_label(team, season): [src.get(m) for m in axes]}),
            width="stretch",
        )  # fmt: skip
        st.caption(
            "Teams have five axes: Presence is per-possession impact of players (see the "
            "roster)." + (" Opponent-adjusted." if use_adj else "")
        )
    with right:
        table = rating_table(r, D.ordered(d.index))
        if not adj.empty:
            key = {D.LABELS.get(m) or D.COMPONENT_LABELS.get(m, m): m for m in adj.index}
            table["opp-adjusted"] = table.measure.map(
                lambda x: (
                    round(float(adj.rating_adj.get(key.get(x), float("nan"))))
                    if pd.notna(adj.rating_adj.get(key.get(x), float("nan")))
                    else None
                )
            )
        st.dataframe(table, hide_index=True, height=380)

    st.subheader("Shot zones")
    tab_o, tab_d = st.tabs(["Offense", "Defense (opponent shots)"])
    where = "c.season = ? and c.season_type = ? and c.team_id = ?"
    short = "the " + team_label(team, season).split()[-1]
    with tab_o:
        zone_charts(D.zone_profile(con, "metrics.team_season_counts", "a_", where,
                                   [season, stype, team]), short, key="off")  # fmt: skip
    with tab_d:
        zone_charts(D.zone_profile(con, "metrics.team_season_counts", "o_", where,
                                   [season, stype, team]), short, allowed=True,
                    key="def")  # fmt: skip

    st.subheader("Season trend (game ratings, 10-game rolling mean)")
    g = D.ratings(
        con, "team_game", "r.season = ? and r.team_id = ? and r.measure in "
        "('shooting', 'defense', 'ball_control', 'backcourt', 'frontcourt')",
        [season, team],
    ).merge(D.q(con, "select game_id, game_date from stg.games"), on="game_id")  # fmt: skip
    if not g.empty:
        g = g.sort_values("game_date")
        g["rolling"] = g.groupby("measure").rating.transform(
            lambda s: s.rolling(10, min_periods=3).mean()
        )
        g["dimension"] = g.measure.map(D.LABELS)
        st.plotly_chart(
            px.line(g, x="game_date", y="rolling", color="dimension",
                    labels={"rolling": "rating", "game_date": ""}).add_hline(
                        y=50, line_dash="dot", line_color="gray"),
            width="stretch",
        )  # fmt: skip

    st.subheader("Roster")
    roster = D.q(
        con,
        """select p.person_id, p.season_type,
                  p.shooting_rating, p.defense_rating, p.ball_control_rating,
                  p.backcourt_rating, p.frontcourt_rating, i.impact_rating as presence_rating,
                  i.impact as impact_per100, i.points_added, r.usage, c.minutes
           from metrics.player_season p
           join metrics.player_season_counts c
             using (season, season_type, person_id, team_id)
           left join metrics.player_impact i on i.season = p.season and i.person_id = p.person_id
           left join metrics.player_role r
             using (season, season_type, person_id, team_id)
           where p.season = ? and p.team_id = ? and p.season_type = ?
           order by c.minutes desc""",
        [season, team, stype],
    ).merge(_players(), on="person_id", how="left")
    roster["usage"] = 100 * roster.usage
    cols = [
        "player", "minutes", "usage", "shooting_rating", "defense_rating",
        "ball_control_rating", "backcourt_rating", "frontcourt_rating", "presence_rating",
        "impact_per100", "points_added",
    ]  # fmt: skip
    st.dataframe(roster[cols].round(1), hide_index=True)
    st.caption(
        "usage: share of the team's plays he finishes while on court (%). presence / "
        "impact: per-possession impact (adjusted plus-minus, all teams that season), points "
        "per 100 possessions; points added: over the season, vs league average."
    )


def game_view() -> None:
    con = _con()
    c1, c2 = st.columns([1, 3])
    season = c1.selectbox("Season", _seasons(), format_func=season_label, key="g_season")
    games = D.q(
        con,
        """select game_id, game_date, home_team_id, away_team_id, home_score, away_score,
                  season_type
           from stg.games where season = ? and game_id in
                (select distinct game_id from metrics.team_game where season = ?)
           order by game_date desc""",
        [season, season],
    )
    if games.empty:
        st.warning("No rated games.")
        return

    def label(gid):
        r = games[games.game_id == gid].iloc[0]
        away, home = team_label(r.away_team_id, season), team_label(r.home_team_id, season)
        return f"{r.game_date:%Y-%m-%d}  {away} {r.away_score} @ {home} {r.home_score}"

    gid = c2.selectbox("Game", games.game_id.tolist(), format_func=label)
    r = D.ratings(con, "team_game", "r.game_id = ?", [gid])
    # Season baseline per team: mean team-game rating this season (spec section 2)
    base = D.q(
        con,
        """select team_id, measure, avg(rating) as season_avg
           from metrics.ratings_long
           where level = 'team_game' and season = ? and season_type = 'regular'
             and team_id in (select team_id from metrics.team_game where game_id = ?)
           group by all""",
        [season, gid],
    )
    r = r.merge(base, on=["team_id", "measure"], how="left")
    r["delta"] = r.rating - r.season_avg
    teams = r.team_id.unique().tolist()
    cols = st.columns(len(teams))
    for col, team in zip(cols, teams, strict=True):
        with col:
            st.markdown(f"**{team_label(team, season)}**")
            t = r[r.team_id == team].set_index("measure")
            axes = [m for m in D.HEADLINE if m in t.index]
            st.plotly_chart(
                court.radar([D.LABELS[m] for m in axes],
                            {"tonight": [t.rating.get(m) for m in axes],
                             "season avg": [t.season_avg.get(m) for m in axes]}),
                width="stretch", key=f"radar_{team}",
            )  # fmt: skip
            show = t.loc[D.ordered(t.index)].reset_index()
            show["measure"] = show.measure.map(
                lambda m: D.LABELS.get(m) or D.COMPONENT_LABELS.get(m, m)
            )
            st.dataframe(
                show[["measure", "value", "rating", "season_avg", "delta"]].round(1),
                hide_index=True, height=300,
            )  # fmt: skip
    st.caption(
        "delta = tonight's rating minus the team's average game rating this season: the "
        "in-game adjustment signal. Both are on the game scale (never compare a game rating "
        "with a season rating)."
    )
    st.subheader("Players")
    p = D.q(
        con,
        """select g.*, c.minutes from metrics.player_game g
           join metrics.player_game_counts c using (game_id, person_id, team_id)
           where g.game_id = ? order by g.team_id, c.minutes desc""",
        [gid],
    ).merge(_players(), on="person_id", how="left")
    cols = ["team_id", "player", "minutes"] + [f"{m}_rating" for m in D.HEADLINE
                                               if f"{m}_rating" in p.columns]  # fmt: skip
    p["team_id"] = p.team_id.map(lambda t: team_label(t, season))
    st.dataframe(p[cols].round(1), hide_index=True)


def player_view() -> None:
    con = _con()
    players = _players()
    rated = D.q(
        con,
        "select person_id, count(*) n from metrics.player_season where team_id = 0 group by 1",
    ).merge(players, on="person_id")
    name = st.selectbox(
        "Player", sorted(rated.player.dropna().unique()),
        index=None, placeholder="Type a name",
    )  # fmt: skip
    if not name:
        return
    pid = int(rated[rated.player == name].person_id.iloc[0])
    seasons = D.q(
        con,
        "select distinct season from metrics.player_season where person_id = ? order by 1 desc",
        [pid],
    ).season.tolist()
    season = st.selectbox("Season", seasons, format_func=season_label)
    r = D.ratings(
        con, "player_season",
        "r.person_id = ? and r.season = ? and r.season_type = 'regular' and r.team_id = 0",
        [pid, season],
    )  # fmt: skip
    imp = D.impact(con, pid)
    now = imp[imp.season == season]
    d = r.set_index("measure") if not r.empty else pd.DataFrame()
    axes = D.HEADLINE + ["presence"]
    vals = [d.rating.get(m) if not d.empty else None for m in D.HEADLINE]
    vals.append(now.impact_rating.iloc[0] if not now.empty else None)
    left, right = st.columns(2)
    with left:
        st.plotly_chart(court.radar([D.LABELS[m] for m in axes], {name: vals}),
                        width="stretch")  # fmt: skip
        st.caption(
            "Backcourt / Frontcourt are rated only among players at least half guard / "
            "half forward-center. Presence is per-possession impact (adjusted plus-minus)."
        )
    with right:
        if not r.empty:
            st.dataframe(rating_table(r, D.ordered(d.index)), hide_index=True, height=380)

    st.subheader("Impact per possession")
    if now.empty:
        st.info("No lineup data for this season (play-by-play lineups exist from 1996-97).")
    else:
        i = now.iloc[0]
        c = st.columns(5)
        c[0].metric("Impact per 100 poss.", f"{i.impact:+.1f}", help="Points per 100 "
                    "possessions vs league average, offense + defense, adjusted for "
                    "teammates and opponents (ridge regression on every stint).")  # fmt: skip
        c[1].metric("Offense / defense", f"{i.impact_off:+.1f} / {i.impact_def:+.1f}")
        c[2].metric("Uncertainty (± 1 se)", f"{i.impact_se:.1f}")
        c[3].metric("Points added", f"{i.points_added:+.0f}",
                    help="Over the season vs league average: impact x possessions.")  # fmt: skip
        per36 = i.points_added / i.minutes * 36 if pd.notna(i.minutes) and i.minutes else None
        c[4].metric("Per 36 minutes", f"{per36:+.1f}" if per36 is not None else "—",
                    help="Points added per 36 minutes played. Prefer the per-possession "
                    "number to compare players on fast and slow teams.")  # fmt: skip
        role = (f"Usage {100 * i.usage:.0f}% of team plays, assists on "
                f"{100 * i.ast_pct:.0f}% of teammates' baskets while on court."
                if pd.notna(i.usage) else "")  # fmt: skip
        st.caption(
            f"{int(i.poss):,} possessions on court. Season-only estimate (no prior): "
            f"{i.season_only:+.1f}. {role}"
        )
        hist = imp.dropna(subset=["impact"])
        if len(hist) > 1:
            fig = go.Figure([
                go.Scatter(x=list(hist.season) + list(hist.season[::-1]),
                           y=list(hist.impact + hist.impact_se)
                           + list((hist.impact - hist.impact_se)[::-1]),
                           fill="toself", line={"width": 0}, hoverinfo="skip",
                           fillcolor="rgba(57,135,229,0.18)", showlegend=False),
                go.Scatter(x=hist.season, y=hist.impact, mode="lines+markers",
                           line={"color": "#3987e5", "width": 2}, name="impact",
                           hovertemplate="%{x}: %{y:+.1f} per 100<extra></extra>"),
            ])  # fmt: skip
            fig.add_hline(y=0, line_color="#898781", line_width=1)
            fig.update_layout(height=260, margin={"l": 0, "r": 0, "t": 10, "b": 0},
                              showlegend=False,
                              yaxis={"title": "pts / 100 poss."},
                              xaxis={"title": "season (start year)"})  # fmt: skip
            st.plotly_chart(fig, width="stretch")
            st.caption("Band: ± 1 standard error.")
    if D.has_market(con):
        mk = D.q(con, "select * from market.players where person_id = ?", [pid])
        if not mk.empty and pd.notna(mk.impact.iloc[0]):
            m = mk.iloc[0]
            st.subheader(f"Market ({int(m.season)}-{(int(m.season) + 1) % 100:02d})")
            c = st.columns(4)
            sal = f"${m.salary / 1e6:.1f}M" if pd.notna(m.salary) else "unsigned"
            c[0].metric("Salary", sal)
            c[1].metric("Estimated fair salary", f"${m.fair_salary / 1e6:.1f}M")
            c[2].metric("Wins above replacement", f"{m.war:.1f}",
                        help=f"Bad / good case: {m.war_p10:.1f} / {m.war_p90:.1f}")  # fmt: skip
            c[3].metric("Risk", m.risk)
            if m.risk_flags is not None and len(m.risk_flags):
                st.caption("Risk factors: " + "; ".join(m.risk_flags))
    st.subheader("Shot zones")
    zone_charts(
        D.zone_profile(con, "metrics.player_season_counts", "a_",
                       "c.season = ? and c.season_type = 'regular' and c.person_id = ? "
                       "and c.team_id = 0", [season, pid]),
        name, key="player",
    )  # fmt: skip
    st.subheader("Game log")
    g = D.ratings(
        con, "player_game", "r.person_id = ? and r.season = ? and r.measure in "
        "('shooting', 'defense', 'ball_control')", [pid, season],
    ).merge(D.q(con, "select game_id, game_date from stg.games"), on="game_id")  # fmt: skip
    if not g.empty:
        g = g.sort_values("game_date")
        g["rolling"] = g.groupby("measure").rating.transform(
            lambda s: s.rolling(10, min_periods=3).mean()
        )
        g["dimension"] = g.measure.map(D.LABELS)
        st.plotly_chart(
            px.line(g, x="game_date", y="rolling", color="dimension",
                    labels={"rolling": "rating (10-game mean)", "game_date": ""}),
            width="stretch",
        )  # fmt: skip


def live_view() -> None:
    from courtvision.app import live_page

    live_page.render(_con())


def market_view() -> None:
    from courtvision.app import market_page

    market_page.render(_con(), team_label, _theme())


VIEWS = {"Team": team_view, "Game": game_view, "Player": player_view, "Market": market_view,
         "Live": live_view}  # fmt: skip

st.sidebar.title("courtvision")
choice = st.sidebar.radio("View", list(VIEWS))
st.sidebar.caption(
    "0-100 ratings: 50 = league average that season, 10 = one standard deviation. "
    "Tier A = play-by-play (1996-97+); B / B- / C = box-score versions for older seasons."
)
VIEWS[choice]()
