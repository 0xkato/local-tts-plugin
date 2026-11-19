"""Registering the host with Firefox."""
import json
import os
import re
import subprocess
import sys

import pytest

from local_tts_plugin import register
from local_tts_plugin.protocol import encode, read_message

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_install_writes_a_manifest_pointing_at_an_executable_launcher(tmp_path):
    paths = register.install(python="/opt/env with space/bin/python", home=str(tmp_path))
    manifest = json.loads(open(paths["firefox"]).read())
    assert manifest["name"] == register.HOST_NAME
    assert manifest["type"] == "stdio"
    assert manifest["path"] == paths["launcher"] and os.path.isabs(manifest["path"])
    assert manifest["allowed_extensions"] == [register.EXTENSION_ID]
    assert os.access(paths["launcher"], os.X_OK)
    assert "'/opt/env with space/bin/python' -m local_tts_plugin.host" in open(paths["launcher"]).read()


def test_uninstall_removes_both(tmp_path):
    paths = register.install(home=str(tmp_path))
    assert sorted(register.uninstall(home=str(tmp_path))) == sorted(paths.values())
    assert register.uninstall(home=str(tmp_path)) == []


def test_the_names_match_the_extension():
    manifest = json.load(open(os.path.join(ROOT, "extension", "manifest.json")))
    assert manifest["browser_specific_settings"]["gecko"]["id"] == register.EXTENSION_ID
    background = open(os.path.join(ROOT, "extension", "background.js")).read()
    assert re.search(r'HOST\s*=\s*"%s"' % register.HOST_NAME, background)


@pytest.mark.skipif(not os.path.exists("/opt/homebrew/bin/ffmpeg"), reason="needs Homebrew ffmpeg")
def test_the_launcher_finds_homebrew_with_a_bare_path(tmp_path):
    """Firefox opened from the Dock hands the host /usr/bin:/bin only."""
    paths = register.install(home=str(tmp_path))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LOCAL_TTS_FAKE_VOICES": "1",
           "LOCAL_TTS_VOICES_PYTHON": sys.executable}
    proc = subprocess.Popen([paths["launcher"], paths["firefox"], register.EXTENSION_ID],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    try:
        proc.stdin.write(encode({"type": "hello"}))
        proc.stdin.flush()
        catalog = read_message(proc.stdout)
        phonon = next(e for e in catalog["engines"] if e["key"] == "phonon")
        assert phonon["speedControl"], "ffmpeg was not found through the launcher"
    finally:
        proc.stdin.close()
        proc.wait(10)
