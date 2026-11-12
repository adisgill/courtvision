"""Live-feed actions map to the same events and zones as the historical pipeline."""

from __future__ import annotations

import duckdb
import pytest

from courtvision import live
from courtvision.db import MACROS
from courtvision.metrics import pbp

A, B = 1610612760, 1610612745


def action(n, act, team=A, person=1, **kw):
    a = {
        "actionNumber": n, "orderNumber": n * 10000, "period": 1, "clock": "PT11M00.00S",
        "actionType": act, "subType": "", "teamId": team, "personId": person,
        "possession": team,
    }  # fmt: skip
    a.update(kw)
    return a


def test_actions_map_to_events():
    actions = [
        action(1, "period", team=0, person=0, subType="start", clock="PT12M00.00S"),
        action(2, "3pt", shotResult="Missed", xLegacy=0, yLegacy=250, clock="PT11M40.00S"),
        action(3, "rebound", team=B, person=2, subType="defensive", clock="PT11M38.00S"),
        action(4, "2pt", team=B, person=2, shotResult="Made", xLegacy=0, yLegacy=10),
        action(5, "freethrow", team=B, person=2, subType="1 of 1", shotResult="Made"),
        action(6, "freethrow", team=A, person=1, subType="1 of 1", descriptor="technical",
               shotResult="Made"),
        action(7, "rebound", team=0, person=0, subType="offensive", possession=A),
        action(8, "substitution", team=A, person=3, subType="out"),
    ]  # fmt: skip
    ev = live.actions_to_events("0022600001", actions)
    assert [e.etype for e in ev] == [
        "period_start", "fga", "reb", "fga", "ft", "ft", "reb", "sub",
    ]  # fmt: skip
    assert ev[1].made is False and ev[3].made is True
    assert ev[1].elapsed_s == pytest.approx(20.0)
    assert ev[2].reb_kind == "def"
    assert ev[4].ft_last is True and ev[4].ft_technical is False
    assert ev[5].ft_technical is True and ev[5].ft_last is False
    assert ev[6].team_id == A and ev[6].person_id is None  # team rebound via possession
    assert ev[7].sub_dir == "out"
    assert all(isinstance(e, pbp.Event) for e in ev)


@pytest.mark.parametrize(
    "value,x,y",
    [(2, 0, 10), (2, 50, 50), (2, 70, 130), (2, 100, 100), (2, 0, 200), (3, -230, 50),
     (3, 230, 80), (3, 0, 260), (3, 0, 450), (2, None, 10)],
)  # fmt: skip
def test_python_zone_matches_sql_macro(value, x, y):
    con = duckdb.connect()
    con.execute(MACROS)
    sql = con.execute("select cv_zone(?, ?, ?)", [value, x, y]).fetchall()[0][0]
    assert live.zone(value, x, y) == sql
