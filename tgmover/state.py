"""Durable message, discussion, download, and uncertain-send state in SQLite."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any


_MESSAGE_FIELDS = (
    "source_id", "target_id", "grouped_id", "source_chat_id", "target_chat_id",
    "source_post_id", "source_root_id", "target_root_id", "status", "text", "metadata",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for field in ("metadata", "signature", "source_ids"):
        if field in result:
            result[field] = json.loads(result[field])
    return result


class State:
    """A single-process state store, with atomic batch and cursor commits.

    An operation is persisted *before* sending and removed only after recording
    results. If a process dies between these steps, pending() exposes the
    uncertain result so callers can stop instead of duplicating an upload.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS routes (
                id TEXT PRIMARY KEY,
                signature TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS cursors (
                route TEXT NOT NULL REFERENCES routes(id),
                scope TEXT NOT NULL,
                thread_id INTEGER NOT NULL DEFAULT 0,
                value INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(route, scope, thread_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                route TEXT NOT NULL REFERENCES routes(id),
                scope TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                target_id INTEGER,
                grouped_id INTEGER,
                source_chat_id INTEGER,
                target_chat_id INTEGER,
                source_post_id INTEGER,
                source_root_id INTEGER,
                target_root_id INTEGER,
                status TEXT NOT NULL DEFAULT 'done',
                text TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(route, scope, source_id)
            );
            CREATE TABLE IF NOT EXISTS threads (
                route TEXT NOT NULL REFERENCES routes(id),
                source_post_id INTEGER NOT NULL,
                source_root_id INTEGER,
                target_post_id INTEGER,
                target_root_id INTEGER,
                source_chat_id INTEGER,
                target_chat_id INTEGER,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(route, source_post_id)
            );
            CREATE TABLE IF NOT EXISTS downloads (
                route TEXT NOT NULL REFERENCES routes(id),
                scope TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                size INTEGER NOT NULL CHECK(size >= 0),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(route, scope, source_id)
            );
            CREATE TABLE IF NOT EXISTS operations (
                route TEXT NOT NULL REFERENCES routes(id),
                scope TEXT NOT NULL,
                key TEXT NOT NULL,
                source_ids TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(route, scope, key)
            );
        """)
        # Keep a file audit independently of the latest per-message download.
        # A user may choose another download directory for the same route.
        # Losing the previous path would make that archived copy unmanageable.
        download_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(downloads)")}
        additions = {
            "root": "TEXT", "st_dev": "INTEGER", "st_ino": "INTEGER",
            "mtime_ns": "INTEGER", "cleanup_status": "TEXT NOT NULL DEFAULT ''",
            "cleaned_at": "TEXT", "cleanup_error": "TEXT",
        }
        with self.conn:
            for name, definition in additions.items():
                if name not in download_columns:
                    self.conn.execute(f"ALTER TABLE downloads ADD COLUMN {name} {definition}")
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS download_files (
                    route TEXT NOT NULL REFERENCES routes(id),
                    scope TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK(size >= 0),
                    root TEXT,
                    st_dev INTEGER,
                    st_ino INTEGER,
                    mtime_ns INTEGER,
                    cleanup_status TEXT NOT NULL DEFAULT '',
                    cleaned_at TEXT,
                    cleanup_error TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(route, scope, source_id, path)
                )
            """)
            self.conn.execute("""
                INSERT OR IGNORE INTO download_files(
                    route, scope, source_id, path, size, root, st_dev, st_ino,
                    mtime_ns, cleanup_status, cleaned_at, cleanup_error, updated_at)
                SELECT route, scope, source_id, path, size, root, st_dev, st_ino,
                    mtime_ns, cleanup_status, cleaned_at, cleanup_error, updated_at
                FROM downloads
            """)

    def close(self) -> None:
        self.conn.close()

    def route(self, signature: dict[str, Any]) -> str:
        canonical = _json(signature)
        route = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO routes(id, signature) VALUES (?, ?)", (route, canonical))
        return route

    def cursor(self, route: str, scope: str, thread_id: int = 0) -> int:
        row = self.conn.execute(
            "SELECT value FROM cursors WHERE route=? AND scope=? AND thread_id=?",
            (route, scope, thread_id),
        ).fetchone()
        return int(row["value"]) if row is not None else 0

    def _set_cursor(self, route: str, scope: str, value: int, thread_id: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("A cursor must be a non-negative integer.")
        self.conn.execute(
            """INSERT INTO cursors(route, scope, thread_id, value) VALUES (?, ?, ?, ?)
               ON CONFLICT(route, scope, thread_id) DO UPDATE SET value=MAX(value, excluded.value)""",
            (route, scope, thread_id, value),
        )

    def set_cursor(self, route: str, scope: str, value: int, thread_id: int = 0) -> None:
        with self.conn:
            self._set_cursor(route, scope, value, thread_id)

    def get_message(self, route: str, scope: str, source_id: int) -> dict[str, Any] | None:
        return _decode(self.conn.execute(
            "SELECT * FROM messages WHERE route=? AND scope=? AND source_id=?",
            (route, scope, source_id),
        ).fetchone())

    def save_messages(
        self, route: str, scope: str, rows: list[dict[str, Any]],
        cursor: int | None = None, thread_id: int = 0,
    ) -> None:
        placeholders = ", ".join("?" for _ in range(len(_MESSAGE_FIELDS) + 2))
        updates = ", ".join(f"{field}=excluded.{field}" for field in _MESSAGE_FIELDS if field != "source_id")
        sql = (
            f"INSERT INTO messages(route, scope, {', '.join(_MESSAGE_FIELDS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(route, scope, source_id) DO UPDATE SET {updates}, updated_at=CURRENT_TIMESTAMP"
        )
        with self.conn:
            for row in rows:
                if "source_id" not in row:
                    raise ValueError("Every message row requires source_id.")
                values = {field: row.get(field) for field in _MESSAGE_FIELDS}
                values["status"] = row.get("status", "done")
                values["text"] = row.get("text", "")
                values["metadata"] = _json(row.get("metadata", {}))
                self.conn.execute(sql, (route, scope, *(values[field] for field in _MESSAGE_FIELDS)))
            if cursor is not None:
                self._set_cursor(route, scope, cursor, thread_id)

    def messages(self, route: str, scope: str) -> list[dict[str, Any]]:
        return [_decode(row) for row in self.conn.execute(
            "SELECT * FROM messages WHERE route=? AND scope=? ORDER BY source_id", (route, scope)
        )]

    def get_thread(self, route: str, source_post_id: int) -> dict[str, Any] | None:
        return _decode(self.conn.execute(
            "SELECT * FROM threads WHERE route=? AND source_post_id=?", (route, source_post_id)
        ).fetchone())

    def save_thread(
        self, route: str, source_post_id: int, source_root_id: int | None,
        target_post_id: int | None, target_root_id: int | None,
        source_chat_id: int | None, target_chat_id: int | None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO threads(route, source_post_id, source_root_id, target_post_id,
                       target_root_id, source_chat_id, target_chat_id) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(route, source_post_id) DO UPDATE SET
                       source_root_id=excluded.source_root_id, target_post_id=excluded.target_post_id,
                       target_root_id=excluded.target_root_id, source_chat_id=excluded.source_chat_id,
                       target_chat_id=excluded.target_chat_id, updated_at=CURRENT_TIMESTAMP""",
                (route, source_post_id, source_root_id, target_post_id, target_root_id, source_chat_id, target_chat_id),
            )

    def threads(self, route: str) -> list[dict[str, Any]]:
        return [_decode(row) for row in self.conn.execute(
            "SELECT * FROM threads WHERE route=? ORDER BY source_post_id", (route,)
        )]

    def download(self, route: str, scope: str, source_id: int) -> dict[str, Any] | None:
        return _decode(self.conn.execute(
            "SELECT * FROM downloads WHERE route=? AND scope=? AND source_id=?", (route, scope, source_id)
        ).fetchone())

    def save_download(
        self, route: str, scope: str, source_id: int, path: str | Path, size: int,
        *, root: str | Path | None = None,
    ) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("A downloaded file size must be a non-negative integer.")
        # Do not resolve symlinks here: cleanup must be able to detect a linked
        # component rather than silently registering its destination as trusted.
        location = Path(os.path.abspath(path))
        base = Path(os.path.abspath(root)) if root is not None else None
        if base is not None and not location.is_relative_to(base):
            raise ValueError("A downloaded file must be inside its registered root.")
        identity = (None, None, None)
        try:
            info = location.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_size == size:
                # Windows/ReFS (and some NTFS providers) expose an unsigned or
                # 128-bit file id. Decimal SQLite INTEGER affinity can overflow
                # or silently round it; a hex string preserves every bit.
                identity = (hex(info.st_dev), hex(info.st_ino), info.st_mtime_ns)
        except OSError:
            # Legacy callers can record an unavailable backup. Cleanup keeps
            # records without identity; it never claims a later replacement.
            pass
        values = (route, scope, source_id, str(location), size,
                  str(base) if base is not None else None, *identity)
        with self.conn:
            # Preserve rows created by older versions or direct SQLite imports
            # before replacing the single latest-download pointer.
            self.conn.execute("""
                INSERT OR IGNORE INTO download_files(
                    route, scope, source_id, path, size, root, st_dev, st_ino,
                    mtime_ns, cleanup_status, cleaned_at, cleanup_error, updated_at)
                SELECT route, scope, source_id, path, size, root, st_dev, st_ino,
                    mtime_ns, cleanup_status, cleaned_at, cleanup_error, updated_at
                FROM downloads WHERE route=? AND scope=? AND source_id=?
            """, (route, scope, source_id))
            self.conn.execute(
                """INSERT INTO downloads(route, scope, source_id, path, size, root, st_dev, st_ino, mtime_ns)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(route, scope, source_id) DO UPDATE SET
                       path=excluded.path, size=excluded.size, root=excluded.root,
                       st_dev=excluded.st_dev, st_ino=excluded.st_ino, mtime_ns=excluded.mtime_ns,
                       cleanup_status='', cleaned_at=NULL, cleanup_error=NULL,
                       updated_at=CURRENT_TIMESTAMP""", values,
            )
            self.conn.execute(
                """INSERT INTO download_files(route, scope, source_id, path, size, root, st_dev, st_ino, mtime_ns)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(route, scope, source_id, path) DO UPDATE SET
                       size=excluded.size, root=excluded.root, st_dev=excluded.st_dev,
                       st_ino=excluded.st_ino, mtime_ns=excluded.mtime_ns,
                       cleanup_status='', cleaned_at=NULL, cleanup_error=NULL,
                       updated_at=CURRENT_TIMESTAMP""", values,
            )

    def pending(self, route: str) -> list[dict[str, Any]]:
        return [_decode(row) for row in self.conn.execute(
            "SELECT * FROM operations WHERE route=? ORDER BY started_at, scope, key", (route,)
        )]

    def begin_operation(self, route: str, scope: str, key: str, source_ids: list[int]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO operations(route, scope, key, source_ids) VALUES (?, ?, ?, ?)",
                (route, scope, key, _json(source_ids)),
            )

    def finish_operation(self, route: str, scope: str, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM operations WHERE route=? AND scope=? AND key=?", (route, scope, key))
