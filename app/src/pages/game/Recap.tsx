// The recap of a played game: the prediction against the final, each team's ratings tonight
// against its season average, the players, and each predicted stat line against the box score.
import { lit, Row, useQuery } from "../../db";
import { fmt, seasonLabel } from "../../lib/format";
import { DIMENSIONS, isRateMeasure, labelOf, ordered, rangeText, STATS, useTeamNames } from "../../lib/nba";
import { Radar } from "../../ui/charts";
import { DivBars, HeatGrid, RatingGrid } from "../../ui/grids";
import { Card, PageHead, Tile } from "../../ui/kit";
import { marginRange, tipDay } from "../Home";

export default function Recap({ id }: { id: string }) {
  const gq = useQuery(`select g.game_id, g.season, g.game_date, g.home_team_id, g.away_team_id, g.home_score, g.away_score, g.arena_name
                       from stg.games g where g.game_id = ${lit(id)}`);
  const sc = useQuery(`select * from forecast.scorecard_games where game_id = ${lit(id)}`);
  const g = gq.rows[0], s = sc.rows[0];
  const base = g ?? s;
  const season = g ? Number(g.season) : s ? new Date(s.game_date).getUTCFullYear() - (new Date(s.game_date).getUTCMonth() < 8 ? 1 : 0) : 0;
  const names = useTeamNames(season);
  if (!base) return <p className="loading">Loading…</p>;
  const home = Number(base.home_team_id), away = Number(base.away_team_id);
  const H = names.get(home), A = names.get(away);
  const hs = g?.home_score ?? s?.home_score, as = g?.away_score ?? s?.away_score;
  return (
    <>
      <PageHead eyebrow={`${tipDay(base.game_date ?? base.tipoff)} · ${seasonLabel(season)} · final`}
        title={<>{A?.team} <span className="disp">{as}</span> <span className="muted">at</span> {H?.team} <span className="disp">{hs}</span></>}
        lead={g?.arena_name ? g.arena_name : undefined} />
      {s && (
        <div className="tiles">
          <Tile label="Our pick" value={s.winner_right ? "✓ right" : "✗ wrong"} delta={`${s.pred_margin >= 0 ? H?.name : A?.name} at ${fmt.pct(Math.max(s.home_win_prob, 1 - s.home_win_prob))}`} />
          <Tile label="Predicted margin" value={`${s.pred_margin >= 0 ? H?.abbrev : A?.abbrev} by ${fmt.n(Math.abs(s.pred_margin), 1)}`}
            delta={`final: ${s.actual_margin >= 0 ? H?.abbrev : A?.abbrev} by ${fmt.n(Math.abs(s.actual_margin))}`} />
          <Tile label="Miss" value={`${fmt.n(Math.abs(s.error), 1)} pts`} delta={s.margin_inside_80 ? "inside the 80% range" : "outside the 80% range"} />
          <Tile label="Brier" value={fmt.n(s.brier, 3)} delta="0 perfect · 0.25 a coin flip" />
        </div>
      )}
      {g ? <Ratings id={id} season={season} home={home} away={away} hn={H?.name ?? "Home"} an={A?.name ?? "Away"} /> :
        <p className="small muted">Team and player ratings appear once the nightly data refresh has the game.</p>}
      {s && <LinesVsBox id={id} home={home} away={away} hn={H?.name ?? "Home"} an={A?.name ?? "Away"} />}
      {s && <p className="xs muted">Prediction logged {fmt.dayYear(s.predicted_at)} from games through {fmt.dayYear(s.data_through)}
        {s.margin_lo != null ? `; 80% range ${marginRange(s.margin_lo, s.margin_hi, H?.abbrev ?? "", A?.abbrev ?? "")}` : ""}.</p>}
    </>
  );
}

function Ratings({ id, season, home, away, hn, an }: { id: string; season: number; home: number; away: number; hn: string; an: string }) {
  const r = useQuery(`select r.team_id, r.measure, r.rating from metrics.ratings_long r
                      join metrics.best_tier b using (level, season, measure, tier)
                      where r.level = 'team_game' and r.game_id = ${lit(id)}`);
  const avg = useQuery(`select r.team_id, r.measure, avg(r.rating) season_avg from metrics.ratings_long r
                        join metrics.best_tier b using (level, season, measure, tier)
                        where r.level = 'team_game' and r.season = ${season} and r.season_type = 'regular' and r.team_id in (${home}, ${away})
                        group by all`);
  const pl = useQuery(`select g.*, c.minutes, n.player from metrics.player_game g
                       join metrics.player_game_counts c using (game_id, person_id, team_id) join web.player_names n using (person_id)
                       where g.game_id = ${lit(id)} and c.minutes >= 8 order by g.team_id, c.minutes desc`);
  const A = new Map(avg.rows.map((x) => [`${x.team_id}|${x.measure}`, x.season_avg]));
  const axes = DIMENSIONS.map(([k, l]) => ({ key: k, label: l }));
  return (
    <>
      <section className="section">
        <h2>Tonight against the season</h2>
        <div className="grid">
          {([[away, an, "var(--s2)"], [home, hn, "var(--s1)"]] as [number, string, string][]).map(([t, name, color]) => {
            const rows = r.rows.filter((x) => Number(x.team_id) === t);
            const M = new Map(rows.map((x) => [x.measure, x.rating]));
            const deltas = ordered(rows.filter((x) => !isRateMeasure(x.measure) && A.has(`${t}|${x.measure}`))
              .map((x) => ({ measure: x.measure, v: x.rating - (A.get(`${t}|${x.measure}`) as number) })));
            return (
              <div key={t} className="c6">
                <Card title={name} sub="Tonight's 0-100 ratings against the team's average game this season; bars: tonight minus the average (blue = better than usual).">
                  <Radar axes={axes} size={300} series={[
                    { name: "tonight", color, values: Object.fromEntries(DIMENSIONS.map(([k]) => [k, M.get(k) ?? null])) },
                    { name: "season average", color: "var(--muted)", values: Object.fromEntries(DIMENSIONS.map(([k]) => [k, A.get(`${t}|${k}`) as number ?? null])) },
                  ]} />
                  <DivBars rows={deltas.map((d) => ({ label: labelOf(d.measure), v: d.v, text: fmt.signed(d.v, 0) }))} height={21} unit="rating tonight minus season average" />
                </Card>
              </div>
            );
          })}
        </div>
      </section>
      {pl.rows.length > 0 && (
        <section className="section">
          <h2>Players</h2>
          <div className="grid">
            {([[away, an], [home, hn]] as [number, string][]).map(([t, name]) => (
              <div key={t} className="c6">
                <Card title={`${name} (8+ minutes)`}>
                  <RatingGrid rowHead="Player" cols={DIMENSIONS} rows={pl.rows.filter((x: Row) => Number(x.team_id) === t).map((x) => ({
                    key: String(x.person_id), label: `${x.player} (${Math.round(x.minutes)} min)`, to: `/player/${x.person_id}`,
                    values: Object.fromEntries(DIMENSIONS.map(([k]) => [k, x[`${k}_rating`]])) }))} />
                </Card>
              </div>
            ))}
          </div>
        </section>
      )}
    </>
  );
}

function LinesVsBox({ id, home, away, hn, an }: { id: string; home: number; away: number; hn: string; an: string }) {
  const q = useQuery(`select s.*, n.player from forecast.scorecard_players s join web.player_names n using (person_id)
                      where s.game_id = ${lit(id)} and s.played`);
  if (!q.rows.length) return null;
  const by = new Map<number, Row[]>();
  q.rows.forEach((r) => by.set(Number(r.person_id), [...(by.get(Number(r.person_id)) ?? []), r]));
  return (
    <section className="section">
      <h2>Stat lines against the box score</h2>
      <p className="small ink2" style={{ marginTop: -6 }}>Each cell: what he got, and the predicted 80% range. Blue inside the range, red outside.</p>
      <div className="grid">
        {([[away, an], [home, hn]] as [number, string][]).map(([t, name]) => {
          const ppl = [...by.entries()].filter(([, rs]) => Number(rs[0].team_id) === t)
            .sort((a, b) => (b[1].find((x) => x.stat === "pts")?.pred ?? 0) - (a[1].find((x) => x.stat === "pts")?.pred ?? 0));
          return (
            <div key={t} className="c6">
              <Card title={name}>
                <HeatGrid rowHead="Player" rowWidth={140} compact cols={STATS.map(([k, l]) => ({ key: k, label: l.split(" ")[0] }))}
                  rows={ppl.map(([pid, rs]) => ({ key: String(pid), label: rs[0].player, to: `/player/${pid}`,
                    cells: Object.fromEntries(rs.map((x) => [x.stat, { text: <>{x.actual} <span className="xs">({rangeText(x.lo, x.hi)})</span></>, z: x.inside ? 0.45 : -0.45,
                      tip: <>predicted {fmt.n(x.pred, 1)} ({rangeText(x.lo, x.hi)}), got {x.actual} in {Math.round(x.minutes)} min</> }])) }))} />
              </Card>
            </div>
          );
        })}
      </div>
    </section>
  );
}
