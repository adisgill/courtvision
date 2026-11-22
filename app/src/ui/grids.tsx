// Heat grids and diverging bars: the two figure types most pages share.
import { ReactNode } from "react";
import { Link } from "react-router-dom";
import { div, fmt, inkOn } from "../lib/format";
import { useWidth } from "./Figure";
import { useTip } from "./kit";

export type GridCol = { key: string; label: string; title?: string };
export type GridCell = { text: ReactNode; z?: number | null; tip?: ReactNode; muted?: boolean };

/** Rows x columns of shaded cells. ``z`` in [-1, 1]: blue good, red bad, none = grey text. */
export function HeatGrid({ rows, cols, rowHead = "", rowWidth = 200, compact }: {
  rows: { key: string; label: ReactNode; to?: string; cells: Record<string, GridCell> }[];
  cols: GridCol[]; rowHead?: string; rowWidth?: number; compact?: boolean;
}) {
  const t = useTip();
  return (
    <div className="table-wrap">
      <table className="data heatgrid" style={{ tableLayout: "auto" }}>
        <thead>
          <tr>
            <th style={{ minWidth: rowWidth }}>{rowHead}</th>
            {cols.map((c) => <th key={c.key} className="c" title={c.title}>{c.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td>{r.to ? <Link to={r.to}>{r.label}</Link> : r.label}</td>
              {cols.map((c) => {
                const cell = r.cells[c.key];
                if (!cell) return <td key={c.key} className="c muted">–</td>;
                const z = cell.z;
                const bg = z == null || Number.isNaN(z) ? "transparent" : div(Math.max(-1, Math.min(1, z)));
                const fg = z == null || Number.isNaN(z) ? undefined : Math.abs(z) > 0.55 ? inkOn(bg) : undefined;
                return (
                  <td key={c.key} className="c" style={{ padding: compact ? "2px 3px" : "3px 4px" }}
                      onPointerMove={cell.tip ? (e) => t.show(e, cell.tip) : undefined} onPointerLeave={cell.tip ? t.hide : undefined}>
                    <span className={`gcell ${cell.muted ? "muted" : ""}`} style={{ background: bg, color: fg }}>{cell.text}</span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {t.node}
    </div>
  );
}

/** 0-100 ratings (50 = league average, 10 = one SD) as a heat grid. */
export function RatingGrid({ rows, cols, rowHead, rowWidth }: {
  rows: { key: string; label: ReactNode; to?: string; values: Record<string, number | null | undefined> }[];
  cols: [string, string][]; rowHead?: string; rowWidth?: number;
}) {
  return (
    <HeatGrid rowHead={rowHead} rowWidth={rowWidth} cols={cols.map(([k, l]) => ({ key: k, label: l }))}
      rows={rows.map((r) => ({
        key: r.key, label: r.label, to: r.to,
        cells: Object.fromEntries(cols.map(([k]) => {
          const v = r.values[k];
          return [k, v == null || Number.isNaN(v) ? { text: "–", muted: true } : { text: Math.round(v), z: (v - 50) / 25,
            tip: <>{Math.round(v)} <span className="muted">(50 = league average, 10 = one SD)</span></> }];
        })),
      }))} />
  );
}

/** Horizontal diverging bars around zero: blue right (good), red left (bad). */
export function DivBars({ rows, unit, height = 26, labelWidth, onClick, max }: {
  rows: { label: ReactNode; v: number; text?: string; tip?: ReactNode; to?: string; good?: boolean | null; key?: string }[];
  unit?: string; height?: number; labelWidth?: number; onClick?: (i: number) => void; max?: number;
}) {
  const t = useTip();
  const [ref, W] = useWidth<HTMLDivElement>();
  const lim = max ?? Math.max(0.5, ...rows.map((r) => Math.abs(r.v))) * 1.15;
  const lab = labelWidth ?? Math.min(220, Math.round(W * 0.34));
  const plotW = W - lab - 16;
  const x0 = lab + 8 + plotW / 2;
  const x = (v: number) => x0 + (v / lim) * (plotW / 2);
  return (
    <div ref={ref}>
      <svg width={W} height={rows.length * height + (unit ? 22 : 6)} role="img" style={{ display: "block", overflow: "visible" }}>
        <line x1={x0} x2={x0} y1={0} y2={rows.length * height} stroke="var(--axis)" />
        {rows.map((r, i) => {
          const y = i * height;
          const good = r.good ?? r.v >= 0;
          const c = good ? "var(--pos)" : "var(--neg)";
          const w = Math.abs(x(r.v) - x0);
          const body = (
            <g key={r.key ?? i} style={{ cursor: onClick || r.to ? "pointer" : undefined }} onClick={onClick ? () => onClick(i) : undefined}
               onPointerMove={r.tip ? (e) => t.show(e, r.tip) : undefined} onPointerLeave={r.tip ? t.hide : undefined}>
              <rect x={0} y={y} width={W} height={height} fill="transparent" />
              <text x={lab} y={y + height / 2 + 4} textAnchor="end" fontSize={12.5} fill="var(--text)">{r.label}</text>
              <rect x={r.v >= 0 ? x0 : x0 - w} y={y + height * 0.2} width={Math.max(1, w)} height={height * 0.6} rx={3} fill={c} />
              <text x={r.v >= 0 ? x0 + w + 5 : x0 - w - 5} y={y + height / 2 + 4} textAnchor={r.v >= 0 ? "start" : "end"} fontSize={11.5}
                    fill="var(--text-2)" className="num">{r.text ?? fmt.signed(r.v, 1)}</text>
            </g>
          );
          return r.to ? <Link key={r.key ?? i} to={r.to}>{body}</Link> : body;
        })}
        {unit && <text x={x0} y={rows.length * height + 16} textAnchor="middle" fontSize={11} fill="var(--muted)">{unit}</text>}
      </svg>
      {t.node}
    </div>
  );
}

/** Horizontal bars from zero with an optional reference tick (league average). */
export function Bars({ rows, height = 26, labelWidth, fmtV = (v) => fmt.n(v, 1), color = "var(--s1)" }: {
  rows: { label: ReactNode; v: number; ref?: number | null; tip?: ReactNode; color?: string; to?: string; key?: string }[];
  height?: number; labelWidth?: number; fmtV?: (v: number) => string; color?: string;
}) {
  const t = useTip();
  const [ref, W] = useWidth<HTMLDivElement>();
  const top = Math.max(1e-9, ...rows.map((r) => Math.max(r.v, r.ref ?? 0))) * 1.12;
  const lab = labelWidth ?? Math.min(220, Math.round(W * 0.34));
  const plotW = W - lab - 56;
  const x = (v: number) => lab + 8 + (v / top) * plotW;
  return (
    <div ref={ref}>
      <svg width={W} height={rows.length * height + 4} style={{ display: "block" }}>
        {rows.map((r, i) => {
          const y = i * height;
          const body = (
            <g key={r.key ?? i} onPointerMove={r.tip ? (e) => t.show(e, r.tip) : undefined} onPointerLeave={r.tip ? t.hide : undefined}>
              <rect x={0} y={y} width={W} height={height} fill="transparent" />
              <text x={lab} y={y + height / 2 + 4} textAnchor="end" fontSize={12.5} fill="var(--text)">{r.label}</text>
              <rect x={lab + 8} y={y + height * 0.22} width={Math.max(1, x(r.v) - lab - 8)} height={height * 0.56} rx={3} fill={r.color ?? color} />
              {r.ref != null && <line x1={x(r.ref)} x2={x(r.ref)} y1={y + 2} y2={y + height - 2} stroke="var(--text)" strokeWidth={2.5} />}
              <text x={x(Math.max(r.v, r.ref ?? 0)) + 6} y={y + height / 2 + 4} fontSize={11.5} fill="var(--text-2)" className="num">{fmtV(r.v)}</text>
            </g>
          );
          return r.to ? <Link key={r.key ?? i} to={r.to}>{body}</Link> : body;
        })}
      </svg>
      {t.node}
    </div>
  );
}
