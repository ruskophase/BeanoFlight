(() => {
  "use strict";

  const payload = window.BEANO_BATCH_DATA;
  if (!payload || payload.schema !== "beanoflight-statistics-dashboard/v1") {
    document.querySelector("main").innerHTML = '<div class="fatal"><h1>Dashboard data unavailable</h1><p>Keep <code>batch-data.js</code> beside this page and reopen it.</p></div>';
    return;
  }

  const fieldIndex = Object.fromEntries(payload.fields.map((name, index) => [name, index]));
  const beans = payload.beans.map((row, index) => {
    const bean = { _row: row, _index: index };
    for (const [name, position] of Object.entries(fieldIndex)) bean[name] = row[position];
    bean.time = bean.first_frame == null || payload.first_frame == null || !payload.source_fps
      ? null : (bean.first_frame - payload.first_frame) / payload.source_fps;
    return bean;
  });
  const beansById = new Map(beans.map(bean => [bean.bean_id, bean]));
  const summary = payload.summary || {};
  const counts = summary.counts || {};
  const distributions = summary.distributions || {};
  const dark = summary.dark_bean_screen || {};
  const number = new Intl.NumberFormat("en-ZA");
  const decimal = new Intl.NumberFormat("en-ZA", { maximumFractionDigits: 2 });
  const chartState = new WeakMap();
  let selected = [];
  let selectedDescription = "";
  let selectionContext = { metric: "appearance", paired: false, chart: "unknown" };
  let galleryPage = 0;
  let modalBeanIndex = -1;
  const pageSize = 60;
  const collectionStorageKey = `beano-review-collection:${summary.source_run_id || "unknown"}`;
  let collection = { label: "review-collection", entries: {} };
  let collectionPersistenceAvailable = true;
  let standaloneChart = null;

  function finite(value) { return typeof value === "number" && Number.isFinite(value); }
  function fmt(value, digits = 2) {
    if (!finite(value)) return "—";
    return new Intl.NumberFormat("en-ZA", { maximumFractionDigits: digits }).format(value);
  }
  function pct(numerator, denominator) {
    return denominator ? `${(100 * numerator / denominator).toFixed(2)}%` : "—";
  }
  function compact(value) {
    if (!finite(value)) return "—";
    if (Math.abs(value) >= 1e6) return `${(value / 1e6).toFixed(2)}m`;
    if (Math.abs(value) >= 1e3) return `${(value / 1e3).toFixed(1)}k`;
    return decimal.format(value);
  }
  function escapeHtml(value) {
    return String(value).replace(/[&<>"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[character]);
  }
  function metric(label, value, detail, tone = "") {
    return `<div class="metric ${tone}"><span class="label">${escapeHtml(label)}</span><span class="value">${escapeHtml(value)}</span><span class="detail">${escapeHtml(detail)}</span></div>`;
  }
  function mean(values) {
    const usable = values.filter(finite);
    return usable.length ? usable.reduce((a, b) => a + b, 0) / usable.length : null;
  }
  function standardDeviation(values) {
    const usable = values.filter(finite);
    if (usable.length < 2) return null;
    const centre = mean(usable);
    return Math.sqrt(usable.reduce((sum, value) => sum + (value - centre) ** 2, 0) / (usable.length - 1));
  }
  function quantile(values, fraction) {
    const usable = values.filter(finite).sort((a, b) => a - b);
    if (!usable.length) return null;
    const index = (usable.length - 1) * fraction;
    const lower = Math.floor(index);
    const upper = Math.ceil(index);
    return usable[lower] + (usable[upper] - usable[lower]) * (index - lower);
  }

  document.getElementById("batch-id").textContent = summary.source_run_id || "Completed batch";
  installSelectionControls();
  installCollectionControls();
  loadCollection();
  if (document.body.dataset.chartPage === "true") {
    initialiseStandaloneChart();
  } else {
    renderSummary();
    renderStereoMetrics();
    renderHealth();
    installNavigation();
    installThreshold();
    const availablePages = new Set(["overview", "appearance", "size", "stereo", "timeline", "health"]);
    showPage(availablePages.has(location.hash.slice(1)) ? location.hash.slice(1) : "overview", false);
  }

  function renderSummary() {
    const total = counts.confirmed_beans || beans.length;
    const candidates = dark.candidate_count || beans.filter(bean => bean.dark_2sd).length;
    const colourCount = counts.beans_with_colour == null ? beans.filter(bean => finite(bean.lightness)).length : counts.beans_with_colour;
    document.getElementById("summary-metrics").innerHTML = [
      metric("Confirmed beans", number.format(total), "One row per completed bean track"),
      metric("Two-sample coverage", pct(counts.beans_with_two_samples || 0, total), `${number.format(counts.beans_with_two_samples || 0)} beans`, counts.beans_with_two_samples === total ? "good" : "warn"),
      metric("Colour coverage", pct(colourCount, total), `${number.format(colourCount)} with approximate Lab`, colourCount === total ? "good" : "warn"),
      metric("Dark review set", number.format(candidates), `${pct(candidates, total)} at mean − 2 SD`),
    ].join("");
    const complete = (counts.beans_without_samples || 0) === 0;
    document.getElementById("quality-banner").innerHTML = `<div class="notice ${complete ? "good" : "warn"}"><span class="notice-mark">${complete ? "✓" : "!"}</span><div><strong>${complete ? "Every confirmed bean has statistics" : "Some confirmed beans have no statistics"}</strong><span>${number.format(counts.beans_with_two_samples || 0)} have two samples; ${number.format(counts.beans_with_one_sample || 0)} use the explicit one-sample fallback; ${number.format(counts.beans_without_samples || 0)} have none.</span></div></div>`;
  }

  function renderStereoMetrics() {
    const ratio = distributions.projected_area_ratio_camr_to_caml_median || {};
    const delta = distributions.approx_lab_l_view_delta_median || {};
    document.getElementById("stereo-metrics").innerHTML = [
      metric("Median area ratio", fmt(ratio.p50), "CamR / CamL; ideal agreement is 1.00"),
      metric("Area ratio p10–p90", `${fmt(ratio.p10)}–${fmt(ratio.p90)}`, "Central 80% of measured beans"),
      metric("Median L* difference", fmt(delta.p50), "CamR minus CamL"),
      metric("Edge-affected beans", number.format(counts.beans_with_sensor_edge_observation || 0), "At least one observation touched a sensor boundary", counts.beans_with_sensor_edge_observation ? "warn" : "good"),
    ].join("");
  }

  function renderHealth() {
    const total = counts.confirmed_beans || beans.length;
    const runtime = summary.runtime;
    const sourceStats = summary.source_capture_statistics || {};
    const two = counts.beans_with_two_samples || 0;
    const colour = counts.beans_with_colour == null ? beans.filter(bean => finite(bean.lightness)).length : counts.beans_with_colour;
    const health = [
      metric("At least one sample", pct(total - (counts.beans_without_samples || 0), total), `${number.format(total - (counts.beans_without_samples || 0))} / ${number.format(total)}`, (counts.beans_without_samples || 0) === 0 ? "good" : "warn"),
      metric("Two samples", pct(two, total), `${number.format(two)} / ${number.format(total)}`, two === total ? "good" : "warn"),
      metric("Colour available", pct(colour, total), `${number.format(colour)} / ${number.format(total)}`, colour === total ? "good" : "warn"),
      metric("Enrichment fallbacks", number.format(counts.beans_with_enrichment_fallback || 0), "Beans with a geometry-only observation", counts.beans_with_enrichment_fallback ? "warn" : "good"),
    ];
    document.getElementById("health-metrics").innerHTML = health.join("");
    const target = document.getElementById("runtime-content");
    if (!runtime) {
      target.innerHTML = '<div class="notice warn"><span class="notice-mark">i</span><div><strong>No performance report attached</strong><span>Measurement completeness is available, but frame-rate, deadline, thermal, and pipeline-pressure evidence was not supplied when this bundle was generated.</span></div></div>' + captureTable(sourceStats);
      return;
    }
    const run = runtime.summary || {};
    const outcome = runtime.outcome || {};
    const passed = runtime.acceptance_passed === true;
    target.innerHTML = `<div class="notice ${passed ? "good" : "warn"}"><span class="notice-mark">${passed ? "✓" : "!"}</span><div><strong>${passed ? "Formal run acceptance passed" : "Run acceptance was not recorded as passed"}</strong><span>${number.format(run.frames_processed || 0)} frames over ${fmt(run.elapsed_seconds)} seconds; compact evidence is embedded here and the full timing ledger remains in the source report.</span></div></div>
      <div class="metric-grid">${[
        metric("Achieved processing", `${fmt(run.achieved_fps, 3)} fps`, `Source timeline ${fmt(run.source_timeline_fps, 3)} fps`, run.achieved_fps >= 59 ? "good" : "warn"),
        metric("Mean / max processing", `${fmt(run.mean_processing_ms)} / ${fmt(run.max_processing_ms)} ms`, `${number.format(run.missed_deadlines || 0)} frame deadline misses`),
        metric("Crop jobs", number.format(outcome.jobs_completed || 0), `${number.format(outcome.jobs_dropped || 0)} dropped · ${number.format(outcome.jobs_failed || 0)} failed`, (outcome.jobs_dropped || outcome.jobs_failed) ? "warn" : "good"),
        metric("Maximum temperature", `${fmt(runtime.maximum_temperature_c)} °C`, `${runtime.thermal_abort ? "Thermal abort" : "No thermal abort"} · max RSS ${fmt(runtime.max_rss_mib)} MiB`, runtime.thermal_abort ? "warn" : "good"),
      ].join("")}</div>${runtimeTable(run, outcome)}${captureTable(sourceStats)}`;
  }

  function runtimeTable(run, outcome) {
    const rows = [
      ["Frames skipped", run.frames_skipped], ["Mean / maximum frame age", `${fmt(run.mean_frame_age_ms)} / ${fmt(run.max_frame_age_ms)} ms`],
      ["Crops submitted / dropped", `${number.format(run.crops_submitted || 0)} / ${number.format(run.crops_dropped || 0)}`],
      ["Complete / incomplete stereo pairs", `${number.format(outcome.stereo_pairs_complete || 0)} / ${number.format(outcome.stereo_pairs_incomplete || 0)}`],
      ["Classification deadline fallbacks", outcome.classification_deadline_fallbacks], ["Actuations succeeded / failed", `${number.format(outcome.actuations_succeeded || 0)} / ${number.format(outcome.actuations_failed || 0)}`],
    ];
    return tablePanel("Pipeline evidence", "Compact extract from the matching performance report", rows);
  }

  function captureTable(stats) {
    const rows = Object.entries(stats).filter(([, value]) => ["number", "string", "boolean"].includes(typeof value)).slice(0, 16);
    return tablePanel("Statistics capture", "Counters written by the low-overhead live evidence collector", rows);
  }

  function tablePanel(title, subtitle, rows) {
    return `<article class="panel wide"><p class="eyebrow">${escapeHtml(subtitle)}</p><h2>${escapeHtml(title)}</h2><table class="health-table"><thead><tr><th>Measure</th><th>Value</th></tr></thead><tbody>${rows.map(([key, value]) => `<tr><td>${escapeHtml(String(key).replaceAll("_", " "))}</td><td>${escapeHtml(value == null ? "—" : value)}</td></tr>`).join("")}</tbody></table></article>`;
  }

  function standaloneDefinitions() {
    return {
      lightness: { title: "Lightness L*", eyebrow: "Appearance distribution", page: "appearance", explanation: "Lab L* describes colour-independent lightness from black (0) to white (100). Click a bar to inspect the beans in that exact interval and add them to a labelled review collection." },
      lab: { title: "Lab a* × b* colour plane", eyebrow: "Appearance relationship", page: "appearance", explanation: "a* runs green to red and b* runs blue to yellow. Click the nearest bean or drag a rectangle to create a colour-region selection." },
      chroma: { title: "Approximate Lab chroma", eyebrow: "Colour intensity", page: "appearance", explanation: "Chroma measures distance from neutral grey in the a*/b* plane. Higher values represent a stronger colour cast." },
      outlier: { title: "Appearance outlier score", eyebrow: "Multivariate appearance", page: "appearance", explanation: "This robust score combines L*, a*, and b*. High values identify beans unlike the centre of this batch without assigning a defect class." },
      volume: { title: "Equivalent-sphere volume proxy", eyebrow: "Relative projected size", page: "size", explanation: "The pixel³ proxy is derived from projected area. Selection tiles report both this volume proxy and projected area so the two quantities remain distinct." },
      area: { title: "Projected area proxy", eyebrow: "Projected footprint", page: "size", explanation: "This is the geometric mean of CamL and CamR areas where both are available, with an explicit one-view fallback." },
      "area-scatter": { title: "CamL area × CamR area", eyebrow: "Two-view geometry", page: "size", explanation: "Paired selection tiles show CamL and CamR mean-colour swatches alongside their separate projected areas. These are numerical view summaries, not retained photographs." },
      "area-ratio": { title: "CamR / CamL area ratio", eyebrow: "Stereo geometry agreement", page: "stereo", explanation: "A ratio near 1 indicates similar silhouette area. Paired tiles expose each camera's measurements for human comparison." },
      "lightness-delta": { title: "CamR − CamL lightness", eyebrow: "Stereo colour agreement", page: "stereo", explanation: "A value near zero indicates similar reconstructed mean lightness. Paired CamL/CamR swatches and per-view Lab values make systematic and isolated differences reviewable." },
      throughput: { title: "Confirmed beans per second", eyebrow: "Batch delivery rate", page: "timeline", explanation: "Each bar represents one elapsed second. Selecting it identifies the exact bean tracks first observed in that interval." },
      "timeline-lightness": { title: "Lightness across the batch", eyebrow: "Appearance over time", page: "timeline", explanation: "Each point is one bean. Click a point or drag a time/lightness region to inspect and collect its members." },
    };
  }

  function initialiseStandaloneChart() {
    const definitions = standaloneDefinitions();
    standaloneChart = definitions[location.hash.slice(1)] ? location.hash.slice(1) : "lightness";
    const definition = definitions[standaloneChart];
    document.title = `Beano · ${definition.title}`;
    document.getElementById("standalone-title").textContent = definition.title;
    document.getElementById("standalone-eyebrow").textContent = definition.eyebrow;
    document.getElementById("standalone-explanation").textContent = definition.explanation;
    document.querySelector(".back-link").href = `index.html#${definition.page}`;
    document.getElementById("standalone-chart").classList.toggle("lab-expanded", standaloneChart === "lab");
    const controls = document.getElementById("standalone-controls");
    if (standaloneChart === "lightness") {
      controls.innerHTML = '<div class="standalone-threshold"><label>Dark review screen: standard deviations below mean <output id="standalone-sigma-output">2.0</output><input id="standalone-sigma" type="range" min="0" max="4" value="2" step="0.1"></label><button class="button" id="standalone-select-dark">Select candidates</button></div>';
      document.getElementById("standalone-sigma").addEventListener("input", event => {
        document.getElementById("standalone-sigma-output").textContent = Number(event.target.value).toFixed(1);
        renderStandaloneChart();
      });
      document.getElementById("standalone-select-dark").addEventListener("click", () => {
        const sigma = Number(document.getElementById("standalone-sigma").value);
        const centre = finite(dark.lightness_mean) ? dark.lightness_mean : mean(beans.map(bean => bean.lightness));
        const deviation = finite(dark.lightness_sample_standard_deviation) ? dark.lightness_sample_standard_deviation : standardDeviation(beans.map(bean => bean.lightness));
        const threshold = centre - sigma * deviation;
        setSelection(beans.filter(bean => finite(bean.lightness) && bean.lightness <= threshold), `Approximate L* ≤ ${fmt(threshold)} (mean − ${sigma.toFixed(1)} SD)`, { metric: "lightness", paired: false, chart: "lightness" });
      });
    }
    requestAnimationFrame(renderStandaloneChart);
  }

  function renderStandaloneChart() {
    const id = "standalone-chart";
    if (standaloneChart === "lightness") {
      const sigma = Number(document.getElementById("standalone-sigma")?.value || 2);
      const centre = finite(dark.lightness_mean) ? dark.lightness_mean : mean(beans.map(bean => bean.lightness));
      const deviation = finite(dark.lightness_sample_standard_deviation) ? dark.lightness_sample_standard_deviation : standardDeviation(beans.map(bean => bean.lightness));
      const threshold = centre - sigma * deviation;
      histogram(id, "lightness", { xLabel: "Approximate Lab L*", colour: "#b97937", marker: { value: threshold, label: `mean − ${sigma.toFixed(1)} SD` }, highlightBelow: threshold, bins: 72, chartId: "lightness" });
    } else if (standaloneChart === "lab") {
      scatter(id, "lab_a", "lab_b", { xLabel: "Lab a* (green → red)", yLabel: "Lab b* (blue → yellow)", colourByBean: true, radius: 2.1, chartId: "lab" });
    } else if (standaloneChart === "chroma") {
      histogram(id, "chroma", { xLabel: "Approximate Lab chroma", colour: "#8872a0", bins: 72, chartId: "chroma" });
    } else if (standaloneChart === "outlier") {
      histogram(id, "outlier_score", { xLabel: "Robust appearance outlier score", colour: "#9d5b4a", bins: 72, chartId: "outlier" });
    } else if (standaloneChart === "volume") {
      histogram(id, "volume_proxy", { xLabel: "Equivalent-sphere volume proxy (pixel³)", colour: "#32779a", bins: 72, chartId: "volume" });
    } else if (standaloneChart === "area") {
      histogram(id, "projected_area", { xLabel: "Projected area proxy (pixel²)", colour: "#5f8466", bins: 72, chartId: "area" });
    } else if (standaloneChart === "area-scatter") {
      scatter(id, "caml_area", "camr_area", { xLabel: "CamL projected area (pixel²)", yLabel: "CamR projected area (pixel²)", colour: "#315f82", diagonal: true, paired: true, radius: 2, chartId: "area-scatter" });
    } else if (standaloneChart === "area-ratio") {
      histogram(id, "area_ratio", { xLabel: "CamR area / CamL area", colour: "#44835b", marker: { value: 1, label: "equal area" }, paired: true, bins: 72, chartId: "area-ratio" });
    } else if (standaloneChart === "lightness-delta") {
      histogram(id, "lightness_delta", { xLabel: "CamR L* − CamL L*", colour: "#a15d83", marker: { value: 0, label: "equal lightness" }, paired: true, bins: 72, chartId: "lightness-delta" });
    } else if (standaloneChart === "throughput") {
      timelineHistogram(id);
    } else if (standaloneChart === "timeline-lightness") {
      scatter(id, "time", "lightness", { xLabel: "Elapsed batch time (seconds)", yLabel: "Approximate Lab L*", colourByBean: true, radius: 2, xMinimum: 0, chartId: "timeline-lightness" });
    }
  }

  function installNavigation() {
    document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => showPage(button.dataset.page)));
    document.querySelectorAll("[data-go]").forEach(button => button.addEventListener("click", () => showPage(button.dataset.go)));
    document.querySelectorAll("[data-export-all]").forEach(button => button.addEventListener("click", () => exportCsv(beans, "all-beans.csv")));
  }

  function showPage(name, updateHash = true) {
    document.querySelectorAll(".page").forEach(page => page.classList.toggle("active", page.id === `page-${name}`));
    document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.page === name));
    if (updateHash && location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
    renderPage(name);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function renderPage(name) {
    requestAnimationFrame(() => {
      if (name === "overview") {
        histogram("overview-lightness", "lightness", { xLabel: "Approximate Lab L*", colour: "#b97937", interactive: true, bins: 36, chartId: "lightness" });
        histogram("overview-size", "volume_proxy", { xLabel: "Volume proxy (pixel³)", colour: "#397c61", interactive: true, bins: 36, chartId: "volume" });
      } else if (name === "appearance") {
        renderAppearanceCharts();
      } else if (name === "size") {
        histogram("volume-chart", "volume_proxy", { xLabel: "Equivalent-sphere volume proxy (pixel³)", colour: "#32779a", chartId: "volume" });
        scatter("area-scatter", "caml_area", "camr_area", { xLabel: "CamL projected area (pixel²)", yLabel: "CamR projected area (pixel²)", colour: "#315f82", diagonal: true, paired: true, chartId: "area-scatter" });
        histogram("area-chart", "projected_area", { xLabel: "Projected area proxy (pixel²)", colour: "#5f8466", chartId: "area" });
      } else if (name === "stereo") {
        histogram("ratio-chart", "area_ratio", { xLabel: "CamR area / CamL area", colour: "#44835b", marker: { value: 1, label: "equal area" }, paired: true, chartId: "area-ratio" });
        histogram("delta-chart", "lightness_delta", { xLabel: "CamR L* − CamL L*", colour: "#a15d83", marker: { value: 0, label: "equal lightness" }, paired: true, chartId: "lightness-delta" });
      } else if (name === "timeline") {
        timelineHistogram("throughput-chart");
        scatter("timeline-lightness", "time", "lightness", { xLabel: "Elapsed batch time (seconds)", yLabel: "Approximate Lab L*", colourByBean: true, radius: 1.6, xMinimum: 0, chartId: "timeline-lightness" });
      }
    });
  }

  function renderAppearanceCharts() {
    const sigma = Number(document.getElementById("sigma-slider").value);
    const centre = finite(dark.lightness_mean) ? dark.lightness_mean : mean(beans.map(bean => bean.lightness));
    const deviation = finite(dark.lightness_sample_standard_deviation) ? dark.lightness_sample_standard_deviation : standardDeviation(beans.map(bean => bean.lightness));
    const threshold = centre - sigma * deviation;
    document.getElementById("threshold-value").textContent = fmt(threshold);
    histogram("lightness-chart", "lightness", { xLabel: "Approximate Lab L*", colour: "#b97937", marker: { value: threshold, label: `mean − ${sigma.toFixed(1)} SD` }, highlightBelow: threshold, chartId: "lightness" });
    scatter("lab-chart", "lab_a", "lab_b", { xLabel: "Lab a* (green → red)", yLabel: "Lab b* (blue → yellow)", colourByBean: true, radius: 1.8, chartId: "lab" });
    histogram("chroma-chart", "chroma", { xLabel: "Approximate Lab chroma", colour: "#8872a0", chartId: "chroma" });
    histogram("outlier-chart", "outlier_score", { xLabel: "Robust appearance outlier score", colour: "#9d5b4a", chartId: "outlier" });
  }

  function installThreshold() {
    const slider = document.getElementById("sigma-slider");
    slider.addEventListener("input", () => {
      document.getElementById("sigma-output").textContent = Number(slider.value).toFixed(1);
      renderAppearanceCharts();
    });
    document.getElementById("select-dark").addEventListener("click", () => {
      const sigma = Number(slider.value);
      const centre = finite(dark.lightness_mean) ? dark.lightness_mean : mean(beans.map(bean => bean.lightness));
      const deviation = finite(dark.lightness_sample_standard_deviation) ? dark.lightness_sample_standard_deviation : standardDeviation(beans.map(bean => bean.lightness));
      const threshold = centre - sigma * deviation;
      setSelection(beans.filter(bean => finite(bean.lightness) && bean.lightness <= threshold), `Approximate L* ≤ ${fmt(threshold)} (mean − ${sigma.toFixed(1)} SD)`, { metric: "lightness", paired: false, chart: "lightness" });
    });
  }

  function canvasContext(canvas) {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const width = Math.max(320, Math.round(rect.width));
    const height = Math.max(220, Math.round(rect.height));
    if (canvas.width !== width * dpr || canvas.height !== height * dpr) {
      canvas.width = width * dpr;
      canvas.height = height * dpr;
    }
    const context = canvas.getContext("2d");
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);
    return { context, width, height, dpr };
  }

  function plotArea(width, height) { return { left: 64, top: 18, right: width - 18, bottom: height - 49 }; }
  function extent(values, padding = .04) {
    const usable = values.filter(finite);
    if (!usable.length) return [0, 1];
    let low = Math.min(...usable), high = Math.max(...usable);
    if (low === high) { low -= .5; high += .5; }
    const margin = (high - low) * padding;
    return [low - margin, high + margin];
  }
  function scale(value, low, high, screenLow, screenHigh) { return screenLow + (value - low) / (high - low) * (screenHigh - screenLow); }

  function axes(context, area, xDomain, yDomain, xLabel, yLabel) {
    context.save();
    context.font = "10px Inter, system-ui, sans-serif";
    context.lineWidth = 1;
    context.textAlign = "center";
    context.textBaseline = "top";
    for (let i = 0; i <= 5; i++) {
      const y = area.bottom - i / 5 * (area.bottom - area.top);
      const value = yDomain[0] + i / 5 * (yDomain[1] - yDomain[0]);
      context.strokeStyle = "#e6ebe7";
      context.beginPath(); context.moveTo(area.left, y); context.lineTo(area.right, y); context.stroke();
      context.fillStyle = "#78827b";
      context.textAlign = "right"; context.textBaseline = "middle";
      context.fillText(axisNumber(value), area.left - 8, y);
    }
    for (let i = 0; i <= 5; i++) {
      const x = area.left + i / 5 * (area.right - area.left);
      const value = xDomain[0] + i / 5 * (xDomain[1] - xDomain[0]);
      context.fillStyle = "#78827b";
      context.textAlign = "center"; context.textBaseline = "top";
      context.fillText(axisNumber(value), x, area.bottom + 7);
    }
    context.strokeStyle = "#9da8a0";
    context.beginPath(); context.moveTo(area.left, area.top); context.lineTo(area.left, area.bottom); context.lineTo(area.right, area.bottom); context.stroke();
    context.fillStyle = "#536058";
    context.font = "600 10px Inter, system-ui, sans-serif";
    context.textAlign = "center"; context.textBaseline = "bottom";
    context.fillText(xLabel, (area.left + area.right) / 2, area.bottom + 43);
    context.save(); context.translate(12, (area.top + area.bottom) / 2); context.rotate(-Math.PI / 2); context.fillText(yLabel, 0, 0); context.restore();
    context.restore();
  }
  function axisNumber(value) {
    if (Math.abs(value) >= 1e6) return `${(value / 1e6).toFixed(1)}m`;
    if (Math.abs(value) >= 1e3) return `${(value / 1e3).toFixed(1)}k`;
    if (Math.abs(value) < .1 && value !== 0) return value.toExponential(1);
    return Math.abs(value) < 10 ? value.toFixed(1) : value.toFixed(0);
  }

  function histogram(id, key, options = {}) {
    const canvas = document.getElementById(id);
    if (!canvas) return;
    const points = beans.filter(bean => finite(bean[key]));
    const values = points.map(bean => bean[key]);
    const rawDomain = extent(values, 0);
    const binCount = options.bins || Math.max(18, Math.min(55, Math.round(Math.sqrt(values.length))));
    const widthValue = (rawDomain[1] - rawDomain[0]) / binCount;
    const bins = Array.from({ length: binCount }, (_, index) => ({ low: rawDomain[0] + index * widthValue, high: rawDomain[0] + (index + 1) * widthValue, beans: [] }));
    points.forEach(bean => bins[Math.min(binCount - 1, Math.max(0, Math.floor((bean[key] - rawDomain[0]) / widthValue)))].beans.push(bean));
    const { context, width, height } = canvasContext(canvas);
    const area = plotArea(width, height);
    const maximum = Math.max(1, ...bins.map(bin => bin.beans.length));
    axes(context, area, rawDomain, [0, maximum], options.xLabel || key, "Confirmed bean count");
    const barWidth = (area.right - area.left) / binCount;
    bins.forEach((bin, index) => {
      const x = area.left + index * barWidth + .5;
      const y = scale(bin.beans.length, 0, maximum, area.bottom, area.top);
      const highlighted = finite(options.highlightBelow) && bin.low <= options.highlightBelow;
      context.fillStyle = highlighted ? "#243b2e" : (options.colour || "#397c61");
      context.globalAlpha = .88;
      context.fillRect(x, y, Math.max(.7, barWidth - 1), area.bottom - y);
    });
    context.globalAlpha = 1;
    if (options.marker && finite(options.marker.value) && options.marker.value >= rawDomain[0] && options.marker.value <= rawDomain[1]) {
      const x = scale(options.marker.value, rawDomain[0], rawDomain[1], area.left, area.right);
      context.strokeStyle = "#16271d"; context.setLineDash([4, 3]); context.beginPath(); context.moveTo(x, area.top); context.lineTo(x, area.bottom); context.stroke(); context.setLineDash([]);
      context.fillStyle = "#16271d"; context.font = "600 9px Inter, system-ui, sans-serif"; context.textAlign = x > (area.left + area.right) / 2 ? "right" : "left"; context.fillText(options.marker.label, x + (x > (area.left + area.right) / 2 ? -5 : 5), area.top + 10);
    }
    bindChart(canvas, {
      kind: "histogram", area, bins, domain: rawDomain, key,
      click(x) {
        const index = Math.min(binCount - 1, Math.max(0, Math.floor((x - area.left) / (area.right - area.left) * binCount)));
        const bin = bins[index];
        setSelection(bin.beans, `${options.xLabel || key}: ${fmt(bin.low)} to ${fmt(bin.high)}`, { metric: key, paired: Boolean(options.paired), chart: options.chartId || key });
      },
      hover(x) {
        const index = Math.min(binCount - 1, Math.max(0, Math.floor((x - area.left) / (area.right - area.left) * binCount)));
        const bin = bins[index];
        return `${number.format(bin.beans.length)} beans<br>${fmt(bin.low)} to ${fmt(bin.high)}`;
      },
    });
  }

  function scatter(id, xKey, yKey, options = {}) {
    const canvas = document.getElementById(id);
    if (!canvas) return;
    const points = beans.filter(bean => finite(bean[xKey]) && finite(bean[yKey]));
    const xDomain = extent(points.map(bean => bean[xKey]));
    const yDomain = extent(points.map(bean => bean[yKey]));
    if (finite(options.xMinimum)) xDomain[0] = options.xMinimum;
    const { context, width, height } = canvasContext(canvas);
    const area = plotArea(width, height);
    axes(context, area, xDomain, yDomain, options.xLabel || xKey, options.yLabel || yKey);
    if (options.diagonal) {
      const low = Math.max(xDomain[0], yDomain[0]), high = Math.min(xDomain[1], yDomain[1]);
      context.strokeStyle = "#9ba79f"; context.setLineDash([4, 4]); context.beginPath();
      context.moveTo(scale(low, xDomain[0], xDomain[1], area.left, area.right), scale(low, yDomain[0], yDomain[1], area.bottom, area.top));
      context.lineTo(scale(high, xDomain[0], xDomain[1], area.left, area.right), scale(high, yDomain[0], yDomain[1], area.bottom, area.top)); context.stroke(); context.setLineDash([]);
    }
    context.globalAlpha = .55;
    const screenPoints = points.map(bean => {
      const x = scale(bean[xKey], xDomain[0], xDomain[1], area.left, area.right);
      const y = scale(bean[yKey], yDomain[0], yDomain[1], area.bottom, area.top);
      context.fillStyle = options.colourByBean ? beanColour(bean) : (options.colour || "#397c61");
      context.beginPath(); context.arc(x, y, options.radius || 1.7, 0, Math.PI * 2); context.fill();
      return { x, y, bean };
    });
    context.globalAlpha = 1;
    bindChart(canvas, {
      kind: "scatter", area, screenPoints,
      click(x, y) {
        let closest = null, distance = Infinity;
        for (const point of screenPoints) {
          const candidate = (point.x - x) ** 2 + (point.y - y) ** 2;
          if (candidate < distance) { closest = point; distance = candidate; }
        }
        if (closest) setSelection([closest.bean], `${options.xLabel || xKey} × ${options.yLabel || yKey}`, { metric: `${xKey},${yKey}`, paired: Boolean(options.paired), chart: options.chartId || `${xKey}-${yKey}` });
      },
      drag(start, end) {
        const left = Math.min(start.x, end.x), right = Math.max(start.x, end.x), top = Math.min(start.y, end.y), bottom = Math.max(start.y, end.y);
        const chosen = screenPoints.filter(point => point.x >= left && point.x <= right && point.y >= top && point.y <= bottom).map(point => point.bean);
        setSelection(chosen, `${options.xLabel || xKey} × ${options.yLabel || yKey} selected region`, { metric: `${xKey},${yKey}`, paired: Boolean(options.paired), chart: options.chartId || `${xKey}-${yKey}` });
      },
      hover(x, y) {
        let closest = null, distance = 80;
        for (const point of screenPoints) {
          const candidate = (point.x - x) ** 2 + (point.y - y) ** 2;
          if (candidate < distance) { closest = point; distance = candidate; }
        }
        return closest ? `Bean ${escapeHtml(closest.bean.sequence)}<br>x ${fmt(closest.bean[xKey])} · y ${fmt(closest.bean[yKey])}` : null;
      },
    });
  }

  function timelineHistogram(id = "throughput-chart") {
    const canvas = document.getElementById(id);
    const timed = beans.filter(bean => finite(bean.time));
    const maximumTime = Math.max(1, ...timed.map(bean => Math.floor(bean.time)));
    const bins = Array.from({ length: maximumTime + 1 }, (_, second) => ({ low: second, high: second + 1, beans: [] }));
    timed.forEach(bean => bins[Math.min(maximumTime, Math.max(0, Math.floor(bean.time)))].beans.push(bean));
    const { context, width, height } = canvasContext(canvas);
    const area = plotArea(width, height);
    const maximum = Math.max(1, ...bins.map(bin => bin.beans.length));
    axes(context, area, [0, maximumTime + 1], [0, maximum], "Elapsed batch time (seconds)", "Confirmed beans / second");
    const barWidth = (area.right - area.left) / bins.length;
    context.fillStyle = "#397c61";
    bins.forEach((bin, index) => {
      const y = scale(bin.beans.length, 0, maximum, area.bottom, area.top);
      context.fillRect(area.left + index * barWidth, y, Math.max(.5, barWidth), area.bottom - y);
    });
    bindChart(canvas, {
      kind: "histogram", area, bins,
      click(x) { const bin = bins[Math.min(bins.length - 1, Math.max(0, Math.floor((x - area.left) / (area.right - area.left) * bins.length)))]; setSelection(bin.beans, `Elapsed time ${bin.low}–${bin.high} seconds`, { metric: "time", paired: false, chart: "throughput" }); },
      hover(x) { const bin = bins[Math.min(bins.length - 1, Math.max(0, Math.floor((x - area.left) / (area.right - area.left) * bins.length)))]; return `${number.format(bin.beans.length)} beans<br>${bin.low}–${bin.high} seconds`; },
    });
  }

  function bindChart(canvas, state) {
    chartState.set(canvas, state);
    if (canvas.dataset.bound) return;
    canvas.dataset.bound = "true";
    let dragStart = null;
    canvas.addEventListener("pointerdown", event => {
      const current = chartState.get(canvas); if (!current || current.kind !== "scatter") return;
      dragStart = localPoint(canvas, event); canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener("pointerup", event => {
      const current = chartState.get(canvas); if (!current) return;
      const point = localPoint(canvas, event);
      if (dragStart && Math.hypot(point.x - dragStart.x, point.y - dragStart.y) > 8 && current.drag) current.drag(dragStart, point);
      else if (inside(current.area, point) && current.click) current.click(point.x, point.y);
      dragStart = null;
    });
    canvas.addEventListener("pointermove", event => {
      const current = chartState.get(canvas); if (!current || dragStart) return hideTooltip();
      const point = localPoint(canvas, event);
      if (!inside(current.area, point)) return hideTooltip();
      const content = current.hover ? current.hover(point.x, point.y) : null;
      if (content) showTooltip(event.clientX, event.clientY, content); else hideTooltip();
    });
    canvas.addEventListener("pointerleave", hideTooltip);
  }
  function localPoint(canvas, event) { const rect = canvas.getBoundingClientRect(); return { x: event.clientX - rect.left, y: event.clientY - rect.top }; }
  function inside(area, point) { return point.x >= area.left && point.x <= area.right && point.y >= area.top && point.y <= area.bottom; }
  function showTooltip(x, y, content) { const tooltip = document.getElementById("tooltip"); tooltip.innerHTML = content; tooltip.style.left = `${x + 12}px`; tooltip.style.top = `${y + 12}px`; tooltip.classList.add("visible"); }
  function hideTooltip() { document.getElementById("tooltip").classList.remove("visible"); }

  function installSelectionControls() {
    document.getElementById("close-selection").addEventListener("click", () => closeDockedDrawer("selection"));
    document.getElementById("clear-selection").addEventListener("click", clearSelection);
    document.getElementById("export-selection").addEventListener("click", () => exportCsv(selected, "selected-beans.csv"));
    document.getElementById("export-contact").addEventListener("click", exportSwatchSheet);
    document.getElementById("page-prev").addEventListener("click", () => { if (galleryPage > 0) { galleryPage--; renderGallery(); } });
    document.getElementById("page-next").addEventListener("click", () => { if ((galleryPage + 1) * pageSize < selected.length) { galleryPage++; renderGallery(); } });
    document.getElementById("select-visible").addEventListener("change", event => setVisibleBeansSelected(event.target.checked));
    installBeanModal();
  }

  function setSelection(chosen, description, context = {}) {
    selected = [...new Map(chosen.map(bean => [bean.bean_id, bean])).values()].sort((a, b) => (a.sequence || 0) - (b.sequence || 0));
    selectedDescription = description;
    selectionContext = { metric: context.metric || "appearance", paired: Boolean(context.paired), chart: context.chart || "unknown" };
    galleryPage = 0;
    openDockedDrawer("selection");
    document.getElementById("selection-count").textContent = number.format(selected.length);
    document.getElementById("selection-description").textContent = description;
    renderSelectionSummary();
    renderGallery();
  }
  function clearSelection() {
    selected = []; galleryPage = 0;
    closeDockedDrawer("selection");
  }
  function renderSelectionSummary(detailBean = null) {
    const target = document.getElementById("selection-summary");
    if (detailBean) {
      target.innerHTML = `<strong>Bean ${escapeHtml(detailBean.sequence)} details</strong><br>Combined: L* ${fmt(detailBean.lightness)} · a* ${fmt(detailBean.lab_a)} · b* ${fmt(detailBean.lab_b)} · chroma ${fmt(detailBean.chroma)}<br>CamL: L* ${fmt(detailBean.caml_lightness)} · a* ${fmt(detailBean.caml_lab_a)} · b* ${fmt(detailBean.caml_lab_b)} · area ${fmt(detailBean.caml_area)} px²<br>CamR: L* ${fmt(detailBean.camr_lightness)} · a* ${fmt(detailBean.camr_lab_a)} · b* ${fmt(detailBean.camr_lab_b)} · area ${fmt(detailBean.camr_area)} px²<br>Volume proxy ${compact(detailBean.volume_proxy)} px³ · area ratio ${fmt(detailBean.area_ratio)} · L* Δ ${fmt(detailBean.lightness_delta)}<br>${detailBean.sample_count || 0} sample(s) · ${detailBean.measurement_views || 0} view(s) minimum${detailBean.sensor_edge ? " · sensor-edge flag" : ""}${detailBean.enrichment_fallback ? " · enrichment fallback" : ""}`;
      return;
    }
    target.textContent = selected.length ? `Mean L* ${fmt(mean(selected.map(bean => bean.lightness)))} · median volume proxy ${compact(quantile(selected.map(bean => bean.volume_proxy), .5))} · ${selected.filter(bean => bean.dark_2sd).length} original 2-SD dark candidates` : "No beans in this selection.";
  }
  function renderGallery() {
    const start = galleryPage * pageSize;
    const page = selected.slice(start, start + pageSize);
    document.getElementById("bean-gallery").innerHTML = page.map(bean => {
      const lines = galleryMetricLines(bean);
      const checked = Object.hasOwn(collection.entries, bean.bean_id);
      return `<div class="bean-card${checked ? " checked" : ""}" data-bean-index="${bean._index}" role="button" tabindex="0" title="Open enlarged bean view"><label class="tile-checkbox" title="${checked ? "Remove from" : "Add to"} Review Collection"><input type="checkbox" data-select-bean="${escapeHtml(bean.bean_id)}"${checked ? " checked" : ""} aria-label="Select bean ${escapeHtml(bean.sequence)} for review"></label>${selectionSwatch(bean)}<div class="bean-info"><strong>#${escapeHtml(bean.sequence)}</strong><span>${escapeHtml(lines[0])}</span><span>${escapeHtml(lines[1])}</span></div></div>`;
    }).join("");
    document.querySelectorAll(".bean-card").forEach(card => {
      card.addEventListener("click", event => { if (!event.target.closest(".tile-checkbox")) openBeanModal(beans[Number(card.dataset.beanIndex)]); });
      card.addEventListener("keydown", event => { if ((event.key === "Enter" || event.key === " ") && !event.target.closest(".tile-checkbox")) { event.preventDefault(); openBeanModal(beans[Number(card.dataset.beanIndex)]); } });
    });
    document.querySelectorAll("[data-select-bean]").forEach(input => input.addEventListener("change", event => {
      event.stopPropagation();
      setBeanCollectionMembership(beansById.get(input.dataset.selectBean), input.checked);
    }));
    const pages = Math.max(1, Math.ceil(selected.length / pageSize));
    document.getElementById("page-status").textContent = selected.length ? `Page ${galleryPage + 1} of ${pages} · ${start + 1}–${Math.min(start + pageSize, selected.length)}` : "No beans";
    document.getElementById("page-prev").disabled = galleryPage === 0;
    document.getElementById("page-next").disabled = galleryPage + 1 >= pages;
    updateVisibleSelectionControl();
  }

  function visibleGalleryBeans() {
    return selected.slice(galleryPage * pageSize, (galleryPage + 1) * pageSize);
  }

  function updateVisibleSelectionControl() {
    const visible = visibleGalleryBeans();
    const selectedCount = visible.filter(bean => Object.hasOwn(collection.entries, bean.bean_id)).length;
    const control = document.getElementById("select-visible");
    control.checked = visible.length > 0 && selectedCount === visible.length;
    control.indeterminate = selectedCount > 0 && selectedCount < visible.length;
    control.disabled = visible.length === 0;
    document.getElementById("visible-tile-count").textContent = number.format(visible.length);
    document.getElementById("visible-selected-count").textContent = `${number.format(selectedCount)} selected`;
  }

  function setVisibleBeansSelected(checked) {
    for (const bean of visibleGalleryBeans()) updateBeanCollectionEntry(bean, checked);
    persistCollection();
    renderCollection();
    renderGallery();
  }

  function setBeanCollectionMembership(bean, checked) {
    if (!bean) return;
    updateBeanCollectionEntry(bean, checked);
    persistCollection();
    renderCollection();
    renderGallery();
    updateModalReviewCheckbox(bean);
  }

  function updateBeanCollectionEntry(bean, checked) {
    if (!checked) {
      delete collection.entries[bean.bean_id];
      return;
    }
    const existing = collection.entries[bean.bean_id] || { bean_id: bean.bean_id, sources: [], added_utc: new Date().toISOString() };
    const provenance = `${selectionContext.chart}: ${selectedDescription}`;
    if (!existing.sources.includes(provenance)) existing.sources.push(provenance);
    collection.entries[bean.bean_id] = existing;
  }

  function openDockedDrawer(kind) {
    const selectionOpen = kind === "selection";
    document.getElementById("selection-drawer").classList.toggle("open", selectionOpen);
    document.getElementById("collection-drawer").classList.toggle("open", !selectionOpen);
    document.body.classList.toggle("selection-docked", selectionOpen);
    document.body.classList.toggle("collection-docked", !selectionOpen);
    scheduleChartReflow();
  }

  function closeDockedDrawer(kind) {
    document.getElementById(`${kind === "selection" ? "selection" : "collection"}-drawer`).classList.remove("open");
    document.body.classList.remove(`${kind === "selection" ? "selection" : "collection"}-docked`);
    scheduleChartReflow();
  }

  function scheduleChartReflow() {
    window.setTimeout(() => window.dispatchEvent(new Event("resize")), 240);
  }

  function installBeanModal() {
    document.body.insertAdjacentHTML("beforeend", '<div id="bean-modal" class="bean-modal" role="dialog" aria-modal="true" aria-labelledby="modal-bean-title"><div class="bean-modal-card"><button id="modal-close" class="icon-button modal-close" aria-label="Close enlarged bean view">×</button><div id="modal-content"></div><div class="modal-footer"><label class="modal-review-check"><input id="modal-review-checkbox" type="checkbox"> Selected for Review Collection</label><div class="modal-nav"><button id="modal-prev" class="button secondary">← Previous</button><button id="modal-next" class="button secondary">Next →</button></div></div></div></div><div id="dashboard-toast" class="toast" aria-live="polite"></div>');
    document.getElementById("modal-close").addEventListener("click", closeBeanModal);
    document.getElementById("bean-modal").addEventListener("click", event => { if (event.target.id === "bean-modal") closeBeanModal(); });
    document.getElementById("modal-prev").addEventListener("click", () => moveBeanModal(-1));
    document.getElementById("modal-next").addEventListener("click", () => moveBeanModal(1));
    document.getElementById("modal-review-checkbox").addEventListener("change", event => {
      const bean = selected[modalBeanIndex];
      if (bean) setBeanCollectionMembership(bean, event.target.checked);
    });
    document.addEventListener("keydown", event => {
      if (!document.getElementById("bean-modal").classList.contains("open")) return;
      if (event.key === "Escape") closeBeanModal();
      else if (event.key === "ArrowLeft") moveBeanModal(-1);
      else if (event.key === "ArrowRight") moveBeanModal(1);
    });
  }

  function openBeanModal(bean) {
    modalBeanIndex = selected.findIndex(candidate => candidate.bean_id === bean.bean_id);
    if (modalBeanIndex < 0) return;
    renderBeanModal();
    document.getElementById("bean-modal").classList.add("open");
  }

  function closeBeanModal() {
    document.getElementById("bean-modal").classList.remove("open");
  }

  function moveBeanModal(direction) {
    if (!selected.length) return;
    modalBeanIndex = Math.min(selected.length - 1, Math.max(0, modalBeanIndex + direction));
    renderBeanModal();
  }

  function renderBeanModal() {
    const bean = selected[modalBeanIndex];
    if (!bean) return;
    const colourView = selectionContext.paired
      ? `<div class="modal-view paired"><div class="modal-view-half" style="background:${beanColour(bean, "caml")}"><strong>CamL view</strong></div><div class="modal-view-half" style="background:${beanColour(bean, "camr")}"><strong>CamR view</strong></div></div>`
      : `<div class="modal-view" style="background:${beanColour(bean)}"></div>`;
    document.getElementById("modal-content").innerHTML = `<div class="modal-heading"><p class="eyebrow">Numerical live evidence · ${escapeHtml(selectionContext.chart)}</p><h2 id="modal-bean-title">Bean #${escapeHtml(bean.sequence)}</h2><p>${escapeHtml(bean.bean_id)}</p></div>${colourView}<div class="modal-data"><div><strong>Combined Lab</strong><span>L* ${fmt(bean.lightness)} · a* ${fmt(bean.lab_a)} · b* ${fmt(bean.lab_b)} · C* ${fmt(bean.chroma)}</span></div><div><strong>CamL Lab</strong><span>L* ${fmt(bean.caml_lightness)} · a* ${fmt(bean.caml_lab_a)} · b* ${fmt(bean.caml_lab_b)} · C* ${fmt(bean.caml_chroma)}</span></div><div><strong>CamR Lab</strong><span>L* ${fmt(bean.camr_lightness)} · a* ${fmt(bean.camr_lab_a)} · b* ${fmt(bean.camr_lab_b)} · C* ${fmt(bean.camr_chroma)}</span></div><div><strong>Projected geometry</strong><span>Area ${fmt(bean.projected_area)} px² · volume ${compact(bean.volume_proxy)} px³</span></div><div><strong>Stereo agreement</strong><span>Area ratio ${fmt(bean.area_ratio)} · L* Δ ${fmt(bean.lightness_delta)}</span></div><div><strong>Capture quality</strong><span>${bean.sample_count || 0} sample(s) · ${bean.measurement_views || 0} view(s)${bean.sensor_edge ? " · sensor edge" : ""}${bean.enrichment_fallback ? " · fallback" : ""}</span></div></div>`;
    document.getElementById("modal-prev").disabled = modalBeanIndex === 0;
    document.getElementById("modal-next").disabled = modalBeanIndex === selected.length - 1;
    updateModalReviewCheckbox(bean);
  }

  function updateModalReviewCheckbox(bean) {
    const checkbox = document.getElementById("modal-review-checkbox");
    if (checkbox && bean && selected[modalBeanIndex]?.bean_id === bean.bean_id) checkbox.checked = Object.hasOwn(collection.entries, bean.bean_id);
  }

  function showToast(message) {
    const toast = document.getElementById("dashboard-toast");
    toast.textContent = message;
    toast.classList.add("visible");
    window.setTimeout(() => toast.classList.remove("visible"), 3200);
  }
  function galleryMetricLines(bean) {
    const metricName = selectionContext.metric;
    if (metricName === "volume_proxy") return [`Volume ${compact(bean.volume_proxy)} px³`, `Projected area ${fmt(bean.projected_area)} px²`];
    if (metricName === "projected_area") return [`Projected area ${fmt(bean.projected_area)} px²`, `Volume ${compact(bean.volume_proxy)} px³`];
    if (metricName === "area_ratio") return [`CamL ${fmt(bean.caml_area)} · CamR ${fmt(bean.camr_area)} px²`, `Area ratio ${fmt(bean.area_ratio)}`];
    if (metricName === "lightness_delta") return [`CamL L* ${fmt(bean.caml_lightness)} · CamR L* ${fmt(bean.camr_lightness)}`, `Lightness Δ ${fmt(bean.lightness_delta)}`];
    if (metricName === "caml_area,camr_area") return [`CamL ${fmt(bean.caml_area)} · CamR ${fmt(bean.camr_area)} px²`, `Ratio ${fmt(bean.area_ratio)} · volume ${compact(bean.volume_proxy)}`];
    if (metricName === "time") return [`Elapsed ${fmt(bean.time)} s · frame ${bean.first_frame ?? "—"}`, `L* ${fmt(bean.lightness)} · volume ${compact(bean.volume_proxy)}`];
    if (metricName === "time,lightness") return [`Elapsed ${fmt(bean.time)} s`, `L* ${fmt(bean.lightness)}`];
    if (metricName === "chroma") return [`Chroma ${fmt(bean.chroma)}`, `L* ${fmt(bean.lightness)} · a* ${fmt(bean.lab_a)} · b* ${fmt(bean.lab_b)}`];
    if (metricName === "outlier_score") return [`Outlier score ${fmt(bean.outlier_score)}`, `Percentile ${fmt(bean.outlier_percentile)}%`];
    return [`L* ${fmt(bean.lightness)} · a* ${fmt(bean.lab_a)} · b* ${fmt(bean.lab_b)}`, `${bean.sample_count || 0} sample${bean.sample_count === 1 ? "" : "s"}${bean.dark_2sd ? " · dark screen" : ""}`];
  }
  function selectionSwatch(bean) {
    if (!selectionContext.paired) return `<div class="swatch" style="background:${beanColour(bean)}"></div>`;
    return `<div class="paired-swatches"><div class="paired-swatch" style="background:${beanColour(bean, "caml")}"><span>CamL</span></div><div class="paired-swatch" style="background:${beanColour(bean, "camr")}"><span>CamR</span></div></div>`;
  }
  function beanColour(bean, view = "") {
    const prefix = view ? `${view}_` : "";
    const values = [bean[`${prefix}red`], bean[`${prefix}green`], bean[`${prefix}blue`]];
    if (!values.every(finite)) return "rgb(180,180,180)";
    return `rgb(${Math.round(values[0])},${Math.round(values[1])},${Math.round(values[2])})`;
  }

  function installCollectionControls() {
    document.querySelectorAll("#open-cart, [data-open-cart]").forEach(button => button.addEventListener("click", openCollection));
    document.getElementById("close-cart").addEventListener("click", () => closeDockedDrawer("collection"));
    document.getElementById("collection-name").addEventListener("input", event => {
      collection.label = event.target.value;
      persistCollection();
      renderCollection();
    });
    document.getElementById("export-cart-csv").addEventListener("click", exportCollectionCsv);
    document.getElementById("export-cart-json").addEventListener("click", exportCollectionJson);
    document.getElementById("import-cart-json").addEventListener("change", importCollectionJson);
    document.getElementById("clear-cart").addEventListener("click", () => {
      if (!Object.keys(collection.entries).length || window.confirm("Clear every bean from this review collection?")) {
        collection.entries = {};
        persistCollection();
        renderCollection();
        renderGallery();
        showToast("Review Collection cleared");
      }
    });
  }

  function loadCollection() {
    let saved = window.name.startsWith("beano-review-collection:") ? window.name.slice("beano-review-collection:".length) : null;
    if (!saved) {
      try {
        saved = localStorage.getItem(collectionStorageKey);
      } catch (_error) {
        collectionPersistenceAvailable = false;
      }
    }
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        if (parsed && parsed.run_id === summary.source_run_id && typeof parsed.entries === "object") collection = { label: String(parsed.label || "review-collection"), entries: parsed.entries || {} };
      } catch (_error) {
        collection = { label: "review-collection", entries: {} };
      }
    }
    document.getElementById("collection-name").value = collection.label;
    persistCollection();
    renderCollection();
  }

  function persistCollection() {
    const encoded = JSON.stringify({ schema: "beanoflight-review-collection/v1", run_id: summary.source_run_id, label: collection.label, entries: collection.entries });
    window.name = `beano-review-collection:${encoded}`;
    try {
      localStorage.setItem(collectionStorageKey, encoded);
      collectionPersistenceAvailable = true;
    } catch (_error) {
      collectionPersistenceAvailable = false;
    }
  }

  function openCollection() {
    openDockedDrawer("collection");
    renderCollection();
  }

  function collectionBeans() {
    return beans.filter(bean => Object.hasOwn(collection.entries, bean.bean_id));
  }

  function renderCollection() {
    const chosen = collectionBeans();
    document.querySelectorAll("#cart-count").forEach(target => { target.textContent = number.format(chosen.length); });
    const persistence = collectionPersistenceAvailable ? "saved in this browser" : "browser storage unavailable—export JSON to preserve it";
    document.getElementById("collection-summary").textContent = `${number.format(chosen.length)} unique beans · ${persistence}. CSV export includes full combined and per-view measurements plus selection provenance.`;
    const visible = chosen.slice(0, 250);
    document.getElementById("collection-list").innerHTML = visible.map(bean => {
      const sources = collection.entries[bean.bean_id]?.sources || [];
      return `<div class="collection-row"><div class="collection-swatch" style="background:${beanColour(bean)}"></div><div><strong>#${escapeHtml(bean.sequence)} · ${escapeHtml(bean.bean_id)}</strong><span>${escapeHtml(sources.join(" · "))}</span></div><button class="remove-cart-item" data-remove-bean="${escapeHtml(bean.bean_id)}" aria-label="Remove bean ${escapeHtml(bean.sequence)}">×</button></div>`;
    }).join("") + (chosen.length > visible.length ? `<div class="collection-more">${number.format(chosen.length - visible.length)} additional beans are included in exports.</div>` : "");
    document.querySelectorAll("[data-remove-bean]").forEach(button => button.addEventListener("click", () => {
      delete collection.entries[button.dataset.removeBean];
      persistCollection();
      renderCollection();
      renderGallery();
    }));
  }

  function exportCollectionCsv() {
    const chosen = collectionBeans();
    if (!chosen.length) return;
    const headers = ["collection_label", "source_run_id", "selection_sources", ...payload.fields];
    const rows = chosen.map(bean => {
      const entry = collection.entries[bean.bean_id];
      return [collection.label, summary.source_run_id, (entry.sources || []).join(" | "), ...payload.fields.map(name => bean[name])];
    });
    const csv = [headers.map(csvCell).join(","), ...rows.map(row => row.map(csvCell).join(","))].join("\r\n");
    download(new Blob([csv], { type: "text/csv;charset=utf-8" }), `${safeFilename(collection.label)}.csv`);
    const exportedCount = chosen.length;
    collection.entries = {};
    persistCollection();
    renderCollection();
    renderGallery();
    showToast(`Exported ${number.format(exportedCount)} beans; Review Collection cleared`);
  }

  function exportCollectionJson() {
    const document = {
      schema: "beanoflight-review-collection/v1",
      source_run_id: summary.source_run_id,
      collection_label: collection.label,
      exported_utc: new Date().toISOString(),
      entries: Object.values(collection.entries),
    };
    download(new Blob([JSON.stringify(document, null, 2)], { type: "application/json" }), `${safeFilename(collection.label)}.json`);
  }

  function importCollectionJson(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      try {
        const imported = JSON.parse(String(reader.result));
        if (imported.schema !== "beanoflight-review-collection/v1" || imported.source_run_id !== summary.source_run_id || !Array.isArray(imported.entries)) throw new Error("This collection does not belong to the current batch.");
        collection.label = String(imported.collection_label || collection.label);
        for (const entry of imported.entries) {
          if (!entry || !fieldBean(entry.bean_id)) continue;
          const existing = collection.entries[entry.bean_id] || { bean_id: entry.bean_id, sources: [], added_utc: entry.added_utc || new Date().toISOString() };
          existing.sources = [...new Set([...(existing.sources || []), ...(Array.isArray(entry.sources) ? entry.sources.map(String) : [])])];
          collection.entries[entry.bean_id] = existing;
        }
        document.getElementById("collection-name").value = collection.label;
        persistCollection();
        renderCollection();
        renderGallery();
      } catch (error) {
        window.alert(`Could not import collection: ${error.message}`);
      } finally {
        event.target.value = "";
      }
    });
    reader.readAsText(file);
  }

  function fieldBean(beanId) {
    return beansById.get(beanId);
  }

  function safeFilename(value) {
    const safe = String(value || "review-collection").trim().replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
    return safe || "review-collection";
  }

  function exportCsv(rows, filename) {
    const names = payload.fields;
    const csv = [names.join(","), ...rows.map(bean => names.map(name => csvCell(bean[name])).join(","))].join("\r\n");
    download(new Blob([csv], { type: "text/csv;charset=utf-8" }), filename);
  }
  function csvCell(value) {
    if (value == null) return "";
    const text = String(value);
    return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  }
  function exportSwatchSheet() {
    if (!selected.length) return;
    const cells = selected.slice(galleryPage * pageSize, (galleryPage + 1) * pageSize);
    const columns = 6, cellWidth = 180, cellHeight = 138, header = 82;
    const canvas = document.createElement("canvas");
    canvas.width = columns * cellWidth; canvas.height = header + Math.ceil(cells.length / columns) * cellHeight;
    const context = canvas.getContext("2d");
    context.fillStyle = "#f5f7f4"; context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "#17251d"; context.font = "bold 24px Georgia"; context.fillText("Beano colour-swatch review sheet", 20, 31);
    context.fillStyle = "#68736c"; context.font = "13px sans-serif"; context.fillText(`${selectedDescription} · page ${galleryPage + 1} · numerical live evidence, no images`, 20, 56);
    cells.forEach((bean, index) => {
      const x = (index % columns) * cellWidth, y = header + Math.floor(index / columns) * cellHeight;
      if (selectionContext.paired) {
        const half = (cellWidth - 16) / 2;
        context.fillStyle = beanColour(bean, "caml"); context.fillRect(x + 8, y + 6, half, 76);
        context.fillStyle = beanColour(bean, "camr"); context.fillRect(x + 8 + half, y + 6, half, 76);
        context.fillStyle = "rgba(15,35,24,.72)"; context.fillRect(x + 12, y + 62, 37, 16); context.fillRect(x + 8 + half + 4, y + 62, 39, 16);
        context.fillStyle = "white"; context.font = "bold 9px sans-serif"; context.fillText("CamL", x + 17, y + 74); context.fillText("CamR", x + 13 + half, y + 74);
      } else {
        context.fillStyle = beanColour(bean); context.fillRect(x + 8, y + 6, cellWidth - 16, 76);
      }
      context.strokeStyle = "#d7ddd8"; context.strokeRect(x + 8, y + 6, cellWidth - 16, 76);
      context.fillStyle = "#17251d"; context.font = "bold 12px monospace"; context.fillText(`#${bean.sequence}`, x + 9, y + 101);
      context.fillStyle = "#5f6a63"; context.font = "10px sans-serif"; context.fillText(galleryMetricLines(bean)[0].slice(0, 31), x + 9, y + 121);
    });
    canvas.toBlob(blob => { if (blob) download(blob, `bean-swatches-page-${galleryPage + 1}.png`); }, "image/png");
  }
  function download(blob, filename) {
    const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = filename; document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const active = document.querySelector(".nav-item.active");
      if (active) renderPage(active.dataset.page);
      else if (standaloneChart) renderStandaloneChart();
    }, 120);
  });
})();
