"""不可达页面清理（/prune）端到端测试。运行：pytest tests/test_prune.py"""

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
    resp = client.post("/publish", json={"expected_revision": rev, "changes": changes})
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def snapshot(client):
    """当前 (修订号, 页面集合, 跳转表)：用于校验失败时三者均不变。"""
    revision = client.get("/revision").json()["revision"]
    pages = client.get("/pages").json()["pages"]
    redirects = client.get("/redirects").json()["redirects"]
    return revision, pages, redirects


# ---------- 可达性遍历纯函数 ----------

def test_reachable_traverses_absolute_relative_and_directory_links():
    pages = {
        "/index.html": '<a href="/docs/">dir</a> <a href="rel/a.html">rel</a>',
        "/rel/a.html": '<a href="../about.html#s">up</a>',
        "/about.html": '<p id="s">about</p>',
        "/docs/index.html": '<a href="/index.html">home</a>',
        "/orphan.html": '<a href="/index.html">home</a>',
    }
    keep = reachable_pages(pages, {"/index.html"})
    assert keep == {"/index.html", "/rel/a.html", "/about.html", "/docs/index.html"}
    assert "/orphan.html" not in keep


def test_reachable_ignores_query_fragment_and_external():
    pages = {
        "/index.html": (
            '<a href="/a.html?v=2#frag">q</a> '
            '<a href="https://ext.com/x">ext</a> '
            '<a href="//cdn.example/y">cdn</a> '
            '<a href="mailto:a@b.com">mail</a>'
        ),
        "/a.html": "<p>a</p>",
    }
    assert reachable_pages(pages, {"/index.html"}) == {"/index.html", "/a.html"}


def test_reachable_orphan_cycle_not_kept():
    # 互相引用形成闭环、但从入口不可达的页面同样被清理
    pages = {
        "/index.html": "<p>home</p>",
        "/x.html": '<a href="/y.html">y</a>',
        "/y.html": '<a href="/x.html">x</a>',
    }
    assert reachable_pages(pages, {"/index.html"}) == {"/index.html"}


# ---------- 边界：首次发布前 / 过期修订号 ----------

def test_01_prune_before_first_publish_is_clear_error(client):
    resp = client.post("/prune", json={"expected_revision": 0})
    assert resp.status_code == 404
    assert resp.json() == {"detail": "no published revision to prune", "revision": 0}
    assert client.get("/revision").json() == {"revision": 0}


# ---------- 建景 ----------

def test_02_first_publish(client):
    rev = publish(client, 0, [
        {"op": "upsert", "path": "/index.html",
         "content": '<h1 id="top">Home</h1>'
                    '<a href="/docs/">docs</a> '
                    '<a href="/about.html?lang=zh#s">about</a>'},
        {"op": "upsert", "path": "/about.html",
         "content": '<p id="s">about</p> <a href="/#top">home</a>'},
        {"op": "upsert", "path": "/docs/index.html",
         "content": '<a href="../index.html#top">home</a>'},
    ])
    assert rev == 1


def test_03_prune_with_nothing_to_delete_keeps_revision(client):
    before = snapshot(client)
    resp = client.post("/prune", json={"expected_revision": 1})
    assert resp.status_code == 200
    assert resp.json() == {"revision": 1, "pruned": False, "deleted": []}
    # 不产生新修订，页面与跳转均不变
    assert snapshot(client) == before


def test_04_stale_expected_revision_conflict(client):
    before = snapshot(client)
    resp = client.post("/prune", json={"expected_revision": 0})
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == "revision conflict"
    assert body["expected"] == 0
    assert body["current"] == 1
    assert snapshot(client) == before


def test_05_publish_orphans_and_redirect(client):
    # 孤儿页：独立页、互链闭环、仅被孤儿引用的页；以及现行跳转目标链。
    rev = publish(client, 1, [
        {"op": "upsert", "path": "/orphan.html", "content": "<p>orphan</p>"},
        {"op": "upsert", "path": "/loop/a.html",
         "content": '<a href="/loop/b.html">b</a>'},
        {"op": "upsert", "path": "/loop/b.html",
         "content": '<a href="/loop/a.html">a</a> <a href="/loop/only-via-orphan.html">c</a>'},
        {"op": "upsert", "path": "/loop/only-via-orphan.html", "content": "<p>c</p>"},
        {"op": "upsert", "path": "/deep/hidden.html",
         "content": '<a href="/deep/other.html">other</a>'},
        {"op": "upsert", "path": "/deep/other.html", "content": "<p>other</p>"},
    ])
    assert rev == 2
    # 现行旧址跳转：/legacy/hidden.html -> /deep/hidden.html（不推进修订号）
    resp = client.post("/redirects", json={
        "path": "/legacy/hidden.html", "target": "/deep/hidden.html",
    })
    assert resp.status_code == 200, resp.text
    assert client.get("/revision").json() == {"revision": 2}


# ---------- 清理 ----------

def test_06_prune_deletes_unreachable_pages(client):
    resp = client.post("/prune", json={"expected_revision": 2})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "revision": 3,
        "pruned": True,
        "deleted": [
            "/loop/a.html",
            "/loop/b.html",
            "/loop/only-via-orphan.html",
            "/orphan.html",
        ],
    }
    pages = client.get("/pages").json()
    assert pages["revision"] == 3
    # 入口可达页、跳转目标及其可达页全部保留
    assert set(pages["pages"]) == {
        "/index.html",
        "/about.html",
        "/docs/index.html",
        "/deep/hidden.html",
        "/deep/other.html",
    }
    # 跳转表不变
    assert client.get("/redirects").json() == {
        "redirects": {"/legacy/hidden.html": "/deep/hidden.html"}
    }


def test_07_historical_snapshot_still_readable(client):
    rev2 = client.get("/pages", params={"revision": 2}).json()
    assert rev2["revision"] == 2
    assert "/orphan.html" in rev2["pages"]
    assert "/loop/a.html" in rev2["pages"]


def test_08_site_access_after_prune(client):
    assert client.get("/site/index.html").status_code == 200
    assert client.get("/site/deep/other.html").status_code == 200
    # 被清理的页面 404
    assert client.get("/site/orphan.html").status_code == 404
    assert client.get("/site/loop/a.html").status_code == 404
    # 旧址仍 308 指向保留的真实页
    resp = client.get("/site/legacy/hidden.html", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/site/deep/hidden.html"


def test_09_download_after_prune(client):
    resp = client.get("/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"


def test_10_publish_continues_after_prune(client):
    rev = publish(client, 3, [
        {"op": "upsert", "path": "/about.html",
         "content": '<p id="s">about v2</p> <a href="/#top">home</a>'},
    ])
    assert rev == 4
    assert client.get("/pages").json()["pages"]["/about.html"].startswith('<p id="s">about v2')


def test_11_prune_again_with_nothing_to_delete(client):
    before = snapshot(client)
    resp = client.post("/prune", json={"expected_revision": 4})
    assert resp.status_code == 200
    assert resp.json() == {"revision": 4, "pruned": False, "deleted": []}
    assert snapshot(client) == before
