import asyncio
import json
from pathlib import Path

from xhs_profile_exporter import golden_validation
from xhs_profile_exporter.golden_validation import COMPARE_FIELDS, compare_golden_expected, load_golden_fixtures


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "golden_notes"


def test_live_golden_validation(monkeypatch):
    fixtures = load_golden_fixtures(FIXTURE_DIR)
    fixture_by_note = {fixture["note_id"]: fixture for fixture in fixtures}

    async def fake_extract_note_for_validation(crawler, note_id, creator_filter=None):
        expected = fixture_by_note[note_id]["expected"]
        return {
            "run_id": "mock-live",
            "status": "OK",
            "login_status": "LOGIN_OK",
            "note_id": note_id,
            "detail_ready": True,
            "fields": {
                field: {"value": expected.get(field), "source": "DOM_EXACT"}
                for field in COMPARE_FIELDS
            },
        }

    monkeypatch.setattr(golden_validation, "extract_note_for_validation", fake_extract_note_for_validation)

    result = asyncio.run(golden_validation.run_live_golden_validation(None, None, None, FIXTURE_DIR))

    assert result["passed"] is True
    assert len(result["notes"]) == 3
    assert result["diffs"] == []


def test_golden_note_extraction_mismatch_report_includes_source():
    diffs = compare_golden_expected(
        "664c92e5000000001500804e",
        {"like_count": 32000},
        {"like_count": {"value": 3200, "source": "DOM_EXACT"}},
    )

    assert diffs == [
        {
            "note_id": "664c92e5000000001500804e",
            "field": "like_count",
            "expected": 32000,
            "actual": 3200,
            "source": "DOM_EXACT",
        }
    ]


def test_golden_fixtures_have_expected_schema():
    fixtures = load_golden_fixtures(FIXTURE_DIR)
    assert len(fixtures) == 3
    for fixture in fixtures:
        assert fixture["note_id"]
        assert set(fixture["expected"]) == set(COMPARE_FIELDS)
        json.dumps(fixture, ensure_ascii=False)
