"""Console state machine driven by scripted feeds — no pty (DESIGN §17)."""

import asyncio

import pytest

from altf.console import Console, ConsoleError, TermState, encode_key
from altf.osc import AltfHandshake, Osc133


def make_console(**kwargs):
    writes = []
    console = Console(
        name=kwargs.pop("name", "test"),
        slot="f1",
        purpose="unit test",
        write_fn=writes.append,
        send_settle=0.01,
        press_settle=0.01,
        **kwargs,
    )
    console.shell_pid = 100
    console.boot_complete()
    return console, writes


def feed_command_cycle(console, output="hello\n", exit_code=0):
    console.on_mark(Osc133("C"))
    console.on_text(output)
    console.on_raw(len(output))
    console.on_mark(Osc133("D", exit_code))
    console.on_mark(Osc133("A"))


async def test_run_captures_output_and_exit():
    console, writes = make_console()
    task = asyncio.ensure_future(console.run("echo hello", timeout=5))
    await asyncio.sleep(0.01)
    assert console.state is TermState.BUSY
    feed_command_cycle(console, "hello\n", 0)
    result = await task
    assert "exit:0" in result
    assert "hello" in result
    assert console.state is TermState.IDLE
    assert writes == [b"echo hello\r"]


async def test_run_refused_when_busy():
    console, _ = make_console()
    task = asyncio.ensure_future(console.run("sleep 99", timeout=5))
    await asyncio.sleep(0.01)
    with pytest.raises(ConsoleError, match="BUSY"):
        await console.run("echo nope")
    feed_command_cycle(console, "", 130)
    await task


async def test_run_timeout_keeps_running():
    console, _ = make_console()
    result = await console.run("sleep 99", timeout=0.05)
    assert "still running" in result
    assert console.state is TermState.BUSY
    # the late D (no waiter, not long_running) returns to IDLE quietly
    console.on_mark(Osc133("D", 0))
    assert console.state is TermState.IDLE


async def test_long_running_unattended_death_is_exited():
    console, _ = make_console(long_running=True)
    await console.send("./server")
    assert console.state is TermState.BUSY
    console.on_mark(Osc133("C"))
    console.on_text("boom: segfault\n")
    console.on_mark(Osc133("D", 139))
    assert console.state is TermState.EXITED
    assert console.crash_tail == "boom: segfault"
    assert console.last_exit == 139


async def test_long_running_run_completion_is_not_exited():
    # §18.5: a D resolving an awaiting run() is a requested exit.
    console, _ = make_console(long_running=True)
    task = asyncio.ensure_future(console.run("ls", timeout=5))
    await asyncio.sleep(0.01)
    feed_command_cycle(console, "files\n", 0)
    await task
    assert console.state is TermState.IDLE


async def test_long_running_timeout_then_death_is_exited():
    console, _ = make_console(long_running=True)
    result = await console.run("./server", timeout=0.05)
    assert "still running" in result
    console.on_mark(Osc133("D", 1))
    assert console.state is TermState.EXITED


async def test_exited_console_accepts_next_command():
    console, _ = make_console(long_running=True)
    await console.send("./server")
    console.on_mark(Osc133("D", 1))
    assert console.state is TermState.EXITED
    task = asyncio.ensure_future(console.run("echo back", timeout=5))
    await asyncio.sleep(0.01)
    assert console.state is TermState.BUSY
    feed_command_cycle(console, "back\n", 0)
    await task
    assert console.state is TermState.IDLE


async def test_kill_requested_death_is_idle_not_exited():
    console, _ = make_console(long_running=True)
    await console.send("./server")
    task = asyncio.ensure_future(console.kill())
    await asyncio.sleep(0.05)  # first C-c sent, kill in flight
    console.on_mark(Osc133("D", 130))
    result = await task
    assert console.state is TermState.IDLE
    assert "C-c" in result


async def test_eof_means_dead_and_pending_run_errors():
    console, _ = make_console()
    task = asyncio.ensure_future(console.run("exit", timeout=5))
    await asyncio.sleep(0.01)
    console.on_eof()
    with pytest.raises(ConsoleError, match="died"):
        await task
    assert console.state is TermState.DEAD
    with pytest.raises(ConsoleError, match="DEAD"):
        await console.run("echo nope")


async def test_awaiting_returns_to_busy_on_output():
    console, _ = make_console()
    await console.send("python3")
    console.state = TermState.AWAITING
    console.on_text(">>> ")
    assert console.state is TermState.BUSY


async def test_peek_advances_cursor_and_reports_remainder():
    console, _ = make_console()
    console.on_raw(6)
    console.on_text("abcdef")
    first = await console.peek(max_bytes=3)
    assert first.startswith("abc")
    assert "more unread" in first
    second = await console.peek(max_bytes=100)
    assert second.startswith("def")
    assert await console.peek() == "(no new output)"
    assert console.unread == 0


async def test_handshake_sets_pid():
    console, _ = make_console()
    console.on_mark(AltfHandshake(pid=4242))
    assert console.shell_pid == 4242


async def test_wait_pattern_and_prompt():
    console, _ = make_console()
    await console.send("./job")

    async def feeder():
        await asyncio.sleep(0.02)
        console.on_raw(9)
        console.on_text("Listening")

    asyncio.ensure_future(feeder())
    result = await console.wait(pattern="List", timeout=2)
    assert "[wait: pattern" in result
    assert "Listening" in result

    async def finisher():
        await asyncio.sleep(0.02)
        console.on_mark(Osc133("D", 0))

    asyncio.ensure_future(finisher())
    result = await console.wait(timeout=2)
    assert "[wait: prompt (exit:0)" in result


async def test_wait_timeout():
    console, _ = make_console()
    await console.send("./job")
    result = await console.wait(pattern="never", timeout=0.1)
    assert "[wait: timeout" in result


def test_encode_keys():
    assert encode_key("C-c") == b"\x03"
    assert encode_key("C-d") == b"\x04"
    assert encode_key("C-z") == b"\x1a"
    assert encode_key("Up") == b"\x1b[A"
    assert encode_key("Enter") == b"\r"
    assert encode_key("F5") == b"\x1b[15~"
    assert encode_key("M-b") == b"\x1bb"
    with pytest.raises(ConsoleError):
        encode_key("Hyper-x")
