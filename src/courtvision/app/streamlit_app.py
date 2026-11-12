"""courtvision visualizer: ``uv run courtvision app``.

Team, game and player views over the built ratings, plus the live view. Everything is
read from ``data/courtvision.duckdb`` (read-only); run ``courtvision metrics build`` first.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
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


def zone_charts(prof: pd.DataFrame, who: str, allowed: bool = False) -> None:
    if prof.empty or prof.fga.sum() == 0:
        st.info("No zone data (shot locations exist from 1996-97).")
        return
    sign = -1 if allowed else 1
    hover = {
        z: (
            f"{court.ZONE_LABELS[z]}<br>{int(r.fga)} FGA, {r.unit_share:.1%} of shots "
            f"(league {r.share:.1%})<br>FG {r.unit_fg_pct:.1%} (league {r.fg_pct:.1%})"
        )
        for z, r in prof.set_index("zone").iterrows()
    }
    c1, c2 = st.columns(2)
    with c1:
        vals = dict(zip(prof.zone, sign * prof.fg_pct_diff * 100, strict=True))
        title = f"{who}: FG% {'allowed ' if allowed else ''}vs league (points of %)"
        st.plotly_chart(court.zone_heatmap(vals, hover, title), width="stretch")
    with c2:
        vals = dict(zip(prof.zone, prof.share_diff * 100, strict=True))
        title = f"{who}: share of shots {'allowed ' if allowed else ''}vs league (points of %)"
        st.plotly_chart(court.zone_heatmap(vals, hover, title), width="stretch")
    if allowed:
        st.caption("FG% map: blue = opponents shoot worse than league average from that zone.")
    else:
        st.caption("FG% map: red = better than league average from that zone.")


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
    d = r.set_index("measure")
    axes = [m for m in D.HEADLINE if m in d.index]
    left, right = st.columns([1, 1])
    with left:
        st.plotly_chart(
            court.radar([D.LABELS[m] for m in axes],
                        {team_label(team, season): [d.rating.get(m) for m in axes]}),
            width="stretch",
        )  # fmt: skip
        st.caption(
            "Teams have five axes: Presence is an on/off measure of players (see the roster)."
        )
    with right:
        st.dataframe(rating_table(r, D.ordered(d.index)), hide_index=True, height=380)

    st.subheader("Shot zones")
    tab_o, tab_d = st.tabs(["Offense", "Defense (opponent shots)"])
    where = "c.season = ? and c.season_type = ? and c.team_id = ?"
    with tab_o:
        zone_charts(D.zone_profile(con, "metrics.team_season_counts", "a_", where,
                                   [season, stype, team]), "Offense")  # fmt: skip
    with tab_d:
        zone_charts(D.zone_profile(con, "metrics.team_season_counts", "o_", where,
                                   [season, stype, team]), "Defense", allowed=True)  # fmt: skip

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
                  p.backcourt_rating, p.frontcourt_rating, ps.presence_rating,
                  ps.presence_value, c.minutes
           from metrics.player_season p
           join metrics.player_season_counts c
             using (season, season_type, person_id, team_id)
           left join metrics.presence_season ps
             using (season, season_type, person_id, team_id)
           where p.season = ? and p.team_id = ? and p.season_type = ?
           order by c.minutes desc""",
        [season, team, stype],
    ).merge(_players(), on="person_id", how="left")
    cols = [
        "player", "minutes", "shooting_rating", "defense_rating", "ball_control_rating",
        "backcourt_rating", "frontcourt_rating", "presence_rating", "presence_value",
    ]  # fmt: skip
    st.dataframe(roster[cols].round(1), hide_index=True)


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
    pres = D.q(
        con,
        """select team_id, presence_rating, presence_value, minutes_on
           from metrics.presence_season where person_id = ? and season = ?
             and season_type = 'regular' order by minutes_on desc""",
        [pid, season],
    )
    d = r.set_index("measure") if not r.empty else pd.DataFrame()
    axes = D.HEADLINE + ["presence"]
    vals = [d.rating.get(m) if not d.empty else None for m in D.HEADLINE]
    vals.append(pres.presence_rating.iloc[0] if not pres.empty else None)
    left, right = st.columns(2)
    with left:
        st.plotly_chart(court.radar([D.LABELS[m] for m in axes], {name: vals}),
                        width="stretch")  # fmt: skip
        st.caption(
            "Backcourt / Frontcourt are rated only among players at least half guard / "
            "half forward-center. Presence is on/off for the team he played most for."
        )
    with right:
        if not r.empty:
            st.dataframe(rating_table(r, D.ordered(d.index)), hide_index=True, height=380)
    st.subheader("Shot zones")
    zone_charts(
        D.zone_profile(con, "metrics.player_season_counts", "a_",
                       "c.season = ? and c.season_type = 'regular' and c.person_id = ? "
                       "and c.team_id = 0", [season, pid]),
        name,
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


VIEWS = {"Team": team_view, "Game": game_view, "Player": player_view, "Live": live_view}

st.sidebar.title("courtvision")
choice = st.sidebar.radio("View", list(VIEWS))
st.sidebar.caption(
    "0-100 ratings: 50 = league average that season, 10 = one standard deviation. "
    "Tier A = play-by-play (1996-97+); B / B- / C = box-score versions for older seasons."
)
VIEWS[choice]()
