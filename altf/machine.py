"""Machine: console registry, status renderer, event queue, watchers, state.json.

The status block rendered here is the forgetting-proofing (DESIGN §7): it is
prepended to every tool result by the integration layer.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import re
import time
from collections import deque
from pathlib import Path

import pyte

from .classify import InputStateClassifier
from .console import (
    Console,
    ConsoleError,
    TermState,
    _shorten,
    descendant_pids,
    human_bytes,
    human_dur,
    parse_ps_table,
)
from .logs import CheckpointWriter, RotatingWriter, write_state
from .spawner import DEFAULT_DIMENSIONS, LocalSpawner, Spawner
from .stream import ConsoleStream

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_RULE_WIDTH = 61

# DESIGN §5 / §18.1: single init line, typed then suppressed. PS0 deliberately
# has no \[ \] (readline-only markers; here they would emit literal SOH/STX).
_INIT_LINE = (
    "PS1='\\[\\e]133;A\\a\\]'\"${PS1:-\\u@\\h:\\w\\$ }\"; "
    "PS0='\\e]133;C\\a'; "
    "PROMPT_COMMAND='printf \"\\e]133;D;%s\\a\" \"$?\"'; "
    "printf '\\e]7770;pid=%s\\a' \"$$\"\r"
)

_DEFAULT_SHELL = ["bash", "--noprofile", "--norc", "-i"]

_STATE_GLYPH = {
    TermState.IDLE: "IDLE",
    TermState.BUSY: "BUSY⚡",
    TermState.AWAITING: "AWAIT✋",
    TermState.EXITED: "EXITED💥",
    TermState.DEAD: "DEAD☠",
}

_HINT_LINE = (
    "hint: run()→IDLE consoles only · send()/press()→interact with a running "
    "program · peek()/wait()→follow output · screen()→TUIs · kill()→stop · "
    "logs are grep-able files (paths above)"
)


class Machine:
    def __init__(
        self,
        session: str,
        spawner: Spawner | None = None,
        *,
        workdir: str | os.PathLike,
        classifier: InputStateClassifier | None = None,
        shell: list[str] | None = None,
        env: dict | None = None,
        quiet_threshold: float = 2.0,
        send_settle: float = 1.0,
        press_settle: float = 0.5,
        raw_max_bytes: int | None = None,
        ckpt_every: int = 8 * 1024 * 1024,
    ) -> None:
        self.session = session
        self.spawner: Spawner = spawner or LocalSpawner()
        self.dir = Path(workdir) / session
        self.dir.mkdir(parents=True, exist_ok=True)
        self.classifier = classifier
        self.shell = list(shell) if shell else list(_DEFAULT_SHELL)
        self.env = dict(env) if env else {}
        self.quiet_threshold = quiet_threshold
        self.send_settle = send_settle
        self.press_settle = press_settle
        self.raw_max_bytes = raw_max_bytes
        self.ckpt_every = ckpt_every

        self._consoles: dict[str, Console] = {}
        self._streams: dict[str, ConsoleStream] = {}
        self._writers: dict[str, tuple[RotatingWriter, RotatingWriter, CheckpointWriter]] = {}
        self._spawn_count = 0
        self._pending_events: deque[tuple[str, str]] = deque(maxlen=20)
        self._event_history: deque[tuple[float, str, str]] = deque(maxlen=50)
        self._classify_cache: set[tuple[str, int]] = set()
        self._ticker: asyncio.Task | None = None
        self._dirty = True
        self._last_state_write = 0.0
        self._last_fg_refresh = 0.0
        self._closed = False

    # ------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> "Machine":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def spawn(
        self,
        name: str,
        purpose: str,
        cwd: str | None = None,
        long_running: bool = False,
    ) -> Console:
        if self._closed:
            raise ConsoleError("machine is closed")
        if not _NAME_RE.match(name):
            raise ConsoleError(
                f"bad console name {name!r}: use 1-32 chars of [A-Za-z0-9_-]"
            )
        if name in self._consoles:
            raise ConsoleError(f"console '{name}' already exists — pick another name")

        self._spawn_count += 1
        slot = f"f{self._spawn_count}"
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env.update(self.env)

        pty = self.spawner.spawn(list(self.shell), cwd, env)
        rows, cols = getattr(self.spawner, "dimensions", DEFAULT_DIMENSIONS)
        screen = pyte.Screen(cols, rows)

        console = Console(
            name=name,
            slot=slot,
            purpose=purpose,
            long_running=long_running,
            machine=self,
            spawner=self.spawner,
            pty=pty,
            screen=screen,
            session_dir=self.dir,
            quiet_threshold=self.quiet_threshold,
            send_settle=self.send_settle,
            press_settle=self.press_settle,
            termios_visible=getattr(self.spawner, "pty_reflects_termios", False),
        )
        base = console.file_base
        raw_writer = RotatingWriter(self.dir, f"{base}.raw", max_bytes=self.raw_max_bytes)
        log_writer = RotatingWriter(self.dir, f"{base}.log", max_bytes=self.raw_max_bytes)
        ckpt_writer = CheckpointWriter(self.dir / f"{base}.ckpt")
        stream = ConsoleStream(
            pty.fd,
            console=console,
            raw_writer=raw_writer,
            log_writer=log_writer,
            ckpt_writer=ckpt_writer,
            screen=screen,
            ckpt_every=self.ckpt_every,
        )
        stream.start()

        self._consoles[name] = console
        self._streams[name] = stream
        self._writers[name] = (raw_writer, log_writer, ckpt_writer)

        try:
            pty.write(_INIT_LINE.encode())
            d_before = console.d_count
            await asyncio.wait_for(console._handshake.wait(), timeout=15.0)
            ok = await console._until(lambda: console.d_count > d_before, 10.0)
            if not ok:
                raise asyncio.TimeoutError
        except asyncio.TimeoutError:
            await self._teardown_console(console, force=True)
            raise ConsoleError(
                f"console '{name}': shell integration handshake failed — is bash "
                f"available in {self.spawner.describe()}?"
            ) from None

        console.boot_complete()
        self._ensure_ticker()
        self.mark_dirty()
        self._write_state_now()
        return console

    def unregister(self, console: Console) -> None:
        self._consoles.pop(console.name, None)
        stream = self._streams.pop(console.name, None)
        if stream is not None:
            stream.stop()
        writers = self._writers.pop(console.name, None)
        if writers is not None:
            for writer in writers:
                writer.close()
        self.mark_dirty()
        self._write_state_now()

    async def _teardown_console(self, console: Console, force: bool = False) -> None:
        if console.pty is not None:
            try:
                console.pty.terminate(force=force)
            except Exception:
                pass
        console.state = TermState.DEAD
        self.unregister(console)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        for console in list(self._consoles.values()):
            try:
                await asyncio.wait_for(console.kill(whole_console=True), timeout=15.0)
            except Exception:
                await self._teardown_console(console, force=True)
        self._write_state_now()

    # -------------------------------------------------------------- registry

    def _get(self, name: str) -> Console:
        console = self._consoles.get(name)
        if console is None:
            have = ", ".join(sorted(self._consoles)) or "none"
            raise ConsoleError(f"no console named '{name}' (have: {have}) — spawn() it first")
        return console

    @property
    def consoles(self) -> dict[str, Console]:
        return dict(self._consoles)

    def watch(self, name: str, regex: str, label: str | None = None, severity: str = "high") -> None:
        console = self._get(name)
        console.watchers.append((re.compile(regex), label or regex, severity))

    def enqueue_event(self, severity: str, text: str) -> None:
        self._pending_events.append((severity, text))
        self._event_history.append((time.time(), severity, text))

    def mark_dirty(self) -> None:
        self._dirty = True

    # ---------------------------------------------------------- status block

    def render_status(self) -> str:
        title = f"── altf: {self.session} "
        lines = [title + "─" * max(0, _RULE_WIDTH - len(title))]
        if not self._consoles:
            lines.append("(no consoles — use spawn())")
        for console in sorted(self._consoles.values(), key=lambda c: int(c.slot[1:])):
            lines.append(self._status_row(console))
        while self._pending_events:
            severity, text = self._pending_events.popleft()
            if severity == "high":
                lines.append(f"⚠ {text}")
        lines.append("─" * _RULE_WIDTH)
        return "\n".join(lines)

    def _status_row(self, c: Console) -> str:
        label = f"{c.slot}·{c.name}"
        state = _STATE_GLYPH[c.state]
        purpose = f'"{_shorten(c.purpose, 30)}"'
        if c.state is TermState.IDLE:
            fields = [self.shell[0].rsplit("/", 1)[-1]]
            if c.last_cmd is not None:
                code = c.last_exit if c.last_exit is not None else "?"
                fields.append(f"last:`{_shorten(c.last_cmd, 24)}` exit:{code}")
            fields.append(purpose)
        elif c.state in (TermState.BUSY, TermState.AWAITING):
            fields = [c.fg_command or "?"]
            fields.append(f"unread:{human_bytes(c.unread)}")
            fields.append(f"quiet:{human_dur(c.quiet_for)}")
            if c.long_running:
                fields.append(f"up:{human_dur(time.monotonic() - c.spawned_at)}")
            fields.append(purpose)
        elif c.state is TermState.EXITED:
            code = c.last_exit if c.last_exit is not None else "?"
            ago = human_dur(time.monotonic() - (c.last_exit_at or time.monotonic()))
            fields = [f"exit:{code} {ago} ago"]
            if c.crash_tail:
                fields.append(f'tail:"{_shorten(c.crash_tail, 40)}"')
            fields.append(purpose)
        else:  # DEAD
            fields = ["(pty closed)", purpose]
        return f"{label:<11}{state:<9}{'  '.join(fields)}"

    # ------------------------------------------------------------ tool layer

    async def tool(self, fn, *args, **kwargs) -> str:
        """Wrap a tool_* coroutine: status block + payload, errors in-band."""
        try:
            payload = await fn(*args, **kwargs)
        except ConsoleError as exc:
            payload = f"ERROR: {exc}"
        return self.render_status() + "\n\n" + payload

    async def tool_spawn(
        self, name: str, purpose: str, cwd: str | None = None, long_running: bool = False
    ) -> str:
        console = await self.spawn(name, purpose, cwd=cwd, long_running=long_running)
        return (
            f"spawned {console.slot}·{console.name} "
            f"(shell pid {console.shell_pid} in {self.spawner.describe()}; "
            f"logs: {console.log_path})"
        )

    async def tool_run(
        self, name: str, command: str, timeout: float = 60.0, max_output: int = 8000
    ) -> str:
        return await self._get(name).run(command, timeout=timeout, max_output=max_output)

    async def tool_send(self, name: str, text: str, enter: bool = True) -> str:
        return await self._get(name).send(text, enter=enter)

    async def tool_press(self, name: str, keys: list[str]) -> str:
        return await self._get(name).press(keys)

    async def tool_peek(self, name: str, max_bytes: int = 4000) -> str:
        return await self._get(name).peek(max_bytes=max_bytes)

    async def tool_wait(
        self, name: str, pattern: str | None = None, timeout: float = 30.0
    ) -> str:
        return await self._get(name).wait(pattern=pattern, timeout=timeout)

    async def tool_screen(self, name: str) -> str:
        return await self._get(name).render_screen()

    async def tool_kill(self, name: str, whole_console: bool = False) -> str:
        return await self._get(name).kill(whole_console=whole_console)

    async def tool_status(self) -> str:
        lines = []
        for c in sorted(self._consoles.values(), key=lambda c: int(c.slot[1:])):
            code = c.last_exit if c.last_exit is not None else "-"
            lines.append(
                f"{c.slot}·{c.name}: state={c.state.value} purpose={c.purpose!r} "
                f"long_running={c.long_running} shell_pid={c.shell_pid} "
                f"last_cmd={c.last_cmd!r} last_exit={code} "
                f"unread={human_bytes(c.unread)} logs={c.log_path}"
            )
        if not lines:
            lines.append("(no consoles)")
        if self._event_history:
            lines.append("recent events:")
            for ts, severity, text in list(self._event_history)[-10:]:
                stamp = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                lines.append(f"  {stamp} [{severity}] {text}")
        lines.append(_HINT_LINE)
        return "\n".join(lines)

    # ------------------------------------------------------------ background

    def _ensure_ticker(self) -> None:
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.get_running_loop().create_task(self._tick_loop())

    async def _tick_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.25)
                await self._tick_once()
        except asyncio.CancelledError:
            pass

    async def _tick_once(self) -> None:
        now = time.monotonic()
        busy = [
            c
            for c in self._consoles.values()
            if c.state in (TermState.BUSY, TermState.AWAITING)
        ]
        for console in busy:
            if console.state is TermState.BUSY and console.quiet_for >= self.quiet_threshold:
                verdict = await self._classify(console)
                if verdict == "awaiting" and console.state is TermState.BUSY:
                    console.state = TermState.AWAITING
                    self.enqueue_event(
                        "high",
                        f"{console.slot}·{console.name} appears to be waiting for input",
                    )
                    console._pulse()
                    self.mark_dirty()
        if busy and now - self._last_fg_refresh >= 3.0:
            self._last_fg_refresh = now
            await self._refresh_fg()
        if self._dirty and now - self._last_state_write >= 1.0:
            self._write_state_now()

    async def _classify(self, console: Console) -> str:
        # Layer 1 (DESIGN §11): structural terminal facts — free, deterministic.
        if console.alt_screen or console.echo_off:
            return "awaiting"
        if self.classifier is None:
            return "working"
        # Layer 2: the LLM judges each quiet period at most once.
        key = (console.name, console.raw_total)
        if key in self._classify_cache:
            return "working"
        self._classify_cache.add(key)
        if len(self._classify_cache) > 1000:
            self._classify_cache.clear()
            self._classify_cache.add(key)
        try:
            return await self.classifier.classify(
                console.tail_text, console.quiet_for, console.screen_text()
            )
        except Exception:
            return "working"

    async def _refresh_fg(self) -> None:
        """One out-of-band `ps` covers every console (never a hot poll loop)."""
        try:
            rc, out = await self.spawner.out_of_band(
                ["ps", "-e", "-o", "pid=,ppid=,pgid=,comm="]
            )
        except Exception:
            return
        if rc != 0:
            return
        rows = parse_ps_table(out)
        for console in self._consoles.values():
            if console.shell_pid is None or console.shell_pid not in rows:
                continue
            shell_pgid = rows[console.shell_pid][1]
            fg = [
                pid
                for pid in descendant_pids(rows, console.shell_pid)
                if rows[pid][1] != shell_pgid
            ]
            # newest pid as the best guess at the currently-foreground command
            console.fg_command = rows[max(fg)][2] if fg else None

    # ------------------------------------------------------------ state.json

    def _write_state_now(self) -> None:
        self._dirty = False
        self._last_state_write = time.monotonic()
        consoles: dict[str, dict] = {}
        for c in self._consoles.values():
            writers = self._writers.get(c.name)
            raw_writer = writers[0] if writers else None
            cursor_file, cursor_offset = (
                raw_writer.locate(c.cursor_raw) if raw_writer else (f"{c.file_base}.raw", 0)
            )
            consoles[c.name] = {
                "slot": c.slot,
                "purpose": c.purpose,
                "state": c.state.value,
                "long_running": c.long_running,
                "shell_pid": c.shell_pid,
                "fg_command": c.fg_command,
                "spawned_at": datetime.datetime.fromtimestamp(
                    c.spawned_at_wall, datetime.timezone.utc
                ).isoformat(),
                "last_output_at": datetime.datetime.fromtimestamp(
                    time.time() - max(0.0, c.quiet_for), datetime.timezone.utc
                ).isoformat(),
                "last_cmd": c.last_cmd,
                "last_exit": c.last_exit,
                "raw_file": raw_writer.current_name if raw_writer else f"{c.file_base}.raw",
                "log_file": f"{c.file_base}.log",
                "raw_bytes": c.raw_total,
                "agent_cursor": {"file": cursor_file, "offset": cursor_offset},
            }
        write_state(
            self.dir / "state.json",
            {
                "version": 1,
                "session": self.session,
                "spawner": self.spawner.describe(),
                "consoles": consoles,
            },
        )
