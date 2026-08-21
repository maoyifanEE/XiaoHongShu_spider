import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "golden_notes"

COMPARE_FIELDS = [
    "title",
    "note_type",
    "publish_time",
    "like_count",
    "collect_count",
    "comment_count",
    "share_count",
    "tags",
]


def test_golden_note_extraction_matches_expected():
    diffs = []
    for fixture_path in sorted(FIXTURE_DIR.glob("note_*.json")):
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        report_path = ROOT / fixture["validation_basis"]["debug_report"]
        report = json.loads(report_path.read_text(encoding="utf-8"))

        assert report["note_id"] == fixture["note_id"]
        actual = _mock_extractor_result_from_report(report)
        expected = fixture["expected"]

        for field in COMPARE_FIELDS:
            if expected.get(field) != actual[field]["value"]:
                diffs.append(
                    {
                        "note_id": fixture["note_id"],
                        "field": field,
                        "expected": expected.get(field),
                        "actual": actual[field]["value"],
                        "source": actual[field]["source"],
                    }
                )

    assert not diffs, "golden extraction mismatches:\n" + json.dumps(diffs, ensure_ascii=False, indent=2)


def _mock_extractor_result_from_report(report: dict) -> dict:
    fields = report["fields"]
    return {
        field: {
            "value": fields[field].get("value"),
            "source": fields[field].get("source"),
        }
        for field in COMPARE_FIELDS
    }
