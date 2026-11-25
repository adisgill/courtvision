import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Link, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { lit, useManifest, useNewVersion, useQuery } from "./db";
import { fmt } from "./lib/format";
import { CURRENT_SEASON } from "./lib/nba";
import { Boundary } from "./ui/Boundary";
import { Icon } from "./ui/icons";

const Ticker = lazy(() => import("./ui/Ticker"));

const pages = {
  Home: lazy(() => import("./pages/Home")),
  Upcoming: lazy(() => import("./pages/Upcoming")),
  Game: lazy(() => import("./pages/Game")),
  Games: lazy(() => import("./pages/Games")),
  Scorecard: lazy(() => import("./pages/Scorecard")),
  League: lazy(() => import("./pages/League")),
  Team: lazy(() => import("./pages/Team")),
  Player: lazy(() => import("./pages/Player")),
  Lineups: lazy(() => import("./pages/Lineups")),
  Plays: lazy(() => import("./pages/Plays")),
  Coaches: lazy(() => import("./pages/Coaches")),
  Market: lazy(() => import("./pages/Market")),
  Draft: lazy(() => import("./pages/Draft")),
  Prospect: lazy(() => import("./pages/Prospect")),
  Live: lazy(() => import("./pages/Live")),
  About: lazy(() => import("./pages/About")),
};

type NavItem = { to: string; label: string; icon: string; badge?: string; desc: string };
export const NAV: { group: string; items: NavItem[] }[] = [
  { group: "Gameday", items: [
    { to: "/", label: "Today", icon: "home", desc: "Tonight's games, last night's results, the ledger" },
    { to: "/upcoming", label: "Predictions", icon: "predict", desc: "Every team's next game: margin, win chance, stat lines" },
    { to: "/scorecard", label: "Scorecard", icon: "target", desc: "Every prediction scored against what happened" },
    { to: "/games", label: "Games", icon: "match", desc: "Every result, with box score ratings and shot maps" },
    { to: "/live", label: "Live", icon: "live", desc: "A game in progress against both teams' seasons" },
  ] },
  { group: "Teams & players", items: [
    { to: "/league", label: "League", icon: "league", desc: "Standings, team ratings and the season's leaders" },
    { to: "/team", label: "Teams", icon: "team", desc: "Ratings, form, roster, shot profile and history" },
    { to: "/player", label: "Players", icon: "player", desc: "Ratings by game and season, impact and shot maps" },
    { to: "/lineups", label: "Lineups", icon: "compare", desc: "Five-man units and pairs, by net rating" },
    { to: "/draft", label: "Draft", icon: "transfer", desc: "College prospects: big board, mock draft, the model's record" },
  ] },
  { group: "Analysis", items: [
    { to: "/plays", label: "Play types", icon: "plays", desc: "How teams score and defend, play by play" },
    { to: "/coaches", label: "Coaches", icon: "manager", desc: "What follows a coach from team to team" },
    { to: "/market", label: "Market", icon: "money", desc: "Contracts against production" },
    { to: "/about", label: "About & data", icon: "info", desc: "Sources, methods and limits" },
  ] },
];

const isActive = (to: string, path: string) => (to === "/" ? path === "/" : path.startsWith(to.split("?")[0]));

/** Top navigation: one button per group, each opening a two-column panel of tools. */
function NavBar() {
  const loc = useLocation();
  const [open, setOpen] = useState<string | null>(null);
  const timer = useRef<number>();
  useEffect(() => setOpen(null), [loc.pathname, loc.search]);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(null); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, []);
  const enter = (g: string) => { window.clearTimeout(timer.current); timer.current = window.setTimeout(() => setOpen(g), 90); };
  const leave = () => { window.clearTimeout(timer.current); timer.current = window.setTimeout(() => setOpen(null), 160); };
  return (
    <nav className="navbar" aria-label="Main">
      {NAV.map((g) => {
        const act = g.items.some((i) => isActive(i.to, loc.pathname));
        return (
          <div key={g.group} className={`navtop ${open === g.group ? "open" : ""} ${act ? "active" : ""}`}
               onPointerEnter={(e) => { if (e.pointerType === "mouse") enter(g.group); }} onPointerLeave={leave}>
            <button aria-expanded={open === g.group} onClick={() => setOpen(open === g.group ? null : g.group)}>
              {g.group}<Icon name="chev" />
            </button>
            {open === g.group && (
              <div className="mega" onPointerEnter={() => window.clearTimeout(timer.current)}>
                {g.items.map((i) => (
                  <Link key={i.to} to={i.to} className={isActive(i.to, loc.pathname) ? "active" : ""}>
                    <Icon name={i.icon} /><b>{i.label}{i.badge && <span className="pill accent">{i.badge}</span>}</b><span>{i.desc}</span>
                  </Link>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </nav>
  );
}

function Sheet({ onClose }: { onClose: () => void }) {
  const loc = useLocation();
  return (
    <>
      <div className="sheet-scrim" onClick={onClose} />
      <div className="sheet" role="dialog" aria-label="Menu">
        <div className="row between">
          <Brand />
          <button className="btn ghost" onClick={onClose} aria-label="Close menu"><Icon name="close" /></button>
        </div>
        {NAV.map((g) => (
          <div key={g.group}>
            <div className="eyebrow">{g.group}</div>
            {g.items.map((i) => (
              <Link key={i.to} to={i.to} onClick={onClose} className={isActive(i.to, loc.pathname) ? "active" : ""}><Icon name={i.icon} />{i.label}</Link>
            ))}
          </div>
        ))}
      </div>
    </>
  );
}

const Brand = () => (
  <Link to="/" className="brand" aria-label="courtvision home">
    <svg viewBox="0 0 32 32" width="26" height="26" aria-hidden="true">
      <rect x="1" y="1" width="30" height="30" rx="3" fill="none" stroke="currentColor" strokeWidth="2" />
      <path d="M1 22h30M9 31V22M23 31V22" stroke="currentColor" strokeWidth="2" fill="none" />
      <path d="M8 22a8 8 0 0 1 16 0" stroke="currentColor" strokeWidth="2" fill="none" />
      <circle cx="16" cy="11" r="3" fill="var(--hi)" />
    </svg>
    court<b>vision</b>
  </Link>
);

function useTheme() {
  const [theme, setTheme] = useState<string | null>(() => { try { return localStorage.getItem("theme"); } catch { return null; } });
  useEffect(() => {
    if (theme) document.documentElement.setAttribute("data-theme", theme);
    else document.documentElement.removeAttribute("data-theme");
    try { if (theme) localStorage.setItem("theme", theme); else localStorage.removeItem("theme"); } catch { /* private mode */ }
  }, [theme]);
  const dark = theme ? theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  return [dark, () => setTheme(dark ? "light" : "dark")] as const;
}

function Palette({ onClose }: { onClose: () => void }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const nav = useNavigate();
  const term = q.trim();
  const like = lit(`%${term}%`);
  const res = useQuery(term.length >= 2 ? `
    select * from (
      select 'team' kind, team_id::varchar id, team name, abbrev sub, 0 o from web.team_names
      where season = ${CURRENT_SEASON} and (team ilike ${like} or abbrev ilike ${like})
      union all
      select 'player', person_id::varchar, player, from_year || '–' || to_year, 1 from web.player_names
      where player ilike ${like} and to_year >= 1996
      union all
      select 'prospect', pid::varchar, player, team || ' · #' || rank, 2 from draft.prospects
      where player ilike ${like}
    ) order by o, length(name) limit 14` : null);
  const pagesHit = NAV.flatMap((g) => g.items).filter((i) => term && i.label.toLowerCase().includes(term.toLowerCase()));
  const items = [
    ...pagesHit.map((p) => ({ label: p.label, sub: "page", to: p.to })),
    ...res.rows.map((r) => ({ label: r.name, sub: `${r.kind} · ${r.sub ?? ""}`,
                              to: `/${r.kind}/${encodeURIComponent(r.id)}` })),
  ];
  const go = (to: string) => { nav(to); onClose(); };
  return (
    <div className="palette-scrim" onMouseDown={onClose}>
      <div className="palette" role="dialog" aria-label="Search" onMouseDown={(e) => e.stopPropagation()}>
        <input autoFocus placeholder="Search players, teams, pages…" value={q}
               onChange={(e) => { setQ(e.target.value); setSel(0); }}
               onKeyDown={(e) => {
                 if (e.key === "Escape") onClose();
                 if (e.key === "ArrowDown") { setSel((s) => Math.min(s + 1, items.length - 1)); e.preventDefault(); }
                 if (e.key === "ArrowUp") { setSel((s) => Math.max(s - 1, 0)); e.preventDefault(); }
                 if (e.key === "Enter" && items[sel]) go(items[sel].to);
               }} />
        <ul role="listbox">
          {items.map((it, i) => (
            <li key={it.to + i} role="option" aria-selected={i === sel} onMouseEnter={() => setSel(i)} onClick={() => go(it.to)}>
              {it.label}<span className="muted">{it.sub}</span>
            </li>
          ))}
          {!items.length && <li className="muted" aria-disabled>{term.length < 2 ? "Type a name: Jokić, Celtics, Wembanyama…" : "No matches"}</li>}
        </ul>
      </div>
    </div>
  );
}

export default function App() {
  const [dark, toggle] = useTheme();
  const fresh = useNewVersion();
  const manifest = useManifest();
  const loc = useLocation();
  const [menu, setMenu] = useState(false);
  const [pal, setPal] = useState(false);
  useEffect(() => { window.scrollTo(0, 0); setMenu(false); }, [loc.pathname]);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if ((e.key === "k" && (e.ctrlKey || e.metaKey)) || (e.key === "/" && !/input|textarea|select/i.test((e.target as HTMLElement).tagName))) {
        e.preventDefault(); setPal(true);
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);
  return (
    <>
      {fresh && <div className="banner" role="status">New data is available. <a href="#" onClick={(e) => { e.preventDefault(); window.location.reload(); }}>Refresh</a></div>}
      <header className="mast">
        <Brand />
        <NavBar />
        <div className="spacer" />
        <button className="btn search-trigger" onClick={() => setPal(true)} aria-label="Search (Ctrl+K)">
          <span className="row" style={{ gap: 6 }}><Icon name="search" size={16} /><span className="label">Players, teams, pages</span></span>
          <span className="kbd">Ctrl K</span>
        </button>
        <button className="btn ghost" onClick={toggle} aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}><Icon name={dark ? "sun" : "moon"} /></button>
        <a className="btn ghost" href="https://github.com/adisgill/courtvision" aria-label="Source on GitHub"><Icon name="github" /></a>
        <button className="btn ghost menu-btn" onClick={() => setMenu(true)} aria-label="Open menu"><Icon name="menu" /></button>
      </header>
      <Suspense fallback={<div className="ticker" />}><Ticker /></Suspense>
      {menu && <Sheet onClose={() => setMenu(false)} />}
      <main className="main">
        <Suspense fallback={<div className="loading">Loading</div>}>
          <div className="route" key={loc.pathname}>
            <Boundary label="This page">
            <Routes>
              <Route path="/" element={<pages.Home />} />
              <Route path="/upcoming" element={<pages.Upcoming />} />
              <Route path="/game/:id" element={<pages.Game />} />
              <Route path="/games" element={<pages.Games />} />
              <Route path="/scorecard" element={<pages.Scorecard />} />
              <Route path="/league" element={<pages.League />} />
              <Route path="/team" element={<pages.Team />} />
              <Route path="/team/:id" element={<pages.Team />} />
              <Route path="/player" element={<pages.Player />} />
              <Route path="/player/:id" element={<pages.Player />} />
              <Route path="/lineups" element={<pages.Lineups />} />
              <Route path="/plays" element={<pages.Plays />} />
              <Route path="/coaches" element={<pages.Coaches />} />
              <Route path="/market" element={<pages.Market />} />
              <Route path="/draft" element={<pages.Draft />} />
              <Route path="/prospect/:pid" element={<pages.Prospect />} />
              <Route path="/live" element={<pages.Live />} />
              <Route path="/about" element={<pages.About />} />
              <Route path="*" element={<p>Page not found. <Link to="/">Home</Link></p>} />
            </Routes>
            </Boundary>
          </div>
        </Suspense>
      </main>
      <footer className="footer">
        <Brand />
        <div>
          Open source at <a href="https://github.com/adisgill/courtvision">github.com/adisgill/courtvision</a> ·
          games through {fmt.dayYear(manifest?.data_through)} · data version <span className="mono">{manifest?.version ?? "–"}</span><br />
          Kaggle "Historical NBA Data and Player Box Scores", NBA.com (stats and the official injury report),
          Basketball-Reference (contracts, coaches) · <Link to="/about">credits & methods</Link> · <Link to="/scorecard">public prediction ledger</Link>
        </div>
      </footer>
      {pal && <Palette onClose={() => setPal(false)} />}
    </>
  );
}
