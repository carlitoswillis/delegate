"""Tests for delegate_view.adapters.claude_code — uses temp directory trees, no real data."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from delegate_view.adapters.claude_code import _peek, list_sessions


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_project(root: Path, slug: str) -> Path:
    p = root / slug
    p.mkdir(parents=True, exist_ok=True)
    return p


def _make_top_session(project: Path, name: str, *,
                      cwd: str = "/tmp", model: str = "claude-sonnet",
                      ts: str = "2026-08-16T10:00:00.000Z") -> Path:
    path = project / f"{name}.jsonl"
    _write_jsonl(path, [
        {"sessionId": name, "cwd": cwd, "timestamp": ts},
        {"type": "assistant", "message": {"model": model}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "hello"}]}},
    ])
    return path


def _make_subagent(project: Path, parent_id: str, agent_name: str, *,
                   cwd: str = "/tmp", model: str = "claude-sonnet",
                   ts: str = "2026-08-16T10:00:00.000Z") -> Path:
    sa_dir = project / parent_id / "subagents"
    path = sa_dir / f"{agent_name}.jsonl"
    _write_jsonl(path, [
        {"sessionId": parent_id, "cwd": cwd, "timestamp": ts},
        {"type": "assistant", "message": {"model": model}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "do stuff"}]}},
    ])
    return path


def _make_workflow(project: Path, parent_id: str, wf_id: str, file_name: str, *,
                   cwd: str = "/tmp", ts: str = "2026-08-16T10:00:00.000Z") -> Path:
    wf_dir = project / parent_id / "subagents" / "workflows" / wf_id
    path = wf_dir / f"{file_name}.jsonl"
    _write_jsonl(path, [
        {"sessionId": parent_id, "cwd": cwd, "timestamp": ts},
        {"type": "user", "message": {"content": [{"type": "text", "text": "wf task"}]}},
    ])
    return path


class TestListSessionsLimit(unittest.TestCase):
    """The core performance test: limit=N must peek at most N files."""

    def test_limit_peeks_at_most_n(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _make_project(root, "myproj")
            # Create 20 subagent files with distinct mtimes.
            paths = []
            for i in range(20):
                p = _make_subagent(project, f"parent-{i:02d}", f"agent-{i:02d}",
                                   ts=f"2026-08-16T{10+i//60:02d}:{i%60:02d}:00.000Z")
                os.utime(p, (i, i))  # set mtime to i
                paths.append(p)

            peek_count = 0
            original_peek = _peek

            def counting_peek(path):
                nonlocal peek_count
                peek_count += 1
                return original_peek(path)

            from delegate_view.adapters import claude_code as cc_mod
            old = cc_mod._peek
            cc_mod._peek = counting_peek
            try:
                result = list_sessions(db_path=root, limit=5)
            finally:
                cc_mod._peek = old

            self.assertLessEqual(peek_count, 5)
            self.assertEqual(len(result), 5)

    def test_limit_zero_peeks_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _make_project(root, "myproj")
            for i in range(5):
                _make_subagent(project, f"parent-{i}", f"agent-{i}")

            peek_count = 0
            original_peek = _peek

            def counting_peek(path):
                nonlocal peek_count
                peek_count += 1
                return original_peek(path)

            from delegate_view.adapters import claude_code as cc_mod
            old = cc_mod._peek
            cc_mod._peek = counting_peek
            try:
                result = list_sessions(db_path=root, limit=0)
            finally:
                cc_mod._peek = old

            self.assertEqual(peek_count, 0)
            self.assertEqual(len(result), 0)

    def test_limit_none_peeks_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _make_project(root, "myproj")
            for i in range(5):
                _make_subagent(project, f"parent-{i}", f"agent-{i}")

            peek_count = 0
            original_peek = _peek

            def counting_peek(path):
                nonlocal peek_count
                peek_count += 1
                return original_peek(path)

            from delegate_view.adapters import claude_code as cc_mod
            old = cc_mod._peek
            cc_mod._peek = counting_peek
            try:
                result = list_sessions(db_path=root, limit=None)
            finally:
                cc_mod._peek = old

            self.assertEqual(peek_count, 5)
            self.assertEqual(len(result), 5)


class TestSubagentsOnly(unittest.TestCase):

    def test_subagents_only_returns_only_subagent_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _make_project(root, "myproj")
            top = _make_top_session(project, "sess-top")
            sub1 = _make_subagent(project, "parent-aaa", "agent-1")
            sub2 = _make_subagent(project, "parent-bbb", "agent-2")
            wf = _make_workflow(project, "parent-ccc", "wf-1", "step-1")

            result = list_sessions(db_path=root, subagents_only=True)
            ids = {s.id for s in result}
            # Top-level session should NOT appear.
            self.assertNotIn("sess-top", ids)
            # Subagent and workflow files should appear (workflow paths contain "subagents").
            self.assertEqual(len(result), 3)

    def test_subagents_only_does_not_peek_non_subagent_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _make_project(root, "myproj")
            top = _make_top_session(project, "sess-top")
            sub1 = _make_subagent(project, "parent-aaa", "agent-1")

            peeked: list[Path] = []
            original_peek = _peek

            def tracking_peek(path):
                peeked.append(path)
                return original_peek(path)

            from delegate_view.adapters import claude_code as cc_mod
            old = cc_mod._peek
            cc_mod._peek = tracking_peek
            try:
                result = list_sessions(db_path=root, subagents_only=True)
            finally:
                cc_mod._peek = old

            # Only the subagent file should have been peeked.
            self.assertEqual(len(peeked), 1)
            self.assertIn("agent-1", str(peeked[0]))


class TestOrdering(unittest.TestCase):

    def test_newest_mtime_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _make_project(root, "myproj")
            old = _make_subagent(project, "parent-old", "agent-old",
                                 ts="2026-08-16T08:00:00.000Z")
            new = _make_subagent(project, "parent-new", "agent-new",
                                 ts="2026-08-16T12:00:00.000Z")
            os.utime(old, (100, 100))
            os.utime(new, (200, 200))

            result = list_sessions(db_path=root)
            self.assertEqual(result[0].id, "agent-new")
            self.assertEqual(result[1].id, "agent-old")


class TestDefaultBehavior(unittest.TestCase):

    def test_limit_none_subagents_false_returns_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _make_project(root, "myproj")
            top = _make_top_session(project, "sess-top")
            sub = _make_subagent(project, "parent-aaa", "agent-1")

            result_all = list_sessions(db_path=root)
            result_default = list_sessions(db_path=root, limit=None, subagents_only=False)
            self.assertEqual(len(result_all), len(result_default))
            self.assertEqual({s.id for s in result_all}, {s.id for s in result_default})


class TestMissingFileBetweenGlobAndStat(unittest.TestCase):

    def test_deleted_file_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _make_project(root, "myproj")
            good = _make_subagent(project, "parent-good", "agent-good")
            doomed = _make_subagent(project, "parent-doomed", "agent-doomed")

            original_stat = Path.stat

            def stat_or_delete(self_path):
                if "doomed" in str(self_path):
                    self_path.unlink(missing_ok=True)
                    raise OSError("simulated race")
                return original_stat(self_path)

            from pathlib import Path as P
            old_stat = P.stat
            P.stat = stat_or_delete
            try:
                result = list_sessions(db_path=root)
            finally:
                P.stat = old_stat

            ids = {s.id for s in result}
            self.assertIn("agent-good", ids)
            self.assertNotIn("agent-doomed", ids)


class TestFieldMapping(unittest.TestCase):

    def test_session_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _make_project(root, "myproj")
            sub = _make_subagent(project, "parent-aaa", "agent-1",
                                 cwd="/home/user/repo",
                                 model="claude-sonnet-4",
                                 ts="2026-08-16T10:00:00.000Z")
            result = list_sessions(db_path=root, subagents_only=True)
            self.assertEqual(len(result), 1)
            s = result[0]
            self.assertEqual(s.id, "agent-1")
            self.assertEqual(s.platform, "claude-code")
            self.assertEqual(s.parent_id, "parent-aaa")
            self.assertEqual(s.cwd, "/home/user/repo")
            self.assertEqual(s.model, "claude-sonnet-4")


class TestCwdFiltering(unittest.TestCase):

    def test_cwd_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _make_project(root, "myproj")
            _make_subagent(project, "p1", "a1", cwd="/proj/a")
            _make_subagent(project, "p2", "a2", cwd="/proj/b")

            result = list_sessions(db_path=root, cwd="/proj/a")
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].cwd, "/proj/a")

    def test_cwd_with_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _make_project(root, "myproj")
            for i in range(5):
                _make_subagent(project, f"p{i}", f"a{i}",
                               cwd="/proj/a" if i % 2 == 0 else "/proj/b",
                               ts=f"2026-08-16T10:{i:02d}:00.000Z")
                time.sleep(0.01)

            result = list_sessions(db_path=root, cwd="/proj/a", limit=2)
            self.assertLessEqual(len(result), 2)
            for s in result:
                self.assertEqual(s.cwd, "/proj/a")


if __name__ == "__main__":
    unittest.main()
