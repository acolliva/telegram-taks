"""SQLite storage: tasks, their photos, and the open "to-do" view of each chat."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY,
    title          TEXT NOT NULL,
    notes          TEXT NOT NULL DEFAULT '',
    author_id      INTEGER,
    author_name    TEXT,
    created_at     TEXT NOT NULL,            -- UTC ISO
    status         TEXT NOT NULL DEFAULT 'open',   -- open | done | deleted
    done_at        TEXT,
    done_by_id     INTEGER,
    done_by_name   TEXT,
    voice_file_id  TEXT,
    from_voice     INTEGER NOT NULL DEFAULT 0,
    priority       INTEGER NOT NULL DEFAULT 2      -- 1 high | 2 normal | 3 low
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, done_at);

CREATE TABLE IF NOT EXISTS photos (
    id        INTEGER PRIMARY KEY,
    task_id   INTEGER NOT NULL REFERENCES tasks(id),
    file_id   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS photos_task ON photos(task_id);

-- People who can use the bot (approved from Telegram by an admin, or with the CLI).
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,           -- Telegram user id
    name        TEXT,
    username    TEXT,
    status      TEXT NOT NULL,                 -- allowed | pending | blocked
    is_admin    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);

-- The to-do list message currently open in each chat, kept up to date silently.
CREATE TABLE IF NOT EXISTS views (
    chat_id     INTEGER PRIMARY KEY,
    message_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL,     -- list | detail | done
    page        INTEGER NOT NULL DEFAULT 0
);
"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


@dataclass
class Task:
    id: int
    title: str
    notes: str
    author_id: int | None
    author_name: str | None
    created_at: datetime
    status: str
    done_at: datetime | None
    done_by_id: int | None
    done_by_name: str | None
    voice_file_id: str | None
    from_voice: bool
    priority: int = 2
    photo_count: int = 0

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Task:
        return cls(
            r["id"], r["title"], r["notes"], r["author_id"], r["author_name"], _dt(r["created_at"]),
            r["status"], _dt(r["done_at"]), r["done_by_id"], r["done_by_name"], r["voice_file_id"],
            bool(r["from_voice"]), r["priority"], r["photo_count"] if "photo_count" in r.keys() else 0,
        )


_SELECT = "SELECT t.*, (SELECT COUNT(*) FROM photos p WHERE p.task_id = t.id) AS photo_count FROM tasks t"


@dataclass
class View:
    chat_id: int
    message_id: int
    kind: str
    page: int


@dataclass
class User:
    id: int
    name: str | None
    username: str | None
    status: str
    is_admin: bool

    @property
    def label(self) -> str:
        n = self.name or str(self.id)
        return f"{n} (@{self.username})" if self.username else n

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> User:
        return cls(r["id"], r["name"], r["username"], r["status"], bool(r["is_admin"]))


class DB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Bring databases created by older versions up to date."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        if "priority" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 2")

    # ------------------------------------------------------------------ tasks
    def add_task(self, title: str, notes: str = "", author_id: int | None = None,
                 author_name: str | None = None, voice_file_id: str | None = None,
                 from_voice: bool = False, at: datetime | None = None, priority: int = 2) -> Task:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO tasks(title, notes, author_id, author_name, created_at, voice_file_id, from_voice, priority) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (title, notes, author_id, author_name, _iso(at or now_utc()), voice_file_id, int(from_voice), priority),
            )
        return self.task(cur.lastrowid)

    def task(self, task_id: int) -> Task | None:
        r = self.conn.execute(f"{_SELECT} WHERE t.id = ?", (task_id,)).fetchone()
        return Task.from_row(r) if r else None

    def open_tasks(self) -> list[Task]:
        """Most urgent first; within the same priority, what has been waiting longest."""
        return [Task.from_row(r) for r in self.conn.execute(
            f"{_SELECT} WHERE t.status = 'open' ORDER BY t.priority, t.id")]

    def set_priority(self, task_id: int, priority: int) -> Task | None:
        if priority not in (1, 2, 3):
            raise ValueError(priority)
        with self.conn:
            n = self.conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (priority, task_id)).rowcount
        return self.task(task_id) if n else None

    def done_since(self, since: datetime) -> list[Task]:
        """Most recently done first."""
        rows = self.conn.execute(
            f"{_SELECT} WHERE t.status = 'done' AND t.done_at >= ? ORDER BY t.done_at DESC", (_iso(since),))
        return [Task.from_row(r) for r in rows]

    def last_added_by(self, author_id: int, within: timedelta) -> Task | None:
        r = self.conn.execute(
            f"{_SELECT} WHERE t.author_id = ? AND t.status = 'open' AND t.created_at >= ? ORDER BY t.id DESC LIMIT 1",
            (author_id, _iso(now_utc() - within))).fetchone()
        return Task.from_row(r) if r else None

    def update_text(self, task_id: int, title: str | None = None, append_notes: str | None = None) -> Task | None:
        t = self.task(task_id)
        if t is None:
            return None
        new_title = title if title is not None else t.title
        notes = t.notes
        if append_notes:
            notes = f"{notes}\n{append_notes}".strip() if notes else append_notes.strip()
        with self.conn:
            self.conn.execute("UPDATE tasks SET title = ?, notes = ? WHERE id = ?", (new_title, notes, task_id))
        return self.task(task_id)

    def set_voice(self, task_id: int, voice_file_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE tasks SET voice_file_id = ? WHERE id = ?", (voice_file_id, task_id))

    def mark_done(self, task_id: int, user_id: int, user_name: str | None, at: datetime | None = None) -> Task | None:
        """Returns the task if it was open and is now done, None otherwise."""
        with self.conn:
            n = self.conn.execute(
                "UPDATE tasks SET status = 'done', done_at = ?, done_by_id = ?, done_by_name = ? "
                "WHERE id = ? AND status = 'open'", (_iso(at or now_utc()), user_id, user_name, task_id)).rowcount
        return self.task(task_id) if n else None

    def reopen(self, task_id: int) -> Task | None:
        with self.conn:
            n = self.conn.execute(
                "UPDATE tasks SET status = 'open', done_at = NULL, done_by_id = NULL, done_by_name = NULL "
                "WHERE id = ? AND status = 'done'", (task_id,)).rowcount
        return self.task(task_id) if n else None

    def last_done_by(self, user_id: int, within: timedelta) -> Task | None:
        """The task this user most recently marked done (for the undo button)."""
        r = self.conn.execute(
            f"{_SELECT} WHERE t.status = 'done' AND t.done_by_id = ? AND t.done_at >= ? ORDER BY t.done_at DESC LIMIT 1",
            (user_id, _iso(now_utc() - within))).fetchone()
        return Task.from_row(r) if r else None

    def delete(self, task_id: int) -> Task | None:
        with self.conn:
            n = self.conn.execute(
                "UPDATE tasks SET status = 'deleted' WHERE id = ? AND status != 'deleted'", (task_id,)).rowcount
        return self.task(task_id) if n else None

    def restore(self, task_id: int) -> Task | None:
        with self.conn:
            n = self.conn.execute(
                "UPDATE tasks SET status = 'open' WHERE id = ? AND status = 'deleted'", (task_id,)).rowcount
        return self.task(task_id) if n else None

    # ----------------------------------------------------------------- photos
    def add_photo(self, task_id: int, file_id: str) -> int:
        with self.conn:
            return self.conn.execute("INSERT INTO photos(task_id, file_id) VALUES (?, ?)", (task_id, file_id)).lastrowid

    def remove_photo(self, photo_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))

    def photos(self, task_id: int) -> list[str]:
        return [r["file_id"] for r in self.conn.execute(
            "SELECT file_id FROM photos WHERE task_id = ? ORDER BY id", (task_id,))]

    # ------------------------------------------------------------------ users
    def user(self, user_id: int) -> User | None:
        r = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User.from_row(r) if r else None

    def users(self) -> list[User]:
        order = "CASE status WHEN 'pending' THEN 0 WHEN 'allowed' THEN 1 ELSE 2 END, is_admin DESC, name"
        return [User.from_row(r) for r in self.conn.execute(f"SELECT * FROM users ORDER BY {order}")]

    def admins(self) -> list[User]:
        return [User.from_row(r) for r in self.conn.execute(
            "SELECT * FROM users WHERE is_admin = 1 AND status = 'allowed'")]

    def save_user(self, user_id: int, status: str, name: str | None = None, username: str | None = None,
                  is_admin: bool | None = None) -> User:
        """Insert or update; name/username/is_admin are kept when not given."""
        old = self.user(user_id)
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO users(id, name, username, status, is_admin, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, name if name is not None else (old.name if old else None),
                 username if username is not None else (old.username if old else None), status,
                 int(is_admin if is_admin is not None else (old.is_admin if old else False)), _iso(now_utc())))
        return self.user(user_id)

    def delete_user(self, user_id: int) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount > 0

    # ------------------------------------------------------------------ views
    def set_view(self, chat_id: int, message_id: int, kind: str, page: int = 0) -> int | None:
        """Records the view message of a chat; returns the previous message id if it was a different one."""
        old = self.view(chat_id)
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO views VALUES (?, ?, ?, ?)", (chat_id, message_id, kind, page))
        return old.message_id if old and old.message_id != message_id else None

    def view(self, chat_id: int) -> View | None:
        r = self.conn.execute("SELECT * FROM views WHERE chat_id = ?", (chat_id,)).fetchone()
        return View(r["chat_id"], r["message_id"], r["kind"], r["page"]) if r else None

    def list_views(self) -> list[View]:
        return [View(r["chat_id"], r["message_id"], r["kind"], r["page"])
                for r in self.conn.execute("SELECT * FROM views WHERE kind = 'list'")]

    def forget_view(self, chat_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM views WHERE chat_id = ?", (chat_id,))
