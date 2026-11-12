"""Every rated quantity, written once as a SQL expression (docs/metrics_spec.md section 5).

Expressions use the column conventions of :mod:`courtvision.metrics.sql` (``a_*`` actor
shots, plain = team offense, ``o_*`` = opponent, ``b_*`` = box score, ``bc_*``/``fc_*`` =
position-weighted, ``L.*`` = league baselines) and are applied unchanged to team-game,
team-season, player-game and player-season counts. Because each level's counts are sums
of the level below, every value is a ratio of sums and roll-ups are exact.

Orientation: positive is always good for the unit (fewer points allowed is positive).
Units: points per 100 of the measure's exposure, relative to that season's league average.
"""

from __future__ import annotations

from dataclasses import dataclass

# Box-score inputs each tier needs (D9: a tier exists from the first season in which all
# of its inputs are recorded in >= 95% of team-games). Tier A needs play-by-play instead.
TIER_REQUIREMENTS = {
    "shoot_C": ["fga", "fta"],
    "shoot_B": ["fga", "fg3a", "fta"],
    "bt": ["fga", "fta", "tov"],
    "ball_B": ["fga", "fta", "tov", "oreb", "dreb"],
    "def_Bm": ["fga", "fta", "tov", "oreb", "dreb"],
    "def_B": ["fga", "fg3a", "fta", "tov", "oreb", "dreb"],
    "bcfc_B": ["fga", "fg3a", "fta", "tov"],
    "ast": ["fga", "fta", "tov", "ast"],
}

# Qualifying minimum exposure per family at player levels: (player-season, player-game).
# Spec section 5; Backcourt/Frontcourt use Defense's possessions minimum.
QUALIFY = {
    "shooting": (200, 8),
    "defense": (1000, 20),
    "ball": (500, 10),
    "support": (500, 10),
    "backcourt": (1000, 20),
    "frontcourt": (1000, 20),
    # Presence's exposure is min(on, off) possessions; the full rule (on >= 1000 and
    # off >= 300) is applied in courtvision.metrics.rate.
    "presence": (300, 0),
}
TIER_RANK = {"A": 1, "B": 2, "B-": 3, "C": 4}
HEADLINE = ("shooting", "defense", "ball_control", "backcourt", "frontcourt")


@dataclass(frozen=True)
class Measure:
    name: str
    family: str  # shooting / defense / ball / backcourt / frontcourt / support
    tier: str  # A / B / B- / C
    expr: str
    exposure: str
    start: str  # key into tier_starts(): first season the tier exists
    levels: str = "both"  # both / team / player


def tier_starts(con) -> dict[str, int]:
    """First season of every tier (box tiers from D9 availability; A from play-by-play)."""
    rows = con.execute("select stat, first_season from metrics.stat_first_season").fetchall()
    first = dict(rows)
    starts = {name: max(first[s] for s in stats) for name, stats in TIER_REQUIREMENTS.items()}
    starts["A"] = con.execute("select min(season) from metrics.possessions").fetchall()[0][0]
    return starts


def _xp_b(p: str = "b_") -> str:
    """Expected FG points from the two-class (2PT / 3PT) league make rates (tier B)."""
    return f"(2 * ({p}fga - {p}fg3a) * L.lg_p2_b + 3 * {p}fg3a * L.lg_p3_b)"


def ft_net(b: str = "") -> str:
    """Net points of one extra free-throw attempt (spec v1.1, section 5.1).

    A trip to the line mostly replaces a shot: a two-shot foul ends the possession
    without the field-goal attempt that was fouled. So an FTA is worth its make rate
    minus half a league shot (two FTA per replaced attempt). ``b`` = '' (play-by-play
    constants) or '_b' (box-score constants). Validation against NBA.com's ratings put the
    old full-value weighting at 0.27 of its points (docs/validation.md section 6).
    """
    return f"(L.lg_ftpct{b} - 0.5 * L.lg_pps{b})"


def free_throws(p: str = "b_", den: str | None = None) -> dict[str, str]:
    """Foul drawing (``fd``) and free-throw making (``fm``), per 100 of ``den``
    (default: the unit's FGA). Box-score counts and constants, so every tier has them."""
    den = den or f"{p}fga"
    return {
        "fd": f"100 * ({p}fta - L.lg_ftr_b * {p}fga) * {ft_net('_b')} / nullif({den}, 0)",
        "fm": f"100 * ({p}ftm - L.lg_ftpct_b * {p}fta) / nullif({den}, 0)",
    }


def defense_a(p: str = "") -> dict[str, str]:
    """Tier A Defense components. ``p`` prefixes the team columns (used by Presence)."""
    return {
        "dq": f"100 * (L.lg_pps * {p}o_fga_r - {p}o_xp_r) / nullif({p}o_poss, 0)",
        "dm": f"100 * ({p}o_xp_r - {p}o_pts_r) / nullif({p}o_poss, 0)",
        "dt": f"100 * ({p}o_tov / nullif({p}o_poss, 0) - L.lg_tovr) * L.lg_ppnt",
        "df": f"100 * (L.lg_ftr * {p}o_fga - {p}o_fta) / nullif({p}o_poss, 0) * {ft_net()}",
        # Defensive rebounds above the league rate for the chances the unit had, per 100
        # opponent possessions, each worth a possession (lgPPP). Linear in counts, so it
        # rolls up exactly (section 10).
        "dr": (
            f"100 * ({p}dreb - L.lg_dreb_pct * ({p}dreb + {p}o_oreb))"
            f" / nullif({p}o_poss, 0) * L.lg_ppp"
        ),
    }


def _defense_b() -> dict[str, str]:
    return {
        "dq": f"100 * (L.lg_pps_b * o_b_fga - {_xp_b('o_b_')}) / nullif(o_b_poss, 0)",
        "dm": f"100 * ({_xp_b('o_b_')} - (o_b_pts - o_b_ftm)) / nullif(o_b_poss, 0)",
        "dqm": "100 * (L.lg_pps_b * o_b_fga - (o_b_pts - o_b_ftm)) / nullif(o_b_poss, 0)",
        "dt": "100 * (o_b_tov / nullif(o_b_poss, 0) - L.lg_tovr_b) * L.lg_ppnt_b",
        "df": f"100 * (L.lg_ftr_b * o_b_fga - o_b_fta) / nullif(o_b_poss, 0) * {ft_net('_b')}",
        "dr": (
            "100 * (b_dreb - L.lg_dreb_pct_b * (b_dreb + o_b_oreb))"
            " / nullif(o_b_poss, 0) * L.lg_ppp_b"
        ),
    }


def ball_bo_a(p: str = "") -> str:
    """Offensive rebounds above the league rate for the chances, per 100 possessions."""
    return (
        f"100 * ({p}oreb - L.lg_oreb_pct * ({p}oreb + {p}o_dreb)) / nullif({p}poss, 0) * L.lg_ppp"
    )


def court_parts(side: str) -> dict[str, str]:
    """Backcourt (``bc``) or Frontcourt (``fc``) parts, tier A, points per 100 possessions.

    Offense is the position-weighted actor counts (D4(d), D5); defense is opponent shots in
    the group's zones (perimeter / paint) plus position-weighted steals and fouls.
    ``share`` is 1 for a team and 1/5 for a player, so a player's value is his share of
    the team's and the five players on the floor sum to the team's value.
    """
    zone = "perim" if side == "bc" else "paint"
    ft = free_throws(f"{side}_b_", "poss")
    parts = {
        "sh": f"100 * ({side}_pts_r - L.lg_pps * {side}_fga_r) / nullif(poss, 0)",
        "ft": f"{ft['fd']} + {ft['fm']}",
        "bt": (
            f"100 * (L.lg_tovp * {side}_b_plays - {side}_b_tov) * L.lg_ppnt_b / nullif(poss, 0)"
        ),
        "dz": (
            f"100 * share * ((L.lg_pps * o_fga_{zone} - o_pts_{zone}) - L.lg_dz_{zone} * o_poss)"
            " / nullif(o_poss, 0)"
        ),
        "dt": (
            f"100 * ({side}_b_stl - share * L.lg_{side}_stl * o_poss) * L.lg_ppnt"
            " / nullif(o_poss, 0)"
        ),
        # a personal foul costs its free throws net of the shot they replace (ft_net)
        "df": (
            f"-100 * ({side}_b_pf - share * L.lg_{side}_pf * o_poss) * L.lg_ftpts_per_pf"
            f" * {ft_net('_b')} / L.lg_ftpct_b / nullif(o_poss, 0)"
        ),
    }
    if side == "fc":
        parts["dr"] = "share * " + defense_a()["dr"]
    return parts


def total(exprs) -> str:
    return " + ".join(f"({e})" for e in exprs)


def build_measures() -> list[Measure]:
    m: list[Measure] = []

    def add(*args, **kw):
        m.append(Measure(*args, **kw))

    # ---- Shooting (5.1): field goals by zone + free throws (v1.1) ---------------------
    ft = free_throws()
    fg_a = "100 * (a_pts_r / nullif(a_fga_r, 0) - L.lg_pps)"
    add("shooting", "shooting", "A", total([fg_a, ft["fd"], ft["fm"]]), "a_fga_r", "A")
    add(
        "shooting_sq",
        "shooting",
        "A",
        "100 * (a_xp_r / nullif(a_fga_r, 0) - L.lg_pps)",
        "a_fga_r",
        "A",
    )
    add(
        "shooting_sm",
        "shooting",
        "A",
        "100 * (a_pts_r - a_xp_r) / nullif(a_fga_r, 0)",
        "a_fga_r",
        "A",
    )
    fg_b = "100 * ((b_pts - b_ftm) / nullif(b_fga, 0) - L.lg_pps_b)"
    add("shooting", "shooting", "B", total([fg_b, ft["fd"], ft["fm"]]), "b_fga", "shoot_B")
    add(
        "shooting_sq",
        "shooting",
        "B",
        f"100 * ({_xp_b()} / nullif(b_fga, 0) - L.lg_pps_b)",
        "b_fga",
        "shoot_B",
    )
    add(
        "shooting_sm",
        "shooting",
        "B",
        f"100 * ((b_pts - b_ftm) - {_xp_b()}) / nullif(b_fga, 0)",
        "b_fga",
        "shoot_B",
    )
    add("shooting", "shooting", "C", total([fg_b, ft["fd"], ft["fm"]]), "b_fga", "shoot_C")
    for tier, start in (("A", "A"), ("B", "shoot_B"), ("C", "shoot_C")):
        add("shooting_fd", "shooting", tier, ft["fd"], "b_fga", start)
        add("shooting_fm", "shooting", tier, ft["fm"], "b_fga", start)

    # ---- Defense (5.2): A for teams and on-court for players (D7); box tiers teams only
    da = defense_a()
    add("defense", "defense", "A", total(da.values()), "o_poss", "A")
    for k, e in da.items():
        add(f"defense_{k}", "defense", "A", e, "o_poss", "A")
    db = _defense_b()
    b_parts = ("dq", "dm", "dt", "df", "dr")
    add("defense", "defense", "B", total(db[k] for k in b_parts), "o_b_poss", "def_B", "team")
    for k in b_parts:
        add(f"defense_{k}", "defense", "B", db[k], "o_b_poss", "def_B", "team")
    bm_parts = ("dqm", "dt", "df", "dr")
    add("defense", "defense", "B-", total(db[k] for k in bm_parts), "o_b_poss", "def_Bm", "team")
    for k in bm_parts:
        add(f"defense_{k}", "defense", "B-", db[k], "o_b_poss", "def_Bm", "team")
    # Supporting sub-ratings (D7(b)): credited events per 100 on-court defensive possessions
    add("defense_stl_rate", "support", "A", "100 * b_stl / nullif(o_poss, 0)", "o_poss", "A")
    add("defense_blk_rate", "support", "A", "100 * b_blk / nullif(o_poss, 0)", "o_poss", "A")
    add("defense_pf_rate", "support", "A", "-100 * b_pf / nullif(o_poss, 0)", "o_poss", "A")

    # ---- Ball control (5.3) -------------------------------------------------------------
    bt = "100 * (L.lg_tovp - b_tov / nullif(b_plays, 0)) * L.lg_ppnt_b"
    bo_b = "100 * (b_oreb - L.lg_oreb_pct_b * (b_oreb + o_b_dreb)) / nullif(b_poss, 0) * L.lg_ppp_b"
    add("ball_control", "ball", "A", total([bt, ball_bo_a()]), "b_plays", "A")
    add("ball_bt", "ball", "A", bt, "b_plays", "A")
    add("ball_bo", "ball", "A", ball_bo_a(), "poss", "A")
    add("ball_control", "ball", "B", total([bt, bo_b]), "b_plays", "ball_B", "team")
    add("ball_bo", "ball", "B", bo_b, "b_poss", "ball_B", "team")
    add("ball_control", "ball", "B", bt, "b_plays", "bt", "player")
    add("ball_bt", "ball", "B", bt, "b_plays", "bt")
    add("ball_shot_end_rate", "support", "B", "1 - b_tov / nullif(b_plays, 0)", "b_plays", "bt")
    add(
        "ball_ato",
        "support",
        "B",
        "ln((b_ast + 0.5) / (b_tov + 0.5)) - ln(L.lg_ato)",
        "b_plays",
        "ast",
    )
    add("ball_ast_rate", "support", "B", "b_ast / nullif(b_fgm, 0) - L.lg_ast_rate", "b_fgm", "ast")

    # ---- Backcourt / Frontcourt (5.4, 5.5) ----------------------------------------------
    for side, name in (("bc", "backcourt"), ("fc", "frontcourt")):
        parts = court_parts(side)
        add(name, name, "A", total(parts.values()), "poss", "A")
        for k, e in parts.items():
            add(f"{name}_{k}", name, "A", e, "poss", "A")
        box = (
            f"100 * (({side}_b_pts - {side}_b_ftm - L.lg_pps_b * {side}_b_fga)"
            f" + ({side}_b_fta - L.lg_ftr_b * {side}_b_fga) * {ft_net('_b')}"
            f" + ({side}_b_ftm - L.lg_ftpct_b * {side}_b_fta)"
            f" + (L.lg_tovp * {side}_b_plays - {side}_b_tov) * L.lg_ppnt_b)"
            " / nullif(bcfc_den_b, 0)"
        )
        add(name, name, "B", box, "bcfc_den_b", "bcfc_B")
    return m  # fmt: skip


MEASURES = build_measures()


# ---- Presence (5.6) -------------------------------------------------------------------------


def presence_parts(p: str) -> dict[str, str]:
    """Team values over the counts prefixed ``p`` (``on_`` / ``off_``).

    Native units per dimension (Shooting per 100 FGA, Defense per 100 opponent
    possessions, Ball control per 100 plays + offensive rebounding), plus each converted
    to points per 100 team possessions (``*_pp``) for the composite.
    """
    plays = f"({p}fga + L.c_ft * {p}fta + {p}tov)"
    bc_plays = f"({p}bc_fga + L.c_ft * {p}bc_fta + {p}bc_tov)"
    fc_plays = f"({p}fc_fga + L.c_ft * {p}fc_fta + {p}fc_tov)"
    da = defense_a(p)
    defense = total(da.values())
    bo = ball_bo_a(p)

    def court(side: str, side_plays: str, zone: str, extra: str = "") -> str:
        return total(
            [
                f"100 * ({p}{side}_pts_r - L.lg_pps * {p}{side}_fga_r) / nullif({p}poss, 0)",
                f"100 * (L.lg_tovp_pbp * {side_plays} - {p}{side}_tov) * L.lg_ppnt"
                f" / nullif({p}poss, 0)",
                f"100 * ((L.lg_pps * {p}o_fga_{zone} - {p}o_pts_{zone})"
                f" - L.lg_dz_{zone} * {p}o_poss) / nullif({p}o_poss, 0)",
            ]
            + ([extra] if extra else [])
        )

    # free throws on the play-by-play counts and constants (v1.1)
    ft = f"({p}fta - L.lg_ftr * {p}fga) * {ft_net()} + ({p}ftm - L.lg_ftpct * {p}fta)"
    return {
        "shooting": (
            f"100 * ({p}pts_r / nullif({p}fga_r, 0) - L.lg_pps) + 100 * ({ft}) / nullif({p}fga, 0)"
        ),
        "shooting_pp": f"100 * ({p}pts_r - L.lg_pps * {p}fga_r + {ft}) / nullif({p}poss, 0)",
        "defense": defense,
        "ball_control": total(
            [f"100 * (L.lg_tovp_pbp - {p}tov / nullif({plays}, 0)) * L.lg_ppnt", bo]
        ),
        "ball_control_pp": total(
            [f"100 * (L.lg_tovp_pbp * {plays} - {p}tov) * L.lg_ppnt / nullif({p}poss, 0)", bo]
        ),
        "backcourt": court("bc", bc_plays, "perim"),
        "frontcourt": court("fc", fc_plays, "paint", da["dr"]),
    }


def presence_measures() -> list[tuple[str, str]]:
    """(name, expression) for every Presence output: on - off per dimension + composite."""
    on, off = presence_parts("on_"), presence_parts("off_")
    out = [
        (f"presence_{d}", f"({on[d]}) - ({off[d]})")
        for d in ("shooting", "defense", "ball_control", "backcourt", "frontcourt")
    ]
    composite = total(
        f"({on[d]}) - ({off[d]})" for d in ("shooting_pp", "defense", "ball_control_pp")
    )
    out.insert(0, ("presence", composite))
    return out
