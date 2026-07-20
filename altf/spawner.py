"""Execution environments. The ONLY module allowed to know where shells run.

Everything above this (stream, console, machine, tools, viewer) operates on
ptys, bytes, and environment-local pids, identically across backends.
"""

from __future__ import annotations

import asyncio
import os
from typing import Protocol, Sequence, runtime_checkable

from ptyprocess import PtyProcess

DEFAULT_DIMENSIONS = (50, 120)  # (rows, cols) — a fullscreen-terminal-ish grid


@runtime_checkable
class Spawner(Protocol):
    def spawn(self, argv_shell: list[str], cwd: str | None, env: dict) -> PtyProcess:
        """Start a shell under a local pty, running in the target environment."""
        ...

    async def out_of_band(self, argv: list[str]) -> tuple[int, str]:
        """Run a helper command in the SAME environment/pid-namespace as the
        consoles (e.g. `ps`, `kill`) WITHOUT going through any console's pty."""
        ...

    def describe(self) -> str:
        """Short label for status headers / state.json, e.g. 'local', 'docker:devbox'."""
        ...


async def _run_local(argv: Sequence[str]) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode("utf-8", "replace")
    except FileNotFoundError as exc:
        return 127, str(exc)
    except OSError as exc:
        return 126, str(exc)


class LocalSpawner:
    """Spawns the shell directly on the host. The default, the test substrate,
    and a fully supported production mode (including "harness already runs
    inside the container", which is just LocalSpawner from inside)."""

    # The local pty IS the target environment's pty, so its termios reflect
    # what the foreground program set (echo-off detection works, DESIGN §11).
    pty_reflects_termios = True

    def __init__(self, dimensions: tuple[int, int] = DEFAULT_DIMENSIONS) -> None:
        self.dimensions = dimensions

    def spawn(self, argv_shell: list[str], cwd: str | None, env: dict) -> PtyProcess:
        return PtyProcess.spawn(
            list(argv_shell), cwd=cwd, env=dict(env), dimensions=self.dimensions
        )

    async def out_of_band(self, argv: list[str]) -> tuple[int, str]:
        return await _run_local(argv)

    def describe(self) -> str:
        return "local"


class DockerExecSpawner:
    """Consoles run inside an existing container via `docker exec -it`.

    Hardened per DESIGN §4: the local docker-exec client is only a conduit —
    killing it does NOT kill in-container processes, so every real kill in the
    §12 ladder and in cleanup goes out-of-band (`docker exec <container> kill
    ...`), and client termination is last and cosmetic. `docker=` may point at
    any docker-CLI-compatible binary (e.g. podman).
    """

    # The local pty belongs to the docker client, which keeps it raw/echo-off
    # unconditionally — it says nothing about the in-container terminal, so
    # termios-based AWAITING hints must not be read from it (DESIGN §11).
    pty_reflects_termios = False

    def __init__(
        self,
        container: str,
        shell: str = "bash",
        docker: str = "docker",
        exec_args: Sequence[str] = (),
        dimensions: tuple[int, int] = DEFAULT_DIMENSIONS,
    ) -> None:
        self.container = container
        self.shell = shell
        self.docker = docker
        self.exec_args = tuple(exec_args)
        self.dimensions = dimensions

    def spawn(self, argv_shell: list[str], cwd: str | None, env: dict) -> PtyProcess:
        argv = [self.docker, "exec", "-i", "-t"]
        if cwd:
            argv += ["-w", cwd]
        term = env.get("TERM", "xterm-256color")
        argv += ["-e", f"TERM={term}", *self.exec_args, self.container]
        argv += list(argv_shell) if argv_shell else [self.shell]
        # env here configures the local docker *client* (auth, socket); the
        # in-container environment is what `docker exec` provides plus -e flags.
        return PtyProcess.spawn(
            argv, env=dict(os.environ), dimensions=self.dimensions
        )

    async def out_of_band(self, argv: list[str]) -> tuple[int, str]:
        return await _run_local([self.docker, "exec", self.container, *argv])

    def describe(self) -> str:
        return f"docker:{self.container}"
