// Play types from the play-by-play (no tracking data): every team's mix, one team's profile at
// either end, who runs each play, the passing connections and the turnovers.
import { useMemo } from "react";
import { Row, useQuery } from "../db";
import { fmt, seasonLabel } from "../lib/format";
import { DESCRIBE, PLAYS, TOV_KINDS, useTeamNames } from "../lib/nba";
import { Bars, DivBars, HeatGrid } from "../ui/grids";
import { Card, DataTable, PageHead, Seg, SeasonSelect, Select, useParam, useTip } from "../ui/kit";

const last = (s: string) => String(s ?? "").split(" ").slice(-1)[0];

export default function Plays() {
  const ss = useQuery(`select distinct season from plays.team order by 1 desc`);
  const seasons = ss.rows.map((r) => Number(r.season));
  const [sp, setSeason] = useParam("season", "");
  const season = Number(sp) || seasons[0] || 0;
  const [side, setSide] = useParam("side", "offense");
  const names = useTeamNames(season);
  const all = useQuery(season ? `select * from plays.team where season = ${season} and side = '${side}'` : null);
  const rel = useQuery(`select * from plays.reliability where side = '${side}'`);
  const sign = side === "offense" ? 1 : -1;
  const teamIds = [...new Set(all.rows.map((r) => Number(r.team_id)))].sort((a, b) => (names.get(a)?.team ?? "").localeCompare(names.get(b)?.team ?? ""));
  const [tp, setTeam] = useParam("team", "");
  const team = Number(tp) || teamIds[0] || 0;
  const T = names.get(team);
  const mine = PLAYS.map((p) => all.rows.find((r) => Number(r.team_id) === team && r.play === p)).filter(Boolean) as Row[];
  const ranked = [...mine].sort((a, b) => sign * (b.share * b.pp100_rel_shrunk) - sign * (a.share * a.pp100_rel_shrunk));
  return (
    <>
      <PageHead eyebrow={`Play types · ${season ? seasonLabel(season) : ""}`} title="How teams score and defend"
        lead="Every shot classified from the play-by-play text: transition, putbacks, lobs, cuts, drives, floaters, post-ups, step-backs, pull-ups, spot-up threes and the rest. Efficiency is points per 100 shots against the league on the same play, shrunk by how far a season of that play can be trusted." />
      <div className="filters">
        {seasons.length > 0 && <SeasonSelect value={season} onChange={(v) => setSeason(String(v))} seasons={seasons} />}
        <Select label="Team" value={String(team)} onChange={setTeam} options={teamIds.map((t) => [String(t), names.get(t)?.team ?? String(t)])} />
        <div className="field">Side<Seg options={[["offense", "Offense"], ["defense", "Defense"]]} value={side} onChange={setSide} /></div>
      </div>
      <section className="section">
        <h2>{T?.name ?? ""}: what they {side === "offense" ? "run" : "allow"}</h2>
        <div className="grid">
          <div className="c6">
            <Card title="How often" sub={`Share of ${side === "offense" ? "their" : "opponents'"} shots; tick = league average.`}>
              <Bars rows={mine.map((r) => ({ label: r.play, v: 100 * r.share, ref: 100 * r.lg_share, tip: <><b>{fmt.pct(r.share, 1)}</b> (league {fmt.pct(r.lg_share, 1)})<div className="muted">{DESCRIBE[r.play]}</div></> }))}
                fmtV={(v) => `${v.toFixed(0)}%`} labelWidth={130} />
            </Card>
          </div>
          <div className="c6">
            <Card title="How well" sub={`Points per 100 shots vs league${side === "defense" ? " (blue: the defense held opponents below)" : ""}.`}>
              <DivBars rows={mine.map((r) => ({ label: r.play, v: sign * r.pp100_rel_shrunk, text: fmt.signed(r.pp100_rel_shrunk, 1),
                tip: <>{fmt.signed(r.pp100_rel_shrunk, 1)} per 100 vs league ({fmt.n(r.shots)} shots, shrunk)</> }))} labelWidth={130} />
            </Card>
          </div>
        </div>
        {ranked.length > 1 && <p className="small ink2">Most valuable: <b>{ranked[0].play}</b> ({fmt.pct(ranked[0].share)}, {fmt.signed(ranked[0].pp100_rel_shrunk, 1)}): {DESCRIBE[ranked[0].play]}.
          Costliest: <b>{ranked[ranked.length - 1].play}</b> ({fmt.pct(ranked[ranked.length - 1].share)}, {fmt.signed(ranked[ranked.length - 1].pp100_rel_shrunk, 1)}).</p>}
      </section>
      {side === "offense" && team > 0 && <WhoRunsIt season={season} team={team} />}
      {side === "offense" && team > 0 && <Passing season={season} team={team} />}
      {team > 0 && <Turnovers season={season} team={team} side={side} />}
      <section className="section">
        <h2>Every team's play mix{side === "defense" ? " allowed" : ""}</h2>
        <Card sub="Cell: share of shots on that play; colour: efficiency on it vs league (blue good for the team). Play mix is a team's fingerprint (split-half r 0.7-0.99); efficiency on cuts, lobs, step-backs and in transition is mostly noise within a season.">
          <HeatGrid rowHead="Team" rowWidth={170} compact cols={PLAYS.map((p) => ({ key: p, label: p, title: DESCRIBE[p] }))}
            rows={teamIds.map((t) => ({ key: String(t), label: names.get(t)?.name ?? String(t), to: `/plays?season=${season}&team=${t}&side=${side}`,
              cells: Object.fromEntries(PLAYS.map((p) => {
                const r = all.rows.find((x) => Number(x.team_id) === t && x.play === p);
                return [p, r ? { text: fmt.pct(r.share), z: Math.max(-1, Math.min(1, (sign * r.pp100_rel_shrunk) / 8)),
                  tip: <>{fmt.pct(r.share, 1)} of {side === "defense" ? "opponent " : ""}shots · {fmt.signed(r.pp100_rel_shrunk, 1)} per 100 vs league</> } : { text: "–", muted: true }];
              })) }))} />
        </Card>
        <Card title="How far to trust each play" className="flat">
          <DataTable rows={rel.rows} cols={[{ key: "play", label: "Play" }, { key: "share_r", label: "Mix repeats (r)", num: true, fmt: (v) => fmt.n(v, 2) },
            { key: "efficiency_r", label: "Efficiency repeats (r)", num: true, fmt: (v) => fmt.n(v, 2) }, { key: "k", label: "Shrinkage (shots)", num: true, fmt: (v) => fmt.n(v) }]} />
        </Card>
      </section>
    </>
  );
}

function WhoRunsIt({ season, team }: { season: number; team: number }) {
  const [play, setPlay] = useParam("play", "Pull-up");
  const q = useQuery(`select p.*, n.player from plays.player p join web.player_names n using (person_id)
                      where p.season = ${season} and p.team_id = ${team} and p.play = '${play.replace(/'/g, "''")}' order by p.shots desc limit 10`);
  return (
    <section className="section">
      <h2>Who runs it</h2>
      <div className="filters"><Select label="Play" value={play} onChange={setPlay} options={PLAYS.map((p) => [p, p])} /></div>
      <Card title={`${play}: the team's top shooters on it`} sub={DESCRIBE[play]}>
        <DivBars rows={q.rows.map((r) => ({ label: r.player, v: r.pp100_rel_shrunk, to: `/player/${r.person_id}`,
          text: `${fmt.n(r.shots)} shots · ${fmt.signed(r.pp100_rel_shrunk, 1)}`, tip: <>{fmt.pct(r.share, 1)} of his shots · {fmt.signed(r.pp100_rel_shrunk, 1)} per 100 vs league</> }))} />
      </Card>
    </section>
  );
}

function Passing({ season, team }: { season: number; team: number }) {
  const q = useQuery(`select a.passer_id, a.shooter_id, a.play, a.assists, a.pts, p1.player passer, p2.player shooter
                      from plays.assists a join web.player_names p1 on p1.person_id = a.passer_id join web.player_names p2 on p2.person_id = a.shooter_id
                      where a.season = ${season} and a.team_id = ${team}`);
  const tip = useTip();
  const data = useMemo(() => {
    const pairs = new Map<string, { a: number; b: number; an: string; bn: string; assists: number; pts: number; plays: Map<string, number> }>();
    q.rows.forEach((r) => {
      const k = `${r.passer_id}|${r.shooter_id}`;
      const p = pairs.get(k) ?? { a: Number(r.passer_id), b: Number(r.shooter_id), an: r.passer, bn: r.shooter, assists: 0, pts: 0, plays: new Map() };
      p.assists += r.assists; p.pts += r.pts; p.plays.set(r.play, (p.plays.get(r.play) ?? 0) + r.assists);
      pairs.set(k, p);
    });
    const top = [...pairs.values()].sort((x, y) => y.assists - x.assists).slice(0, 18);
    const vol = new Map<number, { n: string; v: number }>();
    top.forEach((p) => { vol.set(p.a, { n: p.an, v: (vol.get(p.a)?.v ?? 0) + p.assists }); vol.set(p.b, { n: p.bn, v: (vol.get(p.b)?.v ?? 0) + p.assists }); });
    const nodes = [...vol.entries()].sort((x, y) => y[1].v - x[1].v).map(([id, v], i, arr) => {
      const ang = -Math.PI / 2 + (2 * Math.PI * i) / arr.length;
      return { id, name: last(v.n), v: v.v, x: 230 + 165 * Math.cos(ang), y: 220 + 165 * Math.sin(ang) };
    });
    return { top, nodes };
  }, [q.rows]);
  if (!data.top.length) return null;
  const maxV = Math.max(...data.nodes.map((n) => n.v)), maxA = Math.max(...data.top.map((p) => p.assists));
  const pos = new Map(data.nodes.map((n) => [n.id, n]));
  const rad = (v: number) => 5 + 11 * Math.sqrt(v / maxV);
  return (
    <section className="section">
      <h2>Passing connections</h2>
      <div className="grid">
        <div className="c7">
          <Card title="Who assists whom" sub="The 18 most frequent passer → shooter pairs; arrow width: assists. Players around the circle by volume.">
            <svg viewBox="0 0 460 440" style={{ width: "100%", maxWidth: 560, display: "block", margin: "0 auto" }}>
              <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--s1)" /></marker></defs>
              {data.top.map((p, i) => {
                const a = pos.get(p.a)!, b = pos.get(p.b)!;
                const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, dx = b.x - a.x, dy = b.y - a.y, L = Math.hypot(dx, dy) || 1;
                const cx = 230 + (mx - 230) * 0.6 - (dy / L) * L * 0.12, cy = 220 + (my - 220) * 0.6 + (dx / L) * L * 0.12;
                const r = rad(b.v) + 3, ex = b.x - ((b.x - cx) / Math.hypot(b.x - cx, b.y - cy)) * r, ey = b.y - ((b.y - cy) / Math.hypot(b.x - cx, b.y - cy)) * r;
                const top2 = [...p.plays.entries()].sort((x, y) => y[1] - x[1]).slice(0, 2).map(([pl, n]) => `${pl} ${Math.round((100 * n) / p.assists)}%`).join(", ");
                return <path key={i} d={`M${a.x},${a.y} Q${cx},${cy} ${ex},${ey}`} fill="none" stroke="var(--s1)" strokeOpacity={0.55} strokeWidth={1 + (6 * p.assists) / maxA}
                  markerEnd="url(#arr)" onPointerMove={(e) => tip.show(e, <><b>{last(p.an)} → {last(p.bn)}</b>: {p.assists} assists, {fmt.n(p.pts)} points<div className="muted">mostly {top2}</div></>)} onPointerLeave={tip.hide} />;
              })}
              {data.nodes.map((n) => (
                <g key={n.id}>
                  <circle cx={n.x} cy={n.y} r={rad(n.v)} fill="var(--s1)" stroke="var(--surface)" strokeWidth={2} />
                  <text x={n.x} y={n.y - rad(n.v) - 5} textAnchor="middle" fontSize={11.5} fill="var(--text)">{n.name}</text>
                </g>
              ))}
            </svg>
            {tip.node}
          </Card>
        </div>
        <div className="c5">
          <Card title="Top connections">
            <DataTable rows={data.top.slice(0, 10).map((p) => ({ c: `${last(p.an)} → ${last(p.bn)}`, assists: p.assists, pts: p.pts,
              mostly: [...p.plays.entries()].sort((x, y) => y[1] - x[1]).slice(0, 2).map(([pl, n]) => `${pl} ${Math.round((100 * n) / p.assists)}%`).join(", ") }))}
              cols={[{ key: "c", label: "Connection" }, { key: "assists", label: "Ast", num: true }, { key: "pts", label: "Pts", num: true, fmt: (v) => fmt.n(v) }, { key: "mostly", label: "Mostly" }]} />
          </Card>
        </div>
      </div>
    </section>
  );
}

function Turnovers({ season, team, side }: { season: number; team: number; side: string }) {
  const q = useQuery(`select kind, per100, lg_per100, per100_rel from plays.turnovers where season = ${season} and team_id = ${team} and side = '${side}'`);
  const rows = TOV_KINDS.map((k) => q.rows.find((r) => r.kind === k)).filter(Boolean) as Row[];
  if (!rows.length) return null;
  const s = side === "offense" ? -1 : 1;
  return (
    <section className="section">
      <h2>Turnovers{side === "defense" ? " forced" : ""}</h2>
      <Card sub={side === "offense" ? "Per 100 possessions vs league. Blue: fewer turnovers of that kind than league average." : "Per 100 opponent possessions vs league. Blue: forces more of that kind than league average."}>
        <DivBars rows={rows.map((r) => ({ label: r.kind, v: s * r.per100_rel, text: `${fmt.n(r.per100, 1)} (league ${fmt.n(r.lg_per100, 1)})` }))} labelWidth={130} />
      </Card>
    </section>
  );
}
