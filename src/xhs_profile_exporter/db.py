from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .time_utils import now_iso
from .utils import canonical_note_url, sanitize_json, stable_hash


SCHEMA_VERSION = 1


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def backup_before_migration(self, backup_dir: Path) -> Path | None:
        if not self.path.exists():
            return None
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"xhs_data_before_migration_{now_iso().replace(':', '').replace('+', '_')}.sqlite3"
        shutil.copy2(self.path, target)
        return target

    def migrate(self) -> None:
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current >= SCHEMA_VERSION:
            return
        with self.transaction():
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS crawl_runs (
                    run_id TEXT PRIMARY KEY,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    creator_id TEXT,
                    creator_name TEXT,
                    program_version TEXT NOT NULL,
                    browser_version TEXT,
                    login_status TEXT,
                    status TEXT NOT NULL,
                    notes_discovered INTEGER DEFAULT 0,
                    notes_completed INTEGER DEFAULT 0,
                    notes_failed INTEGER DEFAULT 0,
                    safe_stop_reason TEXT,
                    risk_detected INTEGER DEFAULT 0,
                    notes TEXT
                );

                CREATE TABLE IF NOT EXISTS creator_profile_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    creator_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    nickname TEXT,
                    xhs_id TEXT,
                    canonical_url TEXT,
                    avatar_url TEXT,
                    ip_location TEXT,
                    bio TEXT,
                    profile_tags TEXT,
                    identity_tags TEXT,
                    following_value INTEGER,
                    following_raw TEXT,
                    following_is_exact INTEGER,
                    followers_value INTEGER,
                    followers_raw TEXT,
                    followers_is_exact INTEGER,
                    total_interactions_value INTEGER,
                    total_interactions_raw TEXT,
                    total_interactions_is_exact INTEGER,
                    gender TEXT,
                    raw_json TEXT,
                    source TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notes (
                    note_id TEXT PRIMARY KEY,
                    creator_id TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    is_pinned INTEGER,
                    note_type TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    status_note TEXT
                );

                CREATE TABLE IF NOT EXISTS note_content_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id TEXT NOT NULL REFERENCES notes(note_id) ON DELETE CASCADE,
                    captured_at TEXT NOT NULL,
                    title TEXT,
                    body TEXT,
                    hashtags TEXT,
                    publish_time TEXT,
                    publish_time_raw TEXT,
                    updated_time TEXT,
                    updated_time_raw TEXT,
                    content_hash TEXT NOT NULL,
                    raw_json TEXT,
                    source TEXT NOT NULL,
                    UNIQUE(note_id, content_hash)
                );

                CREATE TABLE IF NOT EXISTS note_metrics_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id TEXT NOT NULL REFERENCES notes(note_id) ON DELETE CASCADE,
                    captured_at TEXT NOT NULL,
                    likes_value INTEGER,
                    likes_raw TEXT,
                    likes_is_exact INTEGER,
                    collects_value INTEGER,
                    collects_raw TEXT,
                    collects_is_exact INTEGER,
                    comments_value INTEGER,
                    comments_raw TEXT,
                    comments_is_exact INTEGER,
                    shares_value INTEGER,
                    shares_raw TEXT,
                    shares_is_exact INTEGER,
                    source TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS top_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id TEXT NOT NULL REFERENCES notes(note_id) ON DELETE CASCADE,
                    captured_at TEXT NOT NULL,
                    sorting_mode TEXT NOT NULL,
                    comment_rank INTEGER NOT NULL,
                    comment_id TEXT,
                    author_name TEXT,
                    body TEXT,
                    likes_value INTEGER,
                    likes_raw TEXT,
                    likes_is_exact INTEGER,
                    is_creator INTEGER,
                    is_pinned INTEGER,
                    source TEXT NOT NULL,
                    UNIQUE(note_id, captured_at, comment_rank)
                );

                CREATE TABLE IF NOT EXISTS raw_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    raw_hash TEXT NOT NULL,
                    UNIQUE(raw_hash)
                );

                PRAGMA user_version = 1;
                """
            )

    def start_run(self, run_id: str, creator_id: str, creator_name: str, version: str) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO crawl_runs
            (run_id, start_time, creator_id, creator_name, program_version, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, now_iso(), creator_id, creator_name, version, "RUNNING"),
        )
        self.conn.commit()

    def finish_run(self, run_id: str, status: str, **fields: Any) -> None:
        allowed = {
            "browser_version", "login_status", "notes_discovered", "notes_completed",
            "notes_failed", "safe_stop_reason", "risk_detected", "notes",
        }
        sets = ["end_time = ?", "status = ?"]
        values: list[Any] = [now_iso(), status]
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            values.append(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value)
        values.append(run_id)
        self.conn.execute(f"UPDATE crawl_runs SET {', '.join(sets)} WHERE run_id = ?", values)
        self.conn.commit()

    def save_profile_snapshot(self, creator_id: str, snapshot: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO creator_profile_snapshots
            (creator_id, captured_at, nickname, xhs_id, canonical_url, avatar_url, ip_location, bio,
             profile_tags, identity_tags, following_value, following_raw, following_is_exact,
             followers_value, followers_raw, followers_is_exact, total_interactions_value,
             total_interactions_raw, total_interactions_is_exact, gender, raw_json, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                creator_id,
                snapshot.get("captured_at") or now_iso(),
                snapshot.get("nickname"),
                snapshot.get("xhs_id"),
                snapshot.get("canonical_url"),
                snapshot.get("avatar_url"),
                snapshot.get("ip_location"),
                snapshot.get("bio"),
                json.dumps(snapshot.get("profile_tags"), ensure_ascii=False),
                json.dumps(snapshot.get("identity_tags"), ensure_ascii=False),
                snapshot.get("following_value"),
                snapshot.get("following_raw"),
                _bool(snapshot.get("following_is_exact")),
                snapshot.get("followers_value"),
                snapshot.get("followers_raw"),
                _bool(snapshot.get("followers_is_exact")),
                snapshot.get("total_interactions_value"),
                snapshot.get("total_interactions_raw"),
                _bool(snapshot.get("total_interactions_is_exact")),
                snapshot.get("gender"),
                json.dumps(sanitize_json(snapshot.get("raw_json")), ensure_ascii=False),
                snapshot.get("source", "dom"),
            ),
        )
        self.conn.commit()

    def upsert_note(self, creator_id: str, note: dict[str, Any]) -> None:
        note_id = note["note_id"]
        captured_at = note.get("captured_at") or now_iso()
        canonical_url = note.get("canonical_url") or canonical_note_url(note_id)
        with self.transaction():
            self.conn.execute(
                """
                INSERT INTO notes (note_id, creator_id, canonical_url, is_pinned, note_type, first_seen_at, last_seen_at, status, status_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(note_id) DO UPDATE SET
                    creator_id=excluded.creator_id,
                    canonical_url=excluded.canonical_url,
                    is_pinned=excluded.is_pinned,
                    note_type=COALESCE(excluded.note_type, notes.note_type),
                    last_seen_at=excluded.last_seen_at,
                    status=excluded.status,
                    status_note=excluded.status_note
                """,
                (
                    note_id,
                    creator_id,
                    canonical_url,
                    _bool(note.get("is_pinned")),
                    note.get("note_type"),
                    captured_at,
                    captured_at,
                    note.get("status", "OK"),
                    note.get("status_note"),
                ),
            )
            content_hash = stable_hash(
                {
                    "title": note.get("title"),
                    "body": note.get("body"),
                    "hashtags": note.get("hashtags"),
                    "publish_time": note.get("publish_time"),
                    "updated_time": note.get("updated_time"),
                }
            )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO note_content_versions
                (note_id, captured_at, title, body, hashtags, publish_time, publish_time_raw, updated_time,
                 updated_time_raw, content_hash, raw_json, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note_id,
                    captured_at,
                    note.get("title"),
                    note.get("body"),
                    json.dumps(note.get("hashtags"), ensure_ascii=False),
                    note.get("publish_time"),
                    note.get("publish_time_raw"),
                    note.get("updated_time"),
                    note.get("updated_time_raw"),
                    content_hash,
                    json.dumps(sanitize_json(note.get("raw_json")), ensure_ascii=False),
                    note.get("source", "dom"),
                ),
            )
            self.conn.execute(
                """
                INSERT INTO note_metrics_snapshots
                (note_id, captured_at, likes_value, likes_raw, likes_is_exact, collects_value,
                 collects_raw, collects_is_exact, comments_value, comments_raw, comments_is_exact,
                 shares_value, shares_raw, shares_is_exact, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note_id,
                    captured_at,
                    note.get("likes_value"),
                    note.get("likes_raw"),
                    _bool(note.get("likes_is_exact")),
                    note.get("collects_value"),
                    note.get("collects_raw"),
                    _bool(note.get("collects_is_exact")),
                    note.get("comments_value"),
                    note.get("comments_raw"),
                    _bool(note.get("comments_is_exact")),
                    note.get("shares_value"),
                    note.get("shares_raw"),
                    _bool(note.get("shares_is_exact")),
                    note.get("source", "dom"),
                ),
            )
            for comment in note.get("top_comments", []):
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO top_comments
                    (note_id, captured_at, sorting_mode, comment_rank, comment_id, author_name, body,
                     likes_value, likes_raw, likes_is_exact, is_creator, is_pinned, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        note_id,
                        captured_at,
                        comment.get("sorting_mode", "default_ui_order"),
                        comment.get("rank"),
                        comment.get("comment_id"),
                        comment.get("author_name"),
                        comment.get("body"),
                        comment.get("likes_value"),
                        comment.get("likes_raw"),
                        _bool(comment.get("likes_is_exact")),
                        _bool(comment.get("is_creator")),
                        _bool(comment.get("is_pinned")),
                        comment.get("source", "dom"),
                    ),
                )

    def save_raw(self, run_id: str, entity_type: str, entity_id: str, source: str, raw: Any) -> None:
        payload = json.dumps(sanitize_json(raw), ensure_ascii=False, sort_keys=True, default=str)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO raw_records
            (run_id, entity_type, entity_id, captured_at, source, raw_json, raw_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, entity_type, entity_id, now_iso(), source, payload, stable_hash(payload)),
        )
        self.conn.commit()

    def current_notes(self, creator_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            WITH latest_content AS (
                SELECT ncv.* FROM note_content_versions ncv
                JOIN (
                    SELECT note_id, MAX(captured_at) AS captured_at
                    FROM note_content_versions GROUP BY note_id
                ) latest USING(note_id, captured_at)
            ),
            latest_metrics AS (
                SELECT nms.* FROM note_metrics_snapshots nms
                JOIN (
                    SELECT note_id, MAX(captured_at) AS captured_at
                    FROM note_metrics_snapshots GROUP BY note_id
                ) latest USING(note_id, captured_at)
            )
            SELECT notes.*, latest_content.title, latest_content.body, latest_content.hashtags,
                   latest_content.publish_time, latest_content.publish_time_raw,
                   latest_content.updated_time, latest_content.updated_time_raw,
                   latest_metrics.likes_value, latest_metrics.likes_raw, latest_metrics.likes_is_exact,
                   latest_metrics.collects_value, latest_metrics.collects_raw, latest_metrics.collects_is_exact,
                   latest_metrics.comments_value, latest_metrics.comments_raw, latest_metrics.comments_is_exact,
                   latest_metrics.shares_value, latest_metrics.shares_raw, latest_metrics.shares_is_exact,
                   latest_metrics.captured_at AS metrics_captured_at
            FROM notes
            LEFT JOIN latest_content ON latest_content.note_id = notes.note_id
            LEFT JOIN latest_metrics ON latest_metrics.note_id = notes.note_id
            WHERE notes.creator_id = ? AND notes.status IN ('OK', 'PARSE_PARTIAL')
            ORDER BY notes.first_seen_at ASC, notes.note_id ASC
            """,
            (creator_id,),
        ).fetchall()

    def latest_profile(self, creator_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM creator_profile_snapshots
            WHERE creator_id = ?
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
            """,
            (creator_id,),
        ).fetchone()

    def comments_for_note(self, note_id: str, captured_at: str | None = None) -> list[sqlite3.Row]:
        if captured_at:
            return self.conn.execute(
                """
                SELECT * FROM top_comments
                WHERE note_id = ? AND captured_at = ?
                ORDER BY comment_rank
                """,
                (note_id, captured_at),
            ).fetchall()
        return self.conn.execute(
            """
            WITH latest AS (
                SELECT MAX(captured_at) AS captured_at FROM top_comments WHERE note_id = ?
            )
            SELECT * FROM top_comments
            WHERE note_id = ? AND captured_at = (SELECT captured_at FROM latest)
            ORDER BY comment_rank
            """,
            (note_id, note_id),
        ).fetchall()


def _bool(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0
