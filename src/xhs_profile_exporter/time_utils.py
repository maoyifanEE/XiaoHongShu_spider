from __future__ import annotations

from datetime import datetime, timedelta
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

