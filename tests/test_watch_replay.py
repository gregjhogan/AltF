"""Viewer checkpoint seek + replay correctness (DESIGN §17 golden-file tests)."""

import json

from altf.watch.replay import build_screen, latest_checkpoint


def _write_ckpt(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_replay_without_checkpoint(tmp_path):
    (tmp_path / "f1-work.raw").write_bytes(b"hello\r\nworld")
    screen, _, consumed = build_screen(tmp_path, "f1-work", cols=20, rows=5)
    lines = [l.rstrip() for l in screen.display]
    assert lines[0] == "hello"
    assert lines[1] == "world"
    assert consumed == len(b"hello\r\nworld")


def test_replay_seeks_via_checkpoint(tmp_path):
    # Prefix bytes that would corrupt the screen if replayed (they clear and
    # scribble); the checkpoint at their end carries the settled screen.
    prefix = b"\x1b[2Jgarbage" * 100
    suffix = b"TAIL-MARKER\r\n"
    (tmp_path / "f1-work.raw").write_bytes(prefix + suffix)
    _write_ckpt(
        tmp_path / "f1-work.ckpt",
        [
            {"raw_file": "f1-work.raw", "offset": 10, "lines": ["old"], "cursor": [0, 0]},
            {
                "raw_file": "f1-work.raw",
                "offset": len(prefix),
                "lines": ["CKPT-LINE-1", "CKPT-LINE-2"],
                "cursor": [0, 2],
            },
        ],
    )
    screen, _, consumed = build_screen(tmp_path, "f1-work", cols=40, rows=6)
    lines = [l.rstrip() for l in screen.display]
    assert lines[0] == "CKPT-LINE-1"
    assert lines[1] == "CKPT-LINE-2"
    assert lines[2] == "TAIL-MARKER"
    assert "garbage" not in " ".join(lines)
    assert consumed == len(prefix) + len(suffix)


def test_upto_bounds_both_checkpoint_and_read(tmp_path):
    raw = b"AAAA" + b"BBBB" + b"CCCC"
    (tmp_path / "f1-x.raw").write_bytes(raw)
    _write_ckpt(
        tmp_path / "f1-x.ckpt",
        [
            {"raw_file": "f1-x.raw", "offset": 4, "lines": ["painted"], "cursor": [0, 1]},
            {"raw_file": "f1-x.raw", "offset": 12, "lines": ["late"], "cursor": [0, 0]},
        ],
    )
    record = latest_checkpoint(tmp_path, "f1-x", "f1-x.raw", upto=8)
    assert record["offset"] == 4
    screen, _, consumed = build_screen(tmp_path, "f1-x", cols=20, rows=4, upto=8)
    assert consumed == 8
    lines = [l.rstrip() for l in screen.display]
    assert lines[0] == "painted"
    assert "BBBB" in " ".join(lines)
    assert "CCCC" not in " ".join(lines)


def test_checkpoints_for_other_files_ignored(tmp_path):
    (tmp_path / "f1-x.raw").write_bytes(b"fresh")
    _write_ckpt(
        tmp_path / "f1-x.ckpt",
        [{"raw_file": "f1-x.raw.1", "offset": 3, "lines": ["stale"], "cursor": [0, 0]}],
    )
    assert latest_checkpoint(tmp_path, "f1-x", "f1-x.raw") is None
    screen, _, _ = build_screen(tmp_path, "f1-x", cols=20, rows=4)
    assert screen.display[0].rstrip() == "fresh"
