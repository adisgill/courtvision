"""Live win probability: score and clock first, then what the film adds.

**Baseline (score and clock).** P(home wins) from the margin, the time left, who has the
ball and the pre-game margin, in the form of Stern's (1994) random walk: the margin over the
square root of the time left, the pre-game margin times it, and possession over it. Fitted
by logistic regression on ten seasons of courtvision's play-by-play (2008-09 to 2018-19,
leaving out 2015-16, the tracking season), one state every 10 s of game time.

**Pre-game margin.** courtvision's own team model (``forecast.fit_team_model``: form plus
opponent plus home court, recent games weighted more, last season at a quarter weight),
refitted every week on game margins from before that week. Point in time, for every season.

**What the film adds** (fitted on the tracking games before 2016-01-01, tested on the rest):
- EPV: the expected points of the current possession beyond an average one, over the root
  of the time left (``epv``), so a breakaway layup counts for more than a late shot clock;
- lineup strength on the floor: the ten players' courtvision impact ratings from the season
  before.

The film models keep the baseline as an offset (with a recalibration slope), so their gain
over it is what the film adds, not a better fit of score and clock.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from courtvision.tracking import epv as epv_mod
from courtvision.tracking import shotquality as sq

log = logging.getLogger(__name__)

BASE_SEASONS = [2008, 2009, 2010, 2011, 2012, 2013, 2014, 2016, 2017, 2018]
TRACKING_SEASON = 2015
GRID = 10  # s of game time between baseline training states
REGULATION = 2880.0
IMPACT_SEASON = 2014  # lineup strength from the season before the tracking season
STEP_DAYS = 7


def out_dir() -> Path:
    return epv_mod.out_dir()


def model_path() -> Path:
    return out_dir() / "winprob_model.json"


# --------------------------------------------------------------------------- pre-game


def pregame(con, seasons: list[int]) -> pd.DataFrame:
    """Point-in-time pre-game home margin for every regular-season game of ``seasons``:
    courtvision's team model refitted each week on the margins of earlier games."""
    from courtvision import forecast

    pool = sorted({s for x in seasons for s in (x - 1, x)})
    g = con.execute(f"""
        select game_id, season, game_date, home_team_id, away_team_id,
               home_score - away_score as margin
        from stg.games
        where season_type = 'regular' and home_score is not null
          and season in ({",".join(map(str, pool))})
    """).df()  # fmt: skip
    g["game_date"] = pd.to_datetime(g.game_date)
    tg = pd.concat([
        g.assign(team_id=g.home_team_id, opp_id=g.away_team_id, home=True, value=g.margin),
        g.assign(team_id=g.away_team_id, opp_id=g.home_team_id, home=False, value=-g.margin),
    ], ignore_index=True)  # fmt: skip
    out = []
    for season in seasons:
        sg = g[g.season == season]
        cutoff, end = sg.game_date.min(), sg.game_date.max()
        while cutoff <= end:
            nxt = cutoff + pd.Timedelta(days=STEP_DAYS)
            week = sg[(sg.game_date >= cutoff) & (sg.game_date < nxt)]
            past = tg[(tg.game_date < cutoff) & tg.season.isin([season - 1, season])]
            if len(week) and len(past):
                w = forecast.weights(past.game_date, past.season, cutoff, season)
                m = forecast.fit_team_model(past, w)
                for r in week.itertuples(index=False):
                    h = forecast.predict_team(m, r.home_team_id, r.away_team_id, True)
                    a = forecast.predict_team(m, r.away_team_id, r.home_team_id, False)
                    out.append((r.game_id, (h - a) / 2))
            cutoff = nxt
    return pd.DataFrame(out, columns=["game_id", "pregame"])


# --------------------------------------------------------------------------- baseline


def pbp_states(con, seasons: list[int]) -> pd.DataFrame:
    """One state every ``GRID`` s of game time for every regular-season game: margin (home
    minus away) on the scoreboard, who has the ball (+1 home, -1 away, 0 unknown), seconds
    left, and whether the home team won. Games whose play-by-play doesn't add up to the
    final score are dropped."""
    ss = ",".join(map(str, seasons))
    df = con.execute(f"""
        with ev as (
            select e.game_id, e.seq, e.elapsed_s, p.off_team_id, g.home_team_id,
                   case when e.etype = 'fga' and e.made then e.shot_value
                        when e.etype = 'ft' and e.made then 1 else 0 end
                   * case when e.team_id = g.home_team_id then 1 else -1 end as pts
            from stg.events e
            join stg.games g using (game_id)
            left join metrics.pbp_events p using (game_id, seq)
            where e.season in ({ss}) and e.season_type = 'regular'
        ),
        by_t as (  -- the scoreboard after everything at each moment
            select game_id, elapsed_s, sum(sum(pts)) over (partition by game_id
                   order by elapsed_s rows unbounded preceding) as margin
            from ev group by game_id, elapsed_s
        ),
        own as (
            select game_id, elapsed_s, any_value(case when off_team_id = home_team_id then 1
                   else -1 end) as poss
            from ev where off_team_id is not null group by game_id, elapsed_s
        ),
        games as (
            select g.game_id, g.home_score - g.away_score as final,
                   max(ev.elapsed_s) as length, sum(ev.pts) as pbp_final
            from stg.games g join ev using (game_id) group by all
        ),
        grid as (
            select game_id, final, unnest(range(0, cast(length as bigint), {GRID})) as t
            from games where final = pbp_final
        )
        select grid.game_id, grid.t, grid.final,
               coalesce(b.margin, 0) as margin, coalesce(o.poss, 0) as poss
        from grid
        asof left join by_t b on grid.game_id = b.game_id and b.elapsed_s <= grid.t
        asof left join own o on grid.game_id = o.game_id and o.elapsed_s > grid.t
    """).df()  # fmt: skip
    t = df.t.to_numpy(float)
    ot = np.maximum(t - REGULATION, 0)
    df["s_left"] = np.where(t < REGULATION, REGULATION - t, 300 - np.mod(ot, 300))
    df["home_win"] = (df.final > 0).astype(np.int8)
    return df.drop(columns=["final"])


def base_design(margin, s_left, poss, pregame) -> np.ndarray:
    """Score-and-clock inputs (home's point of view)."""
    u = np.sqrt((np.asarray(s_left, float) + 20.0) / REGULATION)
    m = np.asarray(margin, float) / 10.0
    return np.column_stack([m / u, np.asarray(pregame, float) / 10.0 * u,
                            np.asarray(poss, float) / u, m])  # fmt: skip


def film_design(base_logit, poss, epv, vbar, s_left, lineup=None) -> np.ndarray:
    """The baseline's logit plus what the film adds: the current possession's expected
    points beyond an average one (signed for home) over the root of the time left; and,
    if given, the lineup on the floor (net impact per 100 possessions) times it."""
    u = np.sqrt((np.asarray(s_left, float) + 20.0) / REGULATION)
    d = np.asarray(poss, float) * (np.asarray(epv, float) - vbar)
    cols = [np.asarray(base_logit, float), d / u]
    if lineup is not None:
        cols.append(np.asarray(lineup, float) / 10.0 * u)
    return np.column_stack(cols)


def _pack(m: sq.Logistic) -> dict:
    return {"mu": m.mu.tolist(), "sd": m.sd.tolist(), "w": m.w.tolist()}


def _unpack(d: dict) -> sq.Logistic:
    m = sq.Logistic()
    m.mu, m.sd, m.w = (np.asarray(d[k]) for k in ("mu", "sd", "w"))
    return m


def logit(m: sq.Logistic, X: np.ndarray) -> np.ndarray:
    return m._z(X) @ m.w


def _p(z: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


# --------------------------------------------------------------------------- lineups


def impact_table(con) -> tuple[dict[int, float], float]:
    """courtvision impact ratings (points per 100 possessions) from the season before, and
    the value for a player without one (the median of those who didn't qualify)."""
    d = con.execute(f"""select person_id, impact, qualified from metrics.player_impact
                        where season = {IMPACT_SEASON}""").df()  # fmt: skip
    fill = float(d[~d.qualified].impact.median()) if (~d.qualified).any() else 0.0
    return dict(zip(d.person_id.astype(int), d.impact.astype(float), strict=True)), fill


def lineup_strength(ids: np.ndarray, impact: dict[int, float], fill: float) -> np.ndarray:
    """(T,) home five's impact minus away five's, for slots (T, 10)."""
    look = np.vectorize(lambda p: impact.get(int(p), fill) if p > 0 else 0.0, otypes=[float])
    v = look(ids)
    return v[:, :5].sum(axis=1) - v[:, 5:].sum(axis=1)


# --------------------------------------------------------------------------- fit + test


def _bootstrap(per_game: pd.DataFrame, a: str, b: str, n: int = 2000, seed: int = 3) -> list:
    """95% interval of the mean per-row log-loss difference a - b, resampling games."""
    rng = np.random.default_rng(seed)
    s, c = per_game[f"ll_{a}"].to_numpy() - per_game[f"ll_{b}"].to_numpy(), per_game.n.to_numpy()
    k = len(s)
    draws = []
    for _ in range(n):
        i = rng.integers(0, k, k)
        draws.append(s[i].sum() / c[i].sum())
    return [round(float(np.quantile(draws, 0.025)), 5), round(float(np.quantile(draws, 0.975)), 5)]


BUCKETS = {
    "all": lambda d: np.ones(len(d), bool),
    "1st quarter": lambda d: (d.period == 1).to_numpy(),
    "2nd quarter": lambda d: (d.period == 2).to_numpy(),
    "3rd quarter": lambda d: (d.period == 3).to_numpy(),
    "4th, before the last 5 min": lambda d: ((d.period == 4) & (d.s_left > 300)).to_numpy(),
    "last 5 min of the 4th": lambda d: ((d.period == 4) & (d.s_left <= 300)).to_numpy(),
    "last 5 min, within 5 points": lambda d: ((d.period == 4) & (d.s_left <= 300)
                                              & (d.margin.abs() <= 5)).to_numpy(),
    "last 2 min, within 5 points": lambda d: ((d.period == 4) & (d.s_left <= 120)
                                              & (d.margin.abs() <= 5)).to_numpy(),
    "overtime": lambda d: (d.period >= 5).to_numpy(),
}  # fmt: skip


def _ll(y: np.ndarray, p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def fit() -> dict:
    """Fit the baseline on play-by-play seasons, the film terms on the training tracking
    games, and score everything on the test tracking games -> ``winprob_model.json``,
    ``winprob_report.json`` and win-probability columns in the rows."""
    import duckdb

    from courtvision.config import get_paths

    con = duckdb.connect(str(get_paths().duckdb), read_only=True)
    pre = pregame(con, [*BASE_SEASONS, TRACKING_SEASON])
    states = pbp_states(con, BASE_SEASONS).merge(pre, on="game_id")
    log.info("baseline states: %d from %d games", len(states), states.game_id.nunique())
    Xb = base_design(states.margin, states.s_left, states.poss, states.pregame)
    base = sq.Logistic(lam=1.0).fit(Xb, states.home_win.to_numpy(float))

    rows = pd.read_parquet(epv_mod.rows_path())
    games = con.execute("""select game_id, (home_score > away_score)::int as home_win
                           from stg.games""").df()  # fmt: skip
    rows = rows.merge(games, on="game_id").merge(pre, on="game_id", how="left")
    rows["pregame"] = rows.pregame.fillna(0.0)
    rows = rows[rows.s_left > 0].reset_index(drop=True)
    impact, fill = impact_table(con)
    rows["lineup"] = lineup_strength(rows[epv_mod.state_slots()].to_numpy(), impact, fill)
    poss = np.select([rows.offense == 0, rows.offense == 1], [1.0, -1.0], 0.0)
    rows["poss"] = poss
    model_epv = json.loads(epv_mod.model_path().read_text())
    vbar = float(model_epv["vbar"])
    zb = logit(base, base_design(rows.margin, rows.s_left, poss, rows.pregame))
    y = rows.home_win.to_numpy(float)
    train = epv_mod._split(rows)
    fold = rows.game_id.str[-1].astype(int).to_numpy() % 2

    designs = {
        "recalibrated": lambda: zb[:, None],
        "film_epv": lambda: film_design(zb, poss, rows.epv, vbar, rows.s_left),
        "film_epv_lineup": lambda: film_design(zb, poss, rows.epv, vbar, rows.s_left,
                                               rows.lineup),
    }  # fmt: skip
    fits, oof = {}, {}
    rows["p_base"] = _p(zb)
    for name, make in designs.items():
        X = make()
        fits[name] = sq.Logistic(lam=1.0).fit(X[train], y[train])
        rows[f"p_{name}"] = _p(logit(fits[name], X))
        o = np.zeros(len(rows))
        for f in (0, 1):  # out of fold within the training games, to choose the live model
            m = sq.Logistic(lam=1.0).fit(X[train & (fold != f)], y[train & (fold != f)])
            sel = train & (fold == f)
            o[sel] = _p(logit(m, X[sel]))
        oof[name] = float(_ll(y[train], o[train]).mean())
    live = min(("film_epv", "film_epv_lineup"), key=lambda k: oof[k])

    # coefficients on the raw scale, to read: what an expected point is worth vs a scored one
    raw = {k: (m.w[1:] / m.sd).tolist() for k, m in fits.items()}
    b_margin = float(base.w[1] / base.sd[0])  # per (margin / 10 / u)
    a, b_epv = raw["film_epv"][0], raw["film_epv"][1]
    point_worth = b_epv / (a * b_margin / 10.0)
    # the EPV term alone on the baseline's own scale (its slope over the recalibration's), so
    # the short-horizon test compares like with like
    d_u = film_design(zb, poss, rows.epv, vbar, rows.s_left)[:, 1]
    rows["p_base_epv"] = _p(zb + b_epv / a * d_u)

    test = rows[~train]
    names = ["base", *designs]
    rep = {"baseline_states": len(states), "baseline_games": int(states.game_id.nunique()),
           "baseline_seasons": BASE_SEASONS, "train_rows": int(train.sum()),
           "test_rows": len(test), "test_games": int(test.game_id.nunique()),
           "train_oof_log_loss": {k: round(v, 5) for k, v in oof.items()}, "live_model": live,
           "epv_point_worth": round(point_worth, 3),
           "coefficients_raw": {k: [round(x, 4) for x in v] for k, v in raw.items()}}  # fmt: skip
    yt = test.home_win.to_numpy(float)
    by = {}
    for bname, sel in BUCKETS.items():
        m = sel(test)
        if m.sum() < 50:
            continue
        by[bname] = {"rows": int(m.sum()), **{
            k: {"log_loss": round(float(_ll(yt[m], test[f"p_{k}"].to_numpy()[m]).mean()), 5),
                "brier": round(float(np.mean((yt[m] - test[f"p_{k}"].to_numpy()[m]) ** 2)), 5)}
            for k in names}}  # fmt: skip
    rep["test"] = by
    per = test.assign(**{f"ll_{k}": _ll(yt, test[f"p_{k}"].to_numpy()) for k in names})
    late = per[BUCKETS["last 5 min, within 5 points"](per)]
    agg = {f"ll_{k}": "sum" for k in names} | {"home_win": "size"}
    pg = per.groupby("game_id").agg(agg).rename(columns={"home_win": "n"})
    pl = late.groupby("game_id").agg(agg).rename(columns={"home_win": "n"})
    rep["gain_ci95"] = {
        "film_epv vs recalibrated, all": _bootstrap(pg, "recalibrated", "film_epv"),
        "film_epv vs recalibrated, last 5 min within 5": _bootstrap(pl, "recalibrated",
                                                                    "film_epv"),
        "recalibrated vs base, all": _bootstrap(pg, "base", "recalibrated"),
        "film_epv_lineup vs film_epv, all": _bootstrap(pg, "film_epv", "film_epv_lineup"),
        f"{live} vs base, all": _bootstrap(pg, "base", live),
    }  # fmt: skip
    rep["anticipation"] = anticipation(test)
    cal = pd.DataFrame({"p": test[f"p_{live}"], "y": yt, "period": test.period})
    cal["bin"] = np.clip((cal.p * 10).astype(int), 0, 9)
    rep["calibration"] = (cal.groupby("bin").agg(predicted=("p", "mean"), actual=("y", "mean"),
                                                 rows=("y", "size")).round(3)
                          .reset_index().to_dict("records"))  # fmt: skip
    rep["calibration_by_period"] = {
        str(p): (d.groupby("bin").agg(predicted=("p", "mean"), actual=("y", "mean"),
                                       rows=("y", "size")).round(3).reset_index()
                 .to_dict("records"))
        for p, d in cal.assign(period=np.minimum(cal.period, 5)).groupby("period")}  # fmt: skip
    model = {"base": _pack(base), "film": {k: _pack(m) for k, m in fits.items()},
             "live": live, "vbar": vbar, "impact_season": IMPACT_SEASON,
             "impact_fill": fill}  # fmt: skip
    model_path().write_text(json.dumps(model))
    keep = [
        "game_id",
        "frame",
        "p_base",
        "p_base_epv",
        *[f"p_{k}" for k in designs],
        "pregame",
        "lineup",
    ]
    rows[keep].to_parquet(out_dir() / "winprob_rows.parquet", index=False)
    (out_dir() / "winprob_report.json").write_text(json.dumps(rep, indent=2))
    return rep


def anticipation(test: pd.DataFrame) -> dict:
    """The short-horizon test of what EPV is for: during a possession, how well does win
    probability now predict where the score-and-clock one will be once the possession is
    over (its points on the scoreboard), with and without the EPV term? Mean squared error,
    by situation; ``error_cut`` is the share of the baseline's error EPV removes."""
    out = {}
    parts = []
    for _, d in test.sort_values(["game_id", "frame"]).groupby("game_id", sort=False):
        o, p = d.offense.to_numpy(), d.period.to_numpy()
        new = np.r_[True, (o[1:] != o[:-1]) | (p[1:] != p[:-1])]
        run = np.cumsum(new)
        first = np.flatnonzero(new)  # first row of each run
        nxt = np.append(first[1:], -1)[run - 1]  # first row of the next run
        ok = (o >= 0) & (nxt >= 0)
        tgt = d.p_base.to_numpy()[np.where(ok, nxt, 0)]
        parts.append(d[ok].assign(target=tgt[ok]))
    a = pd.concat(parts, ignore_index=True)
    for bname in ("all", "last 5 min of the 4th", "last 5 min, within 5 points",
                  "last 2 min, within 5 points"):  # fmt: skip
        m = BUCKETS[bname](a)
        d = a[m]
        mse = {k: float(np.mean((d[f"p_{k}"] - d.target) ** 2)) for k in ("base", "base_epv")}
        se = d.assign(b=(d.p_base - d.target) ** 2, e=(d.p_base_epv - d.target) ** 2)
        g = se.groupby("game_id")[["b", "e"]].sum().to_numpy()
        rng = np.random.default_rng(5)
        cuts = []
        for _ in range(2000):  # resample games
            s_ = g[rng.integers(0, len(g), len(g))].sum(axis=0)
            cuts.append(1 - s_[1] / s_[0])
        out[bname] = {"rows": int(m.sum()), **{k: round(v, 6) for k, v in mse.items()},
                      "error_cut": round(1 - mse["base_epv"] / mse["base"], 4),
                      "error_cut_ci95": [round(float(np.quantile(cuts, q)), 4)
                                         for q in (0.025, 0.975)]}  # fmt: skip
    return out


class Model:
    """The fitted models, for the engine: ``predict(...)`` -> (baseline, film) P(home wins)."""

    def __init__(self, path: Path | None = None) -> None:
        m = json.loads((path or model_path()).read_text())
        self.base = _unpack(m["base"])
        self.live = m["live"]
        self.film = _unpack(m["film"][self.live])
        self.vbar = float(m["vbar"])
        self.uses_lineup = self.live == "film_epv_lineup"

    def predict_many(self, margin, s_left, poss, pregame, epv, lineup) -> tuple:
        """(baseline, film) P(home wins) for arrays of states."""
        zb = logit(self.base, base_design(margin, s_left, poss, pregame))
        X = film_design(zb, poss, epv, self.vbar, s_left, lineup if self.uses_lineup else None)
        return _p(zb), _p(logit(self.film, X))

    def predict(self, margin, s_left, poss, pregame, epv, lineup=0.0) -> tuple[float, float]:
        b, f = self.predict_many([margin], [s_left], [poss], [pregame], [epv], [lineup])
        return float(b[0]), float(f[0])
