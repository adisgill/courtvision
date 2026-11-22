// A team: its five dimensions (raw or opponent-adjusted), what drives them, season by season,
// shot zones at both ends, the season trend and the roster by usage and impact.
import * as Plot from "@observablehq/plot";
import { useMemo } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Row, useQuery } from "../db";
import { css, div, fmt, seasonLabel } from "../lib/format";
import { CURRENT_SEASON, DIMENSIONS, useSeasons, useTeamNames } from "../lib/nba";
import { Radar } from "../ui/charts";
import { Figure } from "../ui/Figure";
import { RatingGrid } from "../ui/grids";
import { Card, PageHead, Seg, SeasonSelect, Select, useParam } from "../ui/kit";
import { DimensionCharts, SeasonGrid, seasonRowLabel, ShotZones, StatCol, Trend } from "../ui/profile";

const TEAM_COLS: StatCol[] = [
  { key: "wins", label: "W", dir: 1, fmt: (v) => String(v) }, { key: "losses", label: "L", dir: -1, fmt: (v) => String(v) },
  { key: "pts", label: "PTS", dir: 1, fmt: (v) => v.toFixed(1) },
  { key: "opp", label: "OPP", dir: -1, fmt: (v) => v.toFixed(1) }, { key: "ortg", label: "ORTG", dir: 1, fmt: (v) => v.toFixed(1) },
  { key: "drtg", label: "DRTG", dir: -1, fmt: (v) => v.toFixed(1) }, { key: "net", label: "NET", dir: 1, fmt: (v) => fmt.signed(v, 1) },
  { key: "pace", label: "PACE", dir: 0, fmt: (v) => v.toFixed(1) }, { key: "fg", label: "FG%", dir: 1, fmt: (v) => fmt.pct(v, 1) },
  { key: "fg3", label: "3P%", dir: 1, fmt: (v) => fmt.pct(v, 1), need: "fg3a" }, { key: "ft", label: "FT%", dir: 1, fmt: (v) => fmt.pct(v, 1) },
  { key: "reb", label: "REB", dir: 1, fmt: (v) => v.toFixed(1), need: "dreb" }, { key: "ast", label: "AST", dir: 1, fmt: (v) => v.toFixed(1), need: "ast" },
  { key: "tov", label: "TOV", dir: -1, fmt: (v) => v.toFixed(1), need: "tov" }, { key: "stl", label: "STL", dir: 1, fmt: (v) => v.toFixed(1), need: "stl" },
  { key: "blk", label: "BLK", dir: 1, fmt: (v) => v.toFixed(1), need: "blk" },
];

export default function Team() {
  const { id } = useParams();
  const nav = useNavigate();
  const seasons = useSeasons();
  const [sp, setSeason] = useParam("season", "");
  const season = Number(sp) || seasons[0] || CURRENT_SEASON;
  const [adj, setAdj] = useParam("adj", "0");
  const [cmp, setCmp] = useParam("cmp", "");
  const names = useTeamNames(season);
  const teams = useQuery(`select distinct team_id from metrics.team_season where season = ${season} order by 1`);
  const teamOpts = teams.rows.map((r) => [String(r.team_id), names.get(Number(r.team_id))?.team ?? String(r.team_id)] as [string, string])
    .sort((a, b) => a[1].localeCompare(b[1]));
  if (!id) {
    return (
      <>
        <PageHead eyebrow="Teams" title="Pick a team" lead="Ratings, what drives them, season by season, shot zones at both ends, the season trend and the roster." />
        <div className="toolgrid">
          {teamOpts.map(([tid, name]) => <Link key={tid} to={`/team/${tid}?season=${season}`} className="tool spot"><b>{name}</b><span className="ar">→</span></Link>)}
        </div>
      </>
    );
  }
  const team = Number(id);
  const T = names.get(team);
  return (
    <>
      <PageHead eyebrow={`Team · ${seasonLabel(season)}`} title={T?.team ?? id} lead="0-100 ratings: 50 = league average that season, 10 = one standard deviation." />
      <div className="filters">
        <Select label="Team" value={id} onChange={(v) => nav(`/team/${v}?season=${season}`)} options={teamOpts} />
        {seasons.length > 0 && <SeasonSelect value={season} onChange={(v) => setSeason(String(v))} seasons={seasons} />}
        <Select label="Compare with" value={cmp} onChange={(v) => setCmp(v || null)} options={[["", "—"], ...teamOpts.filter(([t]) => t !== id)]} />
        <div className="field">Ratings<Seg options={[["0", "Raw"], ["1", "Opponent-adjusted"]]} value={adj} onChange={setAdj} /></div>
      </div>
      <Ratings team={team} season={season} adj={adj === "1"} cmp={cmp ? Number(cmp) : null} name={T?.name ?? ""} cmpName={cmp ? names.get(Number(cmp))?.name ?? "" : ""} />
      <Seasons team={team} name={T?.name ?? ""} />
      <section className="section">
        <h2>Shot zones</h2>
        <h3 style={{ margin: "0.5rem 0" }}>Offense</h3>
        <ShotZones who={`the ${T?.name ?? ""}`} prefix="a_" seasons={[season]}
          shotsSql={`select season, x_legacy::double x, y_legacy::double y, made::int made, shot_value from stg.shots
                     where season = ${season} and season_type = 'regular' and team_id = ${team} and x_legacy is not null`}
          countsSql={`select * from metrics.team_season_counts where season = ${season} and season_type = 'regular' and team_id = ${team}`} />
        <h3 style={{ margin: "1.5rem 0 0.5rem" }}>Defense</h3>
        <ShotZones who={`the ${T?.name ?? ""}`} prefix="o_" seasons={[season]} allowed
          shotsSql={`select s.season, s.x_legacy::double x, s.y_legacy::double y, s.made::int made, s.shot_value from stg.shots s join stg.games g using (game_id)
                     where s.season = ${season} and s.season_type = 'regular' and s.team_id <> ${team} and (g.home_team_id = ${team} or g.away_team_id = ${team})
                       and s.x_legacy is not null`}
          countsSql={`select * from metrics.team_season_counts where season = ${season} and season_type = 'regular' and team_id = ${team}`} />
      </section>
      <TrendSection team={team} season={season} />
      <Roster team={team} season={season} />
    </>
  );
}

function Ratings({ team, season, adj, cmp, name, cmpName }: { team: number; season: number; adj: boolean; cmp: number | null; name: string; cmpName: string }) {
  const sql = (t: number) => `select r.measure, r.value, r.rating, r.tier, r.low_sample, a.value_adj, a.rating_adj
    from metrics.ratings_long r join metrics.best_tier b using (level, season, measure, tier)
    left join metrics.team_season_adj a on a.season = r.season and a.team_id = r.team_id and a.measure = r.measure and a.tier = r.tier
    where r.level = 'team_season' and r.season = ${season} and r.team_id = ${t} and r.season_type = 'regular'`;
  const q = useQuery(sql(team));
  const c = useQuery(cmp ? sql(cmp) : null);
  const use = (rows: Row[]) => rows.map((r) => (adj && r.rating_adj != null ? { ...r, value: r.value_adj, rating: r.rating_adj } : r));
  const rows = use(q.rows), crow = use(c.rows);
  const val = (rs: Row[]) => Object.fromEntries(DIMENSIONS.map(([k]) => [k, rs.find((r) => r.measure === k)?.rating ?? null]));
  return (
    <section className="section">
      <h2>Ratings</h2>
      {!q.loading && !rows.length && <p className="muted">No ratings for this selection.</p>}
      <div className="grid">
        <div className="c5">
          <Card title="Five dimensions" sub={`0-100${adj ? ", opponent-adjusted (what the team would post against an average schedule at a neutral site)" : ""}.`}>
            <Radar axes={DIMENSIONS.map(([k, l]) => ({ key: k, label: l }))} series={[
              { name, color: "var(--s1)", values: val(rows) },
              ...(cmp ? [{ name: cmpName, color: "var(--s2)", values: val(crow) }] : []),
            ]} />
          </Card>
        </div>
        <div className="c7"><DimensionCharts rows={rows} /></div>
      </div>
    </section>
  );
}

function Seasons({ team, name }: { team: number; name: string }) {
  const av = useQuery(`select season, stat from metrics.stat_availability where share >= 0.95`);
  const pool = useQuery(`
    with g as (select season, home_team_id team_id, (home_score > away_score)::int win from stg.games where season_type = 'regular' and home_score is not null
               union all select season, away_team_id, (away_score > home_score)::int from stg.games where season_type = 'regular' and home_score is not null),
    rec as (select season, team_id, sum(win) wins, count(*) - sum(win) losses from g group by all)
    select c.season, c.team_id, c.games, r.wins, r.losses, c.b_pts / c.games pts, c.o_b_pts / c.games opp,
           100 * c.b_pts / nullif(c.b_poss, 0) ortg, 100 * c.o_b_pts / nullif(c.o_b_poss, 0) drtg,
           100 * c.b_pts / nullif(c.b_poss, 0) - 100 * c.o_b_pts / nullif(c.o_b_poss, 0) net, (c.b_poss + c.o_b_poss) / 2 / c.games pace,
           c.b_fgm / nullif(c.b_fga, 0) fg, c.b_fg3m / nullif(c.b_fg3a, 0) fg3, c.b_ftm / nullif(c.b_fta, 0) ft,
           (c.b_oreb + c.b_dreb) / c.games reb, c.b_ast / c.games ast, c.b_tov / c.games tov, c.b_stl / c.games stl, c.b_blk / c.games blk
    from metrics.team_season_counts c left join rec r using (season, team_id)
    where c.season_type = 'regular' and c.games > 0
      and c.season in (select season from metrics.team_season_counts where team_id = ${team} and season_type = 'regular')`);
  const hist = useQuery(`select season, name from web.team_names where team_id = ${team}`);
  const nick = new Map(hist.rows.map((r) => [Number(r.season), r.name]));
  const rows = pool.rows.filter((r) => Number(r.team_id) === team).sort((a, b) => b.season - a.season);
  const recorded = new Set(av.rows.map((r) => `${r.season}|${r.stat}`));
  return (
    <section className="section">
      <h2>Season by season</h2>
      <SeasonGrid rows={rows} pool={pool.rows} recorded={recorded} cols={TEAM_COLS}
        label={(r) => seasonRowLabel(Number(r.season), nick.get(Number(r.season)) !== name ? nick.get(Number(r.season)) ?? "" : "", Number(r.season) === CURRENT_SEASON)}
        note={<p className="xs muted">Per game except ORTG, DRTG and NET (points per 100 possessions) and pace (possessions per game). Shading: where the season ranks
          among that season's teams (blue better, red worse; pace isn't judged). A dash: not recorded in the source that season.</p>} />
    </section>
  );
}

function TrendSection({ team, season }: { team: number; season: number }) {
  const q = useQuery(`select r.measure, r.rating, g.game_date from metrics.ratings_long r join metrics.best_tier b using (level, season, measure, tier)
                      join stg.games g using (game_id)
                      where r.level = 'team_game' and r.season = ${season} and r.team_id = ${team} and r.measure in (${DIMENSIONS.map(([k]) => `'${k}'`).join(",")})`);
  if (!q.rows.length) return null;
  return <section className="section"><h2>Season trend</h2><Trend rows={q.rows} title="Game ratings, 10-game mean" /></section>;
}

function Roster({ team, season }: { team: number; season: number }) {
  const q = useQuery(`
    select p.person_id, n.player, p.shooting_rating, p.defense_rating, p.ball_control_rating, p.backcourt_rating, p.frontcourt_rating,
           i.impact_rating presence_rating, i.impact, i.impact_se, i.points_added, 100 * r.usage usage, c.minutes
    from metrics.player_season p join metrics.player_season_counts c using (season, season_type, person_id, team_id)
    join web.player_names n using (person_id)
    left join metrics.player_impact i on i.season = p.season and i.person_id = p.person_id
    left join metrics.player_role r using (season, season_type, person_id, team_id)
    where p.season = ${season} and p.team_id = ${team} and p.season_type = 'regular' order by c.minutes desc`);
  const rows = q.rows;
  const maxMin = Math.max(1, ...rows.map((r) => r.minutes ?? 0));
  const bubble = useMemo(() => rows.filter((r) => r.impact != null && r.usage != null).length ? (width: number) => {
    const pts = rows.filter((r) => r.impact != null && r.usage != null);
    return { width, height: 420, x: { label: "usage (% of team plays while on court)", grid: true }, y: { label: "impact per 100 possessions", grid: true },
      marks: [Plot.ruleY([0], { stroke: css("--axis") }),
              Plot.dot(pts, { x: "usage", y: "impact", r: (d: any) => 4 + 14 * Math.sqrt(d.minutes / maxMin), fill: (d: any) => div(d.impact / 5), stroke: css("--surface"), strokeWidth: 1.5 }),
              Plot.text(pts, { x: "usage", y: "impact", text: (d: any) => String(d.player).split(" ").slice(-1)[0], dy: -14, fontSize: 10.5, fill: css("--text-2") }),
              Plot.tip(pts, Plot.pointer({ x: "usage", y: "impact", title: (d: any) => `${d.player}\nusage ${d.usage.toFixed(1)}%, impact ${fmt.signed(d.impact, 1)} per 100\n${Math.round(d.minutes)} minutes` }))] };
  } : undefined, [rows, maxMin]);
  if (!rows.length) return null;
  return (
    <section className="section">
      <h2>Roster</h2>
      <div className="grid">
        <div className="c6">{bubble && <Figure title="Usage against impact" subtitle="Bubble size: minutes; colour: impact." plot={bubble}
          table={{ rows, cols: [["player", "Player"], ["minutes", "Min", (v) => fmt.n(v)], ["usage", "Usage", (v) => fmt.n(v, 1)], ["impact", "Impact", (v) => fmt.signed(v, 1)]] }} />}</div>
        <div className="c6">
          <Card title="Ratings by player" sub="Season ratings (0-100); presence is per-possession impact on the same scale. By minutes.">
            <RatingGrid rowHead="Player" cols={[...DIMENSIONS, ["presence", "Presence"]]} rows={rows.map((r) => ({ key: String(r.person_id), label: `${r.player} (${Math.round(r.minutes)})`,
              to: `/player/${r.person_id}?season=${season}`, values: { ...Object.fromEntries(DIMENSIONS.map(([k]) => [k, r[`${k}_rating`]])), presence: r.presence_rating } }))} />
          </Card>
        </div>
      </div>
    </section>
  );
}
