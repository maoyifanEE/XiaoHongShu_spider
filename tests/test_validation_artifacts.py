import csv
import json
from pathlib import Path

import pytest

from xhs_profile_exporter.runtime import CollectionResult
from xhs_profile_exporter.validation_artifacts import (
    CSV_HEADERS,
    build_validation_note,
    write_live_validation_artifact,
)


def test_validation_artifact_schema(tmp_path: Path):
    note_id = "664c92e5000000001500804e"
    collection = CollectionResult(
        attempted_ids=[note_id],
        verified_ids=[note_id],
        exportable_ids=[note_id],
    )
    collection.validation_notes.append(
        build_validation_note(
            note_id,
            {
                "title": "标题",
                "body": "正文不会写入 CSV",
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
                    "publish_time": "PAGE_RESPONSE",
                    "like_count": "DOM_EXACT",
                    "collect_count": "DOM_EXACT",
                    "comment_count": "DOM_EXACT",
                    "share_count": "DETAIL_INITIAL_STATE",
                    "tags": "DOM_EXACT",
                },
            },
            detail_ready=True,
            exportable=True,
        )
    )

    artifact_dir = write_live_validation_artifact(
        tmp_path,
        run_id="run-safe",
        status="SUCCESS",
        login_status="LOGIN_OK",
        notes_discovered=1,
        collection=collection,
    )

    summary = json.loads((artifact_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["run_id"] == "run-safe"
    assert summary["attempted"] == 1
    assert summary["target_verified"] == 1
    assert summary["exportable"] == 1
    assert summary["detail_not_ready"] == 0
    assert summary["fields"]["title"] == {"value_present": 1, "source": {"DOM_EXACT": 1}}
    assert summary["fields"]["share_count"] == {"value_present": 1, "source": {"DETAIL_INITIAL_STATE": 1}}

    with (artifact_dir / "field_validation.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0].keys() == set(CSV_HEADERS)
    assert rows[0]["note_id"] == note_id
    assert rows[0]["title"] == "标题"
    assert rows[0]["source_title"] == "DOM_EXACT"
    assert rows[0]["source_body"] == "DOM_EXACT"
    assert json.loads(rows[0]["source_metrics"])["share_count"] == "DETAIL_INITIAL_STATE"


def test_validation_artifact_no_sensitive_fields(tmp_path: Path):
    note_id = "664c92e5000000001500804e"
    collection = CollectionResult(attempted_ids=[note_id], verified_ids=[note_id], exportable_ids=[note_id])
    collection.validation_notes.append(
        build_validation_note(
            note_id,
            {
                "title": "公开标题",
                "body": "正文不进入 artifact",
                "note_type": "图文",
                "publish_time": "2024-05-29",
                "likes_value": 1,
                "hashtags": ["标签"],
                "top_comments": [{"body": "excluded nested text"}],
                "raw_json": {"markup": "excluded markup"},
                "field_sources": {"title": "DOM_EXACT", "body": "DOM_EXACT", "like_count": "DOM_EXACT"},
            },
            detail_ready=True,
            exportable=True,
        )
    )

    artifact_dir = write_live_validation_artifact(
        tmp_path,
        run_id="run-public",
        status="SUCCESS",
        login_status="LOGIN_OK",
        notes_discovered=1,
        collection=collection,
    )
    payload = (artifact_dir / "summary.json").read_text(encoding="utf-8") + (artifact_dir / "field_validation.csv").read_text(encoding="utf-8-sig")
    lowered = payload.lower()
    assert "cookie" not in lowered
    assert "token" not in lowered
    assert "authorization" not in lowered
    assert "bearer" not in lowered
    assert "xsec" not in lowered
    assert "session" not in lowered
    assert "initial_state" not in lowered
    assert "正文不进入 artifact" not in payload
    assert "excluded nested text" not in payload
    assert "excluded markup" not in payload


def test_validation_artifact_sensitive_scan_fails_before_write(tmp_path: Path):
    note_id = "664c92e5000000001500804e"
    collection = CollectionResult(attempted_ids=[note_id], verified_ids=[note_id], exportable_ids=[note_id])
    collection.validation_notes.append(
        build_validation_note(
            note_id,
            {"title": "contains token value", "field_sources": {"title": "DOM_EXACT"}},
            detail_ready=True,
            exportable=True,
        )
    )

    with pytest.raises(ValueError, match="sensitive terms"):
        write_live_validation_artifact(
            tmp_path,
            run_id="run-sensitive",
            status="SUCCESS",
            login_status="LOGIN_OK",
            notes_discovered=1,
            collection=collection,
        )
    assert not (tmp_path / "validation" / "live_runs" / "run-sensitive").exists()
