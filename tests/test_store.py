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

from delegate_view.runs import (
    UNCLOSED_LIVE_WINDOW_S, Run, transcript_state,
)
from delegate_view.store import RunStore, key_of


def load_runs_like(path: str) -> list[Run]:
    """A ledger run for `path`, with its transcript state read off disk.

    The store recomputes liveness itself, but end_reason comes from whoever
    loaded the run — this is the smallest stand-in for load_runs() that does
    not need a ledger file.
    """
    _has_reply, end_reason, size, _mtime = transcript_state(path)
    return [Run(started=1000, prompt="", transcript=path, model="test/model",
                cwd="/tmp", platform="delegate-log", size=size,
                end_reason=end_reason)]


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
    """6. Liveness is recomputed each refresh from the transcript's own state."""

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
            # File was just written and has no END marker: still running.
            store._refresh()
            self.assertTrue(store.snapshot()[0].live)

            # Back-date the mtime past the unclosed-transcript window. A run
            # that was killed hard never writes its marker, so mtime is what
            # eventually calls it dead.
            stale = time.time() - (UNCLOSED_LIVE_WINDOW_S + 60)
            os.utime(path, (stale, stale))
            store._refresh()
            self.assertFalse(store.snapshot()[0].live)
        finally:
            os.unlink(path)

    def test_end_marker_beats_a_fresh_mtime(self) -> None:
        """A closed transcript is not live no matter how recently it changed."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as f:
            f.write(b"\n===== REPLY =====\n\nhi\n")
            path = f.name
        try:
            run = _make_run(transcript=path, started=1000)
            store = RunStore(
                _load_runs=lambda **kw: [run],
                _load_subagent_runs=lambda **kw: [],
            )
            store._refresh()
            self.assertTrue(store.snapshot()[0].live)

            # delegate.sh closes the transcript on the way out.
            with open(path, "a") as fh:
                fh.write("\n===== END 2026-08-17 21:51:30 — completed =====\n")
            store._load_runs_fn = lambda **kw: load_runs_like(path)
            store._refresh()
            self.assertFalse(store.snapshot()[0].live)
        finally:
            os.unlink(path)

    def test_opencode_run_is_live_from_updated(self) -> None:
        """A session with no file of its own is live off its `updated` time."""
        now_ms = int(time.time() * 1000)
        fresh = Run(started=now_ms, prompt="", transcript="", model="m",
                    cwd="/proj", session_id="s1", platform="opencode",
                    source="opencode", updated=now_ms)
        store = RunStore(_all_runs=lambda **kw: [fresh])
        store._refresh()
        self.assertTrue(store.snapshot()[0].live)

        stale = Run(started=1000, prompt="", transcript="", model="m",
                    cwd="/proj", session_id="s2", platform="opencode",
                    source="opencode", updated=now_ms - 600_000)
        store2 = RunStore(_all_runs=lambda **kw: [stale])
        store2._refresh()
        self.assertFalse(store2.snapshot()[0].live)


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


class TestUnifiedSource(unittest.TestCase):
    """The store now loads one unified list instead of two partial ones."""

    def test_default_loader_is_all_runs(self):
        called = {}

        def fake_all_runs(**kw):
            called.update(kw)
            return [_make_run(started=1000, session_id="s1",
                              platform="opencode")]

        store = RunStore(ledger_path="/tmp/ledger.jsonl", _all_runs=fake_all_runs)
        store._refresh()
        self.assertEqual(called["ledger_path"], "/tmp/ledger.jsonl")
        self.assertEqual(len(store.snapshot()), 1)

    def test_claude_code_is_the_only_capped_source(self):
        """Capping opencode is the bug this work was for."""
        called = {}
        store = RunStore(subagent_limit=7,
                         _all_runs=lambda **kw: called.update(kw) or [])
        store._refresh()
        self.assertEqual(called["limit_per_source"], {"claude-code": 7})

    def test_a_failing_unified_load_leaves_the_list_alone(self):
        store = RunStore(_all_runs=lambda **kw: [_make_run(started=1000)])
        store._refresh()
        self.assertEqual(len(store.snapshot()), 1)

        def boom(**kw):
            raise RuntimeError("everything is on fire")

        store._all_runs_fn = boom
        store._refresh()
        self.assertEqual(len(store.snapshot()), 1, "kept, not wiped")


class TestSubagentFilter(unittest.TestCase):
    """include_subagents hides agent-to-agent chats, not a whole platform."""

    def _runs(self):
        chat = _make_run(started=2000, session_id="chat", platform="opencode")
        sub = _make_run(started=1000, session_id="sub", platform="opencode")
        sub.is_subagent = True
        return [chat, sub]

    def test_subagents_included_by_default(self):
        store = RunStore(_all_runs=lambda **kw: self._runs())
        store._refresh()
        self.assertEqual(len(store.snapshot()), 2)

    def test_subagents_excluded_on_request(self):
        store = RunStore(include_subagents=False,
                         _all_runs=lambda **kw: self._runs())
        store._refresh()
        snap = store.snapshot()
        self.assertEqual([r.session_id for r in snap], ["chat"])

    def test_a_direct_chat_survives_the_filter(self):
        """The old flag dropped Claude Code entirely, top-level chats included."""
        direct = _make_run(started=3000, session_id="cc-1",
                           platform="claude-code")
        store = RunStore(include_subagents=False,
                         _all_runs=lambda **kw: [direct])
        store._refresh()
        self.assertEqual(len(store.snapshot()), 1)


class TestInPlaceUpdateCarriesNewFields(unittest.TestCase):
    """A refresh must not strand the fields the display keys off."""

    def test_failure_and_source_fields_refresh(self):
        first = _make_run(started=1000, session_id="s1", platform="opencode")
        store = RunStore(_all_runs=lambda **kw: [first])
        store._refresh()
        held = store.snapshot()[0]

        updated = _make_run(started=1000, session_id="s1", platform="opencode")
        updated.failed = True
        updated.end_reason = "failed (exit 3)"
        updated.is_subagent = True
        updated.parent_id = "p1"
        updated.source = "opencode"
        updated.updated = 12345
        store._all_runs_fn = lambda **kw: [updated]
        store._refresh()

        self.assertIs(store.snapshot()[0], held, "same object, updated in place")
        self.assertTrue(held.failed)
        self.assertEqual(held.end_reason, "failed (exit 3)")
        self.assertTrue(held.is_subagent)
        self.assertEqual(held.parent_id, "p1")
        self.assertEqual(held.source, "opencode")
        self.assertEqual(held.updated, 12345)

    def test_a_resolved_session_is_never_unresolved_by_a_later_cycle(self):
        resolved = _make_run(started=1000, session_id="s1", platform="opencode")
        store = RunStore(_all_runs=lambda **kw: [resolved])
        store._refresh()

        blank = _make_run(started=1000, session_id="s1", platform="opencode")
        blank.model = ""
        store._all_runs_fn = lambda **kw: [blank]
        store._refresh()
        self.assertEqual(store.snapshot()[0].model, "test/model")


class TestStatus(unittest.TestCase):
    """The header reads "loading…" until the first refresh completes.

    Without it an empty list is ambiguous — "nothing delegated yet" and
    "still reading four thousand transcripts" render identically.
    """

    def _wait_for_clear(self, store: RunStore) -> None:
        deadline = time.time() + 2.0
        while store.status() and time.time() < deadline:
            time.sleep(0.01)

    def test_loading_before_start_then_cleared(self):
        store = RunStore(_all_runs=lambda **kw: [])
        self.assertEqual(store.status(), "loading…")
        store.start()
        self._wait_for_clear(store)
        self.assertEqual(store.status(), "")
        store.stop()

    def test_cleared_even_when_the_source_raises(self):
        def boom(**kw):
            raise RuntimeError("no store on this machine")

        store = RunStore(_all_runs=boom)
        store.start()
        self._wait_for_clear(store)
        self.assertEqual(store.status(), "", "a list that will never load "
                         "must not say loading forever")
        store.stop()


if __name__ == "__main__":
    unittest.main()
