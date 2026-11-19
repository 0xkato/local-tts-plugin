"""The host's reading session: commands from the extension in, events out."""
import time

import pytest

from local_tts_plugin.host import Session, utf16_positions
from tests.fakes import Events, FakeClient, FakeEngine, FakePlayer, spec

TEXT = "First one. Second one. Third one."


@pytest.fixture
def make():
    sessions = []

    def build(engines=None, player=None, **engine_kw):
        client = FakeClient()
        if engines is None:
            engines = {
                "alpha": FakeEngine(spec("alpha", "Alpha"), client, **engine_kw),
                "beta": FakeEngine(spec("beta", "Beta"), client),
            }
        else:
            for e in engines.values():
                e.client = client
        events = Events()
        session = Session(events, engines, client, player=player or FakePlayer())
        session.RESTART_DELAY = 0.05
        sessions.append(session)
        return session, events, engines, client

    yield build
    for s in sessions:
        s.close()


def sentences_heard(events, reading_id):
    return [m["index"] for m in events.of("sentence") if m["readingId"] == reading_id]


# ── catalog ──────────────────────────────────────────────────────────────

def test_hello_lists_the_engines_and_their_voices(make):
    session, events, _, _ = make()
    session.handle({"type": "hello"})
    catalog = events.wait_for(lambda m: m["type"] == "catalog")
    assert [e["key"] for e in catalog["engines"]] == ["alpha", "beta"]
    alpha = catalog["engines"][0]
    assert alpha["voices"] == [{"id": "v1", "label": "Voice one"}, {"id": "v2", "label": "Voice two"}]
    assert alpha["defaultVoice"] == "v1" and alpha["available"] and alpha["reason"] == ""
    assert catalog["defaultEngine"] == "alpha"


def test_the_default_engine_is_the_first_that_can_run(make):
    client = FakeClient()
    engines = {"alpha": FakeEngine(spec("alpha", "Alpha"), client, reason="needs a Mac"),
               "beta": FakeEngine(spec("beta", "Beta"), client)}
    session, events, _, _ = make(engines=engines)
    session.handle({"type": "hello"})
    catalog = events.wait_for(lambda m: m["type"] == "catalog")
    assert catalog["defaultEngine"] == "beta"
    assert catalog["engines"][0]["reason"] == "needs a Mac"


# ── reading ──────────────────────────────────────────────────────────────

def test_play_reads_every_sentence_and_says_where_each_one_is(make):
    session, events, engines, client = make()
    session.handle({"type": "play", "text": TEXT, "readingId": 7, "speed": 1.5})
    reading = events.wait_for(lambda m: m["type"] == "reading")
    assert reading["readingId"] == 7
    assert [TEXT[s:e] for s, e in reading["sentences"]] == ["First one.", "Second one.", "Third one."]
    done = events.wait_state("finished")
    assert done["readingId"] == 7
    assert sentences_heard(events, 7) == [0, 1, 2]
    assert engines["alpha"].said() == ["First one.", "Second one.", "Third one."]
    assert {c["speed"] for c in engines["alpha"].calls} == {1.5}
    assert client.active[-1] == "alpha"


def test_offsets_are_in_utf16_units_like_javascript_strings(make):
    """An emoji is one character to Python and two to the page."""
    text = "Hi 😀 there. Next one."
    session, events, _, _ = make()
    session.handle({"type": "play", "text": text, "readingId": 1})
    reading = events.wait_for(lambda m: m["type"] == "reading")
    js = text.encode("utf-16-le")
    spans = [js[2 * s:2 * e].decode("utf-16-le") for s, e in reading["sentences"]]
    assert spans == ["Hi 😀 there.", "Next one."]


def test_utf16_positions_leave_plain_text_alone():
    assert utf16_positions("abc. def.", [(0, 4), (5, 9)]) == [[0, 4], [5, 9]]


def test_the_chosen_voice_is_used_and_an_unknown_one_falls_back(make):
    session, events, engines, _ = make()
    session.handle({"type": "play", "text": "One.", "readingId": 1, "engine": "beta", "voice": "v2"})
    events.wait_state("finished")
    assert engines["beta"].calls[0]["voice"] == "v2"
    session.handle({"type": "play", "text": "Two.", "readingId": 2, "engine": "beta", "voice": "nope"})
    events.wait_for(lambda m: m.get("state") == "finished" and m["readingId"] == 2)
    assert engines["beta"].calls[-1]["voice"] == "v1"


def test_an_unknown_engine_falls_back_to_the_default(make):
    session, events, engines, _ = make()
    session.handle({"type": "play", "text": "One.", "readingId": 1, "engine": "gamma"})
    events.wait_state("finished")
    assert engines["alpha"].said() == ["One."]


def test_an_engine_that_cannot_run_says_why(make):
    client = FakeClient()
    engines = {"alpha": FakeEngine(spec("alpha", "Alpha"), client, reason="needs espeak-ng")}
    session, events, _, _ = make(engines=engines)
    session.handle({"type": "play", "text": TEXT, "readingId": 3})
    error = events.wait_state("error")
    assert "needs espeak-ng" in error["message"] and error["readingId"] == 3
    assert engines["alpha"].calls == []


def test_text_with_nothing_to_read_finishes_at_once(make):
    session, events, _, _ = make()
    session.handle({"type": "play", "text": "   \n ", "readingId": 4})
    assert events.wait_state("finished")["readingId"] == 4


def test_a_failed_sentence_is_reported(make):
    session, events, _, _ = make(fail_on="Second")
    session.controller.RETRY_BACKOFF = 0.01
    session.handle({"type": "play", "text": TEXT, "readingId": 5})
    error = events.wait_state("error")
    assert "Second" in error["message"]
    assert sentences_heard(events, 5) == [0]


def test_a_new_reading_replaces_the_old_one(make):
    session, events, engines, _ = make(player=FakePlayer(seconds=0.4))
    session.handle({"type": "play", "text": TEXT, "readingId": 1})
    events.wait_for(lambda m: m["type"] == "sentence" and m["readingId"] == 1)
    session.handle({"type": "play", "text": "Fresh text. Here.", "readingId": 2})
    events.wait_for(lambda m: m.get("state") == "finished" and m["readingId"] == 2)
    assert sentences_heard(events, 2) == [0, 1]
    assert "Third one." not in engines["alpha"].said()


# ── transport ────────────────────────────────────────────────────────────

def test_pause_holds_the_place_and_resume_carries_on(make):
    player = FakePlayer(seconds=0.3)
    session, events, _, _ = make(player=player)
    session.handle({"type": "play", "text": TEXT, "readingId": 1})
    events.wait_for(lambda m: m["type"] == "sentence" and m["index"] == 1)
    session.handle({"type": "pause"})
    assert events.wait_state("paused")["index"] == 1
    time.sleep(0.5)
    assert sentences_heard(events, 1) == [0, 1], "it kept reading while paused"
    session.handle({"type": "resume"})
    events.wait_state("finished")
    assert sentences_heard(events, 1) == [0, 1, 1, 2]   # the paused sentence starts over


def test_toggle_pauses_then_resumes(make):
    session, events, _, _ = make(player=FakePlayer(seconds=0.3))
    session.handle({"type": "play", "text": TEXT, "readingId": 1})
    events.wait_for(lambda m: m["type"] == "sentence")
    session.handle({"type": "toggle"})
    events.wait_state("paused")
    session.handle({"type": "toggle"})
    events.wait_state("finished")


def test_skipping_while_paused_moves_the_highlight_without_playing(make):
    player = FakePlayer(seconds=0.3)
    session, events, _, _ = make(player=player)
    session.handle({"type": "play", "text": TEXT, "readingId": 1})
    events.wait_for(lambda m: m["type"] == "sentence")
    session.handle({"type": "pause"})
    events.wait_state("paused")
    played = len(player.played)
    session.handle({"type": "next"})
    moved = events.wait_for(lambda m: m["type"] == "sentence" and m["index"] == 1)
    assert moved["paused"] is True
    time.sleep(0.3)
    assert len(player.played) == played


def test_next_and_prev_while_playing(make):
    session, events, _, _ = make(player=FakePlayer(seconds=1.0))
    session.handle({"type": "play", "text": TEXT, "readingId": 1})
    events.wait_count("sentence", 1)
    session.handle({"type": "next"})
    events.wait_count("sentence", 2)
    session.handle({"type": "prev"})
    events.wait_count("sentence", 3)
    assert sentences_heard(events, 1)[:3] == [0, 1, 0]


def test_stop_ends_the_reading(make):
    player = FakePlayer(seconds=0.5)
    session, events, _, _ = make(player=player)
    session.handle({"type": "play", "text": TEXT, "readingId": 1})
    events.wait_for(lambda m: m["type"] == "sentence")
    session.handle({"type": "stop"})
    assert events.wait_state("stopped")["readingId"] == 1
    time.sleep(0.6)
    assert sentences_heard(events, 1) == [0]


def test_a_speed_change_is_heard_on_the_current_sentence(make):
    session, events, engines, _ = make(player=FakePlayer(seconds=1.0))
    session.handle({"type": "play", "text": TEXT, "readingId": 1, "speed": 1.0})
    events.wait_for(lambda m: m["type"] == "sentence" and m["index"] == 0)
    session.handle({"type": "speed", "value": 1.8})
    events.wait_for(lambda m: m["type"] == "sentence" and m["index"] == 0 and m.get("speed") == 1.8)
    assert {"text": "First one.", "speed": 1.8, "voice": "v1"} in engines["alpha"].calls


def test_speed_is_kept_within_range(make):
    session, events, engines, _ = make()
    session.handle({"type": "play", "text": "One.", "readingId": 1, "speed": 9})
    events.wait_state("finished")
    assert engines["alpha"].calls[0]["speed"] == 2.0


def test_switching_voice_mid_sentence_rereads_it_in_the_new_voice(make):
    session, events, engines, client = make(player=FakePlayer(seconds=1.0))
    session.handle({"type": "play", "text": TEXT, "readingId": 1})
    events.wait_for(lambda m: m["type"] == "sentence" and m["index"] == 0)
    session.handle({"type": "voice", "engine": "beta", "voice": "v2"})
    events.wait_for(lambda m: m["type"] == "sentence" and m.get("engine") == "beta")
    assert engines["beta"].calls[0] == {"text": "First one.", "speed": 1.5, "voice": "v2"}
    assert client.active[-1] == "beta"


# ── loading, idling, housekeeping ────────────────────────────────────────

def test_the_first_sentence_says_the_model_is_loading(make):
    session, events, _, _ = make(delay=0.2, downloaded=False)
    session.handle({"type": "play", "text": "One.", "readingId": 1})
    loading = events.wait_state("loading")
    assert loading["message"].startswith("Loading Alpha")
    assert "downloading" in loading["message"]


def test_preload_brings_the_model_in_ahead_of_time(make):
    session, events, _, client = make()
    session.handle({"type": "preload", "engine": "beta"})
    ready = events.wait_for(lambda m: m["type"] == "ready")
    assert ready["engine"] == "beta" and client.loads == ["beta"]


def test_idle_time_counts_only_when_nothing_is_playing(make):
    session, events, _, _ = make(player=FakePlayer(seconds=0.4))
    session.handle({"type": "play", "text": "One. Two.", "readingId": 1})
    events.wait_for(lambda m: m["type"] == "sentence")
    assert session.idle_seconds() == 0
    events.wait_state("finished")
    time.sleep(0.2)
    assert session.idle_seconds() >= 0.15


def test_an_unknown_command_is_answered_with_an_error(make):
    session, events, _, _ = make()
    session.handle({"type": "dance"})
    assert "dance" in events.wait_for(lambda m: m["type"] == "error")["message"]


def test_close_frees_the_worker(make):
    session, _, _, client = make()
    session.close()
    assert client.closed


def test_events_carry_the_names_to_show(make):
    session, events, _, _ = make()
    session.handle({"type": "play", "text": "One.", "readingId": 1, "engine": "beta", "voice": "v2"})
    reading = events.wait_for(lambda m: m["type"] == "reading")
    sentence = events.wait_for(lambda m: m["type"] == "sentence")
    for message in (reading, sentence):
        assert (message["engineName"], message["voiceLabel"]) == ("Beta", "Voice two")


def test_play_can_start_partway_through(make):
    """Retrying after an error picks up at the sentence that failed."""
    session, events, engines, _ = make()
    session.handle({"type": "play", "text": TEXT, "readingId": 1, "startIndex": 2})
    events.wait_state("finished")
    assert engines["alpha"].said() == ["Third one."]
    assert sentences_heard(events, 1) == [2]
