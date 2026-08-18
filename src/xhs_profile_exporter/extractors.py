from __future__ import annotations

import json
import re
from typing import Any

from .time_utils import now_iso, normalize_relative_time
from .utils import canonical_note_url, canonical_profile_url, parse_count, sanitize_url

NOTE_ID_RE = re.compile(r"(?:explore|discovery/item)/([0-9a-fA-F]{24})")
HEX_NOTE_RE = re.compile(r"\b[0-9a-fA-F]{24}\b")


def extract_user_id(url: str) -> str:
    match = re.search(r"/user/profile/([^/?#]+)", url)
    if not match:
        raise ValueError(f"无法从 URL 提取 user_id: {url}")
    return match.group(1)


def extract_note_id(url_or_text: str) -> str | None:
    match = NOTE_ID_RE.search(url_or_text)
    if match:
        return match.group(1)
    match = HEX_NOTE_RE.search(url_or_text)
    return match.group(0) if match else None


def note_type_from_text(text: str) -> str | None:
    lowered = text.lower()
    if "视频" in text or "video" in lowered:
        return "视频"
    if "图文" in text or "图片" in text or "image" in lowered:
        return "图文"
    return None


async def extract_profile_dom(page: Any, creator_url: str, configured_name: str | None = None) -> dict[str, Any]:
    user_id = extract_user_id(creator_url)
    data = await page.evaluate(
        """
        () => {
          const text = document.body ? document.body.innerText : "";
          const meta = Object.fromEntries(Array.from(document.querySelectorAll("meta")).map(m => [m.getAttribute("name") || m.getAttribute("property") || "", m.getAttribute("content") || ""]));
          const img = document.querySelector("img.avatar, .avatar img, [class*=avatar] img");
          const h1 = document.querySelector("h1");
          return {
            text,
            title: document.title || "",
            meta,
            h1: h1 ? h1.innerText : "",
            avatar_url: img ? img.src : null
          };
        }
        """
    )
    text = data.get("text") or ""
    nickname = data.get("h1") or configured_name or _first_non_empty(data.get("title", "").split("-"))
    numbers = _extract_labeled_counts(text, ["关注", "粉丝", "获赞与收藏", "获赞", "收藏"])
    followers = parse_count(numbers.get("粉丝"))
    following = parse_count(numbers.get("关注"))
    total_raw = numbers.get("获赞与收藏") or numbers.get("获赞") or numbers.get("收藏")
    total_interactions = parse_count(total_raw)
    bio = _extract_bio(text, nickname)
    return {
        "captured_at": now_iso(),
        "nickname": nickname,
        "xhs_id": _extract_xhs_id(text),
        "canonical_url": canonical_profile_url(user_id),
        "avatar_url": sanitize_url(data.get("avatar_url")),
        "ip_location": _extract_after_label(text, "IP属地"),
        "bio": bio,
        "profile_tags": _extract_tags(text),
        "identity_tags": [],
        "following_value": following[0],
        "following_raw": following[1],
        "following_is_exact": following[2],
        "followers_value": followers[0],
        "followers_raw": followers[1],
        "followers_is_exact": followers[2],
        "total_interactions_value": total_interactions[0],
        "total_interactions_raw": total_interactions[1],
        "total_interactions_is_exact": total_interactions[2],
        "gender": None,
        "raw_json": {"title": data.get("title"), "meta": data.get("meta")},
        "source": "dom",
    }


async def discover_note_cards(page: Any) -> list[dict[str, Any]]:
    cards = await page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href*="/explore/"], a[href*="/discovery/item/"]')).map((a, index) => {
          const card = a.closest('[class*=note], [class*=card], section, div') || a;
          return {
            index,
            href: a.href,
            text: card.innerText || a.innerText || "",
            aria: a.getAttribute("aria-label") || "",
            classes: card.className || "",
          };
        })
        """
    )
    results = []
    seen = set()
    for item in cards:
        note_id = extract_note_id(item.get("href") or "")
        if not note_id or note_id in seen:
            continue
        seen.add(note_id)
        text = item.get("text") or item.get("aria") or ""
        results.append(
            {
                "note_id": note_id,
                "access_url": item.get("href"),
                "canonical_url": canonical_note_url(note_id),
                "is_pinned": "置顶" in text,
                "note_type": note_type_from_text(text),
                "card_text": text[:1000],
            }
        )
    return results


async def extract_note_dom(page: Any, note_id: str, top_n: int = 3) -> dict[str, Any]:
    data = await page.evaluate(
        """
        () => {
          const text = document.body ? document.body.innerText : "";
          const titleEl = document.querySelector("h1, [class*=title]");
          const descEl = document.querySelector('[class*=desc], [class*=content], .note-content');
          const meta = Object.fromEntries(Array.from(document.querySelectorAll("meta")).map(m => [m.getAttribute("name") || m.getAttribute("property") || "", m.getAttribute("content") || ""]));
          const comments = Array.from(document.querySelectorAll('[class*=comment-item], [class*=commentItem], [class*=comment]')).slice(0, 12).map((el) => ({
             text: el.innerText || "",
             id: el.getAttribute("data-id") || el.id || null,
             classes: el.className || ""
          }));
          return {text, title: titleEl ? titleEl.innerText : "", desc: descEl ? descEl.innerText : "", meta, comments, url: location.href};
        }
        """
    )
    text = data.get("text") or ""
    unavailable_status = _detect_unavailable_status(text)
    title = _clean_line(data.get("title")) or _meta_title(data.get("meta", {}))
    body = _extract_body(data.get("desc") or text, title)
    hashtags = _extract_tags(body or text)
    publish_raw = _extract_publish_time(text)
    publish_time, publish_time_raw = normalize_relative_time(publish_raw)
    metrics = _extract_note_metrics(text)
    comments = _extract_comments(data.get("comments") or [], top_n)
    return {
        "note_id": note_id,
        "canonical_url": canonical_note_url(note_id),
        "is_pinned": "置顶" in text[:300],
        "note_type": note_type_from_text(text) or ("视频" if "video" in json.dumps(data.get("meta", {})).lower() else "图文"),
        "title": title,
        "body": body,
        "hashtags": hashtags,
        "publish_time": publish_time,
        "publish_time_raw": publish_time_raw,
        "updated_time": None,
        "updated_time_raw": None,
        "status": unavailable_status[0] if unavailable_status else ("OK" if body or title else "PARSE_PARTIAL"),
        "status_note": unavailable_status[1] if unavailable_status else (None if body or title else "DOM 未提取到标题或正文"),
        "top_comments": comments,
        "raw_json": {"meta": data.get("meta"), "url": sanitize_url(data.get("url"))},
        "source": "dom",
        **metrics,
    }


def merge_note_with_structured(note: dict[str, Any], structured: dict[str, Any] | None) -> dict[str, Any]:
    if not structured:
        return note
    candidates = _find_dicts_with_note_id(structured, note["note_id"])
    for item in candidates:
        title = item.get("title") or item.get("display_title")
        desc = item.get("desc") or item.get("content")
        note["title"] = note.get("title") or title
        note["body"] = note.get("body") or desc
        note["note_type"] = note.get("note_type") or _structured_note_type(item)
        for key, out in [
            ("liked_count", "likes"),
            ("collected_count", "collects"),
            ("comment_count", "comments"),
            ("share_count", "shares"),
        ]:
            if item.get(key) is not None and note.get(f"{out}_value") is None:
                value, raw, exact = parse_count(item.get(key))
                note[f"{out}_value"] = value
                note[f"{out}_raw"] = raw
                note[f"{out}_is_exact"] = exact
        if item.get("time") and not note.get("publish_time"):
            note["publish_time"] = str(item.get("time"))
            note["publish_time_raw"] = str(item.get("time"))
        break
    return note


def _find_dicts_with_note_id(value: Any, note_id: str) -> list[dict[str, Any]]:
    found = []
    if isinstance(value, dict):
        if note_id in json.dumps(value, ensure_ascii=False):
            if value.get("id") == note_id or value.get("note_id") == note_id:
                found.append(value)
            for item in value.values():
                found.extend(_find_dicts_with_note_id(item, note_id))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_dicts_with_note_id(item, note_id))
    return found


def _structured_note_type(item: dict[str, Any]) -> str | None:
    for key in ("type", "note_type", "model_type"):
        value = str(item.get(key) or "")
        if value:
            return note_type_from_text(value) or value
    return None


def _extract_note_metrics(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label, key in [("点赞", "likes"), ("收藏", "collects"), ("评论", "comments"), ("分享", "shares")]:
        raw = _extract_metric_around_label(text, label)
        value, raw_display, exact = parse_count(raw)
        result[f"{key}_value"] = value
        result[f"{key}_raw"] = raw_display
        result[f"{key}_is_exact"] = exact
    return result


def _detect_unavailable_status(text: str) -> tuple[str, str] | None:
    if "当前笔记暂时无法浏览" in text or "暂时无法浏览" in text:
        return "ACCESS_RESTRICTED", "页面明确显示当前笔记暂时无法浏览"
    if "笔记不存在" in text or "内容不存在" in text:
        return "NOT_FOUND", "页面明确显示笔记不存在"
    if "已被删除" in text:
        return "NO_LONGER_PUBLIC", "页面明确显示内容已被删除"
    return None


def _extract_metric_around_label(text: str, label: str) -> str | None:
    patterns = [
        rf"{label}\s*([0-9]+(?:\.[0-9]+)?\s*(?:万|千|w|W|k|K)?)",
        rf"([0-9]+(?:\.[0-9]+)?\s*(?:万|千|w|W|k|K)?)\s*{label}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).replace(" ", "")
    return None


def _extract_comments(items: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    comments = []
    seen = set()
    for item in items:
        text = _clean_multiline(item.get("text") or "")
        if not text or text in seen:
            continue
        if any(word in text for word in ["全部评论", "暂无评论", "展开", "回复"]):
            continue
        seen.add(text)
        lines = [line for line in text.splitlines() if line.strip()]
        author = lines[0] if lines else None
        body = "\n".join(lines[1:]) if len(lines) > 1 else text
        like_raw = _extract_metric_around_label(text, "赞")
        value, raw, exact = parse_count(like_raw)
        comments.append(
            {
                "rank": len(comments) + 1,
                "comment_id": item.get("id"),
                "author_name": author,
                "body": body,
                "likes_value": value,
                "likes_raw": raw,
                "likes_is_exact": exact,
                "is_creator": "作者" in text,
                "is_pinned": "置顶" in text,
                "sorting_mode": "default_ui_order",
                "source": "dom",
            }
        )
        if len(comments) >= top_n:
            break
    return comments


def _extract_labeled_counts(text: str, labels: list[str]) -> dict[str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    found: dict[str, str] = {}
    for i, line in enumerate(lines):
        for label in labels:
            if line == label and i > 0:
                found[label] = lines[i - 1]
            elif line.startswith(label):
                match = re.search(r"([0-9]+(?:\.[0-9]+)?\s*(?:万|千|w|W|k|K)?)", line)
                if match:
                    found[label] = match.group(1).replace(" ", "")
    return found


def _extract_xhs_id(text: str) -> str | None:
    match = re.search(r"小红书号[:：]\s*([A-Za-z0-9_.-]+)", text)
    return match.group(1) if match else None


def _extract_after_label(text: str, label: str) -> str | None:
    match = re.search(rf"{label}[:：]?\s*([^\n]+)", text)
    return match.group(1).strip() if match else None


def _extract_bio(text: str, nickname: str | None) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    ignored = {"关注", "粉丝", "获赞与收藏", "笔记", "收藏", "点赞"}
    for line in lines[:30]:
        if line == nickname or line in ignored or line.startswith("小红书号"):
            continue
        if len(line) > 3 and not re.fullmatch(r"[0-9.万千wWkK]+", line):
            return line
    return None


def _extract_tags(text: str | None) -> list[str]:
    if not text:
        return []
    return sorted(set(re.findall(r"#[\w\u4e00-\u9fff-]+", text)))


def _extract_publish_time(text: str) -> str | None:
    patterns = [
        r"\d{4}[-./]\d{1,2}[-./]\d{1,2}",
        r"\d{1,2}[-./]\d{1,2}",
        r"昨天",
        r"\d+天前",
        r"\d+小时前",
        r"\d+分钟前",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None


def _extract_body(text: str, title: str | None) -> str | None:
    cleaned = _clean_multiline(text)
    if title and cleaned.startswith(title):
        cleaned = cleaned[len(title):].strip()
    return cleaned or None


def _meta_title(meta: dict[str, str]) -> str | None:
    for key in ("og:title", "twitter:title", "description"):
        if meta.get(key):
            return _clean_line(meta[key])
    return None


def _clean_line(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip() or None


def _clean_multiline(value: str | None) -> str:
    if not value:
        return ""
    lines = [line.strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _first_non_empty(items: list[str]) -> str | None:
    for item in items:
        clean = _clean_line(item)
        if clean:
            return clean
    return None
