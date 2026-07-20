"""Log writers, size-capped rotation, checkpoint sidecars, state.json I/O."""

from __future__ import annotations

import json
import os
from pathlib import Path


class RotatingWriter:
    """Append-only writer; optional logrotate-style shifting (`x.raw` → `x.raw.1`).

    By default (`max_bytes=None`) files grow without limit and no rotation
    happens. Pass a byte cap to opt in (DESIGN §10 / §18.7). Rotation itself
    is trivial; the rest exists for the cursor contract (DESIGN §9–10):
    offsets are global and monotonic across rotations, and `locate()` maps
    one to the (file name, in-file offset) pair stored in state.json.
    `logging.handlers.RotatingFileHandler` shares the naming scheme but is a
    text LogRecord sink — it cannot carry a binary pty stream or the offset
    accounting — so this stays small and direct.
    """

    def __init__(
        self,
        directory: Path | str,
        filename: str,
        *,
        max_bytes: int | None = None,
        keep: int = 8,
    ) -> None:
        self.dir = Path(directory)
        self.filename = filename
        self.max_bytes = max_bytes
        self.keep = keep
        self._rotated: list[int] = []  # sizes of x.1, x.2, ... (newest first)
        self._pruned = 0  # total bytes in files deleted by the keep cap
        self._size = 0  # bytes in the current file
        # Sessions do not resume (DESIGN §15): start fresh.
        self._fh = open(self.dir / filename, "wb", buffering=0)

    def _path(self, index: int) -> Path:
        return self.dir / (self.filename if index == 0 else f"{self.filename}.{index}")

    @property
    def total(self) -> int:
        return self._pruned + sum(self._rotated) + self._size

    @property
    def current_name(self) -> str:
        return self.filename

    @property
    def current_size(self) -> int:
        return self._size

    def write(self, data: bytes) -> None:
        if not data:
            return
        if (
            self.max_bytes is not None
            and self._size
            and self._size + len(data) > self.max_bytes
        ):
            self._rotate()
        try:
            self._fh.write(data)
        except ValueError:  # closed mid-teardown
            return
        self._size += len(data)

    def write_text(self, text: str) -> None:
        self.write(text.encode("utf-8", "replace"))

    def _rotate(self) -> None:
        self._fh.close()
        try:
            for i in range(len(self._rotated), 0, -1):
                os.replace(self._path(i), self._path(i + 1))
            os.replace(self._path(0), self._path(1))
        except OSError:
            pass
        self._rotated.insert(0, self._size)
        while len(self._rotated) > self.keep:
            try:
                os.unlink(self._path(len(self._rotated)))
            except OSError:
                pass
            self._pruned += self._rotated.pop()
        self._fh = open(self._path(0), "wb", buffering=0)
        self._size = 0

    def locate(self, global_offset: int) -> tuple[str, int]:
        """Map a global offset to (file name, in-file offset); offsets that
        fell into pruned files clamp to the start of the oldest survivor."""
        oldest = len(self._rotated)
        start = self._pruned
        if global_offset < start:
            return self._path(oldest).name, 0
        for index in range(oldest, 0, -1):  # oldest rotated file first
            size = self._rotated[index - 1]
            if global_offset < start + size:
                return self._path(index).name, global_offset - start
            start += size
        return self.filename, min(global_offset - start, self._size)

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


class CheckpointWriter:
    """Append-only JSON-lines sidecar: {"raw_file", "offset", "lines", "cursor"}."""

    def __init__(self, path: Path | str) -> None:
        self._fh = open(path, "w", encoding="utf-8", buffering=1)

    def write(self, record: dict) -> None:
        try:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def read_checkpoints(path: Path | str) -> list[dict]:
    records: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
    except OSError:
        pass
    return records


def write_state(path: Path | str, obj: dict) -> None:
    """Atomic rewrite: tmp file in the same directory + rename."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def read_state(path: Path | str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
