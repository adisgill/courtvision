"""Coach assignment, tempo control and the reliability test (spec section 15)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from courtvision.coaching import reliability, tempo_control
from courtvision.config import get_paths
from courtvision.ingest.coaches import split_games


def test_split_games_finds_the_order_that_matches_the_results():
    # 5 games: W W L L W; "a" went 2-0, "b" 1-2, listed in the wrong order
    wins = [1, 1, 0, 0, 1]
    out = split_games(wins, [("b", 3, 1), ("a", 2, 2)])
    assert out == ["a", "a", "b", "b", "b"]


def test_split_games_none_when_records_do_not_add_up():
    assert split_games([1, 0, 1], [("a", 2, 1)]) is None  # games don't sum
    assert split_games([1, 1, 1], [("a", 1, 0), ("b", 2, 2)]) is None  # wins never match


def _tg(rows):
    return pd.DataFrame(rows, columns=["game_id", "season", "team_id", "poss", "coach_id"])


def test_tempo_control_sign_and_symmetry():
    # team 1 usually plays at 100, team 2 at 90; their game is played at 98
    rows = [("g0", 1, 1, 100, "c1"), ("g0", 1, 3, 100, "c3"),
            ("g1", 1, 2, 90, "c2"), ("g1", 1, 4, 90, "c4"),
            ("g2", 1, 1, 98, "c1"), ("g2", 1, 2, 98, "c2")]  # fmt: skip
    t = tempo_control(_tg(rows))
    g = t[t.game_id == "g2"].set_index("team_id")
    assert g.loc[1, "usual"] == 100 and g.loc[2, "usual"] == 90
    assert g.loc[1, "control"] == pytest.approx(8 - 2)  # |98-90| - |98-100|
    assert g.loc[1, "control"] + g.loc[2, "control"] == pytest.approx(0)
    assert g.loc[1, "opp_coach_id"] == "c2"


def test_tempo_control_uses_one_pace_per_game():
    # possession counts can differ by one between the two teams: the game has one pace
    t = tempo_control(_tg([("g", 1, 1, 99, "a"), ("g", 1, 2, 100, "b")]))
    assert t.game_pace.nunique() == 1 and t.game_pace.iloc[0] == 99.5


def _coach_rows(effect_sd: float, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for c in range(40):
        eff = rng.normal(0, effect_sd)
        for s in (1, 2):
            for d in range(82):
                rows.append({"coach_id": c, "season": s, "game_date": d,
                             "x": eff + rng.normal(0, 12)})  # fmt: skip
    return pd.DataFrame(rows)


def test_reliability_tells_signal_from_noise():
    signal = reliability(_coach_rows(4.0, 0), "x")
    noise = reliability(_coach_rows(0.0, 1), "x")
    assert signal["split_half"] > 0.5 and signal["year_to_year"] > 0.5
    assert abs(noise["split_half"]) < 0.35 and abs(noise["year_to_year"]) < 0.4
    # evidence needed before a coach's own number counts as much as zero: ~ 144 / 16 = 9
    assert 4 < signal["k_games"] < 20


@pytest.mark.integration
def test_built_coach_tables():
    import duckdb

    path = get_paths().duckdb
    if not path.exists():
        pytest.skip("no database")
    con = duckdb.connect(str(path), read_only=True)
    try:
        con.execute("select 1 from coach.tempo limit 1")
    except duckdb.Error:
        pytest.skip("coaching not built; run `courtvision coaching`")
    worst = con.execute(
        "select max(abs(s)) from (select sum(control) s from coach.tempo group by game_id)"
    ).fetchall()[0][0]
    assert worst < 1e-9
    # every regular-season team-game since 1996-97 has a coach
    missing = con.execute(
        """select count(*) from stg.team_games t
           left join coach.assignments a using (game_id, team_id)
           where t.season_type = 'regular' and t.season >= 1996 and t.is_win is not null
             and a.coach_id is null"""
    ).fetchall()[0][0]
    assert missing == 0
    con.close()
