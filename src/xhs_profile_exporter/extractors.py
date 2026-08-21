from __future__ import annotations

import json
import re
from typing import Any

from .time_utils import now_iso, normalize_publish_time_value, normalize_relative_time
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
    if text in {"normal", "image", "images"}:
        return "图文"
    if text in {"video"}:
        return "视频"
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
        (noteId) => {
          const meta = Object.fromEntries(Array.from(document.querySelectorAll("meta")).map(m => [m.getAttribute("name") || m.getAttribute("property") || "", m.getAttribute("content") || ""]));
          const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
          };
          const detailSelector = '[class*="note-detail"], [class*="noteDetail"], [class*="NoteDetail"], [data-testid*="note-detail"], [role="dialog"]';
          const evidenceSelectors = '#detail-title, #detail-desc, .engage-bar, [class*=engage], [class*=interaction], [class*=Interact]';
          const detailRoots = Array.from(document.querySelectorAll(detailSelector)).filter(visible);
          const evidence = Array.from(document.querySelectorAll(evidenceSelectors)).filter(visible);
          const evidenceRoot = evidence.map((el) => el.closest(detailSelector)).find((el) => el && visible(el));
          const root = evidenceRoot || detailRoots.find((el) => {
            const hasEvidence = Boolean(el.querySelector(evidenceSelectors));
            const hasExactLink = noteId ? Boolean(el.querySelector(`a[href*="${noteId}"]`)) : false;
            return hasEvidence || hasExactLink;
          });
          if (!root) {
            return {
              rootFound: false,
              rootReason: "NO_STRONG_DETAIL_ROOT",
              meta,
              url: location.href,
              diagnostics: {detailRootCount: detailRoots.length, evidenceCount: evidence.length}
            };
          }
          const q = (selectors) => selectors.map((sel) => root.querySelector(sel)).find(Boolean);
          const titleEl = q(["#detail-title", "h1", "[class*=title]", "[data-testid*=title]"]);
          const descEl = q(["#detail-desc", "[class*=desc]", "[class*=content]", ".note-content", "[data-testid*=content]"]);
          const clean = (el) => el ? (el.innerText || el.textContent || "").trim() : "";
          const metricText = (selectors) => {
            const el = q(selectors);
            const count = el ? el.querySelector(".count, [class*=count], [class*=Count]") : null;
            return clean(count || el);
          };
          const domMetrics = {
            likes: metricText([".engage-bar .like-wrapper", ".engage-bar [class*=like-wrapper]", ".engage-bar [class*=likeWrapper]", ".engage-bar [class*=Like]"]),
            collects: metricText([".engage-bar .collect-wrapper", ".engage-bar [class*=collect-wrapper]", ".engage-bar [class*=collectWrapper]", ".engage-bar [class*=Collect]"]),
            comments: metricText([".engage-bar .chat-wrapper", ".engage-bar [class*=chat-wrapper]", ".engage-bar [class*=comment-wrapper]", ".engage-bar [class*=Chat]", ".engage-bar [class*=Comment]"]),
            shares: metricText([".engage-bar .share-wrapper", ".engage-bar [class*=share-wrapper]", ".engage-bar [class*=shareWrapper]", ".engage-bar [class*=Share]"])
          };
          const tagNames = Array.from(root.querySelectorAll('#detail-desc a[href*="search"], #detail-desc a[href*="search_result"], a[href*="/search_result"], a[href*="/search"]'))
            .map((el) => clean(el).replace(/^#/, ""))
            .filter(Boolean);
          const commentEls = Array.from(root.querySelectorAll('[class*=comment-item], [class*=commentItem], [data-testid*=comment]')).slice(0, 20);
          const comments = commentEls.map((el) => {
             const childComment = el.parentElement && el.parentElement.closest('[class*=comment-item], [class*=commentItem], [data-testid*=comment]') !== el;
             return {
               text: el.innerText || "",
               id: el.getAttribute("data-id") || el.id || null,
               classes: el.className || "",
               nested: Boolean(childComment),
             };
          });
          return {
            rootFound: true,
            rootReason: evidenceRoot ? "DETAIL_EVIDENCE_ROOT" : "DETAIL_WRAPPER_ROOT",
            text: root.innerText || "",
            title: titleEl ? titleEl.innerText : "",
            desc: descEl ? descEl.innerText : "",
            domMetrics,
            tagNames,
            meta,
            comments,
            url: location.href
          };
        }
        """,
        note_id,
    )
    if not data.get("rootFound"):
        return _empty_note(note_id, "PARSE_PARTIAL", "未找到可靠的可见笔记详情 root", data)
    text = data.get("text") or ""
    unavailable_status = _detect_unavailable_status(text)
    title = _clean_line(data.get("title")) or _meta_title(data.get("meta", {}))
    body = _extract_body(data.get("desc") or "", title)
    hashtags = merge_tags(data.get("tagNames") or [], _extract_tags(body))
    publish_raw = _extract_publish_time(text)
    publish_time, publish_time_raw = normalize_relative_time(publish_raw)
    metrics = _extract_note_metrics(data.get("domMetrics") or {})
    comments = _extract_comments(data.get("comments") or [], top_n)
    return {
        "note_id": note_id,
        "canonical_url": canonical_note_url(note_id),
        "is_pinned": "置顶" in text[:300],
        "note_type": note_type_from_text(text) or ("视频" if "video" in json.dumps(data.get("meta", {})).lower() else None),
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
        "raw_json": {"meta": data.get("meta"), "url": sanitize_url(data.get("url")), "root_reason": data.get("rootReason")},
        "source": "dom",
        "field_sources": _note_field_sources(
            {
                "title": title,
                "body": body,
                "note_type": note_type_from_text(text) or ("视频" if "video" in json.dumps(data.get("meta", {})).lower() else None),
                "publish_time": publish_time,
                "like_count": metrics.get("likes_value"),
                "collect_count": metrics.get("collects_value"),
                "comment_count": metrics.get("comments_value"),
                "share_count": metrics.get("shares_value"),
                "tags": hashtags,
            },
            "DOM_EXACT",
        ),
        **metrics,
    }


def merge_note_with_structured(note: dict[str, Any], structured: dict[str, Any] | None) -> dict[str, Any]:
    if not structured:
        return note
    original_field_sources = structured.get("_field_sources") if isinstance(structured.get("_field_sources"), dict) else {}
    structured = normalize_public_note_record(structured, note["note_id"]) or structured
    structured_field_sources = original_field_sources
    field_sources = dict(note.get("field_sources") or {})
    candidates = _find_dicts_with_note_id(structured, note["note_id"])
    for item in candidates:
        title = item.get("title") or item.get("display_title")
        desc = item.get("desc") or item.get("content")
        if not note.get("title") and title:
            note["title"] = title
            field_sources["title"] = _structured_field_source(structured_field_sources, "title")
        if not note.get("body") and desc:
            note["body"] = desc
            field_sources["body"] = _structured_field_source(structured_field_sources, "body")
        structured_type = _structured_note_type(item)
        if not note.get("note_type") and structured_type:
            note["note_type"] = structured_type
            field_sources["note_type"] = _structured_field_source(structured_field_sources, "note_type")
        for key, out in [
            ("liked_count", "likes"),
            ("collected_count", "collects"),
            ("comment_count", "comments"),
            ("share_count", "shares"),
        ]:
            if item.get(key) is None:
                continue
            state_value, state_raw, state_exact = parse_count(item.get(key))
            current_value = note.get(f"{out}_value")
            current_exact = note.get(f"{out}_is_exact")
            if current_value is None:
                note[f"{out}_value"] = state_value
                note[f"{out}_raw"] = state_raw
                note[f"{out}_is_exact"] = state_exact
                field_sources[_metric_field_name(out)] = _structured_field_source(structured_field_sources, _metric_field_name(out))
            elif state_value is not None and current_value != state_value:
                note.setdefault("raw_json", {}).setdefault("metric_source_mismatch", []).append(
                    {"field": _metric_field_name(out), "dom_value": current_value, "state_value": state_value}
                )
                if state_exact is True and current_exact is not True:
                    note[f"{out}_value"] = state_value
                    note[f"{out}_is_exact"] = True
                    field_sources[_metric_field_name(out)] = _structured_field_source(structured_field_sources, _metric_field_name(out))
        publish_value = item.get("publish_time") or item.get("time")
        if publish_value is not None and not note.get("publish_time"):
            note["publish_time"], note["publish_time_raw"] = normalize_publish_time_value(publish_value)
            field_sources["publish_time"] = _structured_field_source(structured_field_sources, "publish_time")
        if item.get("tags"):
            before_tags = note.get("hashtags") or []
            merged_tags = merge_tags(note.get("hashtags") or [], item.get("tags") or [])
            structured_tag_source = _structured_field_source(structured_field_sources, "tags")
            if merged_tags != before_tags:
                note["hashtags"] = merged_tags
                field_sources["tags"] = structured_tag_source if not before_tags else f"DOM_EXACT+{structured_tag_source}"
            elif item.get("tags") and not before_tags:
                field_sources["tags"] = structured_tag_source
        break
    note["field_sources"] = _normalize_note_field_sources(note, field_sources)
    return note


def _structured_field_source(field_sources: dict[str, Any], key: str) -> str:
    return str(field_sources.get(key) or "INITIAL_STATE")


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


def normalize_public_note_record(value: dict[str, Any], note_id: str) -> dict[str, Any] | None:
    candidate_id = value.get("note_id") or value.get("id") or value.get("noteId")
    if candidate_id and candidate_id != note_id:
        return None
    record: dict[str, Any] = {"note_id": note_id}
    for source_key, out_key in [
        ("id", "id"),
        ("note_id", "note_id"),
        ("noteId", "note_id"),
        ("title", "title"),
        ("display_title", "display_title"),
        ("displayTitle", "display_title"),
        ("desc", "desc"),
        ("content", "content"),
        ("type", "type"),
        ("note_type", "note_type"),
        ("noteType", "note_type"),
        ("model_type", "model_type"),
        ("modelType", "model_type"),
        ("time", "time"),
        ("publish_time", "publish_time"),
        ("publishTime", "publish_time"),
    ]:
        if source_key in value and not isinstance(value.get(source_key), (dict, list)):
            record[out_key] = value.get(source_key)
    interact = value.get("interactInfo") if isinstance(value.get("interactInfo"), dict) else {}
    for out_key, aliases in {
        "liked_count": ["liked_count", "likedCount"],
        "collected_count": ["collected_count", "collectedCount"],
        "comment_count": ["comment_count", "commentCount"],
        "share_count": ["share_count", "shareCount"],
    }.items():
        for alias in aliases:
            if alias in value and not isinstance(value.get(alias), (dict, list)):
                record[out_key] = value.get(alias)
                break
            if alias in interact and not isinstance(interact.get(alias), (dict, list)):
                record[out_key] = interact.get(alias)
                break
    record["tags"] = normalize_tag_names(value.get("tagList") or value.get("tags"))
    return {key: item for key, item in record.items() if item not in (None, "", [])}


def normalize_tag_names(value: Any) -> list[str]:
    tags: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("tagName") or item.get("title")
            else:
                name = item
            if name:
                tags.append(str(name).strip().lstrip("#"))
    elif isinstance(value, str):
        tags.extend(part.strip().lstrip("#") for part in re.split(r"[,，\s]+", value) if part.strip())
    return sorted({tag for tag in tags if tag})


def merge_tags(*tag_groups: Any) -> list[str]:
    tags: list[str] = []
    for group in tag_groups:
        if isinstance(group, str):
            tags.append(group)
        elif isinstance(group, list):
            tags.extend(str(item) for item in group if item)
    return sorted({tag.strip().lstrip("#") for tag in tags if tag and tag.strip().lstrip("#")})


def _extract_note_metrics(dom_metrics: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ["likes", "collects", "comments", "shares"]:
        raw = dom_metrics.get(key)
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
        if item.get("nested"):
            continue
        text = _clean_multiline(item.get("text") or "")
        if not text or text in seen:
            continue
        if any(word in text for word in ["全部评论", "暂无评论"]):
            continue
        seen.add(text)
        lines = [line for line in text.splitlines() if line.strip()]
        author = lines[0] if lines else None
        body_lines = [
            line
            for line in lines[1:]
            if line not in {"回复", "点赞", "展开回复"} and not line.startswith("展开") and not line.endswith("回复")
        ]
        body = "\n".join(body_lines) if body_lines else (lines[1] if len(lines) > 1 else text)
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


def _empty_note(note_id: str, status: str, status_note: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "note_id": note_id,
        "canonical_url": canonical_note_url(note_id),
        "is_pinned": None,
        "note_type": None,
        "title": None,
        "body": None,
        "hashtags": [],
        "publish_time": None,
        "publish_time_raw": None,
        "updated_time": None,
        "updated_time_raw": None,
        "status": status,
        "status_note": status_note,
        "top_comments": [],
        "raw_json": {"url": sanitize_url((raw or {}).get("url"))},
        "source": "dom",
        "field_sources": _note_field_sources({}, "DOM_EXACT"),
        "likes_value": None,
        "likes_raw": None,
        "likes_is_exact": None,
        "collects_value": None,
        "collects_raw": None,
        "collects_is_exact": None,
        "comments_value": None,
        "comments_raw": None,
        "comments_is_exact": None,
        "shares_value": None,
        "shares_raw": None,
        "shares_is_exact": None,
    }


def _metric_field_name(metric_prefix: str) -> str:
    return {
        "likes": "like_count",
        "collects": "collect_count",
        "comments": "comment_count",
        "shares": "share_count",
    }[metric_prefix]


def _note_field_sources(values: dict[str, Any], source: str) -> dict[str, str]:
    return {field: (source if _field_present(values.get(field)) else "MISSING") for field in NOTE_COMPLETENESS_FIELDS}


def _normalize_note_field_sources(note: dict[str, Any], field_sources: dict[str, str]) -> dict[str, str]:
    values = note_completeness_values(note)
    return {
        field: field_sources.get(field, "MISSING") if _field_present(values.get(field)) else "MISSING"
        for field in NOTE_COMPLETENESS_FIELDS
    }


NOTE_COMPLETENESS_FIELDS = [
    "title",
    "body",
    "note_type",
    "publish_time",
    "like_count",
    "collect_count",
    "comment_count",
    "share_count",
    "tags",
]


def note_completeness_values(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": note.get("title"),
        "body": note.get("body"),
        "note_type": note.get("note_type"),
        "publish_time": note.get("publish_time"),
        "like_count": note.get("likes_value"),
        "collect_count": note.get("collects_value"),
        "comment_count": note.get("comments_value"),
        "share_count": note.get("shares_value"),
        "tags": note.get("hashtags"),
    }


def _field_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


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
