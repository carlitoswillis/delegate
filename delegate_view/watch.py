"""Terminal UI for watching live agent-to-agent conversations.

This module is now only the loop: input, state, and painting what the pure
functions elsewhere return. The pieces it drives:

    views.py      screens as list[render.Line]  (pure)
    render.py     styled spans, wrapping, painting
    theme.py      style name -> curses attribute
    keys.py       key and SGR-mouse decoding      (pure)
    scrollbar.py  bar geometry and its inverse    (pure)
    store.py      the refreshing run list         (threaded)

Keeping the loop this thin is deliberate. Everything that used to be wrong in
here — colour that bled, a footer that never painted, a wheel that raised
AttributeError, a scroll bar you could not grab — was logic tangled into the
curses calls where it could not be tested. What remains is the part that
genuinely needs a terminal, and nothing else.
"""

from __future__ import annotations

import argparse
import curses
import logging
import os
import sys
import threading
import time
import traceback

from delegate_view import views
from delegate_view.keys import MOUSE_OFF, MOUSE_ON, InputDecoder
from delegate_view.render import paint
from delegate_view.scrollbar import (
    grab_offset,
    scroll_for_click,
    scroll_for_thumb_top,
    thumb_for,
)
from delegate_view.theme import Theme

_log_path = os.path.join(os.path.expanduser("~"), ".delegate", "watch.log")
os.makedirs(os.path.dirname(_log_path), exist_ok=True)
logging.basicConfig(
    filename=_log_path, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
)
_log = logging.getLogger("watch")

# How often the spinner advances. The old loop stepped it once per frame at
# 50fps, which is not an animation so much as a flicker.
SPIN_MS = 110

# Frame budget. 20fps is imperceptibly different from 50 for this content and
# leaves the process mostly asleep.
TICK_MS = 50


def resolve_key_action(key: int, view: str) -> str:
    """Map a key code + current view to an action string. Pure.

    Kept here rather than in keys.py because it encodes what the *views* do,
    not how the terminal encodes input.
    """
    if view == "list":
        if key in (ord("q"), 27):
            return "quit"
        if key == 9:
            return "toggle_expand"
        if key in (curses.KEY_UP, ord("k")):
            return "up"
        if key in (curses.KEY_DOWN, ord("j")):
            return "down"
        if key in (ord("g"), curses.KEY_HOME):
            return "top"
        if key in (ord("G"), curses.KEY_END):
            return "bottom"
        if key in (10, 13, curses.KEY_ENTER):
            return "select"
        if key == curses.KEY_NPAGE:
            return "page_down"
        if key == curses.KEY_PPAGE:
            return "page_up"
        if key == 4:
            return "half_page_down"
        if key == 21:
            return "half_page_up"
        if key == ord("r"):
            return "refresh"
        if key in (curses.KEY_LEFT, ord("h")):
            return "noop"
        return "noop"

    if key in (27, curses.KEY_LEFT, ord("h"), ord("q")):
        return "back"
    if key == 9:
        return "toggle_expand"
    if key in (curses.KEY_UP, ord("k")):
        return "scroll_up"
    if key in (curses.KEY_DOWN, ord("j")):
        return "scroll_down"
    if key in (ord("g"), curses.KEY_HOME):
        return "scroll_top"
    if key in (ord("G"), curses.KEY_END):
        return "scroll_bottom"
    if key == curses.KEY_NPAGE:
        return "page_down"
    if key == curses.KEY_PPAGE:
        return "page_up"
    if key == 4:
        return "half_page_down"
    if key == 21:
        return "half_page_up"
    return "noop"


class ConversationLoader:
    """Loads a conversation off the main thread.

    Pressing Enter used to call the adapter inline, so opening a large session
    froze the UI for as long as the parse took. Here the request is handed to a
    worker and the screen keeps painting; the body appears when it is ready.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._want: tuple | None = None
        self._have: tuple | None = None
        self._body: list = []
        self._title = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def request(self, run) -> None:
        key = (getattr(run, "platform", ""), getattr(run, "session_id", ""),
               getattr(run, "transcript", ""))
        with self._lock:
            self._want = (key, run)
        self._ensure_thread()

    def result(self) -> tuple[list, str]:
        with self._lock:
            return self._body, self._title

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _work(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                want = self._want
            if want is None:
                return
            key, run = want
            if key == self._have:
                # Live runs keep growing, so re-read on a slow cadence.
                if not getattr(run, "live", False):
                    return
                self._stop.wait(1.0)
                if self._stop.is_set():
                    return
            try:
                body, title = _load_body(run)
            except Exception:
                body, title = [], ""
            with self._lock:
                if self._want and self._want[0] == key:
                    self._body, self._title = body, title
                    self._have = key
            if self._want and self._want[0] != key:
                continue


def _load_body(run) -> tuple[list, str]:
    """Build the conversation lines for a run, plus its title."""
    from delegate_view import adapters

    title = views.run_title(run)[0] or "conversation"
    platform = getattr(run, "platform", "") or ""
    sid = getattr(run, "session_id", "") or ""
    is_subagent = not getattr(run, "prompt", "")

    if platform and sid:
        session = adapters.load_session(platform, sid)
        if session.tokens_in:
            run.tokens_in = session.tokens_in
        if session.tokens_out:
            run.tokens_out = session.tokens_out
        if session.cost:
            run.cost = session.cost
        body = views.session_blocks(
            session, platform=platform,
            model=getattr(run, "model", "") or session.model,
            is_subagent=is_subagent,
        )
        return body, title

    # No resolved session: fall back to the raw transcript tail.
    from delegate_view.render import Span
    from delegate_view.runs import tail

    raw = tail(getattr(run, "transcript", ""), 500)
    return [[Span("  " + ln, "dim")] for ln in raw], title


def main():
    parser = argparse.ArgumentParser(
        description="Watch live agent-to-agent conversations")
    parser.add_argument("--ledger", dest="ledger_path", default=None,
                        help="Path to the ledger file")
    parser.add_argument("--once", action="store_true",
                        help="Render one frame to stdout and exit (no curses)")
    parser.add_argument("--no-subagents", action="store_true",
                        help="Show only delegate.sh runs")
    parser.add_argument("--subagent-limit", type=int, default=25,
                        help="How many recent subagent conversations (default 25)")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Seconds between background refreshes")
    args = parser.parse_args()

    store = _make_store(args)
    store.start()

    if args.once:
        deadline = time.monotonic() + 5.0
        while not store.snapshot() and time.monotonic() < deadline:
            time.sleep(0.05)
        from delegate_view.render import text_of
        for ln in views.render_list(store.snapshot(), 0, 80, 24,
                                    int(time.time() * 1000),
                                    status=store.status()):
            print(text_of(ln).rstrip())
        store.stop()
        return

    try:
        curses.wrapper(_curses_main, store, args)
    except KeyboardInterrupt:
        pass
    finally:
        store.stop()
        try:
            sys.stdout.write(MOUSE_OFF)
            sys.stdout.flush()
        except Exception:
            pass


def _make_store(args):
    """Build the run store, falling back to a one-shot load if unavailable."""
    from delegate_view.store import RunStore

    return RunStore(
        ledger_path=args.ledger_path,
        subagent_limit=args.subagent_limit,
        include_subagents=not args.no_subagents,
        interval=args.interval,
    )


def _curses_main(stdscr, store, args):
    curses.curs_set(0)
    curses.set_escdelay(25)
    stdscr.keypad(True)
    stdscr.timeout(TICK_MS)
    stdscr.nodelay(False)

    theme = Theme()
    theme.start()

    # Mouse: our own SGR mode, not curses' mousemask. See keys.py for why.
    try:
        sys.stdout.write(MOUSE_ON)
        sys.stdout.flush()
    except Exception:
        pass

    decoder = InputDecoder()
    loader = ConversationLoader()

    view = "list"
    sel = 0
    sel_key = None
    scroll = 0
    list_scroll = 0
    expanded = False
    spin_frame = 0
    last_spin = 0.0
    dragging = None      # ("convo"|"list", grab_offset) while a drag is active
    body: list = []
    wrapped: list = []
    wrapped_key = None
    title = ""

    while True:
        runs = store.snapshot()

        # Keep the selection on the same conversation across a refresh, which
        # inserts new runs at the top and would otherwise slide the highlight
        # onto something the user never chose.
        if sel_key is not None:
            idx = _index_of(runs, sel_key)
            if idx is not None:
                sel = idx
        if runs:
            sel = max(0, min(sel, len(runs) - 1))
            sel_key = _key_of(runs[sel])
        else:
            sel = 0
            sel_key = None

        h, w = stdscr.getmaxyx()
        split = view == "convo" and w >= views.SPLIT_MIN_WIDTH
        # Recomputed every frame from the same function the renderer uses, so
        # a click maps to the run actually drawn on that row.
        list_scroll = views.list_scroll_for(sel, list_scroll, h)

        now = time.monotonic()
        if now - last_spin > SPIN_MS / 1000:
            spin_frame += 1
            last_spin = now

        if view == "convo" and runs:
            loader.request(runs[sel])
            body, title = loader.result()

        # Wrap once, here, and use the SAME list for painting and for scroll
        # limits. Wrapping separately in the renderer is what made the last
        # screenful of a long conversation unreachable — the loop's idea of
        # how many lines there were came from the unwrapped blocks.
        convo_w = _convo_width(w, split)
        if (id(body), convo_w) != wrapped_key:
            wrapped = views.wrap_body(body, convo_w)
            wrapped_key = (id(body), convo_w)

        now_ms = int(time.time() * 1000)
        try:
            if view == "list":
                screen = views.render_list(
                    runs, sel, w, h, now_ms, scroll=list_scroll,
                    expanded=expanded, spin_frame=spin_frame,
                    status=store.status())
            elif split:
                screen = views.render_split(
                    runs, sel, wrapped, title, w, h, now_ms, scroll=scroll,
                    list_scroll=list_scroll, expanded=expanded,
                    spin_frame=spin_frame, status=store.status())
            else:
                screen = views.render_convo(title, wrapped, scroll, w, h)
        except Exception:
            _log.error("render failed: %s", traceback.format_exc())
            screen = []

        stdscr.erase()
        for y, line in enumerate(screen):
            if y >= h:
                break
            paint(stdscr, y, 0, line, theme)
        stdscr.refresh()

        # ── input ───────────────────────────────────────────────────────
        raw = stdscr.getch()
        if raw == curses.KEY_RESIZE:
            continue
        event = decoder.feed(raw)
        while event is not None:
            kind, value = event
            if kind == "mouse":
                res = _handle_mouse(value, view, split, runs, sel, scroll,
                                    list_scroll, w, h, len(wrapped), dragging)
                sel, scroll, list_scroll, dragging, opened = res
                if opened and runs:
                    view = "convo"
                    scroll = 0
                    sel_key = _key_of(runs[sel])
            else:
                action = resolve_key_action(value, view)
                if action == "quit":
                    return
                out = _handle_action(action, view, runs, sel, scroll,
                                     list_scroll, h, len(wrapped), expanded)
                view, sel, scroll, list_scroll, expanded, quit_now = out
                if quit_now:
                    return
                if runs and 0 <= sel < len(runs):
                    sel_key = _key_of(runs[sel])
            event = decoder.feed(-1)


def _key_of(run) -> tuple:
    sid = getattr(run, "session_id", "") or ""
    platform = getattr(run, "platform", "") or ""
    if sid:
        return (platform, sid)
    return (getattr(run, "transcript", "") or "", getattr(run, "started", 0))


def _index_of(runs, key) -> int | None:
    for i, r in enumerate(runs):
        if _key_of(r) == key:
            return i
    return None


def _handle_action(action, view, runs, sel, scroll, list_scroll, h,
                   body_len, expanded):
    """Apply a key action. Returns the new state tuple."""
    n = len(runs)
    body_h = views.convo_body_height(h)
    max_scroll = max(0, body_len - body_h)

    if action == "back":
        return "list", sel, 0, list_scroll, expanded, False
    if action == "toggle_expand":
        return view, sel, scroll, list_scroll, not expanded, False
    if action == "refresh":
        return view, sel, scroll, list_scroll, expanded, False

    if view == "list":
        if action == "up":
            sel = max(0, sel - 1)
        elif action == "down":
            sel = min(n - 1, sel + 1) if n else 0
        elif action == "top":
            sel = 0
        elif action == "bottom":
            sel = max(0, n - 1)
        elif action == "page_down":
            sel = min(n - 1, sel + max(1, h // 3)) if n else 0
        elif action == "page_up":
            sel = max(0, sel - max(1, h // 3))
        elif action == "half_page_down":
            sel = min(n - 1, sel + max(1, h // 6)) if n else 0
        elif action == "half_page_up":
            sel = max(0, sel - max(1, h // 6))
        elif action == "select" and n:
            return "convo", sel, 0, list_scroll, expanded, False
        return view, sel, scroll, list_scroll, expanded, False

    if action == "scroll_up":
        scroll = max(0, scroll - 1)
    elif action == "scroll_down":
        scroll = min(max_scroll, scroll + 1)
    elif action == "scroll_top":
        scroll = 0
    elif action == "scroll_bottom":
        scroll = max_scroll
    elif action == "page_down":
        scroll = min(max_scroll, scroll + body_h)
    elif action == "page_up":
        scroll = max(0, scroll - body_h)
    elif action == "half_page_down":
        scroll = min(max_scroll, scroll + body_h // 2)
    elif action == "half_page_up":
        scroll = max(0, scroll - body_h // 2)
    return view, sel, scroll, list_scroll, expanded, False


def _handle_mouse(ev, view, split, runs, sel, scroll, list_scroll, w, h,
                  body_len, dragging):
    """Wheel, click-to-select, and scroll-bar dragging.

    Returns (sel, scroll, list_scroll, dragging, opened).
    """
    opened = False
    body_h = views.convo_body_height(h)

    if ev.is_wheel:
        step = 3
        if view == "list" or (split and ev.x < _list_width(w)):
            sel = (max(0, sel - 1) if ev.is_wheel_up
                   else (min(len(runs) - 1, sel + 1) if runs else 0))
        else:
            if ev.is_wheel_up:
                scroll = max(0, scroll - step)
            else:
                scroll = min(max(0, body_len - body_h), scroll + step)
        return sel, scroll, list_scroll, dragging, opened

    # Release ends any drag.
    if not ev.pressed:
        return sel, scroll, list_scroll, None, opened

    if ev.is_motion:
        if dragging is not None:
            which, off = dragging
            row = ev.y - _body_top()
            if which == "convo":
                scroll = scroll_for_thumb_top(row - off, body_len, body_h)
        return sel, scroll, list_scroll, dragging, opened

    # A fresh press.
    bar_x = w - 1
    if ev.x >= bar_x and view != "list":
        row = ev.y - _body_top()
        thumb = thumb_for(scroll, body_len, body_h)
        if thumb is not None and thumb.covers(row):
            return sel, scroll, list_scroll, ("convo", grab_offset(row, thumb)), opened
        scroll = scroll_for_click(row, body_len, body_h)
        return sel, scroll, list_scroll, ("convo", 0), opened

    if view == "list" or (split and ev.x < _list_width(w)):
        idx = _row_to_index(ev.y, list_scroll)
        if idx is not None and 0 <= idx < len(runs):
            if idx == sel:
                opened = True
            sel = idx
    return sel, scroll, list_scroll, dragging, opened


def _convo_width(w: int, split: bool) -> int:
    """Columns the conversation body is wrapped to, in either layout."""
    return w - _list_width(w) - 1 if split else w


def _list_width(w: int) -> int:
    return max(34, w * 38 // 100)


def _body_top() -> int:
    """First screen row of the scrollable body, in both convo layouts."""
    return 2


def _row_to_index(y: int, list_scroll: int) -> int | None:
    """Which run a screen row belongs to, or None for header/footer/blank."""
    body_y = y - _body_top() + list_scroll
    if body_y < 0:
        return None
    per_item = views.ROWS_PER_RUN + 1
    if body_y % per_item == per_item - 1:
        return None          # the blank separator row
    return body_y // per_item


if __name__ == "__main__":
    main()
