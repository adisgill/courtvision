"""Player market: what each player is worth, what each team can spend, where each team is
short, and which players would fix it, with the risk of each move (spec v1.1, section 9).

Everything is built from the ratings, per-possession impact (``metrics.player_impact``)
and the salary tables (``salary.*``) by ``courtvision market build``:

* ``market.params``   points per win, replacement level, $ per win, thresholds
* ``market.players``  every player with a contract or a recent season: value, price, risk
* ``market.teams``    payroll against cap / tax / aprons, tax bill, needs
* ``market.options``  for each team need, the players who fill it and what they cost

Money is in the target season's dollars. The salary-matching and tax rules are a
simplified reading of the 2023 CBA (documented in the spec); they are a planning aid,
not a cap sheet.
"""

from __future__ import annotations

import logging
import math

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

DIMS = ["shooting", "defense", "ball_control", "backcourt", "frontcourt"]
NEED_BELOW = 47.0  # a team dimension rated below this is a need
STRONG = 58.0  # a player rated at least this in the dimension can address it
TOP_OPTIONS = 12
Z10 = 1.2816  # 10th / 90th percentile of a normal

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
    df = con.execute(
        f"""
        with people as (
            select person_id from salary.contracts
            where season in (?, ?) and person_id is not null
            union
            select person_id from metrics.player_impact where season in (?, ?)
        ),
        imp as (
            select person_id, season, impact, impact_off, impact_def, impact_se, poss,
                   impact_rating
            from metrics.player_impact where season in (?, ?)
        ),
        g as (
            select person_id, season, count(*) filter (where minutes > 0) as games
            from metrics.player_game_counts
            where season in (?, ?) and season_type = 'regular' group by all
        ),
        tg as (
            select season, count(*) / count(distinct team_id) as team_games
            from metrics.team_game_counts
            where season in (?, ?) and season_type = 'regular' group by season
        ),
        cur_team as (
            select person_id, arg_max(team_id, minutes) as team_now, sum(minutes) as minutes_now
            from metrics.player_role
            where season = ? and season_type = 'regular' and team_id <> 0 group by person_id
        )
        select p.person_id,
               trim(coalesce(pl.first_name, '') || ' ' || pl.last_name) as player,
               date_diff('day', try_cast(left(pl.birth_date::varchar, 10) as date),
                         make_date(? + 1, 2, 1)) / 365.25 as age,
               pos.w_bc, pos.w_fc,
               ib.impact as impact_base, ib.poss as poss_base, ib.impact_se as se_base,
               ic.impact as impact_now, ic.impact_off as off_now, ic.impact_def as def_now,
               ic.impact_se as se_now, ic.poss as poss_now, ic.impact_rating as rating_now,
               gb.games as games_base, gc.games as games_now,
               tb.team_games as team_games_base, tc.team_games as team_games_now,
               kb.salary as salary_base, ct.team_now, ct.minutes_now,
               r.usage, {dims}
        from people p
        left join stg.players pl using (person_id)
        left join metrics.positions pos using (person_id)
        left join imp ib on ib.person_id = p.person_id and ib.season = ?
        left join imp ic on ic.person_id = p.person_id and ic.season = ?
        left join g gb on gb.person_id = p.person_id and gb.season = ?
        left join g gc on gc.person_id = p.person_id and gc.season = ?
        left join tg tb on tb.season = ?
        left join tg tc on tc.season = ?
        left join (select person_id, sum(salary) as salary from salary.contracts
                   where season = ? and person_id is not null group by person_id) kb
               using (person_id)
        left join cur_team ct using (person_id)
        left join metrics.player_role r
               on r.person_id = p.person_id and r.season = ? and r.team_id = 0
              and r.season_type = 'regular'
        left join metrics.player_season b
               on b.person_id = p.person_id and b.season = ? and b.team_id = 0
              and b.season_type = 'regular'
        left join metrics.player_season c
               on c.person_id = p.person_id and c.season = ? and c.team_id = 0
              and c.season_type = 'regular'
        """,
        [season, base, season, base, season, base, season, base, season, base, season,
         season, base, season, base, season, base, season, base, base, base, season],
    ).df()  # fmt: skip
    # A waived player can carry two rows (dead money on the old team, a new contract):
    # his price is what his current team pays him, else his largest contract.
    k = con.execute(
        """select person_id, team_id as contract_team, salary, team_option, player_option,
                  two_way
           from salary.contracts where season = ? and person_id is not null""",
        [season],
    ).df()
    k = k.merge(df[["person_id", "team_now"]], on="person_id", how="left")
    k["pick"] = (k.contract_team == k.team_now).fillna(False).astype(int)
    k = k.sort_values(["pick", "salary"], ascending=False).drop_duplicates("person_id")
    return df.merge(k.drop(columns=["pick", "team_now"]), on="person_id", how="left")


def price_players(con, season: int) -> tuple[pd.DataFrame, dict]:
    base = season - 1
    cap, cap_base = cap_row(con, season), cap_row(con, base)
    ppw = points_per_win(con, base)
    coef = aging_curve(con)
    df = _players(con, season, base)

    # replacement level: possession-weighted impact of minimum-salary players last season
    mins = df[(df.salary_base <= 1.05 * cap_base["vet_min"]) & (df.poss_base >= 500)]
    repl = float(np.average(mins.impact_base, weights=mins.poss_base)) if len(mins) else -2.0

    # current estimate: this season's prior-informed impact when he has played
    df["impact"] = df.impact_now.fillna(df.impact_base)
    df["impact_se"] = df.se_now.fillna(df.se_base)
    pace = df.poss_now / df.team_games_now * 82
    df["proj_poss"] = np.where(
        df.poss_base.notna() & pace.notna(), 0.7 * df.poss_base + 0.3 * pace,
        df.poss_base.fillna(pace),
    )  # fmt: skip
    # impact is per 100 possessions at each end; possessions count both ends
    df["war"] = (df.impact - repl) * df.proj_poss / 200 / ppw

    # $ per win above replacement, from last season's salaries above the minimum
    last = df[(df.salary_base > 0) & df.poss_base.notna()].copy()
    last_war = (last.impact_base - repl) * last.poss_base / 200 / ppw
    dpw_base = float((last.salary_base - cap_base["vet_min"]).clip(lower=0).sum()
                     / last_war.clip(lower=0).sum())  # fmt: skip
    dpw = dpw_base * cap["salary_cap"] / cap_base["salary_cap"]
    max_salary = 0.35 * cap["salary_cap"]

    df["fair_salary"] = (cap["vet_min"] + df.war.clip(lower=0) * dpw).clip(upper=max_salary)
    df["surplus"] = df.fair_salary - df.salary
    sd_war = df.impact_se * df.proj_poss / 200 / ppw
    df["war_p10"] = df.war - Z10 * sd_war
    df["war_p90"] = df.war + Z10 * sd_war
    z = (df.salary - df.fair_salary) / (sd_war * dpw).replace(0, np.nan)
    # P(fair salary < salary): the chance he's worth less than he's paid
    df["overpay_prob"] = z.map(lambda v: 0.5 * (1 + math.erf(v / math.sqrt(2)))
                               if pd.notna(v) else np.nan)  # fmt: skip
    df["overpay_prob"] = df.overpay_prob.where(df.salary.notna())
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
        if pd.notna(r.player_option) and bool(r.player_option):
            out.append("player option: can leave after this season")
        if pd.notna(r.two_way) and bool(r.two_way):
            out.append("two-way contract")
        return out

    df["risk_flags"] = df.apply(flags, axis=1)
    serious = df.risk_flags.map(lambda f: sum(not x.startswith("player option") for x in f))
    df["risk"] = np.select(
        [(df.overpay_prob >= 0.75) | (serious >= 2), (df.overpay_prob < 0.35) & (serious == 0)],
        ["high", "low"], "medium",
    )  # fmt: skip
    df["team_id"] = df.team_now.fillna(df.contract_team)
    df["free_agent"] = df.salary.isna() & df.games_now.isna() & (df.poss_base >= 800)
    df["season"] = season
    for d in DIMS:
        # last full season's rating when it qualified, else this season's
        base_ok = ~df[f"{d}_base_low"].fillna(True).astype(bool)
        df[f"{d}_rating"] = df[f"{d}_base"].where(base_ok, df[f"{d}_now"])
    params = {
        "season": season, "base_season": base, "points_per_win": ppw,
        "replacement_impact": repl, "dollars_per_win": dpw, "salary_cap": cap["salary_cap"],
        "luxury_tax_line": cap["luxury_tax_line"], "first_apron": cap["first_apron"],
        "second_apron": cap["second_apron"], "vet_min": cap["vet_min"],
        "max_salary": max_salary,
        "tax_step": TAX_STEP_2023 * cap["salary_cap"] / CAP_2023,
        "aging_a2": coef[0], "aging_a1": coef[1], "aging_a0": coef[2],
    }  # fmt: skip
    return df, params


# ---- teams and options -----------------------------------------------------------------------


def team_budgets(con, players: pd.DataFrame, params: dict) -> pd.DataFrame:
    season = params["season"]
    pay = con.execute(
        """select team_id, sum(salary) as payroll, count(*) as contracts
           from salary.contracts where season = ? and team_id is not null group by team_id""",
        [season],
    ).df()
    cap = {k: params[k] for k in ("salary_cap", "luxury_tax_line", "first_apron",
                                  "second_apron")}  # fmt: skip
    pay["status"] = pay.payroll.map(lambda p: budget_status(p, cap))
    for k in cap:
        pay[f"room_{k}"] = cap[k] - pay.payroll
    pay["tax_bill"] = pay.payroll.map(
        lambda p: tax_bill(p, params["luxury_tax_line"], params["tax_step"])
    )
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


def options(players: pd.DataFrame, teams: pd.DataFrame, params: dict, team_poss: float):
    """For each team need, the players who fill it, what they cost and what they add."""
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
            pool = p[(p.team_id != t.team_id) & (p[f"{need}_rating"] >= STRONG)
                     & (p.salary.notna() | p.free_agent)]  # fmt: skip
            for c in pool.itertuples():
                same = roster[roster.group == c.group]
                if same.empty:
                    continue
                r = same.loc[same.impact.idxmin()]
                gain = (c.impact - r.impact) * c.share  # team net rating per 100
                if gain <= 0:
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
    con.execute("create schema if not exists market")
    keep = [c for c in players.columns if not c.endswith(("_base_low", "_now_low"))]
    for name, df in (("players", players[keep]), ("teams", teams), ("options", opts),
                     ("params", pd.DataFrame([params]))):  # fmt: skip
        con.register("df_tmp", df)
        con.execute(f"create or replace table market.{name} as select * from df_tmp")
        con.unregister("df_tmp")
    log.info("market %s: %d players, %d teams, %d options", season, len(players), len(teams),
             len(opts))  # fmt: skip
    return params
