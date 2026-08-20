from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")


def now_shanghai() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now_shanghai().isoformat(timespec="seconds")


def normalize_relative_time(raw: str | None, captured_at: datetime | None = None) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    text = raw.strip()
    if not text:
        return None, raw
    base = captured_at or now_shanghai()
    try:
        if text in {"刚刚", "刚才"}:
            return base.isoformat(timespec="seconds"), raw
        if text == "昨天":
            return (base - timedelta(days=1)).date().isoformat(), raw
        if text.endswith("天前"):
            return (base - timedelta(days=int(text[:-2]))).date().isoformat(), raw
        if text.endswith("小时前"):
            return (base - timedelta(hours=int(text[:-3]))).isoformat(timespec="seconds"), raw
        if text.endswith("分钟前"):
            return (base - timedelta(minutes=int(text[:-3]))).isoformat(timespec="seconds"), raw
    except ValueError:
        return None, raw
    return text, raw


def normalize_publish_time_value(raw: Any, captured_at: datetime | None = None) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    raw_text = str(raw).strip()
    if not raw_text:
        return None, raw_text
    if raw_text.isdigit():
        try:
            timestamp = int(raw_text)
        except ValueError:
            return None, raw_text
        digit_len = len(raw_text)
        if digit_len == 10:
            return datetime.fromtimestamp(timestamp, TZ).isoformat(timespec="seconds"), raw_text
        if digit_len == 13:
            return datetime.fromtimestamp(timestamp / 1000, TZ).isoformat(timespec="seconds"), raw_text
        return None, raw_text
    return normalize_relative_time(raw_text, captured_at=captured_at)
