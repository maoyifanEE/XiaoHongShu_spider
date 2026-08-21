import json
from pathlib import Path

from openpyxl import Workbook

from xhs_profile_exporter.exporter import NOTE_HEADERS
from xhs_profile_exporter.five_note_review import (
    assert_artifact_text_safe,
    quality_check_actual,
    validate_excel_readback,
)


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
    path = _workbook(tmp_path, [_excel_row(note_id="note1", body=None, shares_value=None, shares_raw=None, shares_is_exact=None)])
    row = _db_row(note_id="note1", body=None, shares_value=None, shares_raw=None, shares_is_exact=None)
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


def test_five_note_quality_flags_body_ui_pollution():
    fields = {"body": {"value": "正文\n说点什么...", "source": "DOM_EXACT"}, "title": {"value": "标题", "source": "DOM_EXACT"}}
    report = quality_check_actual(fields)
    assert report["passed"] is False
    assert report["issues"][0]["field"] == "body"


def test_five_note_artifact_sensitive_scan_allows_source_enum_only():
    assert_artifact_text_safe(json.dumps({"source": "DETAIL_INITIAL_STATE"}), "actual.json")


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
        "分享数": None,
        "分享原始显示": None,
        "分享是否精确": None,
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
        "shares_value": "分享数",
        "shares_raw": "分享原始显示",
        "shares_is_exact": "分享是否精确",
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
        "shares_value": None,
        "shares_raw": None,
        "shares_is_exact": None,
        "hashtags": json.dumps(["A", "B"], ensure_ascii=False),
    }
    row.update(overrides)
    return row
