"""Walk-forward prediction engine: games, team dimensions, player stat lines (spec 16).

Everything is predicted from data before each weekly cutoff, then compared with what
happened, season after season. ``courtvision walkforward`` writes schema ``forecast``:

* ``wf_games``      every game of the evaluation seasons with each model's predicted
                    margin and home win chance, the features, and the result
* ``wf_eval``       per model and season: margin error, winners, Brier, log loss
* ``wf_calibration``predicted vs actual home win rate by decile (the chosen model)
* ``wf_players``    every predicted player-game: expected minutes and stat line vs actual
* ``wf_player_eval``per stat: error of the model vs season-average baseline, interval
                    coverage, and what each ingredient (form, pace, positional matchup) adds
* ``margin_model``  the chosen model's coefficients and sigma, and the player-range
                    widths (``model = 'player intervals'``), for live predictions
* ``wf_residuals``  each game's margin against the base prediction, by coach

``live`` applies the chosen model and the player lines to upcoming games (used by
``courtvision forecast``).

**Stage 1 (per game, from data before the cutoff):** team form on net rating (team +
opponent + site, recent games weighted, last season at a quarter weight); the roster
view (last season's per-possession impact over expected minutes: no leakage from the
predicted season); expected pace; rest (days since each team's last game, back-to-backs);
coach (the coach's average margin against expectation so far, shrunk); matchup style
interactions (three-point reliance vs threes allowed, offensive vs defensive rebounding,
ball security vs forced turnovers, paint reliance vs paint defense).

**Stage 2:** a linear model of the home margin on those features, trained only on
earlier seasons' walk-forward predictions (expanding window). Feature groups are added
one at a time; each model is scored on the same games, so what each group adds is
measured, not assumed.

**Players:** expected minutes when he plays (recent games weighted; a team's total is
scaled down only past 250); per-minute rates for points, rebounds, assists, threes,
steals + blocks and turnovers from recent games (shrunk toward last season); x expected
pace / team pace; x the opponent's allowance to the player's position group (guards,
wings, bigs) for that stat, shrunk toward league average. 80% ranges: c * sqrt(expected),
c fitted on the first two evaluation seasons and checked on the rest.
"""

from __future__ import annotations

import logging
import math

import duckdb
import numpy as np
import pandas as pd

from courtvision.forecast import fit_team_model, predict_team, weights

log = logging.getLogger(__name__)

EVAL_SEASONS = [2021, 2022, 2023, 2024, 2025]
WARMUP = 2020  # stage-1 outputs used only to train stage 2 for the first eval season
STEP_DAYS = 7
BASE_BLEND = 0.75  # form vs roster for the coach residual (fixed, so no leakage)
K_COACH = 50  # games of prior for a coach's margin against expectation
STATS = ["pts", "reb", "ast", "fg3m", "stocks", "tov"]
PLAYER_HALF_LIFE = 25.0  # games, per-minute rates
MINUTES_HALF_LIFE = 5.0  # games, expected minutes
K_RATE = 250.0  # minutes of prior (last season's rate) for per-minute rates
K_DVP = 12.0  # games of prior for an opponent's allowance to a position group
FEATURES = {
    "home court only": [],
    "team form": ["form"],
    "form + roster": ["form", "roster"],
    "+ rest": ["form", "roster", "b2b_h", "b2b_a", "rest_diff"],
    "+ coach": ["form", "roster", "b2b_h", "b2b_a", "rest_diff", "coach_diff"],
    "+ matchups": ["form", "roster", "b2b_h", "b2b_a", "rest_diff", "coach_diff",
                   "m_three", "m_reb", "m_tov", "m_paint"],
}  # fmt: skip
STYLE = {  # team rate, the opponent's rate allowed: (numerator, denominator)
    "three": (("b_fg3a", "b_fga"), ("o_b_fg3a", "o_b_fga")),
    "reb": (("b_oreb", "b_oreb + o_b_dreb"), ("o_b_oreb", "o_b_oreb + b_dreb")),
    "tov": (("b_tov", "b_poss"), ("o_b_tov", "o_b_poss")),
    "paint": (("fga_paint", "fga_r"), ("o_fga_paint", "o_fga_r")),
}


# ---- data ---------------------------------------------------------------------------------


def load(con, seasons: list[int]) -> dict:
    s = ",".join(map(str, seasons))
    games = con.execute(f"""
        select g.game_id, g.season, g.game_date, g.home_team_id, g.away_team_id,
               g.home_score - g.away_score as margin
        from stg.games g
        where g.season in ({s}) and g.season_type = 'regular' and g.home_score is not null
        order by g.game_date, g.game_id""").df()
    games["game_date"] = pd.to_datetime(games.game_date)
    style_cols = ", ".join(sorted({c for pair in STYLE.values() for side in pair for x in side
                                   for c in x.replace("+", " ").split()}))  # fmt: skip
    teams = con.execute(f"""
        select c.game_id, c.season, c.game_date, c.team_id, c.opponent_team_id as opp_id,
               c.team_id = g.home_team_id as home, c.poss, c.pbp_ok,
               100 * (c.pts_fg + c.ftm - c.o_pts_fg - c.o_ftm) / nullif(c.poss, 0) as net,
               {style_cols}
        from metrics.team_game_counts c join stg.games g using (game_id)
        where c.season in ({s}) and c.season_type = 'regular'""").df()
    teams["game_date"] = pd.to_datetime(teams.game_date)
    players = con.execute(f"""
        select c.game_id, c.season, c.game_date, c.team_id, c.opponent_team_id as opp_id,
               c.person_id, c.minutes, c.b_pts as pts, c.b_oreb + c.b_dreb as reb,
               c.b_ast as ast, c.b_fg3m as fg3m, c.b_stl + c.b_blk as stocks, c.b_tov as tov,
               case when p.is_center then 'B'
                    when p.is_guard and not coalesce(p.is_forward, false) then 'G'
                    else 'W' end as grp
        from metrics.player_game_counts c left join stg.players p using (person_id)
        where c.season in ({s}) and c.season_type = 'regular'""").df()
    players["game_date"] = pd.to_datetime(players.game_date)
    impact = con.execute(f"""select season, person_id, impact, home_court
                             from metrics.player_impact where season in ({s})""").df()
    coach = con.execute(f"""select game_id, team_id, coach_id from coach.assignments
                            where season in ({s})""").df()
    newc = con.execute("""select newcomer_off + newcomer_def as v from metrics.impact_lambda
                          where mode = 'headline'""").fetchall()  # fmt: skip
    return {"games": games, "teams": teams, "players": players, "impact": impact,
            "coach": coach, "newcomer": newc[0][0] if newc else -0.6}  # fmt: skip


# ---- stage 1 --------------------------------------------------------------------------------


def rest_days(games: pd.DataFrame) -> pd.DataFrame:
    """Days since each team's previous game in the same season (first game: 3)."""
    long = pd.concat([games[["game_id", "season", "game_date", "home_team_id"]]
                      .rename(columns={"home_team_id": "team_id"}),
                      games[["game_id", "season", "game_date", "away_team_id"]]
                      .rename(columns={"away_team_id": "team_id"})])  # fmt: skip
    long = long.sort_values(["team_id", "game_date"])
    long["rest"] = long.groupby(["team_id", "season"]).game_date.diff().dt.days
    long["rest"] = long.rest.fillna(3).clip(upper=3)
    return long.set_index(["game_id", "team_id"]).rest


def style_rates(teams: pd.DataFrame, before: pd.Timestamp, season: int) -> pd.DataFrame:
    """Season-to-date style rates per team relative to league (last season if early)."""
    h = teams[(teams.game_date < before) & (teams.season == season)]
    fewest = h.groupby("team_id").size().min() if len(h) else 0
    if fewest < 8:  # early season: include last season
        h = teams[(teams.game_date < before) & (teams.season >= season - 1)]
    out = {}
    for name, (own, allowed) in STYLE.items():
        for tag, (num, den) in (("own", own), ("allowed", allowed)):
            n = h.groupby("team_id")[num].sum()
            d = h.eval(den).groupby(h.team_id).sum()
            lg = h[num].sum() / h.eval(den).sum()
            out[f"{name}_{tag}"] = n / d - lg
    return pd.DataFrame(out)


def expected_minutes(players: pd.DataFrame, before: pd.Timestamp, season: int) -> pd.DataFrame:
    """Expected minutes per player for his team's next game, if he plays: his recent games
    weighted (half-life ``MINUTES_HALF_LIFE`` team games), for players in one of the
    team's last three games; a team's total is scaled down only past 250."""
    p = players[(players.season == season) & (players.game_date < before)]
    if p.empty:  # season opener: last season's final weeks
        p = players[(players.season == season - 1) & (players.game_date < before)]
    if p.empty:
        return pd.DataFrame(columns=["person_id", "team_id", "exp_minutes"])
    tg = p[["team_id", "game_id", "game_date"]].drop_duplicates()
    tg["rk"] = tg.groupby("team_id").game_date.rank(ascending=False, method="first")
    tg = tg[tg.rk <= 15]
    roster = p.merge(tg[["team_id", "rk"]].drop_duplicates(), on="team_id")[
        ["team_id", "person_id"]].drop_duplicates()  # fmt: skip
    # minutes when he plays (a projection is conditional on playing; availability is a
    # separate question the data can't answer)
    full = roster.merge(tg, on="team_id").merge(
        p[["game_id", "person_id", "minutes"]], on=["game_id", "person_id"], how="inner"
    )
    full = full[full.minutes > 0]
    full["w"] = 0.5 ** ((full.rk - 1) / MINUTES_HALF_LIFE)
    full["wm"] = full.w * full.minutes
    agg = full.groupby(["person_id", "team_id"])[["wm", "w"]].sum()
    recent = full[full.rk <= 3].groupby(["person_id", "team_id"]).minutes.sum()
    out = (agg.wm / agg.w).rename("exp_minutes").reset_index()
    out = out[out.set_index(["person_id", "team_id"]).index.isin(recent[recent > 0].index)]
    # a returning player squeezes the others: scale down only past a full game
    tot = out.groupby("team_id").exp_minutes.transform("sum")
    out["exp_minutes"] = out.exp_minutes * np.minimum(1.0, 250 / tot.where(tot > 0, 1))
    return out


def stage1(data: dict, games: pd.DataFrame, cutoff: pd.Timestamp, season: int,
           coach_resid: pd.Series) -> pd.DataFrame:  # fmt: skip
    """Stage-1 features for ``games`` (home perspective) from data before ``cutoff``."""
    t = data["teams"]
    hist = t[(t.game_date < cutoff) & (t.season >= season - 1) & t.pbp_ok]
    model = fit_team_model(hist.rename(columns={"net": "value"}),
                           weights(hist.game_date, hist.season, cutoff, season))  # fmt: skip
    pace = hist[hist.season == season].groupby("team_id").poss.mean()
    pace_prev = hist[hist.season == season - 1].groupby("team_id").poss.mean()
    imp = data["impact"][data["impact"].season == season - 1].set_index("person_id").impact
    hca = data["impact"][data["impact"].season == season - 1].home_court.mean()
    mins = expected_minutes(data["players"], cutoff, season)
    mins["impact"] = mins.person_id.map(imp).fillna(data["newcomer"])
    roster = (mins.impact * mins.exp_minutes / 48).groupby(mins.team_id).sum()
    style = style_rates(t, cutoff, season)
    if "rest" not in data:
        data["rest"] = rest_days(data["games"])
        data["coach_idx"] = data["coach"].set_index(["game_id", "team_id"]).coach_id
    rest, coach = data["rest"], data["coach_idx"]
    rows = []
    for g in games.itertuples():
        h, a = g.home_team_id, g.away_team_id
        form = 0.5 * (predict_team(model, h, a, True) - predict_team(model, a, h, False))
        p_h = pace.get(h, pace_prev.get(h, np.nan))
        p_a = pace.get(a, pace_prev.get(a, np.nan))
        poss = float(np.nanmean([p_h, p_a])) if not (np.isnan(p_h) and np.isnan(p_a)) else 99.0
        ch, ca = coach.get((g.game_id, h)), coach.get((g.game_id, a))

        def sty(name, team, opp):
            if team not in style.index or opp not in style.index:
                return 0.0
            return float(style.loc[team, f"{name}_own"] * style.loc[opp, f"{name}_allowed"])

        rows.append({
            "game_id": g.game_id, "season": season, "game_date": g.game_date,
            "home_team_id": h, "away_team_id": a, "margin": g.margin, "pace": poss,
            "form": form * poss / 100,
            "roster": (roster.get(h, 0.0) - roster.get(a, 0.0) + hca) * poss / 100,
            "rest_h": rest.get((g.game_id, h), 3), "rest_a": rest.get((g.game_id, a), 3),
            "coach_h": ch, "coach_a": ca,
            "coach_diff": coach_resid.get(ch, 0.0) - coach_resid.get(ca, 0.0),
            # interaction: home's reliance x away's allowance, minus the reverse; scaled
            "m_three": 100 * (sty("three", h, a) - sty("three", a, h)),
            "m_reb": 100 * (sty("reb", h, a) - sty("reb", a, h)),
            "m_tov": -100 * (sty("tov", h, a) - sty("tov", a, h)),
            "m_paint": 100 * (sty("paint", h, a) - sty("paint", a, h)),
        })  # fmt: skip
    out = pd.DataFrame(rows)
    out["b2b_h"] = (out.rest_h == 1).astype(float)
    out["b2b_a"] = (out.rest_a == 1).astype(float)
    out["rest_diff"] = out.rest_h - out.rest_a
    out["base_pred"] = BASE_BLEND * out.form + (1 - BASE_BLEND) * out.roster
    return out


# ---- players -----------------------------------------------------------------------------


def player_lines(data: dict, games: pd.DataFrame, cutoff: pd.Timestamp, season: int,
                 paces: dict | None = None) -> pd.DataFrame:  # fmt: skip
    """Predicted stat lines for every expected player of every team in ``games``."""
    p = data["players"]
    cur = p[(p.season == season) & (p.game_date < cutoff)]
    prev = p[p.season == season - 1]
    mins = expected_minutes(p, cutoff, season)
    if mins.empty:
        return pd.DataFrame()
    base = prev if prev.minutes.sum() > 0 else cur
    lg_rate = {s: base[s].sum() / base.minutes.sum() for s in STATS}
    # per-minute rates: recent games weighted, shrunk toward last season's rate
    c = cur.assign(rk=cur.groupby("person_id").game_date.rank(ascending=False, method="first"))
    c["w"] = 0.5 ** ((c.rk - 1) / PLAYER_HALF_LIFE)
    who = pd.Index(mins.person_id.unique(), name="person_id")
    den = (c.w * c.minutes).groupby(c.person_id).sum().reindex(who).fillna(0)
    prev_min = prev.groupby("person_id").minutes.sum()
    rates = pd.DataFrame(index=who)
    for s in STATS:
        num = (c.w * c[s]).groupby(c.person_id).sum().reindex(who).fillna(0)
        prior = prev.groupby("person_id")[s].sum() / prev_min.where(prev_min > 0)
        prior = prior.reindex(who).fillna(lg_rate[s] * 0.8)  # newcomers: below average
        rates[s] = (num + prior * K_RATE) / (den + K_RATE)
    # opponent's allowance per player-minute to each position group, vs league, shrunk
    pool = cur if cur.game_id.nunique() >= 60 else p[(p.season >= season - 1)
                                                     & (p.game_date < cutoff)]  # fmt: skip
    lg = pool.groupby("grp")[STATS + ["minutes"]].sum()
    lg_r = lg[STATS].div(lg.minutes, axis=0)
    al = pool.groupby(["opp_id", "grp"])[STATS + ["minutes"]].sum()
    al_r = al[STATS].div(al.minutes.where(al.minutes > 0), axis=0)
    fac = al_r / lg_r.reindex(al_r.index.get_level_values("grp")).to_numpy()
    n = pool.groupby("opp_id").game_id.nunique().reindex(
        al_r.index.get_level_values("opp_id")).fillna(0).to_numpy()[:, None]  # fmt: skip
    dvp = ((fac.fillna(1.0) * n + K_DVP) / (n + K_DVP)).add_suffix("_dvp").reset_index()
    tt = data["teams"]
    team_pace = tt[(tt.season == season) & (tt.game_date < cutoff)].groupby("team_id").poss.mean()
    if team_pace.empty:
        team_pace = data["teams"][data["teams"].season == season - 1].groupby("team_id").poss.mean()
    sides = pd.concat([
        games.assign(team_id=games.home_team_id, opp_id=games.away_team_id, home=True),
        games.assign(team_id=games.away_team_id, opp_id=games.home_team_id, home=False),
    ])[["game_id", "team_id", "opp_id", "home"]]  # fmt: skip
    sides["pace_factor"] = [
        (paces or {}).get(g, np.nan) / team_pace.get(t, np.nan)
        for g, t in zip(sides.game_id, sides.team_id, strict=True)
    ]
    sides["pace_factor"] = sides.pace_factor.where(np.isfinite(sides.pace_factor), 1.0)
    grp = p.drop_duplicates("person_id", keep="last").set_index("person_id").grp
    out = sides.merge(mins, on="team_id")
    out["grp"] = out.person_id.map(grp).fillna("W")
    out = out.merge(rates.reset_index(), on="person_id", how="left", suffixes=("", "_rate"))
    out = out.merge(dvp, left_on=["opp_id", "grp"], right_on=["opp_id", "grp"], how="left")
    for s in STATS:
        out[f"{s}_dvp"] = out[f"{s}_dvp"].fillna(1.0)
        rate = out[s].fillna(lg_rate[s])
        out[f"{s}_base"] = rate * out.exp_minutes
        out[s] = out[f"{s}_base"] * out.pace_factor * out[f"{s}_dvp"]
    return out


# ---- stage 2 -------------------------------------------------------------------------------


def fit_stage2(train: pd.DataFrame, feats: list[str]) -> dict:
    x = np.column_stack([np.ones(len(train)), *(train[f].to_numpy(float) for f in feats)])
    y = train.margin.to_numpy(float)
    pen = np.full(x.shape[1], 1.0)
    pen[0] = 1e-6
    beta = np.linalg.solve(x.T @ x + np.diag(pen), x.T @ y)
    sigma = float(np.sqrt(np.mean((y - x @ beta) ** 2)))
    return {"features": feats, "beta": beta, "sigma": sigma}


def apply_stage2(model: dict, df: pd.DataFrame) -> np.ndarray:
    x = np.column_stack([np.ones(len(df)), *(df[f].to_numpy(float) for f in model["features"])])
    return x @ model["beta"]


def _phi(z):
    return 0.5 * (1 + np.vectorize(math.erf)(z / math.sqrt(2)))


def score(df: pd.DataFrame, pred: np.ndarray, sigma: float) -> dict:
    y = df.margin.to_numpy(float)
    p = np.clip(_phi(pred / sigma), 1e-4, 1 - 1e-4)
    win = (y > 0).astype(float)
    return {"games": len(df), "margin_mae": float(np.mean(np.abs(y - pred))),
            "margin_rmse": float(np.sqrt(np.mean((y - pred) ** 2))),
            "accuracy": float(np.mean((pred > 0) == (y > 0))),
            "brier": float(np.mean((p - win) ** 2)),
            "log_loss": float(-np.mean(win * np.log(p) + (1 - win) * np.log(1 - p)))}  # fmt: skip


# ---- run ------------------------------------------------------------------------------------


def run(con: duckdb.DuckDBPyConnection) -> dict:
    seasons = list(range(WARMUP - 1, max(EVAL_SEASONS) + 1))
    data = load(con, seasons)
    games = data["games"]
    stage, plines, resid = [], [], []  # resid: (game_id, coach_id, residual, date)
    for season in [WARMUP, *EVAL_SEASONS]:
        sg = games[games.season == season]
        cutoff, end = sg.game_date.min(), sg.game_date.max()
        while cutoff <= end:
            nxt = cutoff + pd.Timedelta(days=STEP_DAYS)
            week = sg[(sg.game_date >= cutoff) & (sg.game_date < nxt)]
            if not week.empty:
                r = pd.DataFrame(resid, columns=["coach_id", "res", "date"])
                r = r[r.date < cutoff]
                cr = r.groupby("coach_id").res.sum() / (r.groupby("coach_id").size() + K_COACH)
                s1 = stage1(data, week, cutoff, season, cr)
                stage.append(s1)
                for x in s1.itertuples():  # residuals become known once the games are played
                    res = x.margin - x.base_pred
                    if x.coach_h:
                        resid.append((x.coach_h, res, x.game_date))
                    if x.coach_a:
                        resid.append((x.coach_a, -res, x.game_date))
                if season in EVAL_SEASONS:
                    paces = dict(zip(s1.game_id, s1.pace, strict=True))
                    plines.append(player_lines(data, week, cutoff, season, paces))
            cutoff = nxt
        log.info("walk-forward %s done", season)
    st = pd.concat(stage, ignore_index=True)

    # stage 2: expanding window over earlier seasons, every feature set
    rows, preds = [], {}
    for name, feats in FEATURES.items():
        col = np.full(len(st), np.nan)
        for season in EVAL_SEASONS:
            train, test = st[st.season < season], st[st.season == season]
            m = fit_stage2(train, feats)
            p = apply_stage2(m, test)
            col[test.index] = p
            rows.append({"model": name, "season": season, **score(test, p, m["sigma"])})
        preds[name] = col
    ev = pd.DataFrame(rows)
    overall = ev.groupby("model").apply(
        lambda d: pd.Series({k: np.average(d[k], weights=d.games) for k in
                             ["margin_mae", "margin_rmse", "accuracy", "brier", "log_loss"]}
                            | {"games": d.games.sum()}), include_groups=False,
    ).reset_index()  # fmt: skip
    overall["season"] = 0
    ev = pd.concat([ev, overall], ignore_index=True)
    best = overall.sort_values("log_loss").model.iloc[0]
    for name, col in preds.items():
        st[f"pred_{name}"] = col
    # calibration of the chosen model
    test = st[st.season.isin(EVAL_SEASONS)].copy()
    final = fit_stage2(st[st.season.isin(EVAL_SEASONS)], FEATURES[best])
    sig = float(np.sqrt(np.mean((test.margin - test[f"pred_{best}"]) ** 2)))
    test["p_home"] = _phi(test[f"pred_{best}"] / sig)
    test["bin"] = pd.qcut(test.p_home, 10, labels=False, duplicates="drop")
    cal = test.groupby("bin").agg(predicted=("p_home", "mean"),
                                  actual=("margin", lambda m: float((m > 0).mean())),
                                  games=("margin", "size")).reset_index()  # fmt: skip
    # players
    pl = pd.concat([x for x in plines if not x.empty], ignore_index=True)
    act = data["players"][["game_id", "person_id", "minutes", *STATS]]
    pl = pl.merge(act, on=["game_id", "person_id"], how="left", suffixes=("", "_act"))
    pl = pl.fillna({f"{s}_act": 0 for s in STATS} | {"minutes": 0})
    # season-to-date per-game average: the baseline a fan or a box score would use
    pg = data["players"].sort_values("game_date")
    for s in STATS:
        pg[f"{s}_avg"] = pg.groupby(["person_id", "season"])[s].transform(
            lambda v: v.shift().expanding().mean())  # fmt: skip
    pl = pl.merge(pg[["game_id", "person_id", *[f"{s}_avg" for s in STATS]]],
                  on=["game_id", "person_id"], how="left")  # fmt: skip
    pev, spread = [], {}
    played = pl[pl.minutes > 0].merge(st[["game_id", "season"]], on="game_id")
    fit_on = played.season.isin(EVAL_SEASONS[:2])  # interval widths: first two seasons
    check = ~fit_on  # coverage: the seasons after
    for s in STATS:
        err = played[s] - played[f"{s}_act"]
        b = played[f"{s}_avg"].fillna(played[f"{s}_base"])
        # spread grows with the expected count: sd = c * sqrt(pred). c is set so 80% of the
        # fit seasons' misses fall inside (counts are peaked with long tails, so an RMS-based
        # width over-covers), then tested on the later seasons
        z = (err[fit_on].abs() / np.sqrt(np.maximum(played[s][fit_on], 0.5))).quantile(0.8)
        c = float(z / 1.2816)
        spread[s] = c
        sd = c * np.sqrt(np.maximum(played[s], 0.5))
        inside = (played[f"{s}_act"] >= played[s] - 1.2816 * sd) & (
            played[f"{s}_act"] <= played[s] + 1.2816 * sd)  # fmt: skip
        pev.append({"stat": s, "player_games": len(played),
                    "model_mae": float(err.abs().mean()),
                    "no_matchup_mae": float((played[f"{s}_base"] * played.pace_factor
                                             - played[f"{s}_act"]).abs().mean()),
                    "form_only_mae": float((played[f"{s}_base"] - played[f"{s}_act"]).abs().mean()),
                    "season_avg_mae": float((b - played[f"{s}_act"]).abs().mean()),
                    "corr": float(played[s].corr(played[f"{s}_act"])),
                    "coverage_80": float(inside[check].mean()), "spread_c": c})  # fmt: skip
    pev.append({"stat": "minutes (when he plays)", "player_games": len(played),
                "model_mae": float((played.exp_minutes - played.minutes).abs().mean()),
                "season_avg_mae": float("nan"),
                "corr": float(played.exp_minutes.corr(played.minutes)),
                "no_matchup_mae": float("nan"), "form_only_mae": float("nan"),
                "coverage_80": float("nan"), "spread_c": float("nan")})  # fmt: skip
    pev.append({"stat": "played (of expected players)", "player_games": len(pl),
                "model_mae": float("nan"), "season_avg_mae": float("nan"),
                "corr": float((pl.minutes > 0).mean()), "no_matchup_mae": float("nan"),
                "form_only_mae": float("nan"), "coverage_80": float("nan"),
                "spread_c": float("nan")})  # fmt: skip
    pev = pd.DataFrame(pev)

    margin_model = pd.DataFrame({"feature": ["intercept", *final["features"]],
                                 "beta": final["beta"]})  # fmt: skip
    margin_model["model"], margin_model["sigma"] = best, final["sigma"]
    margin_model = pd.concat([margin_model, pd.DataFrame(
        {"feature": [f"spread_{s}" for s in STATS], "beta": [spread[s] for s in STATS],
         "model": "player intervals", "sigma": np.nan})], ignore_index=True)  # fmt: skip
    resid_df = st[["game_id", "season", "game_date", "home_team_id", "away_team_id",
                   "coach_h", "coach_a", "margin", "base_pred"]].copy()  # fmt: skip
    con.execute("create schema if not exists forecast")
    keep_pl = ["game_id", "team_id", "opp_id", "person_id", "grp", "exp_minutes", "minutes",
               *STATS, *[f"{s}_act" for s in STATS], *[f"{s}_avg" for s in STATS]]  # fmt: skip
    for name, df in (("wf_games", st), ("wf_eval", ev), ("wf_calibration", cal),
                     ("wf_players", pl[keep_pl]), ("wf_player_eval", pev),
                     ("margin_model", margin_model), ("wf_residuals", resid_df)):  # fmt: skip
        con.register("tmp_df", df)
        con.execute(f"create or replace table forecast.{name} as select * from tmp_df")
        con.unregister("tmp_df")
    return {"best": best, "overall": overall, "players": pev}


# ---- live: the tested model on upcoming games ------------------------------------------------


def schedule_rest(con, games: pd.DataFrame) -> pd.Series:
    """Days since each team's previous game (played or scheduled) for upcoming ``games``,
    capped at 3, indexed by (game_id, team_id)."""
    sched = con.execute(
        """select gameDateTimeEst::date as game_date, homeTeamId as h, awayTeamId as a
           from kaggle.league_schedule_26_27
           where gameLabel is distinct from 'Preseason' and homeTeamId > 0 and awayTeamId > 0
           union all
           select game_date, home_team_id, away_team_id from stg.games
           where season_type = 'regular' and home_score is not null"""
    ).df()
    sched["game_date"] = pd.to_datetime(sched.game_date)
    days = pd.concat([sched[["game_date", "h"]].rename(columns={"h": "team_id"}),
                      sched[["game_date", "a"]].rename(columns={"a": "team_id"})]
                     ).drop_duplicates()  # fmt: skip
    out = {}
    for g in games.itertuples():
        gd = pd.Timestamp(g.game_date)
        for team in (g.home_team_id, g.away_team_id):
            prev = days[(days.team_id == team) & (days.game_date < gd)].game_date
            gap = (gd - prev.max()).days if len(prev) else 3
            out[(g.game_id, team)] = float(min(gap, 3))
    return pd.Series(out)


def live(con: duckdb.DuckDBPyConnection, season: int, games: pd.DataFrame) -> dict:
    """The walk-forward model (``forecast.margin_model``) and player lines with calibrated
    80% ranges, for upcoming ``games`` (game_id, game_date, home/away team ids)."""
    mm = con.execute("select * from forecast.margin_model").df()
    game_m = mm[mm.model != "player intervals"]
    spread = {f.removeprefix("spread_"): b for f, b in
              zip(mm[mm.model == "player intervals"].feature,
                  mm[mm.model == "player intervals"].beta, strict=True)}  # fmt: skip
    data = load(con, [season - 1, season])
    data["rest"] = schedule_rest(con, games)
    data["coach_idx"] = pd.Series(dtype=object)
    g = games[["game_id", "game_date", "home_team_id", "away_team_id"]].copy()
    g["game_date"] = pd.to_datetime(g.game_date)
    g["margin"] = np.nan
    cutoff = g.game_date.min()
    s1 = stage1(data, g, cutoff, season, pd.Series(dtype=float))
    beta = dict(zip(game_m.feature, game_m.beta, strict=True))
    s1["pred_margin"] = beta["intercept"] + sum(beta[f] * s1[f] for f in beta if f != "intercept")
    sigma = float(game_m.sigma.iloc[0])
    s1["home_win_prob"] = _phi(s1.pred_margin / sigma)
    s1["model"] = game_m.model.iloc[0]
    pl = player_lines(data, g, cutoff, season, dict(zip(s1.game_id, s1.pace, strict=True)))
    if not pl.empty:
        for s in STATS:
            half = 1.2816 * spread.get(s, 1.0) * np.sqrt(np.maximum(pl[s], 0.5))
            pl[f"{s}_lo"] = np.maximum(pl[s] - half, 0.0)
            pl[f"{s}_hi"] = pl[s] + half
        keep = ["game_id", "team_id", "opp_id", "person_id", "grp", "exp_minutes",
                *[f"{s}{x}" for s in STATS for x in ("", "_lo", "_hi")]]  # fmt: skip
        pl = pl[keep]
    cols = ["game_id", "form", "roster", "pace", "rest_h", "rest_a", "b2b_h", "b2b_a",
            "rest_diff", "pred_margin", "home_win_prob", "model"]  # fmt: skip
    return {"games": s1[cols], "players": pl, "sigma": sigma}
