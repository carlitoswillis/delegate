"""Adapter that reads the opencode SQLite database and emits Session / Event."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from delegate_view.schema import Event, Session, norm_dir


def default_db_path() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _open_ro(db_path: Path) -> sqlite3.Connection:
    """A read-only connection, retried, then degraded rather than abandoned.

    The database is LIVE: opencode is usually running and writing to it. That
    is safe to read from — it runs in WAL mode, where a reader sees a
    consistent snapshot and never blocks a writer or waits on one. Verified
    under load: 28 full listings taken while another connection committed
    105,000 rows, none blocked and none failed.

    A read-only open still needs the -shm file, and creating it needs a
    writable DIRECTORY. That is the one way this fails on a healthy database:
    a hot WAL left behind by a killed process, sitting somewhere the viewer
    cannot write — a copied backup, a read-only mount. `immutable=1` is the
    honest last resort there: it tells SQLite the file cannot change, so the
    WAL is ignored and the last checkpointed state is read. That state can be
    slightly stale, which for a viewer is a far better answer than no list at
    all. It is tried only after the normal open has failed three times, so a
    healthy database never takes this path.
    """
    uri = f"file:{db_path}?mode=ro"
    last_exc: Exception | None = None
    for _ in range(3):
        try:
            return sqlite3.connect(uri, uri=True, timeout=5)
        except sqlite3.OperationalError as exc:
            last_exc = exc
            time.sleep(0.2)
    try:
        return sqlite3.connect(f"{uri}&immutable=1", uri=True, timeout=5)
    except sqlite3.OperationalError:
        raise last_exc  # type: ignore[misc]


def _parse_model(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    pid = obj.get("providerID", "")
    mid = obj.get("id", "")
    if pid and mid:
        return f"{pid}/{mid}"
    return ""


def _part_to_event(part_data: dict, role: str, fallback_ts: int = 0) -> Event | None:
    """Map one part payload to an Event, or None for control frames.

    fallback_ts is the parent message's time_created. Most part types carry no
    time of their own — only `reasoning` has a top-level `time.start`, and
    `tool` hides its own under `state.time.start`. Without the fallback, text,
    patch and compaction events all land at ts=0 and sort to the epoch.
    """
    kind = part_data.get("type", "")
    if kind in ("step-start", "step-finish"):
        return None

    part_time = part_data.get("time") or {}
    state = part_data.get("state") or {}
    state_time = state.get("time") or {}
    # state.time.start is where `tool` parts keep their start; the top-level
    # time.start is where `reasoning` keeps it. Neither exists on text/patch.
    ts = part_time.get("start") or state_time.get("start") or fallback_ts or 0

    if kind == "text":
        return Event(ts=ts, role=role, kind="text",
                     text=part_data.get("text", ""), raw=part_data)

    if kind == "reasoning":
        return Event(ts=ts, role=role, kind="reasoning",
                     text=part_data.get("text", ""), raw=part_data)

    if kind == "compaction":
        return Event(ts=ts, role=role, kind="compaction", raw=part_data)

    if kind == "patch":
        return Event(ts=ts, role=role, kind="patch", raw=part_data)

    if kind == "tool":
        start = state_time.get("start")
        end = state_time.get("end")
        tool_ms = None
        if start is not None and end is not None:
            diff = end - start
            tool_ms = diff if diff >= 0 else None

        return Event(
            ts=ts,
            role=role,
            kind="tool_call",
            tool_name=part_data.get("tool", ""),
            tool_input=state.get("input", {}) or {},
            tool_output=state.get("output", "") or "",
            tool_status=state.get("status", "") or "",
            tool_ms=tool_ms,
            raw=part_data,
        )

    return None


def list_sessions(db_path: Path | None = None,
                  cwd: str | None = None) -> list[Session]:
    db_path = db_path or default_db_path()
    conn = _open_ro(db_path)
    try:
        # The cwd filter is applied in Python, not as `WHERE directory = ?`.
        # An exact string match misses the session it is looking for whenever
        # the two sides spell the same directory differently — the ledger
        # records the shell's logical `$PWD` (`/tmp/x`) while opencode stores
        # the physical path (`/private/tmp/x`), and macOS's case-insensitive
        # filesystem lets `termdeck` and `Termdeck` both be real. That failed
        # SILENTLY: the run simply never resolved to its session. norm_dir()
        # is the comparison both sides agree on, and it cannot be pushed into
        # SQL. Scanning the whole session table costs nothing worth measuring
        # (0.5ms for 64 rows on a 140MB database) because the rows are small
        # and the bulk of the file is messages and parts.
        sql = """
            SELECT id, parent_id, directory, title, cost,
                   tokens_input, tokens_output,
                   agent, model, time_created, time_updated
            FROM session
            ORDER BY time_updated DESC
        """
        rows = conn.execute(sql).fetchall()

        if cwd is not None:
            want = norm_dir(cwd)
            rows = [r for r in rows if norm_dir(r[2]) == want]

        return [
            Session(
                id=r[0],
                platform="opencode",
                title=r[3],
                cwd=r[2],
                model=_parse_model(r[8]),
                agent=r[7] or "",
                parent_id=r[1],
                created=r[9],
                updated=r[10],
                cost=r[4] or 0.0,
                tokens_in=r[5] or 0,
                tokens_out=r[6] or 0,
                events=[],
            )
            for r in rows
        ]
    finally:
        conn.close()


def load_session(session_id: str,
                 db_path: Path | None = None) -> Session:
    db_path = db_path or default_db_path()
    conn = _open_ro(db_path)
    try:
        row = conn.execute(
            """SELECT id, parent_id, directory, title, cost,
                      tokens_input, tokens_output,
                      agent, model, time_created, time_updated
               FROM session WHERE id = ?""",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(session_id)

        session = Session(
            id=row[0],
            platform="opencode",
            title=row[3],
            cwd=row[2],
            model=_parse_model(row[8]),
            agent=row[7] or "",
            parent_id=row[1],
            created=row[9],
            updated=row[10],
            cost=row[4] or 0.0,
            tokens_in=row[5] or 0,
            tokens_out=row[6] or 0,
            events=[],
        )

        # Fetch all messages ordered by (time_created, id)
        messages = conn.execute(
            """SELECT id, data, time_created FROM message
               WHERE session_id = ?
               ORDER BY time_created, id""",
            (session_id,),
        ).fetchall()

        events: list[Event] = []
        for msg_id, msg_data, msg_ts in messages:
            try:
                msg_obj = json.loads(msg_data)
            except (json.JSONDecodeError, TypeError):
                continue
            role = msg_obj.get("role", "")

            parts = conn.execute(
                """SELECT id, data FROM part
                   WHERE message_id = ?
                   ORDER BY id""",
                (msg_id,),
            ).fetchall()

            # Parts that carry no time of their own inherit the latest time
            # seen so far in this message rather than the message's start.
            # Using the message start would send an untimed `patch` backwards
            # past the timed `tool` call that produced it.
            running_ts = msg_ts or 0
            for _part_id, part_data_raw in parts:
                try:
                    part_data = json.loads(part_data_raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                ev = _part_to_event(part_data, role, fallback_ts=running_ts)
                if ev is not None:
                    running_ts = max(running_ts, ev.ts)
                    events.append(ev)

        session.events = events
        return session
    finally:
        conn.close()
