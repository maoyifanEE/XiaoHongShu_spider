import asyncio

from playwright.async_api import async_playwright

from xhs_profile_exporter.extractors import _extract_comments, extract_note_dom, merge_note_with_structured, normalize_public_note_record


NOTE_ID = "66dabcde000000001f01abcd"


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


def test_generic_main_recommendation_content_is_not_detail_body():
    html = """
    <main style="display:block;width:800px;height:600px">
      <h1>推荐标题</h1>
      <p>推荐侧栏正文不应该进入目标笔记正文</p>
      <span>点赞 9999</span>
    </main>
    """
    note = asyncio.run(_extract_from_html(html))
    assert note["status"] == "PARSE_PARTIAL"
    assert note["body"] is None
    assert note["likes_value"] is None


def test_article_shell_without_detail_evidence_is_not_detail_root():
    html = """
    <article style="display:block;width:800px;height:600px">
      <h1>普通文章壳</h1>
      <p>这不是明确的小红书详情容器</p>
    </article>
    """
    note = asyncio.run(_extract_from_html(html))
    assert note["status"] == "PARSE_PARTIAL"
    assert note["title"] is None


def test_strong_detail_root_extracts_fields():
    html = """
    <section class="note-detail" style="display:block;width:800px;height:600px">
      <h1 id="detail-title">目标标题</h1>
      <div id="detail-desc">目标正文 #测试标签</div>
      <div class="engage-bar">
        <div class="like-wrapper"><span class="count">12</span></div>
        <div class="collect-wrapper"><span class="count">3</span></div>
        <div class="chat-wrapper"><span class="count">4</span></div>
        <div class="share-wrapper"><span class="count">5</span></div>
      </div>
      <time>2026-08-20</time>
    </section>
    """
    note = asyncio.run(_extract_from_html(html))
    assert note["status"] == "OK"
    assert note["title"] == "目标标题"
    assert note["body"] == "目标正文 #测试标签"
    assert note["likes_value"] == 12
    assert note["collects_value"] == 3
    assert note["comments_value"] == 4
    assert note["shares_value"] == 5
    assert note["hashtags"] == ["测试标签"]


def test_label_only_comment_metric_does_not_infer_zero():
    html = """
    <section class="note-detail" style="display:block;width:800px;height:600px">
      <h1 id="detail-title">目标标题</h1>
      <div id="detail-desc">目标正文</div>
      <div class="engage-bar">
        <div class="chat-wrapper"><span class="count">评论</span></div>
      </div>
    </section>
    """
    note = asyncio.run(_extract_from_html(html))
    assert note["comments_value"] is None
    assert note["field_sources"].get("comment_count") == "MISSING"
    assert note["comment_zero_evidence"] is False


def test_scoped_empty_comment_state_infers_exact_zero():
    html = """
    <section class="note-detail" style="display:block;width:800px;height:600px">
      <h1 id="detail-title">目标标题</h1>
      <div id="detail-desc">目标正文</div>
      <div class="engage-bar">
        <div class="chat-wrapper"><span class="count">评论</span></div>
      </div>
      <div class="comments-empty">这是一片荒地点击评论</div>
    </section>
    """
    note = asyncio.run(_extract_from_html(html))
    assert note["comments_value"] == 0
    assert note["comments_raw"] == "0"
    assert note["comments_is_exact"] is True
    assert note["field_sources"].get("comment_count") == "DOM_EXACT"
    assert note["comment_zero_evidence"] is True
    assert note["comment_zero_evidence_text"] == "这是一片荒地点击评论"


def test_numeric_comment_metric_wins_without_empty_state():
    html = """
    <section class="note-detail" style="display:block;width:800px;height:600px">
      <h1 id="detail-title">目标标题</h1>
      <div id="detail-desc">目标正文</div>
      <div class="engage-bar">
        <div class="chat-wrapper"><span class="count">3</span></div>
      </div>
    </section>
    """
    note = asyncio.run(_extract_from_html(html))
    assert note["comments_value"] == 3
    assert note["comments_is_exact"] is True


def test_empty_comment_text_outside_detail_root_does_not_infer_zero():
    html = """
    <aside style="display:block;width:800px;height:100px">暂无评论</aside>
    <section class="note-detail" style="display:block;width:800px;height:600px">
      <h1 id="detail-title">目标标题</h1>
      <div id="detail-desc">目标正文</div>
      <div class="engage-bar">
        <div class="chat-wrapper"><span class="count">评论</span></div>
      </div>
    </section>
    """
    note = asyncio.run(_extract_from_html(html))
    assert note["comments_value"] is None
    assert note["field_sources"].get("comment_count") == "MISSING"
    assert note["comment_zero_evidence"] is False


def test_sidebar_fake_count_does_not_pollute_note_metrics():
    html = """
    <section class="note-detail" style="display:block;width:800px;height:600px">
      <h1 id="detail-title">目标标题</h1>
      <div id="detail-desc">目标正文</div>
      <div class="engage-bar">
        <div class="like-wrapper"><span class="count">1</span></div>
        <div class="collect-wrapper"><span class="count">2</span></div>
        <div class="chat-wrapper"><span class="count">3</span></div>
        <div class="share-wrapper"><span class="count">4</span></div>
      </div>
    </section>
    <aside style="display:block;width:300px;height:600px">
      <div class="recommendation">
        <div class="like-wrapper"><span class="count">999万</span></div>
        <div class="collect-wrapper"><span class="count">888万</span></div>
      </div>
    </aside>
    """
    note = asyncio.run(_extract_from_html(html))
    assert note["likes_value"] == 1
    assert note["collects_value"] == 2
    assert note["comments_value"] == 3
    assert note["shares_value"] == 4


def test_normalize_exact_note_state_nested_interact_info():
    record = normalize_public_note_record(
        {
            "title": "T",
            "desc": "B",
            "type": "normal",
            "time": 1234567890000,
            "interactInfo": {
                "likedCount": "123",
                "collectedCount": "45",
                "commentCount": "6",
                "shareCount": "7",
                "liked": True,
            },
            "tagList": [{"name": "杭州"}, {"name": "旅行"}],
            "privatePayload": {"x": 1},
        },
        NOTE_ID,
    )
    assert record["liked_count"] == "123"
    assert record["collected_count"] == "45"
    assert record["comment_count"] == "6"
    assert record["share_count"] == "7"
    assert record["tags"] == ["旅行", "杭州"]
    assert "privatePayload" not in record
    assert "liked" not in record


def test_normalize_wrong_note_state_is_rejected():
    assert normalize_public_note_record({"id": "aaaaaaaaaaaaaaaaaaaaaaaa", "title": "wrong"}, NOTE_ID) is None


def test_zero_metrics_from_state_are_exact_values():
    note = _base_note()
    merged = merge_note_with_structured(
        note,
        {"note_id": NOTE_ID, "interactInfo": {"likedCount": "0", "collectedCount": 0, "commentCount": "0", "shareCount": 0}},
    )
    assert merged["likes_value"] == 0
    assert merged["likes_is_exact"] is True
    assert merged["collects_value"] == 0
    assert merged["comments_value"] == 0
    assert merged["shares_value"] == 0


def test_dom_tags_and_state_tags_are_merged():
    note = _base_note()
    note["hashtags"] = ["杭州"]
    note["field_sources"] = {"tags": "DOM_EXACT"}
    merged = merge_note_with_structured(note, {"note_id": NOTE_ID, "tagList": [{"name": "杭州"}, {"name": "旅行"}]})
    assert merged["hashtags"] == ["旅行", "杭州"]
    assert merged["field_sources"]["tags"] == "DOM_EXACT+INITIAL_STATE"


def test_missing_desc_does_not_use_root_ui_text_as_body():
    html = """
    <main class="note-detail">
      <h1 id="detail-title">标题</h1>
      <div>作者</div>
      <button>关注</button>
      <div>加载中</div>
      <div class="engage-bar">
        <span class="collect-wrapper">收藏</span>
        <span class="chat-wrapper">评论</span>
      </div>
    </main>
    """
    note = asyncio.run(_extract_from_html(html))
    assert note["title"] == "标题"
    assert note["body"] is None
    assert "作者" not in (note["body"] or "")
    assert note["field_sources"].get("body") == "MISSING"


def test_missing_desc_does_not_extract_comment_hashtag_as_note_tag():
    html = """
    <main class="note-detail">
      <h1 id="detail-title">标题</h1>
      <section class="comment-item">用户A\n评论里的 #误提取</section>
      <div class="engage-bar"><span class="like-wrapper"><span class="count">1</span></span></div>
    </main>
    """
    note = asyncio.run(_extract_from_html(html))
    assert note["body"] is None
    assert note["hashtags"] == []
    assert note["field_sources"].get("tags") == "MISSING"


def test_structured_desc_still_fills_missing_dom_body():
    note = _base_note()
    note["title"] = "标题"
    note["field_sources"] = {"title": "DOM_EXACT"}
    merged = merge_note_with_structured(
        note,
        {"note_id": NOTE_ID, "desc": "结构化正文", "_field_sources": {"body": "DETAIL_INITIAL_STATE"}},
    )
    assert merged["body"] == "结构化正文"
    assert merged["field_sources"]["body"] == "DETAIL_INITIAL_STATE"


async def _extract_from_html(html: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.set_content(html)
        note = await extract_note_dom(page, NOTE_ID, 3)
        await browser.close()
        return note


def _base_note():
    return {
        "note_id": NOTE_ID,
        "title": None,
        "body": None,
        "note_type": None,
        "publish_time": None,
        "hashtags": [],
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
        "raw_json": {},
        "field_sources": {},
    }
