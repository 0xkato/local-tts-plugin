// Local TTS -- background script.
//
// Keeps the one connection to the helper program (native messaging), starts
// readings for the page that asked, and sends that page the helper's
// progress. The audio itself plays from the helper, not from any tab.
"use strict";

const api = globalThis.browser ?? globalThis.chrome;
const HOST = "local_tts_plugin";
const DEFAULTS = { engine: null, voices: {}, speed: 1.5 };
const MISSING =
  "The Local TTS helper isn't installed. Run ./install.sh in the local-tts-plugin folder, then try again.";
const CRASHED =
  "The Local TTS helper stopped unexpectedly. Details: ~/.local/share/local-tts-plugin/host.log";

let port = null;
let host = { state: "idle", message: "" }; // idle | connecting | connected | missing | error
let catalog = null;
let lastError = null;
let sawBye = false;
let reading = null; // { id, tabId, frameId, state, index, total, message, engineName, voiceLabel, speed }
let nextId = Date.now() % 1e9;
let settings = { ...DEFAULTS };
const popups = new Set();

const settingsLoaded = api.storage.local.get(DEFAULTS).then((stored) => {
  settings = { ...DEFAULTS, ...stored };
});

// ── The helper ──────────────────────────────────────────────────────────

function connect() {
  if (port) return port;
  sawBye = false;
  host = { state: "connecting", message: "" };
  try {
    port = api.runtime.connectNative(HOST);
  } catch (error) {
    port = null;
    disconnected({ error });
    return null;
  }
  port.onMessage.addListener(fromHost);
  port.onDisconnect.addListener(disconnected);
  port.postMessage({ type: "hello" });
  broadcast();
  return port;
}

function toHost(message) {
  const p = connect();
  if (!p) return;
  try {
    p.postMessage(message);
  } catch (error) {
    disconnected({ error });
  }
}

function disconnected(p) {
  const error = (p && p.error && p.error.message) || (api.runtime.lastError && api.runtime.lastError.message) || "";
  port = null;
  if (/no such native application|not found|manifest/i.test(error)) {
    host = { state: "missing", message: MISSING };
  } else if (sawBye || !error) {
    host = { state: "idle", message: "" };
  } else {
    host = { state: "error", message: CRASHED };
  }
  if (reading && ["loading", "playing", "paused"].includes(reading.state)) {
    const quiet = host.state === "idle" && sawBye;
    reading.state = quiet ? "stopped" : "error";
    reading.message = quiet ? null : host.message || CRASHED;
    toTab({ type: "host", message: { type: "state", readingId: reading.id, state: reading.state,
                                     message: reading.message, index: reading.index, total: reading.total } });
  }
  broadcast();
}

function fromHost(message) {
  if (host.state !== "connected") host = { state: "connected", message: "" };
  switch (message.type) {
    case "catalog":
      catalog = message;
      break;
    case "heartbeat":
      return;
    case "bye":
      sawBye = true;
      return;
    case "ready":
      break;
    case "error":
      lastError = message.message;
      if (reading && !message.engine) {
        toTab({ type: "host", message: { type: "state", readingId: reading.id, state: "error",
                                         message: message.message } });
      }
      break;
    case "reading":
    case "sentence":
    case "state":
      if (!reading || message.readingId !== reading.id) return;
      if (message.type === "state") {
        reading.state = message.state;
        reading.message = message.message ?? null;
      }
      if (message.type === "reading") reading.total = message.sentences.length;
      if ("index" in message) reading.index = message.index;
      if ("total" in message) reading.total = message.total;
      if (message.engineName) {
        reading.engineName = message.engineName;
        reading.voiceLabel = message.voiceLabel;
      }
      if ("speed" in message) reading.speed = message.speed;
      toTab({ type: "host", message });
      break;
  }
  broadcast();
}

function toTab(message) {
  if (!reading) return;
  api.tabs.sendMessage(reading.tabId, message, { frameId: reading.frameId }).catch(() => {});
}

// ── Readings ────────────────────────────────────────────────────────────

function chosenVoice() {
  const engine = settings.engine ?? (catalog && catalog.defaultEngine);
  return { engine: settings.engine, voice: engine ? settings.voices[engine] ?? null : null };
}

async function startReading(message, sender) {
  await settingsLoaded;
  const tabId = sender.tab.id;
  const frameId = sender.frameId ?? 0;
  if (reading && (reading.tabId !== tabId || reading.frameId !== frameId)) {
    toTab({ type: "ended", readingId: reading.id });
  }
  const { engine, voice } = chosenVoice();
  reading = { id: ++nextId, tabId, frameId, state: "loading", index: message.startIndex || 0,
              total: 0, message: null, engineName: null, voiceLabel: null, speed: settings.speed };
  toHost({ type: "play", text: message.text, readingId: reading.id, engine, voice,
           speed: settings.speed, startIndex: message.startIndex || 0 });
  broadcast();
  return { readingId: reading.id, speed: settings.speed, hostState: host.state, hostMessage: host.message };
}

function control(action) {
  if (!reading) return;
  toHost({ type: action });
  if (action === "stop") {
    reading.state = "stopped";
    broadcast();
  }
}

async function saveSettings(change) {
  await settingsLoaded;
  const before = chosenVoice();
  if ("engine" in change) settings.engine = change.engine;
  if ("voice" in change) {
    const engine = change.engineKey ?? settings.engine ?? (catalog && catalog.defaultEngine);
    if (engine) settings.voices = { ...settings.voices, [engine]: change.voice };
  }
  if ("speed" in change) settings.speed = Math.min(2, Math.max(0.5, Number(change.speed) || 1.5));
  await api.storage.local.set(settings);
  const live = reading && ["loading", "playing", "paused"].includes(reading.state);
  const after = chosenVoice();
  if (live && (before.engine !== after.engine || before.voice !== after.voice)) {
    toHost({ type: "voice", engine: after.engine ?? undefined, voice: after.voice ?? undefined });
  } else if ("engine" in change && !live) {
    toHost({ type: "preload", engine: after.engine ?? undefined });
  }
  if (live && "speed" in change) toHost({ type: "speed", value: settings.speed });
  broadcast();
}

// ── Messages from pages ─────────────────────────────────────────────────

api.runtime.onMessage.addListener((message, sender, sendResponse) => {
  switch (message && message.type) {
    case "play":
      startReading(message, sender).then(sendResponse, (e) => sendResponse({ error: String(e) }));
      return true;
    case "control":
      if (fromReadingTab(sender)) control(message.action);
      return false;
    case "speed":
      saveSettings({ speed: message.value });
      return false;
    case "preload":
      settingsLoaded.then(() => toHost({ type: "preload", engine: settings.engine ?? undefined }));
      return false;
    case "page-gone":
      if (fromReadingTab(sender)) {
        control("stop");
        reading = null;
        broadcast();
      }
      return false;
  }
  return false;
});

function fromReadingTab(sender) {
  return reading && sender.tab && sender.tab.id === reading.tabId && (sender.frameId ?? 0) === reading.frameId;
}

api.tabs.onRemoved.addListener((tabId) => {
  if (reading && reading.tabId === tabId) {
    control("stop");
    reading = null;
    broadcast();
  }
});

// ── The toolbar panel ───────────────────────────────────────────────────

function status() {
  return { type: "status", host, catalog, settings, reading, lastError };
}

function broadcast() {
  const message = status();
  for (const p of popups) {
    try {
      p.postMessage(message);
    } catch (_) {
      popups.delete(p);
    }
  }
}

api.runtime.onConnect.addListener((p) => {
  if (p.name !== "popup") return;
  popups.add(p);
  p.onDisconnect.addListener(() => popups.delete(p));
  p.onMessage.addListener((message) => {
    if (message.type === "settings") saveSettings(message);
    else if (message.type === "control") control(message.action);
    else if (message.type === "reconnect") {
      if (port) port.disconnect();
      port = null;
      connect();
    }
  });
  settingsLoaded.then(() => {
    if (!port && host.state !== "missing") connect();
    p.postMessage(status());
  });
});

// ── Menu and shortcuts ──────────────────────────────────────────────────

function createMenu() {
  Promise.resolve(api.contextMenus.removeAll())
    .then(() => api.contextMenus.create({ id: "read-selection", title: "Read aloud", contexts: ["selection"] }))
    .catch(() => {});
}
api.runtime.onInstalled.addListener(createMenu);
api.runtime.onStartup.addListener(createMenu);

api.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "read-selection" && tab) {
    api.tabs.sendMessage(tab.id, { type: "read-selection" }, { frameId: info.frameId ?? 0 }).catch(() => {});
  }
});

api.commands.onCommand.addListener(async (command) => {
  if (command === "toggle-pause") {
    control("toggle");
  } else if (command === "read-selection") {
    const [tab] = await api.tabs.query({ active: true, currentWindow: true });
    if (tab) api.tabs.sendMessage(tab.id, { type: "read-selection" }).catch(() => {});
  }
});
