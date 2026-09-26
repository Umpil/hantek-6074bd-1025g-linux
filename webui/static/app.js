"use strict";

const META = JSON.parse(document.getElementById("meta").textContent);
const COLORS = ["#f2c94c", "#38c7e8", "#e36ad6", "#6f8cff"];
const SHAPE_RU = { sine: "синус", square: "меандр", triangle: "треугольник", sawtooth: "пила" };
const STORE_KEY = "hantek-web-settings-v1";
const CHANNEL_DEFAULTS = [  // vdiv, lever: OUTPUT on CH1 (±4 V visible), SYNC on CH2 (≈0..6 V visible)
  [1, 128], [2, 96], [1, META.default_levers[2]], [1, META.default_levers[3]],
];
const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ formatting
function si(value, unit, digits = 4) {
  if (value === null || value === undefined || !isFinite(value)) return "—";
  const a = Math.abs(value);
  for (const [f, p] of [[1e6, "M"], [1e3, "k"], [1, ""], [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"]]) {
    if (a >= f * 0.9999) return `${+(value / f).toPrecision(digits)} ${p}${unit}`;
  }
  return value === 0 ? `0 ${unit}` : `${value.toPrecision(digits)} ${unit}`;
}
const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (id) => ($(id).value === "" ? null : Number($(id).value));

// ------------------------------------------------------------------ controls
function buildControls() {
  for (const s of META.shapes) $("g-shape").add(new Option(SHAPE_RU[s] || s, s));
  for (const n of [2, 3, 4, 5]) $("g-burst-cycles").add(new Option(n, n));
  for (const tb of META.timebases) $("s-timebase").add(new Option(`${tb.index}: ${tb.label}`, tb.index));
  $("s-timebase").value = "12";
  $("s-window").min = META.window_s[0];
  $("s-window").max = META.window_s[1];
  const body = $("channels").tBodies[0];
  for (let ch = 1; ch <= 4; ch++) {
    const tr = body.insertRow();
    tr.innerHTML = `
      <td><span class="sw" style="background:${COLORS[ch - 1]}"></span>CH${ch}</td>
      <td><input type="checkbox" id="c${ch}-show" checked></td>
      <td><select id="c${ch}-vdiv"></select></td>
      <td><select id="c${ch}-coupling"><option value="dc">DC</option><option value="ac">AC</option><option value="gnd">GND</option></select></td>
      <td><input type="number" id="c${ch}-lever" min="0" max="255" step="1" value="${CHANNEL_DEFAULTS[ch - 1][1]}"></td>
      <td><input type="checkbox" id="c${ch}-bw" title="ограничение полосы"></td>`;
    for (const v of META.volts_per_div) $(`c${ch}-vdiv`).add(new Option(si(v, "V", 3), v));
    $(`c${ch}-vdiv`).value = String(CHANNEL_DEFAULTS[ch - 1][0]);
  }
}

function settingElements() {
  return document.querySelectorAll("#gen input, #gen select, #scope input, #scope select, #live-interval");
}
function saveSettings() {
  try {
    const s = {};
    for (const el of settingElements()) s[el.id] = el.type === "checkbox" ? el.checked : el.value;
    localStorage.setItem(STORE_KEY, JSON.stringify(s));
  } catch (e) { /* storage unavailable: settings just are not remembered */ }
}
function loadSettings() {
  try {
    const s = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    for (const el of settingElements()) {
      if (!(el.id in s)) continue;
      if (el.type === "checkbox") el.checked = s[el.id]; else el.value = s[el.id];
    }
  } catch (e) { /* ignore */ }
}

function generatorPayload() {
  return {
    frequency_hz: num("g-frequency"),
    amplitude_v: num("g-amplitude"),
    offset_v: num("g-offset"),
    shape: $("g-shape").value,
    duty_cycle: num("g-duty") === null ? null : num("g-duty") / 100,
    burst_cycles: $("g-burst").checked ? Number($("g-burst-cycles").value) : null,
    burst_interval_ms: num("g-burst-interval"),
    single: $("g-single").checked,
    external_trigger: $("g-ext").checked,
    falling: $("g-falling").checked,
  };
}
function scopePayload() {
  return {
    time_div_index: Number($("s-timebase").value),
    trigger_source: Number($("t-source").value),
    trigger_level_v: num("t-level"),
    trigger_slope: $("t-slope").value,
    trigger_sweep: $("t-sweep").value,
    channels: [1, 2, 3, 4].map((ch) => ({
      volts_per_div: Number($(`c${ch}-vdiv`).value),
      coupling: $(`c${ch}-coupling`).value,
      lever: num(`c${ch}-lever`),
      bandwidth_limit: $(`c${ch}-bw`).checked,
    })),
  };
}

// ------------------------------------------------------------------ server
async function api(path, body) {
  const init = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
function showError(err) {
  const box = $("error");
  if (!err) { box.hidden = true; return; }
  box.hidden = false;
  box.textContent = `${new Date().toLocaleTimeString()}  ${err.message || err}`;
}
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const state = (open) => (open ? '<span class="good">открыт</span>' : '<span class="muted">закрыт</span>');
    const left = Math.max(0, s.idle_release_s - s.idle_s);
    $("usb-status").innerHTML = `USB: генератор ${state(s.generator_open)}, осциллограф ${state(s.scope_open)}` +
      (s.generator_open || s.scope_open ? ` <span class="small muted">(авто-освобождение через ${Math.round(left)} с простоя)</span>` : "");
  } catch (e) {
    $("usb-status").innerHTML = '<span class="bad">сервер недоступен</span>';
  }
}

// ------------------------------------------------------------------ generator
let planTimer = null;
function schedulePlan() {
  const amp = num("g-amplitude");
  $("g-vpp").textContent = amp === null ? "" : `= ${si(2 * amp, "V", 3)} п-п`;
  clearTimeout(planTimer);
  planTimer = setTimeout(updatePlan, 250);
}
function renderPlan(p) {
  const lines = [
    `${p.burst ? "Повтор пачек" : "Частота"}: <b>${si(p.actual_hz, "Hz", 7)}</b> ` +
      `<span class="muted">(отклонение ${p.error_pct.toFixed(4)} %)</span>`,
    `DDS: ${p.samples} точек / ${p.periods} пер., делитель ${p.divider}`,
    `SYNC: ${p.sync_phase_locked ? '<span class="good">фаза привязана к выходу</span>'
      : '<span class="warn">фаза «плавает» (нецелая геометрия DDS)</span>'}`,
  ];
  if (p.burst) {
    const dense = p.samples_per_carrier_cycle;
    lines.push(`Пачка ${si(p.burst_duration_s, "s", 3)}, ${dense.toFixed(1)} точек на период несущей` +
      (dense < 10 ? ' <span class="warn">(грубо)</span>' : ""));
  }
  $("g-plan").innerHTML = lines.join("<br>");
}
async function updatePlan() {
  try { renderPlan(await api("/api/generator/plan", generatorPayload())); }
  catch (e) { $("g-plan").innerHTML = `<span class="bad">${escapeHtml(e.message)}</span>`; }
}
function describeGenerator() {
  const g = generatorPayload();
  const what = g.burst_cycles ? `пачка ${g.burst_cycles}× синус, повтор ${g.burst_interval_ms} мс` : SHAPE_RU[g.shape];
  return `${si(g.frequency_hz, "Hz", 6)}, ${what}, ${si(2 * g.amplitude_v, "V", 3)} п-п` +
    (g.offset_v ? `, смещение ${si(g.offset_v, "V", 3)}` : "") + (g.single ? ", одиночный" : "");
}
async function applyGenerator() {
  try {
    const p = await api("/api/generator/apply", generatorPayload());
    renderPlan(p);
    $("g-applied").textContent = `Применено в ${p.applied_at}: ${describeGenerator()}`;
    showError(null);
  } catch (e) { showError(e); }
  refreshStatus();
}
async function zeroGenerator() {
  try {
    const r = await api("/api/generator/zero", {});
    $("g-applied").textContent = `В ноль в ${r.applied_at}: OUTPUT = 0 В DC, SYNC продолжает работать`;
    showError(null);
  } catch (e) { showError(e); }
  refreshStatus();
}

// ------------------------------------------------------------------ trigger / time
function triggerRange() {
  const ch = Number($("t-source").value);
  const step = 8 * Number($(`c${ch}-vdiv`).value) / 255;
  const lever = num(`c${ch}-lever`) ?? 128;
  return [(0 - lever) * step, (255 - lever) * step];
}
function updateTriggerHint() {
  const [lo, hi] = triggerRange();
  $("t-range").textContent = `допустимо ${si(lo, "V", 3)} … ${si(hi, "V", 3)}`;
  $("t-level").min = lo.toFixed(6);
  $("t-level").max = hi.toFixed(6);
}
async function autoLevel() {
  try {
    const r = await api("/api/autolevel", { scope: scopePayload() });
    $("t-level").value = r.trigger_level_v.toFixed(3);
    $("t-auto").textContent = `AUTO: ${si(r.min_v, "V", 3)} … ${si(r.max_v, "V", 3)} → ${si(r.trigger_level_v, "V", 3)}`;
    saveSettings();
    showError(r.warning ? new Error(r.warning) : null);
  } catch (e) { showError(e); }
  refreshStatus();
}
function fitWindow() {
  const w = num("s-window");
  const [lo, hi] = META.window_s;
  if (w === null || !(w >= lo && w <= hi)) {
    showError(new Error(`окно должно быть от ${si(lo, "s")} до ${si(hi, "s")}`));
    return;
  }
  const tb = META.timebases.find((t) => t.frame_s >= w * (1 - 1e-9));
  $("s-timebase").value = String(tb.index);
  userZoom = null;
  saveSettings();
  showError(null);
}

// ------------------------------------------------------------------ chart
let chart = null;
let lastPayload = null;
let userZoom = null;      // x range the user selected with the mouse, in display units
let lastTimebase = null;

function timeUnit(frame) {
  if (frame >= 0.5) return [1, "s"];
  if (frame >= 5e-4) return [1e3, "ms"];
  return [1e6, "µs"];
}
function defaultRange(p, scale) {
  const w = num("s-window");
  return w !== null && w > 0 && w < p.frame_s ? [-w / 2 * scale, w / 2 * scale] : null;
}
function drawTriggerMarks(u) {
  if (!lastPayload) return;
  const { ctx, bbox } = u;
  ctx.save();
  ctx.setLineDash([6, 4]);
  ctx.lineWidth = 1;
  const x = u.valToPos(0, "x", true);
  if (x >= bbox.left && x <= bbox.left + bbox.width) {
    ctx.strokeStyle = "#9aa3b2";
    ctx.beginPath(); ctx.moveTo(x, bbox.top); ctx.lineTo(x, bbox.top + bbox.height); ctx.stroke();
  }
  const y = u.valToPos(lastPayload.trigger_level_v, "y", true);
  if (y >= bbox.top && y <= bbox.top + bbox.height) {
    ctx.strokeStyle = COLORS[lastPayload.trigger_source - 1];
    ctx.beginPath(); ctx.moveTo(bbox.left, y); ctx.lineTo(bbox.left + bbox.width, y); ctx.stroke();
  }
  ctx.restore();
}
function chartSize() {
  return { width: $("chart").clientWidth - 2, height: Math.max(300, Math.round(window.innerHeight * 0.5)) };
}
function createChart(unit, data) {
  if (chart) chart.destroy();
  const axis = { stroke: "#aab1bd", grid: { stroke: "#2b2f37" }, ticks: { stroke: "#2b2f37" } };
  chart = new uPlot({
    ...chartSize(),
    scales: { x: { time: false } },
    axes: [{ ...axis, label: `время от триггера, ${unit}` }, { ...axis, label: "В" }],
    series: [
      { label: `t, ${unit}`, value: (u, v) => (v == null ? "—" : v.toPrecision(5)) },
      ...[1, 2, 3, 4].map((ch) => ({
        label: `CH${ch}`, stroke: COLORS[ch - 1], width: 1,
        value: (u, v) => (v == null ? "—" : `${v.toFixed(3)} V`),
      })),
    ],
    cursor: { drag: { x: true, y: false } },
    hooks: {
      draw: [drawTriggerMarks],
      setSelect: [(u) => {
        if (u.select.width > 2) {
          userZoom = [u.posToVal(u.select.left, "x"), u.posToVal(u.select.left + u.select.width, "x")];
        }
      }],
      setSeries: [(u, idx) => {
        if (idx) { $(`c${idx}-show`).checked = u.series[idx].show; saveSettings(); }
      }],
      ready: [(u) => u.over.addEventListener("dblclick", () => {
        userZoom = null;
        setTimeout(() => applyRange(), 0);  // after uPlot's own reset
      })],
    },
  }, data, $("chart"));
  chart._unit = unit;
}
function applyRange() {
  if (!chart || !lastPayload) return;
  const [scale] = timeUnit(lastPayload.frame_s);
  const range = userZoom || defaultRange(lastPayload, scale);
  if (range) chart.setScale("x", { min: range[0], max: range[1] });
}
function applyVisibility() {
  if (!chart) return;
  for (let ch = 1; ch <= 4; ch++) {
    const show = $(`c${ch}-show`).checked;
    if (chart.series[ch].show !== show) chart.setSeries(ch, { show });
  }
}
function render(p) {
  lastPayload = p;
  const [scale, unit] = timeUnit(p.frame_s);
  const n = p.channels[0].codes.length;
  const t = new Float64Array(n);
  for (let i = 0; i < n; i++) t[i] = (i - p.trigger_index) / p.sample_rate_hz * scale;
  const data = [t, ...p.channels.map((c) => {
    const v = new Float64Array(n);
    for (let i = 0; i < n; i++) v[i] = (c.codes[i] - c.lever) * c.volts_per_code;
    return v;
  })];
  if (p.time_div_index !== lastTimebase) { userZoom = null; lastTimebase = p.time_div_index; }
  if (!chart || chart._unit !== unit) createChart(unit, data); else chart.setData(data, true);
  applyVisibility();
  applyRange();
  renderTable(p);
  const tb = META.timebases.find((x) => x.index === p.time_div_index);
  $("info").textContent = `${tb.label} · триггер CH${p.trigger_source} ${p.trigger_sweep.toUpperCase()} ` +
    `${si(p.trigger_level_v, "V", 3)} · state ${p.state} · захват ${Math.round(p.capture_ms)} мс · ` +
    new Date().toLocaleTimeString();
}
function renderTable(p) {
  $("measure").tBodies[0].innerHTML = p.channels.map((c) => `<tr>
    <td><span class="sw" style="background:${COLORS[c.channel - 1]}"></span>CH${c.channel}</td>
    <td>${si(c.freq_hz, "Hz", 6)}</td><td>${si(c.vpp, "V")}</td><td>${si(c.vmin, "V")}</td>
    <td>${si(c.vmax, "V")}</td><td>${si(c.mean, "V")}</td><td>${si(c.rms_ac, "V")}</td>
    <td class="${c.clipped ? "bad" : ""}">${c.clipped ? `${c.clipped} сэмпл.` : "0"}</td>
    <td>${si(c.volts_per_div, "V", 3)}/div ${c.coupling.toUpperCase()} · lever ${c.lever}</td></tr>`).join("");
}

// ------------------------------------------------------------------ capture / live
let busy = false;
let live = false;
async function capture() {
  if (busy) return;
  busy = true;
  try {
    render(await api("/api/capture", { scope: scopePayload() }));
    showError(null);
  } finally {
    busy = false;
    refreshStatus();
  }
}
function setLive(on) {
  live = on;
  $("live").textContent = on ? "Live ■ стоп" : "Live ▶";
  $("live").classList.toggle("active", on);
  if (on) liveLoop();
}
async function liveLoop() {
  let errors = 0;
  while (live) {
    const started = performance.now();
    try { await capture(); errors = 0; } catch (e) {
      showError(e);
      if (++errors >= 3) { setLive(false); showError(new Error(`Live остановлен после 3 ошибок подряд: ${e.message}`)); }
    }
    const interval = Math.min(5, Math.max(0.2, num("live-interval") ?? 0.5)) * 1000;
    await new Promise((r) => setTimeout(r, Math.max(0, interval - (performance.now() - started))));
  }
}

// ------------------------------------------------------------------ wiring
function init() {
  buildControls();
  loadSettings();
  updateTriggerHint();
  schedulePlan();
  refreshStatus();
  setInterval(refreshStatus, 3000);

  for (const el of document.querySelectorAll("#gen input, #gen select")) {
    el.addEventListener("input", () => { schedulePlan(); saveSettings(); });
  }
  for (const el of document.querySelectorAll("#scope input, #scope select, #live-interval")) {
    el.addEventListener("input", () => { updateTriggerHint(); saveSettings(); });
  }
  for (let ch = 1; ch <= 4; ch++) $(`c${ch}-show`).addEventListener("change", applyVisibility);
  $("s-window").addEventListener("change", () => { userZoom = null; applyRange(); });
  $("s-fit").addEventListener("click", fitWindow);
  $("g-apply").addEventListener("click", applyGenerator);
  $("g-zero").addEventListener("click", zeroGenerator);
  $("t-autolevel").addEventListener("click", autoLevel);
  $("capture").addEventListener("click", () => capture().catch(showError));
  $("live").addEventListener("click", () => setLive(!live));
  $("csv").addEventListener("click", () => { window.location = "/api/csv"; });
  $("release").addEventListener("click", async () => {
    setLive(false);
    try { await api("/api/release", {}); showError(null); } catch (e) { showError(e); }
    refreshStatus();
  });
  $("reset-settings").addEventListener("click", () => {
    try { localStorage.removeItem(STORE_KEY); } catch (e) { /* ignore */ }
    location.reload();
  });
  window.addEventListener("resize", () => chart && chart.setSize(chartSize()));
}
init();
