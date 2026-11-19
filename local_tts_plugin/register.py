"""Tells the browsers where the host is: a start script, a native messaging
manifest naming it for each browser, and a copy of the extension for the
Chrome-family browsers to load.

    python -m local_tts_plugin.register install [--extension DIR]   (what install.sh runs)
    python -m local_tts_plugin.register uninstall
    python -m local_tts_plugin.register status

Exit status: 0 all set, 1 something to install still (see the list), 2 usage,
3 registering failed.
"""
import base64
import hashlib
import json
import os
import shlex
import shutil
import sys

HOST_NAME = "local_tts_plugin"
EXTENSION_ID = "local-tts@local-tts-plugin"   # Firefox
DESCRIPTION = "Local TTS: reads highlighted text with the voices on this computer"

# Chrome-family browsers let a host talk only to the extension IDs it names. An
# unpacked extension's ID comes from its folder unless its manifest carries a
# public key; this key pins it. Only the public half exists: nothing is ever
# packed or published with it.
CHROME_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA534PJQK37HMtX/H9ykvW2+r2lP+Sh/USdr9CaG0jnFaaea9y"
    "OFAvThdn12DRORnOD4xMSjdbdJUYreeTOAMRDrRQLrn+7MVJ4KLYC+rcBCo4Z0V4EQ5rVFZ4dqTU+TLldJzb1ABMAMjA"
    "34u8ctCRW4FbS6nhUM0zLfjThYpzBivi5geNTPOxSSzuE/8kuX7zNj0MpRbOhDKkTpSECg/1LXqxbfivxBBcyrBpVXGO"
    "Jyoo8MPolkiIMFUiRcaSPpG6CxcLFe71CNNrCJLY15a2wRzmp5l6Iu/C1KyXnzGuFgLM25acemyLQp+t6/eRo/tBMque"
    "FlEIFZ8/eFQIFJPZYQIDAQAB"
)


def chrome_extension_id(key_b64: str) -> str:
    """Chrome's rule: the first 128 bits of SHA-256 of the key, written with a-p."""
    digest = hashlib.sha256(base64.b64decode(key_b64)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


CHROME_EXTENSION_ID = chrome_extension_id(CHROME_KEY)

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


def extension_copy_path(home):
    return os.path.join(data_dir(home), "extension")


def firefox_manifest_path(home):
    if sys.platform == "darwin":
        folder = os.path.join(home, "Library", "Application Support", "Mozilla",
                              "NativeMessagingHosts")
    else:
        folder = os.path.join(home, ".mozilla", "native-messaging-hosts")
    return os.path.join(folder, HOST_NAME + ".json")


def chromium_profiles(home) -> dict:
    """Each Chrome-family browser's profile folder; its registrations live in
    NativeMessagingHosts/ inside it."""
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
        folders = {"Chrome": "Google/Chrome", "Brave": "BraveSoftware/Brave-Browser"}
    else:
        base = os.path.join(home, ".config")
        folders = {"Chrome": "google-chrome", "Brave": "BraveSoftware/Brave-Browser"}
    return {name: os.path.join(base, folder) for name, folder in folders.items()}


def chromium_manifest_path(profile):
    return os.path.join(profile, "NativeMessagingHosts", HOST_NAME + ".json")


def firefox_manifest(launcher) -> dict:
    return {"name": HOST_NAME, "description": DESCRIPTION, "path": launcher, "type": "stdio",
            "allowed_extensions": [EXTENSION_ID]}


def chromium_manifest(launcher) -> dict:
    return {"name": HOST_NAME, "description": DESCRIPTION, "path": launcher, "type": "stdio",
            "allowed_origins": [f"chrome-extension://{CHROME_EXTENSION_ID}/"]}


def _write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def copy_extension(source, dest) -> str:
    """The extension as Chrome and Brave load it: the same files with the key
    that pins its ID, and without Firefox's own manifest keys, which Chrome
    warns about. Kept outside the repo so it survives the repo moving."""
    if not os.path.isfile(os.path.join(source, "manifest.json")):
        raise FileNotFoundError(f"no extension at {source}")
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(".*"))
    path = os.path.join(dest, "manifest.json")
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest.pop("browser_specific_settings", None)
    manifest.get("background", {}).pop("scripts", None)
    manifest["key"] = CHROME_KEY
    _write(path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return dest


def install(python=None, home=None, extension=None) -> dict:
    """Register the host with Firefox and every Chrome-family browser that is
    installed; copy the extension when given its folder."""
    home = home or os.path.expanduser("~")
    python = python or sys.executable
    launcher = launcher_path(home)
    _write(launcher, LAUNCHER.format(python=shlex.quote(python)), mode=0o755)
    paths = {"launcher": launcher}

    firefox = firefox_manifest_path(home)
    _write(firefox, json.dumps(firefox_manifest(launcher), indent=2) + "\n")
    paths["Firefox"] = firefox

    for name, profile in chromium_profiles(home).items():
        if os.path.isdir(profile):
            path = chromium_manifest_path(profile)
            _write(path, json.dumps(chromium_manifest(launcher), indent=2) + "\n")
            paths[name] = path

    if extension:
        paths["extension"] = copy_extension(extension, extension_copy_path(home))
    return paths


def uninstall(home=None) -> list:
    home = home or os.path.expanduser("~")
    targets = [firefox_manifest_path(home), launcher_path(home)]
    targets += [chromium_manifest_path(p) for p in chromium_profiles(home).values()]
    removed = []
    for path in targets:
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    copy = extension_copy_path(home)
    if os.path.isdir(copy):
        shutil.rmtree(copy)
        removed.append(copy)
    return removed


def status(home=None) -> list:
    """(ok, what, fix) for each thing the extension needs."""
    from local_tts.voices import voice_catalog

    home = home or os.path.expanduser("~")

    def registered(path):
        if not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as f:
            return os.access(json.load(f).get("path", ""), os.X_OK)

    checks = [(registered(firefox_manifest_path(home)), "registered with Firefox", "run ./install.sh")]
    for name, profile in chromium_profiles(home).items():
        if os.path.isdir(profile):
            checks.append((registered(chromium_manifest_path(profile)), f"registered with {name}",
                           "run ./install.sh"))
    voices = voice_catalog.voices_python()
    checks += [
        (os.path.exists(voices), f"voices environment ({voices})",
         "set it up as in the local-tts README"),
        (voice_catalog.espeak_paths() is not None, "espeak-ng (Kokoro)", "brew install espeak-ng"),
        (shutil.which("ffmpeg") is not None or os.path.exists("/opt/homebrew/bin/ffmpeg"),
         "ffmpeg (speed control for Phonon and Ming)", "brew install ffmpeg"),
    ]
    return checks


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    command = argv.pop(0) if argv else "install"
    if command == "install":
        extension = None
        if argv[:1] == ["--extension"] and len(argv) > 1:
            extension = argv[1]
        try:
            paths = install(extension=extension)
        except Exception as exc:
            print(f"Registering the helper failed: {exc}", file=sys.stderr)
            return 3
        print("Registered the helper:")
        for name, path in paths.items():
            if name not in ("launcher", "extension"):
                print(f"  {name}: {path}")
        if "extension" in paths:
            print(f"Extension for Chrome and Brave: {paths['extension']}")
        print()
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
