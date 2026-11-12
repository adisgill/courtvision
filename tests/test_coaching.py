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


def test_split_games_partial_for_a_season_in_progress():
    # the page counts 7 games (a: 3-1, then b: 1-2); the data has only the first 6
    wins = [1, 1, 0, 1, 1, 0]
    out = split_games(wins, [("b", 3, 1), ("a", 4, 3)], partial=True)
    assert out == ["a", "a", "a", "a", "b", "b"]
    assert split_games(wins, [("b", 3, 1), ("a", 4, 3)]) is None  # not without partial
    # one coach, page a game ahead of the data
    assert split_games([1, 0], [("c", 3, 2)], partial=True) == ["c", "c"]
    # data with more losses than the page allows can't be his
    assert split_games([0, 0], [("c", 3, 2)], partial=True) is None


PAGE = """<html><table class="sortable stats_table" id="NBA_coaches"><thead><tr>
<th data-stat="coach">Coach</th></tr></thead><tbody>
<tr><th data-stat="coach"><a href='/coaches/maydu99c.html'>Dusty May</a></th>
<td data-stat="team"><a href='/teams/DAL/2027.html'>DAL</a></td>
<td data-stat="cur_g">11</td><td data-stat="cur_w">7</td><td data-stat="cur_l">4</td>
<td data-stat="car_g">11</td></tr>
<tr class="thead"><th data-stat="coach">Coach</th></tr>
<tr><th data-stat="coach"><a href='/coaches/newco01c.html'>New Coach</a></th>
<td data-stat="team"><a href='/teams/MIL/2027.html'>MIL</a></td>
<td data-stat="cur_g"></td><td data-stat="cur_w"></td><td data-stat="cur_l"></td></tr>
</tbody></table></html>"""


def test_parse_season_coaches_page():
    from courtvision.ingest.bbref_coaches import parse

    df = parse(PAGE)
    assert df.coach_id.tolist() == ["maydu99c", "newco01c"]
    assert df.team_abbr.tolist() == ["DAL", "MIL"]
    assert df.iloc[0][["g", "wins", "losses"]].tolist() == [11, 7, 4]
    assert df.iloc[1].g == 0  # hired, no games yet


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
    # one current coach per team
    try:
        sql = "select count(*), count(distinct team_id) from coach.current"
        n, teams = con.execute(sql).fetchall()[0]
        assert n == teams == 30
    except duckdb.CatalogException:
        pass
    con.close()
