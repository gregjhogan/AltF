# altf — DESIGN

`altf` gives a single LLM agent a Linux machine the way a human developer has one:
several virtual consoles (think Alt+F1..F6), switchable by name, each running a real
shell on a real pty. *Where* those shells run is pluggable via the `Spawner` protocol
— the local machine, a Docker container (first-class, fully supported), or anything
else that can exec a shell (SSH, podman, kubectl exec are additive later). The agent
runs a dev server on one console, fires curl at it from another, drives gdb
interactively on a third — and a human can watch everything live, read-only, through
log files.

This document is the authoritative spec. Decisions below were deliberated at length;
their rationale is recorded so they are not relitigated. Sections marked **OPEN** are
genuinely undecided.

---

## 1. Core model and non-goals

- **One agent, N consoles.** This is NOT a multi-agent system. It models one developer
  with multiple terminal windows. No sub-agent coordination anywhere in this library.
  (A sub-agent may later be *handed* the same console registry, but that is out of scope.)
- **The harness owns all state.** LLMs forget; the library must make forgetting
  structurally impossible by re-presenting state mechanically (see Status Block, §7).
  The model's job is deciding what to do about state, never warehousing it.
- **Liveness is pushed, not remembered.** State changes (crashes, input-waits, new
  output) are surfaced to the model in-band with tool results, not left for it to poll.
- Non-goals: window layout/splitting, human *input* through the viewer (observer is
  read-only by construction), session persistence across harness restarts (v1; see §15).

## 2. Settled decisions and rationale

| Decision | Choice | Why (short) |
|---|---|---|
| Substrate | **Raw pty** (`ptyprocess`), not tmux | Push-based reads are native (select on master fd); exact byte-stream ownership makes unread-byte accounting trivial; no external server to health-check; no tmux-CLI parsing. We knowingly gave up tmux's harness-crash survivability and `tmux attach` observability; observability is recovered via log-first design (§9–10), survivability deferred (§15). |
| Completion signaling | **OSC 133 shell-integration marks** (FinalTerm protocol), in-band | Prompt-return + exit codes arrive in-order and race-free in the same stream as output. Strictly better than out-of-band FIFO/`wait-for` designs: no lost-signal race, no sequence bookkeeping. Same mechanism VS Code / WezTerm / iTerm2 use. |
| Execution environment | `Spawner` protocol; ship `LocalSpawner` + `DockerExecSpawner` in v1, both first-class | The library is environment-agnostic; nothing outside `spawner.py` may know or care whether shells run locally or in a container. Docker gets dedicated support (its footguns are real, §4) but is one backend, not the identity of the library. The protocol also keeps "harness runs inside the container" and future SSH/podman/kubectl backends config changes, not surgery. |
| Interrupt/control keys | Keystrokes through the pty (`press("C-c")` → byte 0x03), NOT host-side signals | Ctrl-C ≠ SIGINT for raw-mode apps (gdb uses it to break the inferior; vim to abort). Sending the keystroke is always the semantically-human action. Host-side `kill` exists only as an escalation fallback (§12). |
| `press` vs `send` | Separate tools | Typing text and pressing special keys are different intents; merging them creates escaping bugs. `press` uses tmux-style key names (`C-c`, `Up`, `Tab`…) because models already know that vocabulary. |
| `peek` vs `screen` | Separate tools | `peek` = "what's new?" (consumes the unread cursor, stream semantics). `screen` = "what does it look like?" (pyte-rendered current screen, does NOT advance cursor). TUIs need the latter; logs need the former. |
| `run` timeout | Returns control, does **not** kill | Mirrors a human glancing at a slow build. Console goes BUSY; model chooses wait/interrupt. |
| Name | Package `altf`; class `Console`; registry `Machine` | Evokes Alt+F# virtual-console switching. Consoles get both a name and an `f#` slot (`f1·server`). |
| Logs | Two files per console: `.raw` (exact bytes) + `.log` (ANSI-stripped) | Different consumers: raw replays faithfully in any real terminal / pyte; stripped is greppable by model, human, and tools. |
| Viewer | Standalone read-only `altf watch` app over the log files | Observation via files works remotely, after-the-fact, and is read-only by construction. |

## 3. Repository / module layout

```
altf/
  __init__.py        # public API re-exports
  spawner.py         # Spawner protocol; DockerExecSpawner, LocalSpawner
  stream.py          # per-console reader: pty fd -> logs, pyte screen, OSC marks;
                     #   .log text extraction = gridless pyte listener (§18.8)
  osc.py             # incremental OSC 133 scanner (pure, unit-testable)
  console.py         # Console: state machine, cursor math, run/send/press/peek/wait/screen/kill
  machine.py         # Machine: registry, status renderer, event queue, watchers, state.json
  classify.py        # InputStateClassifier protocol; LLMClassifier (see §11/§18.6)
  logs.py            # log writers, opt-in rotation, checkpoint sidecars, state.json schema
  pydantic_ai.py     # toolset factory + history processor
  watch/             # the observer TUI (Textual or curses), `python -m altf.watch`
  cli.py             # `altf watch`, `altf ls`, `altf tail <console>` entry points
tests/
pyproject.toml       # deps: ptyprocess, pyte; optional: textual (watch), pydantic-ai
DESIGN.md            # this file
CLAUDE.md            # 2 lines pointing here
```

Hard dependencies: `ptyprocess`, `pyte`. Everything else optional extras
(`altf[watch]`, `altf[pydantic-ai]`, `altf[llm-classifier]`).

## 4. Spawner protocol (execution environments)

The Spawner is the ONLY module allowed to know where shells actually run. Everything
above it (`stream`, `console`, `machine`, tools, viewer) operates on ptys, bytes, and
environment-local pids, and must work identically across backends. Enforce this in
review: no `docker` string outside `spawner.py` and docker-marked tests.

```python
class Spawner(Protocol):
    def spawn(self, argv_shell: list[str], cwd: str | None, env: dict) -> PtyProcess:
        """Start a shell under a local pty, running in the target environment."""
    async def out_of_band(self, argv: list[str]) -> tuple[int, str]:
        """Run a helper command in the SAME environment/pid-namespace as the
        consoles (e.g. `ps`, `kill`) WITHOUT going through any console's pty."""
    def describe(self) -> str:
        """Short label for status headers / state.json, e.g. 'local', 'docker:devbox'."""
```

Backend contract notes:
- All pids handled by the library (shell pid handshake §5, `ps` output, kill targets)
  are *environment-local* pids as seen by `out_of_band` — the pid namespace of
  `spawn` and `out_of_band` must match. Each backend guarantees this internally.
- Cleanup semantics differ per backend; `Machine.close()` uses only the generic
  sequence (graceful: `C-c` if busy, then `exit`/`C-d`; forceful: out-of-band kill by
  pid; finally terminate the local client/child). Backends make that sequence correct.

**`LocalSpawner()`** — spawns the shell directly on the host. The default, the test
substrate, and a fully supported production mode (including "harness already runs
inside the container", which is just LocalSpawner from the container's perspective).

**`DockerExecSpawner(container, shell="bash", docker="docker", exec_args=())`** —
spawns `docker exec -it <container> <shell>` under a local pty; `out_of_band` runs
`docker exec <container> <argv...>` (no tty). First-class, hardened backend; its
known footguns are handled here, invisibly to the rest of the library (tests
required, `-m docker`):
  1. Killing the local `docker exec` client does NOT kill the process inside the
     container ⇒ this backend's cleanup and the §12 ladder's out-of-band steps do
     the real killing in-container; client termination is last and cosmetic.
  2. Harness death ⇒ client death ⇒ in-container bash gets SIGHUP ⇒ consoles die.
     Acceptable v1 limitation; documented; see §15.
  3. `docker` CLI may be podman-compatible; `docker=` parameter makes that a
     config choice, not a new backend (a native PodmanSpawner remains possible later).

The shell's environment-local pid is captured at spawn via the OSC handshake (§5)
and stored on the Console; it anchors out-of-band process inspection
(`ps -o pid,ppid,stat,comm` filtered by ancestor) and escalated kills for every
backend.

## 5. Shell integration: OSC 133 (+ altf handshake)

At spawn, before handing the console to the agent, the harness types an init line
(sourced from a heredoc or a tiny file bind-mounted/`docker cp`'d in — implementer's
choice, but it must not depend on the container image being pre-provisioned):

```bash
PS1='\[\e]133;A\a\]'"${PS1:-\\u@\\h:\\w\\$ }"   # A = prompt start
PS0='\[\e]133;C\a\]'                              # C = command output begins
PROMPT_COMMAND='printf "\e]133;D;%s\a" "$?"'      # D;<exit> = command finished
printf '\e]7770;pid=%s\a' "$$"                    # altf handshake: shell pid (in-container)
```

- The `stream.py` reader parses these incrementally (`osc.py`) as it appends bytes.
- Semantics used by `console.py`:
  - `D;<exit>` → foreground command done, exit code known, state → IDLE.
  - Bytes between `C` and the next `D` are *exactly* the command's output — used for
    clean `run()` output extraction and truncation boundaries.
  - `A` after `D` confirms the prompt is being drawn (secondary IDLE confirmation).
  - OSC `7770` is the altf private channel (pid handshake now; extensible later).
- The init line itself and its echo are consumed/suppressed from what the model sees.
- Non-bash shells: out of scope v1 (bash only, documented); the protocol is
  shell-agnostic so zsh/fish adapters are additive later.
- Robustness: the OSC scanner must tolerate marks split across read() chunks, and
  garbage/partial sequences (never crash on malformed input; pass bytes through).

## 6. Console state machine

```python
class TermState(str, Enum):
    IDLE = "idle"           # at shell prompt (last mark was D, prompt drawn)
    BUSY = "busy"           # foreground process running / producing output
    AWAITING = "awaiting"   # running but judged blocked on user input
    EXITED = "exited"       # UNEXPECTED death of a long_running console's fg process
    DEAD = "dead"           # pty/child gone (docker exec client died, shell exited)
```

Transitions:

| From | Event | To |
|---|---|---|
| IDLE | command written via `run`/`send` | BUSY |
| BUSY | OSC `D;<exit>` | IDLE — *unless* console was spawned `long_running=True` and exit was not requested via `kill()`: then **EXITED** (loud) |
| BUSY | quiet ≥ `quiet_threshold` (default 2.0s) AND classifier says waiting | AWAITING |
| AWAITING | any new output ≥ 1 byte | BUSY (re-evaluate) |
| any | pty EOF / child reaped | DEAD |
| EXITED | new `run`/`send` (model restarts something) | BUSY |

- EXITED is a *presentation* state: the shell is actually at a prompt and usable; the
  state exists so the status block screams about it (crash tail inline, §7) until the
  model acknowledges it by issuing the next command on that console.
- AWAITING detection is layered (cheap → expensive), see §11.

## 7. Status block (the forgetting-proofing) — REQUIRED ON EVERY TOOL RESULT

Rendered by `Machine.render_status()`, prepended to **every** tool return value:

```
── altf: devbox ─────────────────────────────────────────────
f1·server  BUSY⚡   node     unread:2.1KB  quiet:34s  up:12m   "dev server :3000"
f2·work    IDLE     bash     last:`curl :3000/users` exit:0    "main shell"
f3·gdb     AWAIT✋  gdb      unread:212B   quiet:8s            "segfault hunt"
f4·build   EXITED💥 exit:2 3m ago  tail:"error: use of undeclared identifier 'foo'"
─────────────────────────────────────────────────────────────
```

Per-console fields: slot·name, state (+glyph), foreground command, **unread bytes**
(human-formatted), quiet time, uptime (long_running only), last command + exit code
(IDLE only), purpose (truncated ~30 chars). EXITED rows render the crash tail
(last non-empty stripped line) inline. High-severity queued events (§13) add a
`⚠ ...` line under the rule. Budget: ≈ one line / ~15 tokens per console.

Foreground command: derived lazily via `Spawner.out_of_band(ps ...)` keyed off the
shell pid, cached, refreshed opportunistically (on state change + max every few s
while any console is BUSY) — never a hot poll loop.

## 8. Tool surface (exact signatures — the LLM-facing API)

All tools are `async`, return `str` (status block + "\n\n" + payload). Docstrings
below are the actual tool descriptions the model sees; keep them.

```python
async def spawn(name: str, purpose: str, cwd: str | None = None,
                long_running: bool = False) -> str
    """Create a new console (like opening a terminal window). `purpose` is shown in
    every status block — write it for your future self. Set long_running=True for
    servers/daemons so an unexpected exit is flagged loudly."""

async def run(name: str, command: str, timeout: float = 60.0,
              max_output: int = 8000) -> str
    """Run a command that terminates, on an IDLE console. Blocks until the prompt
    returns or `timeout`. On timeout the command KEEPS RUNNING (console goes busy;
    use peek/wait/press). Refused with guidance if the console is BUSY/AWAITING.
    Returns the command's output (head+tail truncated to max_output) and exit code."""

async def send(name: str, text: str, enter: bool = True) -> str
    """Type text into whatever is running (REPL, gdb, y/n prompt, password).
    Returns quickly with any output produced within ~1s."""

async def press(name: str, keys: list[str]) -> str
    """Press special keys: 'C-c','C-d','C-z','Up','Down','Left','Right','Tab',
    'Enter','Escape','Space','Backspace','PageUp','PageDown','Home','End','F1'..'F12'.
    Use for interrupting, EOF, shell history, TUI navigation."""

async def peek(name: str, max_bytes: int = 4000) -> str
    """Read output you haven't seen yet (advances your read cursor). Non-blocking.
    Use to check on servers/long jobs."""

async def wait(name: str, pattern: str | None = None, timeout: float = 30.0) -> str
    """Block until `pattern` (regex) appears in NEW output, or the prompt returns,
    or the console starts waiting for input — whichever first. THE tool for
    'start the server, wait until it says Listening'."""

async def screen(name: str) -> str
    """Render the console's current visible screen (what a human would see).
    Use for TUIs (gdb TUI, vim, htop). Does not consume unread output."""

async def kill(name: str, whole_console: bool = False) -> str
    """Stop the foreground process (escalating interrupt). whole_console=True
    destroys the console entirely."""

async def status() -> str
    """Full machine status: all consoles, purposes, states, recent history."""
```

Behavioral requirements:

- **Truncation** (`run`, large `peek`): head 20 lines + `[... N bytes omitted — use
  peek(), or grep /run/altf/<session>/f4-build.log via run() on another console ...]`
  + tail 60 lines. Always include the on-disk log path: the model grepping its own
  transcript is a supported, encouraged pattern.
- **Sequencing guard**: `run` on a non-IDLE console fails fast with a message naming
  the foreground process and suggesting send/press/another console. This converts the
  most common model error into a self-correcting turn.
- **Per-console asyncio lock**: concurrent tool calls on one console serialize.
- `wait` with `pattern=None` on a `long_running` console means "until AWAITING or
  quiet ≥ threshold" (readiness probing without a known banner).
- Unread cursor is **per (file, offset)** pair (rotation-safe, §10) and advances only
  via `peek`/`run`-output delivery; `screen`/status never consume.

## 9. On-disk layout & state.json

```
<workdir>/<session>/   # workdir is explicit — Machine has NO default location (§18.7)
  state.json           # atomic rewrite (tmp+rename) on every state/registry change
  f1-server.raw        # exact pty byte stream (escape codes included)
  f1-server.log        # ANSI-stripped, line-oriented (line-buffered writes, real-time)
  f1-server.ckpt       # checkpoint sidecar (§10)
  ...
```

(Examples throughout this document use `/run/altf` as the workdir; that is a
caller's choice, not a default.)

`state.json` schema (versioned):

```json
{ "version": 1, "session": "devbox", "spawner": "docker:devbox",
  "consoles": { "server": {
      "slot": "f1", "purpose": "dev server :3000", "state": "busy",
      "long_running": true, "shell_pid": 137, "fg_command": "node",
      "spawned_at": "...", "last_output_at": "...",
      "last_cmd": null, "last_exit": null,
      "raw_file": "f1-server.raw", "log_file": "f1-server.log",
      "raw_bytes": 1048576, "agent_cursor": {"file": "f1-server.raw", "offset": 1046427}
  }}}
```

This file is the contract with the viewer and any third-party tooling: **treat
`.raw`/`.log`/`state.json` semantics as public API.**

## 10. Rotation & checkpoints

- Rotation is **opt-in** (§18.7): by default log files grow without limit and are
  never rotated or deleted. `Machine(raw_max_bytes=N)` enables size-capped
  rotation per console (`f1-server.raw.1`, etc.). All offsets in state.json are
  (file, offset) pairs; unread-byte math and the model's grep trick must survive
  rotation when it is enabled.
- **Checkpoints** (for the viewer): every N MB of raw output, `stream.py` snapshots
  its pyte screen + parser state into `f1-server.ckpt` (append-only records:
  `{raw_file, offset, screen_dump}`). The viewer seeks to the latest checkpoint ≤
  target offset and replays forward, instead of replaying gigabytes — and
  alternate-screen apps render correctly from mid-stream.

## 11. AWAITING-input classification

```python
class InputStateClassifier(Protocol):
    async def classify(self, tail_text: str, seconds_quiet: float,
                       screen_text: str) -> Literal["awaiting", "working", "odd_exit"]: ...
```

Layered, cheap-first — with **no pattern lists anywhere** (revised, see §18.6):

1. **Structural terminal facts (always on, free, deterministic):** quiet ≥ threshold
   AND (the pty is in echo-off/raw mode — which is how password prompts, readline
   REPLs (python, pdb, gdb) and curses apps actually present — OR the alternate
   screen is active). These are mechanical facts read from the terminal itself,
   not guesses. Termios is only meaningful when the local pty IS the target
   environment's pty; spawners advertise this via `Spawner.pty_reflects_termios`
   (True for LocalSpawner; False for DockerExecSpawner, whose local fd is the
   docker client's always-raw pty — an out-of-band `stty -a` probe is the additive
   path if a backend needs echo-off detection later).
2. **LLMClassifier (optional, recommended):** a small fast model (default
   `anthropic:claude-haiku-4-5-20251001`) judges everything the structural layer
   cannot see (echo-on y/n prompts, docker consoles). Sees last ~30 stripped lines
   + `screen()` text. **Cache per (console, cursor offset)** — fires at most once
   per quiet period. Constructor takes a pydantic-ai model name or any (a)sync
   callable; keep the dep optional.

pdb (always available in CI) must be caught by layer 1 under LocalSpawner via
echo-off — regression-tested without any pattern matching.

## 12. Kill escalation ladder

`kill(name)`:
1. `press C-c`; wait ~1.5s for `D` mark.
2. `press C-c` again; wait ~1.5s.
3. Out-of-band (via `Spawner.out_of_band`, i.e. inside the target environment):
   find fg process group under `shell_pid`, `kill -TERM -<pgid>`; wait ~2s.
4. `kill -KILL -<pgid>`.

`kill(name, whole_console=True)`: steps above, then `send "exit"` / `press C-d` to
the shell, then terminate the docker-exec client pty; unregister; final log flush;
state.json update. Rationale for pty-first ordering is in §2 (Ctrl-C ≠ SIGINT).

## 13. Events & watchers

- `Machine` keeps an event queue: console crashed (EXITED), entered AWAITING,
  watcher pattern hit, console DEAD.
- `machine.watch(name, regex, label=None, severity="high")` — harness-side pattern
  watcher over the stripped stream (e.g. `ERROR|panic|Traceback`). Hits enqueue events.
- High-severity events render as `⚠` lines in the next status block (whatever tool
  the model calls next), then clear. Nothing waits for the model to think to poll.

## 14. pydantic-ai integration (`altf/pydantic_ai.py`)

```python
from altf import Machine, LocalSpawner, DockerExecSpawner
from altf.pydantic_ai import altf_toolset, status_refresher

machine = Machine(session="devbox",
                  spawner=DockerExecSpawner("devbox"),  # default: LocalSpawner()
                  workdir="/run/altf",
                  classifier=None)            # or LLMClassifier(...)

agent = Agent("anthropic:claude-opus-4-8", toolsets=[altf_toolset(machine)])
```

- `altf_toolset(machine)` → `FunctionToolset` wrapping §8; every return is
  `machine.render_status() + "\n\n" + payload`.
- `status_refresher(machine)` → history processor that (a) keeps the newest status
  block current and (b) strips stale status blocks from older turns, so context
  doesn't accumulate N snapshots of dead state. Both integration points optional
  and independent.
- Keep `pydantic_ai` an optional import; `Machine`/`Console` must be usable bare.

## 15. Deferred (documented non-goals for v1)

- **Survivability across harness restarts.** Design intent: mode-2 deployment where a
  small altf daemon holding the ptys runs inside the container, agent process talks
  to it over a socket. The `Spawner` protocol and log-first observer were shaped so
  this is additive. Do not build in v1.
- Non-bash shells; Windows; multiple containers per Machine; sub-agent registry
  slicing; `mirror` web viewer.

## 16. `altf watch` — read-only observer (v1 scope)

- `altf watch /run/altf/devbox` (also `python -m altf.watch ...`).
- **F1–F9 (and Alt+F#, and plain digits) switch consoles**; Tab cycles. Footer shows
  all consoles with state glyphs (`f1⚡ f2· f3✋`), live from state.json (inotify or
  0.5s poll).
- Each console rendered by a **viewer-side pyte** fed from `.raw` (seek via latest
  checkpoint, then follow). Resizable; PageUp scrollback; `End` resumes follow;
  `/` searches the `.log`.
- Header bar shows the same fields as the model's status block — you watch what the
  model knows.
- **Read-only by construction**: opens files only; no fds, pids, or sockets shared
  with the harness. Running five viewers is safe.
- Zero-install fallbacks (document in README): `tail -f f1-server.raw` in any
  terminal replays one console faithfully; `altf tail <console>` is a tiny wrapper;
  optional `scripts/watch-tmux.sh` spawns a local tmux with one `tail -f` window per
  console — tmux as a dumb viewer only.
- Implementation: Textual preferred (falls under `altf[watch]` extra); plain curses
  acceptable if Textual fights the raw-passthrough rendering. Trick worth trying
  first: for follow-mode, write the raw bytes into a Textual `TerminalDisplay`-style
  widget or even passthrough-to-alternate-screen mode; correctness > beauty.

## 17. Testing strategy

- `osc.py` (pure incremental parser) and the log-text listener in `stream.py`:
  property/fuzz tests (marks/sequences split across chunk boundaries, malformed
  sequences, binary garbage — never crash, no escapes survive into text).
- `console.py` state machine: fake stream fixture (scripted byte feeds), no pty.
- Integration: `LocalSpawner` + real bash under pytest (CI-friendly, no docker):
  run/exit codes, timeout-keeps-running, C-c interrupt, python REPL via send,
  gdb-or-pdb AWAITING detection (pdb is always available), rotation, crash-tail
  rendering for long_running EXITED.
- Docker integration tests behind a marker (`-m docker`): the two §4 footguns
  explicitly (client-kill orphaning; whole-machine cleanup).
- Viewer: golden-file tests for checkpoint seek + replay correctness.

## 18. OPEN questions (decide during implementation, record answers here)

1. Init-line injection mechanics: type-and-suppress vs `bash --rcfile` via
   `docker exec` argv. Prefer whichever suppresses echo most robustly.

   **RESOLVED (v0.1):** type-and-suppress. A single init line is written to the pty
   right after spawn; everything up to and including the first `D` mark that follows
   the OSC 7770 pid handshake (bash banner, default prompt, init-line echo, first
   marked prompt) is consumed — the agent cursor starts after it. This needs no
   rcfile plumbing, works identically for `LocalSpawner` and `DockerExecSpawner`,
   and cannot be defeated by images lacking a writable FS. `PS0` is set *without*
   `\[`/`\]` (those are readline-only markers; in PS0 they would emit literal
   SOH/STX bytes).

2. Exact default thresholds (`quiet_threshold`, send's ~1s settle window, kill ladder
   waits) — tune against the integration tests.

   **RESOLVED (v0.1):** `quiet_threshold=2.0s`; `send` settle `1.0s`; `press` settle
   `0.5s`; kill ladder waits `1.5s` after each `C-c`, `2.0s` after `TERM`, `1.0s`
   after `KILL`. All overridable via `Machine(...)` kwargs (`quiet_threshold`,
   `send_settle`) — integration tests shrink them for speed, which is itself the
   evidence they are safe lower bounds on fast machines; defaults stay conservative
   for loaded CI boxes.

3. `wait()` return payload shape when the pattern never matches but state changed
   (return reason enum + captured output — exact formatting TBD).

   **RESOLVED (v0.1):** first line is `[wait: <reason> after <T>s]` where reason ∈
   `pattern | prompt | awaiting | quiet | exited | dead | timeout` (prompt includes
   `exit:<code>`), followed by the new output captured while waiting (tail-truncated
   ~4KB). Output delivered by `wait()` advances the unread cursor, exactly like
   `peek` — it was shown to the model, so it is no longer "unread".

4. Whether `status()` should include a one-line hint list of available tools for
   model self-orientation (cheap, possibly useful; measure prompt-token cost).

   **RESOLVED (v0.1):** yes, `status()` (the explicit tool only — not the per-result
   status block) appends one hint line (~25 tokens). Cost is paid only when the
   model asks for orientation, which is exactly when it is worth paying.

5. *(clarification recorded during implementation)* §6's EXITED rule says "exit was
   not requested via `kill()`". A `D` mark that resolves a `run()` call actively
   awaiting it is also "requested" — the model receives the exit code in that very
   tool result, so the screamer would be redundant noise. EXITED therefore fires
   only for `D` marks on a `long_running` console with **no** waiting `run()` and no
   kill in progress (i.e. genuinely unattended deaths, including a server that died
   after `run()` timed out and returned control).

6. *(revision, post-v0.1 review, owner decision)* `RegexClassifier` deleted: its
   pattern zoo (`\(gdb\)`, `\(Pdb\)`, password/y-n prompts…) was an enumeration of
   niche special cases with unbounded maintenance. Layer 1 is now purely structural
   (echo-off / alt-screen), gated by the new `Spawner.pty_reflects_termios`
   capability — which also fixed a latent bug where docker consoles' always-raw
   client pty read as perpetually echo-off, flagging every quiet BUSY docker
   console as AWAITING. Everything structure can't see goes to the optional
   `LLMClassifier` (default Haiku: fast/cheap enough per quiet period).
   `Machine(classifier=...)` is the single classifier slot; §11 rewritten.

7. *(revision, post-v0.1 review, owner decision)* Two storage-policy changes:
   `workdir` is now a **required** keyword argument — the library never guesses a
   log location (the `/run/altf` → `$XDG_RUNTIME_DIR` → tmpdir fallback chain was
   deleted; the caller says where bytes go to disk, full stop). And log
   rotation/caps are **off by default**: `raw_max_bytes=None` means files grow
   unbounded; passing a byte count opts into logrotate-style shifting. Rationale:
   silent defaults about disk locations and silent deletion of history are policy
   decisions that belong to the embedding application, not the library.
   (`logging.handlers.RotatingFileHandler` was evaluated and rejected: it is a
   text LogRecord sink and provides none of the binary-stream or (file, offset)
   cursor accounting this needs — see the `RotatingWriter` docstring.)

8. *(revision, post-v0.1 review, owner decision)* `ansi.py` deleted: pyte — a
   hard dependency already — is the terminal-sequence parser; maintaining a
   second hand-written one was duplication. `.log` text now comes from
   `stream.TextListener`, a ~40-line **gridless** pyte listener (draw/linefeed/
   carriage_return/tab events → chronological text; `set_mode` events → the
   alt-screen hint, replacing CSI-body sniffing). Gridless matters: extracting
   text from the render `Screen` would hard-wrap log lines at the screen width
   and break `grep`. `osc.py` remains hand-written only because pyte offers no
   event hook for unrecognized OSC sequences, and the 133/7770 marks must be
   intercepted in-band — it is the single escape parser left in the codebase.
   Verified (2026-07, pyte 0.8.2 + master): unknown OSC codes are silently
   discarded (only 0/1/2 dispatch `set_icon_name`/`set_title`; OSC 133 is in
   fact *misparsed* into `set_icon_name` events, so title events cannot be
   abused as a hook, and OSC 7770 yields no event at all); the only override
   point is the private `_parser_fsm()` generator, and the request for a custom-
   sequence hook — selectel/pyte#94 — has been open since 2017. Re-evaluate only
   if pyte ships such a hook.
