#!/bin/sh
# Sets up Local TTS for your browser: the voices (unless they are already
# there), the helper program the extension talks to, and its registration
# with Firefox, Chrome and Brave. Run it again after pulling changes.
#
#   ./install.sh          install
#   ./install.sh --dev    the helper runs from this folder, so edits apply
#                         without reinstalling
set -eu

here=$(cd "$(dirname "$0")" && pwd)
data="$HOME/.local/share/local-tts-plugin"
env="$data/env"
voices="$HOME/.local/share/local-tts/voices-env"
# The local-tts release the helper is built on, as pinned in pyproject.toml
tag=$(sed -n 's/.*local-tts@\(v[0-9][0-9.]*\).*/\1/p' "$here/pyproject.toml" | head -n 1)

step() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if ! command -v uv >/dev/null 2>&1; then
  echo "Local TTS needs uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

# 1. The voices. The local-tts desktop app uses the same ones, so they are
#    set up once for both.
if [ -n "${LOCAL_TTS_VOICES_PYTHON:-}" ]; then
  echo "Using the voices at $LOCAL_TTS_VOICES_PYTHON"
elif [ -x "$voices/bin/python" ]; then
  echo "The voices are already set up in $voices"
else
  step "Setting up the voices in $voices"
  echo "About 1.3 GB of Python packages. Each voice's model downloads the first time it reads."
  uv venv --quiet --python 3.11 "$voices"
  VIRTUAL_ENV="$voices" uv pip install --quiet \
    "local-tts[voices] @ git+https://github.com/0xkato/local-tts@$tag"
fi

# 2. The helper
step "Setting up the helper in $env"
uv venv --quiet --allow-existing --python 3.11 "$env"
if [ "${1:-}" = "--dev" ]; then
  VIRTUAL_ENV="$env" uv pip install --quiet --editable "$here"
else
  VIRTUAL_ENV="$env" uv pip install --quiet --reinstall-package local-tts-plugin "$here"
fi

# 3. The browsers
step "Registering it with your browsers"
status=0
"$env/bin/python" -m local_tts_plugin.register install --extension "$here/extension" || status=$?
if [ "$status" -ge 2 ]; then
  exit "$status"
fi

step "Last step: add the extension to your browser"
if [ -f "$data/local-tts.xpi" ]; then
  echo "  Firefox         open $data/local-tts.xpi in Firefox and click Add"
else
  echo "  Firefox         download local-tts.xpi from"
  echo "                  https://github.com/0xkato/local-tts-plugin/releases"
  echo "                  and open it in Firefox (or, to try it for one session:"
  echo "                  about:debugging#/runtime/this-firefox > Load Temporary Add-on >"
  echo "                  $here/extension/manifest.json)"
fi
echo "  Chrome / Brave  chrome://extensions (brave://extensions) > turn on Developer mode >"
echo "                  Load unpacked > $data/extension"

if [ "$status" -eq 1 ]; then
  echo
  echo "Something above is marked ✗: install it and run ./install.sh again."
fi
