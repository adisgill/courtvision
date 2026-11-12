"""Download the Kaggle dataset exactly as published.

The dataset is re-published nightly, so every download writes a manifest
(``_manifest.json``) recording when it was fetched and the size of every
file. Pass ``version`` to pin a specific Kaggle dataset version.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from courtvision.config import KAGGLE_DATASET, load_env

MANIFEST = "_manifest.json"


def _api():
    """Authenticate lazily: importing ``kaggle`` authenticates as a side effect."""
    load_env()
    if not os.environ.get("KAGGLE_API_TOKEN") and not (Path.home() / ".kaggle").exists():
        raise RuntimeError(
            "No Kaggle credentials. Set KAGGLE_API_TOKEN in .env "
            "(see .env.example) or create ~/.kaggle/access_token."
        )
    from kaggle.api.kaggle_api_extended import KaggleApi

    api = KaggleApi()
    api.authenticate()
    return api


def download(dest: Path, *, version: int | None = None, force: bool = False) -> dict:
    """Download and unzip every file in the dataset into ``dest``.

    Returns the manifest that was written alongside the files.
    """
    dest.mkdir(parents=True, exist_ok=True)
    ref = KAGGLE_DATASET if version is None else f"{KAGGLE_DATASET}/versions/{version}"
    api = _api()
    api.dataset_download_files(ref, path=str(dest), unzip=True, quiet=False, force=force)

    files = sorted(p for p in dest.iterdir() if p.is_file() and p.name != MANIFEST)
    manifest = {
        "dataset": KAGGLE_DATASET,
        "requested_version": version,
        "downloaded_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": {p.name: p.stat().st_size for p in files},
    }
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
