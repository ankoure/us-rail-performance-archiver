// Public-facing dashboard PoC: fetch the exported JSON bundle and render it with
// Observable Plot (loaded from a CDN, no build step). Edit DATA_URL to point at a
// different export under ./data/.
import * as Plot from "https://cdn.jsdelivr.net/npm/@observablehq/plot@0.6/+esm";

const DATA_URL = "./data/wmata-vehicles-2026-05-20.json";

const $ = (id) => document.getElementById(id);

function fmtPct(v) {
  return v == null ? "—" : `${v.toFixed(1)}%`;
}

function render(data) {
  $("subhead").textContent =
    `${data.feed} · ${data.service_date} · ${data.routes.length} lines · ` +
    `${data.generated_note}`;

  const routes = data.routes;
  const width = 480;

  // (a) On-time % by line — sorted worst first (already sorted by exporter).
  $("chart-otp").replaceChildren(
    Plot.plot({
      width,
      marginLeft: 90,
      x: { domain: [0, 100], label: "on-time %", grid: true },
      y: { label: null },
      marks: [
        Plot.barX(routes.filter((r) => r.on_time_pct != null), {
          x: "on_time_pct",
          y: "name",
          sort: { y: "x", reverse: false },
          fill: (d) => (d.on_time_pct < 70 ? "#f85149" : "#3fb950"),
        }),
        Plot.ruleX([0]),
      ],
    })
  );

  // (b) Headway p50 by line.
  $("chart-headway").replaceChildren(
    Plot.plot({
      width,
      marginLeft: 90,
      x: { label: "median headway (s)", grid: true },
      y: { label: null },
      marks: [
        Plot.barX(routes.filter((r) => r.headway_p50_s != null), {
          x: "headway_p50_s",
          y: "name",
          sort: { y: "x", reverse: true },
          fill: "#4ea1ff",
        }),
        Plot.ruleX([0]),
      ],
    })
  );

  // (c) Median arrival delay by line.
  $("chart-delay").replaceChildren(
    Plot.plot({
      width,
      marginLeft: 90,
      x: { label: "median arrival delay (s)", grid: true },
      y: { label: null },
      marks: [
        Plot.barX(routes.filter((r) => r.arr_delay_p50_s != null), {
          x: "arr_delay_p50_s",
          y: "name",
          sort: { y: "x", reverse: true },
          fill: "#d29922",
        }),
        Plot.ruleX([0]),
      ],
    })
  );

  // (d) Worst stops table.
  const rows = data.worst_stops
    .map(
      (s) => `<tr>
        <td>${s.name ?? s.stop_id}</td>
        <td>${s.route ?? ""}</td>
        <td class="num bad-pct">${fmtPct(s.on_time_pct)}</td>
        <td class="num">${s.matched_count ?? ""}</td>
      </tr>`
    )
    .join("");
  $("table-stops").innerHTML = `<table>
      <thead><tr>
        <th>Stop</th><th>Line</th>
        <th class="num">On-time</th><th class="num">Matched</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  // (e) Slowest segments table.
  const segs = data.slowest_segments ?? [];
  if (segs.length) {
    const segRows = segs
      .map(
        (s) => `<tr>
          <td>${s.from_name ?? s.from_stop_id}</td>
          <td>${s.to_name ?? s.to_stop_id}</td>
          <td>${s.route ?? ""}</td>
          <td class="num">${s.speed_p50_mph != null ? s.speed_p50_mph.toFixed(1) : "—"}</td>
          <td class="num">${s.speed_p90_mph != null ? s.speed_p90_mph.toFixed(1) : "—"}</td>
          <td class="num">${s.sample_count ?? ""}</td>
        </tr>`
      )
      .join("");
    $("table-segments").innerHTML = `<table>
        <thead><tr>
          <th>From</th><th>To</th><th>Line</th>
          <th class="num">Speed p50 (mph)</th>
          <th class="num">Speed p90 (mph)</th>
          <th class="num">Obs</th>
        </tr></thead>
        <tbody>${segRows}</tbody>
      </table>`;
  } else {
    $("table-segments").textContent = "No segment data for this day.";
  }

  $("footer").textContent =
    "Approximate fields (weighted means of per-direction percentiles): " +
    data.approx_fields.join(", ") + ".";
}

fetch(DATA_URL)
  .then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  })
  .then(render)
  .catch((e) => {
    $("subhead").innerHTML =
      `<span class="error">Could not load ${DATA_URL}: ${e.message}. ` +
      `Run the exporter, then serve this directory.</span>`;
  });
