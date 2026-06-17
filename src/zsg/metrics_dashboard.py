"""
metrics_dashboard.py — Render the LLM metrics log as a self-contained HTML dashboard.

Read-only.  Takes the parsed records from metrics.load_records() and writes a
single portable HTML file (no external scripts, no CDN — matching the project's
build_guide.py philosophy: all data and rendering are inlined).  Charts are
drawn with vanilla JS + SVG so the file works offline in any browser.

Visualizes:
  - Latency over time      (round_trip_ms per call, chronological)
  - Latency distribution   (histogram of successful-call latencies)
  - Calls & success rate   (totals, success vs fail, breakdown by provider/model)
  - Unique users           (distinct fingerprints, calls-per-user)

Security note: the records contain only the metrics schema fields
(ts/provider/model/ok/round_trip_ms/user) — no raw keys, prompts, or responses
— so inlining them in the HTML leaks nothing the log doesn't already hold.
"""

import json
from pathlib import Path

from zsg.metrics import compute_stats


def build_html(records: list[dict]) -> str:
    """Return a self-contained HTML dashboard string for *records*."""
    stats = compute_stats(records)
    # Inline only the fields the charts use; keep payload minimal and explicit.
    payload = [
        {
            "ts": r.get("ts"),
            "provider": r.get("provider"),
            "model": r.get("model"),
            "ok": bool(r.get("ok")),
            "round_trip_ms": r.get("round_trip_ms"),
            "user": r.get("user"),
        }
        for r in records
    ]
    data_json = json.dumps({"records": payload, "stats": stats}, ensure_ascii=False)
    # Guard against "</script>" appearing in data closing the tag prematurely.
    data_json = data_json.replace("</", "<\\/")
    return _TEMPLATE.replace("/*__DATA__*/", data_json)


def write_dashboard(records: list[dict], out_path: Path) -> Path:
    """Write the dashboard HTML to *out_path* and return the path."""
    out_path = Path(out_path)
    out_path.write_text(build_html(records), encoding="utf-8")
    return out_path


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM Metrics Dashboard</title>
<style>
  :root {
    --bg: #0f1117; --panel: #1a1d27; --ink: #e7e9ee; --muted: #9aa0b0;
    --accent: #5b8def; --ok: #3fb950; --fail: #f85149; --grid: #2a2e3a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  header { padding: 24px 28px 8px; }
  h1 { margin: 0; font-size: 20px; font-weight: 600; }
  .sub { color: var(--muted); font-size: 13px; margin-top: 4px; }
  .wrap { padding: 16px 28px 40px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--panel); border: 1px solid var(--grid); border-radius: 10px; padding: 14px 16px; }
  .card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  .card .value { font-size: 26px; font-weight: 600; margin-top: 4px; }
  .card .value small { font-size: 13px; color: var(--muted); font-weight: 400; }
  .grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 16px; }
  .panel { background: var(--panel); border: 1px solid var(--grid); border-radius: 10px; padding: 16px 18px; }
  .panel h2 { margin: 0 0 12px; font-size: 14px; font-weight: 600; }
  svg { width: 100%; height: auto; display: block; }
  .empty { color: var(--muted); padding: 40px 0; text-align: center; }
  text { fill: var(--muted); font-size: 11px; }
  .axis line { stroke: var(--grid); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); }
  th { color: var(--muted); font-weight: 500; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .bar-ok { fill: var(--accent); }
  .legend { display: flex; gap: 16px; color: var(--muted); font-size: 12px; margin-top: 8px; }
  .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: middle; }
</style>
</head>
<body>
<header>
  <h1>LLM Metrics Dashboard</h1>
  <div class="sub" id="subtitle"></div>
</header>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <div class="grid2">
    <div class="panel"><h2>Latency over time (ms)</h2><div id="chart-latency-time"></div></div>
    <div class="panel"><h2>Latency distribution — successful calls (ms)</h2><div id="chart-latency-dist"></div></div>
    <div class="panel"><h2>Calls by provider / model</h2><div id="chart-breakdown"></div></div>
    <div class="panel"><h2>Calls per user (distinct key fingerprints)</h2><div id="chart-users"></div></div>
  </div>
</div>
<script>
const DATA = /*__DATA__*/;
const records = DATA.records || [];
const stats = DATA.stats || {};

const SVGNS = "http://www.w3.org/2000/svg";
function el(name, attrs, text) {
  const n = document.createElementNS(SVGNS, name);
  for (const k in (attrs||{})) n.setAttribute(k, attrs[k]);
  if (text != null) n.textContent = text;
  return n;
}
function svg(w, h) {
  const s = el("svg", {viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "xMidYMid meet"});
  s.setAttribute("width", "100%");
  return s;
}
function empty(id, msg) {
  document.getElementById(id).innerHTML = `<div class="empty">${msg}</div>`;
}
function fmt(n) {
  if (n == null) return "—";
  return (Math.round(n * 10) / 10).toLocaleString();
}

// ---- summary cards ----
(function () {
  const cards = [
    ["Total calls", (stats.total_calls||0).toLocaleString()],
    ["Successful", `${(stats.successful||0).toLocaleString()} / ${(stats.total_calls||0).toLocaleString()} `],
    ["Unique users", (stats.unique_users||0).toLocaleString()],
    ["Median latency", `${fmt(stats.latency_median_ms)} <small>ms</small>`],
    ["p95 latency", `${fmt(stats.latency_p95_ms)} <small>ms</small>`],
    ["Max latency", `${fmt(stats.latency_max_ms)} <small>ms</small>`],
  ];
  document.getElementById("cards").innerHTML = cards.map(
    ([l, v]) => `<div class="card"><div class="label">${l}</div><div class="value">${v}</div></div>`
  ).join("");
  const ts = records.map(r => r.ts).filter(Boolean).sort();
  const span = ts.length ? `${ts[0]} → ${ts[ts.length-1]}` : "no data";
  document.getElementById("subtitle").textContent =
    `${(stats.total_calls||0)} calls · ${span}`;
})();

// ---- latency over time (line) ----
(function () {
  const pts = records
    .map((r, i) => ({i, t: Date.parse(r.ts), v: r.round_trip_ms, ok: r.ok}))
    .filter(p => typeof p.v === "number");
  if (!pts.length) return empty("chart-latency-time", "No latency data yet.");
  const W = 560, H = 240, P = {l: 48, r: 12, t: 12, b: 28};
  const xs = pts.map(p => isNaN(p.t) ? p.i : p.t);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const vmax = Math.max(...pts.map(p => p.v), 1);
  const px = x => P.l + (x1 === x0 ? 0.5 : (x - x0) / (x1 - x0)) * (W - P.l - P.r);
  const py = v => H - P.b - (v / vmax) * (H - P.t - P.b);
  const s = svg(W, H);
  // y gridlines
  for (let k = 0; k <= 4; k++) {
    const v = vmax * k / 4, y = py(v);
    s.appendChild(el("line", {x1: P.l, y1: y, x2: W - P.r, y2: y, stroke: "var(--grid)"}));
    s.appendChild(el("text", {x: P.l - 6, y: y + 3, "text-anchor": "end"}, Math.round(v)));
  }
  // path over successful calls
  const ok = pts.filter(p => p.ok);
  if (ok.length) {
    const d = ok.map((p, i) => `${i ? "L" : "M"}${px(isNaN(p.t)?p.i:p.t).toFixed(1)} ${py(p.v).toFixed(1)}`).join(" ");
    s.appendChild(el("path", {d, fill: "none", stroke: "var(--accent)", "stroke-width": 1.5}));
  }
  // dots; failures in red
  pts.forEach(p => s.appendChild(el("circle", {
    cx: px(isNaN(p.t)?p.i:p.t), cy: py(p.v), r: 2.5,
    fill: p.ok ? "var(--accent)" : "var(--fail)"
  })));
  document.getElementById("chart-latency-time").appendChild(s);
})();

// ---- latency distribution (histogram) ----
(function () {
  const vals = records.filter(r => r.ok && typeof r.round_trip_ms === "number").map(r => r.round_trip_ms);
  if (!vals.length) return empty("chart-latency-dist", "No successful calls yet.");
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const bins = Math.min(20, Math.max(5, Math.ceil(Math.sqrt(vals.length))));
  const width = (hi - lo) || 1, step = width / bins;
  const counts = new Array(bins).fill(0);
  vals.forEach(v => { let b = Math.floor((v - lo) / step); if (b >= bins) b = bins - 1; if (b < 0) b = 0; counts[b]++; });
  const W = 560, H = 240, P = {l: 36, r: 12, t: 12, b: 28};
  const cmax = Math.max(...counts, 1);
  const bw = (W - P.l - P.r) / bins;
  const s = svg(W, H);
  for (let k = 0; k <= 4; k++) {
    const c = cmax * k / 4, y = H - P.b - (c / cmax) * (H - P.t - P.b);
    s.appendChild(el("line", {x1: P.l, y1: y, x2: W - P.r, y2: y, stroke: "var(--grid)"}));
    s.appendChild(el("text", {x: P.l - 6, y: y + 3, "text-anchor": "end"}, Math.round(c)));
  }
  counts.forEach((c, i) => {
    const h = (c / cmax) * (H - P.t - P.b);
    s.appendChild(el("rect", {
      x: P.l + i * bw + 1, y: H - P.b - h, width: Math.max(1, bw - 2), height: h, class: "bar-ok", rx: 2
    }));
  });
  s.appendChild(el("text", {x: P.l, y: H - 8}, `${Math.round(lo)} ms`));
  s.appendChild(el("text", {x: W - P.r, y: H - 8, "text-anchor": "end"}, `${Math.round(hi)} ms`));
  document.getElementById("chart-latency-dist").appendChild(s);
})();

// ---- breakdown table by provider/model ----
(function () {
  if (!records.length) return empty("chart-breakdown", "No calls yet.");
  const groups = {};
  records.forEach(r => {
    const key = `${r.provider || "?"} / ${r.model || "?"}`;
    const g = groups[key] || (groups[key] = {calls: 0, ok: 0, lat: []});
    g.calls++; if (r.ok) { g.ok++; if (typeof r.round_trip_ms === "number") g.lat.push(r.round_trip_ms); }
  });
  const rows = Object.entries(groups).sort((a, b) => b[1].calls - a[1].calls);
  const med = arr => { if (!arr.length) return null; const s = [...arr].sort((x, y) => x - y); const m = Math.floor(s.length/2); return s.length % 2 ? s[m] : (s[m-1]+s[m])/2; };
  const html = `<table><thead><tr><th>Provider / model</th><th class="num">Calls</th><th class="num">Success</th><th class="num">Median ms</th></tr></thead><tbody>${
    rows.map(([k, g]) => `<tr><td>${k}</td><td class="num">${g.calls}</td><td class="num">${Math.round(100*g.ok/g.calls)}%</td><td class="num">${fmt(med(g.lat))}</td></tr>`).join("")
  }</tbody></table>`;
  document.getElementById("chart-breakdown").innerHTML = html;
})();

// ---- calls per user (bar) ----
(function () {
  if (!records.length) return empty("chart-users", "No calls yet.");
  const counts = {};
  records.forEach(r => { const u = r.user || "none"; counts[u] = (counts[u] || 0) + 1; });
  const rows = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 15);
  const W = 560, rowH = 22, P = {l: 130, r: 40, t: 8, b: 8};
  const H = P.t + P.b + rows.length * rowH;
  const cmax = Math.max(...rows.map(r => r[1]), 1);
  const s = svg(W, H);
  rows.forEach(([u, c], i) => {
    const y = P.t + i * rowH;
    const label = u === "none" ? "none (keyless)" : u.slice(0, 12);
    s.appendChild(el("text", {x: P.l - 8, y: y + 14, "text-anchor": "end"}, label));
    const bw = (c / cmax) * (W - P.l - P.r);
    s.appendChild(el("rect", {x: P.l, y: y + 3, width: Math.max(1, bw), height: rowH - 8, class: "bar-ok", rx: 2}));
    s.appendChild(el("text", {x: P.l + bw + 6, y: y + 14, fill: "var(--ink)"}, c));
  });
  document.getElementById("chart-users").appendChild(s);
  const total = Object.keys(counts).length;
  if (total > rows.length) {
    document.getElementById("chart-users").insertAdjacentHTML("beforeend",
      `<div class="legend">Showing top ${rows.length} of ${total} users</div>`);
  }
})();
</script>
</body>
</html>
"""
