// Half-court shot maps. Coordinates are the play-by-play x_legacy / y_legacy system: tenths of a
// foot, rim at the origin, +y toward half court (as in courtvision/app/court.py, which this
// ports). The league baseline per hexagon comes precomputed (web.league_hex, from the same
// court.py code); a unit's shots are binned here with the same geometry and rounding.
import { ReactNode } from "react";
import { Row } from "../db";
import { div, fmt } from "../lib/format";
import { useTip } from "./kit";

export const X_MIN = -250, X_MAX = 250, Y_MIN = -47.5, Y_TOP = 300;
export const HEX_R = 17.0; // hexagon radius, tenths of a foot (~1.7 ft)
const K_ZONE = 150, K_HEX = 60; // shots of prior: zone toward league, hexagon toward its zone
export const PP100_SCALE = 20; // points per 100 shots vs league at full colour
const FULL_SHARE = 0.016; // a hexagon with this share of the shots is drawn full size
const SQ3 = Math.sqrt(3);

export const ZONES = ["RA", "PAINT", "MID", "C3", "ATB3"] as const;
export const ZONE_LABELS: Record<string, string> = {
  RA: "Restricted area", PAINT: "Paint (non-RA)", MID: "Mid-range", C3: "Corner 3", ATB3: "Above-the-break 3",
};
export const ZONE_KEY: Record<string, string> = { RA: "ra", PAINT: "pnt", MID: "mid", C3: "c3", ATB3: "atb3" };

/** numpy's round (half to even), so hexagons match the Python baseline exactly */
const rint = (v: number) => {
  const f = Math.floor(v), d = v - f;
  if (d > 0.5) return f + 1;
  if (d < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
};

/** Axial coordinates (q, r) of the pointy-top hexagon containing (x, y). */
export function hexOf(x: number, y: number): [number, number] {
  const cx = ((SQ3 / 3) * x - y / 3) / HEX_R, cz = ((2 / 3) * y) / HEX_R, cy = -cx - cz;
  let rx = rint(cx), rz = rint(cz);
  const ry = rint(cy);
  const dx = Math.abs(rx - cx), dy = Math.abs(ry - cy), dz = Math.abs(rz - cz);
  const fixX = dx > dy && dx > dz;
  const fixZ = !fixX && dz >= dy;
  if (fixX) rx = -ry - rz;
  if (fixZ) rz = -rx - ry;
  return [rx, rz];
}
export const hexCenter = (q: number, r: number): [number, number] => [HEX_R * SQ3 * (q + r / 2), HEX_R * 1.5 * r];

export function zoneOf(v: number, x: number, y: number) {
  if (v === 3 && Math.abs(x) >= 220 && y <= 87) return "C3";
  if (v === 3) return "ATB3";
  if (Math.hypot(x, y) < 40) return "RA";
  if (Math.abs(x) <= 80 && y <= 138) return "PAINT";
  return "MID";
}

export type Hex = { q: number; rr: number; n: number; share: number; fg: number; lg_fg: number; diff: number; zone: string;
  zone_diff: number; cx: number; cy: number };

const mode = (xs: string[]) => {
  const c = new Map<string, number>();
  xs.forEach((x) => c.set(x, (c.get(x) ?? 0) + 1));
  return [...c.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? "MID";
};

/** One unit's shots against the league from the same spots (port of court.unit_hex): per
 * hexagon, weighted attempts, share, FG%, and points per 100 shots vs league (``diff``), shrunk
 * toward the unit's zone and the zone toward league average. ``shots``: season, x, y, made,
 * shot_value, optional w; ``league``: web.league_hex rows for the seasons involved. */
export function unitHex(shots: Row[], league: Row[]): Hex[] {
  const base = new Map(league.map((l) => [`${l.season}|${l.q}|${l.rr}`, l]));
  const s = shots.filter((r) => r.x != null && r.y != null && r.y <= 330 && r.y >= -50);
  if (!s.length) return [];
  const meanMade = s.reduce((a, r) => a + Number(r.made), 0) / s.length;
  const zones = new Map<string, { n: number; ex: number }>();
  const hexes = new Map<string, { q: number; rr: number; n: number; ex: number; made: number; lgm: number; zones: string[] }>();
  for (const r of s) {
    const [q, rr] = hexOf(r.x, r.y);
    const lg = base.get(`${r.season}|${q}|${rr}`);
    const v = Number(r.shot_value), made = Number(r.made), w = r.w ?? 1;
    const lgPps = lg?.lg_pps ?? meanMade * v, lgFg = lg?.lg_fg ?? meanMade;
    const zone = lg?.zone ?? zoneOf(v, r.x, r.y);
    const ex = w * (made * v - lgPps);
    const z = zones.get(zone) ?? { n: 0, ex: 0 };
    z.n += w; z.ex += ex; zones.set(zone, z);
    const k = `${q}|${rr}`;
    const h = hexes.get(k) ?? { q, rr, n: 0, ex: 0, made: 0, lgm: 0, zones: [] };
    h.n += w; h.ex += ex; h.made += w * made; h.lgm += w * lgFg; h.zones.push(zone);
    hexes.set(k, h);
  }
  const total = [...hexes.values()].reduce((a, h) => a + h.n, 0);
  return [...hexes.values()].map((h) => {
    const zone = mode(h.zones);
    const z = zones.get(zone);
    const zone_diff = z ? z.ex / (z.n + K_ZONE) : 0;
    const [cx, cy] = hexCenter(h.q, h.rr);
    return { q: h.q, rr: h.rr, n: h.n, share: h.n / total, fg: h.made / h.n, lg_fg: h.lgm / h.n,
             diff: (100 * (h.ex + K_HEX * zone_diff)) / (h.n + K_HEX), zone, zone_diff, cx, cy };
  });
}

// ---- drawing ----------------------------------------------------------------------------

const sy = (y: number) => Y_TOP - y; // data y (up) -> svg y (down)
const arc = (r: number, a0: number, a1: number, n = 60) =>
  Array.from({ length: n }, (_, i) => {
    const a = ((a0 + ((a1 - a0) * i) / (n - 1)) * Math.PI) / 180;
    return [r * Math.cos(a), r * Math.sin(a)];
  });
const path = (pts: number[][]) => "M " + pts.map(([x, y]) => `${x.toFixed(1)},${sy(y).toFixed(1)}`).join(" L ");
const ARC_A = (Math.asin(87 / 237.5) * 180) / Math.PI;

export function CourtLines() {
  const st = { fill: "none", stroke: "var(--court-line)", strokeWidth: 1.5 };
  return (
    <g>
      <rect x={X_MIN} y={sy(Y_TOP + 5)} width={X_MAX - X_MIN} height={Y_TOP + 5 - Y_MIN} {...st} />
      <circle cx={0} cy={sy(0)} r={7.5} {...st} />
      <line x1={-30} x2={30} y1={sy(-7.5)} y2={sy(-7.5)} {...st} />
      <rect x={-80} y={sy(142.5)} width={160} height={142.5 - Y_MIN} {...st} />
      <circle cx={0} cy={sy(142.5)} r={60} {...st} />
      <path d={path(arc(40, 0, 180))} {...st} />
      <line x1={-220} x2={-220} y1={sy(Y_MIN)} y2={sy(87)} {...st} />
      <line x1={220} x2={220} y1={sy(Y_MIN)} y2={sy(87)} {...st} />
      <path d={path(arc(237.5, ARC_A, 180 - ARC_A))} {...st} />
    </g>
  );
}

const hexPts = (cx: number, cy: number, k: number) =>
  [30, 90, 150, 210, 270, 330].map((a) => {
    const t = (a * Math.PI) / 180;
    return `${(cx + HEX_R * k * Math.cos(t)).toFixed(1)},${sy(cy + HEX_R * k * Math.sin(t)).toFixed(1)}`;
  }).join(" ");

/** Hexagon shot map: area follows the share of shots, colour the points per 100 shots vs
 * league from the same spots (blue good for the unit, red bad, grey average). */
export function HexCourt({ hexes, value = (h) => h.diff, tip, minShare = 0.0015, scale = PP100_SCALE, maxWidth = 520 }: {
  hexes: Hex[]; value?: (h: Hex) => number; tip?: (h: Hex) => ReactNode; minShare?: number; scale?: number; maxWidth?: number;
}) {
  const t = useTip();
  const shown = hexes.filter((h) => h.share >= minShare && h.cy <= Y_TOP - 8);
  return (
    <div style={{ maxWidth, margin: "0 auto" }}>
      <svg viewBox={`${X_MIN - 5} -8 ${X_MAX - X_MIN + 10} ${Y_TOP - Y_MIN + 16}`} role="img" aria-label="Shot map"
           style={{ width: "100%", height: "auto", display: "block", background: "var(--court)", borderRadius: "var(--radius-sm)" }}>
        <CourtLines />
        {shown.map((h) => {
          const k = Math.min(1, Math.max(0.32, Math.sqrt(h.share / FULL_SHARE))) * 0.96;
          const fill = div(Math.max(-1, Math.min(1, value(h) / scale)));
          return (
            <polygon key={`${h.q}|${h.rr}`} points={hexPts(h.cx, h.cy, k)} fill={fill} stroke={fill} strokeWidth={0.6}
                     onPointerMove={(e) => t.show(e, tip ? tip(h) : (
                       <><b>{fmt.signed(h.diff, 1)} per 100 shots vs league</b>
                         <div className="muted">{fmt.pct(h.share, 1)} of shots · FG {fmt.pct(h.fg)} (league {fmt.pct(h.lg_fg)}) · {ZONE_LABELS[h.zone]}</div></>
                     ))} onPointerLeave={t.hide} />
          );
        })}
      </svg>
      {t.node}
    </div>
  );
}

/** A slim legend for the hexagon maps: colour steps and two sizes. */
export function HexKey({ unit, scale = PP100_SCALE }: { unit: string; scale?: number }) {
  const steps = Array.from({ length: 11 }, (_, i) => -1 + i * 0.2);
  return (
    <div className="row xs muted" style={{ gap: 10, justifyContent: "center", marginTop: 6 }}>
      <span>−{scale}</span>
      <svg width={11 * 16} height={16} aria-hidden="true">
        {steps.map((s, i) => <rect key={i} x={i * 16} y={2} width={15} height={12} rx={2} fill={div(s)} />)}
      </svg>
      <span>+{scale}</span>
      <span>{unit}</span>
      <span style={{ marginLeft: 8 }}>size: share of shots</span>
    </div>
  );
}

/** League zone baseline + a unit's zone counts (from a *_counts table) -> the zone profile
 * (port of data.zone_profile): share and FG% vs league, and what shot selection and shot
 * making are worth in points per 100 shots. */
export function zoneProfile(unit: Row | undefined, prefix: string, league: Row[]) {
  if (!unit) return [];
  const lg = new Map(league.map((r) => [r.zone, r]));
  const rows = ZONES.map((z) => ({ zone: z, fga: Number(unit[`${prefix}fga_${ZONE_KEY[z]}`] ?? 0), fgm: Number(unit[`${prefix}fgm_${ZONE_KEY[z]}`] ?? 0),
    share: lg.get(z)?.share ?? 0, fg_pct: lg.get(z)?.fg_pct ?? 0, xpts: lg.get(z)?.xpts ?? 0 }));
  const tot = rows.reduce((a, r) => a + r.fga, 0);
  if (!tot) return [];
  const shareSum = rows.reduce((a, r) => a + r.share, 0);
  const lgPps = rows.reduce((a, r) => a + r.share * r.xpts, 0) / (shareSum || 1);
  return rows.map((r) => {
    const unit_share = r.fga / tot, unit_fg = r.fga ? r.fgm / r.fga : NaN;
    const v = r.zone === "C3" || r.zone === "ATB3" ? 3 : 2;
    return { ...r, unit_share, unit_fg, share_diff: unit_share - r.share, fg_diff: unit_fg - r.fg_pct,
             selection_pp100: 100 * (unit_share - r.share) * (r.xpts - lgPps),
             making_pp100: (100 * (v * r.fgm - r.xpts * r.fga)) / tot };
  });
}
