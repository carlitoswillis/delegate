"""Tests for delegate_view.watch — pure rendering functions, no curses."""

import unittest
from dataclasses import dataclass, field

from delegate_view.watch import (
    render_list,
    render_convo,
    session_lines,
    _relative_age,
    _truncate_path,
)
from delegate_view.schema import Event, Session


@dataclass
class FakeRun:
    started: int = 0
    prompt: str = ""
    transcript: str = ""
    model: str = ""
    cwd: str = ""
    continued: bool = False
    live: bool = False
    size: int = 0
    prompt_text: str = ""
    session_id: str = ""
    platform: str = ""


NOW = 1_700_000_000_000  # fixed epoch ms


class TestRenderListLiveMarker(unittest.TestCase):
    """1. Live runs get a filled marker and 'live'; finished get a dim one."""

    def test_live_run(self):
        run = FakeRun(live=True, started=NOW, prompt="p.py", model="m")
        out = render_list([run], 0, 80, 10, NOW)
        live_rows = [ln for ln in out if "\u25cf" in ln and "live" in ln]
        self.assertEqual(len(live_rows), 1)

    def test_finished_run(self):
        run = FakeRun(live=False, started=NOW, prompt="p.py", model="m")
        out = render_list([run], 0, 80, 10, NOW)
        self.assertTrue(any("\u00b7" in ln for ln in out))
        self.assertFalse(any("live" in ln for ln in out))

    def test_both_live_and_finished(self):
        r1 = FakeRun(live=True, started=NOW, prompt="a.py", model="m")
        r2 = FakeRun(live=False, started=NOW, prompt="b.py", model="m")
        out = render_list([r1, r2], 0, 80, 12, NOW)
        self.assertTrue(any("\u25cf" in ln and "live" in ln for ln in out))
        self.assertTrue(any("\u00b7" in ln for ln in out))


class TestRenderListWidth(unittest.TestCase):
    """2. Every line from render_list is <= width."""

    def test_normal_width(self):
        run = FakeRun(
            live=True, started=NOW, prompt="test.py",
            model="anthropic/claude-opus-4-20250918"
        )
        out = render_list([run], 0, 60, 10, NOW)
        for ln in out:
            self.assertLessEqual(len(ln), 60, f"Line too long: {ln!r}")

    def test_very_long_path(self):
        path = "/very/long/path/to/some/deeply/nested/directory/structure/file.py"
        run = FakeRun(live=True, started=NOW, prompt=path, model="m")
        out = render_list([run], 0, 40, 10, NOW)
        for ln in out:
            self.assertLessEqual(len(ln), 40, f"Line too long: {ln!r}")

    def test_long_model_name(self):
        run = FakeRun(
            live=True, started=NOW, prompt="p.py",
            model="anthropic/claude-opus-4-20250918-extra-long-model"
        )
        out = render_list([run], 0, 50, 10, NOW)
        for ln in out:
            self.assertLessEqual(len(ln), 50, f"Line too long: {ln!r}")

    def test_narrow_width(self):
        run = FakeRun(live=True, started=NOW, prompt="p.py", model="m")
        out = render_list([run], 0, 20, 10, NOW)
        for ln in out:
            self.assertLessEqual(len(ln), 20, f"Line too long: {ln!r}")


class TestRenderListScroll(unittest.TestCase):
    """3. render_list scrolls so the selected index is visible."""

    def test_scroll_to_selected(self):
        runs = [
            FakeRun(started=NOW - i * 60000, prompt=f"f{i}.py", model="m")
            for i in range(20)
        ]
        out = render_list(runs, 15, 80, 8, NOW)
        # height=8, header=2, footer=1, view_height=5
        # selected=15 should scroll
        self.assertTrue(
            any("f15" in ln for ln in out),
            "Selected run f15.py not visible after scroll",
        )

    def test_select_top(self):
        runs = [
            FakeRun(started=NOW - i * 60000, prompt=f"f{i}.py", model="m")
            for i in range(20)
        ]
        out = render_list(runs, 0, 80, 8, NOW)
        self.assertTrue(
            any("f0" in ln for ln in out),
            "Top run f0.py not visible",
        )

    def test_select_bottom(self):
        runs = [
            FakeRun(started=NOW - i * 60000, prompt=f"f{i}.py", model="m")
            for i in range(20)
        ]
        out = render_list(runs, 19, 80, 8, NOW)
        self.assertTrue(
            any("f19" in ln for ln in out),
            "Bottom run f19.py not visible",
        )


class TestRenderListEmpty(unittest.TestCase):
    """4. render_list with empty list shows message, does not crash."""

    def test_empty_list(self):
        out = render_list([], 0, 80, 10, NOW)
        self.assertTrue(len(out) > 0)
        self.assertTrue(
            any("No agent runs yet" in ln for ln in out),
            "Expected 'No agent runs yet' in empty list output",
        )

    def test_empty_list_no_crash_on_width(self):
        out = render_list([], 0, 40, 5, NOW)
        for ln in out:
            self.assertLessEqual(len(ln), 40)


class TestRelativeAge(unittest.TestCase):
    """5. Relative age formats: seconds, minutes, hours, days."""

    def test_seconds(self):
        self.assertEqual(_relative_age(NOW, NOW - 12000), "12s")

    def test_zero_seconds(self):
        self.assertEqual(_relative_age(NOW, NOW), "0s")

    def test_minutes(self):
        self.assertEqual(_relative_age(NOW, NOW - 240000), "4m")

    def test_hours(self):
        self.assertEqual(_relative_age(NOW, NOW - 7200000), "2h")

    def test_days(self):
        self.assertEqual(_relative_age(NOW, NOW - 259200000), "3d")


class TestRenderConvoWrap(unittest.TestCase):
    """6. render_convo wraps long lines to width, all lines <= width."""

    def test_long_line_wraps(self):
        long_line = "This is a very long line that should definitely be wrapped to fit within the terminal width boundary."
        out = render_convo("Test", [long_line], 0, 50, 20)
        for ln in out:
            self.assertLessEqual(len(ln), 50, f"Line too long: {ln!r}")

    def test_short_lines_not_broken(self):
        lines = ["Hello", "World"]
        out = render_convo("Title", lines, 0, 80, 10)
        self.assertTrue(
            any("Hello" in ln for ln in out),
            "Short line should appear intact",
        )

    def test_empty_lines(self):
        out = render_convo("Title", ["", ""], 0, 80, 10)
        for ln in out:
            self.assertLessEqual(len(ln), 80)

    def test_many_long_lines_wrap(self):
        lines = [f"Line {i}: " + "word " * 20 for i in range(5)]
        out = render_convo("Title", lines, 0, 60, 30)
        for ln in out:
            self.assertLessEqual(len(ln), 60, f"Line too long: {ln!r}")


class TestSessionLinesReasoning(unittest.TestCase):
    """7. session_lines truncates reasoning to ~200 chars."""

    def test_long_reasoning_truncated(self):
        ev = Event(ts=0, role="assistant", kind="reasoning",
                   text="x" * 500)
        session = Session(
            id="s1", platform="opencode", title="t", cwd="/",
            model="m", events=[ev]
        )
        out = session_lines(session)
        joined = "\n".join(out)
        # Should contain the ~200 chars plus an ellipsis
        self.assertLessEqual(len(joined), 300)
        self.assertTrue(
            any("~" in ln for ln in out),
            "Reasoning lines should have ~ prefix",
        )

    def test_short_reasoning_not_truncated(self):
        ev = Event(ts=0, role="assistant", kind="reasoning",
                   text="short thought")
        session = Session(
            id="s1", platform="opencode", title="t", cwd="/",
            model="m", events=[ev]
        )
        out = session_lines(session)
        self.assertTrue(any("short thought" in ln for ln in out))


class TestSessionLinesPatch(unittest.TestCase):
    """8. session_lines renders patch file list from raw."""

    def test_patch_with_files(self):
        ev = Event(
            ts=0, role="assistant", kind="patch",
            raw={"files": ["/foo/bar.py", "/baz/qux.py"]}
        )
        session = Session(
            id="s1", platform="opencode", title="t", cwd="/",
            model="m", events=[ev]
        )
        out = session_lines(session)
        self.assertTrue(any("\u00b1 patched:" in ln for ln in out))
        self.assertTrue(any("bar.py" in ln for ln in out))
        self.assertTrue(any("qux.py" in ln for ln in out))

    def test_patch_empty_files(self):
        ev = Event(ts=0, role="assistant", kind="patch", raw={})
        session = Session(
            id="s1", platform="opencode", title="t", cwd="/",
            model="m", events=[ev]
        )
        out = session_lines(session)
        self.assertTrue(any("\u00b1 patched:" in ln for ln in out))


class TestSessionLinesToolOutput(unittest.TestCase):
    """9. session_lines caps tool_output at 10 lines and marks truncation."""

    def test_tool_output_truncated_at_10(self):
        output_lines = "\n".join([f"out {i}" for i in range(25)])
        ev = Event(
            ts=0, role="assistant", kind="tool_call",
            tool_name="bash", tool_status="completed",
            tool_ms=100, tool_output=output_lines
        )
        session = Session(
            id="s1", platform="opencode", title="t", cwd="/",
            model="m", events=[ev]
        )
        out = session_lines(session)
        # Should have header + 10 output lines + truncation marker
        self.assertTrue(any("\u2192 bash" in ln for ln in out))
        indented = [ln for ln in out if ln.startswith("    ")]
        self.assertLessEqual(len(indented), 11)  # 10 shown + possible truncation
        self.assertTrue(
            any("\u2026" in ln for ln in out),
            "Expected truncation marker",
        )

    def test_tool_output_no_truncation_when_short(self):
        ev = Event(
            ts=0, role="assistant", kind="tool_call",
            tool_name="bash", tool_status="completed",
            tool_ms=50, tool_output="line1\nline2"
        )
        session = Session(
            id="s1", platform="opencode", title="t", cwd="/",
            model="m", events=[ev]
        )
        out = session_lines(session)
        self.assertTrue(any("line1" in ln for ln in out))
        self.assertTrue(any("line2" in ln for ln in out))

    def test_compaction_line(self):
        ev = Event(ts=0, role="assistant", kind="compaction")
        session = Session(
            id="s1", platform="opencode", title="t", cwd="/",
            model="m", events=[ev]
        )
        out = session_lines(session)
        self.assertTrue(
            any("compacted" in ln for ln in out),
            "Expected compaction separator",
        )

    def test_text_role_prefix(self):
        ev_user = Event(ts=0, role="user", kind="text", text="hello")
        ev_asst = Event(ts=100, role="assistant", kind="text", text="world")
        session = Session(
            id="s1", platform="opencode", title="t", cwd="/",
            model="m", events=[ev_user, ev_asst]
        )
        out = session_lines(session)
        self.assertTrue(any(ln.startswith("> ") and "hello" in ln for ln in out))
        self.assertTrue(
            any(ln.startswith(" ") and "world" in ln for ln in out)
        )

    def test_truncate_path_helper(self):
        self.assertEqual(_truncate_path("short.py", 50), "short.py")
        result = _truncate_path("/a/b/c/longfilename.py", 20)
        self.assertLessEqual(len(result), 20)
        self.assertTrue(result.startswith("\u2026"))
        self.assertIn("longfilename.py", result)
        # With very narrow width, still respects constraint
        result_narrow = _truncate_path("/a/b/c/longfilename.py", 12)
        self.assertLessEqual(len(result_narrow), 12)
        self.assertTrue(result_narrow.startswith("\u2026"))

    def test_render_list_with_none_model(self):
        run = FakeRun(live=False, started=NOW, prompt="p.py", model=None)
        out = render_list([run], 0, 80, 10, NOW)
        for ln in out:
            self.assertLessEqual(len(ln), 80)

    def test_render_list_selected_beyond_list(self):
        run = FakeRun(started=NOW, prompt="p.py", model="m")
        out = render_list([run], 100, 80, 10, NOW)
        for ln in out:
            self.assertLessEqual(len(ln), 80)


if __name__ == "__main__":
    unittest.main()
