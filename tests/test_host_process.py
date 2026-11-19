"""The host as Firefox runs it: a process speaking native messaging on its
stdin/stdout, with the real voice worker behind it (on its test-tone voice)."""
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

from local_tts_plugin.protocol import ProtocolError, encode, read_message

CHATTY = """
import os
import local_tts_plugin.host as host
real_init = host.Session.__init__
def chatty_init(self, *a, **kw):
    print("progress 50%")               # Python-level chatter
    os.write(1, b"C-level chatter")      # and from C, with no newline
    real_init(self, *a, **kw)
host.Session.__init__ = chatty_init
host.main()
"""


class Host:
    def __init__(self, tmp_path, args=None, **env):
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        full_env = dict(os.environ, HOME=str(self.home), LOCAL_TTS_FAKE_VOICES="1",
                        LOCAL_TTS_VOICES_PYTHON=sys.executable,
                        LOCAL_TTS_PLUGIN_FAKE_PLAYER="0.1")
        full_env.update({k: str(v) for k, v in env.items()})
        self.proc = subprocess.Popen(
            args or [sys.executable, "-m", "local_tts_plugin.host"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=full_env)
        self.messages = queue.Queue()
        self.garbage = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                message = read_message(self.proc.stdout)
                if message is None:
                    break
                self.messages.put(message)
        except ProtocolError as exc:
            self.garbage.append(str(exc))
        self.messages.put(None)

    def send(self, message):
        self.proc.stdin.write(encode(message))
        self.proc.stdin.flush()

    def expect(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        seen = []
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=deadline - time.monotonic())
            except queue.Empty:
                break
            if message is None:
                break
            seen.append(message)
            if predicate(message):
                return message
        raise AssertionError(f"not seen; got {seen}, garbage {self.garbage}")

    def children(self):
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
        return [int(p) for p, pp in (line.split() for line in out.splitlines())
                if int(pp) == self.proc.pid]

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)

    @property
    def log(self):
        path = self.home / ".local" / "share" / "local-tts-plugin" / "host.log"
        return path.read_text() if path.exists() else ""


@pytest.fixture
def start(tmp_path):
    hosts = []

    def run(**kw):
        h = Host(tmp_path, **kw)
        hosts.append(h)
        return h

    yield run
    for h in hosts:
        h.close()


def is_type(kind):
    return lambda m: m.get("type") == kind


def test_hello_gets_the_catalog(start):
    host = start()
    host.send({"type": "hello"})
    catalog = host.expect(is_type("catalog"))
    assert catalog["defaultEngine"] == "fake"
    assert {"kokoro", "phonon", "ming"} <= {e["key"] for e in catalog["engines"]}


def test_a_reading_plays_through_the_real_worker(start):
    host = start()
    host.send({"type": "play", "text": "One here. Two here. Three here.", "readingId": 9})
    reading = host.expect(is_type("reading"))
    assert len(reading["sentences"]) == 3
    indexes = []
    host.expect(lambda m: (m.get("type") == "sentence" and indexes.append(m["index"]) and False)
                or m.get("state") == "finished")
    assert indexes == [0, 1, 2]
    assert host.log.count("played ") == 3
    assert not host.garbage


def test_chatter_on_stdout_never_reaches_the_browser(start, tmp_path):
    host = start(args=[sys.executable, "-c", CHATTY])
    host.send({"type": "hello"})
    host.expect(is_type("catalog"))
    host.send({"type": "play", "text": "Still clean.", "readingId": 1})
    host.expect(lambda m: m.get("state") == "finished")
    assert not host.garbage
    assert "C-level chatter" in host.log


def test_the_host_and_its_worker_exit_when_the_browser_hangs_up(start):
    host = start()
    host.send({"type": "play", "text": "Load the worker.", "readingId": 1})
    host.expect(lambda m: m.get("state") == "finished")
    workers = host.children()
    assert workers, "the voice worker should be running"
    host.proc.stdin.close()
    assert host.proc.wait(10) == 0
    time.sleep(0.5)
    for pid in workers:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_the_host_exits_after_idling(start):
    host = start(LOCAL_TTS_PLUGIN_IDLE_S=1)
    host.send({"type": "hello"})
    host.expect(is_type("catalog"))
    assert host.expect(is_type("bye"), timeout=10)["reason"] == "idle"
    assert host.proc.wait(10) == 0


def test_heartbeats_keep_coming(start):
    host = start(LOCAL_TTS_PLUGIN_HEARTBEAT_S=0.2)
    host.expect(is_type("heartbeat"), timeout=5)
    host.expect(is_type("heartbeat"), timeout=5)


def test_a_garbled_message_ends_the_host_cleanly(start):
    host = start()
    host.proc.stdin.write(b"\x05\x00\x00\x00{oops")
    host.proc.stdin.flush()
    assert host.proc.wait(10) == 0
    assert "unreadable message" in host.log
