from pathlib import Path

from courtvision.config import REPO_ROOT, get_paths


def test_default_data_dir_is_inside_repo(monkeypatch):
    monkeypatch.delenv("COURTVISION_DATA_DIR", raising=False)
    paths = get_paths()
    assert paths.data == REPO_ROOT / "data"
    assert paths.duckdb.name == "courtvision.duckdb"


def test_data_dir_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("COURTVISION_DATA_DIR", str(tmp_path))
    paths = get_paths()
    assert paths.data == tmp_path
    assert paths.kaggle_raw == tmp_path / "raw" / "kaggle"
    assert paths.nba_api_raw == tmp_path / "raw" / "nba_api"
