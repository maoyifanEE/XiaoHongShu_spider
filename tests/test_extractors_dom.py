import asyncio

from playwright.async_api import async_playwright

from xhs_profile_exporter.extractors import _extract_comments, extract_note_dom


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
      <div class="engage-bar">点赞 12 收藏 3 评论 4 分享 5</div>
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
    assert note["hashtags"] == ["#测试标签"]


def test_sidebar_fake_count_does_not_pollute_note_metrics():
    html = """
    <section class="note-detail" style="display:block;width:800px;height:600px">
      <h1 id="detail-title">目标标题</h1>
      <div id="detail-desc">目标正文</div>
      <div class="engage-bar">点赞 1 收藏 2 评论 3 分享 4</div>
    </section>
    <aside style="display:block;width:300px;height:600px">
      <div class="recommendation">点赞 9999 收藏 8888 评论 7777 分享 6666</div>
    </aside>
    """
    note = asyncio.run(_extract_from_html(html))
    assert note["likes_value"] == 1
    assert note["collects_value"] == 2
    assert note["comments_value"] == 3
    assert note["shares_value"] == 4


async def _extract_from_html(html: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.set_content(html)
        note = await extract_note_dom(page, NOTE_ID, 3)
        await browser.close()
        return note
