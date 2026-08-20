from xhs_profile_exporter.navigation_probe import (
    choose_immediate_visible_candidates,
    classify_navigation_outcome,
    discover_all_then_click_available,
    path_query_summary,
)
from xhs_profile_exporter.runtime import VisibleCardProbe


NOTE_ID = "66dabcde000000001f01abcd"


def snapshot(path="/user/profile/abc", dialogs=0, details=0, target_details=0, children=10):
    return {
        "url": {"path": path, "query_keys": [], "note_id": None, "has_query": False},
        "history_length": 2,
        "dialog_count": dialogs,
        "detail_root_count": details,
        "target_detail_root_count": target_details,
        "body_child_count": children,
        "frame_count": 1,
    }


def test_url_changed_target_match_is_verified():
    before = snapshot()
    after = snapshot(path=f"/explore/{NOTE_ID}", details=1)
    verified, reason = classify_navigation_outcome(before, after, NOTE_ID)
    assert verified is True
    assert reason == "URL_CHANGED_TARGET_MATCH"


def test_url_unchanged_modal_target_verified():
    before = snapshot(dialogs=0, details=1)
    after = snapshot(dialogs=1, details=2, target_details=1)
    verified, reason = classify_navigation_outcome(before, after, NOTE_ID)
    assert verified is True
    assert reason == "MODAL_OPENED_TARGET_VERIFIED"


def test_popup_target_verified():
    before = snapshot()
    after = snapshot()
    popup = snapshot(path=f"/discovery/item/{NOTE_ID}", details=1)
    verified, reason = classify_navigation_outcome(before, after, NOTE_ID, popup)
    assert verified is True
    assert reason == "POPUP_OPENED_TARGET_VERIFIED"


def test_virtual_list_discover_all_then_click_fails_but_immediate_probe_succeeds():
    visible_now = [
        VisibleCardProbe(NOTE_ID, 0, True, "visible_card_with_hidden_anchor"),
        VisibleCardProbe("aaaaaaaaaaaaaaaaaaaaaaaa", 1, True, "visible_anchor"),
    ]
    after_scroll = [
        VisibleCardProbe(NOTE_ID, 0, False, "unmounted"),
        VisibleCardProbe("bbbbbbbbbbbbbbbbbbbbbbbb", 2, True, "visible_anchor"),
    ]
    assert choose_immediate_visible_candidates(visible_now, limit=1)[0].note_id == NOTE_ID
    assert discover_all_then_click_available(after_scroll, NOTE_ID) is False


def test_click_no_op_is_not_verified():
    before = snapshot()
    after = snapshot()
    verified, reason = classify_navigation_outcome(before, after, NOTE_ID)
    assert verified is False
    assert reason == "CLICK_NO_STATE_CHANGE"


def test_wrong_modal_is_rejected():
    before = snapshot(dialogs=0, details=1)
    after = snapshot(dialogs=1, details=2, target_details=0)
    verified, reason = classify_navigation_outcome(before, after, NOTE_ID)
    assert verified is False
    assert reason == "MODAL_OPENED_TARGET_UNKNOWN"


def test_probe_url_summary_keeps_only_path_and_query_keys():
    summary = path_query_summary(f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=secret&foo=private_value")
    assert summary["path"] == f"/explore/{NOTE_ID}"
    assert summary["query_keys"] == ["foo"]
    assert "secret" not in str(summary)
    assert "private_value" not in str(summary)


def test_profile_note_route_uses_second_id_as_note_id():
    user_id = "5cfb1f8e00000000100322e4"
    summary = path_query_summary(f"https://www.xiaohongshu.com/user/profile/{user_id}/{NOTE_ID}?xsec_token=secret")
    assert summary["path"] == f"/user/profile/{user_id}/{NOTE_ID}"
    assert summary["note_id"] == NOTE_ID
    assert summary["query_keys"] == []
