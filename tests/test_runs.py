"""Tests for delegate_view.runs — bulk resolution and load_runs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from delegate_view.runs import Run, load_runs, resolve_session, resolve_sessions


def _make_run(started: int, cwd: str, **kw) -> Run:
    defaults = dict(
        started=started, prompt="p.py", transcript="/tmp/t",
        model="m", cwd=cwd,
    )
    defaults.update(kw)
    return Run(**defaults)


def _write_ledger(path: Path, entries: list[dict]) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestResolveSessionsSingleCwd(unittest.TestCase):
    """resolve_sessions issues ONE list_sessions call for runs sharing a cwd."""

    def test_same_cwd_one_call(self):
        runs = [_make_run(started=1000 * i, cwd="/proj") for i in range(5)]
        call_count = 0

        def fake_list_sessions(cwd=None):
            nonlocal call_count
            call_count += 1
            return []

        with patch("delegate_view.adapters.list_sessions",
                    side_effect=fake_list_sessions):
            resolve_sessions(runs)

        self.assertEqual(call_count, 1,
                         f"Expected 1 call, got {call_count}")


class TestResolveSessionsDistinctCwds(unittest.TestCase):
    """resolve_sessions issues one call PER distinct cwd."""

    def test_two_cwds_two_calls(self):
        runs = [
            _make_run(started=100, cwd="/proj/a"),
            _make_run(started=200, cwd="/proj/a"),
            _make_run(started=300, cwd="/proj/b"),
        ]
        call_count = 0

        def fake_list_sessions(cwd=None):
            nonlocal call_count
            call_count += 1
            return []

        with patch("delegate_view.adapters.list_sessions",
                    side_effect=fake_list_sessions):
            resolve_sessions(runs)

        self.assertEqual(call_count, 2,
                         f"Expected 2 calls, got {call_count}")


class TestLoadRunsUsesBulkResolver(unittest.TestCase):
    """load_runs(resolve=True) calls list_sessions once per distinct cwd."""

    def test_ledger_same_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "runs.jsonl"
            transcript = Path(tmp) / "transcript.txt"
            transcript.write_text("hello")
            entries = [
                {"started": i * 1000, "prompt": f"t{i}", "transcript": str(transcript),
                 "cwd": "/proj", "model": "m"}
                for i in range(5)
            ]
            _write_ledger(ledger, entries)

            call_count = 0

            def fake_list_sessions(cwd=None):
                nonlocal call_count
                call_count += 1
                return []

            with patch("delegate_view.adapters.list_sessions",
                        side_effect=fake_list_sessions):
                runs = load_runs(ledger_path=ledger, resolve=True)

            self.assertEqual(len(runs), 5)
            self.assertEqual(call_count, 1,
                             f"Expected 1 call, got {call_count}")

    def test_ledger_two_cwds(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "runs.jsonl"
            transcript = Path(tmp) / "transcript.txt"
            transcript.write_text("hello")
            entries = [
                {"started": 1000, "prompt": "t1", "transcript": str(transcript),
                 "cwd": "/proj/a", "model": "m"},
                {"started": 2000, "prompt": "t2", "transcript": str(transcript),
                 "cwd": "/proj/a", "model": "m"},
                {"started": 3000, "prompt": "t3", "transcript": str(transcript),
                 "cwd": "/proj/b", "model": "m"},
            ]
            _write_ledger(ledger, entries)

            call_count = 0

            def fake_list_sessions(cwd=None):
                nonlocal call_count
                call_count += 1
                return []

            with patch("delegate_view.adapters.list_sessions",
                        side_effect=fake_list_sessions):
                runs = load_runs(ledger_path=ledger, resolve=True)

            self.assertEqual(len(runs), 3)
            self.assertEqual(call_count, 2,
                             f"Expected 2 calls, got {call_count}")


class TestBulkResolutionActuallyStoresTheResult(unittest.TestCase):
    """The batching must still WRITE what it resolves back onto the runs.

    Regression guard. When resolve_sessions was first written it computed the
    match correctly and then dropped it on the floor — the return value of the
    per-run helper was never assigned to r.platform / r.session_id. Every
    existing test still passed, because they all counted adapter CALLS or
    checked the single-run wrapper's return value, and none of them asserted
    that a run came out of the batch path with a session attached. The result
    was a resolver that went 840x faster by resolving nothing.

    A speed test that does not also assert the answer is a test for the wrong
    thing, so these assert the answer.
    """

    def _sessions(self):
        from delegate_view.schema import Session
        return [
            Session(id="early", platform="opencode", title="t", cwd="/proj",
                    model="m", created=2000, tokens_in=11, tokens_out=22,
                    cost=0.5),
            Session(id="later", platform="opencode", title="t", cwd="/proj",
                    model="m", created=9000),
        ]

    def test_resolve_sessions_populates_platform_and_id(self):
        runs = [_make_run(started=1000, cwd="/proj")]
        with patch("delegate_view.adapters.list_sessions",
                   return_value=self._sessions()):
            resolve_sessions(runs)
        self.assertEqual(runs[0].platform, "opencode")
        self.assertEqual(runs[0].session_id, "early")

    def test_resolve_sessions_populates_token_and_cost_stats(self):
        runs = [_make_run(started=1000, cwd="/proj")]
        with patch("delegate_view.adapters.list_sessions",
                   return_value=self._sessions()):
            resolve_sessions(runs)
        self.assertEqual(runs[0].tokens_in, 11)
        self.assertEqual(runs[0].tokens_out, 22)
        self.assertEqual(runs[0].cost, 0.5)

    def test_no_match_leaves_the_run_unresolved_not_wrong(self):
        # A run started after every known session resolves to nothing.
        runs = [_make_run(started=99999, cwd="/proj")]
        with patch("delegate_view.adapters.list_sessions",
                   return_value=self._sessions()):
            resolve_sessions(runs)
        self.assertEqual(runs[0].platform, "")
        self.assertEqual(runs[0].session_id, "")

    def test_load_runs_end_to_end_attaches_sessions(self):
        # The path the TUI actually takes.
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "runs.jsonl"
            transcript = Path(tmp) / "t.log"
            transcript.write_text("hi")
            _write_ledger(ledger, [{
                "started": 1000, "prompt": str(transcript),
                "transcript": str(transcript), "model": "m", "cwd": "/proj",
            }])
            with patch("delegate_view.adapters.list_sessions",
                       return_value=self._sessions()):
                runs = load_runs(ledger_path=ledger, resolve=True)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].session_id, "early",
                         "load_runs(resolve=True) must attach the session")
        self.assertEqual(runs[0].platform, "opencode")


class TestResolveSessionStillWorks(unittest.TestCase):
    """resolve_session(run) remains a functional single-run interface."""

    def test_returns_match(self):
        from delegate_view.schema import Session
        run = _make_run(started=1000, cwd="/proj")
        fake_sessions = [
            Session(id="s1", platform="opencode", title="t", cwd="/proj",
                    model="m", created=2000),
        ]
        with patch("delegate_view.adapters.list_sessions",
                    return_value=fake_sessions):
            platform, sid = resolve_session(run)
        self.assertEqual(platform, "opencode")
        self.assertEqual(sid, "s1")
        self.assertEqual(run.tokens_in, 0)


if __name__ == "__main__":
    unittest.main()
