"""Rotation naming, global-offset cursor math, checkpoint + state.json I/O."""

import json

from altf.logs import (
    CheckpointWriter,
    RotatingWriter,
    read_checkpoints,
    read_state,
    write_state,
)


def test_default_is_unbounded_no_rotation(tmp_path):
    writer = RotatingWriter(tmp_path, "c.raw")
    for _ in range(50):
        writer.write(b"x" * 100_000)  # 5MB, far past any would-be cap
    writer.close()
    assert (tmp_path / "c.raw").stat().st_size == 5_000_000
    assert list(tmp_path.glob("c.raw.*")) == []  # never rotated
    assert writer.locate(1_234_567) == ("c.raw", 1_234_567)


def test_rotation_shifts_names(tmp_path):
    writer = RotatingWriter(tmp_path, "c.raw", max_bytes=10)
    writer.write(b"aaaaaaaa")  # 8
    writer.write(b"bbbbbbbb")  # would exceed -> rotate first
    writer.write(b"cccccccccccc")  # exceeds again -> rotate
    writer.close()
    assert (tmp_path / "c.raw").exists()
    assert (tmp_path / "c.raw.1").exists()
    assert (tmp_path / "c.raw.2").exists()
    assert (tmp_path / "c.raw.2").read_bytes() == b"aaaaaaaa"
    assert (tmp_path / "c.raw.1").read_bytes() == b"bbbbbbbb"
    assert (tmp_path / "c.raw").read_bytes() == b"cccccccccccc"


def test_global_offsets_survive_rotation(tmp_path):
    writer = RotatingWriter(tmp_path, "c.raw", max_bytes=10)
    writer.write(b"0123456789")
    writer.write(b"abcdefghij")
    assert writer.total == 20
    assert writer.locate(3) == ("c.raw.1", 3)
    assert writer.locate(10) == ("c.raw", 0)
    assert writer.locate(15) == ("c.raw", 5)
    assert writer.locate(20) == ("c.raw", 10)  # end-of-stream cursor
    writer.close()


def test_prune_keeps_bounded_files(tmp_path):
    writer = RotatingWriter(tmp_path, "c.raw", max_bytes=4, keep=2)
    for i in range(8):
        writer.write(b"%04d" % i)
    writer.close()
    rotated = sorted(p.name for p in tmp_path.glob("c.raw.*"))
    assert rotated == ["c.raw.1", "c.raw.2"]
    # pruned offsets clamp to the oldest surviving file
    name, offset = writer.locate(0)
    assert name == "c.raw.2" and offset == 0


def test_checkpoints_roundtrip(tmp_path):
    path = tmp_path / "c.ckpt"
    writer = CheckpointWriter(path)
    writer.write({"raw_file": "c.raw", "offset": 100, "lines": ["hi"], "cursor": [0, 0]})
    writer.write({"raw_file": "c.raw", "offset": 200, "lines": ["yo"], "cursor": [1, 0]})
    writer.close()
    records = read_checkpoints(path)
    assert [r["offset"] for r in records] == [100, 200]


def test_read_checkpoints_tolerates_garbage(tmp_path):
    path = tmp_path / "c.ckpt"
    path.write_text('{"offset": 1, "raw_file": "c.raw"}\nnot json\n[1,2]\n')
    assert [r["offset"] for r in read_checkpoints(path)] == [1]


def test_state_atomic_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    write_state(path, {"version": 1, "session": "s", "consoles": {}})
    assert read_state(path)["session"] == "s"
    assert not path.with_name("state.json.tmp").exists()
    # rewrite is a full replace
    write_state(path, {"version": 1, "session": "t", "consoles": {}})
    assert json.loads(path.read_text())["session"] == "t"
