from pathlib import Path

import pytest
import yaml

from xhs_profile_exporter.config import load_config


def write_config(tmp_path: Path, data: dict):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def valid_config() -> dict:
    return {
        "creators": [
            {
                "name": "辣香郭",
                "user_id": "5cfb1f8e00000000100322e4",
                "url": "https://www.xiaohongshu.com/user/profile/5cfb1f8e00000000100322e4",
                "enabled": True,
            }
        ],
        "browser": {"persistent_profile": True},
        "safety": {
            "min_delay_seconds": 1,
            "max_delay_seconds": 2,
            "max_consecutive_errors": 1,
            "scroll_idle_rounds": 1,
            "scroll_max_rounds": 1,
            "max_notes_per_run": None,
            "max_page_visits_per_run": None,
            "max_runtime_minutes": None,
        },
        "collection": {
            "collect_top_comments": 3,
            "download_media": False,
            "smoke_note_limit": 3,
            "smoke_max_attempts": 12,
        },
    }


def test_valid_config_loads(tmp_path: Path):
    path = write_config(tmp_path, valid_config())
    assert load_config(tmp_path, path).creators[0].user_id == "5cfb1f8e00000000100322e4"


@pytest.mark.parametrize(
    ("path_keys", "value"),
    [
        (("safety", "min_delay_seconds"), -1),
        (("safety", "max_consecutive_errors"), 0),
        (("collection", "download_media"), True),
        (("browser", "persistent_profile"), False),
    ],
)
def test_invalid_config_fails_fast(tmp_path: Path, path_keys, value):
    data = valid_config()
    data[path_keys[0]][path_keys[1]] = value
    path = write_config(tmp_path, data)
    with pytest.raises(ValueError):
        load_config(tmp_path, path)


def test_user_id_url_mismatch_fails(tmp_path: Path):
    data = valid_config()
    data["creators"][0]["user_id"] = "wrong"
    path = write_config(tmp_path, data)
    with pytest.raises(ValueError, match="user_id"):
        load_config(tmp_path, path)


def test_smoke_attempts_must_cover_target(tmp_path: Path):
    data = valid_config()
    data["collection"]["smoke_note_limit"] = 5
    data["collection"]["smoke_max_attempts"] = 3
    path = write_config(tmp_path, data)
    with pytest.raises(ValueError, match="smoke_max_attempts"):
        load_config(tmp_path, path)
