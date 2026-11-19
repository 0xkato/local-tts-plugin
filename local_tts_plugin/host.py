"""The helper program the browser extension talks to.

Firefox starts it when the extension connects and speaks native messaging to
it over stdin/stdout (see protocol.py). The audio plays here, through the
same PlaybackController the desktop app uses; the page only shows which
sentence is being read.

Extension -> host:
    {"type": "hello"}
    {"type": "play", "text": "...", "readingId": 3, "engine": "kokoro",
     "voice": "af_heart", "speed": 1.5, "startIndex": 0}   all but text optional
        instead of "voice", "voices" may give the chosen voice of every engine:
        {"kokoro": "af_bella", "phonon": "Hugh"} -- whichever engine reads
    {"type": "pause" | "resume" | "toggle" | "next" | "prev" | "stop"}
    {"type": "speed", "value": 1.7}
    {"type": "voice", "engine": "kokoro", "voice": "af_bella"}
    {"type": "preload", "engine": "kokoro"}

Host -> extension:
    {"type": "catalog", "engines": [...], "defaultEngine": "kokoro", "version": "..."}
    {"type": "reading", "readingId": 3, "sentences": [[start, end], ...]}
        offsets in UTF-16 code units, as JavaScript indexes the text it sent
    {"type": "sentence", "readingId": 3, "index": 0, "total": 12, ...}
    {"type": "state", "readingId": 3, "state": "playing", "index": 0, "total": 12}
        states: loading, playing, paused, stopped, finished, error (with "message")
    {"type": "levels", "readingId": 3, "index": 0, "frame": 0.04, "levels": [0, 37, 88, ...]}
        how loud the clip now starting is, 0-100 per frame, for the page's meter
    {"type": "ready", "engine": "kokoro"}              a preload finished
    {"type": "error", "message": "..."}                a command could not run
    {"type": "heartbeat"}                              Firefox suspends a quiet extension
    {"type": "bye", "reason": "idle"}                  exiting to free the model
"""
import array
import logging
import math
import os
import queue
import sys
import threading
import time
import wave

from local_tts.engine import (
    PYGAME_AVAILABLE,
    AudioPlayer,
    NeuralTTSEngine,
    PlaybackController,
    VoiceSwitched,
    VoiceWorkerClient,
)
from local_tts.voices import voice_catalog
from local_tts.voices.voice_catalog import EngineSpec

from . import __version__
from .protocol import ProtocolError, encode, read_message

DATA_DIR = os.path.expanduser("~/.local/share/local-tts-plugin")
LOG_PATH = os.path.join(DATA_DIR, "host.log")
WORKER_LOG = os.path.join(DATA_DIR, "voices-worker.log")

# With nothing playing for this long the host exits, taking the model out of
# memory with it; the next play starts it again.
IDLE_EXIT_S = float(os.environ.get("LOCAL_TTS_PLUGIN_IDLE_S", "300"))
HEARTBEAT_S = float(os.environ.get("LOCAL_TTS_PLUGIN_HEARTBEAT_S", "15"))

SPEED_MIN, SPEED_MAX, DEFAULT_SPEED = 0.5, 2.0, 1.5

# The page's meter: one loudness reading per 40 ms of audio, over a 36 dB range
FRAME_S = 0.04
METER_RANGE_DB = 36.0

State = PlaybackController.State
log = logging.getLogger("local_tts_plugin")


def clamp_speed(value) -> float:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SPEED
    return round(min(SPEED_MAX, max(SPEED_MIN, speed)), 2)


def utf16_positions(text: str, positions) -> list:
    """Sentence spans as JavaScript counts them: characters outside the Basic
    Multilingual Plane (most emoji) are two UTF-16 units there, one here."""
    if all(ord(ch) <= 0xFFFF for ch in text):
        return [[start, end] for start, end in positions]
    prefix = [0]
    for ch in text:
        prefix.append(prefix[-1] + (2 if ord(ch) > 0xFFFF else 1))
    return [[prefix[start], prefix[end]] for start, end in positions]


def clip_levels(path, frame_s=FRAME_S) -> list:
    """How loud a WAV clip is, 0-100 for each frame, relative to its own peak.
    The audio plays here, not in the page, so the page cannot listen to it:
    this is what its meter shows instead. [] if the file is not 16-bit PCM."""
    try:
        with wave.open(path, "rb") as clip:
            rate, width, channels = clip.getframerate(), clip.getsampwidth(), clip.getnchannels()
            raw = clip.readframes(clip.getnframes())
    except (wave.Error, OSError, EOFError):
        return []
    if width != 2 or not raw:
        return []
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    step = max(1, int(rate * frame_s)) * channels
    rms = []
    for start in range(0, len(samples), step):
        # every 7th sample is plenty for a meter; odd, so stereo counts both channels
        chunk = samples[start:start + step:7]
        rms.append(math.sqrt(sum(x * x for x in chunk) / len(chunk)) if chunk else 0.0)
    peak = max(rms, default=0.0)
    if peak <= 0:
        return [0] * len(rms)
    levels = []
    for value in rms:
        db = 20 * math.log10(max(value, 1e-9) / peak)
        levels.append(round(min(1.0, max(0.0, (db + METER_RANGE_DB) / METER_RANGE_DB)) * 100))
    return levels


class _Metered:
    """The speaker, reporting each clip's loudness the moment it starts."""

    def __init__(self, player, on_play):
        self._player = player
        self._on_play = on_play

    def play(self, path):
        self._on_play(path)
        self._player.play(path)

    def is_busy(self):
        return self._player.is_busy()

    def stop(self):
        self._player.stop()


class Session:
    """The reading in progress: its text, voice and speed, and the controller
    playing it. Commands arrive on the reader thread; the controller's hooks
    fire on its playback thread; both only ever send, never wait on the other."""

    RESTART_DELAY = 0.35   # a burst of speed clicks restarts the sentence once

    def __init__(self, send, engines, client, player=None, clock=time.monotonic):
        self._send = send
        self.engines = dict(engines)
        self.client = client
        self.clock = clock
        metered = _Metered(player, self._on_clip) if player is not None else None
        self.controller = PlaybackController(engine=None, session_store=None, player=metered)
        c = self.controller
        c.params_provider = self._params
        c.on_sentence_start = self._on_sentence
        c.on_generating = self._on_generating
        c.on_finished = self._on_finished
        c.on_error = self._on_error

        self.reading_id = None
        self.engine_key = self.default_engine()
        self.voice = self.engines[self.engine_key].spec.default_voice if self.engine_key else None
        self.speed = DEFAULT_SPEED
        self._ready = set()          # engines the worker has loaded
        self._preloading = 0         # a first download can take many minutes
        self._lock = threading.RLock()
        self._restart = None
        self._last_active = clock()

    # ── what the host asks of it ────────────────────────────────────────

    def handle(self, message) -> None:
        kind = message.get("type") if isinstance(message, dict) else None
        command = None
        if isinstance(kind, str) and kind.isidentifier():
            command = getattr(self, f"_cmd_{kind}", None)
        if command is None:
            self._send({"type": "error", "message": f"unknown command {kind!r}"})
            return
        with self._lock:
            self._touch()
            try:
                command(message)
            except Exception as exc:
                log.exception("%s failed", kind)
                if kind == "play":
                    # the page waiting on this reading needs to hear it failed
                    self._send({"type": "state", "readingId": message.get("readingId"),
                                "state": "error", "message": f"Could not start reading: {exc}"})
                else:
                    self._send({"type": "error", "message": f"{kind} failed: {exc}"})

    def idle_seconds(self) -> float:
        if self.controller.state == State.PLAYING or self._preloading:
            return 0.0
        return max(0.0, self.clock() - self._last_active)

    def default_engine(self):
        for key, engine in self.engines.items():
            if not engine.unavailable_reason():
                return key
        return next(iter(self.engines), None)

    def catalog(self) -> dict:
        engines = []
        for key, engine in self.engines.items():
            spec = engine.spec
            reason = engine.unavailable_reason()
            engines.append({
                "key": key, "name": spec.name, "weight": spec.weight, "detail": spec.detail,
                "voices": [{"id": voice, "label": label} for voice, label in spec.voices],
                "defaultVoice": spec.default_voice,
                "available": not reason, "reason": reason,
                "downloaded": engine.is_downloaded(), "downloadSize": spec.download_size,
                "speedControl": engine.supports_speed(),
            })
        return {"type": "catalog", "version": __version__, "engines": engines,
                "defaultEngine": self.default_engine()}

    def close(self) -> None:
        with self._lock:
            self._cancel_restart()
            self.controller.state = State.STOPPED
            self.controller.cleanup()
        self.client.close()

    # ── commands ────────────────────────────────────────────────────────

    def _cmd_hello(self, message):
        self._send(self.catalog())

    def _cmd_play(self, message):
        text = message.get("text")
        if not isinstance(text, str):
            raise ValueError("play needs the text to read")
        self._cancel_restart()
        c = self.controller
        c.state = State.STOPPED
        c.cancel()
        self.reading_id = message.get("readingId")
        if "speed" in message:
            self.speed = clamp_speed(message["speed"])
        engine, _ = self._select(message.get("engine"), message.get("voice"),
                                 message.get("voices"))
        c.engine = engine
        c.load_text(text)
        if c.total_sentences:
            c.seek_to(int(message.get("startIndex") or 0))
        self._send(dict({"type": "reading", "readingId": self.reading_id,
                         "sentences": utf16_positions(text, c.positions)}, **self._names()))
        reason = engine.unavailable_reason()
        if reason:
            self._state("error", message=f"{engine.get_name()} can't run here: {reason}")
        elif c.total_sentences == 0:
            self._state("finished")
        else:
            self._start()

    def _cmd_pause(self, message):
        c = self.controller
        if c.state == State.PLAYING:
            self._cancel_restart()
            c.state = State.PAUSED
            c.cancel()
            self._state("paused")

    def _cmd_resume(self, message):
        if self.controller.state == State.PAUSED:
            self._start()

    def _cmd_toggle(self, message):
        if self.controller.state == State.PLAYING:
            self._cmd_pause(message)
        else:
            self._cmd_resume(message)

    def _cmd_next(self, message):
        self._skip(self.controller.skip_next, self.controller.next_sentence)

    def _cmd_prev(self, message):
        self._skip(self.controller.skip_prev, self.controller.prev_sentence)

    def _cmd_stop(self, message):
        self._cancel_restart()
        c = self.controller
        c.state = State.STOPPED
        c.cancel()
        c.cleanup_buffer()
        self._state("stopped")

    def _cmd_speed(self, message):
        self.speed = clamp_speed(message.get("value"))
        if self.controller.state == State.PLAYING:
            self._restart_soon()

    def _cmd_voice(self, message):
        before = self.voice
        engine, changed = self._select(message.get("engine"), message.get("voice"),
                                       message.get("voices"))
        if not changed and self.voice == before:
            return
        c = self.controller
        if changed:
            # Clips in the old voice must not be replayed in the new one's place
            c.cancel()
            c.cleanup_buffer()
            c.engine = engine
        if c.state == State.PLAYING:
            self._restart_soon()
        elif changed and not engine.unavailable_reason():
            self._preload(self.engine_key)

    def _cmd_preload(self, message):
        # During a reading its engine stays: switching is what "voice" is for
        reading = self.controller.state in (State.PLAYING, State.PAUSED)
        if message.get("engine") is not None and not reading:
            self._select(message.get("engine"), None, message.get("voices"))
        self._preload(self.engine_key)

    # ── internals ───────────────────────────────────────────────────────

    def _select(self, key, voice, chosen=None):
        """Make an engine and voice current; unknown ones fall back to the
        defaults. `chosen` maps engines to the voice picked for each."""
        if key is None:
            key = self.engine_key
        if key not in self.engines:
            key = self.default_engine()
        voices = dict(self.engines[key].spec.voices)
        if voice is None and isinstance(chosen, dict):
            voice = chosen.get(key)
        if voice is None and key == self.engine_key and self.voice in voices:
            voice = self.voice            # not asked to change it
        elif voice not in voices:
            voice = self.engines[key].spec.default_voice
        changed = key != self.engine_key
        self.engine_key, self.voice = key, voice
        if changed:
            self._ready.clear()       # the worker holds one model at a time
        self.client.set_active(key)
        return self.engines[key], changed

    def _start(self):
        self._cancel_restart()
        c = self.controller
        c.engine = self.engines[self.engine_key]
        c.state = State.PLAYING
        self._state("playing")
        c.start_playback_thread()

    def _skip(self, while_playing, otherwise):
        c = self.controller
        if c.total_sentences == 0:
            return
        if c.state == State.PLAYING:
            self._cancel_restart()
            while_playing()
        elif c.state == State.PAUSED:
            otherwise()
            self._sentence_event(c.current_index, paused=True)

    def _restart_soon(self):
        """Hear a new speed or voice on the sentence being read now, once the
        clicks have settled, rather than three buffered sentences later."""
        self._cancel_restart()
        timer = threading.Timer(self.RESTART_DELAY, lambda: self._restart_now(timer))
        timer.daemon = True
        self._restart = timer
        timer.start()

    def _restart_now(self, timer):
        with self._lock:
            if self._restart is not timer:
                return
            self._restart = None
            if self.controller.state == State.PLAYING:
                self.controller.cancel()
                self._start()

    def _cancel_restart(self):
        if self._restart is not None:
            self._restart.cancel()
            self._restart = None

    def _preload(self, key):
        engine = self.engines[key]
        reason = engine.unavailable_reason()
        if reason:
            self._send({"type": "error", "engine": key,
                        "message": f"{engine.get_name()} can't run here: {reason}"})
            return
        if key in self._ready:
            self._send({"type": "ready", "engine": key})
            return

        def work():
            try:
                engine.preload()
            except VoiceSwitched:
                return
            except Exception as exc:
                log.warning("preloading %s failed: %s", key, exc)
                self._send({"type": "error", "engine": key,
                            "message": f"{engine.get_name()} failed to load: {exc}"})
                return
            finally:
                with self._lock:
                    self._preloading -= 1
                    self._touch()
            if key == self.engine_key:
                self._ready.add(key)
            self._send({"type": "ready", "engine": key})

        self._preloading += 1
        threading.Thread(target=work, daemon=True).start()

    def _params(self):
        return self.speed, {"voice": self.voice}

    def _touch(self):
        self._last_active = self.clock()

    def _state(self, state, **extra):
        c = self.controller
        self._send(dict({"type": "state", "readingId": self.reading_id, "state": state,
                         "index": c.current_index, "total": c.total_sentences}, **extra))

    def _names(self):
        spec = self.engines[self.engine_key].spec
        return {"engine": self.engine_key, "voice": self.voice,
                "engineName": spec.name, "voiceLabel": spec.voice_label(self.voice)}

    def _sentence_event(self, index, paused=False):
        message = dict({"type": "sentence", "readingId": self.reading_id, "index": index,
                        "total": self.controller.total_sentences, "speed": self.speed},
                       **self._names())
        if paused:
            message["paused"] = True
        self._send(message)

    # ── controller hooks (playback thread) ──────────────────────────────

    def _on_sentence(self, index):
        self._ready.add(self.engine_key)
        self._touch()
        self._sentence_event(index)

    def _on_clip(self, path):
        self._send({"type": "levels", "readingId": self.reading_id,
                    "index": self.controller.current_index, "frame": FRAME_S,
                    "levels": clip_levels(path)})

    def _on_generating(self, index):
        if self.engine_key in self._ready:
            self._state("loading", message=None)
            return
        engine = self.engines[self.engine_key]
        note = ""
        if not engine.is_downloaded():
            size = engine.spec.download_size
            note = f" (downloading {size})" if size else " (downloading)"
        self._state("loading", message=f"Loading {engine.get_name()}…{note}")

    def _on_finished(self):
        self._touch()
        self._state("finished")

    def _on_error(self, exc):
        self._touch()
        log.warning("reading failed: %s", exc)
        self._state("error", message=str(exc))


# ── the process ─────────────────────────────────────────────────────────────

FAKE_SPEC = EngineSpec(
    key="fake", name="Test tone", weight="light", detail="test suite", backend="fake",
    repo="local-tts/fake", voices=(("tone", "Tone"),), default_voice="tone", native_speed=True,
)


class Outbox:
    """Writes messages to the browser from a thread of its own, so nothing that
    sends -- the playback thread runs its hooks inside the controller's lock --
    ever waits on the pipe. Messages leave in the order they were sent."""

    def __init__(self, stream):
        self._stream = stream
        self._queue = queue.SimpleQueue()
        self.closed = False
        threading.Thread(target=self._write_all, daemon=True).start()

    def __call__(self, message):
        if self.closed:
            return
        try:
            frame = encode(message)
        except ProtocolError as exc:
            log.error("dropped a message: %s", exc)
            return
        self._queue.put(frame)

    def drain(self, timeout=2.0) -> bool:
        """Wait until everything sent so far has been written."""
        written = threading.Event()
        self._queue.put(written)
        return written.wait(timeout) or self.closed

    def _write_all(self):
        while True:
            item = self._queue.get()
            if isinstance(item, threading.Event):
                item.set()
                continue
            if self.closed:
                continue
            try:
                self._stream.write(item)
                self._stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.closed = True


class MixerPlayer(AudioPlayer):
    """pygame's mixer, opened on first use: answering the toolbar panel
    should not claim the audio device."""

    def play(self, path):
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        super().play(path)


class SilentPlayer:
    """For the test suite: each clip 'plays' for a fixed time, in silence."""

    def __init__(self, seconds):
        self.seconds = seconds
        self._until = 0.0
        self._lock = threading.Lock()

    def play(self, path):
        log.info("played %s", os.path.basename(path))
        with self._lock:
            self._until = time.monotonic() + self.seconds

    def is_busy(self):
        with self._lock:
            return time.monotonic() < self._until

    def stop(self):
        with self._lock:
            self._until = 0.0


def claim_stdout():
    """Keep stdout for messages. Everything else that writes there -- pygame,
    the ML libraries, C code -- goes to the log instead: one stray byte and
    the browser drops the connection."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
        os.replace(LOG_PATH, LOG_PATH + ".old")
    channel = os.fdopen(os.dup(1), "wb")
    log_fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 2)
    os.dup2(log_fd, 1)
    os.close(log_fd)
    sys.stdout = sys.stderr = os.fdopen(os.dup(2), "w", buffering=1)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    return channel


def build_engines():
    client = VoiceWorkerClient(log_path=WORKER_LOG)
    specs = list(voice_catalog.ENGINES)
    if os.environ.get("LOCAL_TTS_FAKE_VOICES"):
        specs.insert(0, FAKE_SPEC)
    return {spec.key: NeuralTTSEngine(spec, client) for spec in specs}, client


def make_player():
    if os.environ.get("LOCAL_TTS_PLUGIN_FAKE_PLAYER"):
        return SilentPlayer(float(os.environ["LOCAL_TTS_PLUGIN_FAKE_PLAYER"]))
    return MixerPlayer() if PYGAME_AVAILABLE else None


def main():
    send = Outbox(claim_stdout())
    log.info("host %s starting (pid %d)", __version__, os.getpid())
    engines, client = build_engines()
    session = Session(send, engines, client, player=make_player())
    finished = threading.Event()

    # Unbuffered: this thread is still blocked in a read when the host exits
    # on its own (idle), and a buffered stdin's lock aborts the shutdown.
    stdin = os.fdopen(os.dup(0), "rb", buffering=0)

    def read_commands():
        try:
            while True:
                message = read_message(stdin)
                if message is None:
                    log.info("the browser closed the connection")
                    break
                session.handle(message)
        except ProtocolError as exc:
            log.error("unreadable message from the browser: %s", exc)
        finally:
            finished.set()

    threading.Thread(target=read_commands, daemon=True).start()
    tick = max(0.05, min(0.5, HEARTBEAT_S / 2, IDLE_EXIT_S / 4))
    last_beat = time.monotonic()
    try:
        while not finished.wait(tick):
            if send.closed:
                break
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_S:
                send({"type": "heartbeat"})
                last_beat = now
            if session.idle_seconds() >= IDLE_EXIT_S:
                log.info("idle for %.0fs, exiting", IDLE_EXIT_S)
                send({"type": "bye", "reason": "idle"})
                break
    finally:
        session.close()
        send.drain()      # "bye" and anything else still queued
    log.info("host exiting")


if __name__ == "__main__":
    main()
