from xhs_profile_exporter.crawler import extract_public_note_records, extract_public_profile_record, is_public_profile_payload
from xhs_profile_exporter.utils import sanitize_json, sanitize_url


def test_token_bearing_url_string_is_sanitized():
    payload = {"url": "https://www.xiaohongshu.com/explore/abc?xsec_token=SECRET&a=1&token=BAD"}
    clean = sanitize_json(payload)
    assert "SECRET" not in str(clean)
    assert "BAD" not in str(clean)
    assert clean["url"] == "https://www.xiaohongshu.com/explore/abc?a=1"


def test_bearer_and_authorization_query_redacted():
    clean = sanitize_json({"text": "Authorization=SECRET Bearer abc.def.ghi"})
    assert "SECRET" not in clean["text"]
    assert "abc.def.ghi" not in clean["text"]
    assert "Authorization" not in clean["text"]
    assert "Bearer" not in clean["text"]


def test_sanitize_url_removes_sensitive_query():
    assert sanitize_url("https://xhs/a?xsec_token=secret&keep=1") == "https://xhs/a?keep=1"


def test_author_fields_are_not_removed_by_auth_sanitizer():
    clean = sanitize_json(
        {
            "author": "A",
            "author_id": "1",
            "author_name": "Alice",
            "authority": "public",
            "authorization": "SECRET1",
            "auth_token": "SECRET2",
            "access_token": "SECRET3",
            "xsec_token": "SECRET4",
            "session_id": "SECRET5",
        }
    )
    assert clean["author"] == "A"
    assert clean["author_id"] == "1"
    assert clean["author_name"] == "Alice"
    assert clean["authority"] == "public"
    assert "SECRET" not in str(clean)
    assert "authorization" not in clean
    assert "auth_token" not in clean
    assert "access_token" not in clean
    assert "xsec_token" not in clean
    assert "session_id" not in clean


def test_structured_exact_note_id_allowlist_only():
    note_id = "66dabcde000000001f01abcd"
    unrelated = "aaaaaaaaaaaaaaaaaaaaaaaa"
    records = extract_public_note_records(
        {
            "items": [
                {"id": note_id, "title": "标题", "xsec_token": "SECRET", "private": "nope"},
                {"some_text": unrelated},
            ]
        }
    )
    assert note_id in records
    assert unrelated not in records
    assert records[note_id]["title"] == "标题"
    assert "private" not in records[note_id]
    assert "SECRET" not in str(records)
    assert "xsec_token" not in str(records)


def test_profile_association_requires_exact_creator_schema():
    creator_id = "5cfb1f8e00000000100322e4"
    assert not is_public_profile_payload({"user": "ordinary"}, creator_id)
    assert is_public_profile_payload({"user_id": creator_id, "nickname": "昵称"}, creator_id)


def test_public_profile_record_is_exact_creator_allowlist_only():
    creator_id = "5cfb1f8e00000000100322e4"
    record = extract_public_profile_record(
        {
            "data": {
                "user_id": creator_id,
                "nickname": "昵称",
                "red_id": "Guo505050",
                "desc": "公开简介",
                "fans": "1万",
                "authorization": "SECRET1",
                "xsec_token": "SECRET2",
                "nested": {"private": "nope"},
            },
            "another_user": {"user_id": "aaaaaaaaaaaaaaaaaaaaaaaa", "nickname": "other"},
            "headers": {"cookie": "SECRET3"},
        },
        creator_id,
    )
    assert record == {
        "user_id": creator_id,
        "nickname": "昵称",
        "xhs_id": "Guo505050",
        "bio": "公开简介",
        "followers": "1万",
    }
    assert "SECRET" not in str(record)
