"""Native messaging framing, as Firefox and Chrome speak it.

Each message, in both directions, is a 32-bit length in the machine's own
byte order followed by that many bytes of UTF-8 JSON.
"""
import json
import struct

# The browsers refuse anything bigger than 1 MB from a host, and cap what
# they send to one well above anything a page selection could need.
MAX_OUTGOING = 1024 * 1024
MAX_INCOMING = 64 * 1024 * 1024

_LENGTH = struct.Struct("=I")


class ProtocolError(Exception):
    """The stream does not hold a well-formed message."""


def encode(message) -> bytes:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_OUTGOING:
        raise ProtocolError(f"a {len(body)}-byte message is over the browser's 1 MB limit")
    return _LENGTH.pack(len(body)) + body


def read_message(stream):
    """The next message on a binary stream, or None once the stream has ended."""
    header = _read_exactly(stream, _LENGTH.size)
    if header is None:
        return None
    (length,) = _LENGTH.unpack(header)
    if length > MAX_INCOMING:
        raise ProtocolError(f"a {length}-byte message is over the {MAX_INCOMING}-byte limit")
    body = _read_exactly(stream, length)
    if body is None:
        raise ProtocolError("the stream ended inside a message")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"not a JSON message: {exc}") from None


def _read_exactly(stream, size):
    """Read `size` bytes, however many reads that takes. None if the stream
    ends before the first byte; ProtocolError if it ends partway."""
    chunks, remaining = [], size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if remaining == size:
                return None
            raise ProtocolError("the stream ended inside a message")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
