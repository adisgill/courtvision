from courtvision.ingest.nba_api_client import CachedClient, cache_key, result_sets


def test_cache_key_is_stable_and_readable():
    a = cache_key({"GameID": "0022500001", "StartPeriod": 0, "EndPeriod": 0})
    b = cache_key({"EndPeriod": 0, "StartPeriod": 0, "GameID": "0022500001"})
    assert a == b
    assert a.startswith("GameID-0022500001__")
    assert cache_key({"GameID": "0022500002"}) != cache_key({"GameID": "0022500001"})


def test_cache_key_strips_unsafe_characters():
    key = cache_key({"Season": "2025-26", "SeasonType": "Regular Season"})
    assert key.startswith("Season-2025-26_SeasonType-RegularSeason__")


def test_cache_hit_skips_network(tmp_path, monkeypatch):
    client = CachedClient(cache_dir=tmp_path)
    calls = []

    def fake_fetch(endpoint, params):
        calls.append(endpoint)
        return {"resultSets": [{"name": "X", "headers": ["A"], "rowSet": [[1]]}]}, "url"

    monkeypatch.setattr(client, "_fetch", fake_fetch)
    first = client.get_raw("someendpoint", {"GameID": "1"})
    second = client.get_raw("someendpoint", {"GameID": "1"})
    assert first == second
    assert calls == ["someendpoint"]
    assert client.cache_hits == 1
    assert result_sets(first) == {"X": [{"A": 1}]}
