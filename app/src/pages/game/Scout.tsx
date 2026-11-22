// The scouting report: where each attack meets the other defense's leaks (shot map), the plays
// to watch, the players to watch and the lineups each side can put out.
import { useMemo, useState } from "react";
import { lit, Row, useQuery } from "../../db";
import { fmt } from "../../lib/format";
import { grpOf, lineupKey, topFives } from "../../lib/lineups";
import { CURRENT_SEASON, DESCRIBE, rangeText } from "../../lib/nba";
import { Hex, HexCourt, HexKey, unitHex, ZONE_LABELS } from "../../ui/court";
import { HeatGrid } from "../../ui/grids";
import { Boundary } from "../../ui/Boundary";
import { Card, Loading, Tabs } from "../../ui/kit";

const PREV_W = 0.25; // last season's shots count a quarter
type Props = { home: number; away: number; hn: string; an: string; gameId: string };
const last = (s: string) => String(s).split(" ").slice(-1)[0];

export default function Scout(p: Props) {
  const [tab, setTab] = useState("zones");
  return (
    <>
      <Tabs tabs={[["zones", "Danger zones"], ["plays", "Plays to watch"], ["players", "Players to watch"], ["lineups", "Lineups"]]} value={tab} onChange={setTab} />
      <Boundary key={tab} label="This tab">
        {tab === "zones" && <DangerZones {...p} />}
        {tab === "plays" && <PlaysToWatch {...p} />}
        {tab === "players" && <PlayersToWatch {...p} />}
        {tab === "lineups" && <Lineups {...p} />}
      </Boundary>
    </>
  );
}

type Edge = Hex & { off: number; def: number; edge: number };

function DangerZones({ home, away, hn, an }: Props) {
  const S = CURRENT_SEASON;
  const shots = useQuery(`select s.season, s.team_id, g.home_team_id, g.away_team_id, s.x_legacy::double x, s.y_legacy::double y,
                                 s.made::int made, s.shot_value
                          from stg.shots s join stg.games g using (game_id)
                          where s.season in (${S - 1}, ${S}) and s.season_type = 'regular' and s.x_legacy is not null
                            and (g.home_team_id in (${home}, ${away}) or g.away_team_id in (${home}, ${away}))`);
  const lg = useQuery(`select * from web.league_hex where season in (${S - 1}, ${S}) and season_type = 'regular'`);
  const maps = useMemo(() => {
    if (!shots.rows.length || !lg.rows.length) return null;
    const w = (r: Row) => ({ ...r, w: Number(r.season) === S ? 1 : PREV_W });
    const inGame = (r: Row, t: number) => Number(r.home_team_id) === t || Number(r.away_team_id) === t;
    return ([[away, home, an, hn], [home, away, hn, an]] as [number, number, string, string][]).map(([att, dfn, A, D]) => {
      const off = unitHex(shots.rows.filter((r) => Number(r.team_id) === att).map(w), lg.rows);
      const de = unitHex(shots.rows.filter((r) => Number(r.team_id) !== dfn && inGame(r, dfn)).map(w), lg.rows);
      const dmap = new Map(de.map((h) => [`${h.q}|${h.rr}`, h.diff]));
      const zdef = new Map(de.map((h) => [h.zone, 100 * h.zone_diff]));
      const edge: Edge[] = off.map((h) => {
        const d = dmap.get(`${h.q}|${h.rr}`) ?? zdef.get(h.zone) ?? 0;
        return { ...h, off: h.diff, def: d, edge: h.diff + d };
      });
      const zones = new Map<string, { share: number; n: number; e: number }>();
      edge.forEach((h) => { const z = zones.get(h.zone) ?? { share: 0, n: 0, e: 0 }; z.share += h.share; z.n += h.n; z.e += h.edge * h.n; zones.set(h.zone, z); });
      const zs = [...zones.entries()].map(([z, v]) => ({ zone: z, share: v.share, edge: v.e / v.n, gain: v.share * (v.e / v.n) })).sort((a, b) => a.gain - b.gain);
      return { att, A, D, edge, best: zs[zs.length - 1], worst: zs[0] };
    });
  }, [shots.rows, lg.rows, home, away, hn, an, S]);
  if (!maps) return <Loading state={shots.loading ? shots : lg} />;
  return (
    <>
      <div className="grid">
        {maps.map((m) => (
          <div key={m.att} className="c6">
            <Card title={`Where the ${m.A} can hurt the ${m.D}`}>
              <HexCourt hexes={m.edge} value={(h) => (h as Edge).edge} tip={(h) => {
                const e = h as Edge;
                return <><b>edge {fmt.signed(e.edge, 1)}</b> points per 100 shots vs league
                  <div className="muted">{m.A} take {fmt.pct(e.share, 1)} of their shots here · they score {fmt.signed(e.off, 1)} · {m.D} allow {fmt.signed(e.def, 1)}</div></>;
              }} />
              {m.best && <p className="small ink2" style={{ marginTop: 8 }}>Biggest edge: <b>{ZONE_LABELS[m.best.zone]}</b> ({fmt.pct(m.best.share)} of their shots, {fmt.signed(m.best.edge, 1)} per 100).
                Where the {m.D} hold up best: <b>{ZONE_LABELS[m.worst.zone]}</b> ({fmt.signed(m.worst.edge, 1)}).</p>}
            </Card>
          </div>
        ))}
      </div>
      <HexKey unit="edge: points per 100 shots vs league (offense's shooting + defense's allowance)" />
      <p className="xs muted">Hexagon size: how often the attacking team shoots from there. Colour: what it scores from there against league average plus what
        the defense allows there, both shrunk toward zone and league averages; this season's shots count fully, last season's a quarter. Blue: the attack's edge.</p>
    </>
  );
}

function PlaysToWatch({ home, away, hn, an }: Props) {
  const q = useQuery(`select * from plays.team where season = (select max(season) from plays.team) and team_id in (${home}, ${away})`);
  if (!q.rows.length) return <Loading state={q} />;
  return (
    <>
      <div className="grid">
        {([[away, home, an, hn], [home, away, hn, an]] as [number, number, string, string][]).map(([att, dfn, A, D]) => {
          const own = q.rows.filter((r) => Number(r.team_id) === att && r.side === "offense");
          const allow = new Map(q.rows.filter((r) => Number(r.team_id) === dfn && r.side === "defense").map((r) => [r.play, r.pp100_rel_shrunk]));
          const rows = own.filter((r) => r.share >= 0.02).map((r): Row => {
            const al = allow.get(r.play) ?? 0;
            const edge = r.pp100_rel_shrunk + al;
            return { ...r, al, edge, impact: r.share * edge };
          }).sort((a, b) => b.impact - a.impact);
          const watch = rows.filter((r) => r.impact > 0).slice(0, 2);
          return (
            <div key={att} className="c6">
              <Card title={`${A} attack vs ${D} defense`} sub="Points per 100 shots vs league on each play, shrunk by how far a season of that play can be trusted.">
                <HeatGrid rowHead="Play" rowWidth={140} cols={[{ key: "often", label: "How often" }, { key: "own", label: `${A} score` },
                  { key: "allow", label: `${D} allow` }, { key: "edge", label: "Edge" }]}
                  rows={rows.map((r) => ({ key: r.play, label: r.play, cells: {
                    often: { text: fmt.pct(r.share), tip: <>{fmt.pct(r.share, 1)} of their shots (league {fmt.pct(r.lg_share, 1)})<div className="muted">{DESCRIBE[r.play]}</div></> },
                    own: { text: fmt.signed(r.pp100_rel_shrunk, 1), z: r.pp100_rel_shrunk / 8 },
                    allow: { text: fmt.signed(r.al, 1), z: r.al / 8 },
                    edge: { text: fmt.signed(r.edge, 1), z: r.edge / 8 },
                  } }))} />
                {watch.length > 0 && <p className="small ink2" style={{ marginTop: 8 }}>Watch: {watch.map((w, i) => <span key={w.play}>{i ? "; " : ""}<b>{w.play}</b> ({fmt.pct(w.share)} of their shots, edge {fmt.signed(w.edge, 1)})</span>)}.</p>}
              </Card>
            </div>
          );
        })}
      </div>
      <p className="xs muted">This season's play-type profiles (plays.team). A play's edge is the attack's efficiency on it plus what the defense allows on it;
        efficiency on cuts, lobs, step-backs and in transition is mostly noise within a season, so those are shrunk hardest.</p>
    </>
  );
}

function PlayersToWatch({ home, away, hn, an, gameId }: Props) {
  const S = CURRENT_SEASON;
  const lines = useQuery(`select l.*, p.player from forecast.player_lines l join web.player_names p using (person_id)
                          where l.game_id = ${lit(gameId)} and l.exp_minutes >= 15`);
  const ids = lines.rows.map((r) => Number(r.person_id));
  const idl = ids.length ? ids.join(",") : "0";
  const form = useQuery(ids.length ? `
    with g as (select c.person_id, c.minutes, c.b_pts pts,
                      row_number() over (partition by c.person_id order by c.game_date desc) rk
               from metrics.player_game_counts c
               where c.season in (${S - 1}, ${S}) and c.season_type = 'regular' and c.minutes > 0 and c.person_id in (${idl}))
    select person_id, sum(pts) filter (where rk <= 5) p5, sum(minutes) filter (where rk <= 5) m5,
           sum(pts) filter (where rk between 6 and 45) / nullif(sum(minutes) filter (where rk between 6 and 45), 0) usual
    from g group by 1` : null);
  const plays = useQuery(ids.length ? `
    with ps as (select * from plays.player where season = (select max(season) from plays.player) and person_id in (${idl})),
    d as (select team_id, play, pp100_rel_shrunk allow from plays.team where season = (select max(season) from plays.team) and side = 'defense')
    select ps.person_id, ps.play, ps.share, ps.pp100_rel_shrunk rel, d.allow, ps.shots
    from ps left join forecast.player_lines l on l.person_id = ps.person_id and l.game_id = ${lit(gameId)}
    left join d on d.team_id = l.opp_id and d.play = ps.play` : null);
  if (!lines.rows.length) return <Loading state={lines} />;
  const F = new Map(form.rows.map((r) => [Number(r.person_id), r.usual ? ((r.p5 + 60 * r.usual) / (r.m5 + 60)) / r.usual - 1 : null]));
  const byP = new Map<number, Row[]>();
  plays.rows.forEach((r) => { const k = Number(r.person_id); byP.set(k, [...(byP.get(k) ?? []), r]); });
  return (
    <>
      <div className="grid">
        {([[away, an], [home, hn]] as [number, string][]).map(([t, name]) => {
          const rows = lines.rows.filter((r) => Number(r.team_id) === t).sort((a, b) => b.pts - a.pts).slice(0, 8);
          return (
            <div key={t} className="c6">
              <Card title={name}>
                <HeatGrid rowHead="Player" rowWidth={150} cols={[{ key: "pts", label: "Points" }, { key: "form", label: "Form" }, { key: "match", label: "Matchup" }, { key: "goto", label: "Go-to play" }]}
                  rows={rows.map((r) => {
                    const ps = byP.get(Number(r.person_id)) ?? [];
                    const tot = ps.reduce((a, x) => a + x.share, 0) || 1;
                    const edge = ps.reduce((a, x) => a + (x.share / tot) * (x.allow ?? 0), 0);
                    const go = [...ps].sort((a, b) => b.share - a.share)[0];
                    const f = F.get(Number(r.person_id));
                    return { key: String(r.person_id), label: r.player, to: `/player/${r.person_id}`, cells: {
                      pts: { text: `${fmt.n(r.pts, 1)} (${rangeText(r.pts_lo, r.pts_hi)})`, z: (r.pts - 12) / 14 },
                      form: f == null ? { text: "–", muted: true } : { text: fmt.signed(100 * f, 0) + "%", z: f / 0.35,
                        tip: <>points per minute in his last 5 games against his 40 before, pulled toward his usual rate by 60 minutes</> },
                      match: ps.length ? { text: fmt.signed(edge, 1), z: edge / 6, tip: <>his play mix against what this defense allows on each play (points per 100 shots vs league)</> } : { text: "–", muted: true },
                      goto: go ? { text: `${go.play} ${fmt.pct(go.share)}`, z: (go.rel ?? 0) / 15, tip: <>{fmt.pct(go.share, 1)} of his shots; {fmt.signed(go.rel, 1)} per 100 vs league on it</> } : { text: "–", muted: true },
                    } };
                  })} />
              </Card>
            </div>
          );
        })}
      </div>
      <p className="xs muted">Points: expected, with the 80% range. Form and matchup describe the game; the tested player model found that matchup adjustments
        add little to his expected line.</p>
    </>
  );
}

function Lineups({ home, away, hn, an, gameId }: Props) {
  const S = CURRENT_SEASON;
  const avail = useQuery(`select person_id, team_id from forecast.player_lines where game_id = ${lit(gameId)} and exp_minutes >= 8`);
  const units = useQuery(`select team_id, lineup, players, poss, starts, proj_net100 from lineups.units
                          where season = (select max(season) from lineups.units) and team_id in (${home}, ${away}) order by poss desc`);
  const roster = useQuery(`select i.person_id, i.impact, p.is_center, p.is_guard, p.is_forward, n.player
                           from metrics.player_impact i join stg.players p using (person_id) join web.player_names n using (person_id)
                           where i.season = ${S}`);
  if (!units.rows.length || !avail.rows.length) return <Loading state={units} />;
  const names = new Map(roster.rows.map((r) => [Number(r.person_id), last(r.player)]));
  return (
    <div className="grid">
      {([[away, an], [home, hn]] as [number, string][]).map(([t, name]) => {
        const ok = new Set(avail.rows.filter((r) => Number(r.team_id) === t).map((r) => Number(r.person_id)));
        const us = units.rows.filter((u) => Number(u.team_id) === t && (u.players as number[]).every((x) => ok.has(Number(x))));
        const starters = [...us].sort((a, b) => b.starts - a.starts)[0];
        const used = us.filter((u) => u !== starters).slice(0, 3);
        const pool = roster.rows.filter((r) => ok.has(Number(r.person_id)) && r.impact != null)
          .map((r) => ({ id: Number(r.person_id), impact: r.impact as number, grp: grpOf(r) }));
        const best = topFives(pool, 1)[0] ?? null;
        const key = best ? lineupKey(best.ids) : "";
        const seen = units.rows.find((u) => Number(u.team_id) === t && u.lineup === key);
        const rows = [
          ...(starters ? [{ label: "Likely starters", u: starters }] : []),
          ...used.map((u) => ({ label: "Most used", u })),
        ];
        return (
          <div key={t} className="c6">
            <Card title={name} sub="Projected net per 100 possessions: the players' impacts plus the lineup's own record, shrunk.">
              <HeatGrid rowHead="" rowWidth={300} cols={[{ key: "proj", label: "Projected net" }, { key: "tog", label: "Together" }]}
                rows={[...rows.map((r, i) => ({ key: `${i}`, label: <><span className="muted xs">{r.label}</span><br />{(r.u.players as number[]).map((x) => names.get(Number(x)) ?? x).join(" · ")}</>,
                  cells: { proj: { text: fmt.signed(r.u.proj_net100, 1), z: r.u.proj_net100 / 10 }, tog: { text: r.u.poss < 50 ? "untried" : `${fmt.n(r.u.poss)} poss`, muted: true } } })),
                  ...(best ? [{ key: "best", label: <><span className="muted xs">Best possible (by impact)</span><br />{best.ids.map((x) => names.get(x) ?? x).join(" · ")}</>,
                    cells: { proj: { text: fmt.signed(best.sum, 1), z: best.sum / 10, tip: <>sum of the five players' impacts per 100</> }, tog: { text: seen ? `${fmt.n(seen.poss)} poss` : "untried", muted: true } } }] : [])]} />
            </Card>
          </div>
        );
      })}
    </div>
  );
}
