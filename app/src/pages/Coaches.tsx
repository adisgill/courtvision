// Coaching: which habits follow a coach to a new team (and which are really the roster), the
// season's style map and results, one coach's profile, and coach against coach.
import * as Plot from "@observablehq/plot";
import { useMemo } from "react";
import { lit, Row, useQuery } from "../db";
import { css, fmt, seasonLabel } from "../lib/format";
import { Figure } from "../ui/Figure";
import { DivBars, HeatGrid } from "../ui/grids";
import { Card, PageHead, SeasonSelect, Select, Tile, useParam } from "../ui/kit";

// metric: label, what it measures, direction (+1 higher is better, -1 lower, 0 style)
const METRICS: Record<string, [string, string, number]> = {
  run_stop_pp100: ["Timeouts that stop runs", "opponent points per 100 possessions fewer in the 3 possessions after his timeout than before, vs league", 1],
  ato_pp100_rel: ["After-timeout scoring", "points per 100 plays out of his timeouts, vs league", 1],
  ato_three_rel: ["After-timeout threes", "share of plays out of timeouts that end in a three, vs league", 0],
  ato_tov_rel: ["After-timeout turnovers", "share of plays out of timeouts that end in a turnover, vs league", -1],
  timeouts_pg: ["Timeouts per game", "timeouts called per game", 0],
  pace: ["Pace", "possessions per game vs that season's league average", 0],
  fg3a_rate_rel: ["3-point rate", "share of shots that are threes, vs league", 0],
  tempo_control: ["Tempo control", "how much closer games are played to his usual pace than the opponent's (possessions)", 1],
  starters_min_blowout: ["Starters' minutes in blowouts", "average minutes of the top five in games decided by 20+", 0],
  rotation: ["Rotation depth", "players with 10+ minutes vs league", 0],
  transition_share: ["Transition share", "share of live-ball possessions used in the first 8 seconds", 0],
  transition_pp100_rel: ["Transition scoring", "points per 100 early possessions after a miss or steal, vs league", 1],
  clutch_net100: ["Clutch net", "net points per 100 possessions in the last 5 minutes within 5 points", 1],
  halftime_adj: ["Halftime adjustment", "third-quarter net minus first-half net, per 100", 1],
  bounce_back: ["Bounce-back", "net per 100 after a loss minus after a win", 1],
  challenges_pg: ["Challenges per game", "coach's challenges per game (2019-20 on)", 0],
  net100: ["Team net", "net points per 100 possessions", 1],
  vs_roster: ["Beats roster expectation", "points per game above what last season's player impact over expected minutes predicted", 1],
  vs_pred: ["Beats the prediction", "points per game above the walk-forward pre-game prediction", 1],
};
const RESULTS = ["run_stop_pp100", "ato_pp100_rel", "tempo_control", "clutch_net100", "halftime_adj", "vs_roster_shrunk", "net100"];
const label = (m: string) => METRICS[m.replace(/_shrunk$/, "")]?.[0] ?? m;
const last = (s: string) => String(s ?? "").split(" ").slice(-1)[0];

export default function Coaches() {
  const prof = useQuery(`select p.*, v.vs_roster, v.vs_pred, v.vs_roster_shrunk, v.vs_pred_shrunk from coach.profiles p
                         left join coach.value v using (coach_id, season)`);
  const rel = useQuery(`select * from coach.value_reliability`);
  const asof = useQuery(`select max(as_of) a from coach.current`);
  const seasons = [...new Set(prof.rows.filter((r) => r.season > 0 && r.games >= 60).map((r) => Number(r.season)))].sort((a, b) => b - a);
  const [sp, setSeason] = useParam("season", "");
  const season = Number(sp) || seasons[0] || 0;
  const rows = prof.rows.filter((r) => Number(r.season) === season && r.games >= 20);
  const meanPace = rows.length ? rows.reduce((a, r) => a + r.pace, 0) / rows.length : 0;
  const career = prof.rows.filter((r) => Number(r.season) === 0).sort((a, b) => b.games - a.games);
  const [cp, setCoach] = useParam("coach", "");
  const coach = cp || [...rows].sort((a, b) => b.games - a.games)[0]?.coach_id || "";
  const relRows = rel.rows.filter((r) => r.new_team_corr != null && METRICS[r.metric]).sort((a, b) => b.new_team_corr - a.new_team_corr);
  const h2hRel = rel.rows.find((r) => r.metric === "h2h_vs_pred");
  const relPlot = useMemo(() => relRows.length ? (width: number) => {
    const long = relRows.flatMap((r) => [{ m: label(r.metric), k: "same coach, next season", v: r.same_coach_corr }, { m: label(r.metric), k: "after he changes teams", v: r.new_team_corr }]);
    return { width, height: 28 * relRows.length + 70, marginLeft: 190, x: { label: "correlation", domain: [-0.5, 1], grid: true },
      y: { label: null, domain: relRows.map((r) => label(r.metric)) }, fy: undefined,
      color: { domain: ["same coach, next season", "after he changes teams"], range: [css("--s1"), css("--s2")], legend: true },
      marks: [Plot.ruleX([0], { stroke: css("--axis") }),
              Plot.barX(long, { y: "m", x: "v", fill: "k", insetTop: 3, insetBottom: 3, ...{ dy: 0 } as any }),
              Plot.tip(long, Plot.pointerY({ y: "m", x: "v", title: (d: any) => `${d.m}, ${d.k}: r ${d.v.toFixed(2)}` }))] };
  } : undefined, [relRows]);
  const style = useMemo(() => rows.length ? (width: number) => {
    const pts = rows.map((r) => ({ ...r, pr: r.pace - meanPace, t3: 100 * r.fg3a_rate_rel }));
    return { width, height: 380, x: { label: "pace vs league (possessions per game)", grid: true }, y: { label: "3-point rate vs league (points)", grid: true },
      marks: [Plot.ruleX([0], { stroke: css("--axis") }), Plot.ruleY([0], { stroke: css("--axis") }),
              Plot.dot(pts, { x: "pr", y: "t3", r: 6, fill: (d: any) => (d.coach_id === coach ? css("--hi") : css("--s1")), stroke: css("--surface"), strokeWidth: 1.5 }),
              Plot.text(pts, { x: "pr", y: "t3", text: (d: any) => last(d.coach_name), dy: -11, fontSize: 10.5, fill: css("--text-2") }),
              Plot.tip(pts, Plot.pointer({ x: "pr", y: "t3", title: (d: any) => `${d.coach_name}\npace ${fmt.signed(d.pr, 1)} · 3-point rate ${fmt.signed(d.t3, 1)}` }))] };
  } : undefined, [rows, meanPace, coach]);
  const stats = RESULTS.map((m) => {
    const v = rows.map((r) => r[m]).filter((x) => x != null) as number[];
    const mean = v.reduce((a, b) => a + b, 0) / (v.length || 1);
    const sd = Math.sqrt(v.reduce((a, b) => a + (b - mean) ** 2, 0) / (v.length || 1)) || 1;
    return { m, mean, sd };
  });
  const results = rows.map((r) => ({ r, z: stats.reduce((a, s) => a + (r[s.m] == null ? 0 : (r[s.m] - s.mean) / s.sd), 0) / stats.length }))
    .sort((a, b) => b.z - a.z);
  return (
    <>
      <PageHead eyebrow="Coaches" title="What does a coach control?"
        lead="Coaching habits measured from the play-by-play since 1996-97. A trait of the coach should follow him to a new team; one that stays with the roster isn't his." />
      <section className="section">
        <h2>Which metrics are coaching</h2>
        <div className="grid">
          <div className="c7">{relPlot && <Figure title="Same coach next season, and after he changes teams" subtitle="Correlation of each metric for the same coach in consecutive seasons (40+ games each). Few coaches move, so new-team correlations below about 0.3 can't be told from zero."
            plot={relPlot} />}</div>
          <div className="c5">
            <p className="small ink2">Orange bars that stay long after a move are traits of the coach: pace, timeouts per game, tempo control and three-point rate travel with him.
              Results like net rating and clutch net mostly stay with the roster.</p>
            {h2hRel && <p className="small ink2">Head-to-head records between coaches predicted the next meeting at r {fmt.n(h2hRel.year_to_year, 3)} beyond the model: history alone doesn't.</p>}
          </div>
        </div>
      </section>
      <section className="section">
        <h2>{season ? seasonLabel(season) : ""}</h2>
        <div className="filters">
          {seasons.length > 0 && <SeasonSelect value={season} onChange={(v) => setSeason(String(v))} seasons={seasons} />}
          <Select label="Coach" value={coach} onChange={setCoach} options={career.map((r) => [r.coach_id, `${r.coach_name} (${r.games})`])} />
        </div>
        {asof.rows[0]?.a && <p className="xs muted">Coaches from Basketball-Reference's season page of {asof.rows[0].a}.</p>}
        <div className="grid">
          <div className="c6">{style && <Figure title="Style map" subtitle="Pace and three-point rate against the league that season. Highlighted: the selected coach." plot={style} />}</div>
          <div className="c6">
            <Card title="Results" sub="Each column scaled across the season's coaches (blue above average); coaches by their average. 'Beats roster expectation' covers 2020-21 to 2025-26.">
              <HeatGrid rowHead="Coach" rowWidth={150} compact cols={RESULTS.map((m) => ({ key: m, label: label(m), title: METRICS[m.replace(/_shrunk$/, "")]?.[1] }))}
                rows={results.map(({ r }) => ({ key: r.coach_id, label: r.coach_name, cells: Object.fromEntries(RESULTS.map((m, i) => {
                  const v = r[m];
                  return [m, v == null ? { text: "–", muted: true } : { text: fmt.signed(v, 1), z: Math.max(-1, Math.min(1, (v - stats[i].mean) / stats[i].sd / 2)) }];
                })) }))} />
            </Card>
          </div>
        </div>
      </section>
      {coach && <CoachProfile coach={coach} prof={prof.rows} />}
    </>
  );
}

function CoachProfile({ coach, prof }: { coach: string; prof: Row[] }) {
  const mine = prof.filter((r) => r.coach_id === coach);
  const car = mine.find((r) => Number(r.season) === 0);
  const pool = prof.filter((r) => Number(r.season) === 0 && r.games >= 164);
  const [metric, setMetric] = useParam("metric", "net100");
  const traits = Object.keys(METRICS).filter((m) => car && car[m] != null && m !== "vs_roster" && m !== "vs_pred").map((m) => {
    const v = pool.map((r) => r[m]).filter((x) => x != null) as number[];
    const mean = v.reduce((a, b) => a + b, 0) / (v.length || 1), sd = Math.sqrt(v.reduce((a, b) => a + (b - mean) ** 2, 0) / (v.length || 1)) || 1;
    const dir = METRICS[m][2];
    return { m, z: ((car![m] - mean) / sd) * (dir === 0 ? 1 : dir) * 10, raw: car![m], dir };
  });
  const bySeason = mine.filter((r) => Number(r.season) > 0 && r[metric] != null).sort((a, b) => a.season - b.season);
  const seasonPlot = useMemo(() => bySeason.length ? (width: number) => ({
    width, height: 240, x: { label: null, tickFormat: (v: any) => seasonLabel(Number(v)), type: "band" as const }, y: { label: label(metric), grid: true },
    marks: [Plot.ruleY([0], { stroke: css("--axis") }),
            Plot.barY(bySeason, { x: "season", y: metric, fill: (d: any) => METRICS[metric][2] === 0 ? css("--s1") : (d[metric] * METRICS[metric][2] >= 0 ? css("--pos") : css("--neg")), rx: 3, insetLeft: 3, insetRight: 3 }),
            Plot.tip(bySeason, Plot.pointerX({ x: "season", y: metric, title: (d: any) => `${seasonLabel(d.season)}: ${fmt.signed(d[metric], 2)}` }))],
  }) : undefined, [bySeason, metric]);
  const opps = useQuery(`select opp_coach_id, games from coach.h2h where coach_id = ${lit(coach)} order by games desc limit 40`);
  const [opp, setOpp] = useParam("opp", "");
  const o = opp || opps.rows[0]?.opp_coach_id || "";
  const h = useQuery(o ? `select * from coach.h2h where coach_id = ${lit(coach)} and opp_coach_id = ${lit(o)}` : null).rows[0];
  const meets = useQuery(o ? `select g.game_date, tg.pts - tg.opp_pts margin, t.control from coach.tempo t join coach.team_games tg using (game_id, team_id)
                              join stg.games g using (game_id) where t.coach_id = ${lit(coach)} and t.opp_coach_id = ${lit(o)} order by g.game_date` : null);
  const nameOf = new Map(prof.filter((r) => Number(r.season) === 0).map((r) => [r.coach_id, r.coach_name]));
  const meetPlot = useMemo(() => meets.rows.length ? (width: number) => ({
    width, height: 220, x: { label: null, type: "utc" as const }, y: { label: "margin", grid: true },
    marks: [Plot.ruleY([0], { stroke: css("--axis") }),
            Plot.ruleX(meets.rows, { x: (d: any) => new Date(d.game_date), y1: 0, y2: "margin", stroke: (d: any) => (d.margin >= 0 ? css("--pos") : css("--neg")), strokeWidth: 4 }),
            Plot.tip(meets.rows, Plot.pointerX({ x: (d: any) => new Date(d.game_date), y: "margin", title: (d: any) => `${fmt.dayYear(d.game_date)}: ${fmt.signed(d.margin, 0)} · tempo control ${fmt.signed(d.control, 1)}` }))],
  }) : undefined, [meets.rows]);
  if (!car) return null;
  return (
    <section className="section">
      <h2>{car.coach_name}</h2>
      <div className="tiles">
        <Tile label="Games coached" value={fmt.n(car.games)} delta="since 1996-97" />
        <Tile label="Record" value={`${car.wins}-${car.games - car.wins}`} delta={fmt.pct(car.wins / car.games, 1)} />
        <Tile label="Seasons" value={fmt.n(mine.filter((r) => Number(r.season) > 0).length)} />
        <Tile label="Team net per 100" value={fmt.signed(car.net100, 1)} />
      </div>
      <div className="grid">
        <div className="c6">
          <Card title="Career profile" sub="Rating points against the average coach with 164+ games (10 = one standard deviation; for results, blue is good).">
            <DivBars rows={traits.map((t) => ({ label: METRICS[t.m][0], v: t.z, good: t.dir === 0 ? null : undefined, text: fmt.signed(t.z, 0),
              tip: <>{METRICS[t.m][1]}: {fmt.signed(t.raw, 2)}</> }))} height={22} labelWidth={190} />
          </Card>
        </div>
        <div className="c6">
          <div className="filters"><Select label="Season by season" value={metric} onChange={setMetric} options={Object.entries(METRICS).map(([k, v]) => [k, v[0]])} /></div>
          {seasonPlot && <Figure title={label(metric)} subtitle={`${METRICS[metric][1]}.`} plot={seasonPlot} />}
        </div>
      </div>
      <h3 style={{ marginTop: "1.5rem" }}>Coach vs coach</h3>
      <div className="filters"><Select label="Against" value={o} onChange={setOpp} options={opps.rows.map((r) => [r.opp_coach_id, `${nameOf.get(r.opp_coach_id) ?? r.opp_coach_id} (${r.games})`])} /></div>
      {h && (
        <div className="tiles">
          <Tile label="Record" value={`${h.wins}-${h.games - h.wins}`} />
          <Tile label="Average margin" value={fmt.signed(h.margin, 1)} />
          <Tile label="Tempo control" value={fmt.signed(h.tempo_control, 1)} delta="possessions" />
          <Tile label="Vs prediction" value={fmt.signed(h.vs_pred, 1)} delta={`${fmt.n(h.vs_pred_games)} games`} />
        </div>
      )}
      {meetPlot && <Figure title="Every meeting" subtitle="Regular season margin from his side. The record didn't predict the next meeting beyond the model." plot={meetPlot} />}
    </section>
  );
}
