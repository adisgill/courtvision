"""Season-by-season box-score stats for the Team and Player pages: one row per season, each
cell shaded by where it ranked in the league that season (blue better, red worse, gray not
judged), so seasons from different eras read on the same terms."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import numpy as np
import pandas as pd
import streamlit as st

from courtvision.app import charts as C
from courtvision.app import data as D

# a stat shows for a season only when the source records it in nearly every game
COMPLETE = 0.95
# display stat -> source stats it needs (names in metrics.stat_availability)
NEEDS = {"reb": ["oreb", "dreb"], "ast": ["ast"], "stl": ["stl"], "blk": ["blk"],
         "tov": ["tov"], "fg_pct": ["fga"], "fg3_pct": ["fg3a"], "ft_pct": ["fta"],
         "ts_pct": ["fga", "fta"], "pace": ["fga", "fta", "oreb", "tov"],
         "ortg": ["fga", "fta", "oreb", "tov"], "drtg": ["fga", "fta", "oreb", "tov"],
         "net": ["fga", "fta", "oreb", "tov"]}  # fmt: skip
PCT = {"fg_pct", "fg3_pct", "ft_pct", "ts_pct"}


def _recorded(con) -> pd.DataFrame:
    """(season, stat) -> True when recorded in at least COMPLETE of that season's games."""
    av = D.q(con, "select season, stat, share from metrics.stat_availability")
    return av.assign(ok=av.share >= COMPLETE).pivot(index="season", columns="stat", values="ok")


def _blank_unrecorded(df: pd.DataFrame, rec: pd.DataFrame) -> pd.DataFrame:
    for col, needs in NEEDS.items():
        if col not in df.columns:
            continue
        ok = pd.Series(True, index=df.index)
        for n in needs:
            if n in rec.columns:
                ok &= df.season.map(rec[n]).fillna(False).astype(bool)
        df.loc[~ok, col] = np.nan
    return df


def _shares(df: pd.DataFrame) -> pd.DataFrame:
    """Shooting percentages, as percent (47.3, not 0.473: the grid has no room for %)."""

    def ratio(a, b):
        return (100 * a / b.where(b > 0)).astype(float)

    return df.assign(
        fg_pct=ratio(df.fgm, df.fga), fg3_pct=ratio(df.fg3m, df.fg3a),
        ft_pct=ratio(df.ftm, df.fta), ts_pct=ratio(df.pts, 2 * (df.fga + 0.44 * df.fta)),
    )  # fmt: skip


def _percentile(league: pd.DataFrame, me: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Share of the season's league rows each of ``me``'s values beats (NaN if unknown)."""
    out = pd.DataFrame(index=me.index)
    for c in cols:
        vals = []
        for s, v in zip(me.season, me[c], strict=True):
            lg = league.loc[league.season == s, c].dropna()
            vals.append(np.nan if pd.isna(v) or lg.empty else float((lg < v).mean()))
        out[c] = vals
    return out


def _so_far(con, season: int) -> bool:
    """True for the season in progress (the latest, with a game in the last ten days)."""
    last = D.q(con, "select max(season) s, max(game_date) d from stg.games "
               "where home_score is not null").iloc[0]  # fmt: skip
    return int(last.s) == season and (date.today() - pd.Timestamp(last.d).date()).days <= 10


def _hover(x, q: float, fmt: str, direction: int, who: str) -> str:
    if pd.isna(x):
        return "not recorded in the source that season"
    if pd.isna(q):
        return fmt.format(x)
    if direction:
        beats = q if direction > 0 else 1 - q
        return f"{fmt.format(x)} · better than {100 * beats:.0f}% of {who}"
    return f"{fmt.format(x)} · {100 * q:.0f}th percentile among {who}"


def _grid(rows: pd.DataFrame, labels: list[str], cols: list[tuple], pct: pd.DataFrame,
          mode: str, key: str, who: str) -> None:  # fmt: skip
    """Draw the season grid: ``cols`` are (column, header, format, direction) with direction
    +1 (higher is better), -1 (lower is better) or 0 (not judged, gray)."""
    z, text, hover = {}, {}, {}
    for c, head, fmt, d in cols:
        v, p = rows[c], pct[c] if c in pct else pd.Series(np.nan, index=rows.index)
        z[head] = ((2 * p - 1) * d).to_numpy() if d else np.full(len(rows), np.nan)
        text[head] = [("—" if pd.isna(x) else fmt.format(x)) for x in v]
        hover[head] = [_hover(x, q, fmt, d, who) for x, q in zip(v, p, strict=True)]
    frame = {k: pd.DataFrame(v, index=labels) for k, v in (("z", z), ("t", text), ("h", hover))}
    fig = C.heat_grid(frame["z"], frame["t"], mode, hover=frame["h"], row_px=28)
    st.plotly_chart(fig, width="stretch", key=key, config={"displayModeBar": False})


PLAYER_COLS = {
    "Per game": [("games", "GP", "{:.0f}", 0), ("minutes", "MIN", "{:.1f}", 0),
                 ("pts", "PTS", "{:.1f}", 1), ("reb", "REB", "{:.1f}", 1),
                 ("ast", "AST", "{:.1f}", 1), ("stl", "STL", "{:.1f}", 1),
                 ("blk", "BLK", "{:.1f}", 1), ("tov", "TOV", "{:.1f}", 0)],
    "Totals": [("games", "GP", "{:.0f}", 0), ("minutes", "MIN", "{:,.0f}", 0),
               ("pts", "PTS", "{:,.0f}", 1), ("reb", "REB", "{:,.0f}", 1),
               ("ast", "AST", "{:,.0f}", 1), ("stl", "STL", "{:,.0f}", 1),
               ("blk", "BLK", "{:,.0f}", 1), ("tov", "TOV", "{:,.0f}", 0)],
    "Per 36 min": [("games", "GP", "{:.0f}", 0), ("minutes", "MIN/G", "{:.1f}", 0),
                   ("pts", "PTS", "{:.1f}", 1), ("reb", "REB", "{:.1f}", 1),
                   ("ast", "AST", "{:.1f}", 1), ("stl", "STL", "{:.1f}", 1),
                   ("blk", "BLK", "{:.1f}", 1), ("tov", "TOV", "{:.1f}", 0)],
}  # fmt: skip
SHOOTING = [("fg_pct", "FG%", "{:.1f}", 1), ("fg3_pct", "3P%", "{:.1f}", 1),
            ("ft_pct", "FT%", "{:.1f}", 1), ("ts_pct", "TS%", "{:.1f}", 1)]  # fmt: skip
COUNTS = ["pts", "reb", "ast", "stl", "blk", "tov"]


QUALIFY = 0.25  # league comparison: players with a quarter of the season's most games


def player_seasons(con, pid: int, season_type: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The player's season totals (all teams combined) and, for the same seasons, the
    league's players with at least QUALIFY of the most games anyone played (so the season
    in progress has a comparison too)."""
    q = """select c.season, c.person_id, c.games, c.minutes, c.b_pts pts,
                  c.b_oreb + c.b_dreb reb, c.b_ast ast, c.b_stl stl, c.b_blk blk, c.b_tov tov,
                  c.b_fgm fgm, c.b_fga fga, c.b_fg3m fg3m, c.b_fg3a fg3a, c.b_ftm ftm,
                  c.b_fta fta
           from metrics.player_season_counts c
           where c.season_type = ? and c.team_id = 0 and c.games > 0
             and c.season in (select season from metrics.player_season_counts
                              where person_id = ? and team_id = 0 and season_type = ?)"""
    df = D.q(con, q, [season_type, pid, season_type])
    df = _blank_unrecorded(_shares(df), _recorded(con))
    me = df[df.person_id == pid].sort_values("season", ascending=False).reset_index(drop=True)
    lg = df[df.games >= QUALIFY * df.groupby("season").games.transform("max")]
    return me, lg


def _scale(df: pd.DataFrame, how: str) -> pd.DataFrame:
    out = df.copy()
    if how == "Per game":
        out[COUNTS] = df[COUNTS].div(df.games, axis=0)
        out["minutes"] = df.minutes / df.games
    elif how == "Per 36 min":
        out[COUNTS] = df[COUNTS].div(df.minutes.where(df.minutes > 0), axis=0) * 36
        out["minutes"] = df.minutes / df.games
    return out


def render_player(con, pid: int, season: int, mode: str,
                  team_label: Callable[[int, int], str]) -> None:  # fmt: skip
    st.subheader("Season by season")
    c1, c2 = st.columns([2, 1])
    how = c1.radio("Show", list(PLAYER_COLS), horizontal=True, key="ps_how",
                   label_visibility="collapsed")  # fmt: skip
    stype = c2.radio("Games", ["regular", "playoffs"], horizontal=True, key="ps_stype",
                     format_func=lambda s: "Regular season" if s == "regular" else "Playoffs",
                     label_visibility="collapsed")  # fmt: skip
    me, lg = player_seasons(con, pid, stype)
    if me.empty:
        st.info("No playoff games." if stype == "playoffs" else "No season stats.")
        return
    me, lg = _scale(me, how), _scale(lg, how)
    teams = D.q(con, """select season, team_id from metrics.player_season_counts
                        where person_id = ? and season_type = ? and team_id <> 0
                        order by season, minutes desc""", [pid, stype])  # fmt: skip
    names = {s: "/".join(team_label(int(t), int(s)).split()[-1] for t in g.team_id)
             for s, g in teams.groupby("season")}  # fmt: skip
    current = _so_far(con, int(me.season.max()))
    labels = [
        ("▸ " if s == season else "")
        + f"{s}-{(s + 1) % 100:02d} {names.get(s, '')}"
        + (" · so far" if current and s == me.season.max() else "")
        for s in me.season
    ]
    cols = PLAYER_COLS[how] + SHOOTING
    pct = _percentile(lg, me, [c for c, *_ in cols])
    who = "regulars" if stype == "regular" else "playoff regulars"
    _grid(me, labels, cols, pct, mode, f"ps_grid_{how}_{stype}", who)
    st.caption(
        "All teams combined; ▸ marks the season above. Shading: where the number ranked that "
        "season among players with at least a quarter of the most games anyone played (blue "
        "better, red worse; games, minutes and turnovers aren't judged). True shooting % "
        "counts free throws and threes. A dash: the source doesn't have that stat for nearly "
        "every game that season (it's patchy before the mid-1980s)."
    )


TEAM_COLS = [("record", "W-L", "{}", 1), ("pts", "PTS", "{:.1f}", 1), ("opp", "OPP", "{:.1f}", -1),
             ("ortg", "ORTG", "{:.1f}", 1), ("drtg", "DRTG", "{:.1f}", -1),
             ("net", "NET", "{:+.1f}", 1), ("pace", "PACE", "{:.1f}", 0),
             ("fg_pct", "FG%", "{:.1f}", 1), ("fg3_pct", "3P%", "{:.1f}", 1),
             ("ft_pct", "FT%", "{:.1f}", 1), ("reb", "REB", "{:.1f}", 1),
             ("ast", "AST", "{:.1f}", 1), ("tov", "TOV", "{:.1f}", -1),
             ("stl", "STL", "{:.1f}", 1), ("blk", "BLK", "{:.1f}", 1)]  # fmt: skip


def team_seasons(con, season_type: str) -> pd.DataFrame:
    """Every team's season: record, per-game box score, points per 100 possessions."""
    df = D.q(con, """
        with g as (
            select season, home_team_id team_id, home_score > away_score win from stg.games
            where season_type = ? and home_score is not null
            union all
            select season, away_team_id, away_score > home_score from stg.games
            where season_type = ? and home_score is not null
        ), rec as (select season, team_id, count(*) filter (where win) wins,
                          count(*) filter (where not win) losses from g group by all)
        select c.season, c.team_id, c.games, r.wins, r.losses, c.b_pts pts, c.o_b_pts opp,
               c.b_poss poss, c.o_b_poss o_poss, c.b_oreb + c.b_dreb reb, c.b_ast ast,
               c.b_tov tov, c.b_stl stl, c.b_blk blk, c.b_fgm fgm, c.b_fga fga,
               c.b_fg3m fg3m, c.b_fg3a fg3a, c.b_ftm ftm, c.b_fta fta
        from metrics.team_season_counts c
        left join rec r using (season, team_id)
        where c.season_type = ? and c.games > 0""", [season_type] * 3)  # fmt: skip
    df = _shares(df)
    per = ["pts", "opp", "reb", "ast", "tov", "stl", "blk"]
    df[per] = df[per].div(df.games, axis=0)
    df["ortg"] = 100 * df.pts * df.games / df.poss.where(df.poss > 0)
    df["drtg"] = 100 * df.opp * df.games / df.o_poss.where(df.o_poss > 0)
    df["net"] = df.ortg - df.drtg
    df["pace"] = (df.poss + df.o_poss) / 2 / df.games
    df["win_pct"] = df.wins / (df.wins + df.losses)
    return _blank_unrecorded(df, _recorded(con))


def render_team(con, team: int, season: int, stype: str, mode: str,
                team_label: Callable[[int, int], str]) -> None:  # fmt: skip
    st.subheader("Season by season")
    lg = team_seasons(con, stype)
    me = lg[lg.team_id == team].sort_values("season", ascending=False).reset_index(drop=True)
    if me.empty:
        st.info("No playoff seasons." if stype == "playoffs" else "No season stats.")
        return
    total = len(me)
    if total > 10 and not st.toggle(f"All {total} seasons", key="ts_all"):
        me = me[me.season >= sorted(me.season)[-10]].reset_index(drop=True)
    me["record"] = [f"{w:.0f}-{lo:.0f}" if pd.notna(w) else "—"
                    for w, lo in zip(me.wins, me.losses, strict=True)]  # fmt: skip
    now = team_label(team, int(lg.season.max())).split()[-1]
    current = _so_far(con, int(lg.season.max()))
    labels = []
    for s in me.season:
        nick = team_label(team, int(s)).split()[-1]
        labels.append(("▸ " if s == season else "") + f"{s}-{(s + 1) % 100:02d}"
                      + (f" {nick}" if nick != now else "")
                      + (" · so far" if current and s == lg.season.max() else ""))  # fmt: skip
    pct = _percentile(lg.assign(record=lg.win_pct), me.assign(record=me.win_pct),
                      [c for c, *_ in TEAM_COLS])  # fmt: skip
    _grid(me, labels, TEAM_COLS, pct, mode, f"ts_grid_{stype}_{len(me)}",
          "teams" if stype == "regular" else "playoff teams")  # fmt: skip
    st.caption(
        "▸ marks the season above. Per game, except ORTG / DRTG / NET (points scored, allowed "
        "and the difference per 100 possessions) and PACE (possessions per game). Shading: "
        "rank among that season's teams (blue better, red worse; pace isn't judged). A dash: "
        "the source doesn't have that stat for nearly every game that season (it's patchy "
        "before the mid-1980s)."
    )
