# Local TTS for Firefox, Chrome and Brave

Highlight text on any page, press play, and hear it read by the neural voices
from [local-tts](https://github.com/0xkato/local-tts) — Kokoro, Phonon or Ming,
running on your own computer. Nothing is sent anywhere.

## How it works

```
browser extension ──native messaging──▶ helper program ──▶ voice worker
  play button, control bar,              local-tts engine:     Kokoro / Phonon / Ming
  sentence highlighting                  sentences, playback   (local-tts voices env)
```

The extension can't run a voice model itself, so a small helper program does:
Firefox starts it when you press play and it plays the audio, using the same
engine as the local-tts desktop app (pinned to a release in `pyproject.toml`).
The page only shows which sentence is being read. After five minutes with
nothing playing the helper quits and frees the model.

## Install

You need the local-tts voices set up first (the *Quick Start* in its README:
`brew install espeak-ng ffmpeg` and the voices environment), and
[uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:0xkato/local-tts-plugin.git
cd local-tts-plugin
./install.sh
```

Then install the extension in Firefox. It has to be signed by Mozilla to stay
installed; `./sign.sh` does that as a private, unlisted add-on (nothing is
published) with an API key from
[addons.mozilla.org](https://addons.mozilla.org/developers/addon/api/key/):

```bash
WEB_EXT_API_KEY=user:… WEB_EXT_API_SECRET=… ./sign.sh
```

and open the file it writes, `~/.local/share/local-tts-plugin/local-tts.xpi`, in
Firefox. Each signed version is final, so raise `version` in
`extension/manifest.json` before signing changes.

For a quick try without signing, load `extension/manifest.json` from
`about:debugging#/runtime/this-firefox` → **Load Temporary Add-on…** instead;
Firefox drops it again when it restarts.

**Chrome or Brave:** open `chrome://extensions` (or `brave://extensions`), turn
on **Developer mode**, click **Load unpacked** and pick
`~/.local/share/local-tts-plugin/extension`. That is the copy `install.sh`
keeps, with a key in its manifest that fixes the extension's ID — Chrome lets
the helper talk only to an extension it knows by ID.

## Using it

- **Highlight text** and press the ▶ button that appears beside it — or
  right-click → *Read aloud*, or press <kbd>⌥ Option</kbd> <kbd>⇧ Shift</kbd> <kbd>R</kbd>.
- The **control bar** in the corner has pause, previous / next sentence, speed
  and stop. <kbd>⌥ Option</kbd> <kbd>⇧ Shift</kbd> <kbd>P</kbd> pauses from anywhere.
- The sentence being read is **highlighted on the page**. With **Follow along**
  on, the page scrolls with the reading; switch it off with the target button
  on the bar (or in the toolbar panel) to look around freely. Clicking the
  bar's title jumps to the sentence being read either way.
- The **toolbar button** picks the engine, voice and speed (Kokoro with Heart
  by default) and controls the reading from any tab.
- It keeps reading when you switch tabs, and stops when you close the page.

## Troubleshooting

```bash
~/.local/share/local-tts-plugin/env/bin/python -m local_tts_plugin.register status
```

lists anything missing. Logs: `~/.local/share/local-tts-plugin/host.log` and
`voices-worker.log` next to it. To remove the helper:
`… -m local_tts_plugin.register uninstall`, then delete
`~/.local/share/local-tts-plugin`.

## Development

```bash
./install.sh --dev                      # Firefox runs the helper from this folder
uv venv -p 3.11 .venv && VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
.venv/bin/python -m pytest tests/
```

The helper's tests run it as a browser does, with a test-tone voice. The
browser tests (`tests/test_browsers.py`) drive headless Firefox and Chrome that
Selenium downloads into `~/.cache/selenium` — never your installed browsers,
since launching an installed Firefox applies any update it has waiting.

To move to a newer local-tts, change the tag in `pyproject.toml` and run
`./install.sh` again.
