"""/site 站点读取的绝对站内 href 改写回归测试。

回归背景：发布含 ``<a href="/docs/guide.html?lang=zh#intro">`` 的 /index.html
与真实目标页后（发布校验通过），从 /site/index.html 点击该链接会请求
/docs/guide.html——不在 /site 挂载之下，按现有路由返回 404。
现在 /site 返回的 HTML 中绝对站内 href 指向 /site 下的同名路径，查询串与
片段仍指向原目标；相对链接、纯片段、纯查询、外部链接与非链接 HTML 保持原样；
/pages 正文与历史修订快照不受读取改写影响。

运行：pytest tests/test_site.py
"""

from __future__ import annotations

import os
import re
import tempfile

import pytest

# 必须在导入 app 之前指定数据库文件
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DOCS_DB_PATH"] = _TMP.name

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
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
    '<a href="/docs/guide.html?lang=zh#intro">指南（绝对）</a>\n'
    '<a href="docs/guide.html#intro">指南（相对）</a>\n'
    '<a href="#top">纯片段</a>\n'
    '<a href="?lang=en">纯查询</a>\n'
    '<a href="https://example.com/x">外链</a>\n'
    '<a href="//cdn.example.com/lib.js">协议相对</a>\n'
    '<a href="mailto:a@b.com">邮件</a>\n'
    '<a href="/">站点根</a>\n'
    '<img src="/docs/guide.html">\n'
    '<!-- <a href="/docs/guide.html">注释里的链接</a> -->\n'
    '<p>正文提到 /docs/guide.html 不改</p>\n'
    '</body></html>'
)

GUIDE_HTML = (
    '<h2 id="intro">Guide</h2>'
    '<a href="../index.html#top">home rel</a>'
    '<a href="/index.html#top">home abs</a>'
)


# ---------- 改写纯函数 ----------

def test_rewrite_unit_only_absolute_insite_hrefs():
    html = (
        '<a href="/a.html?x=1&amp;y=2#f">abs</a>'
        '<a href="b.html">rel</a>'
        '<a href="../c.html">rel up</a>'
        '<a href="#f">frag</a>'
        '<a href="?q=1">query</a>'
        '<a href="">empty</a>'
        '<a href="https://ext.com/a.html">ext</a>'
        '<a href="//cdn.example/x">proto-rel</a>'
        '<a href="mailto:a@b.com">mail</a>'
        '<a href="/">root</a>'
        '<a>no href</a>'
    )
    out = rewrite_html_for_site(html)
    # 绝对站内 href 加 /site 前缀，查询串（含 HTML 实体）与片段原文保留
    assert '<a href="/site/a.html?x=1&amp;y=2#f">abs</a>' in out
    assert '<a href="/site/">root</a>' in out
    # 其余一律原样
    for kept in (
        '<a href="b.html">rel</a>',
        '<a href="../c.html">rel up</a>',
        '<a href="#f">frag</a>',
        '<a href="?q=1">query</a>',
        '<a href="">empty</a>',
        '<a href="https://ext.com/a.html">ext</a>',
        '<a href="//cdn.example/x">proto-rel</a>',
        '<a href="mailto:a@b.com">mail</a>',
        '<a>no href</a>',
    ):
        assert kept in out


def test_rewrite_unit_preserves_quoting_and_non_link_html():
    html = (
        "<a  href = '/a.html' >x</a>"
        '<a href= /b.html >y</a>'
        '<!-- <a href="/c.html">comment</a> -->'
        '<link href="/d.css" rel="stylesheet">'
    )
    out = rewrite_html_for_site(html)
    assert "href = '/site/a.html'" in out  # 单引号与等号两侧空白保留
    assert "href= /site/b.html" in out    # 无引号形式
    assert '<!-- <a href="/c.html">comment</a> -->' in out  # 注释不动
    assert '<link href="/d.css" rel="stylesheet">' in out   # 非 <a> 标签不动


# ---------- HTTP 端到端 ----------

def test_01_publish_pages_with_absolute_insite_link(client):
    resp = client.post("/publish", json={
        "expected_revision": 0,
        "changes": [
            {"op": "upsert", "path": "/index.html", "content": INDEX_HTML},
            {"op": "upsert", "path": "/docs/guide.html", "content": GUIDE_HTML},
        ],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 1


def test_02_site_rewrites_only_absolute_insite_hrefs(client):
    resp = client.get("/site/index.html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    # 绝对站内 href 指向 /site 下，查询串与片段仍指向原目标
    assert '<a href="/site/docs/guide.html?lang=zh#intro">指南（绝对）</a>' in body
    assert 'href="/docs/guide.html?lang=zh#intro"' not in body
    assert '<a href="/site/">站点根</a>' in body
    # 相对链接、纯片段、纯查询、外部链接保持原样
    assert '<a href="docs/guide.html#intro">指南（相对）</a>' in body
    assert '<a href="#top">纯片段</a>' in body
    assert '<a href="?lang=en">纯查询</a>' in body
    assert '<a href="https://example.com/x">外链</a>' in body
    assert '<a href="//cdn.example.com/lib.js">协议相对</a>' in body
    assert '<a href="mailto:a@b.com">邮件</a>' in body
    # 非链接 HTML 保持原样
    assert '<img src="/docs/guide.html">' in body
    assert '<!-- <a href="/docs/guide.html">注释里的链接</a> -->' in body
    assert '<p>正文提到 /docs/guide.html 不改</p>' in body


def test_03_site_rewrites_absolute_href_on_subpage_too(client):
    body = client.get("/site/docs/guide.html").text
    assert '<a href="../index.html#top">home rel</a>' in body      # 相对不改
    assert '<a href="/site/index.html#top">home abs</a>' in body   # 绝对加前缀


def test_04_rewritten_href_is_followable_within_site(client):
    # 模拟浏览器点击：取出 /site HTML 中改写后的 href，去掉 #片段后请求应为 200
    body = client.get("/site/index.html").text
    m = re.search(r'href="(/site/[^"]+)"', body)
    assert m is not None
    url = m.group(1).split("#", 1)[0]  # 片段不发给服务器，查询串随 URL 原样携带
    assert url == "/site/docs/guide.html?lang=zh"
    resp = client.get(url)
    assert resp.status_code == 200
    assert '<h2 id="intro">Guide</h2>' in resp.text


def test_05_pages_api_and_history_keep_published_body(client):
    # /pages 当前集合与历史快照都是发布原文（不带 /site 前缀）
    current = client.get("/pages").json()
    assert current["pages"]["/index.html"] == INDEX_HTML
    assert current["pages"]["/docs/guide.html"] == GUIDE_HTML
    rev1 = client.get("/pages", params={"revision": 1}).json()
    assert rev1["pages"]["/index.html"] == INDEX_HTML
    assert rev1["pages"]["/docs/guide.html"] == GUIDE_HTML


def test_06_relocate_keeps_links_reachable_and_308_direct(client):
    resp = client.post("/relocate", json={
        "expected_revision": 1,
        "old_path": "/docs/guide.html",
        "new_path": "/manual/guide.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2

    # /site 首页的链接跟随搬迁结果，指向 /site 下的新页面
    body = client.get("/site/index.html").text
    assert '<a href="/site/manual/guide.html?lang=zh#intro">指南（绝对）</a>' in body

    # 旧址保持一次 308 直达 /site 下的真实页（不形成跳转链）
    resp = client.get("/site/docs/guide.html", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/site/manual/guide.html"
    resp = client.get("/site/manual/guide.html")
    assert resp.status_code == 200
    assert '<h2 id="intro">Guide</h2>' in resp.text

    # 存储正文是搬迁改写结果（不带 /site 前缀）；历史快照保持搬迁前原文
    current = client.get("/pages").json()["pages"]
    assert '<a href="/manual/guide.html?lang=zh#intro">指南（绝对）</a>' in current["/index.html"]
    assert "/site/" not in current["/index.html"]
    assert "/docs/guide.html" not in current
    rev1 = client.get("/pages", params={"revision": 1}).json()["pages"]
    assert rev1["/index.html"] == INDEX_HTML
    assert rev1["/docs/guide.html"] == GUIDE_HTML


def test_07_unknown_paths_still_404(client):
    assert client.get("/site/unknown.html").status_code == 404
    # /site 挂载之外的路径按现有路由仍是 404（改写让浏览器不再落到这里）
    assert client.get("/docs/guide.html").status_code == 404
    assert client.get("/manual/guide.html").status_code == 404
