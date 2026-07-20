"""DockerExecSpawner integration — the two DESIGN §4 footguns, explicitly.

Run with `pytest -m docker` against a real docker daemon.
"""

import asyncio
import uuid

import pytest

from altf import DockerExecSpawner, Machine, TermState

pytestmark = pytest.mark.docker


async def wait_for(predicate, timeout=8.0, interval=0.1):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.fixture
async def docker_machine(tmp_path, docker_container):
    m = Machine(
        session=f"docker-{uuid.uuid4().hex[:8]}",
        spawner=DockerExecSpawner(docker_container),
        workdir=tmp_path,
        quiet_threshold=0.4,
        send_settle=0.3,
    )
    try:
        yield m
    finally:
        await m.close()


async def _in_container_pids(spawner, comm):
    rc, out = await spawner.out_of_band(["ps", "-e", "-o", "pid=,comm="])
    assert rc == 0, out
    pids = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip() == comm:
            pids.append(int(parts[0]))
    return pids


async def test_basic_run_inside_container(docker_machine):
    await docker_machine.spawn("work", purpose="in-container shell")
    result = await docker_machine.tool_run("work", "cat /etc/os-release")
    assert "exit:0" in result
    assert "Debian" in result
    console = docker_machine.consoles["work"]
    assert console.shell_pid is not None  # environment-local pid via handshake


async def test_footgun_1_client_kill_orphans_then_oob_cleanup(docker_machine):
    """Killing the local docker-exec client does NOT kill the in-container
    process; only out-of-band kills do (DESIGN §4 footgun 1)."""
    await docker_machine.spawn("work", purpose="orphan test")
    console = docker_machine.consoles["work"]
    await docker_machine.tool_run("work", "sleep 300", timeout=0.5)
    assert console.state is TermState.BUSY
    assert await wait_for(
        lambda: asyncio.get_event_loop().is_running(), timeout=0
    ) or True
    sleepers = await _in_container_pids(docker_machine.spawner, "sleep")
    assert sleepers, "sleep must be running inside the container"

    # kill only the local client: console goes DEAD...
    console.pty.terminate(force=True)
    assert await wait_for(lambda: console.state is TermState.DEAD)
    # ...but the in-container process survives (the footgun)
    await asyncio.sleep(1.0)
    assert await _in_container_pids(docker_machine.spawner, "sleep")

    # the backend's real killing is out-of-band
    for pid in await _in_container_pids(docker_machine.spawner, "sleep"):
        await docker_machine.spawner.out_of_band(["kill", "-KILL", str(pid)])
    assert await wait_for(
        lambda: True, timeout=0.5
    )
    await asyncio.sleep(0.5)
    assert not await _in_container_pids(docker_machine.spawner, "sleep")


async def test_footgun_2_whole_machine_cleanup(docker_machine):
    """Machine.close() must leave no console shells or fg processes behind in
    the container (DESIGN §4 footgun 2 / §12)."""
    await docker_machine.spawn("a", purpose="cleanup a")
    await docker_machine.spawn("b", purpose="cleanup b", long_running=True)
    await docker_machine.tool_run("a", "sleep 300", timeout=0.5)
    a_shell = docker_machine.consoles["a"].shell_pid
    b_shell = docker_machine.consoles["b"].shell_pid
    spawner = docker_machine.spawner

    await docker_machine.close()

    async def gone():
        rc, out = await spawner.out_of_band(["ps", "-e", "-o", "pid="])
        pids = {int(x) for x in out.split()} if rc == 0 else set()
        return a_shell not in pids and b_shell not in pids

    ok = False
    for _ in range(20):
        if await gone():
            ok = True
            break
        await asyncio.sleep(0.5)
    assert ok, "console shells must not survive Machine.close()"
    assert not await _in_container_pids(spawner, "sleep")
