"""Predictions for each team's next game: team dimensions, players, margin and win chance.

Spec section 14. Built by ``courtvision forecast`` into schema ``forecast``:

* **Team level.** For every team-game measure (the five dimensions and their parts), one
  model per measure fitted on this season's team-game values plus last season's at a
  quarter weight, recent games weighted more (half-life ``HALF_LIFE`` days):
  ``value(team, game) = mu + form[team] + opp[opponent] + h * site``, ridge-regularised.
  ``form`` is the team's current level (trend and form through the recency weights);
  ``opp`` is what the opponent does to everyone (opponent strength). The prediction for
  an upcoming game is ``mu + form[team] + opp[opponent] + h * site``, rated on the
  team-game reference of the season.
* **Margin and win chance.** Two views, blended: the same model on net points per 100
  possessions (team form), and the roster: each expected player's per-possession impact
  times his expected share of minutes, plus home court. Margin = net x expected pace /
  100; win chance = Phi(margin / sigma). The blend weight and sigma come from the
  backtest.
* **Player level.** Players expected to play (in a recent game): expected minutes from
  their recent games (half-life ``PLAYER_HALF_LIFE`` games); each dimension's value =
  recent form shrunk toward the season value by exposure, plus the opponent's effect on
  that measure, rated on the player-game reference.
* **Backtest** (``forecast.backtest``): the last complete season re-predicted week by
  week using only games before each week, against what happened; baselines: season-to-
  date averages and a coin flip.
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

TEAM_MEASURES = [
    "shooting", "defense", "ball_control", "backcourt", "frontcourt",
    "shooting_sq", "shooting_sm", "shooting_fd", "shooting_fm",
    "defense_dq", "defense_dm", "defense_dt", "defense_df", "defense_dr",
    "ball_bt", "ball_bo",
]  # fmt: skip
PLAYER_MEASURES = ["shooting", "defense", "ball_control", "backcourt", "frontcourt"]
HALF_LIFE = 21.0  # days
LAST_SEASON_WEIGHT = 0.25
RIDGE = 4.0  # in game-weights: how much evidence a team effect needs
PLAYER_HALF_LIFE = 6.0  # games
PLAYER_K = 120.0  # exposure units at which recent form and season level count equally


# ---- data -------------------------------------------------------------------------------------


def team_games(con, seasons: list[int]) -> pd.DataFrame:
    """Team-game values of the forecast measures, with opponent, site and date."""
    sql = f"""
        select r.season, r.game_id, g.game_date, r.team_id, r.measure, r.value,
               case when r.team_id = g.home_team_id then g.away_team_id
                    else g.home_team_id end as opp_id,
               r.team_id = g.home_team_id as home
        from metrics.ratings_long r join stg.games g using (game_id)
        where r.level = 'team_game' and r.tier = 'A' and r.season_type = 'regular'
          and r.season in ({",".join(map(str, seasons))}) and r.value is not null
          and r.measure in ({",".join(f"'{m}'" for m in TEAM_MEASURES)})
    """
    net = f"""
        select c.season, c.game_id, c.game_date, c.team_id, 'net' as measure,
               100 * (c.pts_fg + c.ftm - c.o_pts_fg - c.o_ftm) / nullif(c.poss, 0) as value,
               c.opponent_team_id as opp_id, c.team_id = g.home_team_id as home
        from metrics.team_game_counts c join stg.games g using (game_id)
        where c.season_type = 'regular' and c.pbp_ok and c.poss > 0
          and c.season in ({",".join(map(str, seasons))})
    """
    df = pd.concat([con.execute(sql).df(), con.execute(net).df()], ignore_index=True)
    df["game_date"] = pd.to_datetime(df.game_date)
    return df


def weights(dates: pd.Series, seasons: pd.Series, asof: pd.Timestamp, season: int):
    age = (asof - dates).dt.days.clip(lower=0)
    w = 0.5 ** (age / HALF_LIFE)
    return np.where(seasons == season, w, w * LAST_SEASON_WEIGHT)


def fit_team_model(g: pd.DataFrame, w: np.ndarray) -> dict:
    """value = mu + form[team] + opp[opponent] + h * site (weighted ridge)."""
    teams = np.sort(pd.unique(np.concatenate([g.team_id.values, g.opp_id.values])))
    n = len(teams)
    pos = pd.Series(np.arange(n), index=teams)
    ti, oi = pos.loc[g.team_id].to_numpy(), pos.loc[g.opp_id].to_numpy()
    m = len(g)
    x = np.zeros((m, 2 * n + 2))
    x[np.arange(m), ti] = 1.0
    x[np.arange(m), n + oi] = 1.0
    x[:, 2 * n] = np.where(g.home.to_numpy(), 1.0, -1.0)
    x[:, 2 * n + 1] = 1.0
    y = g.value.to_numpy(float)
    pen = np.full(2 * n + 2, RIDGE)
    pen[-2:] = 1e-6
    xw = x * w[:, None]
    beta = np.linalg.solve(x.T @ xw + np.diag(pen), xw.T @ y)
    return {"teams": pos, "form": beta[:n], "opp": beta[n:2 * n], "h": beta[2 * n],
            "mu": beta[-1]}  # fmt: skip


def predict_team(model: dict, team: int, opp: int, home: bool) -> float:
    pos = model["teams"]
    f = model["form"][pos[team]] if team in pos.index else 0.0
    o = model["opp"][pos[opp]] if opp in pos.index else 0.0
    return float(model["mu"] + f + o + model["h"] * (1 if home else -1))


def player_games(con, season: int) -> pd.DataFrame:
    return con.execute(
        f"""select r.game_id, g.game_date, r.person_id, r.team_id, r.measure, r.value,
                  r.exposure, c.minutes
           from metrics.ratings_long r
           join stg.games g using (game_id)
           join metrics.player_game_counts c
             on c.game_id = r.game_id and c.person_id = r.person_id
           where r.level = 'player_game' and r.tier = 'A' and r.season = ?
             and r.season_type = 'regular' and r.value is not null
             and r.measure in ({",".join(f"'{m}'" for m in PLAYER_MEASURES)})""",
        [season],
    ).df()


# ---- roster view of the margin ---------------------------------------------------------------


def expected_minutes(con, season: int, asof: pd.Timestamp) -> pd.DataFrame:
    """Each player's expected minutes for his team's next game: recent games weighted
    (half-life PLAYER_HALF_LIFE games); only players who played in one of the team's last
    three games (a proxy for availability)."""
    pg = con.execute(
        """select c.person_id, c.team_id, c.game_id, g.game_date, c.minutes
           from metrics.player_game_counts c join stg.games g using (game_id)
           where c.season = ? and c.season_type = 'regular' and g.game_date < ?""",
        [season, asof.date()],
    ).df()
    if pg.empty:
        return pg.assign(exp_minutes=[])
    pg["game_date"] = pd.to_datetime(pg.game_date)
    games = pg[["team_id", "game_id", "game_date"]].drop_duplicates()
    games["rk"] = games.groupby("team_id").game_date.rank(ascending=False, method="first")
    pg = pg.merge(games[["team_id", "game_id", "rk"]], on=["team_id", "game_id"])
    full = games[["team_id", "rk"]].merge(pg[["person_id", "team_id"]].drop_duplicates(),
                                          on="team_id")  # fmt: skip
    pg = full.merge(pg, on=["team_id", "person_id", "rk"], how="left").fillna({"minutes": 0})
    pg["w"] = 0.5 ** ((pg.rk - 1) / PLAYER_HALF_LIFE)
    pg["wm"] = pg.w * pg.minutes
    recent = pg[pg.rk <= 3].groupby(["person_id", "team_id"]).minutes.sum()
    agg = pg.groupby(["person_id", "team_id"])[["wm", "w"]].sum()
    out = (agg.wm / agg.w).rename("exp_minutes").reset_index()
    out = out[out.set_index(["person_id", "team_id"]).index.isin(recent[recent > 0].index)]
    # scale each team to 240 minutes
    tot = out.groupby("team_id").exp_minutes.transform("sum")
    out["exp_minutes"] = out.exp_minutes * 240 / tot.where(tot > 0, 1)
    return out


def roster_net(minutes: pd.DataFrame, impact: pd.DataFrame, team: int) -> float:
    m = minutes[minutes.team_id == team].merge(impact, on="person_id", how="left")
    return float((m.impact.fillna(-1.0) * m.exp_minutes / 48).sum())


# ---- upcoming games --------------------------------------------------------------------------


def upcoming(con, season: int, today: date | None = None) -> pd.DataFrame:
    """Each team's next scheduled game from today on (or after the last game with a result,
    if the data is newer than today's date)."""
    last = con.execute(
        "select max(game_date) from stg.games where season = ? and home_score is not null",
        [season],
    ).fetchall()[0][0]
    # games between the data's last day and today have been played but aren't in the data
    # yet (the Kaggle dataset updates nightly): start from today
    last = max(last, (today or date.today()) - timedelta(days=1))
    sched = con.execute(
        """select lpad(gameId::varchar, 10, '0') as game_id,
                  gameDateTimeEst::date as game_date, gameDateTimeEst as tipoff,
                  homeTeamId as home_team_id, awayTeamId as away_team_id
           from kaggle.league_schedule_26_27
           where gameDateTimeEst::date > ? and gameLabel is distinct from 'Preseason'
             and homeTeamId > 0 and awayTeamId > 0  -- knockout games with teams TBD""",
        [last],
    ).df()
    long = pd.concat([
        sched.assign(team_id=sched.home_team_id), sched.assign(team_id=sched.away_team_id)
    ])  # fmt: skip
    nxt = long.sort_values("tipoff").groupby("team_id").head(1)
    return sched[sched.game_id.isin(nxt.game_id)].sort_values("tipoff").reset_index(drop=True)


def _ref(con, level: str, season: int) -> pd.DataFrame:
    return (
        con.execute(
            "select measure, mu, sigma from metrics.reference_stats where level = ? and "
            "season = ? and tier = 'A'",
            [level, season],
        )
        .df()
        .set_index("measure")
    )


def _rating(v: float, ref: pd.DataFrame, m: str) -> float:
    if m not in ref.index or not ref.loc[m, "sigma"]:
        return np.nan
    return float(np.clip(50 + 10 * (v - ref.loc[m, "mu"]) / ref.loc[m, "sigma"], 0, 100))


def team_models(tg: pd.DataFrame, asof: pd.Timestamp, season: int) -> dict:
    hist = tg[tg.game_date < asof]
    models = {}
    for m, g in hist.groupby("measure"):
        if g.team_id.nunique() >= 4:
            models[m] = fit_team_model(g, weights(g.game_date, g.season, asof, season))
    return models


def predict(con, season: int, blend: float, sigma: float) -> dict:
    games = upcoming(con, season)
    asof = pd.Timestamp(games.game_date.min()) if not games.empty else pd.Timestamp.today()
    tg = team_games(con, [season - 1, season])
    models = team_models(tg, asof, season)
    ref_t, ref_p = _ref(con, "team_game", season), _ref(con, "player_game", season)
    season_avg = con.execute(
        """select team_id, measure, avg(rating) as season_avg from metrics.ratings_long
           where level = 'team_game' and season = ? and season_type = 'regular' and tier = 'A'
           group by all""",
        [season],
    ).df()
    impact = con.execute(
        "select person_id, impact from metrics.player_impact where season = ?", [season]
    ).df()
    mins = expected_minutes(con, season, asof)
    pace = (
        con.execute(
            "select team_id, avg(poss) as pace from metrics.team_game_counts where season = ? "
            "and season_type = 'regular' and pbp_ok group by team_id",
            [season],
        )
        .df()
        .set_index("team_id")
        .pace
    )
    hca = (
        con.execute(
            "select avg(home_court) from metrics.player_impact where season = ?", [season]
        ).fetchall()[0][0]
        or 0.0
    )

    team_rows, game_rows = [], []
    for g in games.itertuples():
        for team, opp, home in ((g.home_team_id, g.away_team_id, True),
                                (g.away_team_id, g.home_team_id, False)):  # fmt: skip
            for m, model in models.items():
                if m == "net":
                    continue
                v = predict_team(model, team, opp, home)
                team_rows.append({"game_id": g.game_id, "team_id": team, "opp_id": opp,
                                  "home": home, "measure": m, "pred_value": v,
                                  "pred_rating": _rating(v, ref_t, m)})  # fmt: skip
        form = 0.5 * (
            predict_team(models["net"], g.home_team_id, g.away_team_id, True)
            - predict_team(models["net"], g.away_team_id, g.home_team_id, False)
        )
        roster = (roster_net(mins, impact, g.home_team_id)
                  - roster_net(mins, impact, g.away_team_id) + hca)  # fmt: skip
        net = blend * form + (1 - blend) * roster
        poss = float(np.nanmean([pace.get(g.home_team_id, np.nan),
                                 pace.get(g.away_team_id, np.nan)]))  # fmt: skip
        margin = net * poss / 100
        game_rows.append(
            {
                "game_id": g.game_id,
                "game_date": g.game_date,
                "tipoff": g.tipoff,
                "home_team_id": g.home_team_id,
                "away_team_id": g.away_team_id,
                "net_form": form,
                "net_roster": roster,
                "pred_net": net,
                "pace": poss,
                "pred_margin": margin,
                "home_win_prob": 0.5 * (1 + math.erf(margin / sigma / math.sqrt(2))),
            }
        )
    team = pd.DataFrame(team_rows).merge(season_avg, on=["team_id", "measure"], how="left")
    team["delta"] = team.pred_rating - team.season_avg

    # players: recent form shrunk to season level, plus the opponent's effect
    pgm = player_games(con, season)
    pgm["game_date"] = pd.to_datetime(pgm.game_date)
    pgm = pgm[pgm.game_date < asof]
    pgm["rk"] = pgm.groupby(["person_id", "measure"]).game_date.rank(ascending=False)
    pgm["w"] = pgm.exposure * 0.5 ** ((pgm.rk - 1) / PLAYER_HALF_LIFE)
    pgm["wv"], pgm["ev"] = pgm.w * pgm.value, pgm.exposure * pgm.value
    agg = pgm.groupby(["person_id", "measure"])[["wv", "w", "ev", "exposure"]].sum()
    form = pd.DataFrame({"recent": agg.wv / agg.w, "recent_exp": agg.w,
                         "season": agg.ev / agg.exposure}).reset_index()  # fmt: skip
    form["level"] = ((form.recent * form.recent_exp + form.season * PLAYER_K)
                     / (form.recent_exp + PLAYER_K))  # fmt: skip
    # every listed game gets both teams' expected players (a team can appear in two
    # listed games: its own next game and an opponent's next game)
    sides = [(g.game_id, tid, opp)
             for g in games.itertuples()
             for tid, opp in ((g.home_team_id, g.away_team_id),
                              (g.away_team_id, g.home_team_id))]  # fmt: skip
    player_rows = [_player_row(p, gid, opp, form, models, ref_p)
                   for gid, tid, opp in sides
                   for p in mins[mins.team_id == tid].itertuples()]  # fmt: skip
    players = pd.DataFrame(player_rows).merge(impact, on="person_id", how="left")
    return {"games": pd.DataFrame(game_rows), "team": team, "players": players}


def _player_row(p, gid, opp, form, models, ref_p) -> dict:
    """A player's predicted ratings: recent form shrunk to season level + opponent effect."""
    row = {"game_id": gid, "team_id": p.team_id, "person_id": p.person_id,
           "exp_minutes": p.exp_minutes}  # fmt: skip
    f = form[form.person_id == p.person_id].set_index("measure")
    for m in PLAYER_MEASURES:
        if m not in f.index:
            row[f"{m}_rating"] = np.nan
            continue
        model = models.get(m)
        o = model["opp"][model["teams"][opp]] if model and opp in model["teams"].index else 0.0
        v = f.loc[m, "level"] + o
        row[f"{m}_value"] = v
        row[f"{m}_rating"] = _rating(v, ref_p, m)
    return row


# ---- backtest ---------------------------------------------------------------------------------


def backtest(con, season: int, start_day: int = 21, step_days: int = 7) -> dict:
    """Re-predict ``season`` week by week from games before each week. Team-measure error
    against a season-to-date-average baseline; margin error and win-chance calibration
    for each blend of the form and roster views (the roster view uses last season's
    impact, so nothing from the predicted season leaks into it)."""
    tg = team_games(con, [season - 1, season])
    cur = tg[tg.season == season]
    first = cur.game_date.min()
    impact_prev = con.execute(
        "select person_id, impact from metrics.player_impact where season = ?", [season - 1]
    ).df()
    pace = (
        con.execute(
            "select team_id, avg(poss) as pace from metrics.team_game_counts where season = ? "
            "and season_type = 'regular' and pbp_ok group by team_id",
            [season - 1],
        )
        .df()
        .set_index("team_id")
        .pace
    )
    hca = con.execute("select avg(home_court) from metrics.player_impact where season = ?",
                      [season - 1]).fetchall()[0][0] or 0.0  # fmt: skip
    games = con.execute(
        """select game_id, game_date, home_team_id, away_team_id, home_score - away_score as margin
           from stg.games where season = ? and season_type = 'regular'
             and home_score is not null""",
        [season],
    ).df()
    games["game_date"] = pd.to_datetime(games.game_date)
    errs, base_errs, gm = [], [], []
    asof = first + pd.Timedelta(days=start_day)
    while asof <= cur.game_date.max():
        nxt = asof + pd.Timedelta(days=step_days)
        models = team_models(tg, asof, season)
        week = cur[(cur.game_date >= asof) & (cur.game_date < nxt)]
        hist = cur[cur.game_date < asof]
        avg = hist.groupby(["team_id", "measure"]).value.mean()
        for r in week.itertuples():
            if r.measure in models and r.measure != "net":
                p = predict_team(models[r.measure], r.team_id, r.opp_id, r.home)
                errs.append((r.measure, abs(p - r.value)))
                if (r.team_id, r.measure) in avg.index:
                    base_errs.append((r.measure, abs(avg[(r.team_id, r.measure)] - r.value)))
        mins = expected_minutes(con, season, asof)
        for g in games[(games.game_date >= asof) & (games.game_date < nxt)].itertuples():
            if "net" not in models:
                continue
            form = 0.5 * (
                predict_team(models["net"], g.home_team_id, g.away_team_id, True)
                - predict_team(models["net"], g.away_team_id, g.home_team_id, False)
            )
            roster = (roster_net(mins, impact_prev, g.home_team_id)
                      - roster_net(mins, impact_prev, g.away_team_id) + hca)  # fmt: skip
            poss = float(np.nanmean([pace.get(g.home_team_id, np.nan),
                                     pace.get(g.away_team_id, np.nan)]))  # fmt: skip
            gm.append({"form": form * poss / 100, "roster": roster * poss / 100,
                       "margin": g.margin})  # fmt: skip
        asof = nxt
    e = pd.DataFrame(errs, columns=["measure", "err"]).groupby("measure").err.mean()
    b = pd.DataFrame(base_errs, columns=["measure", "err"]).groupby("measure").err.mean()
    team = pd.DataFrame({"model_mae": e, "season_avg_mae": b}).reset_index()
    gm = pd.DataFrame(gm)
    rows = []
    for blend in (0.0, 0.25, 0.5, 0.75, 1.0):
        pred = blend * gm.form + (1 - blend) * gm.roster
        sigma = float(np.sqrt(((gm.margin - pred) ** 2).mean()))
        prob = 0.5 * (1 + pred.map(lambda v, s=sigma: math.erf(v / s / math.sqrt(2))))
        win = (gm.margin > 0).astype(float)
        rows.append({"blend_form": blend, "margin_mae": float((gm.margin - pred).abs().mean()),
                     "sigma": sigma, "brier": float(((prob - win) ** 2).mean()),
                     "accuracy": float(((pred > 0) == (gm.margin > 0)).mean()),
                     "games": len(gm)})  # fmt: skip
    rows.append({"blend_form": np.nan, "margin_mae": float(gm.margin.abs().mean()),
                 "sigma": np.nan, "brier": 0.25, "accuracy": float((gm.margin > 0).mean()),
                 "games": len(gm)})  # baseline: pick-'em / always home  # fmt: skip
    return {"team": team, "games": pd.DataFrame(rows)}


def build(con: duckdb.DuckDBPyConnection) -> dict:
    season = con.execute("select max(season) from stg.games where home_score is not null"
                         ).fetchall()[0][0]  # fmt: skip
    bt = backtest(con, season - 1)
    g = bt["games"].dropna(subset=["blend_form"])
    best = g.loc[g.brier.idxmin()]
    res = predict(con, season, float(best.blend_form), float(best.sigma))
    lines = pd.DataFrame()
    has_wf = con.execute("select count(*) from information_schema.tables where "
                         "table_schema = 'forecast' and table_name = 'margin_model'"
                         ).fetchall()[0][0]  # fmt: skip
    if has_wf and not res["games"].empty:
        # the walk-forward-tested model replaces the blend for margin and win chance (it
        # adds rest and back-to-backs), and adds each player's stat line with a range
        from courtvision.walkforward import live

        wf = live(con, season, res["games"])
        gm = res["games"].rename(columns={"pred_margin": "blend_margin",
                                          "home_win_prob": "blend_win_prob"})  # fmt: skip
        res["games"] = gm.merge(wf["games"].drop(columns=["pace"]), on="game_id", how="left")
        lines = wf["players"]
    con.execute("create schema if not exists forecast")
    for name, df in (("games", res["games"]), ("team", res["team"]),
                     ("players", res["players"]), ("backtest_team", bt["team"]),
                     ("backtest_games", bt["games"]), ("player_lines", lines)):  # fmt: skip
        if df.empty and name == "player_lines":
            continue
        con.register("tmp_df", df)
        con.execute(f"create or replace table forecast.{name} as select * from tmp_df")
        con.unregister("tmp_df")
    log.info("forecast: %d upcoming games; backtest blend %.2f, sigma %.1f, margin MAE %.1f",
             len(res["games"]), best.blend_form, best.sigma, best.margin_mae)  # fmt: skip
    return {"blend": float(best.blend_form), "sigma": float(best.sigma),
            "games": len(res["games"])}  # fmt: skip
