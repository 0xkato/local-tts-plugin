"""Tells the browser where the host is: a start script plus a native
messaging manifest naming it.

    python -m local_tts_plugin.register install     (what install.sh runs)
    python -m local_tts_plugin.register uninstall
    python -m local_tts_plugin.register status
"""
import json
import os
import shlex
import shutil
import sys

HOST_NAME = "local_tts_plugin"
EXTENSION_ID = "local-tts@local-tts-plugin"
DESCRIPTION = "Local TTS: reads highlighted text with the voices on this computer"

# The browser starts the host with its own environment. Opened from the Dock,
# that PATH is the bare system one, without Homebrew's ffmpeg (speed control).
LAUNCHER = """#!/bin/sh
# Started by the browser for the Local TTS extension.
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH
exec {python} -m local_tts_plugin.host "$@"
"""


def data_dir(home):
    return os.path.join(home, ".local", "share", "local-tts-plugin")


def launcher_path(home):
    return os.path.join(data_dir(home), "local-tts-host")


def firefox_manifest_path(home):
    if sys.platform == "darwin":
        folder = os.path.join(home, "Library", "Application Support", "Mozilla",
                              "NativeMessagingHosts")
    else:
        folder = os.path.join(home, ".mozilla", "native-messaging-hosts")
    return os.path.join(folder, HOST_NAME + ".json")


def _write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def install(python=None, home=None) -> dict:
    home = home or os.path.expanduser("~")
    python = python or sys.executable
    launcher = launcher_path(home)
    _write(launcher, LAUNCHER.format(python=shlex.quote(python)), mode=0o755)
    manifest = {
        "name": HOST_NAME,
        "description": DESCRIPTION,
        "path": launcher,           # must be absolute on macOS
        "type": "stdio",
        "allowed_extensions": [EXTENSION_ID],
    }
    firefox = firefox_manifest_path(home)
    _write(firefox, json.dumps(manifest, indent=2) + "\n")
    return {"launcher": launcher, "firefox": firefox}


def uninstall(home=None) -> list:
    home = home or os.path.expanduser("~")
    removed = []
    for path in (firefox_manifest_path(home), launcher_path(home)):
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    return removed


def status(home=None) -> list:
    """(ok, what, fix) for each thing the extension needs."""
    from local_tts.voices import voice_catalog

    home = home or os.path.expanduser("~")
    manifest = firefox_manifest_path(home)
    registered = os.path.exists(manifest)
    target = ""
    if registered:
        with open(manifest, encoding="utf-8") as f:
            target = json.load(f).get("path", "")
    voices = voice_catalog.voices_python()
    return [
        (registered and os.access(target, os.X_OK), "registered with Firefox", "run ./install.sh"),
        (os.path.exists(voices), f"voices environment ({voices})",
         "set it up as in the local-tts README"),
        (voice_catalog.espeak_paths() is not None, "espeak-ng (Kokoro)", "brew install espeak-ng"),
        (shutil.which("ffmpeg") is not None or os.path.exists("/opt/homebrew/bin/ffmpeg"),
         "ffmpeg (speed control for Phonon and Ming)", "brew install ffmpeg"),
    ]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "install"
    if command == "install":
        paths = install()
        print(f"Registered the helper with Firefox:\n  {paths['firefox']}\n  -> {paths['launcher']}")
        command = "status"
    elif command == "uninstall":
        for path in uninstall():
            print(f"removed {path}")
        return 0
    if command != "status":
        print(__doc__)
        return 2
    problems = 0
    for ok, what, fix in status():
        print(f"{'✓' if ok else '✗'} {what}" + ("" if ok else f"  -- {fix}"))
        problems += not ok
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
