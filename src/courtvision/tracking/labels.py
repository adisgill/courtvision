"""Hand labels for the defense detectors: sample events, draw them, score the detectors.

``sample`` draws about 300 candidate ball screens from the loose pool (stratified: screens
the detector found, by its coverage call, plus near-misses it rejected) and 60 half-court
possessions (30 it called zone, 30 man), in a shuffled order under neutral ids, so the
labeler can't tell what the detector said. ``draw`` renders each as a PNG strip of court
panels (``docs/tracking_labeling_rules.md`` describes them and the labeling rules). The
pictures are drawn with numpy and written with zlib, so no imaging library is needed.

``evaluate`` compares the detector with the labels: screen detection precision and recall
and the coverage confusion table, reweighted for the stratified sample.

The strips show raw positions, so they stay under ``data/`` (decision D1); the labels file
(event ids, game ids, frames and the labels) is derived and lives in ``docs/``.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

from courtvision.config import get_paths
from courtvision.tracking import sportvu
from courtvision.tracking.detect import FPS

STRATA = {"drop": 60, "show": 40, "switch": 40, "blitz": 35, "ice": 35, "rejected": 90}
POSSESSIONS = {"zone": 30, "man": 30}


def labels_dir() -> Path:
    return get_paths().data / "tracking" / "labels"


def labels_file() -> Path:
    return Path(__file__).resolve().parents[3] / "docs" / "tracking_labels.csv"


# --------------------------------------------------------------------------- the sample


def sample(seed: int = 7) -> pd.DataFrame:
    """The events to label, shuffled, with neutral ids (``s001``... and ``p01``...)."""
    d = get_paths().data / "tracking" / "defense"
    pool = pd.read_parquet(d / "pool.parquet")
    strict = pd.read_parquet(d / "screens.parquet")
    rng = np.random.default_rng(seed)
    picks = []
    det = pool[pool.detected].merge(strict[["game_id", "frame", "coverage"]]
                                    .rename(columns={"frame": "sframe", "coverage": "called"}),
                                    on="game_id")  # fmt: skip
    det = det[(det.frame - det.sframe).abs() <= 12].drop_duplicates(["game_id", "frame"])
    for cov, k in STRATA.items():
        src = pool[~pool.detected].assign(called="none") if cov == "rejected" \
            else det[det.called == cov]  # fmt: skip
        take = src.iloc[rng.choice(len(src), size=min(k, len(src)), replace=False)]
        picks.append(take.assign(stratum=cov, stratum_size=len(src)))
    s = pd.concat(picks, ignore_index=True)
    s = s.iloc[rng.permutation(len(s))].reset_index(drop=True)
    s["event"] = [f"s{i + 1:03d}" for i in range(len(s))]

    pos = pd.read_parquet(d / "possessions.parquet")
    pos = pos[pos.readable]
    pp = []
    for kind, k in POSSESSIONS.items():
        src = pos[pos.zone == (kind == "zone")]
        take = src.iloc[rng.choice(len(src), size=min(k, len(src)), replace=False)]
        pp.append(take.assign(stratum=kind, stratum_size=len(src)))
    p = pd.concat(pp, ignore_index=True)
    p = p.iloc[rng.permutation(len(p))].reset_index(drop=True)
    p["event"] = [f"p{i + 1:02d}" for i in range(len(p))]
    out = pd.concat([s.assign(kind="screen"), p.assign(kind="possession")], ignore_index=True)
    labels_dir().mkdir(parents=True, exist_ok=True)
    out.to_parquet(labels_dir() / "sample.parquet", index=False)
    return out


# --------------------------------------------------------------------------- drawing

SCALE = 8  # px per foot
PANEL_W, PANEL_H = 50 * SCALE, 47 * SCALE
MARGIN = 12
OFFENSE, DEFENSE = (40, 100, 210), (205, 50, 60)
TRAIL_O, TRAIL_D = (150, 180, 235), (235, 160, 165)
BALL, RING = (245, 150, 20), (245, 150, 20)
COURT, LINES, BG = (238, 230, 214), (150, 138, 115), (255, 255, 255)
WHITE = (255, 255, 255)

# 3 x 5 bitmap digits
_GLYPHS = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111",
    "3": "111001111001111", "4": "101101111001001", "5": "111100111001111",
    "6": "111100111101111", "7": "111001010010010", "8": "111101111101111",
    "9": "111101111001111",
}  # fmt: skip


class Canvas:
    def __init__(self, w: int, h: int, bg=BG):
        self.a = np.empty((h, w, 3), np.uint8)
        self.a[:] = bg
        yy, xx = np.mgrid[0:h, 0:w]
        self.yy, self.xx = yy, xx

    def disc(self, x: float, y: float, r: float, color) -> None:
        x0, x1 = int(max(x - r - 1, 0)), int(min(x + r + 2, self.a.shape[1]))
        y0, y1 = int(max(y - r - 1, 0)), int(min(y + r + 2, self.a.shape[0]))
        if x0 >= x1 or y0 >= y1:
            return
        m = (self.xx[y0:y1, x0:x1] - x) ** 2 + (self.yy[y0:y1, x0:x1] - y) ** 2 <= r * r
        self.a[y0:y1, x0:x1][m] = color

    def ring(self, x: float, y: float, r: float, width: float, color) -> None:
        x0, x1 = int(max(x - r - 1, 0)), int(min(x + r + 2, self.a.shape[1]))
        y0, y1 = int(max(y - r - 1, 0)), int(min(y + r + 2, self.a.shape[0]))
        if x0 >= x1 or y0 >= y1:
            return
        d2 = (self.xx[y0:y1, x0:x1] - x) ** 2 + (self.yy[y0:y1, x0:x1] - y) ** 2
        m = (d2 <= r * r) & (d2 >= (r - width) ** 2)
        self.a[y0:y1, x0:x1][m] = color

    def _box(self, x0: float, y0: float, x1: float, y1: float) -> tuple[slice, slice] | None:
        """The pixel slices of a box clipped to the canvas, or None if it's off the canvas
        (a negative end would otherwise count from the far edge and paint a whole bar)."""
        h, w = self.a.shape[:2]
        ya, yb = int(np.clip(y0, 0, h)), int(np.clip(y1, 0, h))
        xa, xb = int(np.clip(x0, 0, w)), int(np.clip(x1, 0, w))
        if ya >= yb or xa >= xb:
            return None
        return slice(ya, yb), slice(xa, xb)

    def square(self, x: float, y: float, r: float, color) -> None:
        box = self._box(x - r, y - r, x + r + 1, y + r + 1)
        if box:
            self.a[box] = color

    def line(self, x0, y0, x1, y1, color, width: float = 1.5) -> None:
        n = int(max(abs(x1 - x0), abs(y1 - y0)) / 0.7) + 1
        for t in np.linspace(0, 1, n):
            self.disc(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, width / 2, color)

    def polyline(self, pts: np.ndarray, color, width: float = 1.5) -> None:
        for (xa, ya), (xb, yb) in zip(pts[:-1], pts[1:], strict=True):
            if np.isfinite([xa, ya, xb, yb]).all():
                self.line(xa, ya, xb, yb, color, width)

    def text(self, s: str, x: float, y: float, color, px: int = 2) -> None:
        """Digits centered on (x, y), each 3 x 5 cells of ``px`` pixels."""
        w = len(s) * 4 * px - px
        cx, cy = int(x - w / 2), int(y - 5 * px / 2)
        for k, ch in enumerate(s):
            g = _GLYPHS.get(ch)
            if g is None:
                continue
            for idx, bit in enumerate(g):
                if bit == "1":
                    r, c = divmod(idx, 3)
                    yy, xx = cy + r * px, cx + k * 4 * px + c * px
                    box = self._box(xx, yy, xx + px, yy + px)
                    if box:
                        self.a[box] = color

    def png(self) -> bytes:
        h, w, _ = self.a.shape
        raw = b"".join(b"\x00" + self.a[r].tobytes() for r in range(h))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(
                ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)  # fmt: skip

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))  # fmt: skip


def _to_panel(xy: np.ndarray, hoop: int, ox: int) -> np.ndarray:
    """Court feet -> panel pixels: the attacked hoop at the bottom, depth from its baseline."""
    xy = np.asarray(xy, float)
    depth = xy[..., 0] if hoop == 0 else 94 - xy[..., 0]
    lateral = xy[..., 1] if hoop == 0 else 50 - xy[..., 1]
    return np.stack([ox + lateral * SCALE, MARGIN + (47 - depth) * SCALE], axis=-1)


def _court(c: Canvas, ox: int) -> None:
    c.a[MARGIN : MARGIN + PANEL_H, ox : ox + PANEL_W] = COURT

    def P(lat, dep):
        return ox + lat * SCALE, MARGIN + (47 - dep) * SCALE

    def seg(a, b):
        c.line(*P(*a), *P(*b), LINES, 2)

    for a, b in [((0, 0), (50, 0)), ((0, 0), (0, 47)), ((50, 0), (50, 47)), ((0, 47), (50, 47)),
                 ((17, 0), (17, 19)), ((33, 0), (33, 19)), ((17, 19), (33, 19)),
                 ((3, 0), (3, 14)), ((47, 0), (47, 14)), ((22, 4), (28, 4))]:  # fmt: skip
        seg(a, b)
    ang = np.linspace(-np.arcsin(22 / 23.75), np.arcsin(22 / 23.75), 60)
    arc = np.stack([25 + 23.75 * np.sin(ang), 5.25 + 23.75 * np.cos(ang)], 1)
    c.polyline(np.array([P(*q) for q in arc]), LINES, 2)
    t = np.linspace(0, 2 * np.pi, 60)
    c.polyline(np.array([P(25 + 6 * np.cos(a), 19 + 6 * np.sin(a)) for a in t]), LINES, 2)
    c.ring(*P(25, 5.25), 0.9 * SCALE, 2, (60, 60, 60))


def strip(frames: pd.DataFrame, jerseys: dict[int, str], times: list[int], hoop: int,
          side: int, handler: int | None = None, screener: int | None = None,
          trail_from: int | None = None) -> bytes:  # fmt: skip
    """A PNG of court panels at frame positions ``times`` (row positions in ``frames``), each
    with every player's trail since the previous panel. ``side`` is the team on offense at
    the moment in question (0 home, 1 away): its colors hold for the whole strip."""
    from courtvision.tracking.sportvu import SLOTS

    xy = np.stack([frames[[f"{s}_x", f"{s}_y"]].to_numpy(float) for s in SLOTS], axis=1)
    ids = frames[[f"{s}_id" for s in SLOTS]].to_numpy()
    ball = frames[["ball_x", "ball_y"]].to_numpy(float)
    c = Canvas(len(times) * (PANEL_W + MARGIN) + MARGIN, PANEL_H + 2 * MARGIN)
    prev = trail_from if trail_from is not None else max(times[0] - FPS, 0)
    for k, t in enumerate(times):
        ox = MARGIN + k * (PANEL_W + MARGIN)
        _court(c, ox)
        lo = max(prev, 0)
        for j in range(10):
            same = ids[lo : t + 1, j] == ids[t, j]
            pts = _to_panel(xy[lo : t + 1, j][same], hoop, ox)
            c.polyline(pts, TRAIL_O if j // 5 == side else TRAIL_D, 1.5)
        c.polyline(_to_panel(ball[lo : t + 1], hoop, ox), BALL, 1.0)
        for j in range(10):
            p = _to_panel(xy[t, j], hoop, ox)
            if not np.isfinite(p).all():
                continue
            is_off = (j // 5) == side
            col = OFFENSE if is_off else DEFENSE
            pid = int(ids[t, j])
            r = 1.35 * SCALE
            if pid == screener:
                c.square(*p, r, col)
            else:
                c.disc(*p, r, col)
            if pid == handler:
                c.ring(*p, r + 4, 3, RING)
            c.text(jerseys.get(pid, ""), *p, WHITE, 2)
        b = _to_panel(ball[t], hoop, ox)
        if np.isfinite(b).all():
            c.disc(*b, 4, BALL)
            c.ring(*b, 4.5, 1.2, (90, 60, 20))
        prev = t
    return c.png()


def draw(events: pd.DataFrame | None = None) -> int:
    """Render every sampled event to ``data/tracking/labels/png/<event>.png``."""
    from courtvision.tracking import defense, detect

    ev = pd.read_parquet(labels_dir() / "sample.parquet") if events is None else events
    out = labels_dir() / "png"
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for gid, grp in ev.groupby("game_id"):
        frames = sportvu.load_frames(gid)
        roster = pd.read_parquet(sportvu.frames_dir().parent / "rosters" / f"{gid}.parquet")
        jerseys = {int(r.person_id): str(r.jersey) for r in roster.itertuples(index=False)}
        f = defense.Floor(detect.Game.from_frames(frames))
        for r in grp.itertuples(index=False):
            if r.kind == "screen":
                i = int(r.frame)
                hoop = int(f.hoop[i])
                times = [i - int(1.5 * FPS), i, i + int(1.5 * FPS), i + 3 * FPS]
                times = [min(max(t, 0), f.n - 1) for t in times]
                png = strip(frames, jerseys, times, hoop, int(f.poss[i]), int(r.handler),
                            int(r.screener), trail_from=times[0] - FPS)  # fmt: skip
            else:
                s0, e0 = int(r.start), int(r.end)
                hoop = int(f.hoop[s0])
                times = [s0, (s0 + e0) // 2, e0]
                png = strip(frames, jerseys, times, hoop, int(f.poss[s0]), trail_from=s0)
            (out / f"{r.event}.png").write_bytes(png)
            n += 1
    return n


# --------------------------------------------------------------------------- scoring


def read_labels(path: Path | None = None) -> pd.DataFrame:
    path = path or labels_file()
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return df.apply(lambda c: c.str.strip().str.lower())


def evaluate(path: Path | None = None, half: str | None = None, write: bool = True) -> dict:
    """Detector vs labels. Each stratum's events count by how many it stands for (its size
    over how many were labeled), so rates are for the whole pool, not the sample.

    The detector's calls are today's (``screens.parquet``), not the ones it made when the
    sample was drawn, so a revised rule is scored as it now stands. ``half``: "dev" (odd
    event numbers: rules may be revised looking at these) or "test" (even: never looked at
    while revising)."""
    ev = pd.read_parquet(labels_dir() / "sample.parquet")
    # the pool's own coverage and route columns would clash with the labels'
    ev = ev.drop(columns=["coverage", "route", "called"])
    ev = ev.merge(_current_calls(), on=["game_id", "frame"], how="left")
    ev["called"] = ev.called.fillna("none")
    lab = read_labels(path)
    m = ev.merge(lab, on="event", how="inner")
    if half:
        odd = m.event.str[1:].astype(int) % 2 == 1
        m = m[odd if half == "dev" else ~odd]
    out: dict = {"labeled": len(m), "half": half or "all"}

    sc = m[m.kind == "screen"].copy()
    n_by = sc.groupby("stratum").size()
    sc["w"] = sc.stratum_size / sc.stratum.map(n_by)
    sure = sc[sc.screen.isin(["yes", "no"])]
    is_screen = sure.screen == "yes"
    found = sure.stratum != "rejected"
    tp = float(sure.w[is_screen & found].sum())
    fp = float(sure.w[~is_screen & found].sum())
    fn = float(sure.w[is_screen & ~found].sum())
    out["screens"] = {"labeled": len(sc), "unclear": int((~sc.screen.isin(["yes", "no"])).sum()),
                      "precision": round(tp / max(tp + fp, 1e-9), 3),
                      "recall_in_pool": round(tp / max(tp + fn, 1e-9), 3),
                      "precision_unweighted": round(float(is_screen[found].mean()), 3)}  # fmt: skip

    # coverage, on labeled screens the detector found, with a clear coverage label
    cov = sc[(sc.stratum != "rejected") & (sc.screen == "yes")
             & sc.coverage.isin(["switch", "blitz", "ice", "show", "drop"])]  # fmt: skip
    table = pd.crosstab(cov.called, cov.coverage)
    out["coverage_confusion_unweighted"] = table.to_dict()
    per = {}
    for c in ("switch", "blitz", "ice", "show", "drop"):
        called = cov[cov.called == c]
        prec = float((called.coverage == c).mean()) if len(called) else np.nan
        truth = cov[cov.coverage == c]
        rec = float((truth.w * (truth.called == c)).sum() / truth.w.sum()) if len(truth) else np.nan
        per[c] = {"called": len(called), "precision": round(prec, 3),
                  "labeled_as": len(truth), "recall": round(rec, 3)}  # fmt: skip
    out["coverage"] = per
    agree = cov.called == cov.coverage
    out["coverage_accuracy_weighted"] = round(float((cov.w * agree).sum() / cov.w.sum()), 3)
    rt = sc[(sc.stratum != "rejected") & (sc.screen == "yes") & sc.route.isin(["over", "under"])]
    rt = rt[rt.called_route.isin(["over", "under"])]
    out["route"] = {"n": len(rt), "agree": round(float((rt.route == rt.called_route).mean()), 3)}

    po = m[m.kind == "possession"]
    po = po.assign(label=po.note.where(po.note.isin(["man", "zone", "unclear"]), "unclear"))
    zc = po[po.stratum == "zone"]
    mc = po[po.stratum == "man"]
    out["zone"] = {"called_zone": len(zc),
                   "labeled_zone_of_called_zone": int((zc.label == "zone").sum()),
                   "labeled_man_of_called_zone": int((zc.label == "man").sum()),
                   "called_man": len(mc),
                   "labeled_zone_of_called_man": int((mc.label == "zone").sum()),
                   "labeled_man_of_called_man": int((mc.label == "man").sum())}  # fmt: skip
    if write:
        name = f"evaluation_{half}.json" if half else "evaluation.json"
        (labels_dir() / name).write_text(json.dumps(out, indent=1, default=str))
    return out


def _current_calls() -> pd.DataFrame:
    """The strict detector's coverage and route for each pool event (within half a second)."""
    d = get_paths().data / "tracking" / "defense"
    s = pd.read_parquet(d / "screens.parquet", columns=["game_id", "frame", "coverage", "route"])
    pool = pd.read_parquet(d / "pool.parquet", columns=["game_id", "frame"])
    m = pool.merge(s.rename(columns={"frame": "sframe", "coverage": "called",
                                     "route": "called_route"}), on="game_id")  # fmt: skip
    m = m[(m.frame - m.sframe).abs() <= 12]
    return m.drop_duplicates(["game_id", "frame"])[["game_id", "frame", "called", "called_route"]]


# --------------------------------------------------------------------------- your spot check

SPOT = {"no": 6, "drop": 6, "show": 5, "switch": 5, "blitz": 4, "ice": 4}


def spot_check(seed: int = 11) -> Path:
    """A local page of 30 labeled screens to check by eye (``SPOT`` per label; short classes
    are filled from the rest), each a looping replay from 2 s before to 3.5 s after."""
    from courtvision.tracking import defense, detect
    from courtvision.tracking.sportvu import SLOTS

    ev = pd.read_parquet(labels_dir() / "sample.parquet").drop(columns=["coverage", "route"])
    lab = read_labels()
    sc = ev[ev.kind == "screen"].merge(lab, on="event")
    sc = sc[sc.confidence.isin(["high", "medium"])]
    sc["cls"] = np.where(sc.screen == "no", "no", sc.coverage)
    rng = np.random.default_rng(seed)
    picks, used = [], set()
    for cls, k in SPOT.items():
        src = sc[(sc.cls == cls) & ~sc.event.isin(used)]
        take = src.iloc[rng.permutation(len(src))[:k]]
        picks.append(take)
        used |= set(take.event)
    short = 30 - sum(len(p) for p in picks)
    if short > 0:
        rest = sc[~sc.event.isin(used) & (sc.screen == "yes")]
        picks.append(rest.iloc[rng.permutation(len(rest))[:short]])
    chosen = pd.concat(picks).sort_values("event")
    items = []
    for gid, grp in chosen.groupby("game_id"):
        frames = sportvu.load_frames(gid)
        roster = pd.read_parquet(sportvu.frames_dir().parent / "rosters" / f"{gid}.parquet")
        jerseys = {int(r.person_id): str(r.jersey) for r in roster.itertuples(index=False)}
        f = defense.Floor(detect.Game.from_frames(frames))
        xy = np.stack([frames[[f"{s}_x", f"{s}_y"]].to_numpy(float) for s in SLOTS], axis=1)
        ids = frames[[f"{s}_id" for s in SLOTS]].to_numpy()
        ball = frames[["ball_x", "ball_y"]].to_numpy(float)
        for r in grp.itertuples(index=False):
            i = int(r.frame)
            lo, hi = max(i - 2 * FPS, 0), min(i + int(3.5 * FPS), f.n - 1)
            rows = []
            for k in range(lo, hi + 1):
                v = np.concatenate([ball[k], xy[k].reshape(-1)])
                rows.append([None if not np.isfinite(x) else int(round(x * 10)) for x in v])
            items.append({
                "event": r.event, "hoop": int(f.hoop[i]), "side": int(f.poss[i]),
                "handler": int(r.handler), "screener": int(r.screener), "at": i - lo,
                "ids": [int(x) for x in ids[i]],
                "jerseys": [jerseys.get(int(x), "") for x in ids[i]],
                "label": {"screen": r.screen, "coverage": r.coverage, "route": r.route},
                "frames": rows,
            })  # fmt: skip
    items.sort(key=lambda it: it["event"])
    page = (Path(__file__).with_name("spot_check.html")).read_text(encoding="utf-8")
    out = labels_dir() / "spot_check.html"
    out.write_text(page.replace("__DATA__", json.dumps({"key": "t3-v1", "items": items})),
                   encoding="utf-8")  # fmt: skip
    return out
