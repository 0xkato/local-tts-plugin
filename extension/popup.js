// Local TTS -- the toolbar panel: voice settings, the reading in progress,
// and whether the helper program is there.
"use strict";

const api = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);
const port = api.runtime.connect({ name: "popup" });
let status = null;

port.onMessage.addListener((message) => {
  if (message.type === "status") {
    status = message;
    render();
  }
});

const STATUS_TEXT = {
  idle: "Ready",
  connecting: "Connecting…",
  connected: "Ready",
  missing: "Not installed",
  error: "Stopped",
};

// The engine that will read: the one picked, else the helper's own default,
// the same fallback the helper makes
function selectedEngine() {
  const { catalog, settings } = status;
  if (!catalog) return null;
  const find = (key) => catalog.engines.find((e) => e.key === key);
  return find(settings.engine) || find(catalog.defaultEngine) || catalog.engines[0] || null;
}

function render() {
  const { host, catalog, settings, reading } = status;
  const pill = $("status");
  pill.dataset.state = host.state;
  pill.querySelector("span").textContent = STATUS_TEXT[host.state] || host.state;

  $("setup").hidden = host.state !== "missing";
  $("problem").hidden = host.state !== "error";
  $("problem-text").textContent = host.message || "";

  const live = reading && ["loading", "playing", "paused"].includes(reading.state);
  $("now").hidden = !live;
  if (live) {
    const total = reading.total || 0;
    const index = Math.min((reading.index || 0) + 1, total);
    const where = total ? `Sentence ${index} of ${total}` : "Starting…";
    $("now-text").textContent = reading.state === "paused" ? `Paused · ${where}` : where;
    $("now-fill").style.width = total ? `${(reading.index / total) * 100}%` : "0";
    const toggle = $("toggle");
    toggle.dataset.kind = reading.state === "paused" ? "play" : "pause";
    toggle.title = reading.state === "paused" ? "Resume" : "Pause";
    toggle.setAttribute("aria-label", toggle.title);
  }

  renderEngines(catalog, settings);
  if (document.activeElement !== $("follow")) $("follow").checked = settings.follow !== false;
  const speed = Number(settings.speed) || 1.5;
  if (document.activeElement !== $("speed")) $("speed").value = String(speed);
  $("speed-value").textContent = `${speed.toFixed(1)}×`;
}

function renderEngines(catalog, settings) {
  const box = $("engines");
  const voice = $("voice");
  const note = $("engine-note");
  if (!catalog) {
    box.replaceChildren();
    voice.replaceChildren(new Option("…"));
    voice.disabled = true;
    return;
  }
  const current = selectedEngine();
  box.replaceChildren(...catalog.engines.map((engine) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "engine";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(current && engine.key === current.key));
    if (!engine.available) {
      b.setAttribute("aria-disabled", "true");
      b.title = engine.reason;
    } else {
      b.title = engine.detail;
    }
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = engine.name;
    const weight = document.createElement("span");
    weight.className = "weight";
    weight.textContent = engine.weight;
    b.append(name, weight);
    b.addEventListener("click", () => {
      if (engine.available && (!current || engine.key !== current.key)) {
        port.postMessage({ type: "settings", engine: engine.key });
      }
    });
    return b;
  }));

  voice.disabled = !current;
  if (current) {
    const chosen = settings.voices[current.key] ?? current.defaultVoice;
    voice.replaceChildren(...current.voices.map((v) => new Option(v.label, v.id, false, v.id === chosen)));
  }

  note.hidden = true;
  note.className = "note";
  const failed = current && status.engineErrors && status.engineErrors[current.key];
  if (failed) {
    note.hidden = false;
    note.classList.add("bad");
    note.textContent = failed;
  } else if (current && !current.available) {
    note.hidden = false;
    note.classList.add("bad");
    note.textContent = `${current.name} ${current.reason}`;
  } else if (current && !current.downloaded) {
    note.hidden = false;
    note.textContent = `${current.name} downloads ${current.downloadSize || "its model"} the first time it reads.`;
  } else if (current && !current.speedControl) {
    note.hidden = false;
    note.textContent = "Speed needs ffmpeg for this voice: brew install ffmpeg";
  }
}

$("voice").addEventListener("change", (e) => {
  const engine = selectedEngine();
  port.postMessage({ type: "settings", voice: e.target.value, engineKey: engine && engine.key });
});
$("speed").addEventListener("input", (e) => {
  $("speed-value").textContent = `${Number(e.target.value).toFixed(1)}×`;
});
$("speed").addEventListener("change", (e) => {
  port.postMessage({ type: "settings", speed: Number(e.target.value) });
});
$("follow").addEventListener("change", (e) => {
  port.postMessage({ type: "settings", follow: e.target.checked });
});
for (const [id, action] of [["prev", "prev"], ["next", "next"], ["stop", "stop"]]) {
  $(id).addEventListener("click", () => port.postMessage({ type: "control", action }));
}
$("toggle").addEventListener("click", () => port.postMessage({ type: "control", action: "toggle" }));
$("retry").addEventListener("click", () => port.postMessage({ type: "reconnect" }));
$("restart").addEventListener("click", () => port.postMessage({ type: "reconnect" }));

const ALL_SITES = { origins: ["<all_urls>"] };
async function checkAccess() {
  try {
    $("access").hidden = await api.permissions.contains(ALL_SITES);
  } catch (_) {
    $("access").hidden = true;
  }
}
$("grant").addEventListener("click", async () => {
  try {
    await api.permissions.request(ALL_SITES);
  } finally {
    checkAccess();
  }
});
checkAccess();
