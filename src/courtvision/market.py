"""Player market: what each player is worth, what each team can spend, where each team is
short, which players would fix it, and free agency now and next summer (spec 11.6, 13).

Built from the ratings, per-possession impact (``metrics.player_impact``) and the
contract book (``salary.book``, ``salary.contract_summary``) by ``courtvision market build``:

* ``market.params``     points per win, replacement level, cap figures, the market curve
* ``market.players``    every player with a deal or a recent season: value, price, contract, risk
* ``market.teams``      payroll against cap / tax / aprons, tax bill, needs, next summer's space
* ``market.options``    for each team need, players who fill it now (trade or unsigned)
* ``market.fa_class``   next summer's free agents (UFA, RFA, options) with a projected price
* ``market.fa_options`` for each team need, next summer's free agents and whether they fit

**Fair salary = market comparables.** An increasing (isotonic) curve of salary as a share
of the cap against points added per season, fitted on last season's veteran contracts
(rookie-scale and two-way deals excluded), answers "what do players who produce like this
earn?". It is capped at the player's maximum by years of service (25% / 30% / 35%).

Money is in the target season's dollars. The salary-matching and tax rules are a
simplified reading of the 2023 CBA: a planning aid, not a cap sheet.
"""

from __future__ import annotations

import logging

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

DIMS = ["shooting", "defense", "ball_control", "backcourt", "frontcourt"]
NEED_BELOW = 47.0  # a team dimension rated below this is a need
STRONG = 58.0  # a player rated at least this in the dimension can address it
TOP_OPTIONS = 12
Z10 = 1.2816  # 10th / 90th percentile of a normal
CAP_GROWTH = 1.07  # next season's cap: recent growth (2025-26 -> 2026-27 was +6.7%)
# equal-probability points of a standard normal, for "chance worth less than paid"
_Q = np.array([-1.96, -1.645, -1.28, -1.04, -0.84, -0.67, -0.52, -0.39, -0.25, -0.13, 0.0,
               0.13, 0.25, 0.39, 0.52, 0.67, 0.84, 1.04, 1.28, 1.645, 1.96])  # fmt: skip

# 2023 CBA trade-matching thresholds (2023-24 dollars), scaled with the cap
MATCH_LOW, MATCH_HIGH, MATCH_PAD = 7_500_000, 29_000_000, 250_000
CAP_2023 = 136_021_000
TAX_STEP_2023 = 5_168_000
TAX_RATES = [1.5, 1.75, 2.5, 3.25]  # then +0.5 per further step (non-repeater)


# ---- league parameters ----------------------------------------------------------------------


def points_per_win(con, last: int) -> float:
    """Season point differential worth one win (from team-seasons 2000 on)."""
    df = con.execute(
        """select season, team_id, avg((b_pts > o_b_pts)::int) as win_pct,
                  avg(b_pts - o_b_pts) as margin
           from metrics.team_game_counts
           where season_type = 'regular' and season between 2000 and ?
           group by all""",
        [last],
    ).df()
    slope = np.polyfit(df.margin, df.win_pct, 1)[0]  # win% per point per game
    return float(1 / slope)


def aging_curve(con) -> np.ndarray:
    """Quadratic in age: expected change in impact (pts/100) from one season to the next.

    Delta method on season-only estimates of players with >= 1,500 possessions in both
    seasons, weighted by the harmonic mean of the two seasons' possessions.
    """
    df = con.execute(
        """select i.season, i.person_id, i.season_only as v, i.poss,
                  date_diff('day', try_cast(left(p.birth_date::varchar, 10) as date),
                            make_date(i.season + 1, 2, 1)) / 365.25 as age
           from metrics.player_impact i join stg.players p using (person_id)
           where i.poss >= 1500 and p.birth_date is not null"""
    ).df()
    nxt = df.merge(df, on="person_id", suffixes=("", "_n"))
    nxt = nxt[nxt.season_n == nxt.season + 1]
    w = 2 / (1 / nxt.poss + 1 / nxt.poss_n)
    age = nxt.age.clip(19, 39)
    return np.polyfit(age, nxt.v_n - nxt.v, 2, w=np.sqrt(w))


def age_delta(coef: np.ndarray, age: float | np.ndarray) -> np.ndarray:
    return np.polyval(coef, np.clip(age, 19, 39))


def cap_row(con, season: int) -> dict:
    row = con.execute(
        """select c.salary_cap, c.luxury_tax_line, c.first_apron, c.second_apron,
                  m.ten_year_veteran_minimum as vet_min, m.rookie_minimum as rookie_min
           from salary.cap c join salary.min_salaries m using (season_year)
           where c.season_year = ?""",
        [season],
    ).df()
    return row.iloc[0].to_dict()


def max_share(service_years: float | np.ndarray) -> np.ndarray:
    """Maximum salary as a share of the cap by years of service (2023 CBA)."""
    s = np.asarray(service_years, dtype=float)
    return np.where(s >= 10, 0.35, np.where(s >= 7, 0.30, 0.25))


def pava(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted increasing isotonic regression (pool adjacent violators)."""
    vals, wts, sizes = [], [], []
    for yi, wi in zip(y, w, strict=True):
        vals.append(float(yi))
        wts.append(float(wi))
        sizes.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            v2, w2, s2 = vals.pop(), wts.pop(), sizes.pop()
            v1, w1, s1 = vals.pop(), wts.pop(), sizes.pop()
            wt = w1 + w2
            vals.append((v1 * w1 + v2 * w2) / wt)
            wts.append(wt)
            sizes.append(s1 + s2)
    return np.repeat(vals, sizes)


def market_curve(x: np.ndarray, share: np.ndarray) -> tuple:
    """Market comparables by rank: the player whose value sits at the p-th percentile of
    veteran contracts is priced at the p-th percentile of their salaries (quantile
    matching; interpolating sorted values against sorted salary shares). The most
    valuable players map to max contracts, the least to minimums."""
    return np.sort(np.asarray(x, float)), np.sort(np.asarray(share, float))


REPL_GRID = (-1.0, -1.5, -2.0, -2.5, -3.0)  # replacement levels the market fit may choose


def market_replacement(x_impact: np.ndarray, poss: np.ndarray, share: np.ndarray) -> float:
    """The replacement level (pts/100) at which value above it best explains salaries
    (5-fold cross-validated isotonic fit). The market pays for minutes as well as for
    per-possession impact; a lower replacement level gives minutes more weight."""
    idx = np.random.default_rng(0).permutation(len(share))
    best, best_err = REPL_GRID[0], np.inf
    for repl in REPL_GRID:
        x = (x_impact - repl) * poss / 200
        err = 0.0
        for k in range(5):
            te, tr = idx[k::5], np.setdiff1d(idx, idx[k::5])
            curve = market_curve(x[tr], share[tr])
            err += float(((fair_share(x[te], curve) - share[te]) ** 2).sum())
        if err < best_err:
            best, best_err = repl, err
    return best


def fair_share(x, curve) -> np.ndarray:
    bx, fit = curve
    return np.interp(x, bx, fit)


def tax_bill(payroll: float, tax_line: float, step: float) -> float:
    """Non-repeater luxury tax on payroll above the line (2023 CBA brackets)."""
    over = max(0.0, payroll - tax_line)
    bill, k = 0.0, 0
    while over > 0:
        rate = TAX_RATES[k] if k < len(TAX_RATES) else TAX_RATES[-1] + 0.5 * (k - 3)
        part = min(over, step)
        bill += part * rate
        over -= part
        k += 1
    return bill


def budget_status(payroll: float, cap: dict) -> str:
    if payroll < cap["salary_cap"]:
        return "under the cap"
    if payroll < cap["luxury_tax_line"]:
        return "over the cap, under the tax"
    if payroll < cap["first_apron"]:
        return "over the tax"
    if payroll < cap["second_apron"]:
        return "over the first apron"
    return "over the second apron"


def outgoing_needed(incoming: float, payroll: float, cap: dict) -> float:
    """Least outgoing salary that lets a team take ``incoming`` salary in a trade."""
    ratio = cap["salary_cap"] / CAP_2023
    low, high = MATCH_LOW * ratio, MATCH_HIGH * ratio
    if payroll + incoming <= cap["salary_cap"]:
        return 0.0  # fits in cap space
    if payroll >= cap["first_apron"]:
        return incoming  # apron teams: no more than 100% back
    if payroll < cap["salary_cap"]:
        incoming = incoming - (cap["salary_cap"] - payroll)  # room absorbs part of it
    best = []
    x = (incoming - MATCH_PAD) / 2
    if x <= low:
        best.append(max(x, 0.0))
    x = incoming - low
    if low < x <= high:
        best.append(x)
    x = (incoming - MATCH_PAD) / 1.25
    if x > high:
        best.append(x)
    return min(best) if best else max(incoming - low, 0.0)


# ---- players ---------------------------------------------------------------------------------


def _players(con, season: int, base: int) -> pd.DataFrame:
    dims = ", ".join(
        f"b.{d}_rating as {d}_base, b.{d}_low_sample as {d}_base_low, "
        f"c.{d}_rating as {d}_now, c.{d}_low_sample as {d}_now_low"
        for d in DIMS
    )
    return con.execute(
        f"""
        with people as (
            select person_id from salary.contract_summary
            union
            select person_id from metrics.player_impact where season in ($s, $b)
        ),
        imp as (
            select person_id, season, impact, impact_off, impact_def, impact_se, poss,
                   impact_rating, box
            from metrics.player_impact where season in ($s, $b)
        ),
        g as (
            select person_id, season, count(*) filter (where minutes > 0) as games
            from metrics.player_game_counts
            where season in ($s, $b) and season_type = 'regular' group by all
        ),
        tg as (
            select season, count(*) / count(distinct team_id) as team_games
            from metrics.team_game_counts
            where season in ($s, $b) and season_type = 'regular' group by season
        ),
        cur as (
            select person_id, sum(minutes) as minutes_now
            from metrics.player_role
            where season = $s and season_type = 'regular' and team_id <> 0 group by person_id
        ),
        sal_base as (
            select person_id, sum(salary) filter (where not two_way) as salary_base,
                   bool_or(two_way) as two_way_base
            from salary.contracts where season = $b and person_id is not null group by 1
        )
        select p.person_id,
               trim(coalesce(pl.first_name, '') || ' ' || pl.last_name) as player,
               date_diff('day', try_cast(left(pl.birth_date::varchar, 10) as date),
                         make_date($s + 1, 2, 1)) / 365.25 as age,
               $s - case when pl.draft_year > 0 then pl.draft_year
                         else coalesce(pl.from_year, $s) end as service,
               pl.draft_year, pl.draft_round,
               pos.w_bc, pos.w_fc,
               ib.impact as impact_base, ib.poss as poss_base, ib.impact_se as se_base,
               ib.box as box_base,
               ic.impact as impact_now, ic.impact_off as off_now, ic.impact_def as def_now,
               ic.impact_se as se_now, ic.poss as poss_now, ic.impact_rating as rating_now,
               ic.box as box_now,
               gb.games as games_base, gc.games as games_now,
               tb.team_games as team_games_base, tc.team_games as team_games_now,
               sb.salary_base, sb.two_way_base, cur.minutes_now,
               k.kind as contract_kind, k.salary, k.team_id as book_team, k.status,
               k.through, k.years_left, k.total_left, k.next_option,
               r.usage, {dims}
        from people p
        left join stg.players pl using (person_id)
        left join metrics.positions pos using (person_id)
        left join imp ib on ib.person_id = p.person_id and ib.season = $b
        left join imp ic on ic.person_id = p.person_id and ic.season = $s
        left join g gb on gb.person_id = p.person_id and gb.season = $b
        left join g gc on gc.person_id = p.person_id and gc.season = $s
        left join tg tb on tb.season = $b
        left join tg tc on tc.season = $s
        left join sal_base sb using (person_id)
        left join cur using (person_id)
        left join salary.contract_summary k using (person_id)
        left join metrics.player_role r
               on r.person_id = p.person_id and r.season = $b and r.team_id = 0
              and r.season_type = 'regular'
        left join metrics.player_season b
               on b.person_id = p.person_id and b.season = $b and b.team_id = 0
              and b.season_type = 'regular'
        left join metrics.player_season c
               on c.person_id = p.person_id and c.season = $s and c.team_id = 0
              and c.season_type = 'regular'
        """,
        {"s": season, "b": base},
    ).df()


def price_players(con, season: int) -> tuple[pd.DataFrame, dict]:
    base = season - 1
    cap, cap_base = cap_row(con, season), cap_row(con, base)
    ppw = points_per_win(con, base)
    coef = aging_curve(con)
    df = _players(con, season, base)

    # the market curve: last season's veteran contracts against last season's output
    rookie_scale = (df.draft_round == 1) & (df.draft_year >= base - 3)
    train = df[(df.salary_base > 0) & df.poss_base.ge(500) & ~rookie_scale
               & ~df.two_way_base.fillna(False).astype(bool)]  # fmt: skip
    t_share = (train.salary_base / cap_base["salary_cap"]).to_numpy()
    repl = market_replacement(train.impact_base.to_numpy(), train.poss_base.to_numpy(), t_share)
    curve = market_curve(((train.impact_base - repl) * train.poss_base / 200).to_numpy(),
                         t_share)  # fmt: skip

    # current estimate: this season's impact when he has played, else last season's
    df["impact"] = df.impact_now.fillna(df.impact_base)
    df["impact_se"] = df.se_now.fillna(df.se_base)
    # box-score view over both seasons, possession-weighted (an early season is noisy)
    pb, pn = df.poss_base.fillna(0), df.poss_now.fillna(0)
    df["box"] = (df.box_base.fillna(0) * pb + df.box_now.fillna(0) * pn) / (pb + pn).replace(
        0, np.nan
    )
    pace = df.poss_now / df.team_games_now * 82
    df["proj_poss"] = np.where(
        df.poss_base.notna() & pace.notna(), 0.7 * df.poss_base + 0.3 * pace,
        df.poss_base.fillna(pace),
    )  # fmt: skip
    # impact is per 100 possessions at each end; possessions count both ends
    df["points_added"] = df.impact * df.proj_poss / 200
    df["value"] = (df.impact - repl) * df.proj_poss / 200  # points above replacement
    df["war"] = df.value / ppw
    sd_pts = df.impact_se * df.proj_poss / 200
    df["war_p10"] = df.war - Z10 * sd_pts / ppw
    df["war_p90"] = df.war + Z10 * sd_pts / ppw

    df["max_share"] = max_share(df.service.fillna(0))
    lo, hi = cap["vet_min"], (df.max_share * cap["salary_cap"]).to_numpy()

    def fair(x):
        return np.clip(fair_share(x, curve) * cap["salary_cap"], lo, hi)

    df["fair_salary"] = fair(df.value.to_numpy())
    # the same price from the box score alone: a second opinion when they disagree
    df["fair_box"] = fair(((df.box - repl) * df.proj_poss / 200).to_numpy())
    df["fair_p10"] = fair((df.value - Z10 * sd_pts).to_numpy())
    df["fair_p90"] = fair((df.value + Z10 * sd_pts).to_numpy())
    df["surplus"] = df.fair_salary - df.salary
    # chance he's worth less than he's paid, across his impact uncertainty. A max player
    # can't be worth more than his max, so he's judged against the max, not a salary that
    # grew past today's max with raises.
    grid = df.value.to_numpy()[:, None] + _Q[None, :] * sd_pts.to_numpy()[:, None]
    fair_grid = np.clip(fair_share(grid, curve) * cap["salary_cap"], lo, hi[:, None])
    paid = np.minimum(df.salary.to_numpy(), hi * 0.995)
    below = (fair_grid < paid[:, None]).mean(axis=1)
    df["overpay_prob"] = np.where(df.salary.notna() & df.value.notna(), below, np.nan)

    games = df[["games_base", "games_now"]].fillna(0).sum(axis=1)
    team_games = df.team_games_base.where(df.games_base.notna(), 0).fillna(0) + df[
        "team_games_now"
    ].where(df.games_now.notna(), 0).fillna(0)
    df["availability"] = (games / team_games.replace(0, np.nan)).clip(upper=1)
    df["aging_next"] = age_delta(coef, df.age.fillna(27))

    def flags(r) -> list[str]:
        out = []
        if pd.notna(r.age) and r.aging_next <= -0.75:
            out.append(f"age {r.age:.0f}: expect {r.aging_next:+.1f} pts/100 next season")
        if pd.notna(r.availability) and r.availability < 0.7:
            out.append(f"played {r.availability:.0%} of his teams' games (two seasons)")
        if pd.notna(r.impact_se) and r.impact_se >= 2.5:
            out.append(f"uncertain estimate (±{r.impact_se:.1f} pts/100)")
        if pd.notna(r.proj_poss) and r.proj_poss < 2000:
            out.append("small role: few possessions to judge")
        if pd.notna(r.box) and pd.notna(r.impact) and r.impact - r.box >= 2.5:
            out.append(f"value rests on plus-minus (box score says {r.box:+.1f})")
        if (pd.notna(r.salary) and pd.notna(r.years_left) and r.years_left >= 3
                and r.salary >= 0.2 * cap["salary_cap"]):  # fmt: skip
            out.append(f"long, big deal: {r.years_left:.0f} seasons through "
                       f"{int(r.through)}-{(int(r.through) + 1) % 100:02d}")  # fmt: skip
        if r.contract_kind == "two-way":
            out.append("two-way contract")
        return out

    df["risk_flags"] = df.apply(flags, axis=1)
    serious = df.risk_flags.map(lambda f: sum(not x.startswith("two-way") for x in f))
    df["risk"] = np.select(
        [(df.overpay_prob >= 0.75) | (serious >= 2), (df.overpay_prob < 0.35) & (serious == 0)],
        ["high", "low"], "medium",
    )  # fmt: skip
    df["team_id"] = df.book_team
    df["free_agent"] = df.status.fillna("").str.startswith("free agent")
    df["salary_known"] = df.salary.notna()
    df["season"] = season
    for d in DIMS:
        # last full season's rating when it qualified, else this season's
        base_ok = ~df[f"{d}_base_low"].fillna(True).astype(bool)
        df[f"{d}_rating"] = df[f"{d}_base"].where(base_ok, df[f"{d}_now"])
    params = {
        "season": season, "base_season": base, "points_per_win": ppw,
        "replacement_impact": repl, "salary_cap": cap["salary_cap"],
        "luxury_tax_line": cap["luxury_tax_line"], "first_apron": cap["first_apron"],
        "second_apron": cap["second_apron"], "vet_min": cap["vet_min"],
        "max_salary": 0.35 * cap["salary_cap"],
        "tax_step": TAX_STEP_2023 * cap["salary_cap"] / CAP_2023,
        "next_cap": cap["salary_cap"] * CAP_GROWTH,
        "aging_a2": coef[0], "aging_a1": coef[1], "aging_a0": coef[2],
        "curve_x": list(map(float, curve[0])), "curve_share": list(map(float, curve[1])),
        "curve_n": len(train),
    }  # fmt: skip
    return df, params


# ---- teams and options -----------------------------------------------------------------------


def team_budgets(con, players: pd.DataFrame, params: dict) -> pd.DataFrame:
    season = params["season"]
    pay = con.execute(
        """select team_id,
                  sum(salary) filter (where kind in ('contract', 'dead money') and season = $s)
                      as payroll,
                  sum(salary) filter (where kind = 'dead money' and season = $s) as dead_money,
                  count(*) filter (where kind = 'contract' and season = $s) as contracts,
                  count(*) filter (where kind = 'two-way' and season = $s) as two_way,
                  count(*) filter (where kind = 'unknown' and season = $s) as unknown,
                  -- next season: guaranteed, options, and July deals of unknown length
                  sum(salary) filter (where season = $s + 1 and kind = 'contract'
                                      and option is null) as next_guaranteed,
                  sum(salary) filter (where season = $s + 1 and option = 'player')
                      as next_player_options,
                  sum(salary) filter (where season = $s + 1 and option = 'team')
                      as next_team_options
           from salary.book where team_id is not null group by team_id""",
        {"s": season},
    ).df()
    carry = con.execute(
        """select team_id, sum(salary) as next_unknown_length, count(*) as n_unknown_length
           from salary.book
           where season = ? and kind = 'contract' and not length_known group by team_id""",
        [season],
    ).df()
    pay = pay.merge(carry, on="team_id", how="left").fillna(
        {"dead_money": 0, "next_guaranteed": 0, "next_player_options": 0,
         "next_team_options": 0, "next_unknown_length": 0, "n_unknown_length": 0}
    )  # fmt: skip
    cap = {k: params[k] for k in ("salary_cap", "luxury_tax_line", "first_apron",
                                  "second_apron")}  # fmt: skip
    pay["status"] = pay.payroll.map(lambda p: budget_status(p, cap))
    for k in cap:
        pay[f"room_{k}"] = cap[k] - pay.payroll
    pay["tax_bill"] = pay.payroll.map(
        lambda p: tax_bill(p, params["luxury_tax_line"], params["tax_step"])
    )
    # next summer: committed salary (options picked up, July deals of unknown length
    # assumed to run on) against the projected cap; best case: options declined
    pay["next_committed"] = (pay.next_guaranteed + pay.next_player_options
                             + pay.next_team_options + pay.next_unknown_length)  # fmt: skip
    pay["next_space"] = params["next_cap"] - pay.next_committed
    pay["next_space_best"] = params["next_cap"] - pay.next_guaranteed - pay.next_unknown_length
    ratings = con.execute(
        """select team_id, measure, value_adj, rating_adj, rating
           from metrics.team_season_adj
           where season = ? and tier = 'A' and measure in
                 ('shooting', 'defense', 'ball_control', 'backcourt', 'frontcourt')""",
        [season],
    ).df()
    wide = ratings.pivot_table(index="team_id", columns="measure",
                               values="rating_adj").add_suffix("_adj")  # fmt: skip
    pay = pay.merge(wide, left_on="team_id", right_index=True, how="left")

    def needs(r) -> list[str]:
        vals = {d: r.get(f"{d}_adj") for d in DIMS}
        low = sorted((v, d) for d, v in vals.items() if pd.notna(v) and v < NEED_BELOW)
        if not low:  # every team has a weakest spot worth knowing
            low = sorted((v, d) for d, v in vals.items() if pd.notna(v))[:1]
        return [d for _, d in low[:3]]

    pay["needs"] = pay.apply(needs, axis=1)
    pay["season"] = season
    return pay


def _gain(cand, roster: pd.DataFrame, share: float, impact_col: str = "impact"):
    """(replaced player row, team net per 100) for a candidate in his position group."""
    same = roster[roster.group == cand.group]
    if same.empty:
        return None, 0.0
    r = same.loc[same[impact_col].idxmin()]
    return r, (getattr(cand, impact_col) - r[impact_col]) * share


def options(players: pd.DataFrame, teams: pd.DataFrame, params: dict, team_poss: float):
    """For each team need, the players who fill it now, what they cost and what they add."""
    ppw = params["points_per_win"]
    cap = {k: params[k] for k in ("salary_cap", "luxury_tax_line", "first_apron",
                                  "second_apron")}  # fmt: skip
    p = players[players.impact.notna() & players.proj_poss.notna()].copy()
    p["group"] = np.where(p.w_bc.fillna(0) >= 0.5, "backcourt", "frontcourt")
    p["share"] = (p.proj_poss / 2 / team_poss).clip(upper=0.8)
    rows = []
    for t in teams.itertuples():
        roster = p[p.team_id == t.team_id].sort_values("minutes_now", ascending=False).head(9)
        for need in t.needs:
            # trade targets need a known salary; unsigned players sign for the minimum
            pool = p[
                (p.team_id != t.team_id)
                & (p[f"{need}_rating"] >= STRONG)
                & ((p.salary_known & (p.contract_kind == "contract")) | p.free_agent)
            ]
            for c in pool.itertuples():
                r, gain = _gain(c, roster, c.share)
                if r is None or gain <= 0:
                    continue
                cost = params["vet_min"] if c.free_agent else c.salary
                out = 0.0 if c.free_agent else outgoing_needed(cost, t.payroll, cap)
                after = t.payroll + cost - out
                rows.append({
                    "season": params["season"], "team_id": t.team_id, "need": need,
                    "person_id": c.person_id, "player": c.player, "from_team_id": c.team_id,
                    "free_agent": bool(c.free_agent), "need_rating": getattr(c, f"{need}_rating"),
                    "impact": c.impact, "replaces_id": r.person_id, "replaces": r.player,
                    "replaces_impact": r.impact, "net_gain_per100": gain,
                    "wins_added": gain * team_poss / 100 / ppw,
                    "wins_added_p10": (c.impact - Z10 * c.impact_se - r.impact) * c.share
                                      * team_poss / 100 / ppw,
                    "salary": cost, "fair_salary": c.fair_salary,
                    "contract_status": "free agent now" if c.free_agent else c.status,
                    "through": c.through, "years_left": c.years_left,
                    "outgoing_salary_needed": out, "payroll_after": after,
                    "status_after": budget_status(after, cap),
                    "tax_change": tax_bill(after, params["luxury_tax_line"], params["tax_step"])
                                  - t.tax_bill,
                    "risk": c.risk, "risk_flags": c.risk_flags,
                    "overpay_prob": c.overpay_prob,
                })  # fmt: skip
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["cost_per_win"] = (df.salary + df.tax_change.clip(lower=0)) / df.wins_added
    df["rank"] = df.groupby(["team_id", "need"]).cost_per_win.rank(method="first")
    return df[df["rank"] <= TOP_OPTIONS].reset_index(drop=True)


# ---- free agency next summer -----------------------------------------------------------------

FA_STATUSES = ("UFA", "RFA", "player option", "team option")


def fa_class(players: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Next summer's free agents with next season's projected impact and price."""
    nxt = params["season"] + 1
    curve = (np.array(params["curve_x"]), np.array(params["curve_share"]))
    fa = players[players.status.fillna("").str.startswith(FA_STATUSES)
                 & players.status.fillna("").str.endswith(str(nxt))
                 & players.impact.notna()].copy()  # fmt: skip
    fa["impact_next"] = fa.impact + fa.aging_next
    fa["points_next"] = (fa.impact_next - params["replacement_impact"]) * fa.proj_poss / 200
    hi = max_share(fa.service.fillna(0) + 1) * params["next_cap"]
    vet_min_next = params["vet_min"] * CAP_GROWTH
    fa["ask"] = np.clip(fair_share(fa.points_next, curve) * params["next_cap"], vet_min_next, hi)
    sd = fa.impact_se * fa.proj_poss / 200
    fa["ask_p10"] = np.clip(fair_share(fa.points_next - Z10 * sd, curve) * params["next_cap"],
                            vet_min_next, hi)  # fmt: skip
    fa["ask_p90"] = np.clip(fair_share(fa.points_next + Z10 * sd, curve) * params["next_cap"],
                            vet_min_next, hi)  # fmt: skip
    fa["fa_type"] = fa.status.str.replace(f" {nxt}", "", regex=False)
    keep = ["person_id", "player", "age", "team_id", "fa_type", "status", "salary",
            "impact", "impact_next", "impact_se", "proj_poss", "ask", "ask_p10", "ask_p90",
            "risk", "risk_flags", "w_bc", *[f"{d}_rating" for d in DIMS]]  # fmt: skip
    return fa[keep].sort_values("ask", ascending=False).reset_index(drop=True)


def fa_options(fa: pd.DataFrame, players: pd.DataFrame, teams: pd.DataFrame, params: dict,
               team_poss: float) -> pd.DataFrame:  # fmt: skip
    """For each team need, next summer's free agents who fill it and whether the team's
    projected space covers his projected price."""
    ppw = params["points_per_win"]
    roster_pool = players[players.impact.notna()].copy()
    roster_pool["group"] = np.where(roster_pool.w_bc.fillna(0) >= 0.5, "backcourt", "frontcourt")
    roster_pool["impact_next"] = roster_pool.impact + roster_pool.aging_next
    f = fa.copy()
    f["group"] = np.where(f.w_bc.fillna(0) >= 0.5, "backcourt", "frontcourt")
    f["share"] = (f.proj_poss / 2 / team_poss).clip(upper=0.8)
    rows = []
    for t in teams.itertuples():
        # who is still under contract next season
        staying = roster_pool[(roster_pool.team_id == t.team_id)
                              & ~roster_pool.person_id.isin(fa.person_id)]  # fmt: skip
        roster = staying.sort_values("minutes_now", ascending=False).head(9)
        for need in t.needs:
            for c in f[(f.team_id != t.team_id) & (f[f"{need}_rating"] >= STRONG)].itertuples():
                r, gain = _gain(c, roster, c.share, "impact_next")
                if r is None or gain <= 0:
                    continue
                rows.append({
                    "team_id": t.team_id, "need": need, "person_id": c.person_id,
                    "player": c.player, "fa_type": c.fa_type, "from_team_id": c.team_id,
                    "age": c.age, "impact_next": c.impact_next, "ask": c.ask,
                    "ask_p10": c.ask_p10, "ask_p90": c.ask_p90, "replaces": r.player,
                    "wins_added": gain * team_poss / 100 / ppw,
                    "fits_space": bool(c.ask <= t.next_space),
                    "fits_space_best": bool(c.ask <= t.next_space_best),
                    "risk": c.risk,
                })  # fmt: skip
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["cost_per_win"] = df.ask / df.wins_added
    df["rank"] = df.groupby(["team_id", "need"]).cost_per_win.rank(method="first")
    return df[df["rank"] <= TOP_OPTIONS].reset_index(drop=True)


def build(con: duckdb.DuckDBPyConnection, season: int | None = None) -> dict:
    season = season or con.execute("select max(season) from salary.contracts").fetchall()[0][0]
    players, params = price_players(con, season)
    teams = team_budgets(con, players, params)
    team_poss = con.execute(
        "select avg(poss) from metrics.team_season_counts where season = ? "
        "and season_type = 'regular'",
        [season - 1],
    ).fetchall()[0][0]
    params["team_possessions"] = team_poss
    opts = options(players, teams, params, team_poss)
    fa = fa_class(players, params)
    fa_opts = fa_options(fa, players, teams, params, team_poss)
    con.execute("create schema if not exists market")
    keep = [c for c in players.columns if not c.endswith(("_base_low", "_now_low"))]
    for name, df in (("players", players[keep]), ("teams", teams), ("options", opts),
                     ("fa_class", fa), ("fa_options", fa_opts),
                     ("params", pd.DataFrame([params]))):  # fmt: skip
        con.register("df_tmp", df)
        con.execute(f"create or replace table market.{name} as select * from df_tmp")
        con.unregister("df_tmp")
    log.info("market %s: %d players, %d teams, %d options, %d upcoming free agents",
             season, len(players), len(teams), len(opts), len(fa))  # fmt: skip
    return params
