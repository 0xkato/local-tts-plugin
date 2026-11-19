"""Native messaging framing: a 32-bit length in native byte order, then JSON."""
import io
import struct

import pytest

from local_tts_plugin.protocol import MAX_OUTGOING, ProtocolError, encode, read_message


def test_a_message_round_trips():
    stream = io.BytesIO(encode({"type": "play", "text": "Hi — there 😀"}))
    assert read_message(stream) == {"type": "play", "text": "Hi — there 😀"}


def test_the_length_prefix_counts_utf8_bytes():
    frame = encode({"t": "é"})
    (length,) = struct.unpack("=I", frame[:4])
    assert length == len(frame) - 4 == len('{"t":"é"}'.encode("utf-8"))


def test_messages_are_read_one_after_another():
    stream = io.BytesIO(encode({"n": 1}) + encode({"n": 2}))
    assert [read_message(stream), read_message(stream), read_message(stream)] == [
        {"n": 1}, {"n": 2}, None]


def test_end_of_stream_is_none():
    assert read_message(io.BytesIO(b"")) is None


class Trickle(io.RawIOBase):
    """Hands out one byte per read, as a pipe may."""

    def __init__(self, data):
        self.data = data

    def readable(self):
        return True

    def read(self, n=-1):
        if not self.data:
            return b""
        byte, self.data = self.data[:1], self.data[1:]
        return byte


def test_short_reads_are_reassembled():
    assert read_message(Trickle(encode({"type": "hello"}))) == {"type": "hello"}


def test_a_message_cut_off_midway_is_an_error():
    frame = encode({"type": "hello"})
    with pytest.raises(ProtocolError):
        read_message(io.BytesIO(frame[:-3]))
    with pytest.raises(ProtocolError):
        read_message(io.BytesIO(frame[:2]))


def test_the_browser_limit_on_outgoing_messages_is_enforced():
    """Firefox drops the connection on a message over 1 MB from the host."""
    with pytest.raises(ProtocolError):
        encode({"blob": "x" * MAX_OUTGOING})
