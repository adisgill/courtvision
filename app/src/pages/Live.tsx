import { Link } from "react-router-dom";
import { Card, PageHead } from "../ui/kit";

export default function Live() {
  return (
    <>
      <PageHead eyebrow="Live" title="A game in progress"
        lead="Live mode rates a game while it's being played against both teams' seasons: every possession from the NBA's live play-by-play, run through the same counts and ratings as the full pipeline, with the biggest differences called out." />
      <div className="grid">
        <div className="c7">
          <Card title="Run it locally">
            <p className="small ink2">The NBA's live feed doesn't allow requests from other websites, and parsing a game's possessions needs the Python pipeline, so live
              mode runs on your own machine rather than on this static site:</p>
            <pre className="mono small" style={{ background: "var(--surface-2)", padding: 12, borderRadius: 4, overflowX: "auto" }}>{`uv run courtvision app        # the full app, then open Live
uv run courtvision live       # today's games in the terminal
uv run courtvision live --game-id 0022600287`}</pre>
            <p className="xs muted">It polls the feed at most every 10 seconds per game and caches every response. Setup: see the project's README.</p>
          </Card>
        </div>
        <div className="c5">
          <Card title="Meanwhile">
            <p className="small ink2">Every game today has a full preview with the forecast, the scouting report and each player's stat line: <Link to="/upcoming">predictions</Link>.
              Once it's final, the same page becomes the recap, graded against what we logged before tip-off.</p>
          </Card>
        </div>
      </div>
    </>
  );
}
