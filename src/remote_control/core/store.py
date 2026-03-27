"""SQLite-backed storage for tasks and sessions."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from remote_control.core.models import Memory, Session, Task, TaskStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    message     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    output      TEXT DEFAULT '',
    summary     TEXT DEFAULT '',
    error       TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    started_at  TEXT DEFAULT '',
    finished_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
    user_id       TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    working_dir   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT NOT NULL,
    initialized   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    type             TEXT NOT NULL,
    source_task      TEXT DEFAULT '',
    content          TEXT NOT NULL,
    tags             TEXT DEFAULT '',
    category         TEXT DEFAULT '',
    created_at       TEXT NOT NULL,
    consolidated_at  TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_memories_user_type ON memories(user_id, type, consolidated_at);
"""


class Store:
    """Shared SQLite store. Use ScopedStore for per-agent operations."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _migrate(self) -> None:
        """Apply schema migrations for existing databases."""
        assert self._conn is not None
        # Add initialized column to sessions
        sess_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "initialized" not in sess_cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN initialized INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()

        # Add agent_id columns if missing
        for table in ("tasks", "memories"):
            cols = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "agent_id" not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN agent_id TEXT NOT NULL DEFAULT ''")
                self._conn.commit()

        # Create index after adding agent_id
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent_id, status, created_at)"
        )
        self._conn.commit()

        # Sessions: migrate from single PK (user_id) to composite PK (user_id, agent_id)
        if "agent_id" not in sess_cols:
            # SQLite doesn't support ALTER TABLE to change PK, so we recreate
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions_new (
                    user_id       TEXT NOT NULL,
                    agent_id      TEXT NOT NULL DEFAULT '',
                    session_id    TEXT NOT NULL,
                    working_dir   TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    last_used_at  TEXT NOT NULL,
                    initialized   INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, agent_id)
                );
                INSERT OR IGNORE INTO sessions_new
                    SELECT user_id, '', session_id, working_dir, created_at, last_used_at, initialized
                    FROM sessions;
                DROP TABLE sessions;
                ALTER TABLE sessions_new RENAME TO sessions;
            """)
            self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Store not opened")
        return self._conn

    # --- Global queries (dashboard, watchdog) ---

    def get_task(self, task_id: str) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        output: str | None = None,
        summary: str | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        updates = ["status = ?"]
        params: list[str] = [status.value]
        if status == TaskStatus.RUNNING:
            updates.append("started_at = ?")
            params.append(now)
        if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            updates.append("finished_at = ?")
            params.append(now)
        if output is not None:
            updates.append("output = ?")
            params.append(output)
        if summary is not None:
            updates.append("summary = ?")
            params.append(summary)
        if error is not None:
            updates.append("error = ?")
            params.append(error)
        params.append(task_id)
        self.conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)
        self.conn.commit()

    def get_running_task(self) -> Task | None:
        """Get any running task (across all agents)."""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE status = ? LIMIT 1", (TaskStatus.RUNNING.value,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks_all_users(self, limit: int = 15) -> list[dict]:
        """Return recent tasks as dicts (includes agent_id for dashboard)."""
        rows = self.conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
        result = []
        for row in rows:
            t = self._row_to_task(row)
            d = {"id": t.id, "user_id": t.user_id, "status": t.status.value,
                 "message": t.message, "created_at": t.created_at,
                 "started_at": t.started_at, "finished_at": t.finished_at,
                 "agent_id": row["agent_id"] if "agent_id" in row.keys() else ""}
            result.append(d)
        return result

    def get_latest_task_any_user(self) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1").fetchone()
        return self._row_to_task(row) if row else None

    # --- Key-Value (global) ---

    def get_kv(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> Memory:
        return Memory(
            id=row["id"], user_id=row["user_id"], type=row["type"],
            source_task=row["source_task"] or "", content=row["content"],
            tags=row["tags"] or "", category=row["category"] or "",
            created_at=row["created_at"], consolidated_at=row["consolidated_at"] or "",
        )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"], user_id=row["user_id"], session_id=row["session_id"],
            message=row["message"], status=TaskStatus(row["status"]),
            output=row["output"] or "", summary=row["summary"] or "",
            error=row["error"] or "", created_at=row["created_at"],
            started_at=row["started_at"] or "", finished_at=row["finished_at"] or "",
        )


class ScopedStore:
    """Per-agent wrapper around Store that auto-filters by agent_id.

    All task, session, and memory operations are scoped to this agent.
    Global operations (kv, task status updates) delegate to the shared store.
    """

    def __init__(self, store: Store, agent_id: str):
        self._store = store
        self._agent_id = agent_id

    @property
    def conn(self) -> sqlite3.Connection:
        return self._store.conn

    # --- Delegate global methods ---

    def get_task(self, task_id: str) -> Task | None:
        return self._store.get_task(task_id)

    def update_task_status(self, *args, **kwargs):
        return self._store.update_task_status(*args, **kwargs)

    def get_kv(self, *args, **kwargs):
        return self._store.get_kv(*args, **kwargs)

    def set_kv(self, *args, **kwargs):
        return self._store.set_kv(*args, **kwargs)

    def list_tasks_all_users(self, limit=15):
        return self._store.list_tasks_all_users(limit)

    def get_latest_task_any_user(self) -> Task | None:
        """Get the latest task for this agent (across users)."""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE agent_id = ? ORDER BY created_at DESC LIMIT 1",
            (self._agent_id,),
        ).fetchone()
        return self._store._row_to_task(row) if row else None

    # --- Scoped task operations ---

    def create_task(self, user_id: str, session_id: str, message: str) -> Task:
        task = Task(user_id=user_id, session_id=session_id, message=message)
        self.conn.execute(
            "INSERT INTO tasks (id, user_id, agent_id, session_id, message, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task.id, task.user_id, self._agent_id, task.session_id,
             task.message, task.status.value, task.created_at),
        )
        self.conn.commit()
        return task

    def get_latest_task(self, user_id: str) -> Task | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND agent_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id, self._agent_id),
        ).fetchone()
        return self._store._row_to_task(row) if row else None

    def get_running_task(self) -> Task | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE agent_id = ? AND status = ? LIMIT 1",
            (self._agent_id, TaskStatus.RUNNING.value),
        ).fetchone()
        return self._store._row_to_task(row) if row else None

    def get_next_queued_task(self) -> Task | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE agent_id = ? AND status = ? ORDER BY created_at ASC LIMIT 1",
            (self._agent_id, TaskStatus.QUEUED.value),
        ).fetchone()
        return self._store._row_to_task(row) if row else None

    def list_tasks(self, user_id: str, limit: int = 10) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, self._agent_id, limit),
        ).fetchall()
        return [self._store._row_to_task(row) for row in rows]

    def clear_tasks(self, user_id: str) -> int:
        cursor = self.conn.execute(
            "DELETE FROM tasks WHERE user_id = ? AND agent_id = ? AND status != ?",
            (user_id, self._agent_id, TaskStatus.RUNNING.value),
        )
        self.conn.commit()
        return cursor.rowcount

    # --- Scoped session operations ---

    def get_or_create_session(self, user_id: str, default_working_dir: str) -> Session:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND agent_id = ?",
            (user_id, self._agent_id),
        ).fetchone()
        if row:
            return Session(
                user_id=row["user_id"], session_id=row["session_id"],
                working_dir=row["working_dir"], created_at=row["created_at"],
                last_used_at=row["last_used_at"], initialized=bool(row["initialized"]),
            )
        session = Session(user_id=user_id, working_dir=default_working_dir)
        self.conn.execute(
            "INSERT INTO sessions (user_id, agent_id, session_id, working_dir, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session.user_id, self._agent_id, session.session_id,
             session.working_dir, session.created_at, session.last_used_at),
        )
        self.conn.commit()
        return session

    def reset_session(self, user_id: str, default_working_dir: str) -> Session:
        now = datetime.now(timezone.utc).isoformat()
        new_session_id = str(uuid4())
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions (user_id, agent_id, session_id, working_dir, created_at, last_used_at, initialized) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (user_id, self._agent_id, new_session_id, default_working_dir, now, now),
        )
        self.conn.commit()
        return Session(user_id=user_id, session_id=new_session_id,
                       working_dir=default_working_dir, created_at=now, last_used_at=now)

    def mark_session_initialized(self, user_id: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET initialized = 1 WHERE user_id = ? AND agent_id = ?",
            (user_id, self._agent_id),
        )
        self.conn.commit()

    def update_session_used(self, user_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE sessions SET last_used_at = ? WHERE user_id = ? AND agent_id = ?",
            (now, user_id, self._agent_id),
        )
        self.conn.commit()

    def update_session_working_dir(self, user_id: str, working_dir: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET working_dir = ? WHERE user_id = ? AND agent_id = ?",
            (working_dir, user_id, self._agent_id),
        )
        self.conn.commit()

    # --- Scoped memory operations ---

    def create_memory(self, user_id: str, memory_type: str, content: str, tags: str,
                      source_task: str = "", category: str = "") -> Memory:
        mem = Memory(user_id=user_id, type=memory_type, content=content, tags=tags,
                     source_task=source_task, category=category)
        self.conn.execute(
            "INSERT INTO memories (id, user_id, agent_id, type, source_task, content, tags, category, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mem.id, mem.user_id, self._agent_id, mem.type, mem.source_task,
             mem.content, mem.tags, mem.category, mem.created_at),
        )
        self.conn.commit()
        return mem

    def get_recent_memories(self, user_id: str, limit: int = 5) -> list[Memory]:
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE user_id = ? AND agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, self._agent_id, limit),
        ).fetchall()
        return [self._store._row_to_memory(row) for row in rows]

    def get_consolidated_memories(self, user_id: str, limit: int = 50) -> list[Memory]:
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE user_id = ? AND agent_id = ? AND type = 'consolidated' "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, self._agent_id, limit),
        ).fetchall()
        return [self._store._row_to_memory(row) for row in rows]

    def get_keyword_matched_memories(self, user_id: str, keywords: list[str],
                                      limit: int = 5, exclude_recent: int = 5) -> list[Memory]:
        if not keywords:
            return []
        exclude_ids: list[str] = []
        if exclude_recent > 0:
            rows = self.conn.execute(
                "SELECT id FROM memories WHERE user_id = ? AND agent_id = ? AND type = 'raw' "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, self._agent_id, exclude_recent),
            ).fetchall()
            exclude_ids = [row["id"] for row in rows]

        cases = " + ".join("(CASE WHEN tags LIKE ? THEN 1 ELSE 0 END)" for _ in keywords)
        like_params = [f"%{kw.replace('%', '').replace('_', '')}%" for kw in keywords]
        exclude_clause = ""
        exclude_params: list[str] = []
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            exclude_clause = f" AND id NOT IN ({placeholders})"
            exclude_params = exclude_ids

        inner = (
            f"SELECT *, ({cases}) AS relevance FROM memories "
            f"WHERE user_id = ? AND agent_id = ? AND type = 'raw' AND consolidated_at = ''"
            f"{exclude_clause}"
        )
        sql = f"SELECT * FROM ({inner}) WHERE relevance > 0 ORDER BY relevance DESC, created_at DESC LIMIT ?"
        params = like_params + [user_id, self._agent_id] + exclude_params + [limit]
        rows = self.conn.execute(sql, params).fetchall()
        return [self._store._row_to_memory(row) for row in rows]

    def clear_memories(self, user_id: str) -> int:
        cursor = self.conn.execute(
            "DELETE FROM memories WHERE user_id = ? AND agent_id = ?",
            (user_id, self._agent_id),
        )
        self.conn.commit()
        return cursor.rowcount

    def get_memory_stats(self, user_id: str) -> dict:
        row = self.conn.execute(
            "SELECT "
            "SUM(CASE WHEN type = 'raw' THEN 1 ELSE 0 END) as raw_count, "
            "SUM(CASE WHEN type = 'consolidated' THEN 1 ELSE 0 END) as consolidated_count "
            "FROM memories WHERE user_id = ? AND agent_id = ?",
            (user_id, self._agent_id),
        ).fetchone()
        return {"raw_count": row["raw_count"] or 0, "consolidated_count": row["consolidated_count"] or 0}
