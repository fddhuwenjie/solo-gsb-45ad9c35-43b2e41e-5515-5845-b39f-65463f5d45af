"""锚点其他属性值内含伪 ``href=`` 文本时的改写回归测试。

回归背景：``<a title="hint href='/docs/bait.html'" href="/docs/guide.html?lang=zh#intro">``
的 title 属性值里出现了形似 href 的文本。旧实现用正则在标签原文里找第一个
``href=`` 出现位置，结果改写了 title 里的伪 href，真正的 href 原样保留——
/site 输出后点击仍请求 /docs/guide.html（不在 /site 挂载下）返回 404；
/relocate 搬迁目标页时真正的 href 也未被重指。修复后按 HTML 属性语法定位
真正的 href 属性：

- /site 只给真正的绝对站内 href 加 /site 前缀，title 等其他属性值原样保留；
- /relocate 只重指真正的 href，查询串与 #片段 保留，首页到达新页，
  旧址一次 308 直达新页；整包校验失败则页面、跳转和修订号均不变；
- /pages 正文与历史快照保持发布原文。

运行：pytest tests/test_pseudo_href.py
"""

from __future__ import annotations

import os
import tempfile

import pytest

# 必须在导入 app 之前指定数据库文件
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DOCS_DB_PATH"] = _TMP.name

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.htmlrewrite import rewrite_anchor_hrefs  # noqa: E402
from app.main import app  # noqa: E402
from app.site import rewrite_html_for_site  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # 与其他测试模块隔离：直接改用本模块专属的数据库文件
    db.DB_PATH = _TMP.name
    db.init_db()
    with TestClient(app) as c:
        yield c


INDEX_HTML = (
    '<html><body>\n'
    '<h1 id="top">首页</h1>\n'
    # title 属性值内含伪 href= 文本；真正的 href 是最后一个属性
    '<a title="hint href=\'/docs/bait.html\'" href="/docs/guide.html?lang=zh#intro">指南</a>\n'
    '</body></html>'
)

GUIDE_HTML = '<h2 id="intro">Guide</h2>'


# ---------- 改写纯函数 ----------

def test_unit_site_rewrite_touches_only_real_href():
    html = (
        '<a title="hint href=\'/docs/bait.html\'" href="/docs/guide.html?lang=zh#intro">指南</a>'
        '<a href="/a.html" title="see href=\'/bait.html\'">伪 href 在真 href 之后</a>'
        '<a data-href="/bait.html" href="/real.html">data-href 不命中</a>'
        "<a title='say href=\"/bait.html\"' href='/single.html'>单引号</a>"
    )
    out = rewrite_html_for_site(html)
    # 真正的绝对站内 href 加 /site 前缀，查询串与片段保留
    assert 'href="/site/docs/guide.html?lang=zh#intro"' in out
    assert 'href="/site/a.html"' in out
    assert 'href="/site/real.html"' in out
    assert "href='/site/single.html'" in out
    # 其他属性值内的伪 href= 文本与 data-href 一律原样
    assert 'title="hint href=\'/docs/bait.html\'"' in out
    assert 'title="see href=\'/bait.html\'"' in out
    assert 'data-href="/bait.html"' in out
    assert "title='say href=\"/bait.html\"'" in out
    # 除真正的 href 值之外没有任何字节变化
    assert "/bait.html" in out and "/site/docs/bait.html" not in out


def test_unit_relocate_style_rewrite_touches_only_real_href():
    html = '<a title="hint href=\'/docs/bait.html\'" href="/docs/guide.html?lang=zh#intro">指南</a>'

    def rewrite(raw):
        # 模拟 relocate._repoint_other_page_href：只改指向旧页的 href
        if raw.strip() == "/docs/guide.html?lang=zh#intro":
            return "/manual/guide.html?lang=zh#intro"
        return None

    out = rewrite_anchor_hrefs(html, rewrite)
    assert out == (
        '<a title="hint href=\'/docs/bait.html\'" '
        'href="/manual/guide.html?lang=zh#intro">指南</a>'
    )


def test_unit_scanner_edge_forms():
    # 大小写不敏感 / 无引号 / 无值 href / 重复 href 取第一个（与浏览器一致）
    assert rewrite_html_for_site('<a HREF="/up.html">x</a>') == '<a HREF="/site/up.html">x</a>'
    assert rewrite_html_for_site('<a href=/plain.html>x</a>') == '<a href=/site/plain.html>x</a>'
    assert rewrite_html_for_site('<a href>x</a>') == '<a href>x</a>'
    assert (
        rewrite_html_for_site('<a href="/first.html" href="/second.html">x</a>')
        == '<a href="/site/first.html" href="/second.html">x</a>'
    )
    # 跨行标签与等号两侧空白保留
    assert (
        rewrite_html_for_site('<a\n  title="href=\'/b.html\'"\n  href = "/m.html">x</a>')
        == '<a\n  title="href=\'/b.html\'"\n  href = "/site/m.html">x</a>'
    )


# ---------- HTTP 端到端 ----------

def test_01_publish_target_and_index_with_bait_title(client):
    resp = client.post("/publish", json={
        "expected_revision": 0,
        "changes": [
            {"op": "upsert", "path": "/index.html", "content": INDEX_HTML},
            {"op": "upsert", "path": "/docs/guide.html", "content": GUIDE_HTML},
        ],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 1


def test_02_site_rewrites_real_href_and_keeps_title(client):
    body = client.get("/site/index.html").text
    # 真正的 href 指向 /site 下（查询串与片段保留），title 里的伪 href 原样
    assert 'href="/site/docs/guide.html?lang=zh#intro"' in body
    assert 'title="hint href=\'/docs/bait.html\'"' in body
    assert "/site/docs/bait.html" not in body


def test_03_rewritten_href_is_followable(client):
    # 模拟浏览器点击改写后的链接：去掉 #片段后请求应为 200（修复前是 404）
    resp = client.get("/site/docs/guide.html?lang=zh")
    assert resp.status_code == 200
    assert '<h2 id="intro">Guide</h2>' in resp.text


def test_04_pages_api_keeps_published_body(client):
    current = client.get("/pages").json()
    assert current["pages"]["/index.html"] == INDEX_HTML
    assert current["pages"]["/docs/guide.html"] == GUIDE_HTML


def test_05_relocate_repoints_only_real_href(client):
    resp = client.post("/relocate", json={
        "expected_revision": 1,
        "old_path": "/docs/guide.html",
        "new_path": "/manual/guide.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2

    pages = client.get("/pages").json()["pages"]
    # 存储正文中真正的 href 已改指新页（查询串与片段保留），title 伪 href 原样
    assert pages["/index.html"] == (
        '<html><body>\n'
        '<h1 id="top">首页</h1>\n'
        '<a title="hint href=\'/docs/bait.html\'" '
        'href="/manual/guide.html?lang=zh#intro">指南</a>\n'
        '</body></html>'
    )
    assert "/docs/guide.html" not in pages

    # /site 首页跟随搬迁结果，点击可到达新页
    body = client.get("/site/index.html").text
    assert 'href="/site/manual/guide.html?lang=zh#intro"' in body
    assert 'title="hint href=\'/docs/bait.html\'"' in body
    resp = client.get("/site/manual/guide.html?lang=zh")
    assert resp.status_code == 200
    assert '<h2 id="intro">Guide</h2>' in resp.text

    # 旧址一次 308 直达 /site 下的新页（不形成跳转链）
    resp = client.get("/site/docs/guide.html", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/site/manual/guide.html"


def test_06_history_snapshot_keeps_pre_relocate_body(client):
    rev1 = client.get("/pages", params={"revision": 1}).json()["pages"]
    assert rev1["/index.html"] == INDEX_HTML
    assert rev1["/docs/guide.html"] == GUIDE_HTML


def test_07_failed_relocate_changes_nothing(client):
    # 整包校验失败（新路径被占用）：页面、跳转和修订号均不变
    before_pages = client.get("/pages").json()["pages"]
    before_redirects = client.get("/redirects").json()["redirects"]
    before_revision = client.get("/revision").json()["revision"]

    resp = client.post("/relocate", json={
        "expected_revision": before_revision,
        "old_path": "/manual/guide.html",
        "new_path": "/index.html",
    })
    assert resp.status_code == 422

    assert client.get("/pages").json()["pages"] == before_pages
    assert client.get("/redirects").json()["redirects"] == before_redirects
    assert client.get("/revision").json()["revision"] == before_revision
