import json
from pathlib import Path

from openpyxl import Workbook

from xhs_profile_exporter.exporter import NOTE_HEADERS
from xhs_profile_exporter.five_note_review import (
    FIELD_STATUS_CONFIRMED_ABSENT,
    FIELD_STATUS_PRESENT,
    FIELD_STATUS_UNVERIFIED_MISSING,
    REVIEW_FIELDS,
    annotate_actual_field_status,
    assert_artifact_text_safe,
    build_e2e_review_summary,
    e2e_review_dir,
    e2e_review_enabled,
    quality_check_actual,
    validate_excel_readback,
)
from xhs_profile_exporter.runtime import CollectionResult


def test_five_note_review_fields_are_final_product_scope():
    assert REVIEW_FIELDS == [
        "title",
        "body",
        "note_type",
        "publish_time",
        "like_count",
        "collect_count",
        "comment_count",
        "tags",
    ]


def test_excel_headers_exclude_removed_fields():
    assert "分享数" not in NOTE_HEADERS
    assert "分享原始显示" not in NOTE_HEADERS
    assert "分享是否精确" not in NOTE_HEADERS
    assert not any(header.startswith(("评论1_", "评论2_", "评论3_")) for header in NOTE_HEADERS)
    assert {"评论数", "评论原始显示", "评论是否精确"}.issubset(set(NOTE_HEADERS))


def test_e2e_review_enablement_supports_five_and_ten_only():
    assert e2e_review_enabled("collect", 5) is True
    assert e2e_review_enabled("collect", 10) is True
    assert e2e_review_enabled("collect", 20) is False
    assert e2e_review_enabled("smoke", 10) is False


def test_e2e_review_dir_uses_generic_path(tmp_path: Path):
    path = e2e_review_dir(tmp_path, "run")
    assert path == tmp_path / "validation" / "e2e_review" / "run"
    assert path.exists()


def test_excel_readback_numeric_normalization(tmp_path: Path):
    path = _workbook(
        tmp_path,
        [
            {
                "note_id": "note1",
                "标题": "标题",
                "帖子类型": "视频",
                "发布时间": "08-11",
                "点赞数": 5.0,
                "点赞原始显示": "5",
                "点赞是否精确": "是",
                "收藏数": 1.0,
                "收藏原始显示": "1",
                "收藏是否精确": "是",
                "评论数": 0.0,
                "评论原始显示": "0",
                "评论是否精确": "是",
                "标签": "A B",
                "正文": "正文",
            }
        ],
    )
    report = validate_excel_readback(path, [_db_row(note_id="note1")], ["note1"])
    assert report["rows_found"] == 1
    assert report["field_diffs"] == []


def test_excel_readback_none_and_blank_are_equal(tmp_path: Path):
    path = _workbook(tmp_path, [_excel_row(note_id="note1", body=None)])
    row = _db_row(note_id="note1", body=None)
    report = validate_excel_readback(path, [row], ["note1"])
    assert report["field_diffs"] == []


def test_excel_readback_empty_tags_and_blank_are_equal(tmp_path: Path):
    path = _workbook(tmp_path, [_excel_row(note_id="note1", tags=None)])
    row = _db_row(note_id="note1", hashtags="[]")
    report = validate_excel_readback(path, [row], ["note1"])
    assert report["field_diffs"] == []


def test_excel_readback_duplicate_note_id_detection(tmp_path: Path):
    path = _workbook(tmp_path, [_excel_row(note_id="note1"), _excel_row(note_id="note1")])
    report = validate_excel_readback(path, [_db_row(note_id="note1")], ["note1"])
    assert report["duplicate_note_ids"] == ["note1"]


def test_excel_readback_missing_note_id_detection(tmp_path: Path):
    path = _workbook(tmp_path, [_excel_row(note_id="other")])
    report = validate_excel_readback(path, [_db_row(note_id="note1")], ["note1"])
    assert report["rows_found"] == 0
    assert report["missing_note_ids"] == ["note1"]


def test_excel_readback_field_mismatch_detection(tmp_path: Path):
    path = _workbook(tmp_path, [_excel_row(note_id="note1", title="错误标题")])
    report = validate_excel_readback(path, [_db_row(note_id="note1")], ["note1"])
    assert report["field_diffs"] == [{"note_id": "note1", "field": "title", "expected": "标题", "excel_actual": "错误标题"}]


def test_excel_readback_ten_rows(tmp_path: Path):
    note_ids = [f"note{i}" for i in range(10)]
    path = _workbook(tmp_path, [_excel_row(note_id=note_id) for note_id in note_ids])
    report = validate_excel_readback(path, [_db_row(note_id=note_id) for note_id in note_ids], note_ids)
    assert report["rows_expected"] == 10
    assert report["rows_found"] == 10
    assert report["duplicate_note_ids"] == []
    assert report["missing_note_ids"] == []
    assert report["field_diffs"] == []


def test_five_note_quality_flags_body_ui_pollution():
    fields = {"body": {"value": "正文\n说点什么...", "source": "DOM_EXACT"}, "title": {"value": "标题", "source": "DOM_EXACT"}}
    report = quality_check_actual(fields)
    assert report["passed"] is False
    assert report["issues"][0]["field"] == "body"


def test_five_note_artifact_sensitive_scan_allows_source_enum_only():
    assert_artifact_text_safe(json.dumps({"source": "DETAIL_INITIAL_STATE"}), "actual.json")


def test_partial_safe_stop_review_summary(tmp_path: Path):
    review_dir = e2e_review_dir(tmp_path, "run-partial")
    collection = CollectionResult(
        attempted_ids=["note1", "note2"],
        verified_ids=["note1"],
        exportable_ids=["note1"],
        navigation_failed_ids=[],
        safe_stop_reason="RISK_CONTROL_DETECTED",
        field_presence={"title": {"present": 1, "missing": 0}},
    )
    summary = build_e2e_review_summary(run_id="run-partial", collection=collection, review_dir=review_dir, excel_readback=None)
    assert summary["data_quality_status"] == "PASS"
    assert summary["attempted"] == 2
    assert summary["target_verified"] == 1
    assert summary["detail_ready"] == 1
    assert summary["exportable"] == 1
    assert summary["risk_control"] is True
    assert summary["human_verification"] is False
    assert summary["field_completeness"]["title"] == {"present": 1, "missing": 0}


def test_e2e_review_summary_marks_missing_field(tmp_path: Path):
    review_dir = e2e_review_dir(tmp_path, "run-missing")
    note_dir = review_dir / "note1"
    note_dir.mkdir()
    (note_dir / "actual.json").write_text(
        json.dumps(
            {
                "quality": {"passed": True},
                "fields": {
                    field: {"value": None if field == "tags" else "value", "source": "MISSING" if field == "tags" else "DOM_EXACT"}
                    for field in REVIEW_FIELDS
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    collection = CollectionResult(attempted_ids=["note1"], verified_ids=["note1"], exportable_ids=["note1"])
    summary = build_e2e_review_summary(run_id="run-missing", collection=collection, review_dir=review_dir, excel_readback=None)
    assert summary["data_quality_status"] == "DATA_QUALITY_REVIEW_REQUIRED"
    assert summary["missing_field_note_ids"] == {"tags": ["note1"]}
    assert summary["field_quality"]["tags"] == {
        "present": 0,
        "confirmed_absent": 0,
        "unverified_missing": 1,
        "verified_total": 0,
    }


def test_tags_present_status_is_present():
    actual = {"fields": {"tags": {"value": ["A"], "source": "DOM_EXACT"}}}
    annotated = annotate_actual_field_status(actual, _dom_tags_absent(), _dom_tags_absent())
    assert annotated["fields"]["tags"]["status"] == FIELD_STATUS_PRESENT


def test_tags_confirmed_absent_status_passes_review_gate(tmp_path: Path):
    review_dir = e2e_review_dir(tmp_path, "run-confirmed-absent")
    note_dir = review_dir / "note1"
    note_dir.mkdir()
    actual = annotate_actual_field_status(
        {"quality": {"passed": True}, "fields": _actual_fields(tags={"value": None, "source": "MISSING"})},
        _dom_tags_absent(),
        _dom_tags_absent(),
    )
    (note_dir / "actual.json").write_text(json.dumps(actual, ensure_ascii=False), encoding="utf-8")
    (note_dir / "dom_summary.json").write_text(
        json.dumps(
            {
                "pre_extract_dom_summary": _dom_tags_absent(),
                "post_extract_dom_summary": _dom_tags_absent(),
                "pre_post_consistent": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    collection = CollectionResult(attempted_ids=["note1"], verified_ids=["note1"], exportable_ids=["note1"])
    summary = build_e2e_review_summary(run_id="run-confirmed-absent", collection=collection, review_dir=review_dir, excel_readback=None)
    assert actual["fields"]["tags"]["status"] == FIELD_STATUS_CONFIRMED_ABSENT
    assert actual["fields"]["tags"]["missing_reason"] == "confirmed_absent"
    assert summary["data_quality_status"] == "PASS"
    assert summary["field_quality"]["tags"] == {
        "present": 0,
        "confirmed_absent": 1,
        "unverified_missing": 0,
        "verified_total": 1,
    }


def test_tags_missing_without_absence_evidence_requires_review(tmp_path: Path):
    review_dir = e2e_review_dir(tmp_path, "run-unverified")
    note_dir = review_dir / "note1"
    note_dir.mkdir()
    actual = annotate_actual_field_status(
        {"quality": {"passed": True}, "fields": _actual_fields(tags={"value": None, "source": "MISSING"})},
        None,
        None,
    )
    (note_dir / "actual.json").write_text(json.dumps(actual, ensure_ascii=False), encoding="utf-8")
    collection = CollectionResult(attempted_ids=["note1"], verified_ids=["note1"], exportable_ids=["note1"])
    summary = build_e2e_review_summary(run_id="run-unverified", collection=collection, review_dir=review_dir, excel_readback=None)
    assert actual["fields"]["tags"]["status"] == FIELD_STATUS_UNVERIFIED_MISSING
    assert summary["data_quality_status"] == "DATA_QUALITY_REVIEW_REQUIRED"
    assert summary["unverified_missing_note_ids"] == {"tags": ["note1"]}


def test_extractor_null_alone_cannot_confirm_absent():
    annotated = annotate_actual_field_status(
        {"fields": {"tags": {"value": None, "source": "MISSING", "missing_reason": "not_observed"}}},
        None,
        None,
    )
    assert annotated["fields"]["tags"]["status"] == FIELD_STATUS_UNVERIFIED_MISSING
    assert annotated["fields"]["tags"]["missing_reason"] == "not_observed"


def _workbook(tmp_path: Path, rows: list[dict]):
    path = tmp_path / "out.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "公开笔记"
    ws.append(NOTE_HEADERS)
    for row in rows:
        ws.append([row.get(header) for header in NOTE_HEADERS])
    wb.save(path)
    return path


def _excel_row(**overrides):
    row = {
        "note_id": "note1",
        "标题": "标题",
        "正文": "正文",
        "帖子类型": "视频",
        "发布时间": "08-11",
        "点赞数": 5,
        "点赞原始显示": "5",
        "点赞是否精确": "是",
        "收藏数": 1,
        "收藏原始显示": "1",
        "收藏是否精确": "是",
        "评论数": 0,
        "评论原始显示": "0",
        "评论是否精确": "是",
        "标签": "A B",
    }
    key_map = {
        "note_id": "note_id",
        "title": "标题",
        "body": "正文",
        "note_type": "帖子类型",
        "publish_time": "发布时间",
        "likes_value": "点赞数",
        "likes_raw": "点赞原始显示",
        "likes_is_exact": "点赞是否精确",
        "collects_value": "收藏数",
        "collects_raw": "收藏原始显示",
        "collects_is_exact": "收藏是否精确",
        "comments_value": "评论数",
        "comments_raw": "评论原始显示",
        "comments_is_exact": "评论是否精确",
        "tags": "标签",
    }
    for key, value in overrides.items():
        row[key_map.get(key, key)] = value
    return row


def _db_row(**overrides):
    row = {
        "note_id": "note1",
        "title": "标题",
        "body": "正文",
        "note_type": "视频",
        "publish_time": "08-11",
        "likes_value": 5,
        "likes_raw": "5",
        "likes_is_exact": 1,
        "collects_value": 1,
        "collects_raw": "1",
        "collects_is_exact": 1,
        "comments_value": 0,
        "comments_raw": "0",
        "comments_is_exact": 1,
        "hashtags": json.dumps(["A", "B"], ensure_ascii=False),
    }
    row.update(overrides)
    return row


def _actual_fields(**overrides):
    fields = {field: {"value": "value", "source": "DOM_EXACT"} for field in REVIEW_FIELDS}
    fields["like_count"] = {"value": 1, "source": "DOM_EXACT"}
    fields["collect_count"] = {"value": 1, "source": "DOM_EXACT"}
    fields["comment_count"] = {"value": 0, "source": "DOM_EXACT"}
    fields["tags"] = {"value": ["A"], "source": "DOM_EXACT"}
    fields.update(overrides)
    return fields


def _dom_tags_absent():
    return {
        "detail_root": {"root_found": True},
        "fields": {
            "body": {"matched_count": 1, "text": "正文没有话题标签"},
            "tags": {
                "selectors_checked": ['#detail-desc a[href*="search"]', 'a[href*="/search"]'],
                "matched_count": 0,
                "text": "",
            },
        },
    }
