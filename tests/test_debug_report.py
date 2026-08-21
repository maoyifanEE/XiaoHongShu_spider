import json
from pathlib import Path

import pytest

from xhs_profile_exporter.debug_report import build_extraction_report, write_extraction_report


def test_debug_extract_report_schema(tmp_path: Path):
    note_id = "664c92e5000000001500804e"
    report = build_extraction_report(
        run_id="debug-run",
        note_id=note_id,
        detail_ready=True,
        note={
            "status": "OK",
            "title": "标题",
            "body": "正文",
            "note_type": "视频",
            "publish_time": "2024-05-29",
            "likes_value": 1,
            "collects_value": 2,
            "comments_value": 3,
            "shares_value": 4,
            "hashtags": ["标签"],
            "field_sources": {
                "title": "DOM_EXACT",
                "body": "DOM_EXACT",
                "note_type": "DOM_EXACT",
                "publish_time": "DOM_EXACT",
                "like_count": "DOM_EXACT",
                "collect_count": "DOM_EXACT",
                "comment_count": "DOM_EXACT",
                "share_count": "DETAIL_INITIAL_STATE",
                "tags": "DOM_EXACT",
            },
        },
        dom_summary={
            "selectors_checked": ["#detail-title", "#detail-desc"],
            "matched_nodes": [{"selector": "#detail-title", "text_preview": "标题"}],
        },
    )
    path = write_extraction_report(tmp_path, report)
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["note_id"] == note_id
    assert saved["detail_ready"] is True
    assert set(saved["fields"]) == {
        "title",
        "body",
        "note_type",
        "publish_time",
        "like_count",
        "collect_count",
        "comment_count",
        "share_count",
        "tags",
    }
    assert saved["fields"]["title"]["source"] == "DOM_EXACT"
    assert saved["fields"]["share_count"]["source"] == "DETAIL_INITIAL_STATE"
    assert saved["fields"]["share_count"]["confidence"] == "high"
    assert saved["dom_summary"]["selectors_checked"] == ["#detail-title", "#detail-desc"]


def test_debug_artifact_no_sensitive_content(tmp_path: Path):
    report = build_extraction_report(
        run_id="debug-safe",
        note_id="664c92e5000000001500804e",
        detail_ready=True,
        note={
            "status": "OK",
            "title": "公开标题",
            "body": "公开正文",
            "field_sources": {"title": "DOM_EXACT", "body": "DOM_EXACT"},
            "top_comments": [{"body": "excluded nested text"}],
            "raw_json": {"markup": "excluded markup"},
        },
        dom_summary={
            "selectors_checked": ["#detail-title"],
            "matched_nodes": [{"selector": "#detail-title", "text_preview": "x" * 250}],
        },
    )
    path = write_extraction_report(tmp_path, report)
    payload = path.read_text(encoding="utf-8")
    lowered = payload.lower()

    assert "cookie" not in lowered
    assert "token" not in lowered
    assert "authorization" not in lowered
    assert "bearer" not in lowered
    assert "xsec" not in lowered
    assert "session" not in lowered
    assert "<html" not in lowered
    assert "response" not in lowered
    assert "initial_state" not in lowered
    assert "excluded nested text" not in payload
    assert "excluded markup" not in payload
    assert len(json.loads(payload)["dom_summary"]["matched_nodes"][0]["text_preview"]) == 200


def test_debug_artifact_sensitive_scan_fails_before_write(tmp_path: Path):
    report = build_extraction_report(
        run_id="debug-unsafe",
        note_id="664c92e5000000001500804e",
        detail_ready=True,
        note={"status": "OK", "title": "contains token text", "field_sources": {"title": "DOM_EXACT"}},
        dom_summary={"selectors_checked": ["#detail-title"], "matched_nodes": []},
    )

    with pytest.raises(ValueError, match="sensitive terms"):
        write_extraction_report(tmp_path, report)
    assert not (tmp_path / "debug" / "live_extract" / "debug-unsafe").exists()
