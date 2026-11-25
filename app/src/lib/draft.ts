// The draft: labels, formatting, and the mock draft and lottery (the same rules as
// courtvision/draft.py, so a re-drawn lottery gets the same kind of picks).
import type { Row } from "../db";

export const DIMS: [string, string][] = [["shooting", "Shooting"], ["defense", "Defense"], ["ball_control", "Ball control"],
  ["backcourt", "Backcourt"], ["frontcourt", "Frontcourt"]];

export const FEATURE_LABEL: Record<string, string> = {
  age: "Age at the draft", age2: "Age (curve)", dob_known: "Birth date on record", height: "Height", rec: "Recruiting score",
  rec_top: "Elite recruit", rec_missing: "Unrated recruit", high_major: "High-major conference", seasons: "Seasons in college",
  mpg: "Minutes per game", min_pct: "Share of team minutes", bpm_gain: "Gain on last season", bpm: "Box plus-minus",
  obpm: "Offensive BPM", dbpm: "Defensive BPM", usg: "Usage", ts: "True shooting", ast_pct: "Assist rate", tov_pct: "Turnover rate",
  orb_pct: "Offensive rebounding", drb_pct: "Defensive rebounding", stl_pct: "Steal rate", blk_pct: "Block rate", ftr: "Free-throw rate",
  fg3_per100: "Threes per 100", fg3_rate: "Share of shots from three", rim_rate: "Share of shots at the rim", dunk_rate: "Dunk rate",
  sos: "Schedule strength", ft_pct: "Free-throw %", fg3_pct: "Three-point %",
};

export const TIER_LABEL: [string, string, string][] = [["p_nba", "NBA player", "1+ WAR in his first four seasons"],
  ["p_rotation", "Rotation", "4+ WAR"], ["p_starter", "Starter", "10+ WAR"], ["p_star", "Star", "20+ WAR"]];

export const height = (inches: number | null | undefined) =>
  inches == null || Number.isNaN(inches) ? "–" : `${Math.floor(inches / 12)}′${Math.round(inches % 12)}″`;

export const pct1 = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? "–" : `${(v > 1 ? v : v * 100).toFixed(1)}%`);

const GUARDS = new Set(["Pure PG", "Scoring PG", "Combo G", "Wing G"]);
const BIGS = new Set(["Stretch 4", "PF/C", "C"]);
export const posGroup = (role: string) => (GUARDS.has(role) ? "Guards" : BIGS.has(role) ? "Bigs" : "Wings");

/** How much a team needs each dimension: points below average, in tens, 0-2 (draft.need_weights). */
export function needWeights(team: Row | undefined): Record<string, number> {
  return Object.fromEntries(DIMS.map(([d]) => {
    const v = team?.[`${d}_adj`];
    return [d, v == null ? 0 : Math.max(0, Math.min(2, (50 - v) / 10))];
  }));
}

/** The dimension where a prospect answers the team's need most, if any. */
export function bestFill(team: Row | undefined, p: Row): string | null {
  const w = needWeights(team);
  let best: string | null = null, top = 0.3;
  for (const [d] of DIMS) {
    const s = w[d] * (p[d] ?? 0);
    if (w[d] > 0 && (p[d] ?? 0) > 0.4 && s > top) { top = s; best = d; }
  }
  return best;
}

export type MockPick = { pick: number; team: number; original: number; p: Row; bpa: Row; fit: number };

/** At each pick, the available prospect with the most expected WAR after fit:
 * ev x (1 + weight x fit), fit from draft.fit (team x prospect). */
export function runMock(owners: number[], originals: number[], board: Row[], fit: Map<string, number>, weight: number): MockPick[] {
  const left = [...board];
  return owners.map((team, i) => {
    let bi = 0, best = -Infinity;
    left.forEach((p, j) => {
      const v = p.ev * (1 + weight * (fit.get(`${team}:${p.pid}`) ?? 0));
      if (v > best) { best = v; bi = j; }
    });
    const bpa = left.reduce((a, b) => (b.ev > a.ev ? b : a));
    const [p] = left.splice(bi, 1);
    return { pick: i + 1, team, original: originals[i], p, bpa, fit: fit.get(`${team}:${p.pid}`) ?? 0 };
  });
}

/** One lottery: four picks drawn among the 14 worst projected teams by the odds, the
 * rest of them in seed order, then everyone else by record. ``seeded``: teams worst first. */
export function drawLottery(seeded: number[], odds: number[], rand = Math.random): number[] {
  const lott = seeded.slice(0, 14);
  const left = lott.map((_, i) => i);
  const won: number[] = [];
  for (let k = 0; k < 4; k++) {
    const total = left.reduce((a, i) => a + odds[i], 0);
    let r = rand() * total, j = 0;
    while (j < left.length - 1 && r >= odds[left[j]]) { r -= odds[left[j]]; j++; }
    won.push(left.splice(j, 1)[0]);
  }
  return [...won, ...left].map((i) => lott[i]).concat(seeded.slice(14));
}

/** Who picks at each slot after recorded trades (``order`` rows carry trade_owner_id,
 * kind and protected_through for a traded pick). */
export function applyTrades(slots: number[], order: Row[]): number[] {
  const owner = new Map(slots.map((t) => [t, t]));
  const slotOf = new Map(slots.map((t, i) => [t, i + 1]));
  for (const r of order) {
    const t = Number(r.team_id), to = r.trade_owner_id == null ? null : Number(r.trade_owner_id);
    if (to == null || Number.isNaN(to)) continue;
    if (r.kind === "swap") {
      if ((slotOf.get(t) ?? 99) < (slotOf.get(to) ?? 99)) { owner.set(t, to); owner.set(to, t); }
    } else if (r.protected_through == null || Number.isNaN(r.protected_through) || (slotOf.get(t) ?? 0) > r.protected_through) {
      owner.set(t, to);
    }
  }
  return slots.map((t) => owner.get(t) ?? t);
}
