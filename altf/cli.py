"""`altf` command-line entry points: watch, ls, tail (DESIGN §16)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .logs import read_state


def _cmd_ls(args: argparse.Namespace) -> int:
    state_path = Path(args.session_dir) / "state.json"
    try:
        state = read_state(state_path)
    except (OSError, ValueError) as exc:
        print(f"altf ls: cannot read {state_path}: {exc}", file=sys.stderr)
        return 1
    print(f"session: {state.get('session')}  spawner: {state.get('spawner')}")
    for name, c in state.get("consoles", {}).items():
        print(
            f"{c.get('slot'):<5}{name:<16}{c.get('state'):<10}"
            f"{c.get('raw_bytes', 0):>10}B  {c.get('purpose', '')!r}"
        )
    return 0


def _cmd_tail(args: argparse.Namespace) -> int:
    session_dir = Path(args.session_dir)
    try:
        state = read_state(session_dir / "state.json")
        console = state["consoles"][args.console]
        raw_name = f"{console['slot']}-{args.console}.raw"
    except (OSError, ValueError, KeyError):
        # fall back to globbing when state.json is unavailable
        matches = sorted(session_dir.glob(f"f*-{args.console}.raw"))
        if not matches:
            print(f"altf tail: no console '{args.console}' in {session_dir}", file=sys.stderr)
            return 1
        raw_name = matches[-1].name
    path = session_dir / raw_name
    out = sys.stdout.buffer
    try:
        fh = open(path, "rb")
    except OSError as exc:
        print(f"altf tail: {exc}", file=sys.stderr)
        return 1
    try:
        size = path.stat().st_size
        fh.seek(max(0, size - 8192))
        while True:
            data = fh.read(65536)
            if data:
                out.write(data)
                out.flush()
                continue
            try:  # reopen after rotation (current file replaced/truncated)
                if path.stat().st_size < fh.tell():
                    fh.close()
                    fh = open(path, "rb")
            except OSError:
                pass
            time.sleep(0.1)
    except KeyboardInterrupt:
        return 0
    finally:
        fh.close()


def _cmd_watch(args: argparse.Namespace) -> int:
    try:
        from .watch.app import run_app
    except ImportError:
        print(
            "altf watch needs Textual: pip install 'altf[watch]'\n"
            "zero-install fallback: tail -f <session-dir>/f1-<name>.raw",
            file=sys.stderr,
        )
        return 1
    return run_app(Path(args.session_dir))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="altf", description="altf console observer tools")
    sub = parser.add_subparsers(dest="command", required=True)

    p_watch = sub.add_parser("watch", help="read-only TUI over a session directory")
    p_watch.add_argument("session_dir")
    p_watch.set_defaults(func=_cmd_watch)

    p_ls = sub.add_parser("ls", help="list consoles from state.json")
    p_ls.add_argument("session_dir")
    p_ls.set_defaults(func=_cmd_ls)

    p_tail = sub.add_parser("tail", help="follow one console's raw byte stream")
    p_tail.add_argument("session_dir")
    p_tail.add_argument("console")
    p_tail.set_defaults(func=_cmd_tail)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
