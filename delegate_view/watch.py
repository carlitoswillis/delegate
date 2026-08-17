"""Terminal UI for watching live agent-to-agent conversations.

Pure rendering functions (render_list, render_convo, session_lines) return
list[str] with no curses calls or I/O, making them testable as plain data.
The curses loop in main() only paints lines returned by those functions.

resolve_key_action() is a pure mapping from (key, view) -> action string,
keeping key-handling logic testable without curses.
"""

from __future__ import annotations

import argparse
import curses
import os
import sys
import threading
import time
import textwrap
from dataclasses import dataclass, field


def _relative_age(now: int, started: int) -> str:
    """Human-readable relative age from now to started (both epoch ms)."""
    secs = max(0, (now - started) // 1000)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _truncate_path(path: str, width: int) -> str:
    """Truncate a path to fit in width chars, keeping the filename visible.

    Always returns at most width characters. When truncation is needed,
    shows leading ellipsis followed by as much of the filename as fits.
    """
    if len(path) <= width:
        return path
    if width < 3:
        return path[:width]
    name = path.rsplit("/", 1)[-1] if "/" in path else path
    avail = width - 1  # 1 char for the ellipsis
    if len(name) >= avail:
        return "\u2026" + name[-avail:]
    return "\u2026" + name


def _truncate_title(text: str, width: int) -> str:
    """Clip a session title from the END, keeping the opening words.

    The mirror image of _truncate_path. A title says what the agent was asked
    to do and says it first ("Fix a scoring bug in /Users/…"), so the front is
    the part worth keeping.
    """
    if len(text) <= width:
        return text
    if width < 2:
        return text[:width]
    return text[: width - 1] + "\u2026"


# ── Pure key-action mapping ──────────────────────────────────────────────

def resolve_key_action(key: int, view: str) -> str:
    """Map a curses key code + current view to an action string.

    Pure function, no curses calls.  Action strings:
        quit, back, up, down, top, bottom, select,
        scroll_up, scroll_down, scroll_top, scroll_bottom,
        page_up, page_down, half_page_up, half_page_down,
        mouse_up, mouse_down, noop
    """
    if view == "list":
        if key in (ord("q"), 27):           # q / Esc
            return "quit"
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
        if key in (curses.KEY_NPAGE,):
            return "page_down"
        if key in (curses.KEY_PPAGE,):
            return "page_up"
        if key == 4:                        # Ctrl-D
            return "half_page_down"
        if key == 21:                       # Ctrl-U
            return "half_page_up"
        if key == curses.KEY_MOUSE:
            return "mouse"
        if key in (curses.KEY_LEFT, ord("h")):
            return "noop"                   # left does nothing in list
        return "noop"

    # view == "convo"
    if key in (27, curses.KEY_LEFT, ord("h")):  # Esc / Left / h
        return "back"
    if key in (ord("q"),):
        return "back"                       # q in convo goes back, not quit
    if key in (curses.KEY_UP, ord("k")):
        return "scroll_up"
    if key in (curses.KEY_DOWN, ord("j")):
        return "scroll_down"
    if key in (ord("g"), curses.KEY_HOME):
        return "scroll_top"
    if key in (ord("G"), curses.KEY_END):
        return "scroll_bottom"
    if key in (curses.KEY_NPAGE,):
        return "page_down"
    if key in (curses.KEY_PPAGE,):
        return "page_up"
    if key == 4:                            # Ctrl-D
        return "half_page_down"
    if key == 21:                           # Ctrl-U
        return "half_page_up"
    if key == curses.KEY_MOUSE:
        return "mouse"
    return "noop"


# ── Conversation cache ──────────────────────────────────────────────────

class ConversationCache:
    """Caches loaded conversation lines keyed by (platform, session_id).

    Only reloads when the run is live AND the transcript file's mtime has
    changed since the cached copy.  Finished runs are never re-read.
    At most one reload per second.
    """

    def __init__(self):
        self._lines: dict[tuple, list[str]] = {}
        self._mtime: dict[tuple, float] = {}
        self._last_load: dict[tuple, float] = {}

    def get(self, run) -> list[str] | None:
        """Return cached lines or None if not cached."""
        key = self._key(run)
        return self._lines.get(key)

    def put(self, run, lines: list[str]):
        """Store lines for a run."""
        key = self._key(run)
        self._lines[key] = lines
        self._last_load[key] = time.monotonic()
        mtime = self._get_mtime(run)
        if mtime is not None:
            self._mtime[key] = mtime

    def needs_reload(self, run) -> bool:
        """True if the cache is stale or missing and a reload is due."""
        key = self._key(run)
        now_mono = time.monotonic()
        last = self._last_load.get(key, 0.0)
        # At most once per second
        if now_mono - last < 1.0:
            return False
        if key not in self._lines:
            return True
        # Finished runs: never re-read
        if not run.live:
            return False
        # Live run: re-read only if mtime changed
        cur_mtime = self._get_mtime(run)
        cached_mtime = self._mtime.get(key)
        if cur_mtime is not None and cur_mtime != cached_mtime:
            return True
        return False

    @staticmethod
    def _key(run) -> tuple:
        return (getattr(run, "platform", "") or "",
                getattr(run, "session_id", "") or "")

    @staticmethod
    def _get_mtime(run) -> float | None:
        transcript = getattr(run, "transcript", "")
        if not transcript:
            return None
        try:
            return os.path.getmtime(transcript)
        except OSError:
            return None


# ── Pure rendering functions ─────────────────────────────────────────────

def render_list(
    runs: list, selected: int, width: int, height: int, now: int,
    header: str = "",
) -> list[str]:
    """Render the landing list screen.

    Pure function: returns list[str] where each element is one screen line
    of exactly width chars or fewer.

    Args:
        runs: list of Run objects (newest first).
        selected: index of the currently highlighted run.
        width: terminal width in columns.
        height: terminal height in rows.
        now: current epoch ms for age calculations.
        header: optional status text shown in the first line (e.g.
                "loading subagents…").
    """
    lines: list[str] = []
    first_line = header if header else "Agent Runs"
    lines.append(first_line.ljust(width)[:width])
    lines.append("\u2500" * width)

    if not runs:
        msg = "  No agent runs yet."
        lines.append(msg.ljust(width)[:width])
        blank_count = height - 4
        for _ in range(max(0, blank_count)):
            lines.append("".ljust(width)[:width])
        lines.append(
            "  q: quit".ljust(width)[:width]
        )
        return lines

    view_height = height - 3  # header (2 lines) + footer (1 line)
    view_height = max(1, view_height)

    if selected < 0:
        selected = 0
    if selected >= len(runs):
        selected = len(runs) - 1

    scroll = 0
    if selected >= scroll + view_height:
        scroll = selected - view_height + 1
    if selected < scroll:
        scroll = selected

    visible = runs[scroll : scroll + view_height]

    for i, run in enumerate(visible):
        idx = scroll + i
        is_selected = (idx == selected)
        gutter = ">" if is_selected else " "
        marker = "\u25cf" if run.live else "\u00b7"
        age = _relative_age(now, run.started)
        model = getattr(run, "model", "") or ""
        prompt = getattr(run, "prompt", "") or ""
        is_path = bool(prompt)
        if not is_path:
            prompt = getattr(run, "prompt_text", "") or ""

        # Truncate model and prompt to fit together
        # gutter(1) + space(1) + marker(1) + space(1) + age + space(1)
        fixed = 1 + 1 + len(marker) + 1 + len(age) + 1
        remaining = width - fixed
        if remaining < 10:
            row = (f"{gutter} {marker} {age}").ljust(width)[:width]
            lines.append(row)
            continue

        model_budget = min(25, remaining // 3)
        if len(model) > model_budget:
            model_str = model[: model_budget - 1] + "\u2026"
        else:
            model_str = model
        prompt_budget = remaining - len(model_str) - 1
        if prompt_budget < 1:
            prompt_budget = 1
        if is_path:
            prompt_trunc = _truncate_path(prompt, prompt_budget)
        else:
            prompt_trunc = _truncate_title(prompt, prompt_budget)

        live_tag = " live" if run.live else ""
        row = f"{gutter} {marker} {age}{live_tag} {model_str} {prompt_trunc}"
        lines.append(row.ljust(width)[:width])

    # Pad to view_height
    while len(lines) < 2 + view_height:
        lines.append("".ljust(width)[:width])

    lines.append(
        "  j/k/\u2191\u2193:move  Enter:open  q:quit".ljust(width)[:width]
    )
    return lines


def session_lines(session) -> list[str]:
    """Convert a Session object into displayable flat lines.

    Pure function: no I/O, no curses.
    """
    from delegate_view.schema import Event

    lines: list[str] = []
    for ev in session.events:
        if ev.kind == "text":
            role_prefix = ">" if ev.role == "user" else " "
            text = ev.text.replace("\r\n", "\n").replace("\r", "\n")
            for para in text.split("\n"):
                lines.append(f"{role_prefix} {para}")
        elif ev.kind == "reasoning":
            truncated = ev.text[:200]
            if len(ev.text) > 200:
                truncated += "\u2026"
            for ln in truncated.split("\n"):
                lines.append(f"  ~ {ln}")
        elif ev.kind == "tool_call":
            ms = f"{ev.tool_ms}ms" if ev.tool_ms is not None else "pending"
            lines.append(
                f"  \u2192 {ev.tool_name} ({ev.tool_status}, {ms})"
            )
            if ev.tool_output:
                out_lines = ev.tool_output.splitlines()
                shown = out_lines[:10]
                for ln in shown:
                    lines.append(f"    {ln}")
                if len(out_lines) > 10:
                    lines.append("    \u2026")
        elif ev.kind == "patch":
            files = ev.raw.get("files", [])
            lines.append("  \u00b1 patched: " + ", ".join(files))
        elif ev.kind == "compaction":
            lines.append("\u2500\u2500\u2500 context compacted \u2500\u2500\u2500")
    return lines


def render_convo(
    title: str, lines: list[str], scroll: int, width: int, height: int
) -> list[str]:
    """Render the conversation view.

    Pure function: returns list[str] where each element is one screen line
    of exactly width chars or fewer.

    Args:
        title: header text.
        lines: conversation lines (already formatted, not yet word-wrapped).
        scroll: top of the visible window into the wrapped lines.
        width: terminal width.
        height: terminal height.
    """
    screen: list[str] = []
    screen.append(f"  {title}".ljust(width)[:width])
    screen.append("\u2500" * width)

    # Word-wrap all conversation lines
    wrapped: list[str] = []
    indent = 2
    wrap_width = max(1, width - indent)
    for ln in lines:
        if ln == "":
            wrapped.append("")
        else:
            wrapped.extend(textwrap.wrap(ln, wrap_width) or [""])

    view_height = height - 3
    view_height = max(1, view_height)

    total = len(wrapped)
    if scroll < 0:
        scroll = 0
    if total > 0 and scroll > total - view_height:
        scroll = max(0, total - view_height)

    visible = wrapped[scroll : scroll + view_height]
    for ln in visible:
        padded = ("  " + ln).ljust(width)[:width]
        screen.append(padded)

    while len(screen) < 2 + view_height:
        screen.append("".ljust(width)[:width])

    pos = f"{scroll + 1}-{min(scroll + view_height, total)}/{total}"
    footer = f"  j/k/\u2191\u2193:scroll  Esc:back  {pos}"
    screen.append(footer.ljust(width)[:width])
    return screen


def _render_once(runs, width, height, now, header=""):
    """Single-frame render for --once mode (no curses needed)."""
    lines = render_list(runs, 0, width, height, now, header=header)
    for line in lines:
        print(line)


# ── Conversation loader (factored out for caching) ──────────────────────

def _load_conversation(run) -> list[str]:
    """Load conversation lines for a run.

    Tries adapters.load_session first, falls back to tail().
    """
    convo_lines: list[str] | None = None

    try:
        from delegate_view import adapters
        session = adapters.load_session(
            run.platform, run.session_id
        )
        convo_lines = session_lines(session)
    except Exception:
        pass

    if convo_lines is None:
        try:
            from delegate_view.runs import tail
            raw = tail(run.transcript, 500)
            convo_lines = raw if isinstance(raw, list) else raw.splitlines()
        except Exception:
            convo_lines = ["(unable to load conversation)"]

    return convo_lines


# ── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Watch live agent-to-agent conversations"
    )
    parser.add_argument(
        "--ledger", dest="ledger_path", default=None,
        help="Path to the ledger directory"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Render one frame to stdout and exit (no curses)"
    )
    parser.add_argument(
        "--no-subagents", action="store_true",
        help="Show only delegate.sh runs, not Claude Code subagents"
    )
    parser.add_argument(
        "--subagent-limit", type=int, default=25,
        help="How many recent subagent conversations to include (default 25)"
    )
    args = parser.parse_args()

    try:
        from delegate_view.runs import load_runs
    except ImportError:
        def load_runs(ledger_path=None):
            return []

    runs = []  # start empty so TUI opens immediately
    runs_lock = threading.Lock()

    def _load_runs_bg():
        nonlocal runs
        try:
            fresh = load_runs(ledger_path=args.ledger_path, resolve=True)
            with runs_lock:
                runs.extend(fresh)
                runs.sort(key=lambda r: r.started, reverse=True)
        except Exception:
            pass

    load_thread = threading.Thread(target=_load_runs_bg, daemon=True)
    load_thread.start()

    subagent_status = ""  # shown in header while loading
    subagent_done = threading.Event()

    # Two kinds of agent-to-agent conversation, one list. The ledger covers
    # work handed out by delegate.sh; subagents covers the ones Claude Code
    # spawns itself, which never touch the ledger. Either source failing
    # should cost you that half, not the whole screen.
    if not args.no_subagents and args.subagent_limit != 0:
        subagent_status = "loading subagents\u2026"

        def _load_subagents():
            try:
                from delegate_view.subagents import load_subagent_runs
                sa_runs = load_subagent_runs(limit=args.subagent_limit)
                with runs_lock:
                    runs.extend(sa_runs)
                    runs.sort(key=lambda r: r.started, reverse=True)
            except Exception:
                pass
            finally:
                nonlocal subagent_status
                subagent_status = ""
                subagent_done.set()

        sa_thread = threading.Thread(target=_load_subagents, daemon=True)
        sa_thread.start()
    else:
        subagent_done.set()

    runs.sort(key=lambda r: r.started, reverse=True)

    if args.once:
        # Wait for subagents to finish so --once shows the full picture
        subagent_done.wait(timeout=5.0)
        _render_once(runs, 80, 24, int(time.time() * 1000),
                     header=subagent_status)
        return

    def _curses_main(stdscr):
        nonlocal subagent_status

        curses.curs_set(0)
        curses.set_escdelay(25)
        stdscr.timeout(20)

        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)

        # Enable mouse events
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            # Enable xterm mouse protocol for terminals that need it
            sys.stdout.write("\033[?1003h")
            sys.stdout.flush()
        except Exception:
            pass

        sel = 0
        view = "list"
        scroll = 0
        last_action = "noop"
        last_key = -1
        last_key_time = 0
        repeat_delay_ms = 250
        convo_cache = ConversationCache()
        cached_convo_lines: list[str] = []
        cached_convo_title: str = ""

        def _load_and_cache_convo(run_obj):
            """Load conversation for run_obj, using cache when valid."""
            lines = convo_cache.get(run_obj)
            if lines is not None and not convo_cache.needs_reload(run_obj):
                return lines
            lines = _load_conversation(run_obj)
            convo_cache.put(run_obj, lines)
            return lines

        while True:
            try:
                h, w = stdscr.getmaxyx()
            except curses.error:
                h, w = 24, 80

            now_ms = int(time.time() * 1000)

            if view == "list":
                key = stdscr.getch()
                if key != -1 and key == last_key and now_ms - last_key_time < repeat_delay_ms:
                    key = -1  # debounce: treat rapid same-key as no input
                if key == -1:
                    if last_action in ("up", "down") and now_ms - last_key_time >= repeat_delay_ms:
                        if last_action == "up":
                            sel = max(0, sel - 1)
                        else:
                            n = len(runs_snapshot)
                            sel = min(n - 1, sel + 1) if n else 0
                        stdscr.erase()
                        with runs_lock:
                            runs_snapshot = list(runs)
                        now_ms = int(time.time() * 1000)
                        text = render_list(runs_snapshot, sel, w, h, now_ms,
                                           header=subagent_status)
                        for i, ln in enumerate(text):
                            try:
                                if i < h and 0 <= i < h - 1:
                                    stdscr.addnstr(i, 0, ln, w, 0)
                            except curses.error:
                                pass
                        stdscr.refresh()
                    continue

                with runs_lock:
                    runs_snapshot = list(runs)
                action = resolve_key_action(key, "list")
                if action == "quit":
                    break
                elif action == "up":
                    sel = max(0, sel - 1)
                    last_action = "up"
                    last_key = key
                    last_key_time = now_ms
                elif action == "down":
                    n = len(runs_snapshot)
                    sel = min(n - 1, sel + 1) if n else 0
                    last_action = "down"
                    last_key = key
                    last_key_time = now_ms
                elif action == "top":
                    sel = 0
                    last_action = "noop"
                    last_key = -1
                elif action == "bottom":
                    sel = len(runs_snapshot) - 1 if runs_snapshot else 0
                    last_action = "noop"
                    last_key = -1
                elif action == "select":
                    if runs_snapshot and 0 <= sel < len(runs_snapshot):
                        view = "convo"
                        scroll = 0
                        last_action = "noop"
                        last_key = -1
                        run_obj = runs_snapshot[sel]
                        cached_convo_lines = _load_and_cache_convo(run_obj)
                        cached_convo_title = (
                            getattr(run_obj, "prompt", "")
                            or getattr(run_obj, "prompt_text", "")
                            or "Conversation"
                        )
                elif action == "page_down":
                    page = max(1, h - 3)
                    n = len(runs_snapshot)
                    sel = min(n - 1, sel + page) if n else 0
                    last_action = "noop"
                    last_key = -1
                elif action == "page_up":
                    page = max(1, h - 3)
                    sel = max(0, sel - page)
                    last_action = "noop"
                    last_key = -1
                elif action == "half_page_down":
                    half = max(1, (h - 3) // 2)
                    n = len(runs_snapshot)
                    sel = min(n - 1, sel + half) if n else 0
                    last_action = "noop"
                    last_key = -1
                elif action == "half_page_up":
                    half = max(1, (h - 3) // 2)
                    sel = max(0, sel - half)
                    last_action = "noop"
                    last_key = -1
                elif action == "mouse":
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                    except Exception:
                        continue
                    if bstate & curses.BUTTON4_PRESSED:
                        sel = max(0, sel - 3)
                    elif bstate & curses.BUTTON5_PRESSED:
                        n = len(runs_snapshot)
                        sel = min(n - 1, sel + 3) if n else 0
                    last_action = "noop"
                    last_key = -1
                else:
                    last_action = "noop"
                    last_key = -1

                stdscr.erase()
                now_ms = int(time.time() * 1000)
                text = render_list(runs_snapshot, sel, w, h, now_ms,
                                   header=subagent_status)
                for i, ln in enumerate(text):
                    try:
                        if i < h and 0 <= i < h - 1:
                            stdscr.addnstr(i, 0, ln, w, 0)
                    except curses.error:
                        pass
                stdscr.refresh()

            elif view == "convo":
                with runs_lock:
                    runs_snapshot = list(runs)
                run_obj = (runs_snapshot[sel]
                           if 0 <= sel < len(runs_snapshot) else None)
                if run_obj is None:
                    view = "list"
                    continue

                convo_lines = cached_convo_lines

                key = stdscr.getch()
                if key == -1:
                    if last_action in ("scroll_up", "scroll_down") and now_ms - last_key_time >= repeat_delay_ms:
                        max_scroll = max(0, len(convo_lines) - (h - 3))
                        if last_action == "scroll_up":
                            scroll = max(0, scroll - 1)
                        else:
                            scroll = min(max_scroll, scroll + 1)
                    elif not (run_obj.live and convo_cache.needs_reload(run_obj)):
                        continue

                    if run_obj.live and convo_cache.needs_reload(run_obj):
                        cached_convo_lines = _load_and_cache_convo(run_obj)
                        cached_convo_title = (
                            getattr(run_obj, "prompt", "")
                            or getattr(run_obj, "prompt_text", "")
                            or "Conversation"
                        )
                        convo_lines = cached_convo_lines

                    if run_obj.live and scroll >= len(convo_lines) - (h - 3) - 1:
                        scroll = max(0, len(convo_lines) - (h - 3))

                    stdscr.erase()
                    text = render_convo(
                        cached_convo_title, convo_lines, scroll, w, h
                    )
                    for i, ln in enumerate(text):
                        try:
                            if i < h and 0 <= i < h - 1:
                                stdscr.addnstr(i, 0, ln, w, 0)
                        except curses.error:
                            pass
                    stdscr.refresh()
                    continue

                if key == curses.KEY_MOUSE:
                    try:
                        _, mx, my, _, bstate = curses.getmouse()
                    except Exception:
                        continue
                    max_scroll = max(0, len(convo_lines) - (h - 3))
                    if bstate & curses.BUTTON4_PRESSED:
                        scroll = max(0, scroll - 3)
                    elif bstate & curses.BUTTON5_PRESSED:
                        scroll = min(max_scroll, scroll + 3)
                    last_action = "noop"
                    last_key = -1
                    continue

                action = resolve_key_action(key, "convo")
                max_scroll = max(0, len(convo_lines) - (h - 3))
                if action == "back":
                    view = "list"
                    scroll = 0
                    last_action = "noop"
                    last_key = -1
                elif action == "scroll_up":
                    scroll = max(0, scroll - 1)
                    last_action = "scroll_up"
                    last_key = key
                    last_key_time = now_ms
                elif action == "scroll_down":
                    scroll = min(max_scroll, scroll + 1)
                    last_action = "scroll_down"
                    last_key = key
                    last_key_time = now_ms
                elif action == "scroll_top":
                    scroll = 0
                    last_action = "noop"
                    last_key = -1
                elif action == "scroll_bottom":
                    scroll = max_scroll
                    last_action = "noop"
                    last_key = -1
                elif action == "page_down":
                    page = max(1, h - 3)
                    scroll = min(max_scroll, scroll + page)
                    last_action = "noop"
                    last_key = -1
                elif action == "page_up":
                    page = max(1, h - 3)
                    scroll = max(0, scroll - page)
                    last_action = "noop"
                    last_key = -1
                elif action == "half_page_down":
                    half = max(1, (h - 3) // 2)
                    scroll = min(max_scroll, scroll + half)
                    last_action = "noop"
                    last_key = -1
                elif action == "half_page_up":
                    half = max(1, (h - 3) // 2)
                    scroll = max(0, scroll - half)
                    last_action = "noop"
                    last_key = -1
                else:
                    last_action = "noop"
                    last_key = -1

                if convo_cache.needs_reload(run_obj):
                    cached_convo_lines = _load_and_cache_convo(run_obj)
                    cached_convo_title = (
                        getattr(run_obj, "prompt", "")
                        or getattr(run_obj, "prompt_text", "")
                        or "Conversation"
                    )
                    convo_lines = cached_convo_lines

                convo_lines = cached_convo_lines
                if run_obj.live and scroll >= len(convo_lines) - (h - 3) - 1:
                    scroll = max(0, len(convo_lines) - (h - 3))

                stdscr.erase()
                text = render_convo(
                    cached_convo_title, convo_lines, scroll, w, h
                )
                for i, ln in enumerate(text):
                    try:
                        if i < h and 0 <= i < h - 1:
                            stdscr.addnstr(i, 0, ln, w, 0)
                    except curses.error:
                        pass
                stdscr.refresh()

    try:
        curses.wrapper(_curses_main)
    except KeyboardInterrupt:
        pass
    except Exception:
        pass
    finally:
        # Restore terminal: disable xterm mouse reporting
        try:
            sys.stdout.write("\033[?1003l")
            sys.stdout.flush()
        except Exception:
            pass


if __name__ == "__main__":
    main()
