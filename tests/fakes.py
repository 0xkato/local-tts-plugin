"""Stand-ins for the voice worker, the models and the speaker."""
import os
import tempfile
import threading
import time

from local_tts.voices.voice_catalog import EngineSpec


def spec(key, name, voices=(("v1", "Voice one"), ("v2", "Voice two")), **kw):
    return EngineSpec(key=key, name=name, weight="light", detail="test", backend="fake",
                      repo=f"test/{key}", voices=voices, default_voice=voices[0][0],
                      native_speed=True, **kw)


class FakeClient:
    """The worker client's surface that the session uses."""

    def __init__(self):
        self.active = []
        self.loads = []
        self.closed = False

    def set_active(self, key):
        self.active.append(key)

    def load(self, key):
        self.loads.append(key)
        return 0.1

    def close(self):
        self.closed = True


class FakeEngine:
    """Writes an empty clip per sentence and remembers what it was asked."""

    def __init__(self, spec, client, reason="", fail_on=None, delay=0.0, downloaded=True):
        self.spec = spec
        self.client = client
        self.reason = reason
        self.fail_on = fail_on
        self.delay = delay
        self.downloaded = downloaded
        self.calls = []
        self._lock = threading.Lock()

    def generate(self, text, speed, voice=None, **kwargs):
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.calls.append({"text": text, "speed": speed, "voice": voice})
        if self.fail_on and self.fail_on in text:
            raise RuntimeError(f"could not say {text!r}")
        fd, path = tempfile.mkstemp(prefix=f"{self.spec.key}-", suffix=".wav")
        os.close(fd)
        return path

    def said(self):
        with self._lock:
            return [c["text"] for c in self.calls]

    def unavailable_reason(self):
        return self.reason

    def is_available(self):
        return not self.reason

    def is_downloaded(self):
        return self.downloaded

    def supports_speed(self):
        return True

    def preload(self):
        return self.client.load(self.spec.key)

    def get_name(self):
        return self.spec.name

    def stop(self):
        pass


class FakePlayer:
    """Each clip 'plays' for a fixed time."""

    def __init__(self, seconds=0.15):
        self.seconds = seconds
        self.played = []
        self._until = 0.0
        self._lock = threading.Lock()

    def play(self, path):
        with self._lock:
            self.played.append(os.path.basename(path))
            self._until = time.monotonic() + self.seconds

    def is_busy(self):
        with self._lock:
            return time.monotonic() < self._until

    def stop(self):
        with self._lock:
            self._until = 0.0


class Events:
    """Collects what the session sends, and waits for what a test expects."""

    def __init__(self):
        self.items = []
        self._cond = threading.Condition()

    def __call__(self, message):
        with self._cond:
            self.items.append(message)
            self._cond.notify_all()

    def of(self, kind):
        with self._cond:
            return [m for m in self.items if m.get("type") == kind]

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for m in self.items:
                    if predicate(m):
                        return m
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out; got {self.items}")
                self._cond.wait(remaining)

    def wait_count(self, kind, n, timeout=5.0):
        deadline = time.monotonic() + timeout
        with self._cond:
            while len([m for m in self.items if m.get("type") == kind]) < n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out waiting for {n} {kind}; got {self.items}")
                self._cond.wait(remaining)

    def wait_state(self, state, timeout=5.0):
        return self.wait_for(lambda m: m.get("type") == "state" and m.get("state") == state,
                             timeout)

    def clear(self):
        with self._cond:
            self.items.clear()
