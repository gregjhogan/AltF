"""LocalSpawner + real bash integration (DESIGN §17). CI-friendly, no docker."""

import asyncio
import sys

import pytest

from altf import Machine, TermState
from altf.logs import read_state


async def wait_for(predicate, timeout=6.0, interval=0.05):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def test_run_exit_codes_and_output(machine):
    await machine.spawn("work", purpose="main shell")
    result = await machine.tool_run("work", "echo hello world")
    assert "exit:0" in result
    assert "hello world" in result
    result = await machine.tool_run("work", "false")
    assert "exit:1" in result
    result = await machine.tool_run("work", "bash -c 'exit 42'")
    assert "exit:42" in result


async def test_run_output_is_clean_of_prompt_and_echo(machine):
    await machine.spawn("work", purpose="clean output")
    result = await machine.tool_run("work", "printf 'AAA\\nBBB\\n'")
    body = result.split("\n", 1)[1]
    assert "AAA" in body and "BBB" in body
    assert "printf" not in body  # command echo excluded (C..D capture)
    assert "133" not in body  # marks never leak


async def test_timeout_keeps_running_then_interrupt(machine):
    await machine.spawn("work", purpose="slow job")
    result = await machine.tool_run("work", "sleep 30", timeout=0.4)
    assert "still running" in result
    console = machine.consoles["work"]
    assert console.state is TermState.BUSY
    with pytest.raises(Exception, match="BUSY"):
        await console.run("echo nope")
    await machine.tool_press("work", ["C-c"])
    assert await wait_for(lambda: console.state is TermState.IDLE)
    assert console.last_exit == 130
    result = await machine.tool_run("work", "echo recovered")
    assert "recovered" in result


async def test_python_repl_via_send(machine):
    await machine.spawn("repl", purpose="python repl")
    result = await machine.tool_run("repl", f"{sys.executable} -q -u", timeout=0.8)
    assert "still running" in result
    console = machine.consoles["repl"]
    out = await machine.tool_send("repl", "6*7")
    for _ in range(20):
        if "42" in out:
            break
        await asyncio.sleep(0.1)
        out += await machine.tool_peek("repl")
    assert "42" in out
    await machine.tool_press("repl", ["C-d"])
    assert await wait_for(lambda: console.state is TermState.IDLE)


async def test_pdb_detected_awaiting(machine, tmp_path):
    # DESIGN §11 layer 1: pdb's readline prompt turns echo off, which the
    # structural classifier reads straight from the pty — no pattern matching.
    script = tmp_path / "prog.py"
    script.write_text("x = 1\nprint('finished', x)\n")
    await machine.spawn("dbg", purpose="pdb session")
    console = machine.consoles["dbg"]
    result = await machine.tool_run(
        "dbg", f"{sys.executable} -m pdb {script}", timeout=0.8
    )
    assert "still running" in result
    assert await wait_for(lambda: console.state is TermState.AWAITING, timeout=8.0)
    status = machine.render_status()
    assert "AWAIT" in status
    await machine.tool_send("dbg", "c")  # run to completion; pdb re-prompts
    await machine.tool_send("dbg", "q")
    assert await wait_for(lambda: console.state is TermState.IDLE)


async def test_wait_for_server_banner_and_peek(machine):
    await machine.spawn("server", purpose="http server", long_running=True)
    await machine.tool_run(
        "server",
        f"{sys.executable} -u -m http.server 0 --bind 127.0.0.1",
        timeout=0.6,
    )
    result = await machine.tool_wait("server", pattern="Serving HTTP", timeout=15)
    assert "[wait: pattern" in result
    assert "Serving HTTP" in result
    await machine.tool_press("server", ["C-c"])


async def test_long_running_unexpected_exit_screams(machine):
    await machine.spawn("daemon", purpose="crashy daemon", long_running=True)
    console = machine.consoles["daemon"]
    await machine.tool_send("daemon", "bash -c 'echo boom; exit 3'")
    assert await wait_for(lambda: console.state is TermState.EXITED)
    assert console.last_exit == 3
    assert "boom" in console.crash_tail
    status = machine.render_status()
    assert "EXITED💥" in status
    assert "boom" in status
    # next command acknowledges the crash: console usable again
    result = await machine.tool_run("daemon", "echo back")
    assert "back" in result
    assert console.state is TermState.IDLE


async def test_rotation_survives_and_cursor_maps(machine):
    machine.raw_max_bytes = 4096
    await machine.spawn("big", purpose="rotation test")
    result = await machine.tool_run("big", "seq 1 3000", max_output=2000)
    assert "exit:0" in result
    assert "omitted" in result
    assert "3000" in result  # tail preserved
    session_dir = machine.dir
    assert (session_dir / "f1-big.raw.1").exists()
    machine._write_state_now()
    state = read_state(session_dir / "state.json")
    cursor = state["consoles"]["big"]["agent_cursor"]
    assert cursor["file"].startswith("f1-big.raw")
    result = await machine.tool_run("big", "echo after-rotation")
    assert "after-rotation" in result


async def test_kill_ladder_stops_sigint_immune_process(machine):
    await machine.spawn("stubborn", purpose="ignores sigint")
    console = machine.consoles["stubborn"]
    await machine.tool_run(
        "stubborn", "bash -c 'trap \"\" INT; sleep 60'", timeout=0.4
    )
    assert console.state is TermState.BUSY
    result = await machine.tool_kill("stubborn")
    assert console.state in (TermState.IDLE, TermState.EXITED)
    assert "C-c" in result


async def test_kill_whole_console_unregisters(machine):
    await machine.spawn("doomed", purpose="short lived")
    result = await machine.tool_kill("doomed", whole_console=True)
    assert "destroyed" in result
    assert "doomed" not in machine.consoles
    with pytest.raises(Exception, match="no console named"):
        await machine.tool_run("doomed", "echo nope")


async def test_status_block_and_state_json(machine):
    await machine.spawn("one", purpose="first console")
    await machine.spawn("two", purpose="second console")
    await machine.tool_run("one", "echo hi")
    status = machine.render_status()
    assert "── altf:" in status
    assert "f1·one" in status and "f2·two" in status
    assert "last:`echo hi` exit:0" in status
    machine._write_state_now()
    state = read_state(machine.dir / "state.json")
    assert state["version"] == 1
    assert state["spawner"] == "local"
    assert state["consoles"]["one"]["slot"] == "f1"
    assert state["consoles"]["one"]["shell_pid"] is not None
    full = await machine.tool_status()
    assert "hint:" in full


async def test_watchers_surface_events(machine):
    await machine.spawn("logs", purpose="watched console")
    machine.watch("logs", r"ERROR|Traceback", label="errors")
    await machine.tool_run("logs", "echo 'ERROR: disk full'")
    status = machine.render_status()
    assert "⚠" in status
    assert "disk full" in status
    # events clear after rendering once
    assert "⚠" not in machine.render_status()


async def test_screen_renders_current_view(machine):
    await machine.spawn("scr", purpose="screen test")
    await machine.tool_run("scr", "printf 'TOP\\nBOTTOM\\n'")
    view = await machine.tool_screen("scr")
    assert "TOP" in view and "BOTTOM" in view
    console = machine.consoles["scr"]
    unread_before = console.unread
    await machine.tool_screen("scr")
    assert console.unread == unread_before  # screen never consumes


async def test_send_enter_false_then_press_enter(machine):
    await machine.spawn("typed", purpose="typing test")
    await machine.tool_send("typed", "echo par", enter=False)
    await machine.tool_send("typed", "tial", enter=False)
    console = machine.consoles["typed"]
    assert console.state is TermState.IDLE  # nothing submitted yet
    out = await machine.tool_press("typed", ["Enter"])
    assert await wait_for(lambda: console.state is TermState.IDLE and console.last_exit == 0)
    out += await machine.tool_peek("typed")  # press drains within its settle window
    assert "partial" in out
