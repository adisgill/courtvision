// Lineup helpers (as courtvision.lineups): the best fives a roster could play.

export type Candidate = { id: number; impact: number; grp: string };

/** The ``n`` fives with the highest summed impact among the top 13 by impact, each with at
 * least one guard and one or two bigs (lineups.best_fives). */
export function topFives(players: Candidate[], n = 5) {
  const pool = [...players].sort((a, b) => b.impact - a.impact).slice(0, 13);
  const out: { ids: number[]; sum: number }[] = [];
  const k = pool.length;
  for (let a = 0; a < k; a++) for (let b = a + 1; b < k; b++) for (let c = b + 1; c < k; c++) for (let d = c + 1; d < k; d++) for (let e = d + 1; e < k; e++) {
    const five = [pool[a], pool[b], pool[c], pool[d], pool[e]];
    const g = five.filter((p) => p.grp === "G").length, big = five.filter((p) => p.grp === "B").length;
    if (g < 1 || big < 1 || big > 2) continue;
    out.push({ ids: five.map((p) => p.id), sum: five.reduce((s, p) => s + p.impact, 0) });
  }
  return out.sort((x, y) => y.sum - x.sum).slice(0, n);
}

/** Position group: big (center), guard (guard and not forward), wing otherwise. */
export const grpOf = (r: { is_center?: boolean; is_guard?: boolean; is_forward?: boolean }) =>
  r.is_center ? "B" : r.is_guard && !r.is_forward ? "G" : "W";

/** A lineup's key in lineups.units: sorted ids joined by "-". */
export const lineupKey = (ids: number[]) => [...ids].sort((a, b) => a - b).join("-");
