from __future__ import annotations

import logging
from typing import Any

from .db import Database


def run_offline_qa(db: Database, creator_id: str, logger: logging.Logger) -> dict[str, Any]:
    result: dict[str, Any] = {"creator_id": creator_id, "checks": {}, "passed": True}
    fk = db.conn.execute("PRAGMA foreign_key_check").fetchall()
    result["checks"]["foreign_key"] = len(fk) == 0

    duplicate_notes = db.conn.execute(
        "SELECT note_id, COUNT(*) AS c FROM notes GROUP BY note_id HAVING c > 1"
    ).fetchall()
    result["checks"]["duplicate_note_id"] = len(duplicate_notes) == 0

    duplicate_comment_rank = db.conn.execute(
        """
        SELECT note_id, captured_at, comment_rank, COUNT(*) AS c
        FROM top_comments
        GROUP BY note_id, captured_at, comment_rank
        HAVING c > 1
        """
    ).fetchall()
    result["checks"]["duplicate_comment_rank"] = len(duplicate_comment_rank) == 0

    negative_counts = db.conn.execute(
        """
        SELECT note_id FROM note_metrics_snapshots
        WHERE COALESCE(likes_value, 0) < 0
           OR COALESCE(collects_value, 0) < 0
           OR COALESCE(comments_value, 0) < 0
           OR COALESCE(shares_value, 0) < 0
        """
    ).fetchall()
    result["checks"]["negative_counts"] = len(negative_counts) == 0

    invalid_ranks = db.conn.execute(
        "SELECT id FROM top_comments WHERE comment_rank < 1 OR comment_rank > 3"
    ).fetchall()
    result["checks"]["comment_rank_range"] = len(invalid_ranks) == 0

    current = db.current_notes(creator_id)
    ids = [row["note_id"] for row in current]
    result["checks"]["current_note_unique"] = len(ids) == len(set(ids))
    result["metrics"] = _metric_quality(current)
    result["comments"] = _comment_quality(db, current)
    result["passed"] = all(bool(v) for v in result["checks"].values())
    logger.info("OFFLINE_QA passed=%s checks=%s metrics=%s comments=%s", result["passed"], result["checks"], result["metrics"], result["comments"])
    return result


def _metric_quality(rows: list[Any]) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for key in ["likes", "collects", "comments", "shares"]:
        output[key] = {"exact": 0, "approx": 0, "missing": 0}
        for row in rows:
            exact = row[f"{key}_is_exact"]
            value = row[f"{key}_value"]
            raw = row[f"{key}_raw"]
            if value is None and raw is None:
                output[key]["missing"] += 1
            elif exact:
                output[key]["exact"] += 1
            else:
                output[key]["approx"] += 1
    return output


def _comment_quality(db: Database, rows: list[Any]) -> dict[str, int]:
    output = {"three_complete": 0, "less_than_three": 0, "collection_exception": 0}
    for row in rows:
        comments = db.comments_for_note(row["note_id"])
        if len(comments) >= 3:
            output["three_complete"] += 1
        elif row["comments_value"] is not None and row["comments_value"] < 3:
            output["less_than_three"] += 1
        else:
            output["collection_exception"] += 1
    return output

