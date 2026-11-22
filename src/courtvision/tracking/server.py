"""The Live view (decision D7): a small local web page fed by the engine over a WebSocket.

``courtvision tracking live GAME_ID`` serves http://127.0.0.1:8770/. It replays a recorded
game at 25 frames a second (or faster), stepping the engine one frame at a time and pushing
the state to the page: the court, the score, the possession's expected value, and the win
probability from score and clock and with the film. The page can play, pause, change speed
and jump to any moment.

Two things on the page come from the batch tables, at the moment they would be known: a
shot's chance of going in (T2) at its release, and a screen's coverage (T3) 3 s after the
screen, when the detector's look at what the defenders did is complete.

Standard library only: asyncio and a minimal RFC 6455 WebSocket. It listens on 127.0.0.1,
answers only pages from that address, and nothing leaves this machine (decision D1).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import struct
import time
from pathlib import Path

import numpy as np
import pandas as pd

from courtvision.tracking import engine, replay
from courtvision.tracking.detect import FPS

log = logging.getLogger(__name__)

PORT = 8770
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TICK = 1 / FPS  # s between pushes to the page
GAP_MS = 1000  # a jump in the timestamps longer than this (a dead ball) is skipped, not waited
COVERAGE_DELAY = 3 * FPS  # frames: the T3 coverage window closes 3 s after the screen
SPEEDS = (0.5, 1, 2, 4, 8, 16)


# --------------------------------------------------------------------------- WebSocket


def accept_key(key: str) -> str:
    """The handshake answer for a client's Sec-WebSocket-Key (RFC 6455, section 4.2.2)."""
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def encode(payload: str | bytes, opcode: int = 0x1) -> bytes:
    """One unmasked, final frame from the server."""
    data = payload.encode() if isinstance(payload, str) else payload
    n = len(data)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 1 << 16:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + data


async def read_message(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """One frame from the client (clients always mask): (opcode, payload)."""
    b0, b1 = await reader.readexactly(2)
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", await reader.readexactly(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", await reader.readexactly(8))
    if n > 1 << 20:
        raise ValueError("message too large")
    mask = await reader.readexactly(4) if b1 & 0x80 else b"\0\0\0\0"
    data = bytearray(await reader.readexactly(n))
    for k in range(n):
        data[k] ^= mask[k % 4]
    return b0 & 0x0F, bytes(data)


# --------------------------------------------------------------------------- one game


class LiveGame:
    """A recorded game, its models, and what the page needs to draw it."""

    def __init__(self, game_id: str) -> None:
        from courtvision.config import get_paths

        self.game_id = game_id
        self.replay = engine.Replay(game_id)
        self.models = engine.models_for(game_id)
        self.hist = engine.history(self.replay, self.models)
        r, g = self.replay, self.replay.g
        data = get_paths().data / "tracking"
        roster = pd.read_parquet(data / "rosters" / f"{game_id}.parquet")
        self.players = {int(p.person_id): {"name": f"{p.first_name} {p.last_name}",
                                           "jersey": str(p.jersey),
                                           "team": 0 if p.team_id == r.home_id else 1}
                        for p in roster.itertuples(index=False)}  # fmt: skip
        self.teams = [replay._team_abbr(r.home_id), replay._team_abbr(r.away_id)]
        per = pd.Series(np.arange(r.n)).groupby(g.period)
        self.periods = [{"p": int(p), "start": int(s.min()), "end": int(s.max())}
                        for p, s in per]  # fmt: skip
        self.events = self._events(data)
        pp = g.period.astype(np.float64)
        self.elapsed = np.where(pp <= 4, (pp - 1) * 720 + (720 - g.clock),
                                2880 + (pp - 5) * 300 + (300 - g.clock))  # fmt: skip

    def _events(self, data: Path) -> dict[int, list[dict]]:
        """Shots (at the release, then the result at the rim) and screens (3 s later), keyed
        by the frame they become known."""
        out: dict[int, list[dict]] = {}
        n = self.replay.n
        sh = pd.read_parquet(data / "shots" / f"{self.game_id}.parquet")
        sqp = data / "shot_quality.parquet"
        if sqp.exists():
            q = pd.read_parquet(sqp, columns=["game_id", "action_number", "xfg", "closest_def"],
                                filters=[("game_id", "==", self.game_id)])  # fmt: skip
            sh = sh.merge(q, left_on="actionNumber", right_on="action_number", how="left")
        for s in sh.itertuples(index=False):
            r = int(s.release)
            if not 0 <= r < n:
                continue
            xfg = getattr(s, "xfg", np.nan)
            cd = getattr(s, "closest_def", np.nan)
            out.setdefault(r, []).append(
                {
                    "k": "shot",
                    "pid": int(s.person_id),
                    "three": "3PT" in str(s.description),
                    "xfg": None if pd.isna(xfg) else round(float(xfg), 3),
                    "def": None if pd.isna(cd) else round(float(cd), 1),
                    "id": int(s.actionNumber),
                }
            )
            rim = int(s.end) if np.isfinite(s.end) else r + FPS
            out.setdefault(min(rim + 12, n - 1), []).append(
                {"k": "result", "id": int(s.actionNumber), "made": s.shotResult == "Made"}
            )
        scp = data / "defense" / "screens.parquet"
        if scp.exists():
            sc = pd.read_parquet(scp, filters=[("game_id", "==", self.game_id)])
            for s in sc.itertuples(index=False):
                k = int(s.frame) + COVERAGE_DELAY
                if k < n:
                    out.setdefault(k, []).append({
                        "k": "screen", "coverage": s.coverage, "route": s.route,
                        "action": s.screener_action, "handler": int(s.handler),
                        "screener": int(s.screener), "team": int(s.offense)})  # fmt: skip
        g = self.replay.g
        for k, evs in out.items():  # when, on the game clock
            for e in evs:
                e["p"], e["gc"] = int(g.period[k]), round(float(g.clock[k]), 1)
        return out

    def hello(self) -> dict:
        rep = {}
        p = engine.winprob.out_dir() / "winprob_report.json"
        if p.exists():
            r = json.loads(p.read_text())
            rep = {"live_model": r.get("live_model"), "test_games": r.get("test_games")}
        return {"t": "hello", "game_id": self.game_id, "teams": self.teams,
                "players": self.players, "n": self.replay.n, "periods": self.periods,
                "pregame": round(self.models["pregame"], 1), "report": rep,
                "vbar": round(self.models["epv"].vbar, 3),
                "budget_ms": engine.BUDGET_MS, "speeds": SPEEDS}  # fmt: skip

    def chart_until(self, i: int) -> list[list]:
        h = self.hist[self.hist.frame < i]
        el = self.elapsed[h.frame.to_numpy()]
        return [[int(f), round(float(e), 1), round(float(w), 4), round(float(b), 4)]
                for f, e, w, b in zip(h.frame, el, h.wp, h.wp_base, strict=True)]  # fmt: skip


# --------------------------------------------------------------------------- one viewer


class Session:
    """One page's replay: its own engine, position, speed."""

    def __init__(self, game: LiveGame, writer: asyncio.StreamWriter) -> None:
        self.game, self.writer = game, writer
        self.eng = engine.make_engine(game.models)
        self.i = 0  # next frame to step
        self.playing = False
        self.speed = 1.0
        self.rt: float | None = None  # replay clock (ms of frame time)
        self.wall = 0.0  # when the replay clock last moved
        self.ids: tuple = ()
        self.times: list[float] = []

    async def send(self, obj: dict | str) -> None:
        self.writer.write(encode(obj if isinstance(obj, str) else json.dumps(obj)))
        await self.writer.drain()

    def seek(self, i: int) -> None:
        r = self.game.replay
        i = int(np.clip(i, 0, r.n - 1))
        self.eng = engine.make_engine(self.game.models)
        if i > 0:
            self.eng.resume(r.g, r.st, i - 1, r.score_after(i - 1))
        self.i, self.rt, self.ids = i, None, ()

    def recent_events(self, i: int, k: int = 12) -> list[dict]:
        ev = []
        for f in sorted(x for x in self.game.events if x < i)[-40:]:
            for e in self.game.events[f]:
                ev.append({**e, "i": f})
        return ev[-k:]

    def advance(self) -> tuple[list[tuple], list[dict]]:
        """Step every frame that is due by the replay clock; return (stepped outputs,
        events that became known)."""
        r = self.game.replay
        if self.i >= r.n:
            self.playing = False
            return [], []
        ts = r.g.ts
        now = time.perf_counter()
        if self.rt is None:
            self.rt, self.wall = float(ts[self.i]), now
        # the replay clock follows the wall clock (timer ticks on Windows run long)
        self.rt += (now - self.wall) * 1000 * self.speed
        self.wall = now
        if ts[self.i] - self.rt > GAP_MS:  # dead ball: jump to the next frame
            self.rt = float(ts[self.i])
        outs, events = [], []
        cap = int(FPS * max(self.speed, 1) * 2)
        while self.i < r.n and ts[self.i] <= self.rt and len(outs) < cap:
            i = self.i
            f = r.frame(i)
            t0 = time.perf_counter()
            for side, pts in r.scores.get(i, ()):
                self.eng.add_points(side, pts)
                events.append({"k": "score", "side": side, "pts": pts, "i": i,
                               "p": int(f.period), "gc": round(float(f.clock), 1)})  # fmt: skip
            out = self.eng.step(f)
            self.times.append((time.perf_counter() - t0) * 1000)
            events += [{**e, "i": i} for e in self.game.events.get(i, ())]
            outs.append((out, f))
            self.i += 1
        if len(outs) == cap:  # fell behind (very high speed): don't build up a backlog
            self.rt = float(ts[min(self.i, r.n - 1)])
        self.times = self.times[-500:]
        return outs, events

    def frame_message(self, outs: list[tuple], events: list[dict]) -> dict:
        out, f = outs[-1]
        msg = json.loads(engine.message(out, f))
        ids = tuple(int(x) for x in f.ids)
        if ids != self.ids:
            msg["ids"] = list(ids)
            self.ids = ids
        el = self.game.elapsed
        msg["c"] = [[o["i"], round(float(el[o["i"]]), 1), round(o["wp"], 4),
                     round(o["wp_base"], 4)] for o, _ in outs if o["i"] % FPS == 0]  # fmt: skip
        msg["ev"] = events
        t = np.asarray(self.times) if self.times else np.zeros(1)
        msg["ms"] = {"p50": round(float(np.median(t)), 2), "max": round(float(t.max()), 2)}
        msg["sent"] = time.time() * 1000
        return msg

    async def reset_message(self) -> None:
        r = self.game.replay
        i = self.i
        j = max(i - 1, 0)
        await self.send({"t": "reset", "i": i, "chart": self.game.chart_until(i),
                         "events": self.recent_events(i), "s": r.score_after(j)})  # fmt: skip

    async def command(self, raw: bytes) -> None:
        try:
            c = json.loads(raw)
        except ValueError:
            return
        cmd = c.get("cmd")
        if cmd == "play":
            self.playing, self.rt = True, None
        elif cmd == "pause":
            self.playing = False
        elif cmd == "speed" and float(c.get("v", 1)) in SPEEDS:
            self.speed = float(c["v"])
        elif cmd in ("seek", "seek_clock"):
            if cmd == "seek_clock":  # the first frame of a period at or under a clock time
                g = self.game.replay.g
                at = np.flatnonzero((g.period == int(c.get("p", 4)))
                                    & (g.clock <= float(c.get("clock", 120))))  # fmt: skip
                if not len(at):
                    return
                c["i"] = int(at[0])
            self.seek(int(c.get("i", 0)))
            await self.reset_message()
            if not self.playing:  # show the frame jumped to
                outs, ev = self._one()
                if outs:
                    await self.send(self.frame_message(outs, ev))

    def _one(self) -> tuple[list[tuple], list[dict]]:
        r = self.game.replay
        if self.i >= r.n:
            return [], []
        f = r.frame(self.i)
        out = self.eng.step(f)
        self.i += 1
        return [(out, f)], []

    async def run(self, reader: asyncio.StreamReader) -> None:
        await self.send(self.game.hello())
        await self.reset_message()
        outs, ev = self._one()
        await self.send(self.frame_message(outs, ev))

        async def listen():
            while True:
                op, data = await read_message(reader)
                if op == 0x8:  # close
                    self.writer.write(encode(b"", 0x8))
                    return
                if op == 0x9:  # ping
                    self.writer.write(encode(data, 0xA))
                elif op == 0x1:
                    await self.command(data)

        task = asyncio.create_task(listen())
        try:
            while not task.done():
                t0 = time.perf_counter()
                if self.playing:
                    outs, ev = self.advance()
                    if outs:
                        await self.send(self.frame_message(outs, ev))
                    if not self.playing:
                        await self.send({"t": "end"})
                await asyncio.sleep(max(TICK - (time.perf_counter() - t0), 0.001))
        finally:
            task.cancel()


# --------------------------------------------------------------------------- HTTP


def page(game: LiveGame) -> bytes:
    html = Path(__file__).with_name("live.html").read_text(encoding="utf-8")
    title = f"{game.teams[1]} at {game.teams[0]}"
    return html.replace("__TITLE__", title).encode()


def _allowed(headers: dict, port: int) -> bool:
    """Only this machine: the Host must be localhost, and a page opening the socket must
    come from it too (no other site can drive the replay)."""
    ok = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if headers.get("host") not in ok:
        return False
    origin = headers.get("origin")
    return origin is None or origin.removeprefix("http://") in ok


async def handle(game: LiveGame, port: int, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:  # fmt: skip
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = (lines[0].split(" ") + ["", "", ""])[:3]
        headers = {k.strip().lower(): v.strip()
                   for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}  # fmt: skip
        if method != "GET" or not _allowed(headers, port):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        elif path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
            key = headers.get("sec-websocket-key", "")
            writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n").encode())  # fmt: skip
            await writer.drain()
            await Session(game, writer).run(reader)
        elif path in ("/", "/index.html"):
            body = page(game)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                         b"Cache-Control: no-store\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)  # fmt: skip
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError, ValueError):
        pass
    finally:
        writer.close()


async def _serve(game_id: str, port: int, ready=None) -> None:
    game = LiveGame(game_id)
    server = await asyncio.start_server(lambda r, w: handle(game, port, r, w), "127.0.0.1", port)
    log.info("live view: http://127.0.0.1:%d/", port)
    print(f"Live view of {game.teams[1]} at {game.teams[0]}: http://127.0.0.1:{port}/", flush=True)
    if ready:
        ready()
    async with server:
        await server.serve_forever()


def serve(game_id: str, port: int = PORT, open_browser: bool = False) -> None:
    """Serve the Live view until stopped (Ctrl+C)."""

    def opened():
        if open_browser:
            import webbrowser

            webbrowser.open(f"http://127.0.0.1:{port}/")

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(game_id, port, opened))
