"""A cached, rate-limited wrapper around nba_api's stats endpoints.

Every response is written to disk *before* it is parsed, as gzipped JSON in
an envelope that records the endpoint, the exact request parameters, and
when it was fetched::

    data/raw/nba_api/<endpoint>/<readable-key>__<param-hash>.json.gz

A cache hit never touches the network, so re-running a pull is free and an
interrupted pull resumes where it stopped. Delete a file to refetch it.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Parameters worth putting in the human-readable part of a cache filename.
_KEY_PARAMS = ("GameID", "Season", "SeasonType", "TeamID", "PlayerID", "PlayerOrTeam")


class FetchError(RuntimeError):
    """The endpoint failed after every retry."""


def cache_key(params: Mapping[str, Any]) -> str:
    """Stable filename stem: readable id params + a hash of all params."""
    canon = json.dumps({k: params[k] for k in sorted(params)}, default=str, separators=(",", ":"))
    digest = hashlib.sha1(canon.encode()).hexdigest()[:10]
    readable = "_".join(f"{k}-{params[k]}" for k in _KEY_PARAMS if params.get(k) not in (None, ""))
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "", readable.replace(" ", ""))
    return f"{readable}__{digest}" if readable else digest


@dataclass
class CachedClient:
    cache_dir: Path
    min_interval: float = 1.0
    """Seconds between the *starts* of consecutive network requests."""
    jitter: float = 0.5
    timeout: float = 30.0
    max_retries: int = 5
    _last_request: float = field(default=0.0, init=False, repr=False)
    network_calls: int = field(default=0, init=False)
    cache_hits: int = field(default=0, init=False)

    def path_for(self, endpoint: str, params: Mapping[str, Any]) -> Path:
        return self.cache_dir / endpoint / f"{cache_key(params)}.json.gz"

    def get(self, endpoint_cls: type, **kwargs: Any) -> dict:
        """Return the raw response dict for ``endpoint_cls(**kwargs)``."""
        ep = endpoint_cls(**kwargs, get_request=False)
        return self.get_raw(ep.endpoint, ep.parameters)

    def get_raw(self, endpoint: str, params: Mapping[str, Any]) -> dict:
        path = self.path_for(endpoint, params)
        if path.exists():
            self.cache_hits += 1
            return read_envelope(path)["response"]
        response, url = self._fetch(endpoint, dict(params))
        envelope = {
            "endpoint": endpoint,
            "params": dict(params),
            "url": url,
            "fetched_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "response": response,
        }
        _write_atomic(path, envelope)
        return response

    def _throttle(self) -> None:
        wait = self._last_request + self.min_interval + random.uniform(0, self.jitter)
        delay = wait - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()

    def _fetch(self, endpoint: str, params: dict) -> tuple[dict, str | None]:
        from nba_api.stats.library.http import NBAStatsHTTP

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            self.network_calls += 1
            try:
                resp = NBAStatsHTTP().send_api_request(
                    endpoint=endpoint, parameters=params, timeout=self.timeout
                )
                status = getattr(resp, "_status_code", 200)
                text = resp.get_response()
                if status != 200:
                    raise FetchError(f"HTTP {status}: {text[:200]!r}")
                return json.loads(text), resp.get_url()
            except Exception as err:  # network errors, timeouts, bad JSON, HTTP errors
                last_err = err
                backoff = min(120.0, 5.0 * 2**attempt) + random.uniform(0, 2)
                log.warning(
                    "%s %s failed (attempt %d/%d): %s; retrying in %.0fs",
                    endpoint, cache_key(params), attempt + 1, self.max_retries, err, backoff,
                )  # fmt: skip
                time.sleep(backoff)
        raise FetchError(f"{endpoint} {cache_key(params)}: {last_err}") from last_err


def read_envelope(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _write_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    tmp.replace(path)


def result_sets(response: dict) -> dict[str, list[dict]]:
    """Rows of a legacy ``resultSets``/``resultSet`` response, by set name."""
    sets = response.get("resultSets", response.get("resultSet"))
    if sets is None:
        raise ValueError(f"not a legacy response; top-level keys: {sorted(response)}")
    if isinstance(sets, dict):
        sets = [sets]
    return {
        s["name"]: [dict(zip(s["headers"], row, strict=True)) for row in s["rowSet"]] for s in sets
    }
