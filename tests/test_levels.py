"""The loudness the page's bars show: measured from each clip as it plays."""
import math
import struct
import wave


from local_tts_plugin.host import FRAME_S, Session, clip_levels
from tests.fakes import Events, FakeClient, FakeEngine, FakePlayer, spec

RATE = 24000


def write_clip(path, parts):
    """parts: (seconds, amplitude 0-1) pieces of a 220 Hz tone."""
    samples = []
    for seconds, amplitude in parts:
        for i in range(int(seconds * RATE)):
            samples.append(int(amplitude * 32767 * math.sin(2 * math.pi * 220 * i / RATE)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return str(path)


def test_silence_reads_low_and_speech_reads_high(tmp_path):
    clip = write_clip(tmp_path / "a.wav", [(0.4, 0.0), (0.4, 0.6), (0.4, 0.0)])
    levels = clip_levels(clip)
    per_part = int(0.4 / FRAME_S)
    assert len(levels) == 3 * per_part
    assert max(levels[:per_part - 1]) == 0
    assert min(levels[per_part + 1:2 * per_part - 1]) >= 90
    assert max(levels[2 * per_part + 1:]) == 0


def test_quieter_stretches_read_lower(tmp_path):
    clip = write_clip(tmp_path / "b.wav", [(0.4, 0.8), (0.4, 0.1)])
    levels = clip_levels(clip)
    loud, quiet = levels[2:8], levels[12:18]
    assert min(loud) > max(quiet) > 0


def test_a_file_that_is_not_audio_gives_nothing(tmp_path):
    bogus = tmp_path / "bogus.wav"
    bogus.write_bytes(b"RIFF")
    assert clip_levels(str(bogus)) == []
    assert clip_levels(str(tmp_path / "missing.wav")) == []


class ToneEngine(FakeEngine):
    def generate(self, text, speed, voice=None, **kwargs):
        path = super().generate(text, speed, voice=voice)
        return write_clip(path, [(0.1, 0.0), (0.2, 0.5)])


def test_each_clip_sends_its_levels_as_it_starts():
    client = FakeClient()
    engines = {"alpha": ToneEngine(spec("alpha", "Alpha"), client)}
    events = Events()
    session = Session(events, engines, client, player=FakePlayer(seconds=0.1))
    try:
        session.handle({"type": "play", "text": "One. Two.", "readingId": 4})
        events.wait_state("finished")
    finally:
        session.close()
    kinds = [m["type"] for m in events.items if m["type"] in ("sentence", "levels")]
    assert kinds == ["sentence", "levels", "sentence", "levels"]
    levels = events.of("levels")
    assert [m["index"] for m in levels] == [0, 1]
    assert all(m["readingId"] == 4 and m["frame"] == FRAME_S for m in levels)
    assert levels[0]["levels"][0] == 0 and max(levels[0]["levels"]) == 100
