# altf

Python library giving an LLM agent N named pty-backed consoles (like a developer at
a Linux machine with Alt+F1..F6), with a pydantic-ai toolset, file-based logs, and a
read-only `altf watch` observer. Execution environment is pluggable via the
`Spawner` protocol — local by default, Docker as a first-class hardened backend;
nothing outside `spawner.py` may know which is in use.

**Read DESIGN.md before writing any code.** It is the authoritative spec: settled
decisions (with rationale — do not relitigate, especially raw-pty-over-tmux and
OSC 133), exact tool signatures, state machine, on-disk formats, and open questions.
When you resolve an item in DESIGN.md §18 (OPEN questions), record the decision
there.

Conventions: async throughout; hard deps only `ptyprocess` + `pyte`; `pydantic-ai`,
`textual`, and the LLM classifier are optional extras; `osc.py` is the only
hand-written escape parser (pure, incremental, fuzz-tested — all other terminal
parsing is delegated to pyte); integration tests use `LocalSpawner` + real bash,
docker tests behind `-m docker`.

## Install

```bash
uv add altf                    # core: ptyprocess + pyte only
uv add 'altf[watch]'           # + Textual observer TUI
uv add 'altf[pydantic-ai]'     # + pydantic-ai toolset
```

(Any PEP 517 installer works — `pip install 'altf[watch]'` is equivalent.)

## Demo

Clone it and watch three live consoles at work:

```bash
git clone https://github.com/gregjhogan/AltF && cd AltF
uv sync --extra watch

# terminal 1 — drives the consoles in a loop (Ctrl-C stops and cleans up):
uv run demo.py

# terminal 2 — read-only observer:
uv run altf watch /tmp/altf-demo/demo
# or follow one console:  uv run altf tail /tmp/altf-demo/demo server
# or zero-install:        tail -f /tmp/altf-demo/demo/f1-server.raw
```

The demo runs a real `http.server` on `f1`, polls it from `f2` (including a
404 that trips a `machine.watch` pattern — watch the `⚠` lines), and drives a
python REPL on `f3` via `send()`. Every few rounds it presses `C-c` on the
server so you can watch the status block scream `EXITED💥` and recover.

## Quickstart (bare, no agent framework)

```python
import asyncio
from altf import Machine, LocalSpawner

async def main():
    machine = Machine(session="devbox", spawner=LocalSpawner(), workdir="/tmp/altf")
    try:
        await machine.spawn("server", purpose="dev server :8000", long_running=True)
        await machine.spawn("work", purpose="main shell")

        print(await machine.tool_run("work", "echo hello"))
        # start a server on f1, don't wait for it to finish
        print(await machine.tool_run("server", "python3 -m http.server 8000",
                                     timeout=1.0))       # times out -> stays BUSY
        print(await machine.tool_wait("server", pattern="Serving HTTP"))
        print(await machine.tool_run("work", "curl -s localhost:8000 | head"))
        print(machine.render_status())
    finally:
        await machine.close()

asyncio.run(main())
```

## With pydantic-ai

```python
from pydantic_ai import Agent
from altf import Machine, DockerExecSpawner
from altf.pydantic_ai import altf_toolset, status_refresher

machine = Machine(session="devbox", spawner=DockerExecSpawner("devbox"),
                  workdir="/run/altf")
agent = Agent("anthropic:claude-opus-4-8",
              toolsets=[altf_toolset(machine)],
              history_processors=[status_refresher(machine)])
```

Every tool result is prefixed with a live status block for all consoles, so the
model can never forget a crashed server or a console blocked on input.

## Watching what the agent does

Everything is observable through files under `<workdir>/<session>/` — the
`workdir` you passed to `Machine` (no default; you say where bytes go). Log
files grow unbounded by default; pass `Machine(raw_max_bytes=...)` to opt into
size-capped rotation. With `workdir="/run/altf"`:

```bash
altf watch /run/altf/devbox        # read-only TUI: F1..F9/digits switch, Tab cycles
altf ls    /run/altf/devbox        # one-line-per-console summary from state.json
altf tail  /run/altf/devbox server # follow one console's raw byte stream

# zero-install fallbacks — any terminal replays a console faithfully:
tail -f /run/altf/devbox/f1-server.raw
grep ERROR /run/altf/devbox/f1-server.log
```

The observer opens files only — no fds, pids, or sockets shared with the harness;
it is read-only by construction and safe to run many times.

## Testing

```bash
uv sync                        # installs the dev dependency group
uv run pytest                  # unit + LocalSpawner/bash integration tests
uv run pytest -m docker        # docker backend tests (needs a docker daemon)
```
