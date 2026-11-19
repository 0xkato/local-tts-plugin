// Local TTS -- background script.
//
// Keeps the one connection to the helper program (native messaging), starts
// readings for the page that asked, and sends that page the helper's
// progress. The audio itself plays from the helper, not from any tab.
"use strict";

const api = globalThis.browser ?? globalThis.chrome;
const HOST = "local_tts_plugin";
// bubble: the ▶ button beside a selection (the menu and shortcut work regardless)
const DEFAULTS = { engine: null, voices: {}, speed: 1.5, follow: true, bubble: true };
const MISSING =
  "The Local TTS helper isn't installed. Run ./install.sh from the local-tts-plugin folder, then try again.";
const CRASHED =
  "The Local TTS helper stopped unexpectedly. Details: ~/.local/share/local-tts-plugin/host.log";
const LIVE = ["loading", "playing", "paused"];

let port = null;
let host = { state: "idle", message: "" }; // idle | connecting | connected | missing | error
let catalog = null;
let lastError = null;
let engineErrors = {}; // engine key -> why it failed to load
let sawBye = false;
// { id, tabId, frameId, state, index, total, message, engineName, voiceLabel, speed, detached }
// "detached": paused so long that the helper quit to free the model; resuming
// starts the reading again from the same sentence.
let reading = null;
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

// Starts the helper if need be: for what only the helper can do (reading,
// loading a model, the voice list).
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
  if (reading && LIVE.includes(reading.state)) {
    if (sawBye && reading.state === "paused") {
      // Quit to free the model while paused: keep the place, resume restarts
      reading.detached = true;
      toTab({ type: "detached", readingId: reading.id });
    } else {
      reading.state = "error";
      reading.message = host.message || CRASHED;
      toTab({ type: "host", message: { type: "state", readingId: reading.id, state: "error",
                                       message: reading.message, index: reading.index, total: reading.total } });
    }
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
      delete engineErrors[message.engine];
      break;
    case "error":
      // Failures of a reading arrive as its "state"; these are about a model
      // or a command, and are shown in the toolbar panel.
      if (message.engine) engineErrors[message.engine] = message.message;
      else lastError = message.message;
      break;
    case "levels":
      // the meter's readings: for the page only, many per sentence
      if (reading && message.readingId === reading.id) toTab({ type: "host", message });
      return;
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

async function startReading(message, sender) {
  await settingsLoaded;
  const tabId = sender.tab.id;
  const frameId = sender.frameId ?? 0;
  if (reading && (reading.tabId !== tabId || reading.frameId !== frameId)) {
    toTab({ type: "ended", readingId: reading.id });
  }
  reading = { id: ++nextId, tabId, frameId, state: "loading", index: message.startIndex || 0,
              total: 0, message: null, engineName: null, voiceLabel: null, speed: settings.speed,
              detached: false };
  // Every engine's chosen voice: the helper may fall back to another engine
  toHost({ type: "play", text: message.text, readingId: reading.id, engine: settings.engine,
           voices: settings.voices, speed: settings.speed, startIndex: message.startIndex || 0 });
  broadcast();
  return { readingId: reading.id, speed: settings.speed, hostState: host.state, hostMessage: host.message };
}

// Pause, resume, skip, stop: things a reading in progress does. They never
// start the helper on their own -- with it gone there is nothing to control,
// except a detached reading, which the page restarts.
function control(action) {
  if (!reading) return;
  if (action === "stop") {
    if (port) toHost({ type: "stop" });
    reading.state = "stopped";
    broadcast();
    return;
  }
  if (!port) {
    if (reading.detached && reading.state === "paused" && (action === "resume" || action === "toggle")) {
      toTab({ type: "restart", readingId: reading.id });
    }
    return;
  }
  toHost({ type: action });
}

async function saveSettings(change) {
  await settingsLoaded;
  let voiceChanged = false;
  if ("engine" in change && change.engine !== settings.engine) {
    settings.engine = change.engine;
    voiceChanged = true;
  }
  if ("voice" in change) {
    const engine = change.engineKey ?? settings.engine ?? (catalog && catalog.defaultEngine);
    if (engine && settings.voices[engine] !== change.voice) {
      settings.voices = { ...settings.voices, [engine]: change.voice };
      voiceChanged = true;
    }
  }
  if ("speed" in change) settings.speed = Math.min(2, Math.max(0.5, Number(change.speed) || 1.5));
  if ("follow" in change) settings.follow = Boolean(change.follow);
  if ("bubble" in change) settings.bubble = Boolean(change.bubble);
  await api.storage.local.set(settings);

  const live = reading && LIVE.includes(reading.state) && port;
  if (live && voiceChanged) {
    toHost({ type: "voice", engine: settings.engine ?? undefined, voices: settings.voices });
  } else if ("engine" in change && voiceChanged && !live) {
    toHost({ type: "preload", engine: settings.engine ?? undefined, voices: settings.voices });
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
    case "follow":
      saveSettings({ follow: message.value });
      return false;
    case "preload":
      settingsLoaded.then(() => toHost({ type: "preload", engine: settings.engine ?? undefined,
                                         voices: settings.voices }));
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
  return { type: "status", host, catalog, settings, reading, lastError, engineErrors };
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
      engineErrors = {};
      lastError = null;
      connect();
    }
  });
  settingsLoaded.then(() => {
    // The panel lists the voices, which only the helper knows
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
