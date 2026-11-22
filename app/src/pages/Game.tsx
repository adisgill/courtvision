// A game: the full preview while it's upcoming (forecast.games), the recap once it's played.
import { useParams } from "react-router-dom";
import { lit, useQuery } from "../db";
import { Loading } from "../ui/kit";
import Preview from "./game/Preview";
import Recap from "./game/Recap";

export default function Game() {
  const { id = "" } = useParams();
  const q = useQuery(`select (select count(*) from forecast.games where game_id = ${lit(id)}) upcoming,
                             (select count(*) from stg.games where game_id = ${lit(id)} and home_score is not null) played,
                             (select count(*) from forecast.scorecard_games where game_id = ${lit(id)}) scored`);
  const r = q.rows[0];
  if (!r) return <Loading state={q} />;
  if (r.upcoming > 0 && !(r.scored > 0)) return <Preview id={id} />;
  if (r.played > 0 || r.scored > 0) return <Recap id={id} />;
  return <p className="muted">No game with id {id} in this data (games come from the current and last season).</p>;
}
