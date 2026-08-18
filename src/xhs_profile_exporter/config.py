from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


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
    return AppConfig(base_dir=base_dir, raw=raw, creators=creators)

