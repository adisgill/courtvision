import pytest

from courtvision.ingest import nba_api_pull


class FakeClient:
    """Records which endpoints were requested; returns a tiny game log."""

    network_calls = 0
    cache_hits = 0

    def __init__(self):
        self.calls: list[str] = []

    def get(self, endpoint_cls, **kwargs):
        name = endpoint_cls.__name__
        self.calls.append(name)
        if name == "LeagueGameLog":
            return {
                "resultSets": [
                    {
                        "name": "LeagueGameLog",
                        "headers": ["GAME_ID", "TEAM_ID"],
                        "rowSet": [["0022500001", 1], ["0022500001", 2]],
                    }
                ]
            }
        return {}


def test_season_string():
    assert nba_api_pull.season_string(2025) == "2025-26"
    assert nba_api_pull.season_string(1999) == "1999-00"


def test_pull_everything():
    c = FakeClient()
    summary = nba_api_pull.pull(c, 2025, ["regular"])
    assert summary["games"] == 1 and summary["teams"] == 2
    assert c.calls.count("ShotChartDetail") == 2  # one per team
    assert c.calls.count("PlayByPlayV3") == 1
    assert c.calls.count("GameRotation") == 1


def test_pull_shots_only_skips_per_game_endpoints():
    c = FakeClient()
    nba_api_pull.pull(c, 2025, ["regular"], only=["shots"])
    assert "PlayByPlayV3" not in c.calls and "GameRotation" not in c.calls
    assert c.calls.count("ShotChartDetail") == 2


def test_pull_rejects_unknown_group():
    with pytest.raises(ValueError, match="unknown endpoint group"):
        nba_api_pull.pull(FakeClient(), 2025, ["regular"], only=["boxscores"])
