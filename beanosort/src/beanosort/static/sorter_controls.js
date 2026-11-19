// Copied from ESP_Web_controller on 2026-11-16; authenticated HTTP transport replaces its direct WebSocket.
"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

// Per-page identities prevent a delayed unload release affecting a new page.
const clientId = crypto.randomUUID ? crypto.randomUUID() : `client-${Date.now()}-${Math.random().toString(36).slice(2)}`;
const csrf = document.querySelector('meta[name="sorter-csrf"]').content;
const connectionId = document.querySelector('meta[name="sorter-connection"]').content;
let state = null;
let pageClosed = false;
let heartbeatPending = false;
let lastLogSignature = "";
let valveRequestPending = false;
const editedSettings = new Set();

function preserveEditedSettings() {
  const groups = [
    ["#trigger-frequency", "#trigger-low"],
    ["#strobe-lead", "#strobe-lag"],
    ["#high-speed-mode", "#high-speed-multiplier", "#high-speed-frequency", "#high-speed-pulse", "#high-speed-illumination"],
    ["#vibration-frequency", "#vibration-duty-slider", "#vibration-duty", "#vibration-direction"],
    ["#conveyor-speed", "#conveyor-acceleration", "#conveyor-direction"],
  ];
  groups.forEach((ids) => {
    const preserve = () => ids.forEach((id) => editedSettings.add(id));
    ids.forEach((id) => {
      $(id).addEventListener("input", preserve);
      $(id).addEventListener("change", preserve);
    });
  });
}

function number(id) {
  return Number($(id).value);
}

async function loadCandidateSettings() {
  const preset = await api("/static/candidate-settings.json");
  const values = {
    "#trigger-frequency": preset.cameras.frame_rate_hz,
    "#trigger-low": preset.cameras.trigger_low_us,
    "#strobe-lead": preset.cameras.strobe_lead_us,
    "#strobe-lag": preset.cameras.strobe_lag_us,
    "#vibration-frequency": preset.vibration_motor.frequency_hz,
    "#vibration-duty": preset.vibration_motor.duty_percent,
    "#vibration-duty-slider": preset.vibration_motor.duty_percent,
    "#vibration-direction": preset.vibration_motor.direction,
    "#conveyor-speed": preset.conveyor.speed_steps_per_second,
    "#conveyor-acceleration": preset.conveyor.acceleration_steps_per_second_squared,
    "#conveyor-direction": preset.conveyor.direction,
  };
  Object.entries(values).forEach(([id, value]) => {
    editedSettings.add(id);
    $(id).value = value;
    $(id).dispatchEvent(new Event("input", { bubbles: true }));
  });
  $("#candidate-settings-status").textContent =
    `${preset.name} loaded. Use Apply settings to send values. Air pressure: ${preset.air_pressure.value} PSI, set manually.`;
}

function setInput(id, value) {
  const element = $(id);
  if (!editedSettings.has(id) && document.activeElement !== element && value !== null && value !== undefined) {
    element.value = value;
  }
}

function stepNumberInput(input, direction) {
  if (input.disabled || !Number.isFinite(input.valueAsNumber)) return;
  const previousValue = input.value;
  if (direction > 0) input.stepUp();
  else input.stepDown();
  if (input.value !== previousValue) {
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }
}

function captureFocusedWheel(input) {
  input.addEventListener("wheel", (event) => {
    if (document.activeElement !== input || event.deltaY === 0) return;
    event.preventDefault();
    stepNumberInput(input, event.deltaY < 0 ? 1 : -1);
  }, { passive: false });
}

function isOwner() {
  return Boolean(state?.lease?.active && state.lease.client_id === clientId);
}

function toast(message, kind = "info") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  $("#toast-stack").appendChild(node);
  setTimeout(() => node.remove(), 4200);
}

async function api(path, options = {}, quiet = false) {
  const url = path === "/static/candidate-settings.json"
    ? "/static/sorter_candidate_settings.json" : "/sorter-controls" + path;
  const response = await fetch(url, {
    ...options, credentials: "same-origin", cache: "no-store",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf,
      "X-Sorter-Connection": connectionId, "X-Sorter-Client": clientId, ...(options.headers || {}) },
  });
  let payload = null;
  try { payload = await response.json(); } catch (_) { /* Authentication may redirect to HTML. */ }
  if (!response.ok || response.redirected || !payload) {
    const message = payload?.detail || (response.redirected ? "Session expired. Sign in to BeanoSort again." : "Cannot confirm the machine response.");
    if (!quiet) toast(typeof message === "string" ? message : JSON.stringify(message), "error");
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return payload;
}

async function action(fn, successMessage) {
  try {
    await fn();
    if (successMessage) toast(successMessage);
  } catch (error) {
    console.error(error);
  }
}

function post(path, body) {
  return api(path, { method: "POST", body: JSON.stringify(body) });
}

function withClient(values = {}) {
  return { client_id: clientId, ...values };
}

function formatUptime(milliseconds) {
  if (milliseconds === null || milliseconds === undefined) return "—";
  let seconds = Math.floor(milliseconds / 1000);
  const days = Math.floor(seconds / 86400);
  seconds %= 86400;
  const hours = Math.floor(seconds / 3600);
  seconds %= 3600;
  const minutes = Math.floor(seconds / 60);
  return `${days ? `${days}d ` : ""}${hours}h ${minutes}m`;
}

function updateState(next) {
  state = next;
  const connected = Boolean(state.connection.connected);
  const owner = isOwner();

  const connectionChip = $("#connection-chip");
  connectionChip.classList.toggle("connected", connected);
  $("#connection-label").textContent = connected ? "ESP32 CONNECTED" : "ESP32 DISCONNECTED";
  $("#mode-chip").textContent = `MODE ${state.mode || "UNKNOWN"}`;
  $("#mode-chip").classList.toggle("failsafe", state.mode === "FAILSAFE");
  $("#stop-all").disabled = !connected && !state.valves?.connected;

  let controlText;
  if (!connected) {
    controlText = state.connection.error || "Waiting for the ESP32 serial connection.";
  } else if (owner) {
    controlText = "You have control. Closing this page or losing its heartbeat stops all outputs.";
  } else if (state.lease.active) {
    controlText = `${state.lease.operator_name || "Another operator"} currently has exclusive control. This page is read-only.`;
  } else {
    controlText = "Outputs are locked. Take control to operate the machine.";
  }
  $("#control-message").textContent = controlText;
  $("#take-control").classList.toggle("hidden", owner);
  $("#take-control").disabled = !connected || (state.lease.active && !owner);
  $("#release-control").classList.toggle("hidden", !owner);
  $$(".requires-control").forEach((element) => { element.disabled = !owner || !connected; });
  updateValveState();

  const trigger = state.triggers;
  const strobe = state.strobe;
  setInput("#trigger-frequency", trigger.frequency_hz);
  setInput("#trigger-low", trigger.low_us);
  setInput("#strobe-lead", strobe.lead_us);
  setInput("#strobe-lag", strobe.lag_us);
  setOutputState("#trigger-state", trigger.enabled);
  setOutputState("#strobe-state", strobe.enabled);
  setToggle("#toggle-trigger", trigger.enabled, "Stop triggers", "Start triggers");
  setToggle("#toggle-strobe", strobe.enabled, "Stop illumination", "Start illumination");
  $("#toggle-strobe").disabled = !owner || !connected;
  $("#pulse-strobe").disabled = !owner || !connected;
  $("#pulse-trigger").disabled = !owner || !connected || trigger.enabled;

  const high = state.high_speed || {};
  setInput("#high-speed-mode", high.mode);
  setInput("#high-speed-multiplier", high.multiplier);
  setInput("#high-speed-frequency", high.fixed_frequency_hz);
  setInput("#high-speed-pulse", high.pulse_us);
  if (!editedSettings.has("#high-speed-illumination") && high.illumination !== undefined) {
    $("#high-speed-illumination").checked = high.illumination;
  }
  setOutputState("#high-speed-state", high.enabled);
  setToggle("#toggle-high-speed", high.enabled, "Stop high-speed triggers", "Start high-speed triggers");
  $("#toggle-high-speed").disabled = !owner || !connected || !high.available || (!high.enabled && !high.valid);
  $("#apply-high-speed").disabled = !owner || !connected || !high.available || trigger.enabled || strobe.enabled || high.enabled;
  $("#high-speed-live").textContent = high.available
    ? `Applied rate: ${Number(high.frequency_hz).toFixed(3)} Hz · ${high.mode === "multiple" ? `${high.multiplier}× paired rate` : "fixed rate"} · ${high.enabled ? "running" : "stopped"}${high.valid ? "" : " · cannot start at the current paired rate"}`
    : "High-speed triggers require the updated ESP32 firmware.";
  const ledRate = strobe.effective_frequency_hz ?? (high.enabled && high.illumination ? high.frequency_hz : 1e6 / Math.round(1e6 / trigger.frequency_hz));
  $("#strobe-effective-rate").textContent = strobe.enabled ? `Output rate: ${Number(ledRate).toFixed(3)} Hz` : "Output off";
  updateHighSpeedPreview();

  const vibration = state.vibration_motor;
  const vibrationInputs = [
    $("#vibration-frequency"),
    $("#vibration-duty-slider"),
    $("#vibration-duty"),
    $("#vibration-direction"),
  ];
  if (!vibrationInputs.includes(document.activeElement)) {
    setInput("#vibration-frequency", vibration.frequency_hz);
    setInput("#vibration-duty-slider", vibration.duty_percent);
    setInput("#vibration-duty", vibration.duty_percent);
    setInput("#vibration-direction", vibration.direction);
  }
  setOutputState("#vibration-state", vibration.enabled);
  setToggle("#toggle-vibration", vibration.enabled, "Stop motor", "Start motor");
  updateVibrationPreview();

  const conveyor = state.conveyor;
  const conveyorInputs = [
    $("#conveyor-speed"),
    $("#conveyor-acceleration"),
    $("#conveyor-direction"),
  ];
  if (!conveyorInputs.includes(document.activeElement)) {
    setInput("#conveyor-speed", conveyor.speed_sps);
    setInput("#conveyor-acceleration", conveyor.acceleration_sps2);
    setInput("#conveyor-direction", conveyor.direction);
  }
  updateConveyorState(conveyor);

  $("#device-port").textContent = state.connection.port || "—";
  $("#device-firmware").textContent = state.connection.firmware || "—";
  $("#device-uptime").textContent = formatUptime(state.stats.uptime_ms);
  $("#device-error").textContent = state.stats.last_error || state.connection.error || "NONE";

  updateWaveform();
  renderLogs(state.logs || []);
}

function setOutputState(selector, enabled) {
  const element = $(selector);
  element.textContent = enabled ? "ON" : "OFF";
  element.classList.toggle("on", enabled);
}

function updateValveState() {
  const valves = state?.valves || {};
  const ready = valves.available && valves.connected && valves.synchronized;
  $("#valve-state").textContent = !ready ? "OFFLINE" : valves.testing ? "TESTING" : valves.busy ? "BUSY" : "READY";
  $("#valve-state").classList.toggle("on", Boolean(valves.testing));
  $("#valve-status").textContent = valves.error || (!ready
    ? "Start BeanoActuator and connect the valve ESP32 with the updated firmware."
    : valves.testing ? "Running −V1 → V0 → +V1. All outputs return off after the sequence."
    : valves.busy ? "Sorting outputs are busy. Wait until they are idle to test."
    : "Ready · 50 ms on, 450 ms off between channels · 1.05 seconds total.");
  $("#test-valves").disabled = !isOwner() || !state?.connection?.connected || !ready || valves.testing || valves.busy || valveRequestPending;
  $("#stop-valves").disabled = !ready;
}

function setToggle(selector, enabled, onLabel, offLabel) {
  const button = $(selector);
  button.textContent = enabled ? onLabel : offLabel;
  button.classList.toggle("running", enabled);
}

function conveyorDirectionLabel(direction) {
  if (direction === "forward") return "forwards";
  if (direction === "reverse") return "reverse";
  return direction;
}

function updateConveyorState(conveyor) {
  const moving = conveyor.driver_enabled || conveyor.current_speed_sps > 0;
  const stateElement = $("#conveyor-state");
  const directionLabel = conveyorDirectionLabel(conveyor.direction);
  let label = "OFF";
  let summary = "Stopped · driver disabled";
  if (!conveyor.enabled && moving) {
    label = "STOPPING";
    summary = `Decelerating to stop · ${directionLabel}`;
  } else if (conveyor.enabled && conveyor.current_speed_sps < conveyor.speed_sps) {
    label = "STARTING";
    summary = `Accelerating ${directionLabel}`;
  } else if (conveyor.enabled && conveyor.current_speed_sps > conveyor.speed_sps) {
    label = "CHANGING";
    summary = `Decelerating to ${conveyor.speed_sps.toLocaleString()} steps/s`;
  } else if (conveyor.enabled) {
    label = "ON";
    summary = `Running ${directionLabel}`;
  }
  stateElement.textContent = label;
  stateElement.classList.toggle("on", conveyor.enabled || moving);
  $("#conveyor-current-speed").textContent = `${Number(conveyor.current_speed_sps).toLocaleString()} steps/s`;
  $("#conveyor-ramp-summary").textContent = summary;
  setToggle("#toggle-conveyor", conveyor.enabled, "Stop conveyor", "Start conveyor");
  validateConveyorForm();
}

function currentTiming() {
  const frequency = number("#trigger-frequency");
  const low = number("#trigger-low");
  const lead = number("#strobe-lead");
  const lag = number("#strobe-lag");
  const period = Number.isFinite(frequency) && frequency > 0 ? Math.round(1_000_000 / frequency) : 0;
  return { frequency, low, lead, lag, period, occupied: lead + low + lag };
}

function validateTimingForm() {
  const timing = currentTiming();
  let message = "";
  if (![timing.frequency, timing.low, timing.lead, timing.lag].every(Number.isFinite)) {
    message = "All timing values must be numbers.";
  } else if (timing.frequency <= 0 || timing.low <= 0 || timing.lead < 0 || timing.lag < 0) {
    message = "Frequency and LOW duration must be positive; lead and lag cannot be negative.";
  } else if (timing.lead === 0 && timing.lag === 0) {
    message = "LED lead and lag cannot both be zero.";
  } else if (timing.occupied >= timing.period) {
    message = `Occupied timing (${timing.occupied} µs) must be shorter than the ${timing.period} µs period.`;
  }
  $("#timing-validation").textContent = message;
  return message ? null : timing;
}

function updateWaveform() {
  const timing = currentTiming();
  if (!timing.period || ![timing.low, timing.lead, timing.lag].every(Number.isFinite)) return;
  const span = Math.min(timing.period, Math.max(timing.occupied * 1.25, 100));
  const x0 = 90;
  const width = 880;
  const x = (microseconds) => x0 + Math.min(microseconds / span, 1) * width;
  const leadX = x(timing.lead);
  const cameraEndX = x(timing.lead + timing.low);
  const ledEndX = x(timing.occupied);

  $("#led-wave").setAttribute("d", `M ${x0} 66 L ${x0} 35 L ${ledEndX} 35 L ${ledEndX} 66 L 970 66`);
  $("#camera-wave").setAttribute("d", `M ${x0} 105 L ${leadX} 105 L ${leadX} 136 L ${cameraEndX} 136 L ${cameraEndX} 105 L 970 105`);

  const markers = [
    [0, "0"],
    [timing.lead, `${timing.lead} µs`],
    [timing.lead + timing.low, `${timing.lead + timing.low} µs`],
    [timing.occupied, `${timing.occupied} µs`],
  ].filter((entry, index, all) => all.findIndex((candidate) => candidate[0] === entry[0]) === index);
  const group = $("#wave-markers");
  group.replaceChildren();
  markers.forEach(([value, label], index) => {
    const markerX = x(value);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("class", "marker-line");
    line.setAttribute("x1", markerX);
    line.setAttribute("x2", markerX);
    line.setAttribute("y1", "19");
    line.setAttribute("y2", "151");
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("class", "marker-text");
    text.setAttribute("x", markerX + (index === 0 ? 3 : 0));
    text.setAttribute("y", "165");
    text.setAttribute("text-anchor", index === 0 ? "start" : "middle");
    text.textContent = label;
    group.append(line, text);
  });

  $("#period-value").textContent = `${timing.period.toLocaleString()} µs`;
  $("#actual-frequency").textContent = `≈ ${(1_000_000 / timing.period).toFixed(5)} Hz`;
  $("#wave-window-label").textContent = span < timing.period
    ? `Detail: first ${Math.round(span).toLocaleString()} µs of ${timing.period.toLocaleString()} µs period`
    : `Full ${timing.period.toLocaleString()} µs period`;
  validateTimingForm();
}

function updateHighSpeedPreview() {
  const mode = $("#high-speed-mode").value;
  const multiplier = number("#high-speed-multiplier");
  const fixed = number("#high-speed-frequency");
  const pulse = number("#high-speed-pulse");
  const illumination = $("#high-speed-illumination").checked;
  const paired = number("#trigger-frequency");
  const period = Math.round(1e6 / paired);
  const slot = mode === "multiple" ? Math.floor(period / multiplier) : Math.round(1e6 / fixed);
  const rate = mode === "multiple" ? 1e6 * multiplier / period : 1e6 / slot;
  let error = "";
  if (mode === "multiple" && (!Number.isInteger(multiplier) || multiplier < 1 || multiplier > 16)) error = "Multiplier must be a whole number from 1 to 16.";
  else if (mode === "fixed" && (!Number.isFinite(fixed) || fixed < 1 || fixed >= 200)) error = "Fixed frequency must be at least 1 Hz and below 200 Hz.";
  else if (!Number.isFinite(rate) || slot <= 5000) error = "High-speed triggers must remain strictly below 200 Hz after timer rounding.";
  else if (!Number.isInteger(pulse) || pulse < 1 || pulse > 1000000) error = "Enter a positive whole-number pulse width.";
  else if (mode === "fixed" && illumination) error = "Faster shared illumination requires multiple mode.";
  else if (number("#strobe-lead") + Math.max(number("#trigger-low"), pulse) + number("#strobe-lag") >= slot) error = "Trigger and illumination pulse windows must fit between high-speed frames.";
  $("#high-speed-multiplier").disabled = mode !== "multiple";
  $("#high-speed-frequency").disabled = mode !== "fixed";
  $("#high-speed-preview").textContent = Number.isFinite(rate) ? `Settings preview: ${rate.toFixed(3)} Hz${illumination ? " · illumination follows while enabled" : " · illumination remains at paired rate"}` : "Enter valid timing.";
  $("#high-speed-validation").textContent = error;
  return error ? null : { mode, multiplier, fixed_frequency_hz: fixed, pulse_us: pulse, illumination };
}

function validateVibrationForm() {
  const frequency = number("#vibration-frequency");
  const duty = number("#vibration-duty");
  const direction = $("#vibration-direction").value;
  let message = "";
  if (!Number.isFinite(frequency) || frequency < 1 || frequency > 19531 ||
      !Number.isInteger(frequency)) {
    message = "Frequency must be a whole number from 1 to 19,531 Hz.";
  } else if (!Number.isFinite(duty) || duty < 0 || duty > 100 ||
      Math.abs(duty * 10 - Math.round(duty * 10)) > 0.000001) {
    message = "Duty must be from 0.0% to 100.0% in 0.1% increments.";
  } else if (!["clockwise", "anticlockwise"].includes(direction)) {
    message = "Select a valid motor direction.";
  }
  $("#vibration-validation").textContent = message;
  return message ? null : { frequency_hz: frequency, duty_percent: duty, direction };
}

function updateVibrationPreview() {
  const frequency = number("#vibration-frequency");
  const duty = number("#vibration-duty");
  const direction = $("#vibration-direction").value;
  $("#vibration-duty-value").textContent = Number.isFinite(duty) ? `${duty.toFixed(1)}%` : "—";
  $("#vibration-path").textContent = direction === "anticlockwise"
    ? "GPIO4 PWM · GPIO5 LOW"
    : "GPIO4 LOW · GPIO5 PWM";
  const settings = validateVibrationForm();
  if (!settings) {
    $("#vibration-summary").textContent = "Enter valid motor settings";
    return null;
  }
  const periodUs = 1_000_000 / frequency;
  const highUs = periodUs * duty / 100;
  if (duty === 0) {
    $("#vibration-summary").textContent = `${formatDuration(periodUs)} period · both inputs LOW`;
  } else {
    $("#vibration-summary").textContent = `${formatDuration(periodUs)} period · ${formatDuration(highUs)} HIGH`;
  }
  return settings;
}

async function applyVibrationSettings() {
  const settings = updateVibrationPreview();
  if (!settings) throw new Error("Invalid vibration motor settings");
  await post("/api/vibration/config", withClient(settings));
}

function formatDuration(microseconds) {
  if (microseconds >= 1000) return `${(microseconds / 1000).toFixed(3)} ms`;
  if (microseconds >= 10) return `${microseconds.toFixed(1)} µs`;
  return `${microseconds.toFixed(3)} µs`;
}

function validateConveyorForm() {
  const speed = number("#conveyor-speed");
  const acceleration = number("#conveyor-acceleration");
  const direction = $("#conveyor-direction").value;
  const minSpeed = state?.conveyor?.min_speed_sps ?? 10;
  const maxSpeed = state?.conveyor?.max_speed_sps ?? 5000;
  let message = "";
  if (!Number.isInteger(speed) || speed < minSpeed || speed > maxSpeed) {
    message = `Speed must be a whole number from ${minSpeed.toLocaleString()} to ${maxSpeed.toLocaleString()} steps/s.`;
  } else if (!Number.isInteger(acceleration) || acceleration < 1 || acceleration > 100000) {
    message = "Acceleration must be a whole number from 1 to 100,000 steps/s².";
  } else if (!["forward", "reverse"].includes(direction)) {
    message = "Select a valid conveyor direction.";
  }
  $("#conveyor-validation").textContent = message;
  return message ? null : { speed_sps: speed, acceleration_sps2: acceleration, direction };
}

async function applyConveyorSettings() {
  const settings = validateConveyorForm();
  if (!settings) throw new Error("Invalid conveyor settings");
  await post("/api/conveyor/config", withClient(settings));
}

function renderLogs(logs) {
  const signature = logs.map((entry) => `${entry.time}${entry.level}${entry.message}`).join("|");
  if (signature === lastLogSignature) return;
  lastLogSignature = signature;
  const container = $("#log-lines");
  container.replaceChildren();
  if (!logs.length) {
    const empty = document.createElement("p");
    empty.className = "empty-log";
    empty.textContent = "Waiting for controller events…";
    container.appendChild(empty);
    return;
  }
  logs.slice(-80).forEach((entry) => {
    const row = document.createElement("div");
    row.className = `log-line ${entry.level}`;
    const time = document.createElement("span");
    time.className = "time";
    const parsed = new Date(entry.time);
    time.textContent = Number.isNaN(parsed.valueOf()) ? entry.time : parsed.toLocaleTimeString([], { hour12: false });
    const level = document.createElement("span");
    level.className = "level";
    level.textContent = entry.level;
    const message = document.createElement("span");
    message.className = "message";
    message.textContent = entry.message;
    row.append(time, level, message);
    container.appendChild(row);
  });
  container.scrollTop = container.scrollHeight;
}

function markDisconnected(message) {
  if (state) updateState({ ...state,
    connection: { ...state.connection, connected: false, error: message },
    lease: { ...state.lease, active: false }, valves: { ...state.valves, connected: false } });
  $("#connection-label").textContent = "MACHINE UNREACHABLE";
  $("#connection-chip").classList.remove("connected");
  $("#control-message").textContent = message;
  $("#take-control").disabled = true;
  $$(".requires-control").forEach(element => { element.disabled = true; });
  // A stop may still be attempted when status cannot be confirmed.
  $("#stop-all").disabled = false;
}

async function pollState() {
  if (pageClosed) return;
  try { updateState(await api("/api/state", {}, true)); }
  catch (error) { markDisconnected(error.message); }
  finally { if (!pageClosed) setTimeout(pollState, 750); }
}

function bindActions() {
  preserveEditedSettings();
  ["#high-speed-mode", "#high-speed-multiplier", "#high-speed-frequency", "#high-speed-pulse", "#high-speed-illumination", "#trigger-frequency", "#trigger-low", "#strobe-lead", "#strobe-lag"].forEach((id) => {
    $(id).addEventListener("input", updateHighSpeedPreview);
    $(id).addEventListener("change", updateHighSpeedPreview);
  });
  $("#apply-high-speed").addEventListener("click", () => action(async () => {
    const settings = updateHighSpeedPreview();
    if (!settings) throw new Error("Invalid high-speed settings");
    await post("/api/high-speed/config", withClient(settings));
  }, "High-speed timing applied."));
  $("#toggle-high-speed").addEventListener("click", () => action(
    () => post("/api/high-speed/enabled", withClient({ enabled: !state.high_speed.enabled }))
  ));
  $("#load-candidate-settings").addEventListener("click", () => action(loadCandidateSettings));
  ["#trigger-frequency", "#trigger-low", "#strobe-lead", "#strobe-lag"].forEach((id) => {
    $(id).addEventListener("input", updateWaveform);
  });

  $("#take-control").addEventListener("click", () => action(async () => {
    const next = await post("/api/control/acquire", withClient({ operator_name: $("#operator-name").value.trim() || "Operator" }));
    updateState(next);
  }, "Control acquired. The five-second ESP32 failsafe is active."));

  $("#release-control").addEventListener("click", () => action(async () => {
    await post("/api/control/release", withClient());
  }, "Control released; all outputs stopped."));

  $("#stop-all").addEventListener("click", () => action(async () => {
    await api("/api/stop", { method: "POST" });
  }, "Stop confirmed by both ESP32 controllers."));

  $("#test-valves").addEventListener("click", () => action(async () => {
    valveRequestPending = true;
    updateValveState();
    try {
      const result = await post("/api/valves/test", withClient());
      state.valves = result.valves;
    } finally {
      valveRequestPending = false;
      updateValveState();
    }
  }, "Valve board acknowledged the test: 50 ms pulses, 500 ms apart."));

  $("#stop-valves").addEventListener("click", () => action(
    () => api("/api/valves/stop", { method: "POST" }), "Valve outputs stopped."
  ));

  $("#apply-trigger").addEventListener("click", () => action(async () => {
    const timing = validateTimingForm();
    if (!timing) throw new Error("Invalid timing");
    await post("/api/triggers/config", withClient({ frequency_hz: timing.frequency, low_us: timing.low }));
  }, "Camera timing applied."));

  $("#toggle-trigger").addEventListener("click", () => action(
    () => post("/api/triggers/enabled", withClient({ enabled: !state.triggers.enabled })),
    state?.triggers?.enabled ? "Camera triggers stopped." : "Camera triggers started."
  ));

  $("#pulse-trigger").addEventListener("click", () => action(
    () => post("/api/triggers/pulse", withClient({ count: number("#trigger-count") })),
    "Finite camera pulse train started."
  ));

  $("#apply-strobe").addEventListener("click", () => action(async () => {
    const timing = validateTimingForm();
    if (!timing) throw new Error("Invalid timing");
    await post("/api/strobe/config", withClient({ lead_us: timing.lead, lag_us: timing.lag }));
  }, "Illumination timing applied."));

  $("#toggle-strobe").addEventListener("click", () => action(
    () => post("/api/strobe/enabled", withClient({ enabled: !state.strobe.enabled })),
    state?.strobe?.enabled ? "Illumination stopped." : "Illumination started."
  ));

  $("#pulse-strobe").addEventListener("click", () => action(
    () => post("/api/strobe/pulse", withClient()), "Single illuminated cycle started."
  ));

  const vibrationSlider = $("#vibration-duty-slider");
  const vibrationDuty = $("#vibration-duty");
  vibrationSlider.addEventListener("input", () => {
    vibrationDuty.value = vibrationSlider.value;
    updateVibrationPreview();
  });
  vibrationDuty.addEventListener("input", () => {
    if (Number.isFinite(Number(vibrationDuty.value))) vibrationSlider.value = vibrationDuty.value;
    updateVibrationPreview();
  });
  captureFocusedWheel(vibrationDuty);
  $("#vibration-duty-up").addEventListener("click", () => {
    vibrationDuty.focus();
    stepNumberInput(vibrationDuty, 1);
  });
  $("#vibration-duty-down").addEventListener("click", () => {
    vibrationDuty.focus();
    stepNumberInput(vibrationDuty, -1);
  });
  ["#vibration-frequency", "#vibration-direction"].forEach((id) => {
    $(id).addEventListener("input", updateVibrationPreview);
    $(id).addEventListener("change", updateVibrationPreview);
  });
  $("#apply-vibration").addEventListener("click", () => action(
    applyVibrationSettings, "Vibration motor settings applied."
  ));
  $("#toggle-vibration").addEventListener("click", () => action(async () => {
    const starting = !state.vibration_motor.enabled;
    if (starting) await applyVibrationSettings();
    await post("/api/vibration/enabled", withClient({ enabled: starting }));
  }, state?.vibration_motor?.enabled ? "Vibration motor stopped." : "Vibration motor started."));

  ["#conveyor-speed", "#conveyor-acceleration", "#conveyor-direction"].forEach((id) => {
    $(id).addEventListener("input", validateConveyorForm);
    $(id).addEventListener("change", validateConveyorForm);
  });
  captureFocusedWheel($("#conveyor-speed"));
  $("#apply-conveyor").addEventListener("click", () => action(
    applyConveyorSettings, "Conveyor settings applied."
  ));
  $("#toggle-conveyor").addEventListener("click", () => action(async () => {
    const starting = !state.conveyor.enabled;
    if (starting) await applyConveyorSettings();
    await post("/api/conveyor/enabled", withClient({ enabled: starting }));
  }, state?.conveyor?.enabled ? "Conveyor is decelerating to a stop." : "Conveyor started."));

}

setInterval(async () => {
  if (!isOwner() || heartbeatPending || pageClosed) return;
  heartbeatPending = true;
  try { await api("/api/control/heartbeat", { method: "POST", body: JSON.stringify(withClient()) }, true); }
  catch (error) { markDisconnected(error.message); }
  finally { heartbeatPending = false; }
}, 1000);

window.addEventListener("pagehide", () => {
  pageClosed = true;
  if (isOwner()) api("/api/control/release", {
    method: "POST", body: JSON.stringify(withClient()), keepalive: true,
  }, true).catch(() => {});
});

window.addEventListener("pageshow", event => {
  if (!event.persisted) return;
  pageClosed = false;
  markDisconnected("Reconnecting to the machine…");
  pollState();
});

new ResizeObserver(() => {
  if (window.parent !== window) window.parent.postMessage({type:"sorter-panel-height", height:Math.ceil(document.body.getBoundingClientRect().height)}, location.origin);
}).observe(document.body);

bindActions();
pollState();
updateWaveform();
updateVibrationPreview();
