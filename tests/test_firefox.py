"""The extension in a real (headless) Firefox, talking to the installed helper.

Needs geckodriver and the helper registered (./install.sh --dev while working
on this repo, so Firefox starts the code here). The helper runs on its test
voice with a silent player, so nothing plays out loud.
Screenshots land in $LOCAL_TTS_SHOTS when it is set.
"""
import functools
import http.server
import json
import os
import shutil
import sys
import threading
import time

import pytest

from local_tts_plugin import register

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = os.path.join(ROOT, "tests", "pages")
SHOTS = os.environ.get("LOCAL_TTS_SHOTS")

webdriver = pytest.importorskip("selenium.webdriver")
pytestmark = [
    pytest.mark.skipif(shutil.which("geckodriver") is None, reason="needs geckodriver"),
    pytest.mark.skipif(not os.path.exists(register.firefox_manifest_path(os.path.expanduser("~"))),
                       reason="the helper is not registered: run ./install.sh --dev"),
]

EXPECTED = [
    "Why it matters",
    "The study ran for ten years, and the results surprised everyone.",
    "Its authors called it a turning point 😀.",
    "Nobody expected that.",
    "First finding in the list",
    "Second finding",
]

WATCH = """
const done = arguments[arguments.length - 1];
const until = arguments[0];
const seen = [];
const started = Date.now();
(function poll() {
  const ui = document.querySelector("local-tts-ui");
  const bar = ui && ui.shadowRoot.querySelector(".bar");
  const h = CSS.highlights.get("local-tts-sentence");
  if (h) for (const r of h) { const s = r.toString(); if (seen[seen.length - 1] !== s) seen.push(s); }
  const state = bar && bar.dataset.state;
  if (until.includes(state) || Date.now() - started > 25000) {
    return done({ seen, state, sub: bar && ui.shadowRoot.querySelector(".sub").textContent });
  }
  setTimeout(poll, 20);
})();
"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        if self.path.startswith("/csp.html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self'; script-src 'self'")
        super().end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=PAGES))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def separate_firefox():
    """A Firefox of the test suite's own, downloaded once into Selenium's cache.
    Never the installed one: launching that applies any update it has waiting."""
    import subprocess
    from selenium.webdriver.common.selenium_manager import SeleniumManager

    manager = SeleniumManager()._get_binary()
    out = subprocess.run([str(manager), "--browser", "firefox", "--force-browser-download",
                          "--avoid-stats", "--output", "json"],
                         capture_output=True, text=True, timeout=600)
    result = json.loads(out.stdout)["result"]
    if not result.get("browser_path"):
        pytest.skip(f"could not get a separate Firefox: {result}")
    return result["browser_path"], result.get("driver_path")


@pytest.fixture(scope="module")
def browser():
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service

    firefox, driver_path = separate_firefox()
    options = Options()
    options.binary_location = firefox
    options.add_argument("-headless")
    env = dict(os.environ, LOCAL_TTS_FAKE_VOICES="1", LOCAL_TTS_PLUGIN_FAKE_PLAYER="0.45",
               LOCAL_TTS_VOICES_PYTHON=sys.executable)
    driver = webdriver.Firefox(options=options,
                               service=Service(executable_path=driver_path, env=env))
    driver.set_window_size(1280, 860)
    driver.set_script_timeout(40)
    driver.install_addon(os.path.join(ROOT, "extension"), temporary=True)
    yield driver
    driver.quit()


def shot(driver, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        driver.save_screenshot(os.path.join(SHOTS, f"{name}.png"))


def shadow(driver, selector):
    return driver.execute_script(
        "const ui = document.querySelector('local-tts-ui');"
        "return ui && ui.shadowRoot.querySelector(arguments[0]);", selector)


def select_story(driver):
    driver.execute_script("""
        const sel = getSelection();
        sel.selectAllChildren(document.getElementById('story'));
        document.getElementById('story').dispatchEvent(
            new MouseEvent('mouseup', { bubbles: true, button: 0 }));
    """)


def wait_for(fn, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def open_page(driver, url):
    driver.get(url)
    # The content script arrives at document_idle; until then a selection shows nothing
    def offered():
        select_story(driver)
        time.sleep(0.35)   # the button appears 120 ms after the mouse is released
        return shadow(driver, ".bubble.shown")
    return wait_for(offered)


def test_highlighting_text_offers_a_play_button(browser, site):
    bubble = open_page(browser, f"{site}/article.html")
    time.sleep(0.3)
    shot(browser, "1-bubble")
    rect = browser.execute_script("return arguments[0].getBoundingClientRect().toJSON()", bubble)
    assert round(rect["width"]) == 34


def test_each_sentence_is_highlighted_as_it_is_read(browser, site):
    bubble = open_page(browser, f"{site}/article.html")
    bubble.click()
    # Watching starts at once: each sentence 'plays' for under half a second
    result = browser.execute_async_script(WATCH, ["finished", "error"])
    assert result["state"] == "finished", result
    assert result["seen"] == EXPECTED
    assert not browser.execute_script("return CSS.highlights.has('local-tts-sentence')")


def test_pause_holds_the_place_and_play_carries_on(browser, site):
    bubble = open_page(browser, f"{site}/article.html")
    bubble.click()
    wait_for(lambda: "Sentence 2 of 6" in (shadow(browser, ".sub").text or ""))
    shot(browser, "2-reading")
    shadow(browser, ".main").click()
    wait_for(lambda: shadow(browser, '.bar[data-state="paused"]'))
    shot(browser, "3-paused")
    held = shadow(browser, ".sub").text
    time.sleep(1.2)
    assert shadow(browser, ".sub").text == held, "it kept reading while paused"
    assert "Paused" in held
    shadow(browser, ".main").click()
    result = browser.execute_async_script(WATCH, ["finished", "error"])
    assert result["state"] == "finished", result
    assert result["seen"][-1] == EXPECTED[-1]


def test_stop_closes_the_bar(browser, site):
    bubble = open_page(browser, f"{site}/article.html")
    bubble.click()
    wait_for(lambda: shadow(browser, '.bar.shown[data-state="playing"]'))
    shadow(browser, ".close").click()
    wait_for(lambda: not shadow(browser, ".bar.shown"))
    assert not browser.execute_script("return CSS.highlights.has('local-tts-sentence')")


def test_it_works_and_looks_right_under_a_strict_csp(browser, site):
    bubble = open_page(browser, f"{site}/csp.html")
    rect = browser.execute_script("return arguments[0].getBoundingClientRect().toJSON()", bubble)
    assert round(rect["width"]) == 34, "the overlay's styles were blocked"
    bubble.click()
    result = browser.execute_async_script(WATCH, ["finished", "error"])
    assert result["state"] == "finished", result
    assert result["seen"] == EXPECTED

