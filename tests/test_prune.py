"""不可达页面清理（/prune）端到端与遍历单测。运行：pytest tests/test_prune.py"""

from __future__ import annotations

import io
import os
import tempfile
import zipfile

import pytest

# 必须在导入 app 之前指定数据库文件
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DOCS_DB_PATH"] = _TMP.name

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.prune import reachable_pages  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # 与其他测试模块隔离：直接改用本模块专属的数据库文件
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


# ---------- 可达性遍历纯函数 ----------

def test_reachable_traversal_rules():
    """相对/绝对/目录路径沿发布规则解析；查询串与片段不改变目标；外链与伪 href 不参与。"""
    pages = {
        "/index.html": (
            '<a href="/docs/">dir</a> '
            '<a href="/abs.html?x=1#frag">abs</a> '
            '<a href="https://ext.com/x">ext</a> '
            "<a title=\"hint href='/pseudo.html'\">pseudo href in title</a>"
        ),
        "/docs/index.html": '<a href="../rel/page.html">rel</a>',
        "/rel/page.html": '<a href="/index.html#top">back</a>',
        "/abs.html": "<p>abs</p>",
        "/pseudo.html": "<p>only referenced inside a title attribute</p>",
        "/cycle/a.html": '<a href="/cycle/b.html">b</a>',
        "/cycle/b.html": '<a href="a.html">a</a>',
    }
    keep = reachable_pages(pages, ["/index.html"])
    # 互相引用的 /cycle/* 与仅出现在属性值文本中的 /pseudo.html 均不可达
    assert keep == {"/index.html", "/docs/index.html", "/rel/page.html", "/abs.html"}


def test_reachable_includes_seed_subtree_and_ignores_unknown_seeds():
    """跳转目标种子及其下游页面保留；不在页面集合中的种子被忽略。"""
    pages = {
        "/index.html": "<p>home</p>",
        "/target.html": '<a href="child.html">child</a>',
        "/child.html": "<p>child</p>",
        "/orphan.html": "<p>orphan</p>",
    }
    keep = reachable_pages(pages, ["/index.html", "/target.html", "/gone.html"])
    assert keep == {"/index.html", "/target.html", "/child.html"}


def test_reachable_ignores_unresolvable_hrefs():
    """越过站点根等无法解析的 href 不参与遍历，也不影响其他链接。"""
    pages = {
        "/index.html": '<a href="/../etc/passwd">escape</a> <a href="ok.html">ok</a>',
        "/ok.html": "<p>ok</p>",
        "/orphan.html": "<p>orphan</p>",
    }
    assert reachable_pages(pages, ["/index.html"]) == {"/index.html", "/ok.html"}


# ---------- HTTP 端到端 ----------

def test_prune_before_first_publish(client):
    """首次发布前（revision 0）明确报错；修订号不变。"""
    resp = client.post("/prune", json={"expected_revision": 0})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "no published revision to prune"
    assert client.get("/revision").json() == {"revision": 0}


def test_publish_site_with_orphans(client):
    """revision 1：可达页 + 互相引用的孤立页 + 无入链页 + 仅伪 href 提及的页。"""
    rev = publish(client, 0, [
        {"op": "upsert", "path": "/index.html", "content":
            '<h1 id="top">首页</h1>'
            '<a href="/docs/">文档目录</a> '
            '<a href="/abs.html?lang=zh#s">绝对+查询+片段</a> '
            "<a title=\"hint href='/pseudo.html'\">伪 href</a> "
            '<a href="https://ext.com/x">外链</a>'},
        {"op": "upsert", "path": "/docs/index.html", "content":
            '<a href="../rel/page.html">相对</a>'},
        {"op": "upsert", "path": "/rel/page.html", "content":
            '<a href="/#top">回首页锚点</a>'},
        {"op": "upsert", "path": "/abs.html", "content": '<p id="s">abs</p>'},
        {"op": "upsert", "path": "/pseudo.html", "content": "<p>仅出现在 title 文本中</p>"},
        {"op": "upsert", "path": "/cycle/a.html", "content":
            '<a href="/cycle/b.html">b</a>'},
        {"op": "upsert", "path": "/cycle/b.html", "content":
            '<a href="a.html">a</a>'},
        {"op": "upsert", "path": "/lone.html", "content": "<p>无入链</p>"},
        {"op": "upsert", "path": "/legacy/home.html", "content":
            '<a href="child.html">子页</a>'},
        {"op": "upsert", "path": "/legacy/child.html", "content": "<p>经跳转目标可达</p>"},
    ])
    assert rev == 1


def test_register_redirect_for_otherwise_unreachable_page(client):
    """/legacy/home.html 从首页不可达，但登记跳转后成为必须保留的入口。"""
    resp = client.post("/redirects", json={
        "path": "/old/home.html", "target": "/legacy/home.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 1  # 登记跳转不推进修订号


def test_prune_stale_revision_conflict(client):
    """过期 expected_revision -> 409；页面、跳转与修订号均不变。"""
    before_pages = client.get("/pages").json()
    before_redirects = client.get("/redirects").json()
    resp = client.post("/prune", json={"expected_revision": 0})
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == "revision conflict"
    assert body["expected"] == 0
    assert body["current"] == 1
    assert client.get("/revision").json() == {"revision": 1}
    assert client.get("/pages").json() == before_pages
    assert client.get("/redirects").json() == before_redirects


def test_prune_deletes_unreachable_pages(client):
    """删除全部不可达页面（含互相引用的孤立簇与伪 href 页）；清单按路径排序。"""
    resp = client.post("/prune", json={"expected_revision": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "revision": 2,
        "pruned": True,
        "deleted": ["/cycle/a.html", "/cycle/b.html", "/lone.html", "/pseudo.html"],
    }

    pages = client.get("/pages").json()
    assert pages["revision"] == 2
    # 入口可达页面与跳转目标子树（/legacy/home.html -> /legacy/child.html）保留
    assert set(pages["pages"]) == {
        "/index.html",
        "/docs/index.html",
        "/rel/page.html",
        "/abs.html",
        "/legacy/home.html",
        "/legacy/child.html",
    }
    # 跳转表不受清理影响
    assert client.get("/redirects").json() == {
        "redirects": {"/old/home.html": "/legacy/home.html"}
    }


def test_prune_noop_does_not_advance_revision(client):
    """没有可删页面时不产生新修订。"""
    resp = client.post("/prune", json={"expected_revision": 2})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revision": 2, "pruned": False, "deleted": []}
    assert client.get("/revision").json() == {"revision": 2}


def test_history_snapshot_still_readable(client):
    """历史修订快照保留被清理的页面。"""
    rev1 = client.get("/pages", params={"revision": 1}).json()
    assert rev1["revision"] == 1
    assert {"/cycle/a.html", "/cycle/b.html", "/lone.html", "/pseudo.html"} <= set(
        rev1["pages"]
    )


def test_site_and_download_still_work_after_prune(client):
    """站点访问与离线下载保持可用：保留页 200、旧址 308、已删页 404。"""
    resp = client.get("/site/legacy/home.html")
    assert resp.status_code == 200

    resp = client.get("/site/old/home.html", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/site/legacy/home.html"

    assert client.get("/site/cycle/a.html").status_code == 404

    resp = client.get("/download")
    assert resp.status_code == 200
    names = set(zipfile.ZipFile(io.BytesIO(resp.content)).namelist())
    assert "index.html" in names
    assert "legacy/home.html" in names
    assert "old/home.html" in names  # 旧址离线入口
    assert "cycle/a.html" not in names


def test_publish_still_works_after_prune(client):
    """清理后发布接口照常推进修订号。"""
    rev = publish(client, 2, [
        {"op": "upsert", "path": "/docs/index.html", "content":
            '<a href="../rel/page.html">相对（更新）</a>'},
    ])
    assert rev == 3
    assert client.get("/revision").json() == {"revision": 3}
