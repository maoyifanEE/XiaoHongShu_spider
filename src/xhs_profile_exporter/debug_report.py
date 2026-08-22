from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEBUG_FIELDS = [
    "title",
    "body",
    "note_type",
    "publish_time",
    "like_count",
    "collect_count",
    "comment_count",
    "tags",
]

DEBUG_SENSITIVE_TERMS = [
    "cookie",
    "token",
    "authorization",
    "bearer",
    "xsec",
    "session",
    "<html",
    "</html",
    "response dump",
    "initial_state",
]

ALLOWED_SOURCE_LABELS = ["DETAIL_INITIAL_STATE"]

FIELD_DOM_SELECTORS = {
    "title": ["#detail-title"],
    "body": ["#detail-desc"],
    "note_type": [],
    "publish_time": [],
    "like_count": [".engage-bar .like-wrapper"],
    "collect_count": [".engage-bar .collect-wrapper"],
    "comment_count": [".engage-bar .chat-wrapper"],
    "tags": ['#detail-desc a[href*="search"]', 'a[href*="/search"]'],
}


def build_extraction_report(
    *,
    run_id: str,
    note_id: str,
    detail_ready: bool,
    note: dict[str, Any],
    dom_summary: dict[str, Any],
) -> dict[str, Any]:
    sources = note.get("field_sources") or {}
    normalized_dom = _normalize_dom_summary(dom_summary)
    dom_previews = _field_dom_previews(normalized_dom)
    fields = {
        "title": _field(note.get("title"), sources.get("title"), dom_previews.get("title")),
        "body": _field(note.get("body"), sources.get("body"), dom_previews.get("body")),
        "note_type": _field(note.get("note_type"), sources.get("note_type"), dom_previews.get("note_type")),
        "publish_time": _field(note.get("publish_time"), sources.get("publish_time"), dom_previews.get("publish_time")),
        "like_count": _field(note.get("likes_value"), sources.get("like_count"), dom_previews.get("like_count")),
        "collect_count": _field(note.get("collects_value"), sources.get("collect_count"), dom_previews.get("collect_count")),
        "comment_count": _field(note.get("comments_value"), sources.get("comment_count"), dom_previews.get("comment_count")),
        "tags": _field(note.get("hashtags") or [], sources.get("tags"), dom_previews.get("tags")),
    }
    return {
        "run_id": run_id,
        "note_id": note_id,
        "detail_ready": bool(detail_ready),
        "status": note.get("status"),
        "fields": fields,
        "dom_summary": normalized_dom,
    }


def write_extraction_report(base_dir: Path, report: dict[str, Any]) -> Path:
    _assert_debug_report_safe(report)
    output_dir = base_dir / "debug" / "live_extract" / str(report["run_id"])
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "extraction_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def _field(value: Any, source: Any, dom_evidence_preview: str | None) -> dict[str, Any]:
    source_text = str(source or "MISSING")
    present = _present(value)
    output = {
        "value": value if present else None,
        "source": source_text if present else "MISSING",
        "confidence": _confidence(source_text if present else "MISSING"),
        "dom_evidence_preview": dom_evidence_preview,
    }
    if not present:
        output["reason"] = "not_observed"
    return output


def _confidence(source: str) -> str:
    if source == "DOM_EXACT":
        return "high"
    if "DETAIL_INITIAL_STATE" in source:
        return "high"
    if "PAGE_RESPONSE" in source:
        return "medium"
    return "missing"


def _normalize_dom_summary(dom_summary: dict[str, Any]) -> dict[str, Any]:
    selectors = [str(item) for item in dom_summary.get("selectors_checked") or []]
    nodes = []
    for item in dom_summary.get("matched_nodes") or []:
        if not isinstance(item, dict):
            continue
        nodes.append(
            {
                "selector": str(item.get("selector") or ""),
                "text_preview": str(item.get("text_preview") or "")[:200],
            }
        )
    return {"selectors_checked": selectors, "matched_nodes": nodes}


def _field_dom_previews(dom_summary: dict[str, Any]) -> dict[str, str | None]:
    nodes = dom_summary.get("matched_nodes") or []
    previews: dict[str, str | None] = {}
    for field_name, selectors in FIELD_DOM_SELECTORS.items():
        previews[field_name] = None
        for selector in selectors:
            match = next((node for node in nodes if node.get("selector") == selector and node.get("text_preview")), None)
            if match:
                previews[field_name] = str(match["text_preview"])[:200]
                break
    return previews


def _assert_debug_report_safe(report: dict[str, Any]) -> None:
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, default=str)
    lowered = payload.lower()
    for label in ALLOWED_SOURCE_LABELS:
        lowered = lowered.replace(label.lower(), "")
    hits = [term for term in DEBUG_SENSITIVE_TERMS if term in lowered]
    if hits:
        raise ValueError(f"debug extraction report contains sensitive terms: {sorted(set(hits))}")


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True
