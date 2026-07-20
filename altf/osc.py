"""Incremental OSC 133 / altf-handshake scanner.

Pure and incremental: `feed()` accepts arbitrary byte chunks and returns an
ordered event list; marks split across chunks are reassembled; malformed or
unknown sequences are passed through as bytes. Never raises on any input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class Osc133:
    """A FinalTerm shell-integration mark: A/B (prompt), C (output begins),
    D (command finished, with exit code when the shell provided one)."""

    kind: str
    exit_code: int | None = None


@dataclass(frozen=True)
class AltfHandshake:
    """OSC 7770 — the altf private channel. Currently carries the shell's
    environment-local pid."""

    pid: int


Mark = Union[Osc133, AltfHandshake]
# ("bytes", b"...") for passthrough data, ("mark", Mark) for recognized marks,
# in exact stream order.
Event = tuple[str, "bytes | Mark"]

# A complete OSC sequence: ESC ] payload (BEL | ST). Payloads never contain
# ESC or BEL, so anything else is malformed and falls through as bytes.
_OSC = re.compile(rb"\x1b\]([^\x07\x1b]*)(?:\x07|\x1b\\)")


def _parse_payload(payload: bytes) -> Mark | None:
    if payload.startswith(b"133;"):
        rest = payload[4:]
        if rest in (b"A", b"B", b"C"):
            return Osc133(rest.decode("ascii"))
        if rest == b"D":
            return Osc133("D", None)
        if rest.startswith(b"D;"):
            try:
                return Osc133("D", int(rest[2:]))
            except ValueError:
                return None
    elif payload.startswith(b"7770;pid="):
        try:
            return AltfHandshake(pid=int(payload[9:]))
        except ValueError:
            return None
    return None


def _hold_point(rest: bytes, max_pending: int) -> int:
    """Index where a possibly-incomplete OSC sequence begins (hold from there
    until more bytes arrive), or len(rest) when nothing needs holding."""
    start = rest.find(b"\x1b]")
    while start != -1:
        after = rest[start + 2 :]
        if b"\x07" in after or b"\x1b\\" in after:
            # terminated, so the scan already rejected it (malformed/unknown):
            # it stays passthrough; look for a later candidate
            start = rest.find(b"\x1b]", start + 2)
        elif len(rest) - start > max_pending:
            return len(rest)  # oversized garbage: stop waiting, pass through
        else:
            return start
    if rest.endswith(b"\x1b"):
        return len(rest) - 1  # lone ESC may become ESC ] in the next chunk
    return len(rest)


class OscScanner:
    """Detects OSC 133 / 7770 sequences in a byte stream, removing them from
    the passthrough bytes. All other bytes — including unknown or malformed
    escape sequences — pass through untouched, in order."""

    def __init__(self, max_pending: int = 8192) -> None:
        self._held = b""
        self._max_pending = max_pending

    def feed(self, data: bytes) -> list[Event]:
        buf = self._held + data
        events: list[Event] = []
        pos = 0
        for match in _OSC.finditer(buf):
            mark = _parse_payload(match.group(1))
            if mark is None:
                continue  # unknown/malformed OSC stays in the passthrough bytes
            if match.start() > pos:
                events.append(("bytes", buf[pos : match.start()]))
            events.append(("mark", mark))
            pos = match.end()
        rest = buf[pos:]
        hold = _hold_point(rest, self._max_pending)
        self._held = rest[hold:]
        if hold:
            events.append(("bytes", rest[:hold]))
        return events

    def flush(self) -> bytes:
        """Return any held partial sequence (call at EOF)."""
        held, self._held = self._held, b""
        return held
