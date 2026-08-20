from xhs_profile_exporter.extractors import _extract_comments


def test_comment_with_reply_button_is_kept():
    comments = _extract_comments(
        [{"id": "c1", "text": "用户A\n这是一条评论\n回复\n点赞", "nested": False}],
        3,
    )
    assert len(comments) == 1
    assert comments[0]["author_name"] == "用户A"
    assert "回复" not in comments[0]["body"]


def test_nested_reply_is_not_top_level():
    comments = _extract_comments(
        [
            {"id": "c1", "text": "用户A\n一级评论\n回复", "nested": False},
            {"id": "c2", "text": "用户B\n二级回复", "nested": True},
        ],
        3,
    )
    assert [item["comment_id"] for item in comments] == ["c1"]


def test_no_comments_returns_empty():
    assert _extract_comments([], 3) == []
