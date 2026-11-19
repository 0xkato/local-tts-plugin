"""Sending to the browser never waits on the pipe."""
import io
import threading
import time

from local_tts_plugin.host import Outbox
from local_tts_plugin.protocol import read_message


class SlowPipe(io.RawIOBase):
    def __init__(self):
        self.data = bytearray()
        self.open = threading.Event()

    def writable(self):
        return True

    def write(self, b):
        self.open.wait(5)          # a browser that is not reading right now
        self.data += b
        return len(b)


def test_sending_returns_at_once_and_keeps_the_order():
    pipe = SlowPipe()
    send = Outbox(pipe)
    started = time.monotonic()
    for n in range(3):
        send({"n": n})
    assert time.monotonic() - started < 0.1
    pipe.open.set()
    assert send.drain(timeout=5)
    stream = io.BytesIO(bytes(pipe.data))
    assert [read_message(stream)["n"] for _ in range(3)] == [0, 1, 2]


def test_a_closed_pipe_marks_the_outbox_closed():
    class Broken(io.RawIOBase):
        def writable(self):
            return True

        def write(self, b):
            raise BrokenPipeError

    send = Outbox(Broken())
    send({"type": "hello"})
    send.drain(timeout=5)
    assert send.closed
