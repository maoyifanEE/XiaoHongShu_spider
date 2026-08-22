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

    negative_counts = db.conn.execute(
        """
        SELECT note_id FROM note_metrics_snapshots
        WHERE COALESCE(likes_value, 0) < 0
           OR COALESCE(collects_value, 0) < 0
           OR COALESCE(comments_value, 0) < 0
        """
    ).fetchall()
    result["checks"]["negative_counts"] = len(negative_counts) == 0

    current = db.current_notes(creator_id)
    ids = [row["note_id"] for row in current]
    result["checks"]["current_note_unique"] = len(ids) == len(set(ids))
    result["metrics"] = _metric_quality(current)
    result["passed"] = all(bool(v) for v in result["checks"].values())
    logger.info("OFFLINE_QA passed=%s checks=%s metrics=%s", result["passed"], result["checks"], result["metrics"])
    return result


def _metric_quality(rows: list[Any]) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for key in ["likes", "collects", "comments"]:
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
