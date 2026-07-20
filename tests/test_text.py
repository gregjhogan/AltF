"""Log-text extraction via the gridless pyte listener (replaces the deleted
hand-written ANSI stripper — pyte parses, we only collect events)."""

import random
from types import SimpleNamespace

import pyte

from altf.stream import TextListener


def make():
    console = SimpleNamespace(alt_screen=False)
    listener = TextListener(console)
    return console, listener, pyte.ByteStream(listener)


def extract(chunks):
    _, listener, stream = make()
    out = []
    for chunk in chunks:
        stream.feed(chunk)
        out.append(listener.take())
    out.append(listener.flush())
    return "".join(out)


def test_csi_colors_stripped():
    assert extract([b"\x1b[1;31mred\x1b[0m plain"]) == "red plain"


def test_osc_title_stripped():
    assert extract([b"\x1b]0;title\x07text\x1b]2;t\x1b\\more"]) == "textmore"


def test_charset_designation_stripped():
    assert extract([b"\x1b(Bplain\x1b)0x"]) == "plainx"


def test_crlf_is_one_newline():
    assert extract([b"a\r\nb"]) == "a\nb"


def test_lone_cr_becomes_newline():
    # progress bars: each \r-rewrite becomes a line so the .log stays greppable
    assert extract([b"50%\r51%\r52%"]) == "50%\n51%\n52%"


def test_cr_split_across_chunks():
    assert extract([b"line\r", b"\nnext"]) == "line\nnext"
    assert extract([b"line\r", b"over"]) == "line\nover"


def test_trailing_cr_resolved_at_flush():
    assert extract([b"done\r"]) == "done\n"


def test_csi_split_across_chunks():
    assert extract([b"a\x1b[3", b"1mred"]) == "ared"


def test_tabs_kept_control_noise_dropped():
    assert extract([b"a\x08b\tc\x07d"]) == "ab\tcd"


def test_utf8_multibyte_split():
    payload = "héllo→🚀".encode()
    for cut in range(len(payload)):
        assert extract([payload[:cut], payload[cut:]]) == "héllo→🚀"


def test_alt_screen_tracked():
    console, listener, stream = make()
    for mode in (b"1049", b"1047", b"47"):
        stream.feed(b"\x1b[?" + mode + b"h")
        assert console.alt_screen, mode
        stream.feed(b"\x1b[?" + mode + b"l")
        assert not console.alt_screen, mode
    stream.feed(b"\x1b[4h")  # non-private mode 4: not an alt-screen switch
    assert not console.alt_screen


def test_fuzz_never_crashes_no_escapes_survive():
    rng = random.Random(4321)
    for _ in range(100):
        _, listener, stream = make()
        out = []
        for _ in range(rng.randrange(1, 8)):
            stream.feed(bytes(rng.randrange(256) for _ in range(rng.randrange(300))))
            out.append(listener.take())
        out.append(listener.flush())
        text = "".join(out)
        assert "\x1b" not in text
        assert all(c in "\n\t" or ord(c) >= 0x20 for c in text)


def test_fuzz_printable_text_preserved():
    rng = random.Random(7)
    words = "the quick brown fox jumps over the lazy dog".split()
    for _ in range(50):
        parts, expect = [], []
        for _ in range(rng.randrange(1, 10)):
            word = rng.choice(words)
            expect.append(word)
            parts.append(word.encode())
            parts.append(b"\x1b[%dm" % rng.randrange(100))
        payload = b"".join(parts)
        chunks, pos = [], 0
        while pos < len(payload):
            step = rng.randrange(1, 12)
            chunks.append(payload[pos : pos + step])
            pos += step
        assert extract(chunks) == "".join(expect)
