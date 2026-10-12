from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .commands import Draft, VoteError


@dataclass(frozen=True)
class Poll:
    id: int
    room: str
    umo: str
    creator: str
    title: str
    options: tuple[str, ...]
    max_choices: int
    public: bool
    deadline: float
    status: str
    participants: int
    counts: tuple[int, ...]


class VoteStore:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room TEXT NOT NULL,
                umo TEXT NOT NULL,
                creator TEXT NOT NULL,
                title TEXT NOT NULL,
                options TEXT NOT NULL,
                max_choices INTEGER NOT NULL,
                public INTEGER NOT NULL,
                deadline REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                notified INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS polls_room_status ON polls(room, status);
            CREATE INDEX IF NOT EXISTS polls_pending ON polls(notified, status);
            CREATE TABLE IF NOT EXISTS votes (
                poll_id INTEGER NOT NULL REFERENCES polls(id),
                user_id TEXT NOT NULL,
                choices TEXT NOT NULL,
                PRIMARY KEY (poll_id, user_id)
            );
        """)

    def close(self) -> None:
        self.db.close()

    def _poll(self, row: sqlite3.Row) -> Poll:
        options = tuple(json.loads(row["options"]))
        counts = [0] * len(options)
        votes = self.db.execute(
            "SELECT choices FROM votes WHERE poll_id = ?", (row["id"],)
        ).fetchall()
        for vote in votes:
            for choice in json.loads(vote["choices"]):
                counts[choice - 1] += 1
        return Poll(
            row["id"], row["room"], row["umo"], row["creator"], row["title"],
            options, row["max_choices"], bool(row["public"]), row["deadline"],
            row["status"], len(votes), tuple(counts),
        )

    def _row(self, room: str, poll_id: int) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM polls WHERE id = ? AND room = ?", (poll_id, room)
        ).fetchone()
        if row is None:
            raise VoteError("本群不存在该投票，请检查投票 ID。")
        return row

    def create(self, room: str, umo: str, creator: str, draft: Draft, now: float) -> Poll:
        with self.db:
            cursor = self.db.execute(
                """INSERT INTO polls
                   (room, umo, creator, title, options, max_choices, public, deadline)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (room, umo, creator, draft.title, json.dumps(draft.options, ensure_ascii=False),
                 draft.max_choices, draft.public, now + draft.duration),
            )
        return self._poll(self._row(room, cursor.lastrowid))

    def expire_due(self, now: float) -> None:
        with self.db:
            self.db.execute(
                "UPDATE polls SET status = 'ended' WHERE status = 'active' AND deadline <= ?",
                (now,),
            )

    def get(self, room: str, poll_id: int, now: float) -> Poll:
        self.expire_due(now)
        return self._poll(self._row(room, poll_id))

    def active(self, room: str, now: float) -> list[Poll]:
        self.expire_due(now)
        rows = self.db.execute(
            "SELECT * FROM polls WHERE room = ? AND status = 'active' ORDER BY id", (room,)
        ).fetchall()
        return [self._poll(row) for row in rows]

    def cast(self, room: str, poll_id: int, user_id: str, choices: tuple[int, ...],
             now: float, *, change: bool = False) -> Poll:
        self.expire_due(now)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._row(room, poll_id)
            if row["status"] != "active" or now >= row["deadline"]:
                raise VoteError("该投票已结束或已取消，不能投票或改票。")
            existing = self.db.execute(
                "SELECT 1 FROM votes WHERE poll_id = ? AND user_id = ?", (poll_id, user_id)
            ).fetchone()
            if change and existing is None:
                raise VoteError(f"你还未投过票，请使用 /投票 选择 {poll_id} 选项编号。")
            if not change and existing is not None:
                raise VoteError(f"你已经投过票，请使用 /投票 改票 {poll_id} 选项编号。")
            count = len(json.loads(row["options"]))
            if not choices or any(choice < 1 or choice > count for choice in choices):
                raise VoteError(f"选项编号须为 1 至 {count}。")
            if len(set(choices)) != len(choices):
                raise VoteError("同一个选项不能重复选择。")
            if len(choices) > row["max_choices"]:
                raise VoteError(f"该投票最多选择 {row['max_choices']} 个选项。")
            self.db.execute(
                """INSERT INTO votes(poll_id, user_id, choices) VALUES (?, ?, ?)
                   ON CONFLICT(poll_id, user_id) DO UPDATE SET choices = excluded.choices""",
                (poll_id, user_id, json.dumps(sorted(choices))),
            )
        return self._poll(self._row(room, poll_id))

    def finish(self, room: str, poll_id: int, now: float, *, cancel: bool = False) -> Poll:
        self.expire_due(now)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._row(room, poll_id)
            if row["status"] != "active":
                raise VoteError("该投票已结束或已取消。")
            self.db.execute(
                "UPDATE polls SET status = ? WHERE id = ?",
                ("cancelled" if cancel else "ended", poll_id),
            )
        return self._poll(self._row(room, poll_id))

    def pending(self, now: float) -> list[Poll]:
        self.expire_due(now)
        rows = self.db.execute(
            "SELECT * FROM polls WHERE status != 'active' AND notified = 0 ORDER BY id"
        ).fetchall()
        return [self._poll(row) for row in rows]

    def mark_notified(self, poll_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE polls SET notified = 1 WHERE id = ?", (poll_id,))
