"""pydantic-ai integration: toolset factory + status-block history processor.

`pydantic_ai` is an optional dependency — this module imports it lazily so
`Machine`/`Console` stay usable bare (DESIGN §14).
"""

from __future__ import annotations

import re

from .machine import Machine

# Matches one rendered status block (header rule through closing rule).
_STATUS_BLOCK = re.compile(r"── altf:.*?\n─{10,}\n?", re.S)


def _require_pydantic_ai():
    try:
        from pydantic_ai.toolsets import FunctionToolset
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "pydantic-ai is not installed: pip install 'altf[pydantic-ai]'"
        ) from exc
    return FunctionToolset


def altf_toolset(machine: Machine):
    """Build a FunctionToolset exposing the DESIGN §8 tool surface. Every tool
    result is `machine.render_status() + "\\n\\n" + payload`."""
    FunctionToolset = _require_pydantic_ai()

    async def spawn(
        name: str, purpose: str, cwd: str | None = None, long_running: bool = False
    ) -> str:
        """Create a new console (like opening a terminal window). `purpose` is shown in
        every status block — write it for your future self. Set long_running=True for
        servers/daemons so an unexpected exit is flagged loudly."""
        return await machine.tool(
            machine.tool_spawn, name, purpose, cwd=cwd, long_running=long_running
        )

    async def run(
        name: str, command: str, timeout: float = 60.0, max_output: int = 8000
    ) -> str:
        """Run a command that terminates, on an IDLE console. Blocks until the prompt
        returns or `timeout`. On timeout the command KEEPS RUNNING (console goes busy;
        use peek/wait/press). Refused with guidance if the console is BUSY/AWAITING.
        Returns the command's output (head+tail truncated to max_output) and exit code."""
        return await machine.tool(
            machine.tool_run, name, command, timeout=timeout, max_output=max_output
        )

    async def send(name: str, text: str, enter: bool = True) -> str:
        """Type text into whatever is running (REPL, gdb, y/n prompt, password).
        Returns quickly with any output produced within ~1s."""
        return await machine.tool(machine.tool_send, name, text, enter=enter)

    async def press(name: str, keys: list[str]) -> str:
        """Press special keys: 'C-c','C-d','C-z','Up','Down','Left','Right','Tab',
        'Enter','Escape','Space','Backspace','PageUp','PageDown','Home','End','F1'..'F12'.
        Use for interrupting, EOF, shell history, TUI navigation."""
        return await machine.tool(machine.tool_press, name, keys)

    async def peek(name: str, max_bytes: int = 4000) -> str:
        """Read output you haven't seen yet (advances your read cursor). Non-blocking.
        Use to check on servers/long jobs."""
        return await machine.tool(machine.tool_peek, name, max_bytes=max_bytes)

    async def wait(name: str, pattern: str | None = None, timeout: float = 30.0) -> str:
        """Block until `pattern` (regex) appears in NEW output, or the prompt returns,
        or the console starts waiting for input — whichever first. THE tool for
        'start the server, wait until it says Listening'."""
        return await machine.tool(machine.tool_wait, name, pattern=pattern, timeout=timeout)

    async def screen(name: str) -> str:
        """Render the console's current visible screen (what a human would see).
        Use for TUIs (gdb TUI, vim, htop). Does not consume unread output."""
        return await machine.tool(machine.tool_screen, name)

    async def kill(name: str, whole_console: bool = False) -> str:
        """Stop the foreground process (escalating interrupt). whole_console=True
        destroys the console entirely."""
        return await machine.tool(machine.tool_kill, name, whole_console=whole_console)

    async def status() -> str:
        """Full machine status: all consoles, purposes, states, recent history."""
        return await machine.tool(machine.tool_status)

    return FunctionToolset(
        tools=[spawn, run, send, press, peek, wait, screen, kill, status]
    )


def status_refresher(machine: Machine):
    """History processor: strips stale status blocks from older tool returns and
    replaces the newest one with a current render, so context never accumulates
    N snapshots of dead state. Defensive: on any surprise it returns the
    messages untouched."""

    def _iter_string_parts(messages):
        for message in messages:
            for part in getattr(message, "parts", ()):
                if type(part).__name__ == "ToolReturnPart" and isinstance(
                    getattr(part, "content", None), str
                ):
                    yield part

    def processor(messages):
        try:
            parts = [p for p in _iter_string_parts(messages) if _STATUS_BLOCK.search(p.content)]
            if not parts:
                return messages
            for part in parts[:-1]:
                part.content = _STATUS_BLOCK.sub("", part.content).lstrip("\n")
            parts[-1].content = _STATUS_BLOCK.sub(
                machine.render_status() + "\n", parts[-1].content, count=1
            )
        except Exception:
            pass
        return messages

    return processor
