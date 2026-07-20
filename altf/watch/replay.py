"""Checkpoint-seeking raw-stream replay for the viewer (pure, testable).

The viewer never replays gigabytes: it paints the latest checkpoint whose
offset is <= the target, then feeds only the bytes after it (DESIGN §10).
"""

from __future__ import annotations

from pathlib import Path

import pyte

from ..logs import read_checkpoints


def latest_checkpoint(session_dir: Path, base: str, raw_name: str, upto: int | None = None) -> dict | None:
    records = [
        r
        for r in read_checkpoints(session_dir / f"{base}.ckpt")
        if r.get("raw_file") == raw_name
        and isinstance(r.get("offset"), int)
        and (upto is None or r["offset"] <= upto)
    ]
    return records[-1] if records else None


def paint_checkpoint(screen: pyte.Screen, stream: pyte.ByteStream, record: dict) -> None:
    screen.reset()
    lines = record.get("lines") or []
    payload = "\r\n".join(str(line) for line in lines[: screen.lines])
    stream.feed(payload.encode("utf-8", "replace"))
    cursor = record.get("cursor")
    if isinstance(cursor, (list, tuple)) and len(cursor) == 2:
        try:
            x, y = int(cursor[0]), int(cursor[1])
            stream.feed(f"\x1b[{y + 1};{x + 1}H".encode())
        except (TypeError, ValueError):
            pass


def build_screen(
    session_dir: Path,
    base: str,
    *,
    cols: int = 120,
    rows: int = 50,
    upto: int | None = None,
    screen: pyte.Screen | None = None,
) -> tuple[pyte.Screen, pyte.ByteStream, int]:
    """Replay `<base>.raw` into a pyte screen (a fresh cols×rows one, or the
    caller's — e.g. a HistoryScreen), seeking via the latest usable checkpoint.
    Returns (screen, stream, consumed_offset) — feed bytes read from
    consumed_offset onward into `stream` to follow live."""
    if screen is None:
        screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    raw_path = session_dir / f"{base}.raw"
    start = 0
    record = latest_checkpoint(session_dir, base, raw_path.name, upto=upto)
    if record is not None:
        paint_checkpoint(screen, stream, record)
        start = record["offset"]
    try:
        with open(raw_path, "rb") as fh:
            fh.seek(start)
            data = fh.read() if upto is None else fh.read(max(0, upto - start))
    except OSError:
        data = b""
    if data:
        stream.feed(data)
    return screen, stream, start + len(data)
