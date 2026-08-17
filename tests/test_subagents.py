"""Tests for delegate_view.subagents — no real data, all monkeypatched."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from delegate_view.schema import Session
from delegate_view.runs import Run
from delegate_view.subagents import load_subagent_runs


def _session(
    *,
    id: str = "s1",
    parent_id: str | None = "p1",
    created: int = 1000,
    updated: int = 2000,
    model: str = "anthropic/claude-sonnet",
    cwd: str = "/tmp/project",
    title: str = "refactor foo",
) -> Session:
    return Session(
        id=id,
        platform="claude-code",
        title=title,
        cwd=cwd,
        model=model,
        parent_id=parent_id,
        created=created,
        updated=updated,
    )


class TestSubagents(unittest.TestCase):

    def test_no_parent_excluded(self):
        s = _session(parent_id=None)
        with patch("delegate_view.subagents.list_sessions", return_value=[s]):
            self.assertEqual(load_subagent_runs(), [])

    def test_parent_included(self):
        s = _session(parent_id="p1")
        with patch("delegate_view.subagents.list_sessions", return_value=[s]):
            runs = load_subagent_runs()
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].session_id, "s1")

    def test_newest_updated_first(self):
        older = _session(id="old", updated=100, created=50)
        newer = _session(id="new", updated=500, created=200)
        with patch("delegate_view.subagents.list_sessions", return_value=[older, newer]):
            runs = load_subagent_runs()
            self.assertEqual(runs[0].session_id, "new")
            self.assertEqual(runs[1].session_id, "old")

    def test_limit_caps(self):
        sessions = [_session(id=str(i), updated=i) for i in range(10)]
        with patch("delegate_view.subagents.list_sessions", return_value=sessions):
            self.assertEqual(len(load_subagent_runs(limit=3)), 3)

    def test_since_ms_drops_old(self):
        old = _session(id="old", updated=100)
        new = _session(id="new", updated=500)
        with patch("delegate_view.subagents.list_sessions", return_value=[old, new]):
            runs = load_subagent_runs(since_ms=300)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].session_id, "new")

    def test_live_true_when_recent(self):
        now_ms = int(time.time() * 1000)
        s = _session(updated=now_ms)
        with patch("delegate_view.subagents.list_sessions", return_value=[s]):
            self.assertTrue(load_subagent_runs()[0].live)

    def test_live_false_when_old(self):
        old_ms = int(time.time() * 1000) - 60000
        s = _session(updated=old_ms)
        with patch("delegate_view.subagents.list_sessions", return_value=[s]):
            self.assertFalse(load_subagent_runs()[0].live)

    def test_field_mapping(self):
        s = _session(
            id="sid-abc",
            model="anthropic/claude-sonnet-4-20250514",
            cwd="/home/user/repo",
            title="fix the bug",
        )
        with patch("delegate_view.subagents.list_sessions", return_value=[s]):
            r = load_subagent_runs()[0]
            self.assertEqual(r.session_id, "sid-abc")
            self.assertEqual(r.platform, "claude-code")
            self.assertEqual(r.prompt_text, "fix the bug")
            self.assertEqual(r.model, "anthropic/claude-sonnet-4-20250514")
            self.assertEqual(r.cwd, "/home/user/repo")

    def test_started_fallback_to_updated_when_created_zero(self):
        s = _session(created=0, updated=999)
        with patch("delegate_view.subagents.list_sessions", return_value=[s]):
            self.assertEqual(load_subagent_runs()[0].started, 999)

    def test_adapter_exception_returns_empty(self):
        with patch("delegate_view.subagents.list_sessions", side_effect=RuntimeError("boom")):
            self.assertEqual(load_subagent_runs(), [])


if __name__ == "__main__":
    unittest.main()
