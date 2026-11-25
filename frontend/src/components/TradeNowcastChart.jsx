import React, { useMemo, useState } from "react";

const LINE_COLORS = ["#38bdf8", "#10b981", "#f59e0b", "#f472b6", "#a78bfa", "#fb7185"];

export default function TradeNowcastChart({ rows }) {
  const [hoveredPass, setHoveredPass] = useState(null);

  const { passes, series, maxTrade } = useMemo(() => {
    const passOrder = [];
    const byPort = new Map();
    let maxVal = 10;

    for (const row of rows || []) {
      if (!passOrder.includes(row.pass_time_ist)) {
        passOrder.push(row.pass_time_ist);
      }
      if (!byPort.has(row.port_id)) {
        byPort.set(row.port_id, {
          portId: row.port_id,
          portName: row.port_name,
          points: {},
        });
      }
      byPort.get(row.port_id).points[row.pass_time_ist] = row.estimated_trade_value_inr_cr;
      if (row.estimated_trade_value_inr_cr > maxVal) {
        maxVal = row.estimated_trade_value_inr_cr;
      }
    }

    return {
      passes: passOrder,
      series: Array.from(byPort.values()),
      maxTrade: Math.ceil(maxVal * 1.15),
    };
  }, [rows]);

  if (!series.length || !passes.length) {
    return <p className="muted">No trade nowcasting data available for current swath.</p>;
  }

  const width = 520;
  const height = 235;
  const pad = { top: 20, right: 20, bottom: 38, left: 48 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const xForIdx = (idx) =>
    passes.length === 1 ? pad.left + plotW / 2 : pad.left + (idx / (passes.length - 1)) * plotW;
  const yForVal = (val) => pad.top + plotH - (val / maxTrade) * plotH;

  return (
    <div>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        style={{ display: "block", inlineSize: "100%", blockSize: "auto", background: "#0b1324", borderRadius: "8px", border: "1px solid #243554" }}
        role="img"
        aria-label="Multi-pass regional trade nowcasting chart in INR Crores"
      >
        {[0, 0.25, 0.5, 0.75, 1].map((frac) => {
          const val = Math.round(maxTrade * frac);
          const y = yForVal(val);
          return (
            <g key={`grid-${frac}`}>
              <line x1={pad.left} y1={y} x2={width - pad.right} y2={y} stroke="rgba(148,163,184,0.16)" />
              <text x={pad.left - 8} y={y + 4} textAnchor="end" fontSize="10" fill="#94a3b8">
                ₹{val}
              </text>
            </g>
          );
        })}

        {passes.map((passLabel, idx) => {
          const x = xForIdx(idx);
          return (
            <g key={passLabel}>
              <line
                x1={x}
                y1={pad.top}
                x2={x}
                y2={height - pad.bottom}
                stroke={hoveredPass === passLabel ? "rgba(56,189,248,0.45)" : "rgba(148,163,184,0.12)"}
                strokeDasharray="3 3"
              />
              <text x={x} y={height - 12} textAnchor="middle" fontSize="10.5" fill="#cbd5e1">
                {passLabel}
              </text>
            </g>
          );
        })}

        {series.map((s, sIdx) => {
          const color = LINE_COLORS[sIdx % LINE_COLORS.length];
          const pathD = passes
            .map((passLabel, idx) => {
              const val = s.points[passLabel] ?? 0;
              return `${idx === 0 ? "M" : "L"}${xForIdx(idx).toFixed(1)},${yForVal(val).toFixed(1)}`;
            })
            .join(" ");

          return (
            <g key={s.portId}>
              <path d={pathD} fill="none" stroke={color} strokeWidth="2.4" />
              {passes.map((passLabel, idx) => {
                const val = s.points[passLabel] ?? 0;
                return (
                  <circle
                    key={`${s.portId}-${passLabel}`}
                    cx={xForIdx(idx)}
                    cy={yForVal(val)}
                    r={hoveredPass === passLabel ? 5.5 : 4}
                    fill={color}
                    stroke="#09111f"
                    strokeWidth="1.5"
                    style={{ cursor: "pointer" }}
                    onMouseEnter={() => setHoveredPass(passLabel)}
                    onMouseLeave={() => setHoveredPass(null)}
                  >
                    <title>{`${s.portName} [${s.portId}] @ ${passLabel}: ₹ ${val} Cr`}</title>
                  </circle>
                );
              })}
            </g>
          );
        })}
      </svg>

      <div className="legend-row" style={{ marginBlockStart: "0.6rem", marginBlockEnd: 0 }}>
        {series.map((s, idx) => (
          <span key={s.portId} className="legend-item">
            <span
              className="legend-dot"
              style={{ backgroundColor: LINE_COLORS[idx % LINE_COLORS.length] }}
            />
            <span>
              {s.portId} ({s.portName})
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}
