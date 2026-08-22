import asyncio
import json
from pathlib import Path

import pytest

from xhs_profile_exporter import golden_validation
from xhs_profile_exporter.golden_validation import (
    COMPARE_FIELDS,
    build_actual_payload,
    compare_golden_expected,
    load_golden_fixtures,
    sanitize_detail_html,
    validate_golden_fixture,
    write_golden_review_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "golden_notes"


def test_live_golden_validation(monkeypatch):
    fixtures = load_golden_fixtures(FIXTURE_DIR)
    fixture_by_note = {fixture["note_id"]: fixture for fixture in fixtures}

    async def fake_extract_golden_notes_for_validation(crawler, fixtures, creator_filter=None, capture_artifacts=True):
        notes = []
        aggregate = {"asserted_fields": 0, "passed_fields": 0, "failed_fields": 0, "skipped_fields": 0}
        for fixture in fixtures:
            fields = _actual_fields_from_expected(fixture_by_note[fixture["note_id"]]["expected"])
            comparison = compare_golden_expected(fixture["note_id"], fixture["expected"], fields)
            for key in aggregate:
                aggregate[key] += comparison["stats"][key]
            notes.append(
                {
                    "note_id": fixture["note_id"],
                    "status": "OK",
                    "login_status": "LOGIN_OK",
                    "detail_ready": True,
                    "stats": comparison["stats"],
                    "diffs": comparison["diffs"],
                }
            )
        return {"mode": "golden-live", "run_id": "mock-live", "passed": True, "safe_stop_reason": None, "stats": aggregate, "notes": notes, "diffs": []}

    monkeypatch.setattr(golden_validation, "extract_golden_notes_for_validation", fake_extract_golden_notes_for_validation)

    result = asyncio.run(golden_validation.run_live_golden_validation(None, None, None, FIXTURE_DIR))

    assert result["passed"] is True
    assert len(result["notes"]) == 3
    assert result["diffs"] == []
    assert result["stats"] == {"asserted_fields": 24, "passed_fields": 24, "failed_fields": 0, "skipped_fields": 0}


def test_golden_exact_assertion():
    result = compare_golden_expected(
        "note",
        _expected_with("like_count", {"assert": "exact", "value": 32000}),
        _actual_with("like_count", 32000, "DOM_EXACT"),
    )
    assert result["passed"] is True
    assert result["stats"]["passed_fields"] == 1


def test_golden_missing_assertion():
    result = compare_golden_expected(
        "note",
        _expected_with("comment_count", {"assert": "missing"}),
        _actual_with("comment_count", None, "MISSING"),
    )
    assert result["passed"] is True
    assert result["stats"]["passed_fields"] == 1


def test_golden_skip_assertion():
    result = compare_golden_expected(
        "note",
        _expected_with("body", {"assert": "skip", "reason": "manual_unknown"}),
        _actual_with("body", "live text", "DOM_EXACT"),
    )
    assert result["passed"] is True
    assert result["stats"]["asserted_fields"] == 0
    assert result["stats"]["skipped_fields"] == len(COMPARE_FIELDS)
    body = next(item for item in result["fields"] if item["field"] == "body")
    assert body["actual"] == "live text"


def test_unknown_null_not_used_as_skip():
    fixture = {"note_id": "note", "expected": {field: {"assert": "skip", "reason": "x"} for field in COMPARE_FIELDS}}
    fixture["expected"]["comment_count"] = None
    with pytest.raises(ValueError, match="assertion semantics"):
        validate_golden_fixture(fixture)


def test_body_is_a_golden_field():
    assert "body" in COMPARE_FIELDS
    assert "share_count" not in COMPARE_FIELDS
    for fixture in load_golden_fixtures(FIXTURE_DIR):
        assert "body" in fixture["expected"]
        assert "share_count" not in fixture["expected"]


def test_first_golden_note_body_is_exact():
    fixture = _fixture("664c92e5000000001500804e")
    assert fixture["expected"]["body"] == {
        "assert": "exact",
        "value": "尊嘟有效！第二次极限艾灸祛湿气成功啦！还有艾灸时注意保暖，我是为了演示才穿的吊带。另外全程无广，可能穴位搭配没那么好，毕竟不是专业的，欢迎专业人士指正 #不懂就问有问必答 #祛湿气#养生 #上热下寒 #中焦不通",
    }


def test_second_golden_note_comment_count_is_exact_zero():
    fixture = _fixture("6a7b27e9000000003400c518")
    assert fixture["expected"]["comment_count"] == {"assert": "exact", "value": 0}


def test_third_golden_note_body_is_exact():
    fixture = _fixture("69de332f000000002301ea70")
    assert fixture["expected"]["body"] == {
        "assert": "exact",
        "value": "这周去查一下是什么原因\n还是太虚了\n#身体容易累 #那些煎熬着的日子 #年轻人身体就是好倒头就睡",
    }


def test_golden_compare_reports_skipped_fields():
    result = compare_golden_expected(
        "note",
        _expected_with("body", {"assert": "skip", "reason": "manual_unknown"}),
        _actual_with("body", "actual body", "DOM_EXACT"),
    )
    body = next(item for item in result["fields"] if item["field"] == "body")
    assert body == {
        "field": "body",
        "assertion": "skip",
        "expected": None,
        "actual": "actual body",
        "source": "DOM_EXACT",
        "passed": None,
        "reason": "manual_unknown",
    }


def test_golden_artifact_schema(tmp_path: Path):
    actual = build_actual_payload(
        "note",
        True,
        {
            field: {"value": "v", "source": "DOM_EXACT"}
            for field in COMPARE_FIELDS
        },
    )
    field_summary = {
        field: {"selectors_checked": ["#x"], "matched_count": 1, "text": "v"}
        for field in COMPARE_FIELDS
    }
    dom_summary = {
        "note_id": "note",
        "pre_extract_dom_summary": {"detail_root": {"root_found": True}, "fields": field_summary},
        "post_extract_dom_summary": {"detail_root": {"root_found": True}, "fields": field_summary},
        "pre_post_consistent": True,
    }
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"note_id": "note", "expected": {}}, ensure_ascii=False), encoding="utf-8")
    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"png")

    artifact_dir = write_golden_review_artifact(tmp_path, "note", fixture, actual, dom_summary, '<a href="https://x.test/a?token=secret">x</a>', screenshot)

    assert (artifact_dir / "actual.json").exists()
    assert (artifact_dir / "dom_summary.json").exists()
    assert (artifact_dir / "detail.html").exists()
    assert (artifact_dir / "fixture.json").exists()
    assert (artifact_dir / "page_screenshot.png").exists()
    saved_summary = json.loads((artifact_dir / "dom_summary.json").read_text(encoding="utf-8"))
    assert "pre_extract_dom_summary" in saved_summary
    assert "post_extract_dom_summary" in saved_summary
    assert saved_summary["pre_post_consistent"] is True
    assert "token" not in (artifact_dir / "detail.html").read_text(encoding="utf-8").lower()


def test_golden_html_url_sanitization():
    html = '<img src="https://img.test/a.png?xsec_token=abc&width=1#frag"><a href="/search?token=abc">tag</a>'
    sanitized = sanitize_detail_html(html).lower()
    assert "xsec" not in sanitized
    assert "token" not in sanitized
    assert "?" not in sanitized
    assert "#frag" not in sanitized


def _expected_with(field: str, spec: dict):
    expected = {name: {"assert": "skip", "reason": "unit_test_not_under_assertion"} for name in COMPARE_FIELDS}
    expected[field] = spec
    return expected


def _actual_with(field: str, value, source: str):
    actual = {name: {"value": None, "source": "MISSING"} for name in COMPARE_FIELDS}
    actual[field] = {"value": value, "source": source}
    return actual


def _actual_fields_from_expected(expected: dict):
    fields = {}
    for field, spec in expected.items():
        value = spec.get("value") if spec["assert"] == "exact" else None
        fields[field] = {"value": value, "source": "DOM_EXACT" if value is not None else "MISSING"}
    return fields


def _fixture(note_id: str):
    return next(fixture for fixture in load_golden_fixtures(FIXTURE_DIR) if fixture["note_id"] == note_id)
