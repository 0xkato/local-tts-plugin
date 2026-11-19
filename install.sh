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
"$env/bin/python" -m local_tts_plugin.register install || true

cat <<NEXT

Now load the extension in Firefox:
  1. Open about:debugging#/runtime/this-firefox
  2. Click "Load Temporary Add-on…" and pick
     $here/extension/manifest.json
NEXT
