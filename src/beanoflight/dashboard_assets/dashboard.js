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
  const samplePairs = payload.sample_pairs || {};
  const stereoSamples = payload.stereo_samples || [];
  const trajectoryData = payload.trajectories || {};
  if (!stereoSamples.length) document.querySelectorAll(".focus-only").forEach(element => { element.style.display = "none"; });
  const summary = payload.summary || {};
  const counts = summary.counts || {};
  const distributions = summary.distributions || {};
  const dark = summary.dark_bean_screen || {};
  const hasPhotos = payload.image_policy?.images_available === true;
  const areaUnit = hasPhotos ? "mm²" : "px²";
  const volumeUnit = hasPhotos ? "mm³" : "px³";
  const number = new Intl.NumberFormat("en-ZA");
  const decimal = new Intl.NumberFormat("en-ZA", { maximumFractionDigits: 2 });
  const chartState = new WeakMap();
  let selected = [];
  let selectedDescription = "";
  let selectionContext = { metric: "appearance", paired: false, chart: "unknown" };
  let galleryPage = 0;
  let modalBeanIndex = -1;
  let modalSampleIndex = 0;
  let fullFrameOpen = false;
  let showCurrentPaths = false;
  let showFinalPaths = false;
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

  const focusByBean = new Map();
  for (const sample of stereoSamples) {
    if (sample[3] > 0 && sample[4] > 0) {
      if (!focusByBean.has(sample[0])) focusByBean.set(sample[0], []);
      focusByBean.get(sample[0]).push(sample[4] / sample[3]);
    }
  }
  for (const bean of beans) bean.focus_ratio = quantile(focusByBean.get(bean.bean_id) || [], .5);

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
    for (const [id, kind] of [["area-scatter", "area-scatter"], ["ratio-chart", "area-ratio"],
      ["delta-chart", "lightness-delta"], ["focus-ratio-chart", "focus-ratio"]]) {
      chartInterpretation(id, cameraComparison(kind));
    }
  }

  function chartInterpretation(canvasId, text) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !text) return;
    let paragraph = document.getElementById(`${canvasId}-interpretation`);
    if (!paragraph) {
      paragraph = document.createElement("p");
      paragraph.id = `${canvasId}-interpretation`;
      paragraph.className = "explanation chart-interpretation";
      canvas.parentElement.append(paragraph);
    }
    paragraph.textContent = `This bundle: ${text}`;
  }

  function comparisonCaution(kind) {
    if (kind === "focus") return "Higher focus proxy means stronger measured image gradients, not necessarily better optical focus. Texture, illumination, camera response and crop size also matter; this chart is not the separate full-size-only focus comparison.";
    if (kind === "lightness") return "This compares the two visible bean surfaces; lighting, surface colour and camera response can differ. It does not establish which camera is closer.";
    return "Larger projected area can be consistent with being closer to that camera, but pose, silhouette segmentation and calibration also affect it. This alone does not locate the waterfall or prescribe a movement.";
  }

  function cameraComparison(kind) {
    if (!["area-scatter", "area-ratio", "lightness-delta", "focus-ratio"].includes(kind)) return "";
    const metricKind = kind === "lightness-delta" ? "lightness" : kind === "focus-ratio" ? "focus" : "area";
    const neutral = metricKind === "lightness" ? 0 : 1;
    const values = kind === "area-scatter"
      ? beans.filter(b => finite(b.caml_area) && finite(b.camr_area) && b.caml_area > 0 && b.camr_area > 0).map(b => b.camr_area / b.caml_area)
      : beans.map(b => b[{"area-ratio": "area_ratio", "lightness-delta": "lightness_delta", "focus-ratio": "focus_ratio"}[kind]])
          .filter(v => finite(v) && (metricKind === "lightness" || v > 0));
    if (!values.length) return `No valid paired ${metricKind} measurements are available, so no camera comparison can be made.`;
    const left = values.filter(v => v < neutral).length;
    const right = values.filter(v => v > neutral).length;
    const equal = values.length - left - right;
    const description = {area: "larger in projected area", lightness: "brighter", focus: "higher in measured focus proxy"}[metricKind];
    const countsText = `${number.format(values.length)} beans with valid paired values: ${pct(left, values.length)} ${description} in CamL, ${pct(right, values.length)} in CamR, and ${pct(equal, values.length)} exactly equal.`;
    const direction = left === right
      ? (equal === values.length ? "All measured values are equal between cameras." : "Neither camera has more beans on its side of equality.")
      : `More beans are ${description} in ${left > right ? "CamL" : "CamR"} than in ${left > right ? "CamR" : "CamL"}.`;
    const median = quantile(values, .5);
    const medianText = metricKind === "lightness"
      ? `Median CamR − CamL lightness: ${fmt(median, 3)} L*.`
      : `Median CamR/CamL ${metricKind === "focus" ? "focus proxy" : "area"} ratio: ${fmt(median, 3)}.`;
    const axes = kind === "area-scatter" ? "Above the diagonal means larger in CamR; below means larger in CamL." :
      metricKind === "lightness" ? "Positive values mean brighter in CamR; negative values mean brighter in CamL." :
      `Ratios above 1 mean ${description} in CamR; below 1 mean ${description} in CamL.`;
    return `${direction} ${countsText} ${medianText} ${axes} ${values.length < 20 ? "Fewer than 20 beans: treat this as a small sample. " : ""}${comparisonCaution(metricKind)}`;
  }

  function bandInterpretation(values, kind) {
    const valid = values.map((value, index) => ({value, band: index + 1})).filter(p => finite(p.value));
    if (!valid.length) return "Not enough valid measurements in bands with at least 20 tracked beans to interpret height dependence.";
    const list = predicate => valid.filter(p => predicate(p.value)).map(p => p.band).join(", ") || "none";
    const description = kind === "focus" ? "higher measured focus proxy" : "larger projected area";
    const first = valid[0], last = valid[valid.length - 1];
    const endpoints = valid.length < 2 ? "Only one band is plotted, so no height change can be inferred." :
      `Uppermost plotted band ${first.band}: ${fmt(first.value, 3)}; lowermost plotted band ${last.band}: ${fmt(last.value, 3)}. ${last.value === first.value ? "The end-point ratios are equal." : `The lower end-point is relatively more weighted towards ${last.value > first.value ? "CamR" : "CamL"}; intermediate bands need not follow a steady trend.`}`;
    return `Bands run from 1 (top of image) to 10 (bottom). CamR has ${description} in bands ${list(v => v > 1)}; CamL in bands ${list(v => v < 1)}; equal medians in bands ${list(v => v === 1)}. ${endpoints} These are medians of sampled CamR/CamL ratios, not necessarily the same beans tracked through every band. ${comparisonCaution(kind)}`;
  }

  function renderSummary() {
    const total = counts.confirmed_beans || beans.length;
    const colourCount = counts.beans_with_colour == null ? beans.filter(bean => finite(bean.lightness)).length : counts.beans_with_colour;
    document.getElementById("summary-metrics").innerHTML = [
      metric("Confirmed beans", number.format(total), "One row per completed bean track"),
      metric("Two-sample coverage", pct(counts.beans_with_two_samples || 0, total), `${number.format(counts.beans_with_two_samples || 0)} beans`, counts.beans_with_two_samples === total ? "good" : "warn"),
      metric("Colour coverage", pct(colourCount, total), `${number.format(colourCount)} with approximate Lab`, colourCount === total ? "good" : "warn"),
      hasPhotos ? metric("Saved bean photographs", number.format(counts.saved_bean_crop_images ?? total * 2), "Individual CamL and CamR crop images") : metric("Measured observations", number.format(counts.derived_observations || 0), "Numerical measurements, not photographs"),
    ].join("");
    const complete = (counts.beans_without_samples || 0) === 0;
    document.getElementById("quality-banner").innerHTML = `${payload.classification === "test" ? '<div class="notice warn"><span class="notice-mark">!</span><div><strong>Test recording only</strong><span>Calibration witness did not pass for this run. Review these measurements and photographs as exploratory evidence, not production validation.</span></div></div>' : ""}<div class="notice ${complete ? "good" : "warn"}"><span class="notice-mark">${complete ? "✓" : "!"}</span><div><strong>${complete ? "Every confirmed bean has statistics" : "Some confirmed beans have no statistics"}</strong><span>${number.format(counts.beans_with_two_samples || 0)} have two samples; ${number.format(counts.beans_with_one_sample || 0)} use the explicit one-sample fallback; ${number.format(counts.beans_without_samples || 0)} have none.</span></div></div>`;
    renderRateMetrics("summary-rate-metrics");
  }

  function renderRateMetrics(targetId) {
    const runtime = summary.runtime || {};
    const run = runtime.summary || {};
    const outcome = runtime.outcome || {};
    const timed = beans.map(bean => bean.time).filter(finite);
    const trackSpan = timed.length > 1 ? Math.max(...timed) - Math.min(...timed) : null;
    const usesRunDuration = finite(run.elapsed_seconds) && run.elapsed_seconds > 0;
    const elapsed = usesRunDuration ? run.elapsed_seconds : trackSpan;
    const durationLabel = usesRunDuration ? "full run seconds" : "tracked-bean span seconds";
    const confirmedBeans = counts.confirmed_beans || beans.length;
    const inferenceJobs = finite(outcome.jobs_completed) ? outcome.jobs_completed : null;
    const measurements = finite(counts.derived_observations) ? counts.derived_observations : null;
    const rate = value => finite(value) && finite(elapsed) && elapsed > 0 ? value / elapsed : null;
    document.getElementById(targetId).innerHTML = [
      metric("Confirmed beans / second", finite(rate(confirmedBeans)) ? `${fmt(rate(confirmedBeans), 3)} /s` : "—", `${number.format(confirmedBeans)} beans over ${fmt(elapsed)} ${durationLabel}`),
      metric("Inferences / second", finite(rate(inferenceJobs)) ? `${fmt(rate(inferenceJobs), 3)} /s` : "—", inferenceJobs == null ? "No completed-inference counter attached" : `${number.format(inferenceJobs)} completed inference jobs over ${fmt(elapsed)} seconds`),
      metric("Measurements / second", finite(rate(measurements)) ? `${fmt(rate(measurements), 3)} /s` : "—", measurements == null ? "No statistics-observation counter attached" : `${number.format(measurements)} retained statistical observations over ${fmt(elapsed)} seconds`),
    ].join("");
  }

  function renderStereoMetrics() {
    const ratio = distributions.projected_area_ratio_camr_to_caml_median || {};
    const delta = distributions.approx_lab_l_view_delta_median || {};
    const focus = beans.map(bean => bean.focus_ratio).filter(finite);
    const focusLeft = stereoSamples.map(sample => sample[3]).filter(finite);
    const focusRight = stereoSamples.map(sample => sample[4]).filter(finite);
    document.getElementById("stereo-metrics").innerHTML = [
      metric("Median area ratio", fmt(ratio.p50), "CamR / CamL; ideal agreement is 1.00"),
      metric("Area ratio p10–p90", `${fmt(ratio.p10)}–${fmt(ratio.p90)}`, "Central 80% of measured beans"),
      metric("Median L* difference", fmt(delta.p50), "CamR minus CamL"),
      ...(focus.length ? [
        metric("Median focus ratio", fmt(quantile(focus, .5)), "CamR / CamL interior gradient proxy"),
        metric("Focus ratio p10–p90", `${fmt(quantile(focus, .1))}–${fmt(quantile(focus, .9))}`, `${number.format(focus.length)} beans with valid focus`),
        metric("Median focus per view", `${fmt(quantile(focusLeft, .5))} / ${fmt(quantile(focusRight, .5))}`, "CamL / CamR; relative proxy units"),
      ] : []),
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
    renderRateMetrics("health-rate-metrics");
    const target = document.getElementById("runtime-content");
    if (!runtime) {
      target.innerHTML = hasPhotos ? '<div class="notice warn"><span class="notice-mark">i</span><div><strong>Offline extraction only</strong><span>This bundle includes calibrated measurements from the recording, not a live pipeline acceptance report.</span></div></div>' + captureTable(sourceStats) : '<div class="notice warn"><span class="notice-mark">i</span><div><strong>No performance report attached</strong><span>Measurement completeness is available, but frame-rate, deadline, thermal, and pipeline-pressure evidence was not supplied when this bundle was generated.</span></div></div>' + captureTable(sourceStats);
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
      volume: { title: "Equivalent-sphere volume proxy", eyebrow: "Relative projected size", page: "size", explanation: `The ${volumeUnit} proxy is derived from projected area. Selection tiles report both this volume proxy and projected area so the two quantities remain distinct.` },
      area: { title: "Projected area proxy", eyebrow: "Projected footprint", page: "size", explanation: "This is the geometric mean of CamL and CamR areas where both are available, with an explicit one-view fallback." },
      "area-scatter": { title: "CamL area × CamR area", eyebrow: "Two-view geometry", page: "size", explanation: hasPhotos ? "Paired selections show calibrated CamL/CamR photographs and small mean-colour swatches alongside each view's projected area." : "Paired selection tiles show CamL and CamR mean-colour swatches alongside their separate projected areas. These are numerical view summaries, not retained photographs." },
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
    chartInterpretation(id, cameraComparison(standaloneChart));
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
      histogram(id, "volume_proxy", { xLabel: `Equivalent-sphere volume proxy (${volumeUnit})`, colour: "#32779a", bins: 72, chartId: "volume" });
    } else if (standaloneChart === "area") {
      histogram(id, "projected_area", { xLabel: `Projected area proxy (${areaUnit})`, colour: "#5f8466", bins: 72, chartId: "area" });
    } else if (standaloneChart === "area-scatter") {
      scatter(id, "caml_area", "camr_area", { xLabel: `CamL projected area (${areaUnit})`, yLabel: `CamR projected area (${areaUnit})`, colour: "#315f82", diagonal: true, paired: true, radius: 2, chartId: "area-scatter" });
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
        histogram("overview-size", "volume_proxy", { xLabel: `Volume proxy (${volumeUnit})`, colour: "#397c61", interactive: true, bins: 36, chartId: "volume" });
      } else if (name === "appearance") {
        renderAppearanceCharts();
      } else if (name === "size") {
        histogram("volume-chart", "volume_proxy", { xLabel: `Equivalent-sphere volume proxy (${volumeUnit})`, colour: "#32779a", chartId: "volume" });
        scatter("area-scatter", "caml_area", "camr_area", { xLabel: `CamL projected area (${areaUnit})`, yLabel: `CamR projected area (${areaUnit})`, colour: "#315f82", diagonal: true, paired: true, chartId: "area-scatter" });
        histogram("area-chart", "projected_area", { xLabel: `Projected area proxy (${areaUnit})`, colour: "#5f8466", chartId: "area" });
      } else if (name === "stereo") {
        histogram("ratio-chart", "area_ratio", { xLabel: "CamR area / CamL area", colour: "#44835b", marker: { value: 1, label: "equal area" }, paired: true, chartId: "area-ratio" });
        histogram("delta-chart", "lightness_delta", { xLabel: "CamR L* − CamL L*", colour: "#a15d83", marker: { value: 0, label: "equal lightness" }, paired: true, chartId: "lightness-delta" });
        if (stereoSamples.length) {
          histogram("focus-ratio-chart", "focus_ratio", { xLabel: "CamR / CamL focus proxy", colour: "#397c61", marker: { value: 1, label: "equal focus" }, paired: true, bins: 60, chartId: "focus-ratio" });
          renderStereoBands();
        }
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
    const candidates = dark.candidate_count || beans.filter(bean => bean.dark_2sd).length;
    document.getElementById("appearance-review-metrics").innerHTML = metric("Dark review set", number.format(candidates), `${pct(candidates, beans.length)} at mean − 2 SD`);
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
  function renderStereoBands() {
    const height = trajectoryData.image_size_px?.[1] || 1088;
    const bands = Array.from({ length: 10 }, () => ({ focus: [], area: [], left: [], right: [], beans: new Set() }));
    for (const sample of stereoSamples) {
      if (!finite(sample[2])) continue;
      const band = bands[Math.max(0, Math.min(9, Math.floor(sample[2] / height * 10)))];
      if (sample[3] > 0 && sample[4] > 0) {
        band.focus.push(sample[4] / sample[3]);
        band.left.push(sample[3]); band.right.push(sample[4]);
      }
      if (finite(sample[5]) && sample[5] > 0) band.area.push(sample[5]);
      band.beans.add(sample[0]);
    }
    for (const [id, key, colour] of [["focus-band-chart", "focus", "#397c61"], ["area-band-chart", "area", "#4483a3"]]) {
      const canvas = document.getElementById(id);
      if (!canvas) continue;
      const values = bands.map(band => band.beans.size >= 20 ? quantile(band[key], .5) : null);
      chartInterpretation(id, bandInterpretation(values, key));
      const { context, width, height: canvasHeight } = canvasContext(canvas);
      const area = plotArea(width, canvasHeight);
      const domain = extent([1, ...values.filter(finite)], .1);
      axes(context, area, [1, 10], domain, "Horizontal band (top to bottom)", `Median CamR / CamL ${key}`);
      const unity = scale(1, domain[0], domain[1], area.bottom, area.top);
      context.strokeStyle = "#7d8580"; context.setLineDash([4, 4]); context.beginPath(); context.moveTo(area.left, unity); context.lineTo(area.right, unity); context.stroke(); context.setLineDash([]);
      context.strokeStyle = colour; context.fillStyle = colour; context.lineWidth = 2;
      values.forEach((value, index) => {
        if (!finite(value)) return;
        const x = scale(index + 1, 1, 10, area.left, area.right);
        const y = scale(value, domain[0], domain[1], area.bottom, area.top);
        context.beginPath(); context.arc(x, y, 4, 0, Math.PI * 2); context.fill();
        if (index && finite(values[index - 1])) {
          const previousX = scale(index, 1, 10, area.left, area.right);
          const previousY = scale(values[index - 1], domain[0], domain[1], area.bottom, area.top);
          context.beginPath(); context.moveTo(previousX, previousY); context.lineTo(x, y); context.stroke();
        }
      });
    }
    const levelCanvas = document.getElementById("focus-level-chart");
    if (levelCanvas) {
      const { context, width, height: canvasHeight } = canvasContext(levelCanvas);
      const area = plotArea(width, canvasHeight);
      const left = bands.map(band => band.beans.size >= 20 ? quantile(band.left, .5) : null);
      const right = bands.map(band => band.beans.size >= 20 ? quantile(band.right, .5) : null);
      const valid = left.map((value, index) => ({left: value, right: right[index], band: index + 1}))
        .filter(p => finite(p.left) && finite(p.right));
      const list = predicate => valid.filter(predicate).map(p => p.band).join(", ") || "none";
      const endpoints = valid.length >= 2 ? `From uppermost plotted band ${valid[0].band} to lowermost plotted band ${valid[valid.length - 1].band}, CamL changes from ${fmt(valid[0].left, 3)} to ${fmt(valid[valid.length - 1].left, 3)} and CamR from ${fmt(valid[0].right, 3)} to ${fmt(valid[valid.length - 1].right, 3)}. This end-point comparison need not describe a steady trend.` : "Too few populated bands to infer a height change.";
      chartInterpretation("focus-level-chart", valid.length ? `CamL (green) has the higher median focus proxy in bands ${list(p => p.left > p.right)}; CamR (blue) in bands ${list(p => p.right > p.left)}; equal values in bands ${list(p => p.left === p.right)}. Bands run from top (1) to bottom (10) of the image. ${endpoints} These are separate per-camera sample medians, not a median of paired ratios. ${comparisonCaution("focus")}` : "Not enough paired focus measurements in bands with at least 20 tracked beans to interpret height dependence.");
      const domain = extent([...left, ...right].filter(finite), .1);
      axes(context, area, [1, 10], domain, "Horizontal band (top to bottom)", "Median focus proxy");
      for (const [values, colour] of [[left, "#397c61"], [right, "#4483a3"]]) {
        context.strokeStyle = colour; context.fillStyle = colour; context.lineWidth = 2;
        values.forEach((value, index) => {
          if (!finite(value)) return;
          const x = scale(index + 1, 1, 10, area.left, area.right);
          const y = scale(value, domain[0], domain[1], area.bottom, area.top);
          context.beginPath(); context.arc(x, y, 4, 0, Math.PI * 2); context.fill();
          if (index && finite(values[index - 1])) {
            context.beginPath();
            context.moveTo(scale(index, 1, 10, area.left, area.right), scale(values[index - 1], domain[0], domain[1], area.bottom, area.top));
            context.lineTo(x, y); context.stroke();
          }
        });
      }
    }
    const areaMedians = bands.map(band => band.beans.size >= 20 ? quantile(band.area, .5) : null);
    const crossings = [];
    for (let index = 1; index < areaMedians.length; index++) {
      const before = areaMedians[index - 1], after = areaMedians[index];
      if (finite(before) && finite(after) && (before - 1) * (after - 1) <= 0)
        crossings.push(`${index}–${index + 1}`);
    }
    document.getElementById("stereo-band-counts").textContent = `Unique beans per band, top to bottom: ${bands.map(band => number.format(band.beans.size)).join(" · ")}. Bands with fewer than 20 beans are not plotted. ${crossings.length ? `Area-ratio unity is crossed between bands ${crossings.join(", ")}; this is a balance clue, not a measured plane intersection.` : "No robust area-ratio unity crossing was observed."}`;
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
      target.innerHTML = `<strong>Bean ${escapeHtml(detailBean.sequence)} details</strong><br>Combined: L* ${fmt(detailBean.lightness)} · a* ${fmt(detailBean.lab_a)} · b* ${fmt(detailBean.lab_b)} · chroma ${fmt(detailBean.chroma)}<br>CamL: L* ${fmt(detailBean.caml_lightness)} · a* ${fmt(detailBean.caml_lab_a)} · b* ${fmt(detailBean.caml_lab_b)} · area ${fmt(detailBean.caml_area)} ${areaUnit}<br>CamR: L* ${fmt(detailBean.camr_lightness)} · a* ${fmt(detailBean.camr_lab_a)} · b* ${fmt(detailBean.camr_lab_b)} · area ${fmt(detailBean.camr_area)} ${areaUnit}<br>Volume proxy ${compact(detailBean.volume_proxy)} ${volumeUnit} · area ratio ${fmt(detailBean.area_ratio)} · L* Δ ${fmt(detailBean.lightness_delta)}<br>${detailBean.sample_count || 0} sample(s) · ${detailBean.measurement_views || 0} view(s) minimum${detailBean.sensor_edge ? " · sensor-edge flag" : ""}${detailBean.enrichment_fallback ? " · enrichment fallback" : ""}`;
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
    modalSampleIndex = 0;
    renderBeanModal();
    document.getElementById("bean-modal").classList.add("open");
  }

  function closeBeanModal() {
    document.getElementById("bean-modal").classList.remove("open");
  }

  function moveBeanModal(direction) {
    if (!selected.length) return;
    modalBeanIndex = Math.min(selected.length - 1, Math.max(0, modalBeanIndex + direction));
    modalSampleIndex = 0;
    // Frame visibility and trajectory overlays are viewing preferences, not bean state.
    // Keep all three when navigating; only the selected capture starts over.
    renderBeanModal();
  }

  function renderBeanModal() {
    const bean = selected[modalBeanIndex];
    if (!bean) return;
    const samples = samplePairs[bean.bean_id] || [{ CamL: bean.photo_CamL, CamR: bean.photo_CamR, frame_index: bean.first_frame }];
    modalSampleIndex = Math.min(modalSampleIndex, samples.length - 1);
    const sample = samples[modalSampleIndex];
    const colourView = hasPhotos ? `<div class="sample-nav"><button class="button secondary" id="sample-prev" aria-label="Previous capture">←</button><span>Capture ${modalSampleIndex + 1} of ${samples.length} · frames CamL ${sample.frame_index ?? "—"} / CamR ${sample.right_frame_index ?? "—"}</span><button class="button secondary" id="sample-next" aria-label="Next capture">→</button></div><div class="modal-photo-pair"><figure><img src="${escapeHtml(sample.CamL)}" alt="CamL calibrated crop of bean ${escapeHtml(bean.sequence)}"><figcaption>CamL <i style="background:${beanColour(bean, "caml")}"></i></figcaption></figure><figure><img src="${escapeHtml(sample.CamR)}" alt="CamR calibrated crop of bean ${escapeHtml(bean.sequence)}"><figcaption>CamR <i style="background:${beanColour(bean, "camr")}"></i></figcaption></figure></div><div class="frame-controls"><button class="button secondary" id="toggle-frame">${fullFrameOpen ? "Hide entire frame" : "View entire frame"}</button>${fullFrameOpen ? '<label><input id="show-current-paths" type="checkbox"> Show all trajectories as known at this frame</label><label><input id="show-final-paths" type="checkbox"> Show all final trajectories + extrapolated tails</label>' : ""}</div>${fullFrameOpen ? '<p class="frame-status" id="frame-status">Full frame not available — showing recorded bean positions on black. CamR non-sample paths are projected; dotted tails are visual extrapolations only.</p><div class="frame-pair"><div class="frame-panel"><strong>CamL</strong><img id="full-CamL" alt="Full CamL frame"><canvas id="overlay-CamL"></canvas></div><div class="frame-panel"><strong>CamR</strong><img id="full-CamR" alt="Full CamR frame"><canvas id="overlay-CamR"></canvas></div></div>' : ""}` : selectionContext.paired
      ? `<div class="modal-view paired"><div class="modal-view-half" style="background:${beanColour(bean, "caml")}"><strong>CamL view</strong></div><div class="modal-view-half" style="background:${beanColour(bean, "camr")}"><strong>CamR view</strong></div></div>`
      : `<div class="modal-view" style="background:${beanColour(bean)}"></div>`;
    const captureMetrics = hasPhotos && (finite(sample.focus_ratio) || finite(sample.area_ratio))
      ? `<p class="frame-status">This capture: focus R/L ${fmt(sample.focus_ratio)} · projected area R/L ${fmt(sample.area_ratio)}</p>` : "";
    document.getElementById("modal-content").innerHTML = `<div class="modal-heading"><p class="eyebrow">${hasPhotos ? "Calibrated paired photographs" : "Numerical live evidence"} · ${escapeHtml(selectionContext.chart)}</p><h2 id="modal-bean-title">Bean #${escapeHtml(bean.sequence)}${hasPhotos ? ` (${modalSampleIndex + 1}/${samples.length})` : ""}</h2><p>${escapeHtml(bean.bean_id)}</p></div>${colourView}${captureMetrics}<div class="modal-data"><div><strong>Combined Lab</strong><span>L* ${fmt(bean.lightness)} · a* ${fmt(bean.lab_a)} · b* ${fmt(bean.lab_b)} · C* ${fmt(bean.chroma)}</span></div><div><strong>CamL Lab</strong><span>L* ${fmt(bean.caml_lightness)} · a* ${fmt(bean.caml_lab_a)} · b* ${fmt(bean.caml_lab_b)} · C* ${fmt(bean.caml_chroma)}</span></div><div><strong>CamR Lab</strong><span>L* ${fmt(bean.camr_lightness)} · a* ${fmt(bean.camr_lab_a)} · b* ${fmt(bean.camr_lab_b)} · C* ${fmt(bean.camr_chroma)}</span></div><div><strong>Projected geometry</strong><span>Area ${fmt(bean.projected_area)} ${areaUnit} · volume ${compact(bean.volume_proxy)} ${volumeUnit}</span></div><div><strong>Stereo agreement</strong><span>Area ratio ${fmt(bean.area_ratio)} · focus ratio ${fmt(bean.focus_ratio)} · L* Δ ${fmt(bean.lightness_delta)}</span></div><div><strong>Capture quality</strong><span>${bean.sample_count || 0} sample(s) · ${bean.measurement_views || 0} view(s)${bean.sensor_edge ? " · sensor edge" : ""}${bean.enrichment_fallback ? " · fallback" : ""}</span></div></div>`;
    if (hasPhotos) {
      document.getElementById("sample-prev").disabled = modalSampleIndex === 0;
      document.getElementById("sample-next").disabled = modalSampleIndex === samples.length - 1;
      document.getElementById("sample-prev").onclick = () => { modalSampleIndex--; renderBeanModal(); };
      document.getElementById("sample-next").onclick = () => { modalSampleIndex++; renderBeanModal(); };
      document.getElementById("toggle-frame").onclick = () => { fullFrameOpen = !fullFrameOpen; renderBeanModal(); };
      if (fullFrameOpen) {
        document.getElementById("show-current-paths").checked = showCurrentPaths;
        document.getElementById("show-final-paths").checked = showFinalPaths;
        document.getElementById("show-current-paths").onchange = event => { showCurrentPaths = event.target.checked; drawFrameOverlay(sample); };
        document.getElementById("show-final-paths").onchange = event => { showFinalPaths = event.target.checked; drawFrameOverlay(sample); };
        loadFullFrames(sample);
        drawFrameOverlay(sample);
      }
    }
    document.getElementById("modal-prev").disabled = modalBeanIndex === 0;
    document.getElementById("modal-next").disabled = modalBeanIndex === selected.length - 1;
    updateModalReviewCheckbox(bean);
  }

  function loadFullFrames(sample) {
    if (!Number.isInteger(sample.frame_index)) return;
    for (const camera of ["CamL", "CamR"]) {
      const image = document.getElementById(`full-${camera}`);
      const portable = sample[`full_${camera}`];
      if (!portable && !/^https?:$/.test(location.protocol)) continue;
      image.onload = () => {
        const status = document.getElementById("frame-status");
        if (status) status.textContent = `${portable ? "Portable full frame loaded from this bundle" : "Source frame loaded from recording"}. Green dots are observed now; amber dots are last known positions. CamR non-sample paths are central-plane projections; dotted tails are visual, not nozzle-crossing predictions.`;
      };
      image.onerror = () => { image.removeAttribute("src"); };
      image.src = portable || `/api/frame?frame=${encodeURIComponent(sample.frame_index)}&camera=${camera}`;
    }
  }

  function drawFrameOverlay(sample) {
    const positions = trajectoryData.frame_positions?.[String(sample.frame_index)] || [];
    const finalPaths = trajectoryData.final_paths || {};
    const width = trajectoryData.image_size_px?.[0] || 1456;
    const height = trajectoryData.image_size_px?.[1] || 1088;
    const frame = Number(sample.frame_index);
    for (const camera of ["CamL", "CamR"]) {
      const canvas = document.getElementById(`overlay-${camera}`);
      if (!canvas) continue;
      canvas.width = width; canvas.height = height;
      const ctx = canvas.getContext("2d");
      const xIndex = camera === "CamL" ? 1 : 4;
      const yIndex = camera === "CamL" ? 2 : 5;
      for (const point of positions) {
        const identity = `/${String(point[0]).padStart(6, "0")}`;
        const path = Object.entries(finalPaths).find(([key]) => key.endsWith(identity))?.[1] || [];
        const pathX = camera === "CamL" ? 1 : 3;
        const pathY = camera === "CamL" ? 2 : 4;
        for (const [enabled, subset, colour] of [
          [showCurrentPaths, path.filter(entry => entry[0] <= frame), "#62d6ff"],
          [showFinalPaths, path, "#ffe78c"],
        ]) {
          if (!enabled || subset.length < 2) continue;
          ctx.strokeStyle = colour; ctx.lineWidth = 2;
          if (camera === "CamR") ctx.setLineDash([6, 4]);
          ctx.beginPath(); ctx.moveTo(subset[0][pathX], subset[0][pathY]);
          for (const entry of subset.slice(1)) ctx.lineTo(entry[pathX], entry[pathY]);
          ctx.stroke(); ctx.setLineDash([]);
          if (enabled === showFinalPaths && colour === "#ffe78c" && subset.length > 1) {
            const last = subset.at(-1), previous = subset.at(-2);
            const dy = last[pathY] - previous[pathY];
            if (dy > 0) {
              const extension = Math.min(120, Math.max(0, height - last[pathY]));
              ctx.strokeStyle = colour; ctx.setLineDash([3, 5]); ctx.beginPath();
              ctx.moveTo(last[pathX], last[pathY]);
              ctx.lineTo(last[pathX] + (last[pathX] - previous[pathX]) / dy * extension, last[pathY] + extension);
              ctx.stroke(); ctx.setLineDash([]);
            }
          }
        }
        if (!finite(point[xIndex]) || !finite(point[yIndex])) continue;
        ctx.fillStyle = point[3] ? "#69f6a7" : "#ffd568";
        ctx.beginPath(); ctx.arc(point[xIndex], point[yIndex], 8, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = "#10271c"; ctx.lineWidth = 2; ctx.stroke();
        ctx.font = "bold 21px system-ui"; ctx.fillStyle = "white";
        ctx.fillText(`#${point[0]}${camera === "CamR" && !point[6] ? " ~" : ""}`, point[xIndex] + 12, point[yIndex] - 10);
      }
    }
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
    if (metricName === "volume_proxy") return [`Volume ${compact(bean.volume_proxy)} ${volumeUnit}`, `Projected area ${fmt(bean.projected_area)} ${areaUnit}`];
    if (metricName === "projected_area") return [`Projected area ${fmt(bean.projected_area)} ${areaUnit}`, `Volume ${compact(bean.volume_proxy)} ${volumeUnit}`];
    if (metricName === "area_ratio") return [`CamL ${fmt(bean.caml_area)} · CamR ${fmt(bean.camr_area)} ${areaUnit}`, `Area ratio ${fmt(bean.area_ratio)}`];
    if (metricName === "focus_ratio") return [`CamR/CamL focus proxy ${fmt(bean.focus_ratio)}`, `Area ratio ${fmt(bean.area_ratio)}`];
    if (metricName === "lightness_delta") return [`CamL L* ${fmt(bean.caml_lightness)} · CamR L* ${fmt(bean.camr_lightness)}`, `Lightness Δ ${fmt(bean.lightness_delta)}`];
    if (metricName === "caml_area,camr_area") return [`CamL ${fmt(bean.caml_area)} · CamR ${fmt(bean.camr_area)} ${areaUnit}`, `Ratio ${fmt(bean.area_ratio)} · volume ${compact(bean.volume_proxy)}`];
    if (metricName === "time") return [`Elapsed ${fmt(bean.time)} s · frame ${bean.first_frame ?? "—"}`, `L* ${fmt(bean.lightness)} · volume ${compact(bean.volume_proxy)}`];
    if (metricName === "time,lightness") return [`Elapsed ${fmt(bean.time)} s`, `L* ${fmt(bean.lightness)}`];
    if (metricName === "chroma") return [`Chroma ${fmt(bean.chroma)}`, `L* ${fmt(bean.lightness)} · a* ${fmt(bean.lab_a)} · b* ${fmt(bean.lab_b)}`];
    if (metricName === "outlier_score") return [`Outlier score ${fmt(bean.outlier_score)}`, `Percentile ${fmt(bean.outlier_percentile)}%`];
    return [`L* ${fmt(bean.lightness)} · a* ${fmt(bean.lab_a)} · b* ${fmt(bean.lab_b)}`, `${bean.sample_count || 0} sample${bean.sample_count === 1 ? "" : "s"}${bean.dark_2sd ? " · dark screen" : ""}`];
  }
  function selectionSwatch(bean) {
    if (hasPhotos) return `<div class="photo-pair"><div><img loading="lazy" src="${escapeHtml(bean.photo_CamL)}" alt="CamL bean ${escapeHtml(bean.sequence)}"><span>CamL</span></div><div><img loading="lazy" src="${escapeHtml(bean.photo_CamR)}" alt="CamR bean ${escapeHtml(bean.sequence)}"><span>CamR</span></div></div><div class="mini-swatches"><i style="background:${beanColour(bean, "caml")}"></i><i style="background:${beanColour(bean, "camr")}"></i></div>`;
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
      const preview = hasPhotos ? `<div class="collection-photos"><img src="${escapeHtml(bean.photo_CamL)}" alt="CamL"><img src="${escapeHtml(bean.photo_CamR)}" alt="CamR"><i style="background:${beanColour(bean)}"></i></div>` : `<div class="collection-swatch" style="background:${beanColour(bean)}"></div>`;
      return `<div class="collection-row">${preview}<div><strong>#${escapeHtml(bean.sequence)} · ${escapeHtml(bean.bean_id)}</strong><span>${escapeHtml(sources.join(" · "))}</span></div><button class="remove-cart-item" data-remove-bean="${escapeHtml(bean.bean_id)}" aria-label="Remove bean ${escapeHtml(bean.sequence)}">×</button></div>`;
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
