"""Tests for the refreshing RunStore.

Uses monkeypatched loaders so nothing touches ~/.delegate or ~/.claude.
Refresh cycles are driven by calling _refresh() directly — no wall-clock
timing, no sleeps over 0.2s.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from delegate_view.runs import Run
from delegate_view.store import RunStore, key_of


def _make_run(
    transcript: str = "",
    started: int = 1000,
    platform: str = "delegate-log",
    session_id: str = "",
    model: str = "test/model",
    cwd: str = "/tmp",
) -> Run:
    return Run(
        started=started,
        prompt="",
        transcript=transcript,
        model=model,
        cwd=cwd,
        session_id=session_id,
        platform=platform,
    )


class TestSnapshotBeforeRefresh(unittest.TestCase):
    """1. snapshot() returns [] immediately after start() without blocking."""

    def test_empty_before_first_refresh(self) -> None:
        store = RunStore(_load_runs=lambda **kw: [], _load_subagent_runs=lambda **kw: [])
        store.start()
        try:
            self.assertEqual(store.snapshot(), [])
        finally:
            store.stop()


class TestNewestFirst(unittest.TestCase):
    """2. A refresh picks up runs and snapshot() returns them newest-first."""

    def test_newest_first(self) -> None:
        runs = [
            _make_run(started=1000),
            _make_run(started=3000),
            _make_run(started=2000),
        ]
        store = RunStore(
            _load_runs=lambda **kw: runs,
            _load_subagent_runs=lambda **kw: [],
        )
        store._refresh()
        snap = store.snapshot()
        self.assertEqual([r.started for r in snap], [3000, 2000, 1000])


class TestAppendNewRun(unittest.TestCase):
    """3. A second refresh with a NEW run appends it and keeps the existing."""

    def test_new_run_appears(self) -> None:
        first = [_make_run(started=1000)]
        second = [_make_run(started=1000), _make_run(started=2000)]

        store = RunStore(
            _load_runs=lambda **kw: first,
            _load_subagent_runs=lambda **kw: [],
        )
        store._refresh()
        self.assertEqual(len(store.snapshot()), 1)

        # Swap the loader to return both.
        store._load_runs_fn = lambda **kw: second
        store._refresh()
        snap = store.snapshot()
        self.assertEqual(len(snap), 2)
        self.assertEqual([r.started for r in snap], [2000, 1000])


class TestInPlaceUpdate(unittest.TestCase):
    """4. A second refresh with the SAME run updates the object in place."""

    def test_identity_preserved(self) -> None:
        run = _make_run(started=1000, model="old/model")
        store = RunStore(
            _load_runs=lambda **kw: [run],
            _load_subagent_runs=lambda **kw: [],
        )
        store._refresh()
        snap = store.snapshot()
        original_obj = snap[0]

        # Now return a *new* Run object with the same key but different model.
        updated_run = _make_run(started=1000, model="new/model")
        store._load_runs_fn = lambda **kw: [updated_run]
        store._refresh()
        snap2 = store.snapshot()

        # The object should be the SAME instance, updated in place.
        self.assertIs(snap2[0], original_obj)
        self.assertEqual(snap2[0].model, "new/model")


class TestVanishingRunRetained(unittest.TestCase):
    """5. A run that disappears from the source is retained."""

    def test_disappearing_run_kept(self) -> None:
        store = RunStore(
            _load_runs=lambda **kw: [_make_run(started=1000), _make_run(started=2000)],
            _load_subagent_runs=lambda **kw: [],
        )
        store._refresh()
        self.assertEqual(len(store.snapshot()), 2)

        # Second source returns only one run.
        store._load_runs_fn = lambda **kw: [_make_run(started=2000)]
        store._refresh()
        self.assertEqual(len(store.snapshot()), 2)


class TestLivenessRecomputed(unittest.TestCase):
    """6. Liveness is recomputed from the current mtime of the transcript."""

    def test_live_then_stale(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"hello")
            path = f.name

        try:
            run = _make_run(transcript=path, started=1000)
            store = RunStore(
                _load_runs=lambda **kw: [run],
                _load_subagent_runs=lambda **kw: [],
            )
            # File was just written, so it should be live.
            store._refresh()
            self.assertTrue(store.snapshot()[0].live)

            # Back-date the mtime to before the live window.
            stale = time.time() - 60
            os.utime(path, (stale, stale))
            store._refresh()
            self.assertFalse(store.snapshot()[0].live)
        finally:
            os.unlink(path)


class TestNoTranscriptNeverLive(unittest.TestCase):
    """7. A run with transcript=\"\" never reports live."""

    def test_empty_transcript(self) -> None:
        run = _make_run(transcript="")
        store = RunStore(
            _load_runs=lambda **kw: [run],
            _load_subagent_runs=lambda **kw: [],
        )
        store._refresh()
        self.assertFalse(store.snapshot()[0].live)


class TestLedgerErrorDoesNotBlockSubagents(unittest.TestCase):
    """8. load_runs raising does not prevent subagent runs from appearing."""

    def test_ledger_error_still_shows_subagents(self) -> None:
        sa_run = _make_run(started=5000, session_id="sa-1")

        def bad_load(**kw):
            raise RuntimeError("ledger broken")

        store = RunStore(
            _load_runs=bad_load,
            _load_subagent_runs=lambda **kw: [sa_run],
        )
        store._refresh()
        snap = store.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0].session_id, "sa-1")


class TestRaisingCycleDoesNotStopThread(unittest.TestCase):
    """9. A raising refresh cycle does not stop later cycles from working."""

    def test_recovery_after_error(self) -> None:
        call_count = 0

        def flaky_load(**kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first cycle explodes")
            return [_make_run(started=1000)]

        store = RunStore(
            _load_runs=flaky_load,
            _load_subagent_runs=lambda **kw: [],
        )
        store._refresh()
        self.assertEqual(store.snapshot(), [])  # first cycle failed

        store._refresh()
        self.assertEqual(len(store.snapshot()), 1)  # second cycle succeeds


class TestStopIdempotent(unittest.TestCase):
    """10. stop() is idempotent and safe before start()."""

    def test_stop_before_start(self) -> None:
        store = RunStore(_load_runs=lambda **kw: [], _load_subagent_runs=lambda **kw: [])
        store.stop()  # must not raise

    def test_stop_twice(self) -> None:
        store = RunStore(_load_runs=lambda **kw: [], _load_subagent_runs=lambda **kw: [])
        store.start()
        store.stop()
        store.stop()  # second stop must not raise


class TestKeyOfStable(unittest.TestCase):
    """11. key_of is stable for the same logical run across two refreshes."""

    def test_key_stability(self) -> None:
        run1 = _make_run(started=1000, session_id="s-1", platform="claude-code")
        run2 = _make_run(started=1000, session_id="s-1", platform="claude-code")
        self.assertEqual(key_of(run1), key_of(run2))

    def test_key_unresolved(self) -> None:
        run1 = _make_run(started=1000, transcript="/tmp/t.jsonl")
        run2 = _make_run(started=1000, transcript="/tmp/t.jsonl")
        self.assertEqual(key_of(run1), key_of(run2))

    def test_key_different(self) -> None:
        run1 = _make_run(started=1000, session_id="s-1", platform="claude-code")
        run2 = _make_run(started=1000, session_id="s-2", platform="claude-code")
        self.assertNotEqual(key_of(run1), key_of(run2))


class TestIndexOf(unittest.TestCase):
    """12. index_of finds a run by key, and returns None for an unknown key."""

    def test_find_existing(self) -> None:
        run = _make_run(started=1000, session_id="s-1", platform="claude-code")
        store = RunStore(
            _load_runs=lambda **kw: [run],
            _load_subagent_runs=lambda **kw: [],
        )
        store._refresh()
        k = key_of(run)
        idx = store.index_of(k)
        self.assertIsNotNone(idx)
        self.assertEqual(store.snapshot()[idx].session_id, "s-1")

    def test_unknown_key(self) -> None:
        store = RunStore(
            _load_runs=lambda **kw: [_make_run(started=1000)],
            _load_subagent_runs=lambda **kw: [],
        )
        store._refresh()
        self.assertIsNone(store.index_of(("nope", "nope")))


class TestMergeOrder(unittest.TestCase):
    """Runs from both sources merge into a single newest-first list."""

    def test_mixed_sources_ordered(self) -> None:
        ledger_runs = [_make_run(started=1000, platform="delegate-log")]
        sa_runs = [_make_run(started=3000, platform="claude-code", session_id="sa")]

        store = RunStore(
            _load_runs=lambda **kw: ledger_runs,
            _load_subagent_runs=lambda **kw: sa_runs,
        )
        store._refresh()
        snap = store.snapshot()
        self.assertEqual([r.started for r in snap], [3000, 1000])
        self.assertEqual(snap[0].platform, "claude-code")
        self.assertEqual(snap[1].platform, "delegate-log")


if __name__ == "__main__":
    unittest.main()
