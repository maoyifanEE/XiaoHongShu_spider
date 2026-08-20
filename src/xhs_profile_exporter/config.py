from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .extractors import extract_user_id


@dataclass(frozen=True)
class CreatorConfig:
    name: str
    url: str
    enabled: bool
    xhs_id: str | None = None
    user_id: str | None = None


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    raw: dict[str, Any]
    creators: list[CreatorConfig]

    @property
    def version(self) -> str:
        return str(self.raw.get("program", {}).get("version", "0.1.0"))


def load_config(base_dir: Path, config_path: Path | None = None) -> AppConfig:
    path = config_path or base_dir / "config" / "config.yaml"
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    creators = [
        CreatorConfig(
            name=str(item.get("name") or item.get("user_id") or item.get("url")),
            url=str(item["url"]),
            enabled=bool(item.get("enabled", True)),
            xhs_id=item.get("xhs_id"),
            user_id=item.get("user_id"),
        )
        for item in raw.get("creators", [])
    ]
    if not creators:
        raise ValueError("config.yaml 中未配置 creators")
    _validate_config(raw, creators)
    return AppConfig(base_dir=base_dir, raw=raw, creators=creators)


def _require(condition: bool, key: str, message: str) -> None:
    if not condition:
        raise ValueError(f"config.yaml {key}: {message}")


def _nullable_number(raw: dict[str, Any], section: str, key: str) -> Any:
    return raw.get(section, {}).get(key)


def _validate_config(raw: dict[str, Any], creators: list[CreatorConfig]) -> None:
    for index, creator in enumerate(creators):
        prefix = f"creators[{index}]"
        _require(bool(creator.name), f"{prefix}.name", "不能为空")
        _require(bool(creator.url), f"{prefix}.url", "不能为空")
        _require("/user/profile/" in creator.url, f"{prefix}.url", "必须是小红书 profile URL")
        extracted = extract_user_id(creator.url)
        if creator.user_id:
            _require(extracted == creator.user_id, f"{prefix}.user_id", "必须与 profile URL 中的 user_id 一致")

    browser = raw.get("browser", {})
    _require(browser.get("persistent_profile") is True, "browser.persistent_profile", "项目安全设计要求为 true")

    safety = raw.get("safety", {})
    min_delay = float(safety.get("min_delay_seconds", 0))
    max_delay = float(safety.get("max_delay_seconds", min_delay))
    _require(min_delay >= 0, "safety.min_delay_seconds", "必须 >= 0")
    _require(max_delay >= min_delay, "safety.max_delay_seconds", "必须 >= min_delay_seconds")
    _require(int(safety.get("max_consecutive_errors", 1)) >= 1, "safety.max_consecutive_errors", "必须 >= 1")
    _require(int(safety.get("scroll_idle_rounds", 1)) >= 1, "safety.scroll_idle_rounds", "必须 >= 1")
    _require(int(safety.get("scroll_max_rounds", 1)) >= 1, "safety.scroll_max_rounds", "必须 >= 1")
    for key in ["max_notes_per_run", "max_page_visits_per_run"]:
        value = safety.get(key)
        _require(value is None or int(value) >= 1, f"safety.{key}", "必须为 null 或 >= 1")
    runtime = safety.get("max_runtime_minutes")
    _require(runtime is None or float(runtime) > 0, "safety.max_runtime_minutes", "必须为 null 或 > 0")

    collection = raw.get("collection", {})
    smoke_limit = int(collection.get("smoke_note_limit", 1))
    smoke_attempts = int(collection.get("smoke_max_attempts", smoke_limit))
    _require(smoke_limit >= 1, "collection.smoke_note_limit", "必须 >= 1")
    _require(smoke_attempts >= smoke_limit, "collection.smoke_max_attempts", "必须 >= smoke_note_limit")
    _require(int(collection.get("collect_top_comments", 0)) >= 0, "collection.collect_top_comments", "必须 >= 0")
    _require(collection.get("download_media") is False, "collection.download_media", "当前范围要求保持 false")
