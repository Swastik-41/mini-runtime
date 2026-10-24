"use strict";

// --------------------------------------------------------------------
// State
// --------------------------------------------------------------------

const WINDOW_SECONDS = 60;
const STALE_AFTER = 5;                 // seconds without a fresh sample
const TOKEN = document.querySelector('meta[name="csrf-token"]').content;

const state = {
  serverId: null,
  lastEvent: 0,
  seenEvents: new Set(),
  containers: [],
  images: [],
  selected: null,
  selToken: 0,          // bumped on every selection; late responses are dropped
  history: {},          // name -> [{t, mem, cpu, pids}]
  lastStats: {},        // name -> previous raw sample, for CPU %
  sampleAt: {},         // name -> browser time of the last good sample
  connected: null,
  updated: null,
};

const $ = (id) => document.getElementById(id);

// Builds DOM nodes; text is always set with textContent.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

class HttpError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new HttpError(data.error || res.statusText || "Request failed", res.status);
  return data;
}

const post = (path, body, raw = false) => api(path, {
  method: "POST",
  headers: raw ? { "X-Dashboard-Token": TOKEN }
               : { "Content-Type": "application/json", "X-Dashboard-Token": TOKEN },
  body: raw ? body : JSON.stringify(body || {}),
});

const enc = encodeURIComponent;

function formatBytes(bytes) {
  if (bytes == null) return "Unavailable";
  if (bytes === "max") return "no limit";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return `${bytes >= 100 || i === 0 ? Math.round(bytes) : bytes.toFixed(1)} ${units[i]}`;
}

const show = (v, fmt = String) => (v == null ? "Unavailable" : v === "max" ? "no limit" : fmt(v));

// --------------------------------------------------------------------
// Toasts and connection indicator
// --------------------------------------------------------------------

function toast(message, kind = "info") {
  const t = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status", text: message });
  $("toasts").append(t);
  setTimeout(() => t.classList.add("leaving"), 4500);
  setTimeout(() => t.remove(), 5000);
}

function setConnected(ok) {
  if (state.connected === ok) return;
  state.connected = ok;
  const c = $("conn");
  c.textContent = ok ? "Connected" : "Reconnecting…";
  c.className = `conn ${ok ? "ok" : "bad"}`;
}

// Runs an async action with its button showing a busy state.
async function busy(button, label, action) {
  const old = button.textContent;
  button.disabled = true;
  button.textContent = label;
  button.setAttribute("aria-busy", "true");
  try {
    return await action();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    button.textContent = old;
    button.removeAttribute("aria-busy");
    button.disabled = false;
    render();
  }
}

// --------------------------------------------------------------------
// Overview loop: containers, images, events
// --------------------------------------------------------------------

let overviewTimer = null, failures = 0;

async function refreshOverview() {
  clearTimeout(overviewTimer);
  let data;
  try {
    data = await api(`/api/overview?since=${state.lastEvent}`);
  } catch {
    failures++;
    setConnected(false);
    clearTimeout(overviewTimer);
    overviewTimer = setTimeout(refreshOverview, Math.min(5000, 1000 * failures));
    return;
  }
  failures = 0;
  setConnected(true);

  if (state.serverId && data.server_id !== state.serverId) {
    // A new server process: old cursors, terminals and the token are void.
    resetEvents();
    $("restart-banner").hidden = false;
    state.serverId = data.server_id;
    clearTimeout(overviewTimer);
    overviewTimer = setTimeout(refreshOverview, 0);
    return;
  }
  state.serverId = data.server_id;
  if (data.reset) resetEvents();

  $("runtime-dir").textContent = data.runtime_dir;
  state.containers = data.containers;
  state.updated = new Date();
  renderImages(data.images);
  addEvents(data.events);
  state.lastEvent = Math.max(state.lastEvent, data.last_event_id);

  if (state.selected && !findContainer(state.selected)) select(null);
  render();
  clearTimeout(overviewTimer);   // a manual refresh may have overlapped this one
  overviewTimer = setTimeout(refreshOverview, 1500);
}

const findContainer = (name) => state.containers.find((c) => c.name === name);

function renderImages(images) {
  if (images.join("\n") === state.images.join("\n")) return;
  state.images = images;
  const sel = $("image-select");
  const current = sel.value;
  sel.replaceChildren(...images.map((i) => el("option", { value: i, text: i })));
  if (!images.length) sel.append(el("option", { value: "", text: "No images: run make setup" }));
  if (images.includes(current)) sel.value = current;
  else if (images.includes("base")) sel.value = "base";
}

function resetEvents() {
  state.lastEvent = 0;
  state.seenEvents.clear();
  $("event-rows").replaceChildren();
}

const eventIcon = { start: "▶", exit: "■", oom: "⚠", stop: "■", delete: "✕", error: "⚠" };
function addEvents(events) {
  const rows = $("event-rows");
  for (const ev of events) {
    if (state.seenEvents.has(ev.id)) continue;
    state.seenEvents.add(ev.id);
    const time = new Date(ev.time * 1000).toLocaleTimeString();
    rows.prepend(el("tr", { class: `k-${ev.kind}` },
      el("td", { text: time }),
      el("td", { text: ev.container }),
      el("td", { text: `${eventIcon[ev.kind] || ""} ${ev.text}` })));
  }
  while (rows.children.length > 200) rows.lastChild.remove();
}

// --------------------------------------------------------------------
// Container table
// --------------------------------------------------------------------

const STATE_LABEL = { RUNNING: "Running", STARTING: "Starting", STOPPED: "Stopped", UNKNOWN: "Unknown" };
const TERMINAL_LABEL = {
  ATTACHED: "Terminal here", ENDED: "Output kept", ELSEWHERE: "Terminal elsewhere", NONE: "No terminal",
};
const isRunning = (c) => c.state === "RUNNING" || c.state === "STARTING";

function statusOf(c) {
  if (isRunning(c)) return { text: STATE_LABEL[c.state], cls: "running" };
  if (c.state === "UNKNOWN") return { text: "Unknown", cls: "warn" };
  if (c.exit_code != null) return { text: `Exited ${c.exit_code}`, cls: c.exit_code >= 128 ? "killed" : "" };
  return { text: "Stopped", cls: "" };
}

function memoryBytes(v) {
  if (typeof v === "number") return v;
  const m = /^(\d+)([KMG])$/i.exec(v || "");
  return m ? Number(m[1]) * { K: 1024, M: 1024 ** 2, G: 1024 ** 3 }[m[2].toUpperCase()] : null;
}

function limitText(c, key) {
  const l = c.limits;
  if (!l || l[key] == null) return "Unknown";
  if (l[key] === "max") return "no limit";
  if (key === "memory") return formatBytes(memoryBytes(l.memory));
  if (key === "cpu") return `${l.cpu}%`;
  return String(l[key]);
}

function visibleContainers() {
  const q = $("search").value.trim().toLowerCase();
  const f = $("filter").value;
  const sort = $("sort").value;
  const list = state.containers.filter((c) => {
    if (q && !c.name.toLowerCase().includes(q) && !(c.image || "").toLowerCase().includes(q)) return false;
    if (f === "ATTACHED") return c.terminal === "ATTACHED";
    if (f === "RUNNING") return isRunning(c);
    if (f !== "all") return c.state === f;
    return true;
  });
  const num = (v) => (typeof v === "number" ? v : v === "max" ? Infinity : -1);
  const key = {
    name: (c) => c.name,
    state: (c) => (isRunning(c) ? 0 : c.state === "UNKNOWN" ? 1 : 2),
    image: (c) => c.image || "",
    memory: (c) => -num(memoryBytes(c.limits && c.limits.memory) ?? (c.limits && c.limits.memory)),
    cpu: (c) => -num(c.limits && c.limits.cpu),
  }[sort];
  return list.sort((a, b) => {
    const x = key(a), y = key(b);
    return (x < y ? -1 : x > y ? 1 : 0) || a.name.localeCompare(b.name);
  });
}

function renderTable() {
  const rows = $("container-rows");
  const list = visibleContainers();
  const empty = $("containers-empty");
  if (!state.containers.length) {
    empty.textContent = "No containers yet. Run one with the form.";
  } else if (!list.length) {
    empty.textContent = "No containers match the search or filter.";
  }
  empty.hidden = list.length > 0;
  $("updated").textContent = state.updated ? `Updated ${state.updated.toLocaleTimeString()}` : "";

  rows.replaceChildren(...list.map((c) => {
    const s = statusOf(c);
    const action = isRunning(c)
      ? el("button", { class: "small", text: "Stop", "aria-label": `Stop ${c.name}`,
        onclick: (e) => { e.stopPropagation(); stopContainer(c.name, e.currentTarget); } })
      : el("button", { class: "small go", text: "Start", "aria-label": `Start ${c.name}`,
        disabled: c.state === "UNKNOWN",
        onclick: (e) => { e.stopPropagation(); startAgain(c.name, e.currentTarget); } });
    return el("tr", {
      class: c.name === state.selected ? "selected" : null,
      tabindex: "0", "aria-selected": String(c.name === state.selected),
      onclick: () => select(c.name),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(c.name); } },
    },
    el("td", { class: "cname", text: c.name }),
    el("td", {}, el("span", { class: `pill ${s.cls}`, text: s.text })),
    el("td", { class: "muted", text: TERMINAL_LABEL[c.terminal] || c.terminal }),
    el("td", { text: c.image || "Unknown" }),
    el("td", { text: limitText(c, "memory") }),
    el("td", { text: limitText(c, "cpu") }),
    el("td", { text: limitText(c, "pids") }),
    el("td", { class: "row-action" }, action));
  }));
}

["search", "filter", "sort"].forEach((id) => $(id).addEventListener("input", renderTable));

// --------------------------------------------------------------------
// Run form, Start and Edit and run
// --------------------------------------------------------------------

const presets = {
  oom: { name: "oom-demo", memory: "50M", cpu: 50, pids: 64, command: "/bin/memory_hog" },
  cpu: { name: "cpu-demo", memory: "100M", cpu: 20, pids: 64, command: "sh -c 'while :; do :; done & sleep 30; kill $!'" },
  fork: { name: "fork-demo", memory: "100M", cpu: 50, pids: 10, command: "sh -c 'i=0; while [ $i -lt 20 ]; do sleep 30 & i=$((i+1)); done; wait'" },
  cow: { name: "cow-demo", memory: "100M", cpu: 50, pids: 64, command: "sh -c 'echo CHANGED > /message.txt; echo hello > /newfile.txt; rm \"$(command -v hostname)\"; cat /message.txt; sleep 20'" },
};

function fillForm(values) {
  const form = $("run-form");
  for (const key of ["name", "memory", "cpu", "pids", "command"]) {
    if (values[key] != null) form.elements[key].value = values[key];
  }
  form.elements.command.value = values.command || "";
  if (values.image && state.images.includes(values.image)) form.elements.image.value = values.image;
  form.elements.net.checked = !!values.net;
}

document.querySelectorAll("[data-preset]").forEach((btn) => {
  btn.addEventListener("click", () => fillForm(presets[btn.dataset.preset]));
});

async function launch(body) {
  await post("/api/containers", body);
  state.history[body.name] = [];
  delete state.lastStats[body.name];
  delete state.sampleAt[body.name];
  await refreshOverview();
  select(body.name, true);
}

$("run-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target;
  const body = {
    name: form.elements.name.value.trim(),
    image: form.elements.image.value,
    memory: form.elements.memory.value.trim(),
    cpu: form.elements.cpu.value,
    pids: form.elements.pids.value,
    command: form.elements.command.value.trim(),
    net: form.elements.net.checked,
  };
  $("run-error").textContent = "";
  if (!form.elements.name.checkValidity()) {
    $("run-error").textContent = "Name: use letters, numbers, _ or - (up to 64).";
    form.elements.name.focus();
    return;
  }
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  button.textContent = "Starting…";
  launch(body)
    .then(() => toast(`Started ${body.name}`))
    .catch((err) => { $("run-error").textContent = err.message; })
    .finally(() => { button.disabled = false; button.textContent = "Run"; });
});

// Start a stopped container again with the settings it last ran with here.
function startAgain(name, button) {
  const c = findContainer(name);
  if (!c) return;
  const body = { name, ...(c.options || {}) };
  if (!c.options && c.image) body.image = c.image;
  busy(button, "Starting…", async () => {
    await launch(body);
    toast(c.options ? `Started ${name} with its last settings`
                    : `Started ${name} with default limits (its last settings are not known here)`);
  });
}

function editAndRun(name) {
  const c = findContainer(name);
  if (!c) return;
  fillForm({ name, image: c.image, ...(c.options || {}) });
  $("run-form").elements.memory.focus();
  $("run-form").scrollIntoView({ behavior: "smooth", block: "nearest" });
  toast("Settings copied into the form. Change them and press Run.");
}

function stopContainer(name, button) {
  if (!confirm(`Stop container "${name}"? Every process in it is killed.`)) return;
  busy(button, "Stopping…", async () => {
    await post(`/api/containers/${enc(name)}/stop`);
    toast(`Stopped ${name}`);
    await refreshOverview();
  });
}

$("d-start").addEventListener("click", (e) => startAgain(state.selected, e.currentTarget));
$("d-edit").addEventListener("click", () => editAndRun(state.selected));
$("d-stop").addEventListener("click", (e) => stopContainer(state.selected, e.currentTarget));
$("term-stop").addEventListener("click", (e) => stopContainer(state.selected, e.currentTarget));
$("d-delete").addEventListener("click", (e) => {
  const name = state.selected;
  if (!confirm(`Delete container "${name}" and its private layer?`)) return;
  busy(e.currentTarget, "Deleting…", async () => {
    await post(`/api/containers/${enc(name)}/delete`);
    delete state.history[name];
    toast(`Deleted ${name}`);
    select(null);
    await refreshOverview();
  });
});
$("reload").addEventListener("click", () => location.reload());

// --------------------------------------------------------------------
// Detail view
// --------------------------------------------------------------------

function select(name, fresh = false) {
  const changed = name !== state.selected;
  state.selected = name;
  if (changed || fresh) state.selToken++;
  $("empty").hidden = !!name;
  $("detail").hidden = !name;
  if (!name) { closeTerminal(); renderTable(); return; }
  if (changed || fresh) {
    openTerminal(findContainer(name));
    refreshDiff();
    scheduleStats(0);
  }
  render();
}

function render() {
  renderTable();
  const c = state.selected && findContainer(state.selected);
  if (!c) return;
  // The terminal follows the container: attach once its session shows up,
  // and drop it when the session is gone (for example after a restart).
  if (hasSession(c) !== (termName === c.name)) openTerminal(c);
  renderDetailHead(c);
  renderStats();
  updateTerminalNote(c);
}

function setPill(id, text, cls) {
  const p = $(id);
  p.textContent = text;
  p.className = `pill ${cls || ""}`;
}

function metricsState(name) {
  const c = findContainer(name);
  const at = state.sampleAt[name];
  if (!c || !isRunning(c)) return "UNAVAILABLE";
  if (!at) return state.lastStats[name] === null ? "UNAVAILABLE" : "COLLECTING";
  return (Date.now() - at) / 1000 > STALE_AFTER ? "STALE" : "AVAILABLE";
}

function renderDetailHead(c) {
  $("d-name").textContent = c.name;
  const o = c.options;
  const parts = [`Image ${c.image || "unknown"}`];
  if (o) parts.push(`memory ${o.memory}, CPU ${o.cpu}%, PIDs ${o.pids}${o.net ? ", network" : ""}`);
  if (o && o.command) parts.push(`command: ${o.command}`);
  $("d-meta").textContent = parts.join(" · ");

  const s = statusOf(c);
  setPill("b-state", s.text, s.cls);
  setPill("b-terminal", TERMINAL_LABEL[c.terminal] || c.terminal, c.terminal === "ATTACHED" ? "running" : "");
  const m = metricsState(c.name);
  setPill("b-metrics", { AVAILABLE: "Metrics live", COLLECTING: "Metrics collecting…",
    STALE: "Metrics stale", UNAVAILABLE: "Metrics unavailable" }[m], m === "STALE" ? "warn" : m === "AVAILABLE" ? "running" : "");

  const running = isRunning(c);
  $("d-start").disabled = running || c.state === "UNKNOWN";
  $("d-edit").disabled = running;
  $("d-stop").disabled = !running;
  $("term-stop").disabled = !running;
  $("d-delete").disabled = running || c.state === "UNKNOWN";

  const notice = $("d-notice");
  let text = "";
  if (c.terminal === "ELSEWHERE") {
    text = "This container is running, but its terminal is not in this dashboard: it was started from a terminal window, " +
      "or before the dashboard restarted. The runtime cannot re-attach to it. You can still watch its usage and stop it.";
  } else if (c.terminal === "ENDED") {
    text = `This run has ended${c.exit_code != null ? ` with exit code ${c.exit_code}` : ""}. Its terminal output is kept below. Start runs it again with the same settings.`;
  } else if (c.state === "UNKNOWN") {
    text = "The dashboard could not read this container's lock file, so its state is unknown.";
  } else if (c.terminal === "NONE" && !running) {
    text = "Start runs this container again. Its settings from before are not known here, so default limits are used; use Edit and run to choose them.";
  }
  notice.textContent = text;
  notice.hidden = !text;
}

// --------------------------------------------------------------------
// Live stats from cgroup v2 (selected container only)
// --------------------------------------------------------------------

let statsTimer = null;

function scheduleStats(delay = 1000) {
  clearTimeout(statsTimer);
  statsTimer = setTimeout(pollStats, delay);
}

async function pollStats() {
  const name = state.selected, token = state.selToken;
  const c = name && findContainer(name);
  if (!c || !isRunning(c)) { scheduleStats(); return; }

  let s = null;
  try {
    s = await api(`/api/containers/${enc(name)}/stats`);
  } catch {
    s = undefined;                     // network error: the sample just goes stale
  }
  if (token !== state.selToken) return;   // the user picked another container meanwhile

  if (s && s.metrics === "AVAILABLE") addSample(name, s);
  else if (s && !state.sampleAt[name]) state.lastStats[name] = null;
  renderDetailHead(findContainer(name) || c);
  renderStats();
  scheduleStats();
}

function addSample(name, s) {
  const prev = state.lastStats[name];
  state.lastStats[name] = s;
  state.sampleAt[name] = Date.now();
  const h = state.history[name] || (state.history[name] = []);

  let cpu = null;
  if (prev && s.time > prev.time && s.cpu_usage_usec != null && prev.cpu_usage_usec != null) {
    if (s.cpu_usage_usec < prev.cpu_usage_usec) {
      h.length = 0;                    // counter went back: a new run with the same name
    } else {
      cpu = ((s.cpu_usage_usec - prev.cpu_usage_usec) / ((s.time - prev.time) * 1e6)) * 100;
    }
  }
  h.push({ t: s.time, mem: s.memory_current, cpu, pids: s.pids_current });
  const cutoff = s.time - WINDOW_SECONDS;
  while (h.length && h[0].t < cutoff) h.shift();
}

function renderStats() {
  const name = state.selected;
  if (!name) return;
  const c = findContainer(name);
  const live = c && isRunning(c);
  const h = state.history[name] || [];
  const raw = state.lastStats[name] || null;
  const last = h[h.length - 1];
  const lastCpu = [...h].reverse().find((p) => p.cpu != null);
  const m = metricsState(name);

  const value = (v, fmt) => (!live ? "-" : !raw ? (m === "COLLECTING" ? "…" : "Unavailable") : show(v, fmt));
  $("t-mem").textContent = value(last && last.mem, formatBytes);
  $("t-cpu").textContent = !live ? "-" : lastCpu ? `${lastCpu.cpu.toFixed(0)}%` : "Collecting…";
  $("t-pids").textContent = value(last && last.pids);
  $("t-oom").textContent = raw ? show(raw.oom_kills) : "-";
  $("t-oom").parentElement.classList.toggle("alert", !!(raw && raw.oom_kills > 0));

  $("t-mem-note").textContent = raw
    ? `limit ${show(raw.memory_max, formatBytes)}, peak ${show(raw.memory_peak, formatBytes)}` : "limit -";
  $("t-cpu-note").textContent = raw
    ? `cap ${show(raw.cpu_limit_percent, (v) => `${v}%`)}, throttled ${show(raw.cpu_throttled, (v) => `${v}×`)}` : "cap -";
  $("t-pids-note").textContent = raw ? `limit ${show(raw.pids_max)}` : "limit -";

  const age = state.sampleAt[name] ? Math.round((Date.now() - state.sampleAt[name]) / 1000) : null;
  $("sample-age").textContent = !live ? "" : age == null ? "Waiting for the first sample." :
    m === "STALE" ? `STALE: last sample ${age}s ago.` : `Last sample ${age}s ago.`;
  $("sample-age").className = m === "STALE" ? "stale" : "";
  $("chart-mode").textContent = live || !h.length
    ? "Live samples only: the last 60 seconds, not saved."
    : "Final samples (container exited).";

  const now = live ? Date.now() / 1000 : (last ? last.t : Date.now() / 1000);
  const lim = (v) => (typeof v === "number" ? v : null);
  const cpuPoints = h.filter((p) => p.cpu != null);
  const memPoints = h.filter((p) => p.mem != null);
  const pidPoints = h.filter((p) => p.pids != null);
  const emptyText = live ? (m === "UNAVAILABLE" ? "Metrics unavailable" : "Collecting…") : "No live data: the container is not running";
  drawChart($("chart-mem"), "Memory used", "memory.current vs memory.max", memPoints, "mem",
    raw && lim(raw.memory_max), formatBytes, now, emptyText);
  drawChart($("chart-cpu"), "CPU", "% of one CPU core vs cpu.max", cpuPoints, "cpu",
    raw && lim(raw.cpu_limit_percent), (v) => `${v.toFixed(0)}%`, now, emptyText);
  drawChart($("chart-pids"), "Processes", "pids.current vs pids.max", pidPoints, "pids",
    raw && lim(raw.pids_max), (v) => String(Math.round(v)), now, emptyText);
}

// A single-series line with the cgroup limit as a dashed reference line.
// Only numbers and fixed labels go into the SVG markup.
function drawChart(el, title, subtitle, points, key, limit, fmt, now, emptyText) {
  const W = Math.max(240, el.clientWidth - 24), H = 150, L = 48, R = 8, T = 14, B = 20;
  const values = points.map((p) => p[key]);
  const full = key === "cpu" ? Math.max(100, limit || 0) : Math.max(limit || 0, ...values, 1);
  const top = full * 1.12;
  const x = (t) => L + ((t - (now - WINDOW_SECONDS)) / WINDOW_SECONDS) * (W - L - R);
  const y = (v) => T + (1 - Math.min(Math.max(v, 0), top) / top) * (H - T - B);

  const ticks = [0, full / 2, full].map((v) => (key === "pids" ? Math.round(v) : v));
  const grid = ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>`).join("");
  const yLabels = ticks.map((v) => `<text x="${L - 6}" y="${y(v) + 3.5}" text-anchor="end">${fmt(v)}</text>`).join("");
  const xLabels = [60, 30, 0].map((s) =>
    `<text x="${x(now - s)}" y="${H - 4}" text-anchor="${s === 60 ? "start" : s === 0 ? "end" : "middle"}">${s ? `-${s}s` : "now"}</text>`).join("");

  let line = "", area = "";
  if (points.length > 1) {
    const d = points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)} ${y(p[key]).toFixed(1)}`).join("");
    line = `<path class="line" d="${d}"/>`;
    area = `<path class="area" d="${d}L${x(points[points.length - 1].t).toFixed(1)} ${y(0)}L${x(points[0].t).toFixed(1)} ${y(0)}Z"/>`;
  }
  const limitLine = limit ? `<line class="limit" x1="${L}" x2="${W - R}" y1="${y(limit)}" y2="${y(limit)}"/>
    <text class="limit-label" x="${L + 6}" y="${y(limit) - 5}">limit ${fmt(limit)}</text>` : "";

  el.innerHTML = `<figcaption>${title} <span class="cap-sub">${subtitle}</span></figcaption>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${title} chart">
      <g class="grid">${grid}</g><g class="axis">${yLabels}${xLabels}</g>
      ${area}${line}${limitLine}<text class="empty-text muted" x="${(L + W - R) / 2}" y="${H / 2}" text-anchor="middle" fill="currentColor"></text><g class="hover"></g>
      <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>
    </svg><div class="tooltip" hidden></div>`;
  if (points.length < 2) el.querySelector(".empty-text").textContent = emptyText;

  const svg = el.querySelector("svg"), tip = el.querySelector(".tooltip"), hover = el.querySelector(".hover");
  svg.addEventListener("mousemove", (e) => {
    if (points.length < 1) return;
    const box = svg.getBoundingClientRect();
    const t = (now - WINDOW_SECONDS) + ((e.clientX - box.left) / box.width * W - L) / (W - L - R) * WINDOW_SECONDS;
    const p = points.reduce((a, b) => (Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a));
    const px = x(p.t), py = y(p[key]);
    hover.innerHTML = `<line class="cross" x1="${px}" x2="${px}" y1="${T}" y2="${H - B}"/><circle class="dot" cx="${px}" cy="${py}" r="4"/>`;
    tip.hidden = false;
    tip.textContent = `${fmt(p[key])} · ${Math.round(now - p.t)}s ago`;
    tip.style.left = `${(px / W) * box.width + 12}px`;
    tip.style.top = `${(py / H) * box.height + 34}px`;
  });
  svg.addEventListener("mouseleave", () => { hover.innerHTML = ""; tip.hidden = true; });
}

// --------------------------------------------------------------------
// Terminal (xterm.js over server-sent events)
// --------------------------------------------------------------------

let term = null, fit = null, source = null, termName = null, offset = 0, streamState = "";

function ensureTerminal() {
  if (term || !window.Terminal) return;
  term = new Terminal({
    fontFamily: 'ui-monospace, "Cascadia Mono", Consolas, monospace',
    fontSize: 13, cursorBlink: true, convertEol: false,
    theme: { background: "#0d0d0c" },
  });
  fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("terminal"));
  fit.fit();

  let pending = "", timer = null;
  term.onData((data) => {
    if (!termName) return;
    pending += data;
    clearTimeout(timer);
    const name = termName;
    timer = setTimeout(() => {
      const chunk = pending; pending = "";
      post(`/api/containers/${enc(name)}/input`, chunk, true).catch((err) => {
        if (err.status !== 409) toast(`Input not sent: ${err.message}`, "error");
      });
    }, 10);
  });
  term.onResize(({ cols, rows }) => {
    if (termName) post(`/api/containers/${enc(termName)}/resize`, { cols, rows }).catch(() => {});
  });
  new ResizeObserver(() => { try { fit.fit(); } catch { /* hidden */ } }).observe($("terminal"));
}

function closeTerminal() {
  if (source) source.close();
  source = null;
  termName = null;
  streamState = "";
}

function hasSession(c) {
  return c && (c.terminal === "ATTACHED" || c.terminal === "ENDED");
}

function openTerminal(c) {
  ensureTerminal();
  closeTerminal();
  $("term-name").textContent = c ? `· ${c.name}` : "";
  if (!term) {
    $("term-note").textContent = "Terminal library failed to load.";
    return;
  }
  term.reset();
  if (!hasSession(c)) {
    term.write(c && c.terminal === "ELSEWHERE"
      ? "\x1b[90mThis container's shell is in another window. The runtime cannot re-attach to it here.\x1b[0m\r\n"
      : "\x1b[90mStart this container from the dashboard to use its terminal here.\x1b[0m\r\n");
    updateTerminalNote(c);
    return;
  }
  termName = c.name;
  offset = 0;
  connect();
  if (c.terminal === "ATTACHED") {
    post(`/api/containers/${enc(termName)}/resize`, { cols: term.cols, rows: term.rows }).catch(() => {});
    term.focus();
  }
}

function updateTerminalNote(c) {
  let text = "";
  if (!c) text = "";
  else if (c.terminal === "ATTACHED") text = streamState === "reconnecting" ? "Reconnecting…" : "Live: type here";
  else if (c.terminal === "ENDED") text = "Finished: output kept";
  else if (c.terminal === "ELSEWHERE") text = "Not attached here";
  else text = "No terminal";
  $("term-note").textContent = text;
}

function connect() {
  const name = termName;
  source = new EventSource(`/api/containers/${enc(name)}/output?from=${offset}&token=${enc(TOKEN)}`);
  source.onopen = () => { streamState = "open"; updateTerminalNote(findContainer(name)); };
  source.onmessage = (e) => {
    offset = Number(e.lastEventId) || offset;
    const bytes = Uint8Array.from(atob(e.data), (ch) => ch.charCodeAt(0));
    term.write(bytes);
  };
  source.addEventListener("end", () => {
    source.close();
    streamState = "ended";
    refreshOverview().then(() => { if (termName === name) refreshDiff(); });
  });
  source.onerror = () => {
    source.close();
    if (termName !== name || streamState === "ended") return;
    streamState = "reconnecting";
    updateTerminalNote(findContainer(name));
    setTimeout(() => {
      const c = findContainer(name);
      if (termName === name && hasSession(c)) connect();
    }, 1500);
  };
}

$("term-clear").addEventListener("click", () => { if (term) term.clear(); });
$("term-copy").addEventListener("click", async () => {
  if (!term) return;
  term.selectAll();
  const text = term.getSelection();
  term.clearSelection();
  try {
    await navigator.clipboard.writeText(text);
    toast("Terminal output copied");
  } catch {
    toast("The browser blocked clipboard access.", "error");
  }
});

function setFullscreen(on) {
  $("term-panel").classList.toggle("fullscreen", on);
  $("term-full").setAttribute("aria-pressed", String(on));
  $("term-full").textContent = on ? "Exit fullscreen" : "Fullscreen";
  if (fit) setTimeout(() => { try { fit.fit(); } catch { /* hidden */ } }, 0);
  if (term) term.focus();
}
$("term-full").addEventListener("click", () => setFullscreen(!$("term-panel").classList.contains("fullscreen")));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("term-panel").classList.contains("fullscreen")) setFullscreen(false);
});

// --------------------------------------------------------------------
// Copy-on-write changes
// --------------------------------------------------------------------

function sizeText(m) {
  if (!m || m.apparent == null) return `Unavailable${m && m.error ? ` (${m.error})` : ""}`;
  const extra = m.complete ? "" : ` (partial: ${m.error})`;
  return `${formatBytes(m.apparent)} apparent, ${formatBytes(m.disk)} on disk${extra}`;
}

async function refreshDiff() {
  const name = state.selected, token = state.selToken;
  if (!name) return;
  let d;
  try {
    d = await api(`/api/containers/${enc(name)}/diff`);
  } catch (err) {
    if (token === state.selToken) {
      $("diff-size").replaceChildren(el("span", { class: "error", text: err.message }));
    }
    return;
  }
  if (token !== state.selToken) return;

  const share = d.upper.apparent != null && d.image.apparent
    ? Math.max(0.5, (d.upper.apparent / d.image.apparent) * 100) : null;
  const bar = el("div", { class: "bar", title: "private layer as a share of the image" }, el("span"));
  if (share != null) bar.firstChild.style.width = `${Math.min(100, share)}%`;
  else bar.hidden = true;
  $("diff-size").replaceChildren(
    el("div", {}, "Private layer: ", el("strong", { text: sizeText(d.upper) })),
    el("div", {}, "Shared image: ", el("strong", { text: sizeText(d.image) })),
    bar,
    el("span", { class: "muted", text: "Only changed files are stored privately. The image (lower layer) is shared by every container that uses it, so its files are not charged to any one container." }));

  const list = $("diff-list");
  if (!d.changes.length) {
    list.replaceChildren(el("li", { class: "muted", text: "No changes: the container still matches its image." }));
    return;
  }
  list.replaceChildren(...d.changes.map((ch) => el("li", {},
    el("span", { class: `badge ${ch.kind}`, text: ch.kind.toUpperCase() }),
    el("span", { class: "path mono", text: ch.path }))));
}

$("diff-refresh").addEventListener("click", refreshDiff);

// --------------------------------------------------------------------
// Loops: one chain each, never overlapping
// --------------------------------------------------------------------

let diffTimer = null;
function diffLoop() {
  const c = state.selected && findContainer(state.selected);
  const next = () => { diffTimer = setTimeout(diffLoop, 3000); };
  if (c && isRunning(c)) refreshDiff().finally(next);
  else next();
}

// Keeps the "last sample" age and the STALE badge current between polls.
setInterval(() => {
  const c = state.selected && findContainer(state.selected);
  if (c) { renderDetailHead(c); renderStats(); }
}, 1000);
refreshOverview();
scheduleStats(0);
diffLoop();
