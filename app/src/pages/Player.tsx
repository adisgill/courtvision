// A player: ratings and what drives them, season by season, impact per possession over his
// career, the NBA.com advanced set against his peers, his contract, shot zones and game log.
import * as Plot from "@observablehq/plot";
import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Row, useQuery } from "../db";
import { css, fmt, seasonLabel } from "../lib/format";
import { DIMENSIONS, useTeamNames } from "../lib/nba";
import { Radar } from "../ui/charts";
import { Figure } from "../ui/Figure";
import { Card, EntityPicker, PageHead, Seg, SeasonSelect, Tile, useParam } from "../ui/kit";
import { DimensionCharts, SeasonGrid, seasonRowLabel, ShotZones, StatCol, Trend } from "../ui/profile";

const ADVANCED: [string, string, (v: number) => string, 1 | -1 | 0][] = [
  ["off_rating", "Offensive rating", (v) => v.toFixed(1), 1], ["def_rating", "Defensive rating", (v) => v.toFixed(1), -1],
  ["net_rating", "Net rating", (v) => fmt.signed(v, 1), 1], ["ts_pct", "True shooting", (v) => fmt.pct(v, 1), 1],
  ["efg_pct", "Effective FG", (v) => fmt.pct(v, 1), 1], ["usg_pct", "Usage", (v) => fmt.pct(v, 1), 0],
  ["ast_pct", "Assist rate", (v) => fmt.pct(v, 1), 1], ["ast_to", "Assists per turnover", (v) => v.toFixed(2), 1],
  ["ast_ratio", "Assist ratio", (v) => v.toFixed(1), 1], ["tov_ratio", "Turnover ratio", (v) => v.toFixed(1), -1],
  ["oreb_pct", "Offensive rebound rate", (v) => fmt.pct(v, 1), 1], ["dreb_pct", "Defensive rebound rate", (v) => fmt.pct(v, 1), 1],
  ["reb_pct", "Rebound rate", (v) => fmt.pct(v, 1), 1], ["pace", "Pace", (v) => v.toFixed(1), 0], ["pie", "PIE (NBA.com)", (v) => fmt.pct(v, 1), 1],
];

export default function Player() {
  const { id } = useParams();
  const nav = useNavigate();
  if (!id) {
    return (
      <>
        <PageHead eyebrow="Players" title="Find a player" lead="Ratings by game and season (back to 1974-75), per-possession impact, the NBA.com advanced set, contracts and shot maps." />
        <div style={{ maxWidth: 520 }}><EntityPicker kind="player" placeholder="Type a name: Jokić, Wembanyama, Brunson…" onPick={(e) => nav(`/player/${e.id}`)} /></div>
      </>
    );
  }
  return <PlayerPage pid={Number(id)} />;
}

function PlayerPage({ pid }: { pid: number }) {
  const nav = useNavigate();
  const info = useQuery(`select n.player, p.birth_date, p.height_in, p.weight_lb, p.country, p.school, p.draft_year, p.draft_round, p.draft_number
                         from web.player_names n left join stg.players p using (person_id) where n.person_id = ${pid}`);
  const ss = useQuery(`select distinct season from metrics.player_season where person_id = ${pid} order by 1 desc`);
  const seasons = ss.rows.map((r) => Number(r.season));
  const [sp, setSeason] = useParam("season", "");
  const season = Number(sp) || seasons[0] || 0;
  const [cmp, setCmp] = useParam("cmp", "");
  const cmpName = useQuery(cmp ? `select player from web.player_names where person_id = ${Number(cmp)}` : null).rows[0]?.player;
  const team = useQuery(season ? `select team_id from metrics.player_season_counts where person_id = ${pid} and season = ${season}
                                  and season_type = 'regular' and team_id <> 0 order by minutes desc limit 1` : null);
  const names = useTeamNames(season);
  const I = info.rows[0];
  const tname = names.get(Number(team.rows[0]?.team_id))?.team;
  return (
    <>
      <PageHead eyebrow={`Player · ${season ? seasonLabel(season) : ""}${tname ? ` · ${tname}` : ""}`} title={I?.player ?? "…"}
        lead={I ? [I.height_in ? `${Math.floor(I.height_in / 12)}′${I.height_in % 12}″` : null, I.weight_lb ? `${I.weight_lb} lb` : null,
          I.draft_year ? `drafted ${I.draft_year}${I.draft_number ? ` (#${I.draft_number})` : ""}` : null, I.school || I.country].filter(Boolean).join(" · ") : null} />
      <div className="filters">
        {seasons.length > 0 && <SeasonSelect value={season} onChange={(v) => setSeason(String(v))} seasons={seasons} />}
        <div className="field" style={{ minWidth: 260 }}>Compare with<EntityPicker kind="player" value={cmpName ?? ""} placeholder="Another player, same season" onPick={(e) => setCmp(e.id)} /></div>
        <div className="field" style={{ minWidth: 260 }}>Another player<EntityPicker kind="player" placeholder="Open another player" onPick={(e) => nav(`/player/${e.id}`)} /></div>
      </div>
      {season > 0 && <Ratings pid={pid} season={season} cmp={cmp ? Number(cmp) : null} name={I?.player ?? ""} cmpName={cmpName ?? ""} />}
      <Seasons pid={pid} />
      {season > 0 && <Impact pid={pid} season={season} />}
      {season > 0 && <Advanced pid={pid} season={season} />}
      <Market pid={pid} />
      {season > 0 && (
        <section className="section">
          <h2>Shot zones</h2>
          <ShotZones who={I?.player ?? "his"} prefix="a_" seasons={[season]}
            shotsSql={`select season, x_legacy::double x, y_legacy::double y, made::int made, shot_value from stg.shots
                       where season = ${season} and season_type = 'regular' and person_id = ${pid} and x_legacy is not null`}
            countsSql={`select * from metrics.player_season_counts where season = ${season} and season_type = 'regular' and person_id = ${pid} and team_id = 0`} />
          <p className="xs muted">Shot locations and game-level ratings cover the last two seasons on the site; season ratings go back to 1974-75.</p>
        </section>
      )}
      {season > 0 && <GameLog pid={pid} season={season} />}
    </>
  );
}

function Ratings({ pid, season, cmp, name, cmpName }: { pid: number; season: number; cmp: number | null; name: string; cmpName: string }) {
  const sql = (p: number) => `select r.measure, r.value, r.rating, r.tier, r.low_sample from metrics.ratings_long r
    join metrics.best_tier b using (level, season, measure, tier)
    where r.level = 'player_season' and r.person_id = ${p} and r.season = ${season} and r.season_type = 'regular' and r.team_id = 0`;
  const q = useQuery(sql(pid));
  const c = useQuery(cmp ? sql(cmp) : null);
  const imp = useQuery(`select person_id, impact_rating from metrics.player_impact where season = ${season} and person_id in (${pid}${cmp ? `, ${cmp}` : ""})`);
  const vals = (rows: Row[], p: number) => ({ ...Object.fromEntries(DIMENSIONS.map(([k]) => [k, rows.find((r) => r.measure === k)?.rating ?? null])),
    presence: imp.rows.find((r) => Number(r.person_id) === p)?.impact_rating ?? null });
  return (
    <section className="section">
      <h2>Ratings</h2>
      {!q.loading && !q.rows.length && <p className="muted">No season ratings for this selection.</p>}
      <div className="grid">
        <div className="c5">
          <Card title="Five dimensions and presence" sub="Backcourt / Frontcourt are rated only among players at least half guard / half forward-center. Presence is per-possession impact.">
            <Radar axes={[...DIMENSIONS.map(([k, l]) => ({ key: k, label: l })), { key: "presence", label: "Presence" }]} series={[
              { name, color: "var(--s1)", values: vals(q.rows, pid) },
              ...(cmp ? [{ name: cmpName, color: "var(--s2)", values: vals(c.rows, cmp) }] : []),
            ]} />
          </Card>
        </div>
        <div className="c7"><DimensionCharts rows={q.rows} /></div>
      </div>
    </section>
  );
}

function Seasons({ pid }: { pid: number }) {
  const [how, setHow] = useParam("how", "pg");
  const [stype, setStype] = useParam("stype", "regular");
  const pool = useQuery(`
    select c.season, c.person_id, c.games, c.minutes, c.b_pts pts, c.b_oreb + c.b_dreb reb, c.b_ast ast, c.b_stl stl, c.b_blk blk, c.b_tov tov,
           c.b_fgm fgm, c.b_fga fga, c.b_fg3m fg3m, c.b_fg3a fg3a, c.b_ftm ftm, c.b_fta fta
    from metrics.player_season_counts c
    where c.season_type = '${stype}' and c.team_id = 0 and c.games > 0
      and c.season in (select season from metrics.player_season_counts where person_id = ${pid} and season_type = '${stype}' and team_id = 0)`);
  const teams = useQuery(`select season, team_id, minutes from metrics.player_season_counts where person_id = ${pid} and season_type = '${stype}' and team_id <> 0`);
  const tn = useTeamNames(2026);
  const av = useQuery(`select season, stat from metrics.stat_availability where share >= 0.95`);
  const recorded = new Set(av.rows.map((r) => `${r.season}|${r.stat}`));
  const scale = (r: Row) => (how === "tot" ? 1 : how === "p36" ? 36 / Math.max(1, r.minutes) : 1 / r.games);
  const shape = (r: Row): Row => ({ ...r, ...Object.fromEntries(["pts", "reb", "ast", "stl", "blk", "tov"].map((k) => [k, r[k] * scale(r)])),
    min: how === "tot" ? r.minutes : r.minutes / r.games, fg: r.fga ? r.fgm / r.fga : null, fg3: r.fg3a ? r.fg3m / r.fg3a : null,
    ft: r.fta ? r.ftm / r.fta : null, ts: r.fga + 0.44 * r.fta ? r.pts / (2 * (r.fga + 0.44 * r.fta)) : null });
  const maxG = new Map<number, number>();
  pool.rows.forEach((r) => maxG.set(Number(r.season), Math.max(maxG.get(Number(r.season)) ?? 0, r.games)));
  const lg = pool.rows.filter((r) => r.games >= 0.25 * (maxG.get(Number(r.season)) ?? 0)).map(shape);
  const mine = pool.rows.filter((r) => Number(r.person_id) === pid).map(shape).sort((a, b) => b.season - a.season);
  const ab = new Map<number, string>();
  teams.rows.sort((a, b) => b.minutes - a.minutes).forEach((r) => {
    const a = tn.get(Number(r.team_id))?.abbrev ?? "";
    ab.set(Number(r.season), ab.has(Number(r.season)) ? `${ab.get(Number(r.season))}/${a}` : a);
  });
  const d = how === "tot" ? 0 : 1;
  const cols: StatCol[] = [
    { key: "games", label: "GP", dir: 0, fmt: (v) => String(v) }, { key: "min", label: how === "tot" ? "MIN" : "MIN/G", dir: 0, fmt: (v) => v.toFixed(d) },
    { key: "pts", label: "PTS", dir: 1, fmt: (v) => v.toFixed(d) }, { key: "reb", label: "REB", dir: 1, fmt: (v) => v.toFixed(d), need: "dreb" },
    { key: "ast", label: "AST", dir: 1, fmt: (v) => v.toFixed(d), need: "ast" }, { key: "stl", label: "STL", dir: 1, fmt: (v) => v.toFixed(d), need: "stl" },
    { key: "blk", label: "BLK", dir: 1, fmt: (v) => v.toFixed(d), need: "blk" }, { key: "tov", label: "TOV", dir: 0, fmt: (v) => v.toFixed(d), need: "tov" },
    { key: "fg", label: "FG%", dir: 1, fmt: (v) => fmt.pct(v, 1) }, { key: "fg3", label: "3P%", dir: 1, fmt: (v) => fmt.pct(v, 1), need: "fg3a" },
    { key: "ft", label: "FT%", dir: 1, fmt: (v) => fmt.pct(v, 1) }, { key: "ts", label: "TS%", dir: 1, fmt: (v) => fmt.pct(v, 1) },
  ];
  if (!mine.length) return null;
  return (
    <section className="section">
      <h2>Season by season</h2>
      <div className="filters">
        <div className="field">Show<Seg options={[["pg", "Per game"], ["tot", "Totals"], ["p36", "Per 36 min"]]} value={how} onChange={setHow} /></div>
        <div className="field">Games<Seg options={[["regular", "Regular season"], ["playoffs", "Playoffs"]]} value={stype} onChange={setStype} /></div>
      </div>
      <SeasonGrid rows={mine} pool={lg} cols={cols} recorded={recorded} label={(r) => seasonRowLabel(Number(r.season), ab.get(Number(r.season)) ?? "")}
        note={<p className="xs muted">Shading: where the season ranks among that season's players with at least a quarter of the games (blue better, red worse; games,
          minutes and turnovers aren't judged). A dash: not recorded in the source that season.</p>} />
    </section>
  );
}

function Impact({ pid, season }: { pid: number; season: number }) {
  const q = useQuery(`select i.*, r.usage, r.ast_pct, r.minutes from metrics.player_impact i
                      left join metrics.player_role r on r.person_id = i.person_id and r.season = i.season and r.team_id = 0 and r.season_type = 'regular'
                      where i.person_id = ${pid} order by i.season`);
  const cur = q.rows.find((r) => Number(r.season) === season);
  const plot = useMemo(() => q.rows.length > 1 ? (width: number) => ({
    width, height: 300, x: { label: "season (start year)", tickFormat: (v: number) => String(v) }, y: { label: "points per 100 possessions vs league", grid: true },
    marks: [Plot.ruleY([0], { stroke: css("--axis") }),
            Plot.areaY(q.rows, { x: "season", y1: (d: any) => d.impact - d.impact_se, y2: (d: any) => d.impact + d.impact_se, fill: css("--pos"), fillOpacity: 0.18 }),
            Plot.line(q.rows, { x: "season", y: "impact", stroke: css("--s1"), strokeWidth: 2.5 }),
            Plot.dot(q.rows, { x: "season", y: "impact", r: 3.5, fill: css("--s1") }),
            Plot.line(q.rows, { x: "season", y: "box", stroke: css("--s2"), strokeDasharray: "2 3", strokeWidth: 1.5 }),
            Plot.dot(q.rows, { x: "season", y: "season_only", r: 3.5, fill: css("--s3"), fillOpacity: 0.8 }),
            Plot.tip(q.rows, Plot.pointerX({ x: "season", y: "impact", title: (d: any) => `${seasonLabel(d.season)}: impact ${fmt.signed(d.impact, 1)} (± ${fmt.n(d.impact_se, 1)})\nbox-score model ${fmt.signed(d.box, 1)} · plus-minus alone ${fmt.signed(d.season_only, 1)}\n${fmt.n(d.poss)} possessions` }))],
  }) : undefined, [q.rows]);
  if (!q.rows.length) return null;
  return (
    <section className="section">
      <h2>Impact per possession</h2>
      {cur ? (
        <div className="tiles">
          <Tile label="Impact per 100 possessions" value={fmt.signed(cur.impact, 1)} delta="vs league average; offense + defense" />
          <Tile label="Offense / defense" value={`${fmt.signed(cur.impact_off, 1)} / ${fmt.signed(cur.impact_def, 1)}`} />
          <Tile label="Uncertainty" value={`± ${fmt.n(cur.impact_se, 1)}`} delta="one standard error" />
          <Tile label="Points added" value={fmt.signed(cur.points_added, 0)} delta={cur.minutes ? `${fmt.signed((cur.points_added / cur.minutes) * 36, 1)} per 36 minutes` : undefined} />
        </div>
      ) : <p className="muted small">No lineup data for this season.</p>}
      {cur && <p className="small ink2">{fmt.n(cur.poss)} possessions on court.{cur.usage != null ? ` Usage ${fmt.pct(cur.usage, 1)} of team plays; assists on ${fmt.pct(cur.ast_pct, 1)} of teammates' baskets while on court.` : ""}</p>}
      {plot && <Figure title="Impact by season" subtitle="Blue: impact (band ± 1 standard error), the plus-minus regression with two starting points, last season's impact and what the box score says. Dotted orange: the box-score model. Green dots: this season's plus-minus alone."
        plot={plot} table={{ rows: q.rows, cols: [["season", "Season", (v) => seasonLabel(v)], ["impact", "Impact", (v) => fmt.signed(v, 1)], ["impact_se", "±", (v) => fmt.n(v, 1)], ["box", "Box", (v) => fmt.signed(v, 1)], ["poss", "Possessions", (v) => fmt.n(v)]] }} />}
    </section>
  );
}

function Advanced({ pid, season }: { pid: number; season: number }) {
  const lg = useQuery(`select * from metrics.player_advanced where season = ${season} and team_id = 0 and season_type = 'regular' and possessions >= 1000`);
  const me = useQuery(`select * from metrics.player_advanced where season = ${season} and team_id = 0 and season_type = 'regular' and person_id = ${pid}`);
  const m = me.rows[0];
  if (!m || !lg.rows.length) return null;
  const rows = ADVANCED.filter(([k]) => m[k] != null).map(([k, label, f, dir]) => {
    const vs = lg.rows.map((r) => r[k]).filter((v) => v != null);
    const pct = (100 * vs.filter((v) => v < m[k]).length) / vs.length;
    const good = dir === -1 ? 100 - pct : pct;
    return { k, label, v: m[k], f, dir, pct, good };
  });
  return (
    <section className="section">
      <h2>Advanced stats</h2>
      <Card title="Against players with 1,000+ possessions" sub="Computed from our play-by-play counts and lineups (validated against NBA.com's own numbers: ratings, usage and assist rate agree at r 0.99+). PIE is NBA.com's official figure.">
        <div className="advbars">
          {rows.map((r) => (
            <div key={r.k} className="advrow">
              <span className="l">{r.label}</span>
              <span className="b"><span style={{ width: `${Math.max(2, r.good)}%`, background: r.dir === 0 ? "var(--axis)" : r.good >= 50 ? "var(--pos)" : "var(--neg)" }} /><i /></span>
              <span className="v num">{r.f(r.v)} <span className="muted">· {r.dir === 0 ? `${Math.round(r.pct)}th percentile` : `better than ${Math.round(r.good)}%`}</span></span>
            </div>
          ))}
        </div>
      </Card>
    </section>
  );
}

function Market({ pid }: { pid: number }) {
  const q = useQuery(`select * from market.players where person_id = ${pid}`);
  const m = q.rows[0];
  if (!m) return null;
  return (
    <section className="section">
      <h2>Contract and market{m.season ? ` (${seasonLabel(m.season)})` : ""}</h2>
      <div className="tiles">
        <Tile label="Salary" value={m.salary != null ? fmt.usd(m.salary) : m.free_agent ? "unsigned" : "unknown"} delta={m.contract_kind ?? undefined} />
        <Tile label="Estimated fair salary" value={fmt.usd(m.fair_salary)} delta={m.fair_p10 != null ? `${fmt.usd(m.fair_p10)} to ${fmt.usd(m.fair_p90)}` : undefined} />
        <Tile label="Wins above replacement" value={fmt.n(m.war, 1)} delta={m.war_p10 != null ? `bad case ${fmt.n(m.war_p10, 1)} · good case ${fmt.n(m.war_p90, 1)}` : undefined} />
        <Tile label="Risk" value={m.risk ?? "–"} delta={m.overpay_prob != null ? `${fmt.pct(m.overpay_prob)} chance he's overpaid` : undefined} />
      </div>
      <p className="small ink2">{m.status ? <><b>Contract:</b> {m.status}{m.through ? `; runs through ${seasonLabel(m.through)}` : ""}{m.years_left ? ` (${m.years_left} season${m.years_left > 1 ? "s" : ""}, ${fmt.usd(m.total_left)} left)` : ""}. </> : null}
        Fair salary prices his projected impact like comparable players' contracts, capped at the max for his service; the box-score view prices the box score alone ({fmt.usd(m.fair_box)}).
        {m.risk_flags?.length ? ` Risk factors: ${m.risk_flags.join(", ")}.` : ""}</p>
    </section>
  );
}

function GameLog({ pid, season }: { pid: number; season: number }) {
  const q = useQuery(`select r.measure, r.rating, g.game_date from metrics.ratings_long r join metrics.best_tier b using (level, season, measure, tier)
                      join stg.games g using (game_id)
                      where r.level = 'player_game' and r.person_id = ${pid} and r.season = ${season} and r.measure in ('shooting', 'defense', 'ball_control')`);
  if (!q.rows.length) return null;
  return <section className="section"><h2>Game log</h2><Trend rows={q.rows} title="Game ratings, 10-game mean" /></section>;
}
