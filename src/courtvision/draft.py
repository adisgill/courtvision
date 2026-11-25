"""The NBA draft from college stats (spec section 22): ``courtvision draft``.

Every Division I player-season since 2007-08 (Barttorvik, ``ncaa.player_seasons``) is
linked to the player's NBA career, and a model learns what a college season says about
the NBA value that followed: WAR over a player's first ``HORIZON`` NBA seasons (0 for the
players who never got there), from the same per-possession impact and replacement level
as the market model. It is tested walk-forward on past drafts (each draft predicted from
classes whose outcomes were complete before it) against the order NBA teams actually
picked in, then used on this season's players for a big board, comparable past
prospects, a projected draft order (season simulations and the lottery) and a mock first
round matched to each team's needs.

Tables (schema ``draft``):

* ``outcomes``   every college player linked to an NBA career: WAR by horizon
* ``eval``       walk-forward test by draft: the model, the actual order and both together
* ``history``    drafted college players with the walk-forward prediction and the outcome
* ``prospects``  this season's big board (expected WAR, tier chances, strengths)
* ``prospect_seasons``  college seasons of the board's players
* ``comps``      each prospect's closest past prospects and how they turned out
* ``order``      projected order of the next draft by team (simulated), with pick owners
* ``order_probs`` chance of each team picking at each slot
* ``fit``        each team's fit with each top prospect (for the app's mock simulator)
* ``mock``       the mock first round
* ``lottery``    lottery odds by seed
* ``info``       as-of dates, thresholds, the model's coefficients and the test summary
"""

from __future__ import annotations

import csv
import json
import logging
import re
import unicodedata
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

HORIZON = 4  # NBA seasons in the outcome (a rookie-scale contract)
DRAFT_DAY = (6, 25)
MIN_PCT = 10.0  # player-seasons with at least this share of team minutes are modelled
POSS_PER_MIN = 1.75  # possessions per player-minute, both ends (about 70 per 40 minutes)
# WAR over the horizon: the model estimates P(WAR >= t) at each, and expected WAR from them
THRESHOLDS = (0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0, 13.0, 16.0, 20.0, 25.0, 30.0)
TIERS = {"nba": 1.0, "rotation": 4.0, "starter": 10.0, "star": 20.0}
L2 = 2.0  # ridge penalty on standardized coefficients
HIGH_MAJOR = {"ACC", "B10", "B12", "SEC", "BE", "P12"}
GUARD = {"Pure PG": 1.0, "Scoring PG": 1.0, "Combo G": 1.0, "Wing G": 0.75, "Wing F": 0.25,
         "Stretch 4": 0.0, "PF/C": 0.0, "C": 0.0}  # fmt: skip
# per-possession rates, shrunk toward the season mean by this many possessions (the
# previous season counts at half weight on top of the current one)
RATES = {"bpm": 300, "obpm": 300, "dbpm": 300, "usg": 150, "ts": 250, "ast_pct": 200,
         "tov_pct": 200, "orb_pct": 150, "drb_pct": 150, "stl_pct": 300, "blk_pct": 300,
         "ftr": 250, "fg3_per100": 150, "fg3_rate": 150, "rim_rate": 200, "dunk_rate": 200,
         "sos": 300}  # fmt: skip
PCTS = {"ft_pct": ("ftm", "fta", 40), "fg3_pct": ("fg3m", "fg3a", 60)}  # made, tried, prior n
FEATURES = ["age", "age2", "dob_known", "height", "rec", "rec_top", "rec_missing",
            "high_major", "seasons", "mpg", "min_pct", "bpm_gain", *RATES, *PCTS]  # fmt: skip
REC_BUCKETS = [-2, 0, 50, 80, 90, 95, 98, 99.5, 101]  # recruiting score groups (-1 = unrated)
COHORT = 4  # years after a season by which nearly all its players have left college
TEST_FIRST = 2016  # first draft tested: training needs seasons HORIZON + COHORT years older
# the five dimensions team needs are measured on (market.teams), and what speaks to each
DIMS = ("shooting", "defense", "ball_control", "backcourt", "frontcourt")
LOTTERY = (140, 140, 140, 125, 105, 90, 75, 60, 45, 30, 20, 15, 10, 5)  # per 1,000, seeds 1-14
EAST = {1610612737, 1610612738, 1610612739, 1610612741, 1610612748, 1610612749, 1610612751,
        1610612752, 1610612753, 1610612754, 1610612755, 1610612761, 1610612764, 1610612765,
        1610612766}  # fmt: skip
SIMS = 4000
GAME_SD = 13.0  # margin of a single game around the rating difference
FIT_WEIGHT = 0.1  # a prospect's value moves this share per unit of fit (fit runs -2.5..2.5)
TRADES_FILE = "draft_pick_trades.csv"


# ---------------------------------------------------------------- college -> NBA links


_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm_name(s: str) -> str:
    """'Nikola Jokić', 'D'Angelo Russell Jr.' -> 'nikola jokic', 'dangelo russell'."""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'`’-]", "", s)
    return " ".join(_SUFFIX.sub(" ", s).split())


def last_seasons(seasons: pd.DataFrame) -> pd.DataFrame:
    """One row per college player: his last season's year (= the draft after it), name,
    school and NBA pick (Barttorvik fills it on every season of a drafted player)."""
    s = seasons.sort_values(["pid", "season"])
    last = s.groupby("pid").tail(1)[["pid", "player", "team", "season"]].copy()
    last["last_year"] = last.season + 1
    last["pick"] = s.groupby("pid")["pick"].max().reindex(last.pid).to_numpy()
    return last.drop(columns="season")


def link(last: pd.DataFrame, nba: pd.DataFrame) -> pd.DataFrame:
    """College ``pid`` -> NBA ``person_id``. Drafted players by draft year and pick (the
    last name has to agree), everyone else by name among players whose first NBA season
    came within two years of leaving college (unique matches only). ``nba``: person_id,
    name, draft_year, draft_number, from_year."""
    nba = nba.assign(key=nba.name.map(norm_name))
    nba["last"] = nba.key.str.split().str[-1]
    lk = last.assign(key=last.player.map(norm_name))
    lk["last"] = lk.key.str.split().str[-1]
    out = []
    drafted = lk[lk.pick.notna()]
    for shift in (0, 1):  # drafted the summer after his last season, or a year later
        m = drafted.assign(dy=drafted.last_year + shift).merge(
            nba, left_on=["dy", "pick"], right_on=["draft_year", "draft_number"],
            suffixes=("", "_nba"))  # fmt: skip
        m = m[(m["last"] == m["last_nba"]) | (m.key == m.key_nba)]
        m = m[~m.pid.isin([o for d in out for o in d.pid])]
        out.append(m.assign(how="pick")[["pid", "person_id", "how"]])
    taken = pd.concat(out)
    rest = lk[~lk.pid.isin(taken.pid)]
    free = nba[~nba.person_id.isin(taken.person_id)]
    m = rest.merge(free, on="key", suffixes=("", "_nba"))
    m = m[(m.from_year >= m.last_year) & (m.from_year <= m.last_year + 2)]
    m = m[~m.pid.duplicated(keep=False) & ~m.person_id.duplicated(keep=False)]
    out.append(m.assign(how="name")[["pid", "person_id", "how"]])
    return pd.concat(out, ignore_index=True)


def outcomes(last: pd.DataFrame, links: pd.DataFrame, war: pd.DataFrame,
             complete_through: int) -> pd.DataFrame:  # fmt: skip
    """WAR over the first ``HORIZON`` NBA seasons after leaving college (seasons by start
    year: a player whose last college season ended in June ``Y`` starts in season ``Y``),
    0 without an NBA career. ``complete``: all ``HORIZON`` seasons are over."""
    o = last.merge(links, on="pid", how="left")
    w = o[["pid", "person_id", "last_year"]].dropna().astype({"person_id": "int64"})
    w = w.merge(war, on="person_id")
    w = w[(w.season >= w.last_year) & (w.season < w.last_year + HORIZON)]
    tot = w.groupby("pid").agg(war=("war", "sum"), nba_seasons=("season", "nunique"))
    o = o.merge(tot, left_on="pid", right_index=True, how="left")
    o["war"] = o.war.fillna(0.0)
    o["nba_seasons"] = o.nba_seasons.fillna(0).astype(int)
    o["complete"] = o.last_year + HORIZON - 1 <= complete_through
    o["seasons_seen"] = (complete_through - o.last_year + 1).clip(0, HORIZON)
    return o


# ---------------------------------------------------------------------- features


def features(seasons: pd.DataFrame) -> pd.DataFrame:
    """One row per player-season with the model's inputs. Rates are blended with the
    previous season (half weight) and shrunk toward the season's mean by possessions, so
    a few games early in a season say little and a returning player starts from last
    year."""
    df = seasons.copy()
    df["year"] = df.season + 1  # the draft after this season
    df["poss"] = df.mpg.fillna(0) * df.gp.fillna(0) * POSS_PER_MIN
    fga = (df.fg2a + df.fg3a).replace(0, np.nan)
    df["fg3_rate"] = df.fg3a / fga * 100
    df["rim_rate"] = df.rim_a / fga * 100
    df["dunk_rate"] = df.dunk_a / fga * 100
    df["sos"] = df.adjoe - df.ortg
    raw = [*RATES, *[c for m, a, _ in PCTS.values() for c in (m, a)], "poss"]
    prev = df[["pid", "season", *raw]].copy()
    prev["season"] += 1
    df = df.merge(prev, on=["pid", "season"], how="left", suffixes=("", "_prev"))
    df = df.sort_values(["pid", "season"]).reset_index(drop=True)
    df["seasons"] = df.groupby("pid").cumcount() + 1
    model_rows = df.min_pct >= MIN_PCT
    p, pp = df.poss, 0.5 * df.poss_prev.fillna(0)
    # where a player starts before his numbers count: what players of his recruiting
    # level and class (freshman or not) did, shifted by the season's level. A top recruit
    # a few games into his first season starts near what top recruits usually do.
    bucket = pd.cut(
        df.rec_rank.where(df.rec_rank > 0).fillna(-1), REC_BUCKETS, labels=False
    ).astype(str) + (df["class"] == "Fr").map({True: "f", False: ""})
    for c, k in RATES.items():
        lo = df[c].where(model_rows).groupby(df.season).transform(lambda v: v.quantile(0.01))
        hi = df[c].where(model_rows).groupby(df.season).transform(lambda v: v.quantile(0.99))
        df[f"{c}_raw"] = df[c]
        df[c] = df[c].clip(lo, hi)  # a game or two can produce any rate
        w = df.poss.where(model_rows & df[c].notna(), 0)
        wx = df[c].fillna(0) * w
        season_mu = wx.groupby(df.season).transform("sum") / w.groupby(df.season).transform("sum")
        pooled = wx.sum() / w.sum()
        season_mu = season_mu.fillna(pooled)  # a season without the stat
        group_mu = (wx.groupby(bucket).transform("sum") / w.groupby(bucket).transform("sum")
                    ).fillna(pooled)  # fmt: skip
        mu = group_mu + season_mu - pooled
        x, xp = df[c].fillna(mu), df[f"{c}_prev"]
        df[c] = (x * p + (xp.fillna(0) * pp).where(xp.notna(), 0) + mu * k) / (
            p + pp.where(xp.notna(), 0) + k)  # fmt: skip
    for c, (made, tried, n) in PCTS.items():
        mu = (df[made].where(model_rows).groupby(df.season).transform("sum")
              / df[tried].where(model_rows).groupby(df.season).transform("sum"))  # fmt: skip
        mu = mu.fillna(df[made].where(model_rows).sum() / df[tried].where(model_rows).sum())
        df[f"{c}_raw"] = df[c]
        df[c] = ((df[made] + 0.5 * df[f"{made}_prev"].fillna(0) + mu * n)
                 / (df[tried] + 0.5 * df[f"{tried}_prev"].fillna(0) + n))  # fmt: skip
    # last season's BPM, shrunk the same way; growth from it is the gain
    bp = df.bpm_prev
    mu_b = df.groupby("season").bpm.transform("mean")
    prev_b = (bp.fillna(0) * df.poss_prev.fillna(0) + mu_b * RATES["bpm"]) / (
        df.poss_prev.fillna(0) + RATES["bpm"])  # fmt: skip
    df["bpm_gain"] = np.where(bp.notna(), df.bpm_raw.fillna(mu_b) - prev_b, 0.0)
    df["bpm_gain"] = df.bpm_gain * df.poss / (df.poss + RATES["bpm"])
    bd = pd.to_datetime(df.birth_date)
    df["dob_known"] = (bd.dt.strftime("%m-%d") != "10-15").astype(float)
    draft_day = pd.to_datetime(df.year.astype(str) + f"-{DRAFT_DAY[0]:02d}-{DRAFT_DAY[1]:02d}")
    df["age"] = (draft_day - bd).dt.days / 365.25
    cls_age = {"Fr": 19.3, "So": 20.3, "Jr": 21.3, "Sr": 22.4}
    df["age"] = df.age.fillna(df["class"].map(cls_age)).fillna(21.5)
    df["age2"] = (df.age - 21) ** 2
    df["height"] = df.height_in.fillna(df.groupby("role").height_in.transform("median")).fillna(77)
    rec = df.rec_rank.where(df.rec_rank > 0)
    df["rec_missing"] = rec.isna().astype(float)
    df["rec"] = rec.fillna(rec.groupby(df.season).transform(lambda s: s.quantile(0.05)))
    df["rec_top"] = (df.rec - 95).clip(lower=0)
    df["high_major"] = df.conf.isin(HIGH_MAJOR).astype(float)
    df["mpg"] = df.mpg.fillna(0)
    df["model_row"] = model_rows
    return df


# --------------------------------------------------------------------------- model


def fit_logit(X: np.ndarray, y: np.ndarray, l2: float = L2, iters: int = 50) -> np.ndarray:
    """L2-penalized logistic regression by damped Newton steps (intercept unpenalized):
    each step is halved until the penalized log-likelihood improves."""
    n, k = X.shape
    Xb = np.column_stack([np.ones(n), X])
    w = np.zeros(k + 1)
    m = min(max(y.mean(), 1e-4), 1 - 1e-4)
    w[0] = np.log(m / (1 - m))
    R = np.eye(k + 1) * l2
    R[0, 0] = 0.0

    def objective(w):
        z = Xb @ w
        return float((y * z - np.logaddexp(0, z)).sum() - 0.5 * w @ R @ w)

    f = objective(w)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Xb @ w, -30, 30)))
        g = Xb.T @ (y - p) - R @ w
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb + R + np.eye(k + 1) * 1e-6
        step = np.linalg.solve(H, g)
        t = 1.0
        while t > 1e-4:
            f_new = objective(w + t * step)
            if f_new >= f - 1e-10:
                break
            t /= 2
        w, f_old, f = w + t * step, f, f_new
        if abs(f - f_old) < 1e-8 * (1 + abs(f)):
            break
    return w


class Model:
    """P(WAR >= t) at every threshold from standardized features; expected WAR is the
    area under that curve (probabilities made non-increasing first)."""

    def __init__(self, rows: pd.DataFrame, target: pd.Series):
        X = rows[FEATURES].to_numpy(float)
        sd = X.std(0)
        # a feature that doesn't vary in the training seasons (shot locations before
        # they were tracked) is left out rather than scaled up from rounding noise
        self.mu, self.sd = X.mean(0), np.where(sd > 1e-6, sd, np.inf)
        Z = (X - self.mu) / self.sd
        self.coef = {t: fit_logit(Z, (target.to_numpy() >= t).astype(float))
                     for t in THRESHOLDS}  # fmt: skip

    def probs(self, rows: pd.DataFrame) -> np.ndarray:
        Z = (rows[FEATURES].to_numpy(float) - self.mu) / self.sd
        Zb = np.column_stack([np.ones(len(Z)), Z])
        P = np.column_stack([1 / (1 + np.exp(-np.clip(Zb @ self.coef[t], -30, 30)))
                             for t in THRESHOLDS])  # fmt: skip
        return np.minimum.accumulate(P, axis=1)

    def expected(self, P: np.ndarray) -> np.ndarray:
        steps = np.diff((0.0, *THRESHOLDS))
        return P @ steps

    def contributions(self, rows: pd.DataFrame, t: float = TIERS["rotation"]) -> pd.DataFrame:
        """Each feature's push on the log-odds of reaching ``t``, against an average row."""
        Z = (rows[FEATURES].to_numpy(float) - self.mu) / self.sd
        return pd.DataFrame(Z * self.coef[t][1:], columns=FEATURES, index=rows.index)


def score(model: Model, rows: pd.DataFrame) -> pd.DataFrame:
    P = model.probs(rows)
    out = pd.DataFrame(index=rows.index)
    out["ev"] = model.expected(P)
    for name, t in TIERS.items():
        out[f"p_{name}"] = P[:, THRESHOLDS.index(t)]
    return out


def _spearman(a: pd.Series, b: pd.Series) -> float:
    if len(a) < 3:
        return float("nan")
    return float(a.rank().corr(b.rank()))


def _auc(score: np.ndarray, y: np.ndarray) -> float:
    pos, neg = score[y], score[~y]
    if not len(pos) or not len(neg):
        return float("nan")
    r = pd.Series(np.concatenate([pos, neg])).rank().to_numpy()
    return float((r[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def trainable(rows: pd.DataFrame, complete_through: int) -> pd.DataFrame:
    """Player-seasons usable for training when NBA seasons through ``complete_through``
    are over: the player's outcome is complete (he left college by ``complete_through -
    HORIZON + 1``), and the season is old enough (``COHORT`` more years back) that nearly
    everyone in it has left college too. Without the second rule, recent seasons would
    keep only the players who left early, and early leavers are the best ones."""
    last_ok = complete_through - HORIZON + 1
    return rows[(rows.last_year <= last_ok) & (rows.year <= last_ok - COHORT)]


def walk_forward(feat: pd.DataFrame, out: pd.DataFrame, last_draft: int,
                 scored_through: int) -> tuple:  # fmt: skip
    """Each draft ``Y`` from ``TEST_FIRST``: train on player-seasons usable before it
    (``trainable``), then rank that draft's college players. Drafts after
    ``scored_through`` (no full NBA season yet) get predictions but no score. Returns
    (eval by draft, history rows)."""
    lab = out.set_index("pid")
    rows_all = feat[feat.model_row].join(lab[["last_year", "war", "pick"]], on="pid",
                                         rsuffix="_final")  # fmt: skip
    evals, hist = [], []
    for Y in range(TEST_FIRST, last_draft + 1):
        train = trainable(rows_all, Y - 1)
        test = rows_all[(rows_all.year == Y)]
        if train.empty or test.empty:
            continue
        model = Model(train, train.war)
        sc = score(model, test)
        test = test.assign(**sc)
        test["model_rank"] = test.ev.rank(ascending=False, method="first")
        final = test[test.last_year == Y]  # players who left college for this draft
        drafted = final[final.pick_final.notna()].copy()
        drafted["pick"] = drafted.pick_final
        drafted["model_rank_drafted"] = drafted.ev.rank(ascending=False, method="first")
        drafted["blend"] = -(drafted.model_rank_drafted + drafted.pick) / 2
        o = lab.loc[drafted.pid]
        drafted["war"] = o.war.to_numpy()
        drafted["complete"] = o.complete.to_numpy()
        hist.append(drafted.assign(draft=Y))
        if scored_through < Y:
            continue
        made = (final.war >= TIERS["nba"]).to_numpy()
        top = drafted.nsmallest(10, "model_rank_drafted")
        top_picks = drafted.nsmallest(10, "pick")
        evals.append({
            "draft": Y, "complete": bool(drafted.complete.all()), "drafted": len(drafted),
            "train_rows": len(train),
            "spearman_model": _spearman(drafted.ev, drafted.war),
            "spearman_pick": _spearman(-drafted.pick, drafted.war),
            "spearman_blend": _spearman(drafted.blend, drafted.war),
            "top10_model_hits": int((top.war >= TIERS["rotation"]).sum()),
            "top10_pick_hits": int((top_picks.war >= TIERS["rotation"]).sum()),
            "auc_model": _auc(final.ev.to_numpy(), made),
            "auc_drafted": _auc(final.pick_final.notna().to_numpy(float)
                                + 1e-6 * final.ev.rank().to_numpy(), made),
            "players": len(final),
        })  # fmt: skip
    return pd.DataFrame(evals), pd.concat(hist, ignore_index=True)


# ----------------------------------------------------------------- board and comps


def strengths(rows: pd.DataFrame) -> pd.DataFrame:
    """Prospect strength on the five team-need dimensions, as z-scores among ``rows``."""

    def z(s: pd.Series) -> pd.Series:
        return (s - s.mean()) / (s.std() + 1e-9)

    g = rows.role.map(GUARD).fillna(0.5)
    s = pd.DataFrame(index=rows.index)
    s["shooting"] = (z(rows.ts) + z(rows.fg3_pct) + z(rows.ft_pct) + z(rows.fg3_per100)) / 4
    s["defense"] = (z(rows.dbpm) + z(rows.stl_pct) + z(rows.blk_pct)) / 3
    s["ball_control"] = (z(rows.ast_pct) - z(rows.tov_pct)) / 2
    s["backcourt"] = g * z(rows.obpm)
    s["frontcourt"] = (1 - g) * (z(rows.orb_pct) + z(rows.drb_pct) + z(rows.blk_pct)
                                 + z(rows.rim_rate)) / 4  # fmt: skip
    return s.clip(-2.5, 2.5).fillna(0.0)


COMP_FEATURES = ["age", "height", "bpm", "obpm", "dbpm", "usg", "ts", "ast_pct", "tov_pct",
                 "orb_pct", "drb_pct", "stl_pct", "blk_pct", "fg3_rate", "ft_pct",
                 "rec"]  # fmt: skip


def comps(board: pd.DataFrame, past: pd.DataFrame, k: int = 5) -> pd.DataFrame:
    """The ``k`` past prospects (final college seasons of players who were drafted or
    reached the NBA) closest to each board player, by standardized distance."""
    X = past[COMP_FEATURES].to_numpy(float)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    P = (X - mu) / sd
    B = (board[COMP_FEATURES].to_numpy(float) - mu) / sd
    rows = []
    for i, (_, b) in enumerate(board.iterrows()):
        d = np.sqrt(((P - B[i]) ** 2).mean(1))
        for rank, j in enumerate(np.argsort(d)[:k], 1):
            c = past.iloc[j]
            rows.append({"pid": int(b.pid), "rank": rank, "comp_pid": int(c.pid),
                         "comp_player": c.player, "comp_team": c.team, "comp_year": int(c.year),
                         "comp_person_id": c.person_id, "comp_pick": c.pick_final,
                         "comp_war": float(c.war), "distance": float(d[j])})  # fmt: skip
    return pd.DataFrame(rows)


# ------------------------------------------------------------- order and mock draft


def team_ratings(games: pd.DataFrame, prior: dict[int, float], hca: float,
                 prior_games: float = 12.0) -> dict[int, float]:  # fmt: skip
    """Margin ratings from this season's games (home court removed), pulled toward
    ``prior`` (last season's rating, already regressed) as if it were ``prior_games``
    games."""
    teams = sorted(set(games.home_team_id) | set(games.away_team_id) | set(prior))
    ix = {t: i for i, t in enumerate(teams)}
    rows, y = [], []
    for g in games.itertuples():
        r = np.zeros(len(teams))
        r[ix[g.home_team_id]], r[ix[g.away_team_id]] = 1, -1
        rows.append(r)
        y.append(g.home_score - g.away_score - hca)
    lam = np.sqrt(prior_games / 2)
    for t in teams:
        r = np.zeros(len(teams))
        r[ix[t]] = lam
        rows.append(r)
        y.append(lam * prior.get(t, 0.0))
    A, b = np.array(rows), np.array(y)
    sol = np.linalg.lstsq(A, b, rcond=None)[0]
    sol -= sol.mean()
    return {t: float(sol[ix[t]]) for t in teams}


def lottery_draw(seeds: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Draft order of the 14 lottery teams (``seeds``: worst first): four picks drawn by
    the odds, the rest in seed order."""
    odds = np.array(LOTTERY, float)
    left = list(range(14))
    won = []
    for _ in range(4):
        p = odds[left] / odds[left].sum()
        i = rng.choice(len(left), p=p)
        won.append(left.pop(i))
    return seeds[won + left]


def simulate(teams: list[int], ratings: dict[int, float], wins: dict[int, int],
             remaining: pd.DataFrame, hca: float, rating_sd: float, sims: int = SIMS,
             seed: int = 7) -> dict:  # fmt: skip
    """Season simulations: the rest of the schedule from team ratings (each simulation
    draws its own rating error), play-in by seed, then the lottery. Returns per team the
    expected wins, playoff chance and the chance of each first-round slot, plus the
    simulated slots (sims x teams) for pick protections."""
    rng = np.random.default_rng(seed)
    ix = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    base = np.array([ratings.get(t, 0.0) for t in teams])
    w0 = np.array([wins.get(t, 0) for t in teams], float)
    h = remaining.home_team_id.map(ix).to_numpy()
    a = remaining.away_team_id.map(ix).to_numpy()
    east = np.array([t in EAST for t in teams])
    slots = np.zeros((sims, n), int)
    total = np.zeros((sims, n))
    playoff = np.zeros((sims, n), bool)
    for s in range(sims):
        r = base + rng.normal(0, rating_sd, n)
        p_home = 1 - _norm_cdf(-(r[h] - r[a] + hca) / GAME_SD)
        home_won = rng.random(len(h)) < p_home
        w = w0 + np.bincount(h[home_won], minlength=n) + np.bincount(a[~home_won], minlength=n)
        w = w + rng.random(n) * 1e-3  # random tie-breaks
        total[s] = w
        made = np.zeros(n, bool)
        for conf in (east, ~east):
            idx = np.where(conf)[0]
            order = idx[np.argsort(-w[idx])]
            made[order[:6]] = True
            s7, s8, s9, s10 = order[6:10]
            w78 = _play_in(rng, r, s7, s8, hca)
            l78 = s8 if w78 == s7 else s7
            w910 = _play_in(rng, r, s9, s10, hca)
            made[[w78, _play_in(rng, r, l78, w910, hca)]] = True
        playoff[s] = made
        lott = np.where(~made)[0]
        lott = lott[np.argsort(w[lott])]
        rest = np.where(made)[0]
        rest = rest[np.argsort(w[rest])]
        order = np.concatenate([lottery_draw(lott, rng), rest])
        slots[s, order] = np.arange(1, n + 1)
    probs = np.stack([(slots == k).mean(0) for k in range(1, n + 1)], 1)  # team x slot
    return {"exp_wins": total.mean(0), "p_playoffs": playoff.mean(0), "probs": probs,
            "slots": slots}  # fmt: skip


def _play_in(rng: np.random.Generator, r: np.ndarray, x: int, y: int, hca: float) -> int:
    """Winner of a play-in game, the higher seed ``x`` at home."""
    return x if rng.random() < 1 - _norm_cdf(-(r[x] - r[y] + hca) / GAME_SD) else y


def _norm_cdf(x) -> np.ndarray:
    """Standard normal CDF (Abramowitz-Stegun 7.1.26 erf, error < 2e-7), vectorized."""
    z = np.asarray(x, float) / np.sqrt(2)
    t = 1 / (1 + 0.3275911 * np.abs(z))
    poly = t * (
        0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429)))
    )
    erf = np.sign(z) * (1 - poly * np.exp(-z * z))
    return 0.5 * (1 + erf)


def read_trades(path: Path, year: int) -> pd.DataFrame:
    """``data/draft_pick_trades.csv``: who owns first-round picks other than their own.
    Columns: year, round, original (team abbreviation), owner, protected_through (the
    original team keeps the pick when it lands at or above this slot; blank = none),
    kind (``convey`` or ``swap``: the owner takes the better of its own pick and the
    original's), note."""
    cols = ["year", "round", "original", "owner", "protected_through", "kind", "note"]
    if not path.exists():
        return pd.DataFrame(columns=cols)
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("year") or "").strip()]
    t = pd.DataFrame(rows, columns=cols)
    t = t[(t.year.astype(int) == year) & (t["round"].astype(int) == 1)]
    t["protected_through"] = pd.to_numeric(t.protected_through, errors="coerce")
    t["kind"] = t.kind.fillna("convey").replace("", "convey")
    return t


def owners(slots: np.ndarray, teams: list[int], abbrev: dict[int, str],
           trades: pd.DataFrame) -> tuple[np.ndarray, pd.DataFrame]:  # fmt: skip
    """Owner of each team's pick in every simulation (sims x teams of team ids), and per
    original team the chance its pick conveys."""
    by_abbr = {v: k for k, v in abbrev.items()}
    ix = {t: i for i, t in enumerate(teams)}
    own = np.tile(np.array(teams), (slots.shape[0], 1))
    conv = []
    for tr in trades.itertuples():
        o, w = by_abbr.get(tr.original.strip()), by_abbr.get(tr.owner.strip())
        if o is None or w is None:
            log.warning("pick trade with unknown team: %s -> %s", tr.original, tr.owner)
            continue
        i = ix[o]
        if tr.kind == "swap":
            j = ix[w]
            better = slots[:, i] < slots[:, j]  # the original's pick is the better one
            own[better, i], own[better, j] = w, o
            conv.append({"team_id": o, "owner_id": w, "kind": "swap",
                         "protected_through": None, "p_conveys": float(better.mean()),
                         "note": tr.note})  # fmt: skip
        else:
            keep = slots[:, i] <= tr.protected_through if pd.notna(tr.protected_through) else (
                np.zeros(len(slots), bool))  # fmt: skip
            own[~keep, i] = w
            conv.append({"team_id": o, "owner_id": w, "kind": "convey",
                         "protected_through": tr.protected_through,
                         "p_conveys": float((~keep).mean()), "note": tr.note})  # fmt: skip
    cols = ["team_id", "owner_id", "kind", "protected_through", "p_conveys", "note"]
    return own, pd.DataFrame(conv, columns=cols)


def need_weights(team_dims: pd.DataFrame) -> pd.DataFrame:
    """How much each team needs each dimension: points below average, in tens, 0-2."""
    w = pd.DataFrame(index=team_dims.index)
    for d in DIMS:
        w[d] = ((50 - team_dims[f"{d}_adj"]) / 10).clip(0, 2)
    return w.fillna(0)


def team_fit(needs: pd.DataFrame, strength: pd.DataFrame) -> pd.DataFrame:
    """Team x prospect fit: the prospect's strength on the dimensions the team needs,
    averaged by how much it needs each (0 for a team without needs)."""
    N, S = needs.to_numpy(), strength.to_numpy()
    fit = N @ S.T / np.maximum(N.sum(1, keepdims=True), 1.0)
    return pd.DataFrame(fit.clip(-2.5, 2.5), index=needs.index, columns=strength.index)


def mock(order: list[int], board: pd.DataFrame, fit: pd.DataFrame) -> pd.DataFrame:
    """The first round: at each pick, the available prospect with the most expected WAR
    after fit (``ev x (1 + FIT_WEIGHT x fit)``: up to 25% either way). ``fit``: team x pid."""
    left = board.set_index("pid")
    rows = []
    for k, team in enumerate(order, 1):
        f = fit.loc[team, left.index] if team in fit.index else pd.Series(0.0, left.index)
        val = left.ev * (1 + FIT_WEIGHT * f)
        pick = val.idxmax()
        bpa = left.ev.idxmax()
        rows.append({"pick": k, "team_id": team, "pid": int(pick), "best_available": int(bpa),
                     "fit": float(f[pick]), "ev": float(left.ev[pick])})  # fmt: skip
        left = left.drop(pick)
    return pd.DataFrame(rows)


# -------------------------------------------------------------------------- build


def _nba_war(con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, float, float]:
    p = con.execute("select points_per_win, replacement_impact from market.params").fetchall()
    ppw, repl = p[0] if p else (32.0, -2.0)
    war = con.execute(
        "select person_id, season, (impact - ?) * poss / 200 / ? as war "
        "from metrics.player_impact", [repl, ppw]).df()  # fmt: skip
    return war, float(ppw), float(repl)


def _team_dims(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    cols = ", ".join(f"{d}_adj" for d in DIMS)
    try:
        return con.execute(f"select team_id, {cols} from market.teams").df().set_index("team_id")
    except duckdb.Error:
        return pd.DataFrame(columns=[f"{d}_adj" for d in DIMS])


def _abbrevs(con: duckdb.DuckDBPyConnection, season: int) -> dict[int, str]:
    rows = con.execute(
        """select teamId, any_value(trim(teamAbbrev)) from kaggle.team_histories
           where seasonActiveTill >= ? and league = 'NBA' group by 1""",
        [season],
    ).fetchall()
    return {int(t): ("SAS" if a == "SAN" else a) for t, a in rows}


def run(con: duckdb.DuckDBPyConnection, data_dir: Path) -> dict:
    seasons = con.execute("select * from ncaa.player_seasons").df()
    current = int(seasons.season.max())  # the college season in progress (start year)
    nba_now = con.execute("select max(season) from stg.games where season_type = 'regular'"
                          ).fetchall()[0][0]  # fmt: skip
    draft_year = current + 1
    war, ppw, repl = _nba_war(con)
    complete_through = int(war.season[war.season < nba_now].max())
    nba = con.execute(
        """select person_id, first_name || ' ' || last_name as name, draft_year,
                  draft_number, from_year from stg.players""").df()  # fmt: skip
    last = last_seasons(seasons)
    links = link(last, nba)
    out = outcomes(last, links, war, complete_through)
    feat = features(seasons)

    ev_df, hist = walk_forward(feat, out, draft_year - 1, complete_through)
    lab = out.set_index("pid")
    rows_all = feat[feat.model_row].join(lab[["last_year", "war"]], on="pid")
    train_rows = trainable(rows_all, complete_through)
    model = Model(train_rows, train_rows.war)

    # the board: this season's players (early in the season, mostly last year's numbers
    # for returning players; freshmen lean on recruiting)
    now = feat[(feat.season == current) & (feat.gp >= 1) & (feat.age <= 25.5)].copy()
    now = now.join(score(model, now))
    now = now.sort_values("ev", ascending=False).head(150).reset_index(drop=True)
    now["rank"] = np.arange(1, len(now) + 1)
    now = now.join(strengths(now))
    contrib = model.contributions(now)
    top_reasons = contrib.apply(lambda r: list(r.abs().nlargest(4).index), axis=1)
    now["drivers"] = [  # JSON text: nested lists don't survive the browser's Parquet reader
        json.dumps([{"feature": f, "push": round(float(contrib.loc[i, f]), 4)} for f in fs])
        for i, fs in top_reasons.items()
    ]
    done_rows = rows_all[rows_all.last_year <= complete_through - HORIZON + 1]
    past = done_rows[done_rows.last_year == done_rows.year].merge(
        out[["pid", "person_id", "pick"]].rename(columns={"pick": "pick_final"}), on="pid"
    )
    past = past[past.pick_final.notna() | past.person_id.notna()].reset_index(drop=True)
    cmp = comps(now, past)

    # projected order: ratings from this season's games and last season's, simulated
    games = con.execute(
        """select home_team_id, away_team_id, home_score, away_score, season from stg.games
           where season_type = 'regular' and season in (?, ?) and home_score is not null""",
        [nba_now - 1, nba_now]).df()  # fmt: skip
    hca = float((games.home_score - games.away_score).mean())
    last_r = team_ratings(games[games.season == nba_now - 1], {}, hca, prior_games=0.0)
    prior = {t: 0.6 * r for t, r in last_r.items()}
    cur = games[games.season == nba_now]
    ratings = team_ratings(cur, prior, hca)
    wins = pd.concat(
        [
            cur.home_team_id.where(cur.home_score > cur.away_score),
            cur.away_team_id.where(cur.away_score > cur.home_score),
        ]
    ).value_counts()
    last_day = con.execute("select max(game_date) from stg.games where season = ? and "
                           "season_type = 'regular' and home_score is not null",
                           [nba_now]).fetchall()[0][0]  # fmt: skip
    sched = con.execute(
        f"""select homeTeamId as home_team_id, awayTeamId as away_team_id
            from kaggle.league_schedule_{str(nba_now)[2:]}_{str(nba_now + 1)[2:]}
            where gameLabel is distinct from 'Preseason' and homeTeamId > 0
              and awayTeamId > 0 and gameSubLabel is distinct from 'Championship'
              and gameDateTimeEst::date > ?""", [last_day]).df()  # fmt: skip
    teams = sorted(ratings)
    played = len(cur) * 2 / max(len(teams), 1)
    rating_sd = 3.5 * np.sqrt(max(82 - played, 0) / 82) + 0.5
    sim = simulate(teams, ratings, wins.to_dict(), sched, hca, rating_sd)
    abbrev = _abbrevs(con, nba_now)
    trades = read_trades(data_dir / TRADES_FILE, draft_year)
    own, conveys = owners(sim["slots"], teams, abbrev, trades)
    exp_pick = sim["probs"] @ np.arange(1, len(teams) + 1)
    order = pd.DataFrame({
        "team_id": teams, "rating": [ratings[t] for t in teams],
        "wins_now": [int(wins.get(t, 0)) for t in teams], "exp_wins": sim["exp_wins"],
        "p_playoffs": sim["p_playoffs"], "exp_pick": exp_pick,
        "p_1": sim["probs"][:, 0], "p_top4": sim["probs"][:, :4].sum(1),
        "p_lottery": 1 - sim["p_playoffs"],
    })  # fmt: skip
    order["seed"] = order.exp_wins.rank(method="first").astype(int)  # projected, worst = 1
    # owner of each slot in the projected order (the lottery going to form)
    proj = order.sort_values("seed").team_id.tolist()
    proj_slots = np.zeros((1, len(teams)), int)
    for k, t in enumerate(proj, 1):
        proj_slots[0, teams.index(t)] = k
    own_proj, _ = owners(proj_slots, teams, abbrev, trades)
    owner_of = dict(zip(teams, own_proj[0].tolist(), strict=True))
    order["owner_id"] = order.team_id.map(owner_of)
    order = order.merge(conveys.rename(columns={"owner_id": "trade_owner_id"}), on="team_id",
                        how="left")  # fmt: skip
    order["abbrev"] = order.team_id.map(abbrev)
    order["season"] = nba_now
    order = order.sort_values("seed").reset_index(drop=True)
    probs = pd.DataFrame([(t, k + 1, float(sim["probs"][i, k])) for i, t in enumerate(teams)
                          for k in range(len(teams)) if sim["probs"][i, k] > 0],
                         columns=["team_id", "slot", "prob"])  # fmt: skip

    needs = need_weights(_team_dims(con).reindex(teams))
    fit = team_fit(needs, now.set_index("pid")[list(DIMS)])
    m = mock([owner_of[t] for t in proj], now.head(80), fit)
    m["original_id"] = proj
    fit_long = fit.stack().rename("fit").reset_index().rename(
        columns={"level_0": "team_id", "level_1": "pid"})  # fmt: skip
    fit_long = fit_long[fit_long.pid.isin(now.head(80).pid)]

    hist = hist.merge(out[["pid", "person_id", "how"]], on="pid", how="left")
    keep_hist = ["draft", "pid", "player", "team", "conf", "class", "age", "height_in",
                 "pick", "ev", "p_nba", "p_rotation", "p_starter", "p_star",
                 "model_rank_drafted", "war", "complete", "person_id", "pts", "treb", "ast",
                 "bpm_raw", "ts_raw", "rec_rank"]  # fmt: skip
    disp = ["pid", "player", "team", "conf", "class", "role", "age", "dob_known", "height_in",
            "rec_rank", "gp", "mpg", "pts", "treb", "ast", "stl", "blk", "ts_raw", "fg3_pct_raw",
            "fg3a", "ft_pct_raw", "fta", "bpm_raw", "obpm_raw", "dbpm_raw", "usg_raw",
            "season", "as_of"]  # fmt: skip
    prospects = now[["rank", *disp, "ev", *[f"p_{t}" for t in TIERS], *DIMS, "drivers"]]
    pseasons = seasons[seasons.pid.isin(now.pid)].copy()
    pseasons = pseasons[["pid", "season", "team", "conf", "class", "gp", "mpg", "min_pct",
                         "pts", "treb", "ast", "stl", "blk", "ts", "efg", "fg3_pct", "fg3a",
                         "ft_pct", "fta", "usg", "ast_pct", "tov_pct", "orb_pct", "drb_pct",
                         "stl_pct", "blk_pct", "bpm", "obpm", "dbpm", "as_of"]]  # fmt: skip
    lot = pd.DataFrame({"seed": range(1, 15), "per_1000": LOTTERY})
    done = ev_df[ev_df.complete]
    first = out[out.complete & (out.pick <= 30) & (out.last_year >= 2008)]
    info = pd.DataFrame([{
        "draft_year": draft_year, "college_season": current, "nba_season": nba_now,
        "stats_as_of": seasons.as_of.max(), "horizon": HORIZON,
        "complete_through": complete_through, "points_per_win": ppw, "replacement": repl,
        "tiers": [f"{k}:{v}" for k, v in TIERS.items()], "fit_weight": FIT_WEIGHT,
        "linked_pick": int((links.how == "pick").sum()),
        "linked_name": int((links.how == "name").sum()),
        "train_rows": len(train_rows), "sims": SIMS, "hca": hca,
        "trades": len(trades),
        "test_drafts": f"{int(done.draft.min())}-{int(done.draft.max())}" if len(done) else "",
        "first_round_rotation": (float((first.war >= TIERS["rotation"]).mean())
                                 if len(first) else None),
        "spearman_model": float(done.spearman_model.mean()),
        "spearman_pick": float(done.spearman_pick.mean()),
        "spearman_blend": float(done.spearman_blend.mean()),
        "auc_model": float(done.auc_model.mean()), "auc_drafted": float(done.auc_drafted.mean()),
        "top10_model_hits": float(done.top10_model_hits.mean()),
        "top10_pick_hits": float(done.top10_pick_hits.mean()),
        "coef_rotation": [f"{f}:{c:.3f}" for f, c in
                          zip(FEATURES, model.coef[TIERS["rotation"]][1:], strict=True)],
    }])  # fmt: skip

    con.execute("create schema if not exists draft")
    tables = {
        "outcomes": out, "eval": ev_df, "history": hist[[c for c in keep_hist if c in hist]],
        "prospects": prospects, "prospect_seasons": pseasons, "comps": cmp, "order": order,
        "order_probs": probs, "fit": fit_long, "mock": m, "lottery": lot, "info": info,
    }  # fmt: skip
    for name, df in tables.items():
        con.register("_d", df)
        con.execute(f"create or replace table draft.{name} as select * from _d")
        con.unregister("_d")
    return {k: len(v) for k, v in tables.items()} | {
        "spearman model / pick / blend": (round(info.spearman_model[0], 3),
                                          round(info.spearman_pick[0], 3),
                                          round(info.spearman_blend[0], 3))}  # fmt: skip
