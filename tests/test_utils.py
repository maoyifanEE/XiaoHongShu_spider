from xhs_profile_exporter.extractors import extract_note_id, extract_user_id
from xhs_profile_exporter.time_utils import normalize_publish_time_value
from xhs_profile_exporter.utils import canonical_note_url, parse_count, sanitize_url


def test_parse_count_exact_and_approx():
    assert parse_count("999") == (999, "999", True)
    assert parse_count("1.2万") == (12000, "1.2万", False)
    assert parse_count("3.2w") == (32000, "3.2w", False)
    assert parse_count("") == (None, None, None)


def test_null_is_not_zero():
    value, raw, exact = parse_count(None)
    assert value is None
    assert raw is None
    assert exact is None


def test_canonical_and_sanitize_urls():
    assert canonical_note_url("abc") == "https://www.xiaohongshu.com/explore/abc"
    assert sanitize_url("https://x.test/a?xsec_token=secret&a=1&token=bad") == "https://x.test/a?a=1"


def test_extract_ids():
    assert extract_user_id("https://www.xiaohongshu.com/user/profile/5cfb1f8e00000000100322e4?x=1") == "5cfb1f8e00000000100322e4"
    assert extract_note_id("https://www.xiaohongshu.com/explore/66dabcde000000001f01abcd?xsec_token=abc") == "66dabcde000000001f01abcd"


def test_normalize_publish_time_seconds_milliseconds_and_invalid():
    seconds, seconds_raw = normalize_publish_time_value(1234567890)
    millis, millis_raw = normalize_publish_time_value(1234567890000)
    date, date_raw = normalize_publish_time_value("2026-08-20")
    invalid, invalid_raw = normalize_publish_time_value(123456789012)
    assert seconds == "2009-02-14T07:31:30+08:00"
    assert seconds_raw == "1234567890"
    assert millis == "2009-02-14T07:31:30+08:00"
    assert millis_raw == "1234567890000"
    assert date == "2026-08-20"
    assert date_raw == "2026-08-20"
    assert invalid is None
    assert invalid_raw == "123456789012"
