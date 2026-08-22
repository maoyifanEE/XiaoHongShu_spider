from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


VALIDATION_FIELDS = [
    "title",
    "body",
    "note_type",
    "publish_time",
    "like_count",
    "collect_count",
    "comment_count",
    "tags",
]

SENSITIVE_ARTIFACT_TERMS = [
    "cookie",
    "token",
    "authorization",
    "bearer",
    "xsec",
    "session",
    "initial_state",
]

ALLOWED_SOURCE_LABELS = [
    "DETAIL_INITIAL_STATE",
]

CSV_HEADERS = [
    "note_id",
    "title",
    "note_type",
    "publish_time",
    "like_count",
    "collect_count",
    "comment_count",
    "tags",
    "detail_ready",
    "exportable",
    "source_title",
    "source_body",
    "source_metrics",
]


def write_live_validation_artifact(
    base_dir: Path,
    *,
    run_id: str,
    status: str,
    login_status: str,
    notes_discovered: int,
    collection: Any,
) -> Path:
    artifact_dir = base_dir / "validation" / "live_runs" / run_id
    notes = list(getattr(collection, "validation_notes", []) or [])
    summary = {
        "run_id": run_id,
        "status": status,
        "login_status": login_status,
        "notes_discovered": notes_discovered,
        "attempted": len(getattr(collection, "attempted_ids", []) or []),
        "target_verified": len(getattr(collection, "verified_ids", []) or []),
        "exportable": len(getattr(collection, "exportable_ids", []) or []),
        "navigation_failed": len(getattr(collection, "navigation_failed_ids", []) or []),
        "detail_not_ready": _count_detail_not_ready(notes),
        "safe_stop_reason": getattr(collection, "safe_stop_reason", None),
        "fields": _field_summary(notes),
    }
    csv_rows = [_csv_row(note) for note in notes]
    _assert_artifact_safe(summary, csv_rows)

    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (artifact_dir / "field_validation.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(csv_rows)
    return artifact_dir


def build_validation_note(note_id: str, note: dict[str, Any], *, detail_ready: bool, exportable: bool) -> dict[str, Any]:
    sources = note.get("field_sources") or {}
    return {
        "note_id": note_id,
        "title": note.get("title"),
        "body_present": _present(note.get("body")),
        "note_type": note.get("note_type"),
        "publish_time": note.get("publish_time"),
        "like_count": note.get("likes_value"),
        "collect_count": note.get("collects_value"),
        "comment_count": note.get("comments_value"),
        "tags": note.get("hashtags") or [],
        "detail_ready": bool(detail_ready),
        "exportable": bool(exportable),
        "field_sources": {field: str(sources.get(field) or "MISSING") for field in VALIDATION_FIELDS},
    }


def build_detail_not_ready_validation_note(note_id: str) -> dict[str, Any]:
    return {
        "note_id": note_id,
        "title": None,
        "body_present": False,
        "note_type": None,
        "publish_time": None,
        "like_count": None,
        "collect_count": None,
        "comment_count": None,
        "tags": [],
        "detail_ready": False,
        "exportable": False,
        "field_sources": {field: "MISSING" for field in VALIDATION_FIELDS},
    }


def _field_summary(notes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for field in VALIDATION_FIELDS:
        sources: dict[str, int] = {}
        present_count = 0
        for note in notes:
            present = note.get("body_present") if field == "body" else _present(note.get(field))
            if present:
                present_count += 1
            source = str((note.get("field_sources") or {}).get(field) or "MISSING")
            sources[source] = sources.get(source, 0) + 1
        summary[field] = {"value_present": present_count, "source": sources}
    return summary


def _csv_row(note: dict[str, Any]) -> dict[str, Any]:
    sources = note.get("field_sources") or {}
    metric_sources = {
        field: sources.get(field) or "MISSING"
        for field in ["like_count", "collect_count", "comment_count"]
    }
    return {
        "note_id": note.get("note_id"),
        "title": note.get("title"),
        "note_type": note.get("note_type"),
        "publish_time": note.get("publish_time"),
        "like_count": note.get("like_count"),
        "collect_count": note.get("collect_count"),
        "comment_count": note.get("comment_count"),
        "tags": " ".join(str(tag) for tag in (note.get("tags") or []) if tag),
        "detail_ready": bool(note.get("detail_ready")),
        "exportable": bool(note.get("exportable")),
        "source_title": sources.get("title") or "MISSING",
        "source_body": sources.get("body") or "MISSING",
        "source_metrics": json.dumps(metric_sources, ensure_ascii=False, sort_keys=True),
    }


def _count_detail_not_ready(notes: list[dict[str, Any]]) -> int:
    return sum(1 for note in notes if not note.get("detail_ready"))


def _assert_artifact_safe(summary: dict[str, Any], csv_rows: list[dict[str, Any]]) -> None:
    payload = json.dumps({"summary": summary, "rows": csv_rows}, ensure_ascii=False, sort_keys=True, default=str)
    lowered = payload.lower()
    for label in ALLOWED_SOURCE_LABELS:
        lowered = lowered.replace(label.lower(), "")
    matches = [term for term in SENSITIVE_ARTIFACT_TERMS if term in lowered]
    if matches:
        raise ValueError(f"validation artifact contains sensitive terms: {sorted(set(matches))}")


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True
