import shutil
import subprocess
import uuid

import pytest
import pytest_asyncio

from altf import LocalSpawner, Machine


@pytest_asyncio.fixture
async def machine(tmp_path):
    m = Machine(
        session=f"test-{uuid.uuid4().hex[:8]}",
        spawner=LocalSpawner(),
        workdir=tmp_path,
        quiet_threshold=0.4,
        send_settle=0.3,
        press_settle=0.2,
    )
    try:
        yield m
    finally:
        await m.close()


def docker_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        return (
            subprocess.run(
                [docker, "info"], capture_output=True, timeout=10
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(scope="session")
def docker_container():
    if not docker_available():
        pytest.skip("docker daemon not available")
    name = f"altf-test-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "debian:stable-slim",
         "sleep", "infinity"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if run.returncode != 0:
        pytest.skip(f"cannot start test container: {run.stderr.strip()}")
    try:
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
