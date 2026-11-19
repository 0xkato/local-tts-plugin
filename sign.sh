#!/bin/sh
# Signs the extension as an unlisted (private) add-on on addons.mozilla.org, so
# regular Firefox keeps it installed. It is not listed or shown anywhere.
#
# Needs an API key from https://addons.mozilla.org/developers/addon/api/key/
# in WEB_EXT_API_KEY (the JWT issuer) and WEB_EXT_API_SECRET (the JWT secret).
# Every signed version is final: raise "version" in extension/manifest.json
# before signing again.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
: "${WEB_EXT_API_KEY:?set WEB_EXT_API_KEY to your JWT issuer}"
: "${WEB_EXT_API_SECRET:?set WEB_EXT_API_SECRET to your JWT secret}"

out="$here/dist"
npx --yes web-ext@8 sign --channel unlisted --source-dir "$here/extension" --artifacts-dir "$out"

signed=$(ls -t "$out"/*.xpi | head -1)
dest="$HOME/.local/share/local-tts-plugin/local-tts.xpi"
mkdir -p "$(dirname "$dest")"
cp "$signed" "$dest"
echo "Signed: $dest"
echo "Install it by opening that file in Firefox."
