// Local TTS -- content script.
//
// Shows a play button beside selected text, sends that text to be read, and
// highlights each sentence on the page as it is spoken, with a small control
// bar in the corner. Everything visible lives in a shadow root, so the page's
// styles cannot reach it and its styles cannot leak into the page.
(() => {
  "use strict";
  if (globalThis.__localTtsLoaded) return;
  globalThis.__localTtsLoaded = true;

  const api = globalThis.browser ?? globalThis.chrome;
  const HIGHLIGHT = "local-tts-sentence";
  const SPEED_MIN = 0.5;
  const SPEED_MAX = 2.0;
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

  // ── Text from the page ────────────────────────────────────────────────
  // The text sent to be read is built from the selection's text nodes, with a
  // record of where every character came from, so a sentence the helper
  // reports as characters 120-180 can be found on the page again.

  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "TEXTAREA", "INPUT", "SELECT",
    "OPTION", "BUTTON", "SVG", "MATH", "CANVAS", "VIDEO", "AUDIO", "IFRAME", "OBJECT", "LOCAL-TTS-UI"]);
  const INLINE = /^(inline|inline-block|inline-flex|inline-grid|inline-table|contents|ruby|ruby-base|ruby-text)$/;

  function isFootnoteMarker(el) {
    return el.tagName === "SUP" &&
      (el.querySelector('a[href^="#"]') !== null || /^\s*\[[^\]]{1,4}\]\s*$/.test(el.textContent));
  }

  function isHidden(el) {
    if (el.getAttribute("aria-hidden") === "true") return true;
    if (typeof el.checkVisibility === "function") {
      return !el.checkVisibility({ visibilityProperty: true, checkVisibilityCSS: true });
    }
    return false;
  }

  function extract(range) {
    const chars = [];
    const nodeOf = [];
    const offsetOf = [];
    const nodes = [];
    const blockCache = new Map();
    let pending = "";
    let lastBlock = null;

    const blockOf = (node) => {
      for (let el = node.parentElement; el; el = el.parentElement) {
        let block = blockCache.get(el);
        if (block === undefined) {
          block = !INLINE.test(getComputedStyle(el).display);
          blockCache.set(el, block);
        }
        if (block) return el;
      }
      return document.documentElement;
    };
    const breakBefore = (kind) => {
      if (!chars.length) return;
      if (kind === "\n\n" || pending === "\n\n") pending = "\n\n";
      else pending = pending === "\n" ? "\n\n" : kind;
    };

    let root = range.commonAncestorContainer;
    if (root.nodeType !== Node.ELEMENT_NODE) root = root.parentNode;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (node.nodeType === Node.ELEMENT_NODE) {
          // Outside the selection first: cheap, and spares the style lookups
          // below on the rest of a long page.
          if (!range.intersectsNode(node)) return NodeFilter.FILTER_REJECT;
          if (SKIP.has(node.tagName.toUpperCase()) || isFootnoteMarker(node) || isHidden(node)) {
            return NodeFilter.FILTER_REJECT;
          }
          return node.tagName === "BR" ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
        }
        return range.intersectsNode(node) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      },
    });

    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.nodeType === Node.ELEMENT_NODE) {
        breakBefore("\n"); // <br>
        continue;
      }
      const data = node.data;
      const start = node === range.startContainer ? range.startOffset : 0;
      const end = node === range.endContainer ? range.endOffset : data.length;
      if (start >= end) continue;

      const block = blockOf(node);
      if (lastBlock && block !== lastBlock) breakBefore("\n\n");
      const whiteSpace = getComputedStyle(node.parentElement).whiteSpace;
      const pre = whiteSpace.startsWith("pre") || whiteSpace === "break-spaces";
      const index = nodes.push(node) - 1;

      for (let i = start; i < end; i++) {
        const ch = data[i];
        if (pre && ch === "\n") {
          breakBefore("\n");
          continue;
        }
        if (/\s/.test(ch)) {
          if (chars.length && !pending) pending = " ";
          continue;
        }
        lastBlock = block;
        for (const p of pending) {
          chars.push(p);
          nodeOf.push(-1);
          offsetOf.push(-1);
        }
        pending = "";
        chars.push(ch);
        nodeOf.push(index);
        offsetOf.push(i);
      }
    }
    return { text: chars.join(""), nodes, nodeOf, offsetOf };
  }

  function rangeFor(doc, start, end) {
    let a = start;
    while (a < end && doc.nodeOf[a] < 0) a++;
    let b = end - 1;
    while (b >= a && doc.nodeOf[b] < 0) b--;
    if (a > b) return null;
    const first = doc.nodes[doc.nodeOf[a]];
    const last = doc.nodes[doc.nodeOf[b]];
    if (!first.isConnected || !last.isConnected) return null;
    const range = document.createRange();
    try {
      range.setStart(first, doc.offsetOf[a]);
      range.setEnd(last, doc.offsetOf[b] + 1);
    } catch (_) {
      return null;
    }
    return range;
  }

  // ── Highlighting ──────────────────────────────────────────────────────

  // Firefox shows content scripts the page through Xray wrappers; should the
  // highlight API not be reachable that way, the page's own copy is used.
  function highlight(range) {
    for (const scope of [globalThis, globalThis.wrappedJSObject]) {
      try {
        if (!scope || typeof scope.Highlight !== "function" || !scope.CSS || !scope.CSS.highlights) continue;
        if (range) scope.CSS.highlights.set(HIGHLIGHT, new scope.Highlight(range));
        else scope.CSS.highlights.delete(HIGHLIGHT);
        return;
      } catch (_) {
        // try the next way in
      }
    }
  }

  let lastUserScroll = 0;
  const userScrolled = () => { lastUserScroll = Date.now(); };
  addEventListener("wheel", userScrolled, { passive: true, capture: true });
  addEventListener("touchmove", userScrolled, { passive: true, capture: true });
  addEventListener("keydown", (e) => {
    if (["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown", " "].includes(e.key)) userScrolled();
  }, { capture: true });

  // Follow along: scroll the page to the sentence being read. A setting,
  // remembered across pages; while it is on, scrolling yourself still
  // holds it off for a few seconds.
  let follow = true;

  function keepInView(range, force = false) {
    if (!range) return;
    if (!force && (!follow || Date.now() - lastUserScroll < 4000)) return;
    const rect = range.getBoundingClientRect();
    if (!rect.height) return;
    const bottomLimit = innerHeight - (ui.bar.offsetHeight || 0) - 40;
    if (rect.top >= 64 && rect.bottom <= bottomLimit) return;
    scrollBy({ top: rect.top - innerHeight * 0.3, behavior: reducedMotion.matches ? "auto" : "smooth" });
  }

  // ── The interface ─────────────────────────────────────────────────────

  const ICONS = {
    play: "M8 5.6v12.8a1.1 1.1 0 0 0 1.7.93l10-6.4a1.1 1.1 0 0 0 0-1.86l-10-6.4A1.1 1.1 0 0 0 8 5.6z",
    pause: "M7.2 5h2.6c.66 0 1.2.54 1.2 1.2v11.6c0 .66-.54 1.2-1.2 1.2H7.2C6.54 19 6 18.46 6 17.8V6.2C6 5.54 6.54 5 7.2 5zm7 0h2.6c.66 0 1.2.54 1.2 1.2v11.6c0 .66-.54 1.2-1.2 1.2h-2.6c-.66 0-1.2-.54-1.2-1.2V6.2c0-.66.54-1.2 1.2-1.2z",
    prev: "M6.5 5.5c.55 0 1 .45 1 1v11c0 .55-.45 1-1 1s-1-.45-1-1v-11c0-.55.45-1 1-1zm11.9.3a1 1 0 0 1 1.6.8v10.8a1 1 0 0 1-1.6.8l-7.6-5.4a1.1 1.1 0 0 1 0-1.8z",
    next: "M17.5 5.5c.55 0 1 .45 1 1v11c0 .55-.45 1-1 1s-1-.45-1-1v-11c0-.55.45-1 1-1zM5.6 5.8A1 1 0 0 0 4 6.6v10.8a1 1 0 0 0 1.6.8l7.6-5.4a1.1 1.1 0 0 0 0-1.8z",
    replay: "M12 5a7 7 0 1 1-6.6 4.7 1 1 0 1 1 1.9.66A5 5 0 1 0 12 7h-1.6l1.3 1.3a1 1 0 0 1-1.4 1.4l-3-3a1 1 0 0 1 0-1.4l3-3a1 1 0 1 1 1.4 1.4L10.4 5z",
  };
  const STROKES = {
    close: "M7 7l10 10M17 7L7 17",
    minus: "M7 12h10",
    plus: "M12 7v10M7 12h10",
    follow: "M12 7.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 1 0 0-9zM12 3.5v3M12 17.5v3M3.5 12h3M17.5 12h3",
  };
  const SVG_NS = "http://www.w3.org/2000/svg";

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVG_NS, "path");
    if (ICONS[name]) {
      path.setAttribute("d", ICONS[name]);
      path.setAttribute("fill", "currentColor");
    } else {
      path.setAttribute("d", STROKES[name]);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "currentColor");
      path.setAttribute("stroke-width", "2");
      path.setAttribute("stroke-linecap", "round");
    }
    svg.appendChild(path);
    return svg;
  }

  function el(tag, className, children = []) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    for (const child of children) node.append(child);
    return node;
  }

  function button(className, iconName, label) {
    const b = el("button", className, [icon(iconName)]);
    b.type = "button";
    b.title = label;
    b.setAttribute("aria-label", label);
    return b;
  }

  const STYLE = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    button { font: inherit; color: inherit; background: none; border: 0; margin: 0; padding: 0;
             cursor: pointer; display: inline-grid; place-items: center; -webkit-tap-highlight-color: transparent; }
    button:focus-visible { outline: 2px solid #A9B6FF; outline-offset: 2px; }
    svg { width: 18px; height: 18px; display: block; }

    .bubble {
      position: fixed; width: 34px; height: 34px; border-radius: 50%;
      background: linear-gradient(135deg, #8FA2FF 0%, #A98BFF 100%); color: #fff;
      box-shadow: 0 6px 18px rgba(92, 104, 230, .38), 0 1px 3px rgba(20, 22, 40, .25),
                  inset 0 1px 0 rgba(255, 255, 255, .28);
      opacity: 0; transform: scale(.6); pointer-events: none;
      transition: opacity .14s ease, transform .18s cubic-bezier(.2, .9, .3, 1.3);
    }
    .bubble.shown { opacity: 1; transform: scale(1); pointer-events: auto; }
    .bubble:hover { transform: scale(1.08); }
    .bubble:active { transform: scale(.95); }
    .bubble svg { width: 16px; height: 16px; margin-left: 2px; }

    .bar {
      position: fixed; right: 20px; bottom: 20px; width: 476px; max-width: calc(100vw - 32px);
      display: flex; align-items: center; gap: 6px; padding: 8px 10px 8px 14px;
      border-radius: 18px; overflow: hidden;
      font: 500 13px/1.25 -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", system-ui, sans-serif;
      color: #EEF0F6; background: rgba(22, 24, 31, .92);
      -webkit-backdrop-filter: blur(16px) saturate(1.4); backdrop-filter: blur(16px) saturate(1.4);
      border: 1px solid rgba(255, 255, 255, .08);
      box-shadow: 0 18px 40px rgba(8, 10, 20, .32), 0 2px 8px rgba(8, 10, 20, .22);
      opacity: 0; transform: translateY(14px) scale(.98); pointer-events: none;
      transition: opacity .2s ease, transform .26s cubic-bezier(.2, .8, .2, 1);
    }
    .bar.shown { opacity: 1; transform: none; pointer-events: auto; }

    .meta { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; cursor: pointer;
            border-radius: 10px; margin: -4px; padding: 4px; transition: background .15s; }
    .meta:hover { background: rgba(255, 255, 255, .05); }
    .viz { flex: none; width: 18px; height: 18px; display: flex; align-items: flex-end; justify-content: space-between; }
    .viz i { width: 3px; height: 30%; border-radius: 2px; background: #A9B6FF; transition: height .2s ease; }
    .bar[data-state="playing"] .viz i { animation: eq 1.1s ease-in-out infinite; }
    .bar[data-state="playing"] .viz i:nth-child(2) { animation-delay: -.35s; }
    .bar[data-state="playing"] .viz i:nth-child(3) { animation-delay: -.7s; }
    .bar[data-state="playing"] .viz i:nth-child(4) { animation-delay: -.2s; }
    @keyframes eq { 0%, 100% { height: 25%; } 50% { height: 100%; } }
    .bar[data-state="loading"] .viz i { animation: wait 1s ease-in-out infinite; height: 35%; }
    .bar[data-state="loading"] .viz i:nth-child(2) { animation-delay: .15s; }
    .bar[data-state="loading"] .viz i:nth-child(3) { animation-delay: .3s; }
    .bar[data-state="loading"] .viz i:nth-child(4) { animation-delay: .45s; }
    @keyframes wait { 0%, 100% { opacity: .3; } 50% { opacity: 1; } }
    .bar[data-state="error"] .viz i { background: #FF8A93; height: 30%; }
    .bar[data-state="paused"] .viz i, .bar[data-state="finished"] .viz i { height: 30%; opacity: .7; }

    .text { min-width: 0; }
    .title { font-weight: 600; letter-spacing: -.005em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .sub { margin-top: 2px; font-size: 11.5px; font-weight: 450; color: #9CA3B5;
           font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .bar[data-state="error"] .sub { color: #FF9AA2; white-space: normal; }

    .transport { display: flex; align-items: center; gap: 2px; flex: none; }
    .icon { width: 32px; height: 32px; border-radius: 50%; color: #D5D9E5; transition: background .15s, color .15s; }
    .icon:hover { background: rgba(255, 255, 255, .08); color: #fff; }
    .icon:disabled { opacity: .35; cursor: default; background: none; }
    .main { width: 38px; height: 38px; border-radius: 50%; color: #fff;
            background: linear-gradient(135deg, #8FA2FF 0%, #A98BFF 100%);
            box-shadow: 0 4px 12px rgba(92, 104, 230, .35), inset 0 1px 0 rgba(255, 255, 255, .25);
            transition: transform .12s ease, filter .15s; }
    .main:hover { filter: brightness(1.08); }
    .main:active { transform: scale(.94); }
    .main svg { width: 18px; height: 18px; }
    .main.play svg { margin-left: 2px; }

    .speed { display: flex; align-items: center; flex: none; height: 28px; padding: 0 2px; margin-left: 2px;
             border-radius: 14px; background: rgba(255, 255, 255, .06); }
    .speed button { width: 22px; height: 22px; border-radius: 50%; color: #B9BFCE; }
    .speed button:hover { background: rgba(255, 255, 255, .1); color: #fff; }
    .speed svg { width: 13px; height: 13px; }
    .speed .value { min-width: 34px; text-align: center; font-size: 12px; font-weight: 600;
                    font-variant-numeric: tabular-nums; }
    .follow { width: 30px; height: 30px; color: #8E95A7; }
    .follow svg { width: 17px; height: 17px; }
    .follow[aria-pressed="true"] { color: #B4C0FF; background: rgba(143, 162, 255, .16); }
    .follow[aria-pressed="true"]:hover { background: rgba(143, 162, 255, .24); }
    .close { width: 28px; height: 28px; color: #8E95A7; }
    .close svg { width: 15px; height: 15px; }

    .progress { position: absolute; left: 0; right: 0; bottom: 0; height: 2px; background: rgba(255, 255, 255, .06); }
    .progress span { display: block; height: 100%; width: 0;
                     background: linear-gradient(90deg, #8FA2FF, #A98BFF); transition: width .35s ease; }
    .bar[data-state="loading"] .progress span { opacity: .55; }

    @media (max-width: 520px) { .bar { right: 16px; left: 16px; width: auto; bottom: 16px; } .speed { display: none; } }
    @media (prefers-reduced-motion: reduce) {
      .bubble, .bar, .progress span { transition: none; }
      .viz i { animation: none !important; }
    }
  `;

  const ui = (() => {
    const root = document.createElement("local-tts-ui");
    root.style.setProperty("all", "initial", "important");
    root.style.setProperty("position", "fixed", "important");
    root.style.setProperty("inset", "0 auto auto 0", "important");
    root.style.setProperty("width", "0", "important");
    root.style.setProperty("height", "0", "important");
    root.style.setProperty("z-index", "2147483647", "important");
    const shadow = root.attachShadow({ mode: "open" });

    let styled = false;
    try {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(STYLE);
      shadow.adoptedStyleSheets = [sheet];
      styled = shadow.adoptedStyleSheets.length === 1;
    } catch (_) {}
    if (!styled) {
      const style = document.createElement("style");
      style.textContent = STYLE;
      shadow.append(style);
    }

    const bubble = button("bubble", "play", "Read aloud");
    const viz = el("div", "viz", [el("i"), el("i"), el("i"), el("i")]);
    const title = el("div", "title");
    const sub = el("div", "sub");
    const prev = button("icon prev", "prev", "Previous sentence");
    const main = button("main", "pause", "Pause");
    const next = button("icon next", "next", "Next sentence");
    const minus = button("", "minus", "Slower");
    const value = el("span", "value");
    const plus = button("", "plus", "Faster");
    const followButton = button("icon follow", "follow", "Follow along");
    const close = button("icon close", "close", "Stop reading");
    const fill = el("span");
    const meta = el("div", "meta", [viz, el("div", "text", [title, sub])]);
    meta.title = "Show the sentence being read";
    const bar = el("div", "bar", [
      meta,
      el("div", "transport", [prev, main, next]),
      el("div", "speed", [minus, value, plus]),
      followButton,
      close,
      el("div", "progress", [fill]),
    ]);
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Local TTS");
    shadow.append(bubble, bar);

    const mount = () => {
      if (root.isConnected) return;
      // A copy left behind by an earlier version of the extension
      for (const stale of document.querySelectorAll("local-tts-ui")) stale.remove();
      document.documentElement.append(root);
    };
    return { root, shadow, bubble, bar, meta, title, sub, prev, main, next, minus, value, plus,
             follow: followButton, close, fill, mount };
  })();

  function setMain(kind) {
    const labels = { pause: "Pause", play: "Resume", replay: "Read again", retry: "Try again" };
    const glyph = kind === "pause" ? "pause" : kind === "replay" || kind === "retry" ? "replay" : "play";
    ui.main.replaceChildren(icon(glyph));
    ui.main.className = `main ${glyph}`;
    ui.main.title = labels[kind];
    ui.main.setAttribute("aria-label", labels[kind]);
    ui.main.dataset.kind = kind;
  }

  // ── The bubble beside a selection ─────────────────────────────────────

  let selectionRange = null;
  let lastPreload = 0;

  function selectedRange() {
    const sel = getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
    const first = sel.getRangeAt(0);
    const last = sel.getRangeAt(sel.rangeCount - 1);
    const range = document.createRange();
    range.setStart(first.startContainer, first.startOffset);
    range.setEnd(last.endContainer, last.endOffset);
    if (ui.root.contains(range.commonAncestorContainer)) return null;
    const active = document.activeElement;
    if (active && /^(INPUT|TEXTAREA)$/.test(active.tagName)) return null;
    if (sel.toString().replace(/\s+/g, "").length < 2) return null;
    return range;
  }

  function placeBubble() {
    if (!selectionRange) return;
    // Beside the last selected line on screen: the selection may run on past
    // the bottom of the window (Select All on a long article)
    const rects = [...selectionRange.getClientRects()];
    const rect = rects.reverse().find((r) => (r.width || r.height) && r.bottom > 0 && r.top < innerHeight);
    if (!rect) {
      ui.bubble.classList.remove("shown");
      return;
    }
    const size = 34;
    let x = rect.right + 6;
    let y = rect.bottom - size / 2 + 6;
    if (x + size > innerWidth - 8) x = Math.max(8, rect.right - size);
    if (y + size > innerHeight - 8) y = rect.top - size - 6;
    ui.bubble.style.left = `${Math.round(x)}px`;
    ui.bubble.style.top = `${Math.round(Math.max(8, y))}px`;
    ui.bubble.classList.add("shown");
  }

  function hideBubble() {
    selectionRange = null;
    ui.bubble.classList.remove("shown");
  }

  function offerSelection() {
    const range = selectedRange();
    if (!range) {
      hideBubble();
      return;
    }
    ui.mount();
    selectionRange = range;
    placeBubble();
  }

  let offerTimer = 0;
  const offerSoon = () => {
    clearTimeout(offerTimer);
    offerTimer = setTimeout(offerSelection, 120);
  };

  addEventListener("mouseup", (e) => {
    if (e.button === 0 && !e.composedPath().includes(ui.root)) offerSoon();
  }, true);
  addEventListener("keyup", (e) => {
    if (e.shiftKey || e.key === "Shift" || ((e.metaKey || e.ctrlKey) && e.key === "a")) offerSoon();
  }, true);
  document.addEventListener("selectionchange", () => {
    if (selectionRange && getSelection().isCollapsed) hideBubble();
  });
  addEventListener("mousedown", (e) => {
    if (!e.composedPath().includes(ui.root)) hideBubble();
  }, true);
  addEventListener("scroll", () => requestAnimationFrame(placeBubble), { passive: true, capture: true });
  addEventListener("resize", () => requestAnimationFrame(placeBubble), { passive: true });

  ui.bubble.addEventListener("mousedown", (e) => e.preventDefault());
  ui.bubble.addEventListener("pointerenter", () => {
    if (Date.now() - lastPreload > 30000) {
      lastPreload = Date.now();
      api.runtime.sendMessage({ type: "preload" }).catch(() => {});
    }
  });
  ui.bubble.addEventListener("click", () => {
    const range = selectionRange;
    hideBubble();
    if (range) read(range);
  });

  // ── Reading ───────────────────────────────────────────────────────────

  let current = null; // { id, doc, sentences, index, total, state, speed }
  let early = [];      // helper events that beat the reply carrying our reading id
  let hideTimer = 0;

  async function read(range, startIndex = 0, reuse = null) {
    const doc = reuse || extract(range);
    if (!doc.text.trim()) return;
    getSelection().removeAllRanges();
    clearTimeout(hideTimer);
    highlight(null);
    current = { id: null, doc, sentences: null, index: startIndex, total: 0, state: "loading", speed: 1.5 };
    early = [];
    ui.mount();
    ui.title.textContent = "Local TTS";
    render({ state: "loading", message: "Starting…" });
    ui.bar.classList.add("shown");
    const mine = current;
    let reply;
    try {
      reply = await api.runtime.sendMessage({ type: "play", text: doc.text, startIndex });
    } catch (error) {
      reply = { error: String(error) };
    }
    if (current !== mine) return;
    if (!reply || reply.error) {
      render({ state: "error", message: (reply && reply.error) || "Local TTS isn't responding." });
      return;
    }
    mine.id = reply.readingId;
    mine.speed = reply.speed;
    ui.value.textContent = `${mine.speed.toFixed(1)}×`;
    if (reply.hostState === "missing") render({ state: "error", message: reply.hostMessage });
    const waiting = early;
    early = [];
    for (const message of waiting) onHost(message);
  }

  function render({ state, message }) {
    if (!current) return;
    if (state) current.state = state;
    ui.bar.dataset.state = current.state;
    const counted = current.total ? `Sentence ${Math.min(current.index + 1, current.total)} of ${current.total}` : "";
    const texts = {
      loading: message || counted || "Preparing…",
      playing: counted,
      paused: current.total ? `Paused · ${Math.min(current.index + 1, current.total)} of ${current.total}` : "Paused",
      finished: "Finished",
      stopped: "Stopped",
      error: message || "Something went wrong.",
    };
    ui.sub.textContent = texts[current.state] ?? counted;
    const kind = { playing: "pause", loading: "pause", paused: "play", finished: "replay", error: "retry" }[current.state];
    if (kind) setMain(kind);
    ui.prev.disabled = !current.total || current.index <= 0;
    ui.next.disabled = !current.total || current.index >= current.total - 1;
    const done = current.state === "finished" ? 1 : current.total ? current.index / current.total : 0;
    ui.fill.style.width = `${Math.round(done * 1000) / 10}%`;
  }

  function showSentence(index, force = false) {
    if (!current || !current.sentences) return;
    const span = current.sentences[index];
    if (!span) return;
    const range = rangeFor(current.doc, span[0], span[1]);
    highlight(range);
    keepInView(range, force);
  }

  function endReading(delay = 0) {
    clearTimeout(hideTimer);
    const done = () => {
      highlight(null);
      ui.bar.classList.remove("shown");
      current = null;
    };
    if (delay) hideTimer = setTimeout(done, delay);
    else done();
  }

  function onHost(message) {
    if (!current) return;
    if (current.id === null) {
      early.push(message);
      return;
    }
    if (message.readingId !== current.id) return;
    if (message.engineName) ui.title.textContent = `${message.engineName} · ${message.voiceLabel}`;
    if (typeof message.speed === "number") {
      current.speed = message.speed;
      ui.value.textContent = `${message.speed.toFixed(1)}×`;
    }
    if ("total" in message) current.total = message.total;
    switch (message.type) {
      case "reading":
        current.sentences = message.sentences;
        current.total = message.sentences.length;
        render({});
        break;
      case "sentence":
        current.index = message.index;
        showSentence(message.index);
        render({ state: message.paused ? "paused" : "playing" });
        break;
      case "state":
        if (typeof message.index === "number") current.index = message.index;
        if (message.state === "loading" && !message.message && current.state === "playing") {
          render({}); // a short wait between sentences: nothing to announce
          break;
        }
        render({ state: message.state, message: message.message });
        if (message.state === "playing" || message.state === "paused") showSentence(current.index);
        if (message.state === "finished") {
          highlight(null);
          endReading(2600);
        }
        if (message.state === "stopped") endReading();
        break;
    }
  }

  ui.main.addEventListener("click", () => {
    if (!current) return;
    const kind = ui.main.dataset.kind;
    if (kind === "pause") control("pause");
    else if (kind === "play") control("resume");
    else if (kind === "replay") read(null, 0, current.doc);
    else if (kind === "retry") read(null, current.index, current.doc);
  });
  function showFollow() {
    ui.follow.setAttribute("aria-pressed", String(follow));
    const label = follow ? "Following along — click to stop scrolling the page"
                         : "Not following — click to scroll along with the reading";
    ui.follow.title = label;
    ui.follow.setAttribute("aria-label", label);
  }
  showFollow();

  function setFollow(on, fromStorage = false) {
    const turnedOn = on && !follow;
    follow = on;
    showFollow();
    if (turnedOn && current) showSentence(current.index, true);
    if (!fromStorage) api.runtime.sendMessage({ type: "follow", value: on }).catch(() => {});
  }

  api.storage.local.get({ follow: true }).then((stored) => {
    follow = stored.follow !== false;
    showFollow();
  }).catch(() => {});
  api.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.follow) setFollow(changes.follow.newValue !== false, true);
  });

  ui.follow.addEventListener("click", () => setFollow(!follow));
  ui.meta.addEventListener("click", () => {
    if (current) showSentence(current.index, true);
  });
  ui.prev.addEventListener("click", () => control("prev"));
  ui.next.addEventListener("click", () => control("next"));
  ui.close.addEventListener("click", () => {
    control("stop");
    endReading();
  });
  const nudgeSpeed = (step) => {
    if (!current) return;
    const speed = Math.round(Math.min(SPEED_MAX, Math.max(SPEED_MIN, current.speed + step)) * 10) / 10;
    current.speed = speed;
    ui.value.textContent = `${speed.toFixed(1)}×`;
    api.runtime.sendMessage({ type: "speed", value: speed }).catch(() => {});
  };
  ui.minus.addEventListener("click", () => nudgeSpeed(-0.1));
  ui.plus.addEventListener("click", () => nudgeSpeed(0.1));

  function control(action) {
    api.runtime.sendMessage({ type: "control", action }).catch(() => {});
  }

  api.runtime.onMessage.addListener((message) => {
    if (!message) return;
    if (message.type === "host") onHost(message.message);
    else if (message.type === "ended" && current && message.readingId === current.id) endReading();
    else if (message.type === "read-selection") {
      const range = selectedRange() || selectionRange;
      hideBubble();
      if (range) read(range);
    }
  });

  addEventListener("pagehide", () => {
    if (current) api.runtime.sendMessage({ type: "page-gone" }).catch(() => {});
  });
})();
