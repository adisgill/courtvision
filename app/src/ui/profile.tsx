// Figures the team and player pages share: the dimension bars, shot zones (hex map + shot mix),
// the rating trend, and the season-by-season grid with league percentiles.
import * as Plot from "@observablehq/plot";
import { ReactNode, useMemo, useState } from "react";
import { Row, useQuery } from "../db";
import { css, fmt, seasonLabel, slot } from "../lib/format";
import { DIMENSIONS, isRateMeasure, labelOf, ordered } from "../lib/nba";
import { HexCourt, HexKey, unitHex, ZONE_LABELS, zoneProfile } from "./court";
import { Figure } from "./Figure";
import { Bars, DivBars, HeatGrid } from "./grids";
import { Card, DataTable, Seg, Tabs } from "./kit";

/** Headline dimensions and their components as diverging bars: value vs league, with rating. */
export function DimensionCharts({ rows }: { rows: Row[] }) {
  const [tab, setTab] = useState("dims");
  const heads = ordered(rows.filter((r) => DIMENSIONS.some(([k]) => k === r.measure)));
  const comps = ordered(rows.filter((r) => !DIMENSIONS.some(([k]) => k === r.measure) && !isRateMeasure(r.measure) && r.measure !== "presence"));
  const show = tab === "dims" ? heads : comps;
  return (
    <Card>
      <Tabs tabs={[["dims", "Dimensions"], ["comps", "What drives them"]]} value={tab} onChange={setTab} />
      <DivBars height={tab === "dims" ? 30 : 22} rows={show.map((r) => ({ label: labelOf(r.measure), v: r.value ?? 0,
        text: `${fmt.signed(r.value, 1)} · ${r.rating == null ? "–" : Math.round(r.rating)}`,
        tip: <><b>{labelOf(r.measure)}</b>: {fmt.signed(r.value, 2)} points per 100 vs league · rating {r.rating == null ? "–" : Math.round(r.rating)}
          {r.low_sample ? <div className="muted">small sample</div> : null}</> }))} />
      <p className="xs muted" style={{ marginTop: 8 }}>Bar: points per 100 vs league average (blue helps, red hurts). Number after the dot: the 0-100 rating.</p>
    </Card>
  );
}

/** Shot zones of a unit: the hex map against league from the same spots, and the shot mix. */
export function ShotZones({ shotsSql, countsSql, prefix, seasons, stype = "regular", who, allowed }: {
  shotsSql: string | null; countsSql: string | null; prefix: string; seasons: number[]; stype?: string; who: string; allowed?: boolean;
}) {
  const shots = useQuery(shotsSql);
  const lg = useQuery(seasons.length ? `select * from web.league_hex where season in (${seasons.join(",")}) and season_type = '${stype}'` : null);
  const counts = useQuery(countsSql);
  const zb = useQuery(seasons.length ? `select zone, sum(share) share, sum(fgm) / sum(fga) fg_pct, sum(xpts * fga) / sum(fga) xpts
                                         from metrics.zone_baselines where season = ${Math.max(...seasons)} and zone <> 'BACK' group by zone` : null);
  const hexes = useMemo(() => (shots.rows.length && lg.rows.length ? unitHex(shots.rows, lg.rows).map((h) => (allowed ? { ...h, diff: -h.diff } : h)) : []),
    [shots.rows, lg.rows, allowed]);
  const prof = zoneProfile(counts.rows[0], prefix, zb.rows);
  const sel = prof.reduce((a, r) => a + r.selection_pp100, 0), mk = prof.reduce((a, r) => a + r.making_pp100, 0);
  return (
    <div className="grid">
      <div className="c7">
        <Card title={allowed ? `Opponents' shots against ${who}` : `${who}'s shots`} sub="Where from, and how well against the league from the same spots.">
          {hexes.length ? <HexCourt hexes={hexes} tip={(h) => <><b>{fmt.signed(allowed ? -h.diff : h.diff, 1)} per 100 shots vs league</b>
            <div className="muted">{fmt.pct(h.share, 1)} of {allowed ? "opponent " : ""}shots · FG {fmt.pct(h.fg)} (league {fmt.pct(h.lg_fg)}) · {ZONE_LABELS[h.zone]}</div></>} />
            : <p className="muted small">{shots.loading ? "Loading shots…" : "No shot locations for this selection."}</p>}
          <HexKey unit={allowed ? "points per 100 shots vs league (blue: the defense held them below)" : "points per 100 shots vs league from the same spots"} />
        </Card>
      </div>
      <div className="c5">
        <Card title={`Where ${allowed ? "opponents" : who} shot from`} sub="Share of shots by zone; tick = league average.">
          <Bars rows={prof.map((r) => ({ label: ZONE_LABELS[r.zone], v: 100 * r.unit_share, ref: 100 * r.share, color: "var(--axis)",
            tip: <><b>{fmt.pct(r.unit_share, 1)}</b> of shots (league {fmt.pct(r.share, 1)}) · FG {fmt.pct(r.unit_fg, 1)} (league {fmt.pct(r.fg_pct, 1)})</> }))}
            fmtV={(v) => `${v.toFixed(0)}%`} labelWidth={130} />
          {prof.length > 0 && <p className="small ink2" style={{ marginTop: 10 }}>Shot selection is worth <b>{fmt.signed(sel, 1)}</b> points per 100 shots against a league shot mix;
            how many went in, <b>{fmt.signed(mk, 1)}</b>{allowed ? " (positive is bad for the defense)" : ""}.</p>}
          {prof.length > 0 && <DataTable rows={prof} cols={[{ key: "zone", label: "Zone", fmt: (v) => ZONE_LABELS[v] }, { key: "fga", label: "FGA", num: true },
            { key: "unit_fg", label: "FG%", num: true, fmt: (v) => fmt.pct(v, 1) }, { key: "fg_pct", label: "League", num: true, fmt: (v) => fmt.pct(v, 1) },
            { key: "making_pp100", label: "Making", num: true, fmt: (v) => fmt.signed(v, 1) }]} />}
        </Card>
      </div>
    </div>
  );
}

/** Game ratings over the season: 10-game rolling mean of each headline dimension. */
export function Trend({ rows, title }: { rows: Row[]; title: string }) {
  const plot = useMemo(() => {
    if (!rows.length) return undefined;
    const out: Row[] = [];
    DIMENSIONS.forEach(([k, l], i) => {
      const g = rows.filter((r) => r.measure === k).sort((a, b) => +new Date(a.game_date) - +new Date(b.game_date));
      g.forEach((r, j) => {
        const w = g.slice(Math.max(0, j - 9), j + 1).map((x) => x.rating).filter((x) => x != null);
        if (w.length >= 3) out.push({ date: new Date(r.game_date), v: w.reduce((a, b) => a + b, 0) / w.length, dim: l, c: slot(i + 1) });
      });
    });
    return (width: number) => ({
      width, height: 300, x: { label: null, type: "utc" as const }, y: { label: "rating, 10-game mean", grid: true },
      color: { domain: DIMENSIONS.map(([, l]) => l), range: DIMENSIONS.map((_, i) => slot(i + 1)), legend: true },
      marks: [Plot.ruleY([50], { stroke: css("--axis") }),
              Plot.line(out, { x: "date", y: "v", stroke: "dim", strokeWidth: 2 }),
              Plot.tip(out, Plot.pointer({ x: "date", y: "v", stroke: "dim", title: (d: any) => `${d.dim}: ${d.v.toFixed(0)} (${fmt.day(d.date)})` }))],
    });
  }, [rows]);
  return plot ? <Figure title={title} subtitle="50 = league average game." plot={plot} /> : null;
}

export type StatCol = { key: string; label: string; dir: 1 | -1 | 0; fmt: (v: number) => string; need?: string };

/** Season-by-season heat grid: each cell shaded by the season's league percentile. */
export function SeasonGrid({ rows, pool, cols, label, recorded, note }: {
  rows: Row[]; pool: Row[]; cols: StatCol[]; label: (r: Row) => ReactNode; recorded?: Set<string>; note?: ReactNode;
}) {
  const [all, setAll] = useState(false);
  const bySeason = useMemo(() => {
    const m = new Map<number, Row[]>();
    pool.forEach((r) => m.set(Number(r.season), [...(m.get(Number(r.season)) ?? []), r]));
    return m;
  }, [pool]);
  const shown = (all ? rows : rows.slice(0, 10));
  return (
    <>
      {rows.length > 10 && <div className="filters"><div className="field">Seasons<Seg options={[["10", "Last 10"], ["all", `All ${rows.length}`]]} value={all ? "all" : "10"} onChange={(v) => setAll(v === "all")} /></div></div>}
      <HeatGrid rowHead="Season" rowWidth={170} compact cols={cols.map((c) => ({ key: c.key, label: c.label }))}
        rows={shown.map((r) => ({ key: String(r.season) + (r.key ?? ""), label: label(r), cells: Object.fromEntries(cols.map((c) => {
          const v = r[c.key];
          if (v == null || Number.isNaN(v) || (c.need && recorded && !recorded.has(`${r.season}|${c.need}`))) return [c.key, { text: "–", muted: true, tip: <>not recorded in the source that season</> }];
          const lg = (bySeason.get(Number(r.season)) ?? []).map((x) => x[c.key]).filter((x) => x != null && !Number.isNaN(x));
          const pct = lg.length ? lg.filter((x) => x < v).length / lg.length : 0.5;
          return [c.key, { text: c.fmt(v), z: c.dir ? (2 * pct - 1) * c.dir : null,
            tip: c.dir ? <>{c.fmt(v)} · better than {Math.round(100 * (c.dir > 0 ? pct : 1 - pct))}% that season</> : <>{c.fmt(v)} · {Math.round(100 * pct)}th percentile</> }];
        })) }))} />
      {note}
    </>
  );
}

export const seasonRowLabel = (s: number, extra = "", soFar = false) => <>{seasonLabel(s)}{extra ? <span className="muted"> {extra}</span> : null}{soFar ? <span className="muted"> · so far</span> : null}</>;
