// A prospect: his projection and what it rests on, his strengths on the dimensions teams
// need, the past prospects most like him and how they turned out, his college seasons, and
// the teams in range he fits best.
import { useMemo } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Row, useQuery } from "../db";
import { fmt, seasonLabel } from "../lib/format";
import { DIMS, FEATURE_LABEL, TIER_LABEL, height, pct1, possessive } from "../lib/draft";
import { useTeamNames } from "../lib/nba";
import { DivBars } from "../ui/grids";
import { Card, DataTable, Empty, Loading, PageHead, ProbBar, Tile } from "../ui/kit";

export default function Prospect() {
  const { pid } = useParams();
  const nav = useNavigate();
  const q = useQuery(pid ? `select * from draft.prospects where pid = ${Number(pid)}` : null);
  const p = q.rows[0];
  if (!pid) { nav("/draft"); return null; }
  if (q.loading || q.error) return <Loading state={q} />;
  if (!p) return <Empty>This player isn't on the current board (the top 150). <Link to="/draft">Back to the draft</Link></Empty>;
  return <ProspectPage p={p} />;
}

function ProspectPage({ p }: { p: Row }) {
  const info = useQuery(`select draft_year, nba_season, fit_weight, first_round_rotation from draft.info`).rows[0];
  const comps = useQuery(`select * from draft.comps where pid = ${p.pid} order by rank`);
  const seasons = useQuery(`select * from draft.prospect_seasons where pid = ${p.pid} order by season`);
  // each pick under the team projected to own it (traded picks included)
  const fits = useQuery(`select o.owner_id as team_id, o.team_id as original_id, f.fit, o.exp_pick, o.seed
                         from draft.order o join draft.fit f on f.team_id = o.owner_id and f.pid = ${p.pid}
                         order by f.fit desc, abs(o.exp_pick - ${Number(p.rank)})`);
  const teams = useQuery(`select * from market.teams`);
  const names = useTeamNames(Number(info?.nba_season ?? 2026));
  const teamRow = useMemo(() => new Map(teams.rows.map((r) => [Number(r.team_id), r])), [teams.rows]);
  const drivers = useMemo(() => { try { return JSON.parse(p.drivers ?? "[]") as { feature: string; push: number }[]; } catch { return []; } }, [p.drivers]);
  // teams likely to pick within reach of his board rank, by fit
  const reach = fits.rows.filter((r) => Math.abs(Number(r.exp_pick) - Number(p.rank)) <= 6)
    .filter((r, i, all) => all.findIndex((x) => x.team_id === r.team_id) === i).slice(0, 5);
  return (
    <>
      <PageHead eyebrow={<><Link to="/draft">Draft</Link> · #{p.rank} on the board</>} title={p.player}
        lead={<>{p.team} ({p.conf}) · {p.class} · {p.role} · {height(p.height_in)} · {fmt.n(p.age, 1)} at the {info?.draft_year ?? ""} draft{p.dob_known ? "" : " (estimated)"}
          {p.rec_rank != null && !Number.isNaN(p.rec_rank) ? <> · recruiting score {fmt.n(p.rec_rank, 1)}</> : <> · unrated out of high school</>}</>} />
      <div className="tiles">
        <Tile label="Expected WAR" value={fmt.n(p.ev, 1)} delta="over his first four NBA seasons" />
        {TIER_LABEL.map(([k, l, d]) => <Tile key={k} label={l} value={fmt.pct(p[k], p[k] < 0.1 ? 1 : 0)} delta={d} />)}
      </div>
      <div className="grid">
        <div className="c6">
          <Card title="What the projection rests on" sub="The four inputs that move his chance of becoming a rotation player most, against an average college player (log-odds).">
            <DivBars rows={drivers.map((d) => ({ label: FEATURE_LABEL[d.feature] ?? d.feature, v: d.push, text: fmt.signed(d.push, 2) }))} labelWidth={170} />
          </Card>
        </div>
        <div className="c6">
          <Card title="Strengths" sub="Against the rest of the board (z-scores) on the five dimensions team needs are measured on.">
            <DivBars rows={DIMS.map(([k, l]) => ({ label: l, v: Number(p[k] ?? 0), text: fmt.signed(p[k], 1) }))} labelWidth={120} max={2.5} />
          </Card>
        </div>
      </div>
      <div className="grid">
        <div className="c7">
          <Card title="The past prospects most like him" sub="Closest final college seasons (age, size, production, efficiency, recruiting) among players who were drafted or reached the NBA, and what they did in their first four seasons.">
            <DataTable rows={comps.rows} cols={[
              { key: "comp_player", label: "Player", fmt: (v, r) => r.comp_person_id ? <Link to={`/player/${r.comp_person_id}`}>{v}</Link> : v },
              { key: "comp_team", label: "School" }, { key: "comp_year", label: "Draft", num: true },
              { key: "comp_pick", label: "Pick", num: true, fmt: (v) => (v == null || Number.isNaN(v) ? "undrafted" : fmt.n(v)) },
              { key: "comp_war", label: "WAR", num: true, heat: "pos", fmt: (v) => fmt.n(v, 1) },
              { key: "distance", label: "Distance", num: true, fmt: (v) => fmt.n(v, 2), title: "Average standardized gap across the inputs (0 = identical)" },
            ]} />
          </Card>
        </div>
        <div className="c5">
          <Card title="Teams in range he fits" sub="Teams projected to own a pick within six spots of his board rank, by how well his strengths answer their needs.">
            {reach.length ? reach.map((r) => {
              const label = Object.fromEntries(DIMS);
              const needs = ((teamRow.get(Number(r.team_id))?.needs ?? []) as string[]).map((d) => (label[d] ?? d).toLowerCase());
              return (
                <div key={r.team_id} className="small" style={{ padding: "5px 0", borderBottom: "1px solid var(--rule)" }}>
                  <Link to={`/team/${r.team_id}`}><b>{names.get(Number(r.team_id))?.team ?? r.team_id}</b></Link>
                  <span className="muted"> · expected pick {fmt.n(r.exp_pick, 1)}{Number(r.original_id) !== Number(r.team_id)
                    ? ` (${possessive(names.get(Number(r.original_id))?.team ?? "another team")})` : ""}</span>
                  <div className="xs ink2">fit {fmt.signed(r.fit, 2)}{needs.length ? ` · needs ${needs.join(", ")}` : " · no clear need"}</div>
                </div>
              );
            }) : <p className="small muted">No team is projected to pick near his board rank.</p>}
          </Card>
        </div>
      </div>
      <Card title="College seasons" className="flat">
        <DataTable rows={seasons.rows} cols={[
          { key: "season", label: "Season", fmt: (v, r) => <>{seasonLabel(Number(v))}{r.as_of ? <span className="muted"> · through {fmt.day(r.as_of)}</span> : null}</> },
          { key: "team", label: "School" }, { key: "class", label: "Class", center: true }, { key: "gp", label: "G", num: true },
          { key: "mpg", label: "Min", num: true, fmt: (v) => fmt.n(v, 1) }, { key: "pts", label: "Pts", num: true, fmt: (v) => fmt.n(v, 1) },
          { key: "treb", label: "Reb", num: true, fmt: (v) => fmt.n(v, 1) }, { key: "ast", label: "Ast", num: true, fmt: (v) => fmt.n(v, 1) },
          { key: "stl", label: "Stl", num: true, fmt: (v) => fmt.n(v, 1) }, { key: "blk", label: "Blk", num: true, fmt: (v) => fmt.n(v, 1) },
          { key: "ts", label: "TS%", num: true, fmt: (v) => fmt.n(v, 1) }, { key: "fg3_pct", label: "3P%", num: true, fmt: pct1 },
          { key: "fg3a", label: "3PA", num: true }, { key: "ft_pct", label: "FT%", num: true, fmt: pct1 },
          { key: "usg", label: "Usage", num: true, fmt: (v) => fmt.n(v, 1) }, { key: "ast_pct", label: "Ast%", num: true, fmt: (v) => fmt.n(v, 1) },
          { key: "tov_pct", label: "TO%", num: true, fmt: (v) => fmt.n(v, 1) }, { key: "blk_pct", label: "Blk%", num: true, fmt: (v) => fmt.n(v, 1) },
          { key: "bpm", label: "BPM", num: true, fmt: (v) => fmt.signed(v, 1) },
        ]} />
        <p className="xs muted">Barttorvik season lines. Rates per 100 possessions; BPM: box plus-minus against an average college player.</p>
      </Card>
      <Card title="Read with care" className="flat">
        <p className="small ink2">A projection from college box scores and recruiting only. It doesn't see workouts, medicals or film, and it's built on careers since 2007-08.
          Early in the season it leans on last season (for returning players) and on what players of his recruiting level usually do (for freshmen).
          {" "}<ProbBar p={p.p_rotation} width={90} /> is his chance of 4+ WAR in four seasons; for scale, {fmt.pct(info?.first_round_rotation)} of first-round picks from college since 2008 reached it.</p>
      </Card>
    </>
  );
}
