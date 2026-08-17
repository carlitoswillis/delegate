"""Terminal UI for watching live agent-to-agent conversations.

Pure rendering functions (render_list, render_convo, session_lines) return
list[str] with no curses calls or I/O, making them testable as plain data.
The curses loop in main() only paints lines returned by those functions.
"""

from __future__ import annotations

import argparse
import curses
import time
import textwrap
from dataclasses import dataclass


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


def render_list(
    runs: list, selected: int, width: int, height: int, now: int
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
    """
    lines: list[str] = []
    lines.append("Agent Runs".ljust(width)[:width])
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
        marker = "\u25cf" if run.live else "\u00b7"
        age = _relative_age(now, run.started)
        model = getattr(run, "model", "") or ""
        prompt = getattr(run, "prompt", "") or ""

        # Truncate model and prompt to fit together
        # Available: width - (marker + age + padding + model padding) 
        # Rough budget: marker(1) + space(1) + age(3-4) + space(1) + rest
        fixed = len(marker) + 1 + len(age) + 1  # marker + space + age + space
        remaining = width - fixed
        if remaining < 10:
            row = (f"{marker} {age}").ljust(width)[:width]
            lines.append(row)
            continue

        # Split remaining between model and prompt
        # Give model up to 25 chars, rest to prompt
        model_budget = min(25, remaining // 3)
        if len(model) > model_budget:
            model_str = model[: model_budget - 1] + "\u2026"
        else:
            model_str = model
        prompt_budget = remaining - len(model_str) - 1  # -1 for separator space
        if prompt_budget < 1:
            prompt_budget = 1
        prompt_trunc = _truncate_path(prompt, prompt_budget)

        live_tag = " live" if run.live else ""
        row = f"{marker} {age}{live_tag} {model_str} {prompt_trunc}"
        lines.append(row.ljust(width)[:width])

    # Pad to view_height
    while len(lines) < 2 + view_height:  # 2 header lines
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


def _render_once(runs, width, height, now):
    """Single-frame render for --once mode (no curses needed)."""
    lines = render_list(runs, 0, width, height, now)
    for line in lines:
        print(line)


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
    args = parser.parse_args()

    try:
        from delegate_view.runs import load_runs
    except ImportError:
        def load_runs(ledger_path=None):
            return []

    runs = load_runs(ledger_path=args.ledger_path)

    if args.once:
        _render_once(runs, 80, 24, int(time.time() * 1000))
        return

    def _curses_main(stdscr):
        curses.curs_set(0)
        stdscr.timeout(1000)

        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)

        current_line = 0
        current_col = 0
        sel = 0
        view = "list"
        scroll = 0

        while True:
            try:
                h, w = stdscr.getmaxyx()
            except curses.error:
                h, w = 24, 80

            now_ms = int(time.time() * 1000)

            if view == "list":
                stdscr.erase()
                text = render_list(runs, sel, w, h, now_ms)
                for i, ln in enumerate(text):
                    try:
                        if i < h and 0 <= i < h - 1:
                            run_obj = runs[sel] if sel < len(runs) else None
                            attr = curses.color_pair(1) if (
                                run_obj and run_obj.live and i == sel + 2
                            ) else 0
                            stdscr.addnstr(i, 0, ln, w, attr)
                    except curses.error:
                        pass
                stdscr.refresh()

                key = stdscr.getch()
                if key == -1:
                    continue
                elif key in (ord("q"), 27):
                    break
                elif key in (curses.KEY_UP, ord("k")):
                    sel = max(0, sel - 1)
                elif key in (curses.KEY_DOWN, ord("j")):
                    sel = min(len(runs) - 1, sel + 1) if runs else 0
                elif key in (ord("g"), curses.KEY_HOME):
                    sel = 0
                elif key in (ord("G"), curses.KEY_END):
                    sel = len(runs) - 1 if runs else 0
                elif key in (10, 13, curses.KEY_ENTER):
                    if runs and 0 <= sel < len(runs):
                        view = "convo"
                        scroll = 0
                        current_line = 0
                        current_col = 0

            elif view == "convo":
                run_obj = runs[sel] if 0 <= sel < len(runs) else None
                if run_obj is None:
                    view = "list"
                    continue

                convo_title = getattr(run_obj, "prompt", "") or "Conversation"
                convo_lines: list[str] | None = None
                load_session = None

                try:
                    from delegate_view import adapters
                    session = adapters.load_session(
                        run_obj.platform, run_obj.session_id
                    )
                    convo_lines = session_lines(session)
                except Exception:
                    pass

                if convo_lines is None:
                    try:
                        from delegate_view.runs import tail
                        raw = tail(run_obj.transcript, 500)
                        convo_lines = raw if isinstance(raw, list) else raw.splitlines()
                    except Exception:
                        convo_lines = ["(unable to load conversation)"]

                # Auto-follow live runs
                if run_obj.live and scroll >= len(convo_lines) - (h - 3) - 1:
                    scroll = max(0, len(convo_lines) - (h - 3))

                stdscr.erase()
                text = render_convo(convo_title, convo_lines, scroll, w, h)
                for i, ln in enumerate(text):
                    try:
                        if i < h and 0 <= i < h - 1:
                            stdscr.addnstr(i, 0, ln, w, 0)
                    except curses.error:
                        pass
                stdscr.refresh()

                key = stdscr.getch()
                if key == -1:
                    continue
                elif key in (ord("q"), 27):
                    view = "list"
                    scroll = 0
                elif key in (curses.KEY_UP, ord("k")):
                    scroll = max(0, scroll - 1)
                elif key in (curses.KEY_DOWN, ord("j")):
                    scroll = min(
                        max(0, len(convo_lines) - (h - 3)), scroll + 1
                    )
                elif key in (ord("g"), curses.KEY_HOME):
                    scroll = 0
                elif key in (ord("G"), curses.KEY_END):
                    scroll = max(0, len(convo_lines) - (h - 3))

    try:
        curses.wrapper(_curses_main)
    except Exception:
        pass


if __name__ == "__main__":
    main()
