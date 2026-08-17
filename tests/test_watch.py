"""Tests for the TUI: the view functions and the loop's pure helpers.

Ported from the tag-string renderer these replaced. The assertions are about
behaviour that survived the rewrite — a live run looks different from a
finished one, the selected row is marked and is the *right* row, nothing
overflows its width — rather than about the markup the old renderer emitted,
which was the thing being removed.
"""

from __future__ import annotations

import curses
import unittest

from delegate_view import views
from delegate_view.render import Span, text_of, width_of
from delegate_view.watch import (
    _handle_action,
    _index_of,
    _key_of,
    _row_to_index,
    resolve_key_action,
)


class FakeRun:
    def __init__(self, started=0, prompt="/p/tasks/task.md", model="m",
                 live=False, tokens_in=0, tokens_out=0, cost=0.0,
                 prompt_text="", session_id="", platform="", cwd="/p",
                 transcript="/p/t.log"):
        self.started = started
        self.prompt = prompt
        self.model = model
        self.live = live
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.cost = cost
        self.prompt_text = prompt_text
        self.session_id = session_id
        self.platform = platform
        self.cwd = cwd
        self.transcript = transcript


def _runs(n, **kw):
    return [FakeRun(started=1000 * (n - i), prompt=f"/p/tasks/t{i}.md", **kw)
            for i in range(n)]


def _plain(screen):
    return [text_of(ln) for ln in screen]


NOW = 10_000_000


# ── list view ───────────────────────────────────────────────────────────

class ListWidthTests(unittest.TestCase):
    def test_no_line_exceeds_the_width(self):
        for w in (40, 60, 80, 120, 200):
            screen = views.render_list(_runs(6), 2, w, 24, NOW)
            for ln in screen:
                self.assertLessEqual(width_of(ln), w, f"width={w}")

    def test_narrow_terminal_does_not_raise(self):
        for w in (10, 20, 30):
            views.render_list(_runs(3), 0, w, 12, NOW)

    def test_short_terminal_does_not_raise(self):
        for h in (4, 6, 10):
            views.render_list(_runs(5), 0, 80, h, NOW)


class ListMarkerTests(unittest.TestCase):
    def test_live_and_finished_runs_look_different(self):
        live = _plain(views.render_list([FakeRun(live=True)], 0, 80, 12, NOW))
        idle = _plain(views.render_list([FakeRun(live=False)], 0, 80, 12, NOW))
        self.assertNotEqual(live, idle)

    def test_live_run_shows_a_spinner_frame(self):
        out = "\n".join(_plain(
            views.render_list([FakeRun(live=True)], 0, 80, 12, NOW,
                              spin_frame=0)))
        self.assertIn(views.SPINNER[0], out)

    def test_finished_run_shows_a_dot_not_a_spinner(self):
        out = "\n".join(_plain(
            views.render_list([FakeRun(live=False)], 0, 80, 12, NOW)))
        self.assertIn("·", out)
        for frame in views.SPINNER:
            self.assertNotIn(frame, out)


class SelectionMarkerTests(unittest.TestCase):
    def test_selected_row_is_marked(self):
        out = _plain(views.render_list(_runs(3), 0, 80, 16, NOW))
        self.assertTrue(any("▸" in ln for ln in out))

    def test_marker_is_on_the_selected_row_not_another(self):
        for sel in (0, 1, 2):
            out = _plain(views.render_list(_runs(3), sel, 80, 20, NOW))
            marked = [ln for ln in out if "▸" in ln]
            self.assertEqual(len(marked), 1, f"sel={sel}")
            self.assertIn(f"t{sel}", marked[0], f"sel={sel}")

    def test_selected_differs_from_unselected_for_the_same_run(self):
        a = _plain(views.render_list(_runs(2), 0, 80, 16, NOW))
        b = _plain(views.render_list(_runs(2), 1, 80, 16, NOW))
        self.assertNotEqual(a, b)

    def test_a_live_selected_run_is_still_marked(self):
        out = _plain(views.render_list(
            [FakeRun(live=True), FakeRun(live=True)], 1, 80, 16, NOW))
        self.assertTrue(any("▸" in ln for ln in out))


class ListScrollTests(unittest.TestCase):
    def test_selection_past_the_fold_stays_visible(self):
        runs = _runs(40)
        screen = views.render_list(runs, 39, 80, 20, NOW)
        self.assertIn("t39", "\n".join(_plain(screen)))

    def test_first_selection_shows_the_first_run(self):
        screen = views.render_list(_runs(40), 0, 80, 20, NOW)
        self.assertIn("t0", "\n".join(_plain(screen)))


class ListEmptyTests(unittest.TestCase):
    def test_empty_list_says_so_and_still_fills_the_screen(self):
        screen = views.render_list([], 0, 80, 24, NOW)
        self.assertIn("no runs yet", "\n".join(_plain(screen)))
        for ln in screen:
            self.assertLessEqual(width_of(ln), 80)

    def test_header_reports_counts(self):
        out = "\n".join(_plain(views.render_list(
            [FakeRun(live=True), FakeRun()], 0, 80, 12, NOW)))
        self.assertIn("2 runs", out)
        self.assertIn("1 live", out)

    def test_status_text_reaches_the_header(self):
        out = "\n".join(_plain(views.render_list(
            _runs(1), 0, 80, 12, NOW, status="loading subagents…")))
        self.assertIn("loading subagents", out)


class ExpandedTests(unittest.TestCase):
    def test_expanded_shows_more_than_collapsed(self):
        run = FakeRun(tokens_in=1000, tokens_out=500, cost=1.25, cwd="/a/proj")
        plain = "\n".join(_plain(views.render_list([run], 0, 80, 12, NOW,
                                                   expanded=False)))
        wide = "\n".join(_plain(views.render_list([run], 0, 80, 12, NOW,
                                                  expanded=True)))
        self.assertNotEqual(plain, wide)
        self.assertIn("proj", wide)


# ── formatters ──────────────────────────────────────────────────────────

class RelativeAgeTests(unittest.TestCase):
    def test_scales(self):
        self.assertEqual(views.relative_age(30_000, 30_000), "0s")
        self.assertEqual(views.relative_age(35_000, 30_000), "5s")
        self.assertEqual(views.relative_age(90_000, 30_000), "1m")
        self.assertEqual(views.relative_age(3_630_000, 30_000), "1h")
        self.assertEqual(views.relative_age(86_430_000, 30_000), "1d")

    def test_future_timestamps_do_not_go_negative(self):
        self.assertEqual(views.relative_age(0, 5000), "0s")


class TokenAndCostFormatTests(unittest.TestCase):
    def test_tokens(self):
        self.assertEqual(views.format_tokens(0), "")
        self.assertEqual(views.format_tokens(999), "999")
        self.assertEqual(views.format_tokens(1200), "1.2K")
        self.assertEqual(views.format_tokens(45_000), "45K")
        self.assertEqual(views.format_tokens(3_400_000), "3.4M")

    def test_cost(self):
        self.assertEqual(views.format_cost(0), "")
        self.assertEqual(views.format_cost(3.4), "$3.40")
        self.assertEqual(views.format_cost(0.5), "$0.50")


class TaskNameTests(unittest.TestCase):
    def test_reduces_a_path_to_its_stem(self):
        self.assertEqual(views.task_name("/a/b/tasks/live-refresh.md"),
                         "live-refresh")

    def test_handles_a_bare_name(self):
        self.assertEqual(views.task_name("thing"), "thing")

    def test_subagent_run_uses_its_prompt_text(self):
        run = FakeRun(prompt="", prompt_text="Find the scoring bug")
        title, is_path = views.run_title(run)
        self.assertEqual(title, "Find the scoring bug")
        self.assertFalse(is_path)


# ── conversation view ───────────────────────────────────────────────────

class FakeEvent:
    def __init__(self, kind, role="assistant", text="", tool_name="",
                 tool_output="", tool_status="completed", tool_ms=5, raw=None):
        self.kind = kind
        self.role = role
        self.text = text
        self.tool_name = tool_name
        self.tool_output = tool_output
        self.tool_status = tool_status
        self.tool_ms = tool_ms
        self.raw = raw or {}


class FakeSession:
    def __init__(self, events, model="opencode/big-pickle",
                 platform="opencode"):
        self.events = events
        self.model = model
        self.platform = platform


class SessionBlockTests(unittest.TestCase):
    def test_user_turn_is_labelled_by_delegator_not_the_word_user(self):
        s = FakeSession([FakeEvent("text", role="user", text="do the thing")])
        out = "\n".join(text_of(l) for l in views.session_blocks(s))
        self.assertIn("you → big-pickle", out)
        self.assertNotIn("user", out)

    def test_assistant_turn_is_labelled_with_the_model(self):
        s = FakeSession([FakeEvent("text", role="assistant", text="done")])
        out = "\n".join(text_of(l) for l in views.session_blocks(s))
        self.assertIn("big-pickle", out)

    def test_subagent_prompt_is_attributed_to_claude(self):
        s = FakeSession([FakeEvent("text", role="user", text="go")],
                        model="claude-sonnet-5", platform="claude-code")
        out = "\n".join(text_of(l)
                        for l in views.session_blocks(s, is_subagent=True))
        self.assertIn("claude →", out)

    def test_reasoning_is_truncated(self):
        s = FakeSession([FakeEvent("reasoning", text="x" * 500)])
        out = "\n".join(text_of(l) for l in views.session_blocks(s))
        self.assertIn("…", out)
        self.assertLess(len(out), 400)

    def test_tool_call_shows_name_and_timing(self):
        s = FakeSession([FakeEvent("tool_call", tool_name="read", tool_ms=12)])
        out = "\n".join(text_of(l) for l in views.session_blocks(s))
        self.assertIn("read", out)
        self.assertIn("12ms", out)

    def test_failed_tool_call_is_marked_differently(self):
        ok = FakeSession([FakeEvent("tool_call", tool_name="read")])
        bad = FakeSession([FakeEvent("tool_call", tool_name="read",
                                     tool_status="error")])
        self.assertNotEqual(
            "\n".join(text_of(l) for l in views.session_blocks(ok)),
            "\n".join(text_of(l) for l in views.session_blocks(bad)))

    def test_long_tool_output_is_capped_with_a_count(self):
        s = FakeSession([FakeEvent("tool_call", tool_name="ls",
                                   tool_output="\n".join(str(i)
                                                         for i in range(40)))])
        out = "\n".join(text_of(l) for l in views.session_blocks(s))
        self.assertIn("more lines", out)

    def test_patch_lists_its_files(self):
        s = FakeSession([FakeEvent("patch", raw={"files": ["a.py", "b.py"]})])
        out = "\n".join(text_of(l) for l in views.session_blocks(s))
        self.assertIn("a.py", out)
        self.assertIn("b.py", out)

    def test_compaction_is_marked(self):
        s = FakeSession([FakeEvent("compaction")])
        out = "\n".join(text_of(l) for l in views.session_blocks(s))
        self.assertIn("compacted", out)


class ConvoRenderTests(unittest.TestCase):
    def _body(self, n=60):
        s = FakeSession([FakeEvent("text", text=f"line {i}") for i in range(n)])
        return views.session_blocks(s)

    def test_no_line_exceeds_width(self):
        for w in (40, 80, 132):
            for ln in views.render_convo("t", self._body(), 0, w, 24):
                self.assertLessEqual(width_of(ln), w, f"w={w}")

    def test_scroll_changes_what_is_shown(self):
        top = _plain(views.render_convo("t", self._body(), 0, 80, 20))
        down = _plain(views.render_convo("t", self._body(), 20, 80, 20))
        self.assertNotEqual(top, down)

    def test_scroll_is_clamped_at_both_ends(self):
        body = self._body()
        low = _plain(views.render_convo("t", body, -50, 80, 20))
        zero = _plain(views.render_convo("t", body, 0, 80, 20))
        self.assertEqual(low, zero)
        far = _plain(views.render_convo("t", body, 99_999, 80, 20))
        self.assertEqual(len(far), len(zero))

    def test_title_is_shown(self):
        out = "\n".join(_plain(views.render_convo("my-task", self._body(),
                                                  0, 80, 20)))
        self.assertIn("my-task", out)

    def test_wrapping_keeps_the_rule_on_continuations(self):
        s = FakeSession([FakeEvent("text", text="word " * 60)])
        wrapped = views.wrap_all(views.session_blocks(s), 40)
        ruled = [text_of(l) for l in wrapped if views.RULE in text_of(l)]
        self.assertGreater(len(ruled), 1,
                           "a long paragraph must stay one ruled block")


class SplitViewTests(unittest.TestCase):
    def test_split_fills_exactly_the_width(self):
        body = views.session_blocks(
            FakeSession([FakeEvent("text", text="hi")]))
        for w in (100, 120, 160):
            for ln in views.render_split(_runs(5), 1, body, "t", w, 24, NOW):
                self.assertEqual(width_of(ln), w, f"w={w}")

    def test_split_has_one_row_per_screen_line(self):
        body = views.session_blocks(
            FakeSession([FakeEvent("text", text="hi")]))
        self.assertEqual(len(views.render_split(_runs(3), 0, body, "t",
                                                120, 24, NOW)), 24)


# ── key handling ────────────────────────────────────────────────────────

class ResolveKeyActionTests(unittest.TestCase):
    def test_enter_selects_in_the_list(self):
        for k in (10, 13, curses.KEY_ENTER):
            self.assertEqual(resolve_key_action(k, "list"), "select")

    def test_q_quits_the_list_but_only_goes_back_in_a_convo(self):
        self.assertEqual(resolve_key_action(ord("q"), "list"), "quit")
        self.assertEqual(resolve_key_action(ord("q"), "convo"), "back")

    def test_escape_and_left_go_back_from_a_convo(self):
        for k in (27, curses.KEY_LEFT, ord("h")):
            self.assertEqual(resolve_key_action(k, "convo"), "back")

    def test_left_does_nothing_in_the_list(self):
        self.assertEqual(resolve_key_action(curses.KEY_LEFT, "list"), "noop")

    def test_vim_and_arrow_keys_agree(self):
        self.assertEqual(resolve_key_action(ord("j"), "list"),
                         resolve_key_action(curses.KEY_DOWN, "list"))
        self.assertEqual(resolve_key_action(ord("k"), "convo"),
                         resolve_key_action(curses.KEY_UP, "convo"))

    def test_paging_keys(self):
        self.assertEqual(resolve_key_action(curses.KEY_NPAGE, "list"),
                         "page_down")
        self.assertEqual(resolve_key_action(curses.KEY_PPAGE, "convo"),
                         "page_up")
        self.assertEqual(resolve_key_action(4, "list"), "half_page_down")
        self.assertEqual(resolve_key_action(21, "convo"), "half_page_up")

    def test_tab_toggles_detail_in_both_views(self):
        self.assertEqual(resolve_key_action(9, "list"), "toggle_expand")
        self.assertEqual(resolve_key_action(9, "convo"), "toggle_expand")

    def test_unknown_keys_are_noops(self):
        self.assertEqual(resolve_key_action(ord("z"), "list"), "noop")
        self.assertEqual(resolve_key_action(ord("z"), "convo"), "noop")


class HandleActionTests(unittest.TestCase):
    def _act(self, action, view="list", runs=None, sel=0, scroll=0,
             body_len=100, expanded=False):
        runs = _runs(10) if runs is None else runs
        return _handle_action(action, view, runs, sel, scroll, 0, 24,
                              body_len, expanded)

    def test_movement_is_clamped_at_both_ends(self):
        self.assertEqual(self._act("up", sel=0)[1], 0)
        self.assertEqual(self._act("down", sel=9)[1], 9)

    def test_top_and_bottom(self):
        self.assertEqual(self._act("top", sel=5)[1], 0)
        self.assertEqual(self._act("bottom", sel=0)[1], 9)

    def test_select_switches_view_and_resets_scroll(self):
        view, sel, scroll, _, _, _ = self._act("select", sel=3, scroll=40)
        self.assertEqual(view, "convo")
        self.assertEqual(scroll, 0)
        self.assertEqual(sel, 3)

    def test_back_returns_to_the_list(self):
        self.assertEqual(self._act("back", view="convo")[0], "list")

    def test_convo_scrolling_is_clamped(self):
        self.assertEqual(self._act("scroll_up", view="convo", scroll=0)[2], 0)
        far = self._act("scroll_bottom", view="convo", body_len=100)[2]
        self.assertEqual(far, 100 - views.convo_body_height(24))

    def test_toggle_expand_flips(self):
        self.assertTrue(self._act("toggle_expand", expanded=False)[4])
        self.assertFalse(self._act("toggle_expand", expanded=True)[4])

    def test_empty_list_movement_does_not_raise(self):
        for action in ("up", "down", "top", "bottom", "select"):
            self._act(action, runs=[])


class SelectionIdentityTests(unittest.TestCase):
    def test_key_is_stable_for_the_same_run(self):
        r = FakeRun(session_id="s1", platform="opencode")
        self.assertEqual(_key_of(r), _key_of(r))

    def test_unresolved_runs_fall_back_to_transcript_and_start(self):
        r = FakeRun(session_id="", transcript="/t.log", started=42)
        self.assertEqual(_key_of(r), ("/t.log", 42))

    def test_index_of_finds_a_run_and_survives_an_insertion(self):
        runs = _runs(3)
        key = _key_of(runs[1])
        self.assertEqual(_index_of(runs, key), 1)
        # A refresh prepends a newer run; the selection must follow the key.
        runs.insert(0, FakeRun(prompt="/p/tasks/new.md", started=99_999))
        self.assertEqual(_index_of(runs, key), 2)

    def test_index_of_returns_none_for_an_unknown_key(self):
        self.assertIsNone(_index_of(_runs(3), ("nope", "nope")))


class RowToIndexTests(unittest.TestCase):
    def test_header_rows_are_not_runs(self):
        self.assertIsNone(_row_to_index(0, 0))
        self.assertIsNone(_row_to_index(1, 0))

    def test_first_run_occupies_the_first_body_rows(self):
        self.assertEqual(_row_to_index(2, 0), 0)
        self.assertEqual(_row_to_index(3, 0), 0)

    def test_blank_separator_row_is_not_a_run(self):
        self.assertIsNone(_row_to_index(4, 0))

    def test_second_run_follows_the_separator(self):
        self.assertEqual(_row_to_index(5, 0), 1)

    def test_scrolling_shifts_the_mapping(self):
        self.assertEqual(_row_to_index(2, 3), 1)


if __name__ == "__main__":
    unittest.main()


class ListScrollMappingTests(unittest.TestCase):
    """The renderer and the click handler must agree on where the list is.

    Regression guard: the renderer used to compute this internally while the
    loop kept a copy that never changed, so on a scrolled list a click landed
    on the right screen row but the wrong run.
    """

    def test_scroll_follows_the_selection_downwards(self):
        s = views.list_scroll_for(20, 0, 24)
        self.assertGreater(s, 0)

    def test_scroll_follows_the_selection_upwards(self):
        self.assertEqual(views.list_scroll_for(0, 30, 24), 0)

    def test_scroll_is_stable_when_selection_is_already_visible(self):
        s1 = views.list_scroll_for(10, 0, 40)
        self.assertEqual(views.list_scroll_for(10, s1, 40), s1)

    def test_click_maps_back_to_the_selected_run_when_scrolled(self):
        # Render a scrolled list, then ask which run each body row belongs to
        # and check the marked row maps back to the selection.
        sel, h = 20, 24
        scroll = views.list_scroll_for(sel, 0, h)
        screen = views.render_list(_runs(40), sel, 80, h, NOW, scroll=scroll)
        marked = [i for i, ln in enumerate(screen) if "▸" in text_of(ln)]
        self.assertEqual(len(marked), 1)
        self.assertEqual(_row_to_index(marked[0], scroll), sel)

    def test_click_maps_correctly_at_the_top_of_an_unscrolled_list(self):
        screen = views.render_list(_runs(10), 0, 80, 24, NOW, scroll=0)
        marked = [i for i, ln in enumerate(screen) if "▸" in text_of(ln)]
        self.assertEqual(_row_to_index(marked[0], 0), 0)
