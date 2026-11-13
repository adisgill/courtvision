"""Lineups: five-man units, two-man pairs, starters, closers (spec section 19).

Built by ``courtvision lineups`` from the rebuilt stints (``metrics.stints``: every
stretch with the same ten players, games whose lineups check out) into schema
``lineups``:

* ``team_stints``  every team-stint: the five, points and possessions both ways, the
                   five it faced, and what the players' per-possession impacts expect
                   (own five's impact minus the opponents' five, this season's and last's)
* ``units``        per team, season and five-man lineup: minutes, possessions, offense,
                   defense and net per 100 against league; expected from its players;
                   ``synergy`` (net minus expected) and the projected net (expected plus
                   synergy shrunk by its measured reliability); games started; clutch
* ``pairs``        per team, season and two players: possessions and net together
* ``tests``        does a lineup's net beyond its players repeat (split-half), and what
                   predicts a lineup's later games best: its own early net, its players'
                   impacts, or the projection?

A lineup's raw plus-minus over a few hundred possessions is mostly noise; what its players
do individually travels. The projection uses the two in the proportion the test finds.
"""

from __future__ import annotations

import logging

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

MIN_HALF_POSS = 50  # possessions per half for a lineup to enter the tests
K_MAX = 20000.0

TEAM_STINTS_SQL = """
create or replace table lineups.team_stints as
with g as (
    select game_id, season, home_team_id from stg.games where season_type = 'regular'
), ok as (
    select game_id from metrics.game_pbp_qa where pbp_ok and lineups_ok
), nc as (
    select newcomer_off + newcomer_def as v from metrics.impact_lambda where mode = 'headline'
), five as (
    select s.game_id, s.stint_id, s.team_id, any_value(g.season) as season,
           any_value(s.period) as period, any_value(s.start_s) as start_s,
           any_value(s.end_s) as end_s, any_value(s.team_id = g.home_team_id) as home,
           list(s.person_id order by s.person_id) as p,
           sum(coalesce(i.impact, (select v from nc))) as imp,
           sum(coalesce(ip.impact, (select v from nc))) as imp_prev
    from metrics.stints s
    join g using (game_id)
    join ok using (game_id)
    left join metrics.player_impact i on i.person_id = s.person_id and i.season = g.season
    left join metrics.player_impact ip on ip.person_id = s.person_id
                                      and ip.season = g.season - 1
    group by s.game_id, s.stint_id, s.team_id
    having count(*) = 5
), clutch as (
    select p.game_id, e.stint_id, p.team_id, count(*) as n, sum(p.pts) as pts
    from coach.possessions p
    join metrics.pbp_events e on e.game_id = p.game_id and e.seq = p.first_seq
    where p.clutch
    group by all
)
select a.season, a.game_id, a.stint_id, a.team_id, b.team_id as opp_id, a.period, a.home,
       (a.end_s - a.start_s) / 60.0 as minutes, a.p, array_to_string(a.p, '-') as lineup,
       b.p as opp_p, a.stint_id = min(a.stint_id) over (partition by a.game_id) as start,
       coalesce(t.poss, 0) as off_poss, coalesce(t.pts_fg + t.ftm, 0) as off_pts,
       coalesce(o.poss, 0) as def_poss, coalesce(o.pts_fg + o.ftm, 0) as def_pts,
       coalesce(c.n, 0) as clutch_off_poss, coalesce(c.pts, 0) as clutch_off_pts,
       coalesce(co.n, 0) as clutch_def_poss, coalesce(co.pts, 0) as clutch_def_pts,
       a.imp - b.imp as exp_net, a.imp_prev - b.imp_prev as exp_net_prev
from five a
join five b on b.game_id = a.game_id and b.stint_id = a.stint_id and b.team_id <> a.team_id
left join metrics.pbp_stint_team t on t.game_id = a.game_id and t.stint_id = a.stint_id
                                  and t.team_id = a.team_id
left join metrics.pbp_stint_team o on o.game_id = a.game_id and o.stint_id = a.stint_id
                                  and o.team_id = b.team_id
left join clutch c on c.game_id = a.game_id and c.stint_id = a.stint_id
                  and c.team_id = a.team_id
left join clutch co on co.game_id = a.game_id and co.stint_id = a.stint_id
                   and co.team_id = b.team_id;
"""

PAIRS_SQL = """
create or replace table lineups.pairs as
with x as (
    select season, team_id, p, unnest(p) as a, off_poss, off_pts, def_poss, def_pts, minutes
    from lineups.team_stints
), y as (
    select season, team_id, a, unnest(p) as b, off_poss, off_pts, def_poss, def_pts, minutes
    from x
)
select season, team_id, a as person_a, b as person_b, sum(minutes) as minutes,
       sum(off_poss) as off_poss, sum(off_pts) as off_pts, sum(def_poss) as def_poss,
       sum(def_pts) as def_pts
from y where a <= b group by all;
"""


def _net(df: pd.DataFrame, lg: pd.Series) -> pd.DataFrame:
    """Offense, defense (points allowed) and net per 100 against the season's league."""
    lg100 = df.season.map(lg)
    df["off100"] = 100 * df.off_pts / df.off_poss.where(df.off_poss > 0) - lg100
    df["def100"] = 100 * df.def_pts / df.def_poss.where(df.def_poss > 0) - lg100
    df["net100"] = df.off100 - df.def100
    return df


def _agg(st: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Sum a set of team-stints to ``keys``, with the possession-weighted expectation."""
    s = st.assign(poss=(st.off_poss + st.def_poss) / 2)
    s["w_exp"] = s.exp_net * s.poss
    s["w_exp_prev"] = s.exp_net_prev * s.poss
    g = s.groupby(keys).agg(
        games=("game_id", "nunique"), minutes=("minutes", "sum"), poss=("poss", "sum"),
        off_poss=("off_poss", "sum"), off_pts=("off_pts", "sum"), def_poss=("def_poss", "sum"),
        def_pts=("def_pts", "sum"), w_exp=("w_exp", "sum"), w_exp_prev=("w_exp_prev", "sum"),
    ).reset_index()  # fmt: skip
    g["exp_net100"] = g.w_exp / g.poss.where(g.poss > 0)
    g["exp_prev_net100"] = g.w_exp_prev / g.poss.where(g.poss > 0)
    return g.drop(columns=["w_exp", "w_exp_prev"])


def split_half(st: pd.DataFrame, lg: pd.Series) -> dict:
    """Synergy (net minus what the players' impacts expect) in odd vs even games of a
    season, for lineups with enough possessions in both: if lineups had chemistry beyond
    their players, it would repeat. Returns r, mean possessions per half and k."""
    order = st[["season", "team_id", "game_id"]].drop_duplicates().sort_values("game_id")
    order["half"] = order.groupby(["season", "team_id"]).cumcount() % 2
    s = st.merge(order, on=["season", "team_id", "game_id"])
    s = s[s.season > s.season.min()]  # last season's impacts must exist
    h = _net(_agg(s, ["season", "team_id", "lineup", "half"]), lg)
    # same-season impacts are fitted on these stints (they soak up part of a lineup's
    # result, pulling r down); last season's are independent but miss this season's
    # player changes (pulling r up). The truth lies between the two.
    h["syn"] = h.net100 - h.exp_net100
    h["syn_prev"] = h.net100 - h.exp_prev_net100
    w = h.pivot_table(index=["season", "team_id", "lineup"], columns="half",
                      values=["syn", "syn_prev", "net100", "poss"])  # fmt: skip
    w = w[(w["poss"][0] >= MIN_HALF_POSS) & (w["poss"][1] >= MIN_HALF_POSS)]

    def r(col):
        return float(np.corrcoef(w[col][0], w[col][1])[0, 1])

    r_syn, r_prev = r("syn"), r("syn_prev")
    n_half = float(w["poss"].mean().mean())
    # shrinkage from the upper bound: generous to the lineup's own data
    k = n_half * (1 - r_prev) / r_prev if r_prev > 0.005 else K_MAX
    return {"test": "split-half", "lineups": len(w), "n_half_poss": n_half,
            "synergy_r": r_syn, "synergy_prev_r": r_prev, "net_r": r("net100"),
            "k_poss": float(min(k, K_MAX))}  # fmt: skip


def forward_test(st: pd.DataFrame, lg: pd.Series, k: float) -> pd.DataFrame:
    """Each season's second-half lineup net predicted from its first half: the lineup's
    own first-half net, its players' impacts from last season (nothing from this season),
    and the projection (players' expectation + first-half synergy shrunk by ``k``).
    Correlation and possession-weighted error against what happened."""
    order = st[["season", "game_id"]].drop_duplicates().sort_values("game_id")
    order["late"] = order.groupby("season").cumcount() >= order.groupby(
        "season").game_id.transform("size") / 2  # fmt: skip
    s = st[st.season > st.season.min()].merge(order, on=["season", "game_id"])
    h = _net(_agg(s, ["season", "team_id", "lineup", "late"]), lg)
    w = h.pivot_table(index=["season", "team_id", "lineup"], columns="late",
                      values=["net100", "poss", "exp_prev_net100"])  # fmt: skip
    w = w[(w["poss"][False] >= MIN_HALF_POSS) & (w["poss"][True] >= MIN_HALF_POSS)]
    y, wt = w["net100"][True], w["poss"][True]
    early, n = w["net100"][False], w["poss"][False]
    players = w["exp_prev_net100"][False]
    proj = players + (early - players) * n / (n + k)
    rows = []
    for name, pred in (("league average (0)", pd.Series(0.0, index=y.index)),
                       ("lineup's own first-half net", early),
                       ("its players' impacts (last season)", players),
                       ("projection: players + shrunk synergy", proj)):  # fmt: skip
        rows.append({"test": "second half from first", "predictor": name, "lineups": len(w),
                     "r": float(np.corrcoef(pred, y)[0, 1]) if pred.std() > 0 else np.nan,
                     "mae": float(np.average((pred - y).abs(), weights=wt))})  # fmt: skip
    return pd.DataFrame(rows)


PAIR_HALVES_SQL = """
with o as (
    select season, team_id, game_id,
           row_number() over (partition by season, team_id order by game_id) % 2 as half
    from (select distinct season, team_id, game_id from lineups.team_stints)
), x as (
    select t.season, t.team_id, o.half, t.p, unnest(t.p) as a, t.off_poss, t.off_pts,
           t.def_poss, t.def_pts
    from lineups.team_stints t join o using (season, team_id, game_id)
), y as (
    select season, team_id, half, a, unnest(p) as b, off_poss, off_pts, def_poss, def_pts
    from x
), team as (
    select t.season, t.team_id, o.half,
           100 * (sum(off_pts) / sum(off_poss) - sum(def_pts) / sum(def_poss)) as team_net
    from lineups.team_stints t join o using (season, team_id, game_id) group by all
)
select y.season, y.team_id, y.half, a, b, (sum(off_poss) + sum(def_poss)) / 2 as poss,
       100 * (sum(off_pts) / sum(off_poss) - sum(def_pts) / sum(def_poss))
         - any_value(team.team_net) as rel
from y join team using (season, team_id, half)
where a < b group by all
"""


def pair_test(con) -> dict:
    """Two players' net together, relative to their team's net, in odd vs even games:
    does it repeat? ``k``: the possessions at which a pair's own number counts half."""
    h = con.execute(PAIR_HALVES_SQL).df()
    w = h.pivot_table(index=["season", "team_id", "a", "b"], columns="half",
                      values=["rel", "poss"])  # fmt: skip
    w = w[(w["poss"][0] >= 300) & (w["poss"][1] >= 300)].dropna()
    r = float(np.corrcoef(w["rel"][0], w["rel"][1])[0, 1])
    n_half = float(w["poss"].mean().mean())
    k = n_half * (1 - r) / r if r > 0.005 else K_MAX
    return {"test": "pairs split-half", "lineups": len(w), "n_half_poss": n_half,
            "net_r": r, "k_poss": float(min(k, K_MAX))}  # fmt: skip


def best_fives(cands: pd.DataFrame, n: int = 3, min_g: int = 1, min_b: int = 1,
               max_b: int = 2, pool: int = 13) -> list[tuple[list[int], float]]:  # fmt: skip
    """The ``n`` fives with the highest summed impact from ``cands`` (person_id, impact,
    grp in G / W / B), with at least ``min_g`` guards and ``min_b`` bigs and at most
    ``max_b`` bigs, searched over the ``pool`` best players."""
    from itertools import combinations

    c = cands.sort_values("impact", ascending=False).head(pool)
    rows = list(zip(c.person_id, c.impact, c.grp, strict=True))
    out = []
    for combo in combinations(rows, 5):
        grps = [g for _, _, g in combo]
        if grps.count("G") < min_g or not min_b <= grps.count("B") <= max_b:
            continue
        out.append(([int(p) for p, _, _ in combo], float(sum(i for _, i, _ in combo))))
    return sorted(out, key=lambda x: -x[1])[:n]


def build(con: duckdb.DuckDBPyConnection) -> dict:
    con.execute("create schema if not exists lineups")
    log.info("lineups.team_stints")
    con.execute(TEAM_STINTS_SQL)
    log.info("lineups.pairs")
    con.execute(PAIRS_SQL)
    st = con.execute("""select season, game_id, team_id, lineup, minutes, start, off_poss,
                               off_pts, def_poss, def_pts, exp_net, exp_net_prev,
                               clutch_off_poss, clutch_off_pts, clutch_def_poss, clutch_def_pts
                        from lineups.team_stints""").df()  # fmt: skip
    lg = st.groupby("season").apply(lambda d: 100 * d.off_pts.sum() / d.off_poss.sum(),
                                    include_groups=False)  # fmt: skip
    log.info("lineups: tests")
    sh = split_half(st, lg)
    k = sh["k_poss"]
    pt = pair_test(con)
    tests = pd.concat([pd.DataFrame([sh]), forward_test(st, lg, k), pd.DataFrame([pt])],
                      ignore_index=True)  # fmt: skip

    u = _net(_agg(st, ["season", "team_id", "lineup"]), lg)
    extra = st.groupby(["season", "team_id", "lineup"]).agg(
        starts=("start", "sum"), clutch_off_poss=("clutch_off_poss", "sum"),
        clutch_off_pts=("clutch_off_pts", "sum"), clutch_def_poss=("clutch_def_poss", "sum"),
        clutch_def_pts=("clutch_def_pts", "sum"),
    ).reset_index()  # fmt: skip
    u = u.merge(extra, on=["season", "team_id", "lineup"])
    u["synergy"] = u.net100 - u.exp_net100
    u["proj_net100"] = u.exp_net100 + u.synergy.fillna(0) * u.poss / (u.poss + k)
    u["clutch_poss"] = (u.clutch_off_poss + u.clutch_def_poss) / 2
    u["clutch_net100"] = 100 * (u.clutch_off_pts - u.clutch_def_pts) / u.clutch_poss.where(
        u.clutch_poss > 0)  # fmt: skip
    u["team_poss_share"] = u.poss / u.groupby(["season", "team_id"]).poss.transform("sum")
    u["players"] = u.lineup.str.split("-").map(lambda v: [int(x) for x in v])

    pr = con.execute("select * from lineups.pairs").df()
    pr = _net(pr, lg)
    pr["poss"] = (pr.off_poss + pr.def_poss) / 2
    team = u.groupby(["season", "team_id"])[["off_pts", "off_poss", "def_pts", "def_poss"]].sum()
    team_net = 100 * (team.off_pts / team.off_poss - team.def_pts / team.def_poss)
    tn = team_net.reindex(pd.MultiIndex.from_frame(pr[["season", "team_id"]])).to_numpy()
    raw = 100 * (pr.off_pts / pr.off_poss.where(pr.off_poss > 0)
                 - pr.def_pts / pr.def_poss.where(pr.def_poss > 0))  # fmt: skip
    pr["rel"] = raw - tn  # net together, relative to the team's
    pr["rel_shrunk"] = pr.rel * pr.poss / (pr.poss + pt["k_poss"])
    lgdf = lg.rename("lg100").reset_index()
    for name, df in (("units", u), ("pairs", pr), ("tests", tests), ("league", lgdf)):
        con.register("tmp_df", df)
        con.execute(f"create or replace table lineups.{name} as select * from tmp_df")
        con.unregister("tmp_df")
    return {"team_stints": len(st), "units": len(u), "pairs": len(pr), "k_poss": k,
            "synergy_r": sh["synergy_r"]}  # fmt: skip
