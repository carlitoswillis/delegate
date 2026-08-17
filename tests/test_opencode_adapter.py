"""Tests for delegate_view.adapters.opencode — uses in-memory SQLite only."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from delegate_view.adapters.opencode import (
    default_db_path,
    list_sessions,
    load_session,
)


def _make_db(tmp: Path) -> sqlite3.Connection:
    """Create a fresh opencode-schema DB in *tmp* and return a connection."""
    db = tmp / "test.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE session (
            id text PRIMARY KEY,
            project_id text NOT NULL,
            workspace_id text,
            parent_id text,
            slug text NOT NULL,
            directory text NOT NULL,
            path text,
            title text NOT NULL,
            version text NOT NULL,
            share_url text,
            summary_additions integer,
            summary_deletions integer,
            summary_files integer,
            summary_diffs text,
            metadata text,
            cost real DEFAULT 0 NOT NULL,
            tokens_input integer DEFAULT 0 NOT NULL,
            tokens_output integer DEFAULT 0 NOT NULL,
            tokens_reasoning integer DEFAULT 0 NOT NULL,
            tokens_cache_read integer DEFAULT 0 NOT NULL,
            tokens_cache_write integer DEFAULT 0 NOT NULL,
            revert text,
            permission text,
            agent text,
            model text,
            time_created integer NOT NULL,
            time_updated integer NOT NULL,
            time_compacting integer,
            time_archived integer
        );
        CREATE TABLE message (
            id text PRIMARY KEY,
            session_id text NOT NULL,
            time_created integer NOT NULL,
            time_updated integer NOT NULL,
            data text NOT NULL
        );
        CREATE TABLE part (
            id text PRIMARY KEY,
            message_id text NOT NULL,
            session_id text NOT NULL,
            time_created integer NOT NULL,
            time_updated integer NOT NULL,
            data text NOT NULL
        );
    """)
    conn.commit()
    return conn, db  # type: ignore[return-value]


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._conn, self._db = _make_db(self._tmp)

    def tearDown(self):
        self._conn.close()

    # helpers ---------------------------------------------------------------

    def _insert_session(self, **kw):
        defaults = dict(
            id="s1", project_id="p1", slug="s1", directory="/home/user",
            title="t", version="1", cost=0.0, tokens_input=0, tokens_output=0,
            tokens_reasoning=0, tokens_cache_read=0, tokens_cache_write=0,
            time_created=1000, time_updated=2000,
        )
        defaults.update(kw)
        cols = ", ".join(defaults.keys())
        phs = ", ".join(["?"] * len(defaults))
        self._conn.execute(
            f"INSERT INTO session ({cols}) VALUES ({phs})",
            list(defaults.values()),
        )
        self._conn.commit()

    def _insert_message(self, session_id, msg_id, role, time_created,
                        time_updated=None):
        data = json.dumps({"role": role})
        self._conn.execute(
            "INSERT INTO message (id,session_id,time_created,time_updated,data) "
            "VALUES (?,?,?,?,?)",
            (msg_id, session_id, time_created, time_updated or time_created, data),
        )
        self._conn.commit()

    def _insert_part(self, session_id, msg_id, part_id, data_dict,
                     time_created=100, time_updated=100):
        self._conn.execute(
            "INSERT INTO part (id,message_id,session_id,time_created,time_updated,data) "
            "VALUES (?,?,?,?,?,?)",
            (part_id, msg_id, session_id, time_created, time_updated,
             json.dumps(data_dict)),
        )
        self._conn.commit()


class TestListSessionsOrdering(_Base):
    def test_newest_updated_first(self):
        self._insert_session(id="a", title="A", time_updated=100)
        self._insert_session(id="b", title="B", time_updated=300)
        self._insert_session(id="c", title="C", time_updated=200)
        sessions = list_sessions(self._db)
        self.assertEqual([s.id for s in sessions], ["b", "c", "a"])


class TestListSessionsFilter(_Base):
    def test_filter_by_cwd(self):
        self._insert_session(id="a", directory="/foo", time_updated=100)
        self._insert_session(id="b", directory="/bar", time_updated=200)
        sessions = list_sessions(self._db, cwd="/bar")
        self.assertEqual([s.id for s in sessions], ["b"])


class TestModelParsing(_Base):
    def test_normal_model(self):
        model = json.dumps({"id": "big-pickle", "providerID": "opencode"})
        self._insert_session(id="s1", model=model)
        s = list_sessions(self._db)[0]
        self.assertEqual(s.model, "opencode/big-pickle")

    def test_null_model(self):
        self._insert_session(id="s1", model=None)
        s = list_sessions(self._db)[0]
        self.assertEqual(s.model, "")

    def test_empty_string_model(self):
        self._insert_session(id="s1", model="")
        s = list_sessions(self._db)[0]
        self.assertEqual(s.model, "")

    def test_malformed_model_json(self):
        self._insert_session(id="s1", model="not-json")
        s = list_sessions(self._db)[0]
        self.assertEqual(s.model, "")


class TestStepFramesExcluded(_Base):
    def test_step_start_and_finish_skipped(self):
        self._insert_session(id="s1")
        self._insert_message("s1", "m1", "assistant", 100)
        self._insert_part("s1", "m1", "p1", {"type": "step-start", "step": {}})
        self._insert_part("s1", "m1", "p2", {"type": "step-finish", "step": {}})
        session = load_session("s1", self._db)
        self.assertEqual(len(session.events), 0)


class TestToolPartMapping(_Base):
    def test_tool_part_maps_correctly(self):
        self._insert_session(id="s1")
        self._insert_message("s1", "m1", "assistant", 100)
        tool_data = {
            "type": "tool",
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {"command": "echo hi"},
                "output": "hi\n",
                "time": {"start": 1000, "end": 1050},
            },
        }
        self._insert_part("s1", "m1", "p1", tool_data, time_created=100)
        session = load_session("s1", self._db)
        self.assertEqual(len(session.events), 1)
        ev = session.events[0]
        self.assertEqual(ev.kind, "tool_call")
        self.assertEqual(ev.tool_name, "bash")
        self.assertEqual(ev.tool_input, {"command": "echo hi"})
        self.assertEqual(ev.tool_output, "hi\n")
        self.assertEqual(ev.tool_status, "completed")
        self.assertEqual(ev.tool_ms, 50)


class TestToolNoTimeEnd(_Base):
    def test_tool_with_no_end_yields_none(self):
        self._insert_session(id="s1")
        self._insert_message("s1", "m1", "assistant", 100)
        tool_data = {
            "type": "tool",
            "tool": "bash",
            "state": {
                "status": "pending",
                "input": {"command": "sleep 999"},
                "time": {"start": 1000},
            },
        }
        self._insert_part("s1", "m1", "p1", tool_data, time_created=100)
        session = load_session("s1", self._db)
        ev = session.events[0]
        self.assertIsNone(ev.tool_ms)
        self.assertEqual(ev.tool_status, "pending")


class TestToolMissingInputOutput(_Base):
    def test_tool_with_no_input_output_keys(self):
        self._insert_session(id="s1")
        self._insert_message("s1", "m1", "assistant", 100)
        tool_data = {
            "type": "tool",
            "tool": "read",
            "state": {"status": "completed"},
        }
        self._insert_part("s1", "m1", "p1", tool_data, time_created=100)
        session = load_session("s1", self._db)
        ev = session.events[0]
        self.assertEqual(ev.tool_input, {})
        self.assertEqual(ev.tool_output, "")
        self.assertEqual(ev.tool_status, "completed")


class TestLoadSessionUnknown(_Base):
    def test_raises_key_error(self):
        with self.assertRaises(KeyError):
            load_session("nonexistent", self._db)


class TestEventOrdering(_Base):
    def test_ordered_by_message_time_then_message_id_then_part_id(self):
        self._insert_session(id="s1")
        # Two messages at different times
        self._insert_message("s1", "m2", "user", 200)
        self._insert_message("s1", "m1", "assistant", 100)
        # Parts in m1
        self._insert_part("s1", "m1", "b_part",
                          {"type": "text", "text": "b"}, time_created=100)
        self._insert_part("s1", "m1", "a_part",
                          {"type": "text", "text": "a"}, time_created=100)
        # Part in m2
        self._insert_part("s1", "m2", "c_part",
                          {"type": "text", "text": "c"}, time_created=200)

        session = load_session("s1", self._db)
        texts = [e.text for e in session.events]
        self.assertEqual(texts, ["a", "b", "c"])


class TestPatchAndCompaction(_Base):
    def test_patch_event(self):
        self._insert_session(id="s1")
        self._insert_message("s1", "m1", "assistant", 100)
        patch_data = {"type": "patch", "hash": "abc123",
                      "files": ["/foo/bar.py"]}
        self._insert_part("s1", "m1", "p1", patch_data, time_created=100)
        session = load_session("s1", self._db)
        ev = session.events[0]
        self.assertEqual(ev.kind, "patch")
        self.assertEqual(ev.raw, patch_data)

    def test_compaction_event(self):
        self._insert_session(id="s1")
        self._insert_message("s1", "m1", "assistant", 100)
        compact_data = {"type": "compaction", "auto": True,
                        "overflow": False, "tail_start_id": "msg_xxx"}
        self._insert_part("s1", "m1", "p1", compact_data, time_created=100)
        session = load_session("s1", self._db)
        ev = session.events[0]
        self.assertEqual(ev.kind, "compaction")
        self.assertEqual(ev.raw, compact_data)


class TestDefaultDbPath(unittest.TestCase):
    def test_points_to_expected_location(self):
        p = default_db_path()
        self.assertEqual(p.name, "opencode.db")
        self.assertTrue(str(p).endswith(".local/share/opencode/opencode.db"))


if __name__ == "__main__":
    unittest.main()
