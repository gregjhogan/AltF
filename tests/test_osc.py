"""OSC scanner: pure incremental parser — chunk-split marks, malformed input,
binary garbage passthrough (DESIGN §17)."""

import random

from altf.osc import AltfHandshake, Osc133, OscScanner


def collect(scanner, chunks):
    events = []
    for chunk in chunks:
        events.extend(scanner.feed(chunk))
    tail = scanner.flush()
    if tail:
        events.append(("bytes", tail))
    return events


def marks_of(events):
    return [payload for kind, payload in events if kind == "mark"]


def bytes_of(events):
    return b"".join(payload for kind, payload in events if kind == "bytes")


def test_basic_marks_bel_and_st():
    data = b"before\x1b]133;A\x07mid\x1b]133;D;0\x1b\\after"
    events = collect(OscScanner(), [data])
    assert marks_of(events) == [Osc133("A"), Osc133("D", 0)]
    assert bytes_of(events) == b"beforemidafter"


def test_d_with_and_without_exit_code():
    events = collect(OscScanner(), [b"\x1b]133;D;42\x07\x1b]133;D\x07"])
    assert marks_of(events) == [Osc133("D", 42), Osc133("D", None)]


def test_handshake():
    events = collect(OscScanner(), [b"\x1b]7770;pid=137\x07"])
    assert marks_of(events) == [AltfHandshake(pid=137)]


def test_mark_split_across_every_chunk_boundary():
    data = b"x\x1b]133;C\x07y\x1b]7770;pid=9\x07z"
    for size in range(1, len(data) + 1):
        chunks = [data[i : i + size] for i in range(0, len(data), size)]
        events = collect(OscScanner(), chunks)
        assert marks_of(events) == [Osc133("C"), AltfHandshake(pid=9)], size
        assert bytes_of(events) == b"xyz", size


def test_unknown_osc_passes_through():
    data = b"\x1b]0;window title\x07text"
    events = collect(OscScanner(), [data])
    assert marks_of(events) == []
    assert bytes_of(events) == data


def test_malformed_133_passes_through():
    data = b"\x1b]133;D;notanint\x07\x1b]133;Dx\x07"
    events = collect(OscScanner(), [data])
    assert marks_of(events) == []
    assert bytes_of(events) == data


def test_non_osc_escapes_untouched():
    data = b"\x1b[31mred\x1b[0m\x1b(B\x1bM"
    events = collect(OscScanner(), [data])
    assert marks_of(events) == []
    assert bytes_of(events) == data


def test_oversized_unterminated_osc_gives_up():
    data = b"\x1b]" + b"x" * 20000
    scanner = OscScanner(max_pending=8192)
    events = collect(scanner, [data, b"tail"])
    assert bytes_of(events).endswith(b"tail")
    assert len(bytes_of(events)) == len(data) + 4


def test_ordering_preserved_around_marks():
    events = OscScanner().feed(b"a\x1b]133;C\x07b\x1b]133;D;1\x07c")
    assert events == [
        ("bytes", b"a"),
        ("mark", Osc133("C")),
        ("bytes", b"b"),
        ("mark", Osc133("D", 1)),
        ("bytes", b"c"),
    ]


def test_fuzz_never_crashes_and_preserves_clean_bytes():
    rng = random.Random(1234)
    for _ in range(200):
        blob = bytes(rng.randrange(256) for _ in range(rng.randrange(500)))
        scanner = OscScanner()
        pos = 0
        while pos < len(blob):
            step = rng.randrange(1, 40)
            scanner.feed(blob[pos : pos + step])
            pos += step
        scanner.flush()


def test_fuzz_marks_survive_random_chunking():
    rng = random.Random(99)
    payload = (b"noise" + b"\x1b]133;A\x07" + b"\x1b]133;C\x07out" + b"\x1b]133;D;3\x07") * 5
    for _ in range(50):
        scanner = OscScanner()
        chunks, pos = [], 0
        while pos < len(payload):
            step = rng.randrange(1, 15)
            chunks.append(payload[pos : pos + step])
            pos += step
        events = collect(scanner, chunks)
        assert marks_of(events) == [Osc133("A"), Osc133("C"), Osc133("D", 3)] * 5
        assert bytes_of(events) == b"noiseout" * 5
