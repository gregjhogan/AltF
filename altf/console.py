"""Console: state machine, cursor math, run/send/press/peek/wait/screen/kill.

A Console never knows where its shell runs (DESIGN §4); it owns a pty fd, an
unread cursor, and the OSC-driven state machine. It is constructible without a
pty/machine for state-machine unit tests: feed `on_text`/`on_mark`/`on_eof`
directly and pass a capturing `write_fn`.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Callable

from .osc import AltfHandshake, Osc133


class TermState(str, Enum):
    IDLE = "idle"  # at shell prompt (last mark was D, prompt drawn)
    BUSY = "busy"  # foreground process running / producing output
    AWAITING = "awaiting"  # running but judged blocked on user input
    EXITED = "exited"  # UNEXPECTED death of a long_running console's fg process
    DEAD = "dead"  # pty/child gone


class ConsoleError(Exception):
    """Raised with a model-facing message; the tool layer renders it as ERROR."""


_CTRL_EXTRA = {"@": 0, "[": 27, "\\": 28, "]": 29, "^": 30, "_": 31, "?": 127}

KEY_BYTES = {
    "Enter": b"\r",
    "Tab": b"\t",
    "Escape": b"\x1b",
    "Space": b" ",
    "Backspace": b"\x7f",
    "Delete": b"\x1b[3~",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "Right": b"\x1b[C",
    "Left": b"\x1b[D",
    "Home": b"\x1b[H",
    "End": b"\x1b[F",
    "PageUp": b"\x1b[5~",
    "PageDown": b"\x1b[6~",
    "F1": b"\x1bOP",
    "F2": b"\x1bOQ",
    "F3": b"\x1bOR",
    "F4": b"\x1bOS",
    "F5": b"\x1b[15~",
    "F6": b"\x1b[17~",
    "F7": b"\x1b[18~",
    "F8": b"\x1b[19~",
    "F9": b"\x1b[20~",
    "F10": b"\x1b[21~",
    "F11": b"\x1b[23~",
    "F12": b"\x1b[24~",
}


def encode_key(key: str) -> bytes:
    if key in KEY_BYTES:
        return KEY_BYTES[key]
    if key.startswith("C-") and len(key) == 3:
        c = key[2]
        if c.lower() != c.upper():  # a letter
            return bytes([ord(c.lower()) - 96])
        if c in _CTRL_EXTRA:
            return bytes([_CTRL_EXTRA[c]])
    if key.startswith("M-") and len(key) == 3:
        return b"\x1b" + key[2].encode("utf-8", "replace")
    if len(key) == 1:
        return key.encode("utf-8", "replace")
    raise ConsoleError(
        f"unknown key {key!r}; use C-<char>, M-<char>, single characters, "
        f"or one of: {', '.join(sorted(KEY_BYTES))}"
    )


class _CapBuf:
    """Bounded command-output capture: keeps head + tail, counts the middle."""

    def __init__(self, head_cap: int = 200_000, tail_cap: int = 200_000) -> None:
        self._head: list[str] = []
        self._head_len = 0
        self._head_cap = head_cap
        self._tail: deque[str] = deque()
        self._tail_len = 0
        self._tail_cap = tail_cap
        self.omitted = 0

    def add(self, text: str) -> None:
        if self._head_len < self._head_cap:
            take = min(len(text), self._head_cap - self._head_len)
            self._head.append(text[:take])
            self._head_len += take
            text = text[take:]
        if not text:
            return
        self._tail.append(text)
        self._tail_len += len(text)
        while self._tail_len > self._tail_cap and self._tail:
            dropped = self._tail.popleft()
            self._tail_len -= len(dropped)
            self.omitted += len(dropped)

    def get(self) -> str:
        head = "".join(self._head)
        tail = "".join(self._tail)
        if self.omitted:
            return head + f"\n[... {self.omitted} bytes omitted ...]\n" + tail
        return head + tail


def _shorten(text: str, limit: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def human_dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


_PENDING_CAP = 2_000_000  # chars of stripped text kept in memory per console


class Console:
    def __init__(
        self,
        *,
        name: str,
        slot: str,
        purpose: str,
        long_running: bool = False,
        write_fn: Callable[[bytes], None] | None = None,
        machine=None,
        spawner=None,
        pty=None,
        screen=None,
        session_dir: Path | None = None,
        quiet_threshold: float = 2.0,
        send_settle: float = 1.0,
        press_settle: float = 0.5,
        termios_visible: bool = False,
    ) -> None:
        self.name = name
        self.slot = slot
        self.purpose = purpose
        self.long_running = long_running
        self.machine = machine
        self.spawner = spawner
        self.pty = pty
        self.screen = screen
        self.session_dir = session_dir
        self.quiet_threshold = quiet_threshold
        self.send_settle = send_settle
        self.press_settle = press_settle
        self.termios_visible = termios_visible

        if write_fn is not None:
            self._write_bytes = write_fn
        elif pty is not None:
            self._write_bytes = pty.write
        else:
            self._write_bytes = lambda data: None

        self.state = TermState.BUSY  # busy until the boot handshake completes
        self.shell_pid: int | None = None
        self.fg_command: str | None = None
        self.last_cmd: str | None = None
        self.last_exit: int | None = None
        self.last_exit_at: float | None = None
        self.spawned_at = time.monotonic()
        self.spawned_at_wall = time.time()
        self.last_output_at = time.monotonic()
        self.crash_tail = ""
        self.alt_screen = False
        self.raw_total = 0
        self.cursor_raw = 0
        self.d_count = 0
        self.watchers: list[tuple[re.Pattern, str, str]] = []

        self._booted = False
        self._kill_requested = False
        self._capture = False
        self._capture_buf: _CapBuf | None = None
        self._d_waiters: deque[asyncio.Future] = deque()
        self._pending: deque[tuple[int, str]] = deque()  # (raw_end, stripped text)
        self._pending_chars = 0
        self._dropped_chars = 0
        self._tail = ""
        self._linebuf = ""
        self._last_line = ""
        self._lock = asyncio.Lock()
        self._activity = asyncio.Event()
        self._handshake = asyncio.Event()

    # ------------------------------------------------------------------ files

    @property
    def file_base(self) -> str:
        return f"{self.slot}-{self.name}"

    @property
    def log_path(self) -> str:
        if self.session_dir is None:
            return f"{self.file_base}.log"
        return str(self.session_dir / f"{self.file_base}.log")

    # ------------------------------------------------------- stream callbacks

    def on_raw(self, nbytes: int) -> None:
        self.raw_total += nbytes
        if self.machine is not None:
            self.machine.mark_dirty()

    def on_text(self, text: str) -> None:
        self.last_output_at = time.monotonic()
        if self.state is TermState.AWAITING:
            self.state = TermState.BUSY
        if self._capture and self._capture_buf is not None:
            self._capture_buf.add(text)

        self._pending.append((self.raw_total, text))
        self._pending_chars += len(text)
        while self._pending_chars > _PENDING_CAP and self._pending:
            _, dropped = self._pending.popleft()
            self._pending_chars -= len(dropped)
            self._dropped_chars += len(dropped)

        self._tail = (self._tail + text)[-2000:]
        self._feed_lines(text)
        self._pulse()

    def _feed_lines(self, text: str) -> None:
        self._linebuf += text
        if len(self._linebuf) > 8192 and "\n" not in self._linebuf:
            self._linebuf = self._linebuf[-4096:]
        while "\n" in self._linebuf:
            line, self._linebuf = self._linebuf.split("\n", 1)
            if line.strip():
                self._last_line = line.strip()
            for rx, label, severity in self.watchers:
                if rx.search(line) and self.machine is not None:
                    self.machine.enqueue_event(
                        severity, f"[{label}] {self.slot}·{self.name}: {_shorten(line.strip(), 90)}"
                    )

    def on_mark(self, mark) -> None:
        if isinstance(mark, AltfHandshake):
            self.shell_pid = mark.pid
            self._handshake.set()
        elif isinstance(mark, Osc133):
            if mark.kind == "C":
                self._capture = True
                self._capture_buf = _CapBuf()
            elif mark.kind == "D":
                self._on_d(mark.exit_code)
            # "A"/"B": prompt drawing — secondary confirmation only.
        self._pulse()

    def _on_d(self, exit_code: int | None) -> None:
        output = self._capture_buf.get() if self._capture_buf is not None else ""
        self._capture = False
        self._capture_buf = None
        self.d_count += 1
        self.last_exit = exit_code
        self.last_exit_at = time.monotonic()

        waiter = None
        while self._d_waiters:
            fut = self._d_waiters.popleft()
            if not fut.done():
                waiter = fut
                break
        if waiter is not None:
            waiter.set_result((exit_code, output))

        if not self._booted or self.state is TermState.DEAD:
            if self.state is not TermState.DEAD:
                self.state = TermState.IDLE
            return
        # DESIGN §6 + §18.5: EXITED only for unattended deaths of long_running
        # consoles — a D resolving an awaiting run(), or one during kill(), is
        # a requested exit.
        if waiter is not None or self._kill_requested or not self.long_running:
            self.state = TermState.IDLE
        else:
            self.state = TermState.EXITED
            self.crash_tail = self._current_tail_line()
            if self.machine is not None:
                self.machine.enqueue_event(
                    "high",
                    f"{self.slot}·{self.name} exited unexpectedly "
                    f"(exit:{exit_code if exit_code is not None else '?'})"
                    + (f' — "{_shorten(self.crash_tail, 60)}"' if self.crash_tail else ""),
                )
        if self.machine is not None:
            self.machine.mark_dirty()

    def on_eof(self) -> None:
        self.state = TermState.DEAD
        while self._d_waiters:
            fut = self._d_waiters.popleft()
            if not fut.done():
                fut.set_exception(ConsoleError(f"console '{self.name}' died (pty closed)"))
        if self.machine is not None:
            self.machine.enqueue_event("high", f"{self.slot}·{self.name} is DEAD (pty closed)")
            self.machine.mark_dirty()
        self._pulse()

    def _current_tail_line(self) -> str:
        partial = self._linebuf.strip()
        return partial or self._last_line

    # ------------------------------------------------------------- primitives

    def _pulse(self) -> None:
        self._activity.set()

    async def _wait_pulse(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._activity.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._activity.clear()

    async def _until(self, predicate: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if predicate():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return predicate()
            await self._wait_pulse(min(remaining, 0.25))

    def _check_alive(self) -> None:
        if self.state is TermState.DEAD:
            raise ConsoleError(
                f"console '{self.name}' is DEAD (shell/pty gone) — spawn a new "
                f"console; its logs remain at {self.log_path}"
            )

    @property
    def unread(self) -> int:
        return max(0, self.raw_total - self.cursor_raw)

    @property
    def quiet_for(self) -> float:
        return time.monotonic() - self.last_output_at

    @property
    def echo_off(self) -> bool:
        """True when the pty is in echo-off mode — a strong 'waiting for input'
        hint (password prompts, readline REPLs, curses apps). Only meaningful
        when the spawner says the local pty IS the target environment's pty
        (`Spawner.pty_reflects_termios`); otherwise always False."""
        if self.pty is None or not self.termios_visible:
            return False
        try:
            import termios

            attrs = termios.tcgetattr(self.pty.fd)
            return not (attrs[3] & termios.ECHO)
        except Exception:
            return False

    @property
    def tail_text(self) -> str:
        return self._tail

    def screen_text(self) -> str:
        if self.screen is None:
            return ""
        lines = [line.rstrip() for line in self.screen.display]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def _drain_pending(self, max_chars: int) -> str:
        chunks: list[str] = []
        taken = 0
        if self._dropped_chars:
            chunks.append(
                f"[... {human_bytes(self._dropped_chars)} of older unread output "
                f"dropped from memory — grep {self.log_path} ...]\n"
            )
            self._dropped_chars = 0
        while self._pending and taken < max_chars:
            raw_end, text = self._pending[0]
            take = min(len(text), max_chars - taken)
            if take == len(text):
                self._pending.popleft()
                self.cursor_raw = max(self.cursor_raw, raw_end)
            else:
                self._pending[0] = (raw_end, text[take:])
            self._pending_chars -= take
            chunks.append(text[:take])
            taken += take
        if self.machine is not None:
            self.machine.mark_dirty()
        return "".join(chunks)

    def _consume_all(self) -> None:
        self._pending.clear()
        self._pending_chars = 0
        self._dropped_chars = 0
        self.cursor_raw = self.raw_total
        if self.machine is not None:
            self.machine.mark_dirty()

    def boot_complete(self) -> None:
        """Called by Machine after the init handshake: suppress everything the
        model has not caused (bash banner, init-line echo, first prompt)."""
        self._booted = True
        self._consume_all()
        self.state = TermState.IDLE
        self.last_cmd = None
        self.last_exit = None
        self.last_exit_at = None

    # ------------------------------------------------------------------ tools

    async def run(self, command: str, timeout: float = 60.0, max_output: int = 8000) -> str:
        async with self._lock:
            self._check_alive()
            if self.state in (TermState.BUSY, TermState.AWAITING):
                fg = self.fg_command or "unknown"
                raise ConsoleError(
                    f"console '{self.name}' is {self.state.value.upper()} "
                    f"(fg: {fg}) — run() needs an IDLE console. Use send()/press() "
                    f"to interact with the running program, wait() to let it "
                    f"finish, kill() to stop it, or another console."
                )
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._d_waiters.append(fut)
            self._kill_requested = False
            self.last_cmd = command
            self.state = TermState.BUSY
            started = time.monotonic()
            self._write_bytes(command.encode() + b"\r")
            try:
                exit_code, output = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                try:
                    self._d_waiters.remove(fut)
                except ValueError:
                    pass
                fut.cancel()
                # Non-consuming preview: the bytes stay unread so a following
                # peek()/wait(pattern=...) still sees them as new output.
                early = "".join(text for _, text in self._pending)[-2000:]
                msg = (
                    f"[run: `{_shorten(command, 60)}` still running after "
                    f"{timeout:g}s — NOT killed; console '{self.name}' stays BUSY. "
                    f"Follow it with peek()/wait(), interrupt with press(['C-c']), "
                    f"or use another console.]"
                )
                if early.strip():
                    msg += f"\noutput so far (still unread):\n{early}"
                return msg
            duration = time.monotonic() - started
            self._consume_all()
            body = _truncate_output(output, max_output, self.log_path)
            code = exit_code if exit_code is not None else "?"
            return f"exit:{code} in {duration:.1f}s\n" + (body if body.strip() else "(no output)")

    async def send(self, text: str, enter: bool = True) -> str:
        async with self._lock:
            self._check_alive()
            if enter and self.state in (TermState.IDLE, TermState.EXITED):
                self.state = TermState.BUSY
                self.last_cmd = text
                self._kill_requested = False
            self._write_bytes(text.encode() + (b"\r" if enter else b""))
            await asyncio.sleep(self.send_settle)
            out = self._drain_pending(4000)
            return out if out.strip() else f"(no output within {self.send_settle:g}s)"

    async def press(self, keys: list[str]) -> str:
        async with self._lock:
            self._check_alive()
            data = b"".join(encode_key(k) for k in keys)
            if "Enter" in keys and self.state in (TermState.IDLE, TermState.EXITED):
                self.state = TermState.BUSY
                self._kill_requested = False
            self._write_bytes(data)
            await asyncio.sleep(self.press_settle)
            out = self._drain_pending(4000)
            head = f"pressed {' '.join(keys)}"
            return head + (f"\n{out}" if out.strip() else " (no output)")

    async def peek(self, max_bytes: int = 4000) -> str:
        async with self._lock:
            out = self._drain_pending(max_bytes)
            if not out.strip() and not self._pending:
                return "(no new output)"
            if self._pending:
                out += (
                    f"\n[... {human_bytes(self.unread)} more unread — peek() again "
                    f"or grep {self.log_path} via run() on another console ...]"
                )
            return out

    async def wait(self, pattern: str | None = None, timeout: float = 30.0) -> str:
        async with self._lock:
            self._check_alive()
            try:
                rx = re.compile(pattern) if pattern else None
            except re.error as exc:
                raise ConsoleError(f"bad wait() regex {pattern!r}: {exc}") from None
            started = time.monotonic()
            deadline = started + timeout
            d_start = self.d_count
            state_start = self.state
            collected: list[str] = []
            reason = "timeout"

            while True:
                text = self._drain_pending(65536)
                if text:
                    collected.append(text)
                joined = "".join(collected)
                if rx is not None and rx.search(joined):
                    reason = "pattern"
                    break
                if self.state is TermState.DEAD:
                    reason = "dead"
                    break
                if self.state is TermState.EXITED:
                    reason = "exited"
                    break
                if self.d_count > d_start or (
                    rx is None and state_start is TermState.IDLE and self.state is TermState.IDLE
                ):
                    reason = "prompt"
                    break
                if self.state is TermState.AWAITING:
                    reason = "awaiting"
                    break
                if (
                    rx is None
                    and self.long_running
                    and time.monotonic() - started >= self.quiet_threshold
                    and self.quiet_for >= self.quiet_threshold
                ):
                    reason = "quiet"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await self._wait_pulse(min(remaining, 0.25))

            elapsed = time.monotonic() - started
            if reason == "prompt":
                code = self.last_exit if self.last_exit is not None else "?"
                head = f"[wait: prompt (exit:{code}) after {elapsed:.1f}s]"
            else:
                head = f"[wait: {reason} after {elapsed:.1f}s]"
            out = "".join(collected)
            if len(out) > 4000:
                out = f"[... truncated, see {self.log_path} ...]\n" + out[-4000:]
            return head + (f"\n{out}" if out.strip() else "")

    async def render_screen(self) -> str:
        text = self.screen_text()
        return text if text.strip() else "(blank screen)"

    async def kill(self, whole_console: bool = False) -> str:
        async with self._lock:
            if self.state is TermState.DEAD and not whole_console:
                return f"console '{self.name}' is already dead"
            steps: list[str] = []
            self._kill_requested = True

            if self.state in (TermState.BUSY, TermState.AWAITING):
                for _ in range(2):
                    self._write_bytes(b"\x03")
                    steps.append("C-c")
                    if await self._until(self._fg_stopped, 1.5):
                        break
                if not self._fg_stopped():
                    pgids = await self._fg_pgids()
                    for sig, grace in (("TERM", 2.0), ("KILL", 1.0)):
                        if not pgids:
                            break
                        for pgid in pgids:
                            await self._oob(["kill", f"-{sig}", "--", f"-{pgid}"])
                        steps.append(f"{sig}→pgid {','.join(map(str, pgids))}")
                        if await self._until(self._fg_stopped, grace):
                            break

            if not whole_console:
                self._kill_requested = False
                if self._fg_stopped():
                    code = self.last_exit if self.last_exit is not None else "?"
                    return f"foreground stopped ({' → '.join(steps) or 'was not running'}; exit:{code})"
                return (
                    f"kill escalation exhausted ({' → '.join(steps)}); console still "
                    f"{self.state.value} — try kill(whole_console=True)"
                )

            # -------- destroy the console entirely (DESIGN §12) --------
            if self.state is not TermState.DEAD:
                self._write_bytes(b"exit\r")
                steps.append("exit")
                await self._until(lambda: self.state is TermState.DEAD, 1.5)
            if self.state is not TermState.DEAD:
                self._write_bytes(b"\x04")
                steps.append("C-d")
                await self._until(lambda: self.state is TermState.DEAD, 1.0)
            if self.state is not TermState.DEAD and self.shell_pid:
                await self._oob(["kill", "-KILL", str(self.shell_pid)])
                steps.append("KILL shell")
                await self._until(lambda: self.state is TermState.DEAD, 1.5)
            # Local client termination is last and cosmetic — the real killing
            # already happened in the target environment.
            if self.pty is not None:
                try:
                    self.pty.terminate(force=True)
                except Exception:
                    pass
            self.state = TermState.DEAD
            if self.machine is not None:
                self.machine.unregister(self)
            return f"console '{self.name}' destroyed ({' → '.join(steps) or 'was idle'})"

    def _fg_stopped(self) -> bool:
        return self.state in (TermState.IDLE, TermState.EXITED, TermState.DEAD)

    async def _oob(self, argv: list[str]) -> tuple[int, str]:
        if self.spawner is None:
            return 1, "no spawner"
        try:
            return await self.spawner.out_of_band(argv)
        except Exception as exc:
            return 1, str(exc)

    async def _fg_pgids(self) -> list[int]:
        """Environment-local pgids of foreground process groups under the shell."""
        if self.shell_pid is None:
            return []
        rc, out = await self._oob(["ps", "-e", "-o", "pid=,ppid=,pgid=,comm="])
        if rc != 0:
            return []
        rows = parse_ps_table(out)
        if self.shell_pid not in rows:
            return []
        shell_pgid = rows[self.shell_pid][1]
        return sorted(
            {
                rows[pid][1]
                for pid in descendant_pids(rows, self.shell_pid)
                if rows[pid][1] != shell_pgid
            }
        )


def parse_ps_table(out: str) -> dict[int, tuple[int, int, str]]:
    """Parse `ps -e -o pid=,ppid=,pgid=,comm=` output into pid -> (ppid, pgid, comm)."""
    rows: dict[int, tuple[int, int, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[3])
        except ValueError:
            continue
    return rows


def descendant_pids(rows: dict[int, tuple[int, int, str]], root: int) -> set[int]:
    """All pids strictly below `root` in the process tree described by `rows`."""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in rows.items():
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    frontier = [root]
    while frontier:
        for pid in children.get(frontier.pop(), ()):
            if pid not in found:
                found.add(pid)
                frontier.append(pid)
    return found


def _truncate_output(text: str, max_output: int, log_path: str) -> str:
    if len(text) <= max_output:
        return text
    lines = text.splitlines()
    head = lines[:20]
    tail = lines[-60:]
    kept = sum(len(l) + 1 for l in head) + sum(len(l) + 1 for l in tail)
    omitted = max(0, len(text) - kept)
    note = (
        f"[... {omitted} bytes omitted — use peek(), or grep {log_path} "
        f"via run() on another console ...]"
    )
    out = "\n".join(head) + "\n" + note + "\n" + "\n".join(tail)
    if len(out) > max_output + 2000:  # pathological single-line output
        out = out[: max_output // 2] + f"\n{note}\n" + out[-max_output // 2 :]
    return out
