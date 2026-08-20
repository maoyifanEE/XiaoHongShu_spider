from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE_KEYS = {
    "cookie",
    "cookies",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "auth",
    "session",
    "session_id",
    "password",
    "passwd",
    "xsec_token",
}

SENSITIVE_PREFIXES = ("token_", "xsec_", "credential", "secret")
SENSITIVE_SUFFIXES = ("_token", "_cookie", "_session", "_password", "_passwd", "_secret", "_credential")


def is_sensitive_key(key: str) -> bool:
    key_text = str(key).strip().lower().replace("-", "_")
    if key_text in SENSITIVE_KEYS:
        return True
    return key_text.startswith(SENSITIVE_PREFIXES) or key_text.endswith(SENSITIVE_SUFFIXES)


def ensure_dirs(base_dir: Path) -> None:
    for rel in [
        "browser_profile",
        "config",
        "data/raw/profile",
        "data/raw/notes",
        "data/checkpoints",
        "data/backups",
        "logs",
        "output",
        "screenshots/errors",
    ]:
        (base_dir / rel).mkdir(parents=True, exist_ok=True)


def setup_logging(base_dir: Path, run_id: str | None = None) -> logging.Logger:
    ensure_dirs(base_dir)
    logger = logging.getLogger("xhs_profile_exporter")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    log_name = f"{run_id or 'startup'}.log"
    file_handler = logging.FileHandler(base_dir / "logs" / log_name, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    return logger


def sanitize_url(url: str | None) -> str | None:
    if not url:
        return url
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return redact_sensitive_text(url)
    kept = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if not is_sensitive_key(key):
            kept.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def redact_sensitive_text(text: str) -> str:
    redacted = text
    redacted = re.sub(
        r"(?i)(?:xsec_token|token|authorization|session|cookie|password|bearer)=([^&\s]+)",
        "[REDACTED_CREDENTIAL]",
        redacted,
    )
    redacted = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "[REDACTED_CREDENTIAL]", redacted)
    return redacted


def canonical_profile_url(user_id: str) -> str:
    return f"https://www.xiaohongshu.com/user/profile/{user_id}"


def canonical_note_url(note_id: str) -> str:
    return f"https://www.xiaohongshu.com/explore/{note_id}"


def parse_count(raw: Any) -> tuple[int | None, str | None, bool | None]:
    if raw is None:
        return None, None, None
    text = str(raw).strip().replace(",", "")
    if not text or text in {"-", "赞", "收藏", "评论", "分享"}:
        return None, text or None, None
    if re.fullmatch(r"\d+", text):
        return int(text), text, True
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(万|w|W)", text)
    if match:
        value = int(float(match.group(1)) * 10000)
        return value, text, False
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(千|k|K)", text)
    if match:
        value = int(float(match.group(1)) * 1000)
        return value, text, False
    return None, text, None


def stable_hash(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if is_sensitive_key(str(key)):
                continue
            else:
                clean[key] = sanitize_json(item)
        return clean
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    if isinstance(value, str):
        if "://" in value:
            value = sanitize_url(value) or value
        value = redact_sensitive_text(value)
        if len(value) > 4000:
            return value[:4000] + "...[TRUNCATED]"
        return value
    return value


def safe_filename(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", text).strip()
    return cleaned or "xhs_export"
