"""页面搬迁（/relocate）端到端与改写单测。运行：pytest tests/test_relocate.py"""

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
from app.linkcheck import resolve_target  # noqa: E402
from app.main import app  # noqa: E402
from app.relocate import (  # noqa: E402
    rewrite_anchor_hrefs,
    _rebase_moved_page_href,
    _repoint_other_page_href,
)


@pytest.fixture(scope="module")
def client():
    # 与 test_api.py 隔离：直接改用本模块专属的数据库文件
    db.DB_PATH = _TMP.name
    db.init_db()
    with TestClient(app) as c:
        yield c


def publish(client, rev, changes):
    resp = client.post(
        "/publish",
        json={"expected_revision": rev, "changes": changes},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


# ---------- href 改写纯函数 ----------

def test_rewrite_only_touches_anchor_href():
    html = (
        '<html><!-- <a href="old.html">commented</a> -->\n'
        '<a  href = old.html data-href="keep.html" title="&quot;">t</a>\n'
        '<a href=\'q?a=1&amp;b=2#frag\'>q</a>\n'
        '<a href="https://ext.com/x">ext</a>\n'
        '<img src="old.html"><span>正文 old.html 不改</span></html>'
    )

    def rewrite(raw):
        if raw.strip() == "old.html":
            return "new.html"
        if raw.strip() == "q?a=1&amp;b=2#frag":
            return "new.html?a=1&amp;b=2#frag"
        return None

    out = rewrite_anchor_hrefs(html, rewrite)
    assert "commented" in out and 'href="old.html">commented' in out  # 注释不动
    assert '<a  href = new.html data-href="keep.html"' in out
    assert "new.html?a=1&amp;b=2#frag" in out  # 查询串/实体/片段原文保留
    assert 'href="https://ext.com/x"' in out
    assert '<img src="old.html">' in out
    assert "<span>正文 old.html 不改</span>" in out


def test_other_page_absolute_and_relative_repoint():
    # 绝对 href 改绝对新路径；相对 href 改相对新路径，查询串/片段保留
    assert _repoint_other_page_href("/index.html", "/docs/a.html", "/info/a.html",
                                    "/docs/a.html?v=1#s") == "/info/a.html?v=1#s"
    assert _repoint_other_page_href("/docs/b.html", "/docs/a.html", "/info/a.html",
                                    "a.html#s") == "../info/a.html#s"
    # 指向别的页面的链接不动
    assert _repoint_other_page_href("/index.html", "/docs/a.html", "/info/a.html",
                                    "/other.html") is None
    # 外链不动
    assert _repoint_other_page_href("/index.html", "/docs/a.html", "/info/a.html",
                                    "https://x/docs/a.html") is None


def test_moved_page_rebase_relative_hrefs():
    old, new = "/docs/guide.html", "/manual/guide.html"

    # 指向 /index.html 的相对链接以新目录为基准
    out = _rebase_moved_page_href(old, new, "../index.html")
    assert resolve_target(new, out)[0] == "/index.html"

    # 同目录兄弟页面
    out = _rebase_moved_page_href(old, new, "detail.html")
    assert resolve_target(new, out)[0] == "/docs/detail.html"

    # 查询串与片段原样保留
    out = _rebase_moved_page_href(old, new, "../index.html?x=1#top")
    assert out == "../index.html?x=1#top"
    assert resolve_target(new, out)[:2] == ("/index.html", "top")

    # 纯片段/纯查询指向本页，不改
    assert _rebase_moved_page_href(old, new, "#sec") is None
    assert _rebase_moved_page_href(old, new, "?v=2") is None

    # 绝对 href 指向别的页面：不动
    assert _rebase_moved_page_href(old, new, "/other.html") is None

    # 绝对自指旧页：改指新页
    assert _rebase_moved_page_href(old, new, "/docs/guide.html#self") == "/manual/guide.html#self"

    # 目录形式指向 /docs/ 自身（该目录 index.html）：以新目录基准重算并保留斜杠
    out = _rebase_moved_page_href(old, new, "./")
    assert resolve_target(new, out)[0] == "/docs/index.html"
    assert out.endswith("/")


# ---------- HTTP 端到端 ----------

def test_01_first_publish(client):
    rev = publish(client, 0, [
        {"op": "upsert", "path": "/index.html",
         "content": '<h1 id="top">Home</h1>'
                    '<a href="/about.html">about abs</a> '
                    '<a href="docs/guide.html?x=1#intro">guide rel</a> '
                    '<a href="https://ext.com/p">ext</a>'},
        {"op": "upsert", "path": "/about.html",
         "content": '<a href="/docs/guide.html#intro">guide</a>'},
        {"op": "upsert", "path": "/docs/guide.html",
         "content": '<h2 id="intro">Guide</h2>'
                    '<a href="../index.html#top">home rel</a> '
                    '<a href="detail.html">detail</a> '
                    '<a href="/index.html?v=2">home abs</a> '
                    '<a href="#intro">self frag</a> '
                    '<a href="/docs/guide.html">self abs</a>'},
        {"op": "upsert", "path": "/docs/detail.html",
         "content": '<a href="guide.html#intro">back</a>'},
    ])
    assert rev == 1


def test_02_relocate_success(client):
    resp = client.post("/relocate", json={
        "expected_revision": 1,
        "old_path": "/docs/guide.html",
        "new_path": "/manual/guide.html",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "revision": 2,
        "relocated": True,
        "old_path": "/docs/guide.html",
        "new_path": "/manual/guide.html",
    }


def test_03_old_path_gone_new_keeps_body_and_id(client):
    pages = client.get("/pages").json()["pages"]
    assert "/docs/guide.html" not in pages
    new_page = pages["/manual/guide.html"]
    assert '<h2 id="intro">Guide</h2>' in new_page  # 正文与 id 保留


def test_04_other_pages_links_repointed(client):
    pages = client.get("/pages").json()["pages"]
    # 其他页面的绝对 href 改指新页
    assert '<a href="/manual/guide.html#intro">guide</a>' in pages["/about.html"]
    # 其他页面的相对 href 改指新页（相对来源页目录），查询串保留
    assert '<a href="manual/guide.html?x=1#intro">guide rel</a>' in pages["/index.html"]
    # 外链不动
    assert '<a href="https://ext.com/p">ext</a>' in pages["/index.html"]
    # detail 原本相对指向 guide，重算后仍指向新页且片段可达
    assert '<a href="../manual/guide.html#intro">back</a>' in pages["/docs/detail.html"]


def test_05_moved_page_relative_hrefs_rebased(client):
    new_page = client.get("/pages").json()["pages"]["/manual/guide.html"]
    assert '<a href="../index.html#top">home rel</a>' in new_page
    assert '<a href="../docs/detail.html">detail</a>' in new_page
    assert '<a href="/index.html?v=2">home abs</a>' in new_page  # 指向他页的绝对链接不动
    assert '<a href="#intro">self frag</a>' in new_page
    assert '<a href="/manual/guide.html">self abs</a>' in new_page  # 绝对自指改新页


def test_06_all_links_resolve_after_relocate(client):
    # 兜底：搬迁后页面集合内不应存在任何链接错误（用一次空发布复用整包校验）
    rev = client.get("/revision").json()["revision"]
    assert publish(client, rev, []) == rev + 1


def test_07_new_path_conflict_fails_atomically(client):
    # 当前修订为 3；占用的新路径 -> 422，内容与修订号不变
    resp = client.post("/relocate", json={
        "expected_revision": 3,
        "old_path": "/about.html",
        "new_path": "/index.html",
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "new path already exists"
    assert client.get("/revision").json() == {"revision": 3}


def test_08_same_path_is_conflict(client):
    resp = client.post("/relocate", json={
        "expected_revision": 3,
        "old_path": "/about.html",
        "new_path": "/about.html",
    })
    assert resp.status_code == 422
    reasons = [e["reason"] for e in resp.json()["errors"]]
    assert "new path already exists" in reasons
    assert client.get("/revision").json() == {"revision": 3}


def test_09_source_missing_and_index_forbidden(client):
    resp = client.post("/relocate", json={
        "expected_revision": 3,
        "old_path": "/no/such.html",
        "new_path": "/x.html",
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "source page not found"

    resp = client.post("/relocate", json={
        "expected_revision": 3,
        "old_path": "/index.html",
        "new_path": "/home.html",
    })
    assert resp.status_code == 422
    assert "cannot be relocated" in resp.json()["errors"][0]["reason"]
    assert client.get("/revision").json() == {"revision": 3}


def test_10_stale_revision_conflict(client):
    resp = client.post("/relocate", json={
        "expected_revision": 1,
        "old_path": "/about.html",
        "new_path": "/about2.html",
    })
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == "revision conflict"
    assert body["expected"] == 1
    assert body["current"] == 3
    assert client.get("/revision").json() == {"revision": 3}


def test_11_bad_path_shape_rejected(client):
    resp = client.post("/relocate", json={
        "expected_revision": 3,
        "old_path": "docs/guide.html",
        "new_path": "/manual/guide.html",
    })
    assert resp.status_code == 422

    resp = client.post("/relocate", json={
        "expected_revision": 3,
        "old_path": "/about.html",
        "new_path": "/x.html?frag",
    })
    assert resp.status_code == 422


def test_12_historical_snapshot_before_relocate_still_readable(client):
    # revision 1（搬迁前）旧路径仍在、内容未改；revision 2 是搬迁结果
    rev1 = client.get("/pages", params={"revision": 1}).json()["pages"]
    assert "/docs/guide.html" in rev1
    assert '<a href="../index.html#top">home rel</a>' in rev1["/docs/guide.html"]
    assert "/manual/guide.html" not in rev1

    rev2 = client.get("/pages", params={"revision": 2}).json()["pages"]
    assert "/docs/guide.html" not in rev2
    assert "/manual/guide.html" in rev2


def test_13_second_relocate_chain(client):
    # 再次搬迁：新页相对链接继续随位置重定位，其他页面引用继续跟随
    resp = client.post("/relocate", json={
        "expected_revision": 3,
        "old_path": "/manual/guide.html",
        "new_path": "/book/guide.html",
    })
    assert resp.status_code == 200, resp.text
    pages = client.get("/pages").json()["pages"]
    assert set(pages) == {
        "/index.html", "/about.html",
        "/book/guide.html", "/docs/detail.html",
    }
    assert '<a href="../index.html#top">home rel</a>' in pages["/book/guide.html"]
    assert '<a href="../docs/detail.html">detail</a>' in pages["/book/guide.html"]
    assert '<a href="/book/guide.html#intro">guide</a>' in pages["/about.html"]
    assert '<a href="../manual/guide.html#intro">back</a>' not in pages["/docs/detail.html"]
    assert '<a href="../book/guide.html#intro">back</a>' in pages["/docs/detail.html"]
    assert client.get("/revision").json() == {"revision": 4}
