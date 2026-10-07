"""并行变更合入（/merge）端到端测试。运行：pytest tests/test_merge.py"""

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


@pytest.fixture(scope="module")
def client():
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


def merge(client, base, changes):
    return client.post(
        "/merge",
        json={"base_revision": base, "changes": changes},
    )


# ---------- 基线 0 / 基线即当前 ----------

def test_01_base_zero_first_publish_requires_index(client):
    # 基线 0 上的合入同样受入口页规则约束
    resp = merge(client, 0, [{"op": "upsert", "path": "/about.html", "content": "<p>x</p>"}])
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "first publish must include /index.html"
    assert client.get("/revision").json() == {"revision": 0}


def test_02_base_zero_merge_success(client):
    resp = merge(client, 0, [
        {"op": "upsert", "path": "/index.html",
         "content": '<h1 id="top">Home</h1><a href="/about.html">about</a>'},
        {"op": "upsert", "path": "/about.html",
         "content": '<a href="/#top">home</a>'},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revision": 1, "merged": True, "base_revision": 0}


def test_03_base_equals_current_is_plain_publish(client):
    # 基线即当前修订：没有并行改动，合入等价于一次正常发布
    resp = merge(client, 1, [
        {"op": "upsert", "path": "/extra.html", "content": '<a href="/index.html#top">x</a>'},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2


# ---------- 并行非冲突合入 ----------

def test_04_parallel_non_conflicting_keeps_other_pages(client):
    # 当前为 rev2。其他人从 rev2 发布 rev3：新增 /news.html 并替换 index 加上链接
    rev3 = publish(client, 2, [
        {"op": "upsert", "path": "/index.html",
         "content": '<h1 id="top">Home</h1><a href="/about.html">about</a> '
                    '<a href="/news.html">news</a>'},
        {"op": "upsert", "path": "/news.html",
         "content": '<a href="/#top">home</a>'},
    ])
    assert rev3 == 3

    # 本端仍基于过期的 rev2，只改动未被触碰的 /about.html -> 合入成功
    resp = merge(client, 2, [
        {"op": "upsert", "path": "/about.html",
         "content": '<a href="/#top">home</a> <a href="/news.html">other-added page visible</a>'},
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"revision": 4, "merged": True, "base_revision": 2}

    pages = client.get("/pages").json()["pages"]
    # 其他人新增的页面与对 index 的改动原样保留
    assert "/news.html" in pages
    assert '<a href="/news.html">news</a>' in pages["/index.html"]
    # 本端改动已应用
    assert "other-added page visible" in pages["/about.html"]
    # 历史快照仍可读取
    rev2 = client.get("/pages", params={"revision": 2}).json()["pages"]
    assert "/news.html" not in rev2
    rev3pages = client.get("/pages", params={"revision": 3}).json()["pages"]
    assert "other-added page visible" not in rev3pages["/about.html"]


# ---------- 同路径冲突：整批不写入 ----------

def test_05_modified_path_conflict(client):
    # 本端基于 rev2 改 index，但其他人已在 rev3 改过 index -> 409
    resp = merge(client, 2, [
        {"op": "upsert", "path": "/index.html", "content": "<h1>stale view</h1>"},
    ])
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == "merge conflict"
    assert body["base_revision"] == 2
    assert body["current"] == 4
    assert body["conflicts"] == [
        {"path": "/index.html", "reason": "path modified by another change"}
    ]
    assert client.get("/revision").json() == {"revision": 4}
    assert "stale view" not in client.get("/pages").json()["pages"]["/index.html"]


def test_06_added_and_deleted_path_conflicts(client):
    # 其他人再发布 rev5：新增 /fresh.html、删除 /extra.html（本端基线 rev2 中存在 extra）
    rev5 = publish(client, 4, [
        {"op": "upsert", "path": "/fresh.html", "content": "<p>fresh</p>"},
        {"op": "delete", "path": "/extra.html"},
    ])
    assert rev5 == 5

    # 本端基于 rev2：删除一个已被别人删除的 extra（基线存在、当前不存在），
    # 同时新增一个已被别人新增的 fresh（基线不存在、当前存在）-> 两个冲突
    resp = merge(client, 2, [
        {"op": "delete", "path": "/extra.html"},
        {"op": "upsert", "path": "/fresh.html", "content": "<p>my version</p>"},
    ])
    assert resp.status_code == 409
    body = resp.json()
    assert body["current"] == 5
    reasons = {c["path"]: c["reason"] for c in body["conflicts"]}
    assert reasons == {
        "/extra.html": "path deleted by another change",
        "/fresh.html": "path added by another change",
    }
    # 冲突按路径排序；整批不写入
    assert [c["path"] for c in body["conflicts"]] == ["/extra.html", "/fresh.html"]
    assert client.get("/revision").json() == {"revision": 5}
    assert client.get("/pages").json()["pages"]["/fresh.html"] == "<p>fresh</p>"


def test_07_unchanged_same_content_no_conflict(client):
    # 本端 upsert 一个基线（rev4）与当前（rev5）完全相同、未被触碰的路径：不构成冲突
    about_current = client.get("/pages").json()["pages"]["/about.html"]
    resp = merge(client, 4, [
        {"op": "upsert", "path": "/about.html", "content": about_current},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 6


def test_08_delete_path_others_deleted_is_conflict(client):
    # 基线 rev2 中有 /extra.html，当前 rev6 中已无；本端再 delete -> 冲突
    resp = merge(client, 2, [{"op": "delete", "path": "/extra.html"}])
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "path deleted by another change"
    assert client.get("/revision").json() == {"revision": 6}


# ---------- 合入后链接失效：422，内容及修订号不变 ----------

def test_09_other_change_breaks_our_link(client):
    # 建景：rev6 上 /about.html 链向 /news.html。其他人从 rev6 发布 rev7，
    # 删除 /news.html 并清掉现存页面中的引用。本端基于 rev6 新增的页面仍引用
    # /news.html -> 合入后链接失效，422 列出来源 /mine.html 与错误目标
    pages6 = client.get("/pages", params={"revision": 6}).json()["pages"]
    assert "/news.html" in pages6
    rev7 = publish(client, 6, [
        {"op": "upsert", "path": "/index.html",
         "content": '<h1 id="top">Home</h1><a href="/about.html">about</a>'},
        {"op": "upsert", "path": "/about.html",
         "content": '<a href="/#top">home</a>'},
        {"op": "delete", "path": "/news.html"},
    ])
    assert rev7 == 7

    resp = merge(client, 6, [
        {"op": "upsert", "path": "/mine.html",
         "content": '<a href="/news.html">gone after parallel delete</a>'},
    ])
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    e = errors[0]
    assert e["source"] == "/mine.html"
    assert e["target"] == "/news.html"
    assert e["reason"] == "target page not found"
    assert client.get("/revision").json() == {"revision": 7}
    assert "/mine.html" not in client.get("/pages").json()["pages"]


def test_10_merge_delete_referenced_by_other_blocked(client):
    # 本端删除 /about.html，但其他人的 /index.html 仍引用它 -> 422
    resp = merge(client, 7, [{"op": "delete", "path": "/about.html"}])
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["source"] == "/index.html"
    assert e["target"] == "/about.html"
    assert e["reason"] == "target page not found"
    assert client.get("/revision").json() == {"revision": 7}


def test_11_merge_delete_index_blocked(client):
    resp = merge(client, 7, [{"op": "delete", "path": "/index.html"}])
    assert resp.status_code == 422
    assert "must not be deleted" in resp.json()["errors"][0]["reason"]


def test_12_merge_cleanup_success(client):
    # 同时替换 index 去掉 about 链接并删除 about -> 成功，生成新修订
    resp = merge(client, 7, [
        {"op": "upsert", "path": "/index.html",
         "content": '<h1 id="top">Home</h1>'},
        {"op": "delete", "path": "/about.html"},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 8
    pages = client.get("/pages").json()["pages"]
    assert set(pages) == {"/index.html", "/fresh.html"}


# ---------- 基线错误 / 接口共存 ----------

def test_13_baseline_not_found(client):
    current = client.get("/revision").json()["revision"]
    resp = merge(client, current + 1, [])
    assert resp.status_code == 404
    assert resp.json() == {
        "detail": "base revision not found",
        "base_revision": current + 1,
    }
    assert client.get("/revision").json() == {"revision": current}


def test_14_publish_stale_revision_still_409(client):
    # 既有 /publish 的过期 expected_revision 409 行为保持可用
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [{"op": "upsert", "path": "/x.html", "content": "<p>x</p>"}],
    })
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == "revision conflict"
    assert body["expected"] == 1
    assert body["current"] == 8


def test_15_relocate_still_works(client):
    # /relocate 在合入产生的快照上继续可用
    resp = client.post("/relocate", json={
        "expected_revision": 8,
        "old_path": "/fresh.html",
        "new_path": "/fresh2.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 9
    pages = client.get("/pages").json()["pages"]
    assert "/fresh.html" not in pages and "/fresh2.html" in pages
