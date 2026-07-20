"""Textual read-only observer TUI.

Read-only by construction: it opens files under the session directory and
nothing else — no fds, pids, or sockets shared with the harness. Correctness
over beauty (DESIGN §16): each console is a viewer-side pyte HistoryScreen
fed from `.raw` via the latest checkpoint, sized to the window, refreshed on
a timer, and rendered inside a scrollable container (scrollback + scrollbar;
staying at the bottom follows live output, End jumps back to following).
"""

from __future__ import annotations

import time
from pathlib import Path

import pyte
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Static

from ..logs import read_state
from .replay import build_screen

_GLYPH = {"idle": "·", "busy": "⚡", "awaiting": "✋", "exited": "💥", "dead": "☠"}
_HISTORY = 2000  # scrollback lines kept per console


class _ConsoleView:
    """Follows one console's current .raw file into a pyte HistoryScreen."""

    def __init__(self, session_dir: Path, base: str, cols: int, rows: int) -> None:
        self.session_dir = session_dir
        self.base = base
        self._pending: tuple[int, int] | None = None
        self._pending_since = 0.0
        self._rebuild(cols, rows)

    def _rebuild(self, cols: int, rows: int) -> None:
        """Full replay of the raw stream at this size — history reflows."""
        screen = pyte.HistoryScreen(cols, rows, history=_HISTORY)
        self.screen, self.stream, self.offset = build_screen(
            self.session_dir, self.base, screen=screen
        )

    def resize(self, cols: int, rows: int) -> None:
        """Reflow to a new size by re-replaying, debounced so a window-edge
        drag doesn't trigger a replay per resize event."""
        if (cols, rows) == (self.screen.columns, self.screen.lines):
            self._pending = None
            return
        now = time.monotonic()
        if self._pending != (cols, rows):
            self._pending = (cols, rows)
            self._pending_since = now
            return
        if now - self._pending_since >= 0.4:
            self._pending = None
            self._rebuild(cols, rows)

    def refresh(self) -> None:
        path = self.session_dir / f"{self.base}.raw"
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self.offset:  # rotated: replay the fresh current file
            self._rebuild(self.screen.columns, self.screen.lines)
            return
        if size == self.offset:
            return
        try:
            with open(path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read(size - self.offset)
        except OSError:
            return
        self.offset += len(data)
        try:
            self.stream.feed(data)
        except Exception:
            pass

    def render_text(self) -> str:
        cols = self.screen.columns
        scrollback = [
            "".join(line[x].data for x in range(cols)).rstrip()
            for line in self.screen.history.top
        ]
        current = [line.rstrip() for line in self.screen.display]
        while current and not current[-1]:
            current.pop()
        return "\n".join([*scrollback, *current])


class WatchApp(App):
    CSS = """
    #header { height: 1; background: $panel; }
    #scroller { height: 1fr; }
    #screen { height: auto; }
    #consoles { height: 1; background: $panel; }
    """

    BINDINGS = [
        Binding("tab", "cycle", "next console", priority=True),
        Binding("pageup", "scroll(-1)", "scrollback", priority=True),
        Binding("pagedown", "scroll(1)", show=False, priority=True),
        Binding("end", "follow", "follow", priority=True),
        Binding("q", "quit", "quit"),
        *[Binding(str(i), f"switch({i})", show=False) for i in range(1, 10)],
        *[Binding(f"f{i}", f"switch({i})", show=False) for i in range(1, 10)],
    ]

    def __init__(self, session_dir: Path) -> None:
        super().__init__()
        self.session_dir = Path(session_dir)
        self.state: dict = {}
        self.views: dict[str, _ConsoleView] = {}
        self.active: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with VerticalScroll(id="scroller"):
            yield Static(id="screen")
        yield Static(id="consoles")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.5, self._reload_state)
        self.set_interval(0.2, self._refresh_active)
        self._reload_state()

    # ------------------------------------------------------------- data pump

    def _viewport(self) -> tuple[int, int] | None:
        """Content area of the scroller, or None before the first layout —
        replaying history into a not-yet-sized (0×0) grid would permanently
        wrap it at the minimum width."""
        area = self.query_one("#scroller", VerticalScroll).container_size
        if area.width <= 0 or area.height <= 0:
            return None
        return max(20, area.width), max(5, area.height)

    def _reload_state(self) -> None:
        try:
            self.state = read_state(self.session_dir / "state.json")
        except (OSError, ValueError):
            return
        consoles = self.state.get("consoles", {})
        viewport = self._viewport()
        for name, c in consoles.items():
            if name not in self.views and viewport is not None:
                self.views[name] = _ConsoleView(
                    self.session_dir, f"{c['slot']}-{name}", *viewport
                )
        for name in list(self.views):
            if name not in consoles:
                del self.views[name]
        if self.active not in self.views:
            self.active = next(iter(self._ordered()), None)
        self._render_chrome()

    def _ordered(self) -> list[str]:
        consoles = self.state.get("consoles", {})
        return sorted(consoles, key=lambda n: int(consoles[n]["slot"][1:]))

    def _refresh_active(self) -> None:
        view = self.views.get(self.active) if self.active else None
        if view is None:
            return
        viewport = self._viewport()
        if viewport is not None:
            view.resize(*viewport)
        view.refresh()
        scroller = self.query_one("#scroller", VerticalScroll)
        following = scroller.scroll_offset.y >= scroller.max_scroll_y
        self.query_one("#screen", Static).update(Text(view.render_text()))
        if following:
            scroller.scroll_end(animate=False)
        self._render_chrome()

    def _render_chrome(self) -> None:
        consoles = self.state.get("consoles", {})
        if self.active and self.active in consoles:
            c = consoles[self.active]
            self.query_one("#header", Static).update(
                f" {c['slot']}·{self.active}  {c['state'].upper()}  "
                f"fg:{c.get('fg_command') or '-'}  {c.get('raw_bytes', 0)}B  "
                f"\"{c.get('purpose', '')}\""
            )
        chips = []
        for name in self._ordered():
            c = consoles[name]
            chip = f"{c['slot']}{_GLYPH.get(c['state'], '?')}"
            chips.append(f"[reverse]{chip}[/]" if name == self.active else chip)
        self.query_one("#consoles", Static).update(" " + "  ".join(chips))

    # --------------------------------------------------------------- actions

    def action_cycle(self) -> None:
        names = self._ordered()
        if not names:
            return
        idx = names.index(self.active) if self.active in names else -1
        self.active = names[(idx + 1) % len(names)]
        self._refresh_active()
        self.action_follow()

    def action_switch(self, slot_number: int) -> None:
        consoles = self.state.get("consoles", {})
        for name, c in consoles.items():
            if c["slot"] == f"f{slot_number}":
                self.active = name
                self._refresh_active()
                self.action_follow()
                return

    def action_scroll(self, direction: int) -> None:
        scroller = self.query_one("#scroller", VerticalScroll)
        if direction < 0:
            scroller.scroll_page_up(animate=False)
        else:
            scroller.scroll_page_down(animate=False)

    def action_follow(self) -> None:
        self.query_one("#scroller", VerticalScroll).scroll_end(animate=False)


def run_app(session_dir: Path) -> int:
    WatchApp(session_dir).run()
    return 0
