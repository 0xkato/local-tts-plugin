#!/bin/sh
# Installs the helper program the Local TTS extension talks to and registers
# it with Firefox. Run it again after pulling changes.
#
#   ./install.sh          install from this folder
#   ./install.sh --dev    editable install: edits here apply without reinstalling
set -eu

here=$(cd "$(dirname "$0")" && pwd)
env="$HOME/.local/share/local-tts-plugin/env"

if ! command -v uv >/dev/null 2>&1; then
  echo "This needs uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

echo "Setting up the helper in $env"
uv venv --quiet --allow-existing --python 3.11 "$env"
if [ "${1:-}" = "--dev" ]; then
  VIRTUAL_ENV="$env" uv pip install --quiet --editable "$here"
else
  VIRTUAL_ENV="$env" uv pip install --quiet --reinstall-package local-tts-plugin "$here"
fi

echo
"$env/bin/python" -m local_tts_plugin.register install --extension "$here/extension" || true

data="$HOME/.local/share/local-tts-plugin"
echo
echo "Then add the extension to your browser:"
if [ -f "$data/local-tts.xpi" ]; then
  echo "  Firefox         open $data/local-tts.xpi in Firefox and click Add"
else
  echo "  Firefox         sign it first with ./sign.sh (see the README), or for a quick try:"
  echo "                  about:debugging#/runtime/this-firefox > Load Temporary Add-on >"
  echo "                  $here/extension/manifest.json"
fi
echo "  Chrome / Brave  chrome://extensions (brave://extensions) > turn on Developer mode >"
echo "                  Load unpacked > $data/extension"
