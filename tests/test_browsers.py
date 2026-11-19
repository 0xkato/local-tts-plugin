"""The extension in real (headless) browsers -- Firefox and Chrome -- talking
to the installed helper.

Needs the helper installed (./install.sh --dev while working on this repo, so
the browsers start the code here). The browsers are copies of their own that
Selenium downloads into ~/.cache/selenium, never the installed ones. The
helper runs on its test voice with a silent player, so nothing plays out loud.
Screenshots land in $LOCAL_TTS_SHOTS when it is set.
"""
import functools
import http.server
import json
import os
import sys
import threading
import time

import pytest

from local_tts_plugin import register

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = os.path.join(ROOT, "tests", "pages")
SHOTS = os.environ.get("LOCAL_TTS_SHOTS")

webdriver = pytest.importorskip("selenium.webdriver")
HOME = os.path.expanduser("~")
pytestmark = pytest.mark.skipif(not os.access(register.launcher_path(HOME), os.X_OK),
                                reason="the helper is not installed: run ./install.sh --dev")
# The helper quits after 4 s with nothing playing, so a pause can outlast it
IDLE_S = 4
TEST_ENV = dict(LOCAL_TTS_FAKE_VOICES="1", LOCAL_TTS_PLUGIN_FAKE_PLAYER="0.45",
                LOCAL_TTS_VOICES_PYTHON=sys.executable, LOCAL_TTS_PLUGIN_IDLE_S=str(IDLE_S))
FIREFOX_UUID = "5b6f0d1e-8c1a-4e8f-9a51-6c0d2f3a7b10"

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


def separate_browser(name):
    """A browser of the test suite's own, downloaded once into Selenium's cache.
    Never the installed one: launching an installed Firefox applies any update
    it has waiting."""
    import subprocess
    from selenium.webdriver.common.selenium_manager import SeleniumManager

    manager = SeleniumManager()._get_binary()
    out = subprocess.run([str(manager), "--browser", name, "--force-browser-download",
                          "--avoid-stats", "--output", "json"],
                         capture_output=True, text=True, timeout=600)
    result = json.loads(out.stdout)["result"]
    if not result.get("browser_path") or not result.get("driver_path"):
        pytest.skip(f"could not get a separate {name}: {result}")
    return result["browser_path"], result["driver_path"]


def start_firefox(tmp_path_factory):
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service

    if not os.path.exists(register.firefox_manifest_path(HOME)):
        pytest.skip("the helper is not registered with Firefox: run ./install.sh --dev")
    firefox, driver_path = separate_browser("firefox")
    options = Options()
    options.binary_location = firefox
    options.add_argument("-headless")
    # A fixed moz-extension:// address, so a test can open the toolbar panel
    # (which the test driver only navigates to with system access, below)
    options.set_preference("extensions.webextensions.uuids",
                           json.dumps({"local-tts@local-tts-plugin": FIREFOX_UUID}))
    driver = webdriver.Firefox(options=options, service=Service(
        executable_path=driver_path, service_args=["--allow-system-access"],
        env=dict(os.environ, **TEST_ENV)))
    driver.install_addon(os.path.join(ROOT, "extension"), temporary=True)
    return driver


def start_chrome(tmp_path_factory):
    """Chrome with the keyed copy of the extension, and the helper registered in
    a throwaway profile: Chrome reads NativeMessagingHosts/ inside it."""
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    chrome, driver_path = separate_browser("chrome")
    profile = tmp_path_factory.mktemp("chrome-profile")
    hosts = profile / "NativeMessagingHosts"
    hosts.mkdir()
    (hosts / f"{register.HOST_NAME}.json").write_text(
        json.dumps(register.chromium_manifest(register.launcher_path(HOME))))
    extension = register.copy_extension(os.path.join(ROOT, "extension"),
                                        str(tmp_path_factory.mktemp("chrome-ext") / "extension"))
    options = Options()
    options.binary_location = chrome
    for arg in ("--headless=new", f"--user-data-dir={profile}", f"--load-extension={extension}",
                "--no-first-run", "--window-size=1280,860"):
        options.add_argument(arg)
    options.add_experimental_option("excludeSwitches", ["disable-extensions"])
    return webdriver.Chrome(options=options, service=Service(
        executable_path=driver_path, env=dict(os.environ, **TEST_ENV)))


@pytest.fixture(scope="module", params=["firefox", "chrome"])
def browser(request, tmp_path_factory):
    start = start_firefox if request.param == "firefox" else start_chrome
    driver = start(tmp_path_factory)
    driver.set_window_size(1280, 860)
    driver.set_script_timeout(40)
    yield driver
    driver.quit()


def shot(driver, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        driver.save_screenshot(os.path.join(SHOTS, f"{driver.name}-{name}.png"))


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



def scroll_y(driver):
    return driver.execute_script("return Math.round(scrollY)")


def test_follow_along_scrolls_only_while_it_is_on(browser, site):
    # On, the default: the page comes along to the sentence far below
    open_page(browser, f"{site}/long.html?on").click()
    result = browser.execute_async_script(WATCH, ["finished", "error"])
    assert result["state"] == "finished", result
    wait_for(lambda: scroll_y(browser) > 1500)

    # Off: the page stays where the reader left it
    open_page(browser, f"{site}/long.html?off").click()
    wait_for(lambda: shadow(browser, ".bar.shown .follow")).click()
    wait_for(lambda: shadow(browser, '.follow[aria-pressed="false"]'))
    result = browser.execute_async_script(WATCH, ["finished", "error"])
    assert result["state"] == "finished", result
    assert scroll_y(browser) == 0

    # Remembered on the next page; switched back on for the tests after this
    open_page(browser, f"{site}/long.html?again").click()
    wait_for(lambda: shadow(browser, '.bar.shown .follow[aria-pressed="false"]')).click()
    wait_for(lambda: shadow(browser, '.follow[aria-pressed="true"]'))
    shadow(browser, ".close").click()


METER = """
const done = arguments[arguments.length - 1];
const bars = document.querySelector("local-tts-ui").shadowRoot.querySelectorAll(".viz i");
const heights = [];
const started = Date.now();
(function poll() {
  heights.push(parseFloat(bars[1].style.height || "22"));
  if (Date.now() - started > 1500) return done(heights);
  setTimeout(poll, 15);
})();
"""


def test_the_meter_moves_with_the_sound_and_rests_in_silence(browser, site):
    open_page(browser, f"{site}/article.html?meter").click()
    wait_for(lambda: "Sentence 2 of 6" in (shadow(browser, ".sub").text or ""))
    heights = browser.execute_async_script(METER)
    assert max(heights) >= 80, heights   # the test voice's tone
    assert min(heights) <= 30, heights   # the silence between sentences
    shadow(browser, ".main").click()
    wait_for(lambda: shadow(browser, '.bar[data-state="paused"]'))
    time.sleep(0.5)
    resting = browser.execute_script(
        "return [...document.querySelector('local-tts-ui').shadowRoot"
        ".querySelectorAll('.viz i')].map(b => b.style.height)")
    assert all(h in ("", "22%") for h in resting), resting


def test_a_page_cannot_start_a_reading_by_itself(browser, site):
    """Only your clicks count: a page's own script selecting text and pressing
    the play button must not make the computer talk."""
    open_page(browser, f"{site}/article.html?script")
    browser.execute_script("""
        const ui = document.querySelector('local-tts-ui');
        ui.shadowRoot.querySelector('.bubble').click();
    """)
    time.sleep(1.5)
    assert not shadow(browser, ".bar.shown")


def test_a_pause_longer_than_the_helper_waits_keeps_the_place(browser, site):
    open_page(browser, f"{site}/article.html?long-pause").click()
    wait_for(lambda: "Sentence 3 of 6" in (shadow(browser, ".sub").text or ""))
    shadow(browser, ".main").click()
    wait_for(lambda: shadow(browser, '.bar[data-state="paused"]'))
    held = shadow(browser, ".sub").text
    time.sleep(IDLE_S + 3)                 # the helper quits to free the model
    assert shadow(browser, '.bar.shown[data-state="paused"]'), "the reading was thrown away"
    assert shadow(browser, ".sub").text == held
    shadow(browser, ".main").click()       # resume: it starts again where it was
    result = browser.execute_async_script(WATCH, ["finished", "error"])
    assert result["state"] == "finished", result
    assert result["seen"][0] == EXPECTED[2] and result["seen"][-1] == EXPECTED[-1], result


def in_panel(driver, fn):
    """Runs fn with the toolbar panel open in a tab of its own, then goes back."""
    page = driver.current_window_handle
    driver.switch_to.new_window("tab")
    try:
        if driver.name == "firefox":
            driver.get(f"moz-extension://{FIREFOX_UUID}/popup.html")
        else:
            driver.get(f"chrome-extension://{register.CHROME_EXTENSION_ID}/popup.html")
        return fn()
    finally:
        driver.close()
        driver.switch_to.window(page)


def flip_play_button(driver, on):
    switch = wait_for(lambda: driver.find_element("id", "bubble"))
    # The settings arrive from the background script; set before then, they'd be overwritten
    wait_for(lambda: driver.find_element("id", "status").get_attribute("data-state"))
    if switch.is_selected() != on:
        driver.find_element("css selector", 'label[for="bubble"] .switch-title').click()
    # The panel's hint changes once the setting is saved
    wait_for(lambda: driver.find_element("id", "how-off").is_displayed() == (not on))
    shot(driver, f"panel-play-button-{'on' if on else 'off'}")


READ_SELECTION = """
const done = arguments[arguments.length - 1];
const api = globalThis.browser ?? globalThis.chrome;
api.tabs.query({}).then((tabs) => {
  const tab = tabs.find((t) => (t.url || "").includes(arguments[0]));
  return api.tabs.sendMessage(tab.id, { type: "read-selection" });
}).then(() => done(true), (e) => done(String(e)));
"""


def test_the_play_button_can_be_switched_off_from_the_toolbar_panel(browser, site):
    """Off, highlighting text shows nothing (copying code in peace), while
    right-click > Read aloud and the shortcut still read the selection."""
    open_page(browser, f"{site}/article.html?no-button")
    in_panel(browser, lambda: flip_play_button(browser, False))
    try:
        assert not shadow(browser, ".bubble.shown")   # the one showing went away
        select_story(browser)
        time.sleep(0.5)
        assert not shadow(browser, ".bubble.shown")
        # What the menu item and the shortcut send to the page
        sent = in_panel(browser, lambda: browser.execute_async_script(READ_SELECTION, "no-button"))
        assert sent is True, sent
        result = browser.execute_async_script(WATCH, ["finished", "error"])
        assert result["state"] == "finished", result
        assert result["seen"] == EXPECTED
    finally:
        in_panel(browser, lambda: flip_play_button(browser, True))
    select_story(browser)
    assert wait_for(lambda: shadow(browser, ".bubble.shown"))
