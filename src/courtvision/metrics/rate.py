"""Levels, values, Presence, and the 0-100 scale (docs/metrics_spec.md sections 2-5).

1. Season counts are sums of game counts (team-season; player-season per team and, with
   ``team_id = 0``, across teams for traded players).
2. Every :data:`~courtvision.metrics.measures.MEASURES` expression is applied to every
   level's counts and written to one long table ``metrics.values_long``.
3. Presence (on/off) has its own counts and values.
4. ``metrics.ratings_long`` = values shrunk at player-season level (section 4), then
   ``clip(50 + 10 z, 0, 100)`` against the level's regular-season reference population.
5. Wide output tables pick, per measure, the best tier available for the unit.
"""

from __future__ import annotations

from courtvision.metrics import measures as M
from courtvision.metrics.sql import (
    ACTOR_SHOTS,
    BOX_COUNTS,
    BOX_WEIGHTED,
    OPP_BOX,
    PBP_COUNTS,
    PBP_WEIGHTED,
    SEASON_AGG_TYPES_SQL,
)

TEAM_SUM_COLS = (
    PBP_COUNTS + ["poss"]
    + [f"{s}_{c}" for s in ("bc", "fc") for c in PBP_WEIGHTED]
    + [f"o_{c}" for c in PBP_COUNTS + ["poss"]]
    + [f"a_{c}" for c in ACTOR_SHOTS]
    + ["minutes"] + [f"b_{c}" for c in BOX_COUNTS] + ["b_poss", "b_team_tov", "b_tov_players"]
    + [f"o_b_{c}" for c in OPP_BOX]
    + [f"{s}_b_{c}" for s in ("bc", "fc") for c in BOX_WEIGHTED]
)  # fmt: skip
PLAYER_SUM_COLS = (
    ["minutes", "sec_on"]
    + [f"a_{c}" for c in ACTOR_SHOTS]
    + PBP_COUNTS + ["poss"]
    + [f"o_{c}" for c in PBP_COUNTS + ["poss"]]
    + [f"b_{c}" for c in BOX_COUNTS] + ["b_pm"]
    + [f"{s}_{c}" for s in ("bc", "fc") for c in ("fga_r", "pts_r", "xp_r")]
    + [f"{s}_b_{c}" for s in ("bc", "fc") for c in BOX_WEIGHTED]
)  # fmt: skip
# Columns of the on-court / team pair used by Presence.
PRESENCE_COLS = (
    PBP_COUNTS + ["poss"]
    + [f"{s}_{c}" for s in ("bc", "fc") for c in PBP_WEIGHTED]
    + [f"o_{c}" for c in PBP_COUNTS + ["poss"]]
)  # fmt: skip

LEVELS = {
    # level: (counts table, key columns, kind)
    "team_game": ("metrics.team_game_counts", ["game_id", "team_id"], "team"),
    "team_season": ("metrics.team_season_counts", ["team_id"], "team"),
    "player_game": ("metrics.player_game_counts", ["game_id", "team_id", "person_id"], "player"),
    "player_season": ("metrics.player_season_counts", ["team_id", "person_id"], "player"),
}


def _sums(cols) -> str:
    return ",\n    ".join(f"sum({c}) as {c}" for c in cols)


def season_counts_sql() -> list[tuple[str, str]]:
    return [
        (
            "team_season_counts",
            f"""
create or replace table metrics.team_season_counts as
select season, season_type, team_id, count(*) as games,
    {_sums(TEAM_SUM_COLS)},
    1.0 as share,
    sum(b_poss) as bcfc_den_b
from metrics.team_game_counts
where season_type in ({SEASON_AGG_TYPES_SQL})
group by season, season_type, team_id;
""",
        ),
        (
            "player_season_counts",
            f"""
-- One row per player, season, season type and team; team_id = 0 is the player's whole
-- season across teams (the row player-season reference populations use).
create or replace table metrics.player_season_counts as
select season, season_type, person_id, team_id, count(*) as games,
    any_value(w_bc) as w_bc, any_value(w_fc) as w_fc,
    {_sums(PLAYER_SUM_COLS)},
    0.2 as share,
    sum(b_plays) as bcfc_den_b
from metrics.player_game_counts
where season_type in ({SEASON_AGG_TYPES_SQL})
group by season, season_type, person_id, team_id
union all by name
select season, season_type, person_id, 0 as team_id, count(*) as games,
    any_value(w_bc) as w_bc, any_value(w_fc) as w_fc,
    {_sums(PLAYER_SUM_COLS)},
    0.2 as share,
    sum(b_plays) as bcfc_den_b
from metrics.player_game_counts
where season_type in ({SEASON_AGG_TYPES_SQL})
group by season, season_type, person_id;
""",
        ),
    ]


def measure_select(m: M.Measure, level: str, table: str, keys: list[str], starts) -> str:
    """Long rows (one per unit) for one measure on one level's counts table."""
    game = "c.game_id" if "game_id" in keys else "null::varchar"
    person = "c.person_id" if "person_id" in keys else "null::bigint"
    expr = f"({m.expr})"
    # Spec 5.4: a player is rated in Backcourt / Frontcourt only if at least half his
    # position weight is in that group, so the reference is backcourt (frontcourt) players.
    if "person_id" in keys and m.family in ("backcourt", "frontcourt"):
        w = "w_bc" if m.family == "backcourt" else "w_fc"
        expr = f"case when coalesce(c.{w}, 0) >= 0.5 then {expr} end"
    return f"""
select * from (
    select '{level}' as level, c.season, c.season_type, {game} as game_id, c.team_id,
           {person} as person_id, '{m.tier}' as tier, '{m.name}' as measure,
           '{m.family}' as family,
           {expr}::double as value,
           ({m.exposure})::double as exposure
    from {table} c
    join metrics.league_baselines L using (season)
    where c.season >= {starts[m.start]}
)
where value is not null and isfinite(value)"""


def values_select(level: str, table: str, keys: list[str], kind: str, starts) -> str:
    """Long rows (one per unit x measure) for one level's counts table."""
    ms = [m for m in M.MEASURES if m.levels in ("both", kind)]
    return "\nunion all by name\n".join(measure_select(m, level, table, keys, starts) for m in ms)


def values_sql(starts: dict[str, int]) -> list[tuple[str, str]]:
    """One statement per level and measure (one wide UNNEST trips a DuckDB 2.0 assertion)."""
    out = [
        (
            "values_long",
            "create or replace table metrics.values_long (level varchar, season integer, "
            "season_type varchar, game_id varchar, team_id bigint, person_id bigint, "
            "tier varchar, measure varchar, family varchar, value double, exposure double);",
        )
    ]
    for lv, (t, k, kind) in LEVELS.items():
        stmts = [
            f"insert into metrics.values_long by name {measure_select(m, lv, t, k, starts)};"
            for m in M.MEASURES
            if m.levels in ("both", kind) and not (lv == "player_game" and m.family == "support")
        ]
        out.append((f"values_{lv}", "\n".join(stmts)))
    return out


# ---- Presence -----------------------------------------------------------------------------


def presence_sql() -> list[tuple[str, str]]:
    on_cols = ",\n    ".join(f"oc.{c} as on_{c}" for c in PRESENCE_COLS)
    tm_cols = ",\n    ".join(f"t.{c} as tm_{c}" for c in PRESENCE_COLS)
    agg = ",\n    ".join(
        f"sum(on_{c}) as on_{c}, sum(tm_{c}) - sum(on_{c}) as off_{c}" for c in PRESENCE_COLS
    )
    return [
        (
            "presence_player_game",
            f"""
-- D6: on/off only within games the player appeared in. The team's totals for those games
-- minus the on-court counts are the off-court counts.
create or replace table metrics.presence_player_game as
select b.game_id, b.season, b.season_type, b.game_date, oc.person_id, oc.team_id,
    oc.sec_on,
    {on_cols},
    {tm_cols}
from metrics.oncourt_player_game oc
join metrics.team_game_counts t on t.game_id = oc.game_id and t.team_id = oc.team_id
join metrics.box_player_game b on b.game_id = oc.game_id and b.person_id = oc.person_id;
""",
        ),
        (
            "presence_season_counts",
            f"""
create or replace table metrics.presence_season_counts as
select season, season_type, person_id, team_id, count(*) as games, sum(sec_on) as sec_on,
    {agg}
from metrics.presence_player_game
where season_type in ({SEASON_AGG_TYPES_SQL})
group by season, season_type, person_id, team_id;
""",
        ),
        (
            "presence_box_season",
            f"""
-- Tier B fallback / cross-check: net on/off from box plus-minus and minutes (spec 5.6).
create or replace table metrics.presence_box_season as
select p.season, p.season_type, p.person_id, p.team_id,
       sum(p.b_pm) as pm, sum(p.minutes) as minutes,
       sum(t.b_pts - o.b_pts) as team_margin, sum(t.minutes) as team_minutes,
       48 * sum(p.b_pm) / nullif(sum(p.minutes), 0)
         - 48 * (sum(t.b_pts - o.b_pts) - sum(p.b_pm))
           / nullif(sum(t.minutes) / 5 - sum(p.minutes), 0) as presence_net_box
from metrics.box_player_game p
join metrics.box_team_game t on t.game_id = p.game_id and t.team_id = p.team_id
join metrics.box_team_game o on o.game_id = p.game_id and o.team_id = t.opponent_team_id
where p.b_pm is not null and p.season_type in ({SEASON_AGG_TYPES_SQL})
group by all;
""",
        ),
    ]


def presence_exposure(p: str) -> str:
    return f"(({p}poss + {p}o_poss) / 2.0)"


def presence_values_select(table: str = "metrics.presence_season_counts") -> str:
    items = ",\n        ".join(
        f"{{'measure': '{name}', 'value': ({expr})::double}}"
        for name, expr in M.presence_measures()
    )
    return f"""
select 'player_season' as level, season, season_type, null::varchar as game_id, team_id,
       person_id, 'A' as tier, m.measure, 'presence' as family, m.value,
       least({presence_exposure("on_")}, {presence_exposure("off_")})::double as exposure,
       {presence_exposure("on_")} as on_poss, {presence_exposure("off_")} as off_poss
from (
    select c.*, unnest([
        {items}
    ]) as m
    from {table} c join metrics.league_baselines L using (season)
)
where m.value is not null and isfinite(m.value)
"""


def presence_rolling_sql(windows=(10, 20)) -> str:
    """Raw (unrated) Presence over each player's last N games for the team, per game."""
    sums = ",\n    ".join(
        f"sum(on_{c}) over w as on_{c}, sum(tm_{c}) over w - sum(on_{c}) over w as off_{c}"
        for c in PRESENCE_COLS
    )
    items = ",\n        ".join(
        f"{{'measure': '{name}', 'value': ({expr})::double}}"
        for name, expr in M.presence_measures()
    )
    parts = []
    for n in windows:
        parts.append(f"""
select {n} as window_games, game_id, season, season_type, game_date, person_id, team_id,
       games_in_window, m.measure, m.value
from (
    select r.*, unnest([
        {items}
    ]) as m
    from (
        select game_id, season, season_type, game_date, person_id, team_id,
               count(*) over w as games_in_window,
               {sums}
        from metrics.presence_player_game
        window w as (partition by person_id, team_id, season order by game_date, game_id
                     rows between {n - 1} preceding and current row)
    ) r
    join metrics.league_baselines L using (season)
)
where m.value is not null and isfinite(m.value)""")
    body = "\nunion all\n".join(parts)
    return f"""
create or replace table metrics.presence_rolling_long as {body};
create or replace table metrics.presence_rolling as
pivot metrics.presence_rolling_long on measure using first(value)
group by window_games, game_id, season, season_type, game_date, person_id, team_id,
         games_in_window;
"""


# ---- ratings ------------------------------------------------------------------------------


def _qualify_case(level: str) -> str:
    if not level.startswith("player"):
        return "true"
    idx = 0 if level == "player_season" else 1
    whens = " ".join(
        f"when '{fam}' then v.exposure >= {mins[idx]}" for fam, mins in M.QUALIFY.items()
    )
    return f"case v.family {whens} else false end"


def ratings_sql() -> list[tuple[str, str]]:
    qual = " ".join(f"when v.level = '{lv}' then {_qualify_case(lv)}" for lv in LEVELS)
    adjusted = f"""
    select v.level, v.season, v.season_type, v.game_id, v.team_id, v.person_id, v.tier,
           v.measure, v.family, v.value, v.exposure,
           case when v.family = 'presence' then v.on_poss >= 1000 and v.off_poss >= 300
                {qual}
                else false end as qualifies,
           case when v.level = 'player_season'
                then v.value * v.exposure / (v.exposure + coalesce(k.k, 0))
                else v.value end as value_adj
    from (
        select *, null::double as on_poss, null::double as off_poss from metrics.values_long
        union all by name
        select * from metrics.values_presence
    ) v
    left join metrics.shrinkage_constants k
      on v.level = 'player_season' and k.tier = v.tier and k.measure = v.measure"""
    return [
        (
            "values_presence",
            f"create or replace table metrics.values_presence as {presence_values_select()};",
        ),
        (
            "reference_stats",
            f"""
-- Reference population per (level, season, tier, measure): regular season, qualified,
-- exposure-weighted at player levels, player-season whole-season rows only (section 2).
create or replace table metrics.reference_stats as
with r as (
    select level, season, tier, measure, value_adj,
           case when level like 'player%' then exposure else 1 end as w
    from ({adjusted}) v
    where season_type = 'regular' and qualifies and value_adj is not null
      -- whole-season rows for player measures; Presence exists only per team
      and not (level = 'player_season' and team_id <> 0 and family <> 'presence')
), m as (
    select level, season, tier, measure, sum(w * value_adj) / sum(w) as mu,
           sum(w) as sw, count(*) as n_ref
    from r group by level, season, tier, measure
)
select r.level, r.season, r.tier, r.measure, any_value(m.mu) as mu,
       sqrt(sum(r.w * power(r.value_adj - m.mu, 2)) / any_value(m.sw)) as sigma,
       any_value(m.n_ref) as n_ref
from r
join m on m.level = r.level and m.season = r.season and m.tier = r.tier
      and m.measure = r.measure
group by r.level, r.season, r.tier, r.measure;
""",
        ),
        (
            "ratings_long",
            f"""
create or replace table metrics.ratings_long as
select v.level, v.season, v.season_type, v.game_id, v.team_id, v.person_id, v.tier,
       v.measure, v.family, v.value, v.value_adj, v.exposure, not v.qualifies as low_sample,
       -- explicit null test: DuckDB's least() / greatest() skip NULL arguments, so an
       -- undefined z would otherwise clip to 100
       case when ref.sigma > 0 and v.value_adj is not null
            then greatest(0, least(100, 50 + 10 * (v.value_adj - ref.mu) / ref.sigma))
       end as rating
from ({adjusted}) v
left join metrics.reference_stats ref using (level, season, tier, measure);
""",
        ),
    ]


# ---- wide outputs -------------------------------------------------------------------------


def wide_sql() -> list[tuple[str, str]]:
    """One wide table per level: for each measure, the best tier that exists that season."""
    rank = " ".join(f"when '{t}' then {r}" for t, r in M.TIER_RANK.items())
    out = [
        (
            "best_tier",
            f"""
create or replace table metrics.best_tier as
select level, season, measure, arg_min(tier, case tier {rank} end) as tier
from metrics.reference_stats group by all;
""",
        )
    ]
    for level in ("team_game", "team_season", "player_game", "player_season"):
        keys = "season, season_type, game_id, team_id, person_id"
        # player-game is ~1M units: its wide table carries the headline dimensions only;
        # components stay in metrics.ratings_long.
        only = (
            "and r.measure in (" + ", ".join(f"'{h}'" for h in M.HEADLINE) + ")"
            if level == "player_game"
            else ""
        )
        out.append(
            (
                level,
                f"""
create or replace table metrics.{level} as
with best as (
    select r.* from metrics.ratings_long r
    join metrics.best_tier b using (level, season, measure, tier)
    where r.level = '{level}' {only}
)
pivot best on measure
using first(value) as value, first(rating) as rating, first(tier) as tier,
      first(low_sample) as low_sample
group by {keys};
""",
            )
        )
    return out
