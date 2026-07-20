"""Per-console reader: pty fd -> raw log, stripped log, pyte screen, OSC marks.

Runs entirely on the event loop via `add_reader` — no threads, no polling.

Text extraction is delegated to pyte: `TextListener` is a gridless listener
attached to a second pyte stream, receiving parsed events (draw/linefeed/...)
instead of raw bytes. pyte already knows where every escape sequence ends, so
there is no hand-written ANSI parser anywhere; and because the listener has no
grid, log lines are never artificially wrapped at the screen width.
"""

from __future__ import annotations

import os

import pyte

from .logs import CheckpointWriter, RotatingWriter
from .osc import OscScanner

_READ_SIZE = 65536
_ALT_MODES = {47, 1047, 1049}  # xterm alternate-screen private modes

# pyte turns the structural C0 controls (\n \r \t \b ...) into events but
# passes the obscure ones (SOH, STX, ...) through in draw() text — delete those.
_DROP_CTRL = {c: None for c in (*range(0x20), 0x7F) if c not in (0x09, 0x0A, 0x0D)}


def _noop(*args, **kwargs) -> None:
    return None


class TextListener:
    """Extracts chronological, escape-free text from pyte's event stream and
    collapses carriage returns into newlines (progress bars become lines, so
    the .log stays greppable). Also tracks the alternate screen for the
    console's structural AWAITING hint (DESIGN §11)."""

    def __init__(self, console) -> None:
        self._console = console
        self._parts: list[str] = []
        self._pending_cr = False

    def take(self) -> str:
        """Drain the text accumulated since the last call."""
        text, self._parts = "".join(self._parts), []
        return text

    def flush(self) -> str:
        """Drain everything, resolving a trailing \\r (call at EOF)."""
        if self._pending_cr:
            self._pending_cr = False
            self._parts.append("\n")
        return self.take()

    def _emit(self, text: str) -> None:
        if self._pending_cr:
            self._pending_cr = False
            self._parts.append("\n")
        self._parts.append(text)

    # -- pyte events we care about; everything else is a no-op via __getattr__

    def draw(self, text: str) -> None:
        text = text.translate(_DROP_CTRL)
        if text:
            self._emit(text)

    def tab(self) -> None:
        self._emit("\t")

    def linefeed(self) -> None:
        self._pending_cr = False
        self._parts.append("\n")

    def carriage_return(self) -> None:
        self._pending_cr = True

    def set_mode(self, *modes, private: bool = False, **kwargs) -> None:
        if private and _ALT_MODES & set(modes):
            self._console.alt_screen = True

    def reset_mode(self, *modes, private: bool = False, **kwargs) -> None:
        if private and _ALT_MODES & set(modes):
            self._console.alt_screen = False

    def __getattr__(self, name):
        return _noop


class ConsoleStream:
    def __init__(
        self,
        fd: int,
        *,
        console,
        raw_writer: RotatingWriter,
        log_writer: RotatingWriter,
        ckpt_writer: CheckpointWriter | None,
        screen: pyte.Screen,
        ckpt_every: int = 8 * 1024 * 1024,
    ) -> None:
        self.fd = fd
        self.console = console
        self.raw = raw_writer
        self.log = log_writer
        self.ckpt = ckpt_writer
        self.screen = screen
        self.screen_stream = pyte.ByteStream(screen)
        self.scanner = OscScanner()
        self.listener = TextListener(console)
        self.text_stream = pyte.ByteStream(self.listener)
        self.ckpt_every = ckpt_every
        self._since_ckpt = 0
        self._loop = None
        self._stopped = False

    def start(self) -> None:
        import asyncio

        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self.fd, self._on_readable)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._loop is not None:
            try:
                self._loop.remove_reader(self.fd)
            except (OSError, ValueError):
                pass

    def _on_readable(self) -> None:
        try:
            data = os.read(self.fd, _READ_SIZE)
        except OSError:  # EIO on Linux when the child side is gone
            data = b""
        if not data:
            self._eof()
            return

        self.raw.write(data)
        try:
            self.screen_stream.feed(data)
        except Exception:
            pass  # a rendering hiccup must never take down the reader
        self.console.on_raw(len(data))
        for kind, payload in self.scanner.feed(data):
            if kind == "bytes":
                self._emit_text(payload)  # type: ignore[arg-type]
            else:
                self.console.on_mark(payload)

        self._since_ckpt += len(data)
        if self.ckpt is not None and self._since_ckpt >= self.ckpt_every:
            self._since_ckpt = 0
            self.ckpt.write(
                {
                    "raw_file": self.raw.current_name,
                    "offset": self.raw.current_size,
                    "lines": list(self.screen.display),
                    "cursor": [self.screen.cursor.x, self.screen.cursor.y],
                }
            )

    def _emit_text(self, payload: bytes) -> None:
        try:
            self.text_stream.feed(payload)
        except Exception:
            pass
        text = self.listener.take()
        if text:
            self.log.write_text(text)
            self.console.on_text(text)

    def _eof(self) -> None:
        self.stop()
        try:
            self.text_stream.feed(self.scanner.flush())
        except Exception:
            pass
        tail = self.listener.flush()
        if tail:
            self.log.write_text(tail)
            self.console.on_text(tail)
        self.console.on_eof()
