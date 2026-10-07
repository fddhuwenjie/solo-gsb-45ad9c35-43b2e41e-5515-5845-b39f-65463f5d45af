"""旧地址跳转端到端测试：登记/撤销（/redirects）、站点读取（/site）、
搬迁联动与发布/合入守护。运行：pytest tests/test_redirects.py"""

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


# ---------- 建景 ----------

def test_01_first_publish(client):
    rev = publish(client, 0, [
        {"op": "upsert", "path": "/index.html",
         "content": '<h1 id="top">Home</h1>'
                    '<a href="/docs/guide.html">guide</a> '
                    '<a href="/about.html">about</a>'},
        {"op": "upsert", "path": "/about.html",
         "content": '<a href="/#top">home</a>'},
        {"op": "upsert", "path": "/docs/guide.html",
         "content": '<h2 id="intro">Guide</h2><a href="../index.html#top">home</a>'},
    ])
    assert rev == 1
    assert client.get("/redirects").json() == {"redirects": {}}


# ---------- 站点读取：真实路径 / 未知路径 ----------

def test_02_site_serves_real_pages_and_404(client):
    resp = client.get("/site/index.html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert '<h1 id="top">Home</h1>' in resp.text

    resp = client.get("/site/docs/guide.html")
    assert resp.status_code == 200
    assert '<h2 id="intro">Guide</h2>' in resp.text

    # 站点根视为入口页
    assert client.get("/site/").status_code == 200
    assert client.get("/site").status_code == 200

    assert client.get("/site/unknown.html").status_code == 404


# ---------- 登记校验 ----------

def test_03_register_validation_errors(client):
    before = snapshot(client)

    # 旧地址已被真实页面占用
    resp = client.post("/redirects", json={"path": "/about.html", "target": "/docs/guide.html"})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "path is an existing page"

    # 目标不是当前真实页面
    resp = client.post("/redirects", json={"path": "/legacy.html", "target": "/ghost.html"})
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["reason"] == "target page not found"
    assert e["source"] == "/legacy.html" and e["target"] == "/ghost.html"

    # 路径格式非法（不带 /、含查询串）
    assert client.post(
        "/redirects", json={"path": "legacy.html", "target": "/about.html"}
    ).status_code == 422
    assert client.post(
        "/redirects", json={"path": "/legacy.html?x=1", "target": "/about.html"}
    ).status_code == 422

    assert snapshot(client) == before  # 页面、跳转、修订号均不变


def test_04_register_success_and_site_308(client):
    resp = client.post("/redirects", json={"path": "/legacy/guide.html", "target": "/docs/guide.html"})
    assert resp.status_code == 200, resp.text
    # 登记不推进页面修订号
    assert resp.json() == {
        "registered": True,
        "path": "/legacy/guide.html",
        "target": "/docs/guide.html",
        "revision": 1,
    }
    assert client.get("/revision").json() == {"revision": 1}
    assert client.get("/redirects").json()["redirects"] == {"/legacy/guide.html": "/docs/guide.html"}

    # 旧地址 -> 308 指向真实路径；跟随一次拿到真实页面 HTML
    resp = client.get("/site/legacy/guide.html", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/site/docs/guide.html"
    resp = client.get("/site/legacy/guide.html")
    assert resp.status_code == 200
    assert '<h2 id="intro">Guide</h2>' in resp.text

    # 重复登记同一旧地址 -> 422
    resp = client.post("/redirects", json={"path": "/legacy/guide.html", "target": "/about.html"})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "path is already a registered redirect"
    assert client.get("/redirects").json()["redirects"] == {"/legacy/guide.html": "/docs/guide.html"}


# ---------- 发布/合入守护 ----------

def test_05_publish_cannot_occupy_redirect_path(client):
    before = snapshot(client)
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [{"op": "upsert", "path": "/legacy/guide.html", "content": "<p>占用旧地址</p>"}],
    })
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["target"] == "/legacy/guide.html"
    assert e["reason"] == "path is occupied by a redirect"
    assert snapshot(client) == before  # 页面、跳转、修订号均不变


def test_06_publish_cannot_delete_redirect_targeted_page(client):
    before = snapshot(client)
    # 同一批内先改掉 index 的 guide 链接（链接校验可通过），再删除仍被跳转指向的页面
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [
            {"op": "upsert", "path": "/index.html",
             "content": '<h1 id="top">Home</h1><a href="/about.html">about</a>'},
            {"op": "delete", "path": "/docs/guide.html"},
        ],
    })
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e == {
        "source": "/legacy/guide.html",
        "target": "/docs/guide.html",
        "reason": "page is still the target of a redirect",
    }
    assert snapshot(client) == before


def test_07_merge_uses_same_redirect_guards(client):
    before = snapshot(client)

    # 合入不得占用旧地址
    resp = client.post("/merge", json={
        "base_revision": 1,
        "changes": [{"op": "upsert", "path": "/legacy/guide.html", "content": "<p>x</p>"}],
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "path is occupied by a redirect"

    # 合入不得删除仍被跳转指向的页面
    resp = client.post("/merge", json={
        "base_revision": 1,
        "changes": [
            {"op": "upsert", "path": "/index.html",
             "content": '<h1 id="top">Home</h1><a href="/about.html">about</a>'},
            {"op": "delete", "path": "/docs/guide.html"},
        ],
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "page is still the target of a redirect"

    assert snapshot(client) == before


# ---------- 搬迁联动 ----------

def test_08_relocate_registers_redirect_and_repoints_existing(client):
    resp = client.post("/relocate", json={
        "expected_revision": 1,
        "old_path": "/docs/guide.html",
        "new_path": "/manual/guide.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "revision": 2,
        "relocated": True,
        "old_path": "/docs/guide.html",
        "new_path": "/manual/guide.html",
    }

    # 腾出的旧路径登记为跳转；此前指向该页的跳转改指新路径（不形成跳转链）
    assert client.get("/redirects").json()["redirects"] == {
        "/docs/guide.html": "/manual/guide.html",
        "/legacy/guide.html": "/manual/guide.html",
    }

    # 旧地址 308 直达真实路径（/legacy/guide.html 不是跳到 /docs/guide.html 再跳一次）
    resp = client.get("/site/docs/guide.html", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/site/manual/guide.html"
    resp = client.get("/site/legacy/guide.html", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/site/manual/guide.html"
    assert '<h2 id="intro">Guide</h2>' in client.get("/site/manual/guide.html").text


def test_09_relocate_to_occupied_redirect_path_fails(client):
    before = snapshot(client)
    for new_path in ("/docs/guide.html", "/legacy/guide.html"):
        resp = client.post("/relocate", json={
            "expected_revision": 2,
            "old_path": "/about.html",
            "new_path": new_path,
        })
        assert resp.status_code == 422
        assert resp.json()["errors"][0]["reason"] == "new path is occupied by a redirect"
    assert snapshot(client) == before


def test_10_register_target_must_be_real_page_not_redirect(client):
    before = snapshot(client)
    # 目标是跳转路径（不是真实页面）-> 拒绝，从登记侧也不形成跳转链
    resp = client.post("/redirects", json={"path": "/another.html", "target": "/docs/guide.html"})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "target page not found"
    assert snapshot(client) == before


def test_11_second_relocate_keeps_redirects_chain_free(client):
    resp = client.post("/relocate", json={
        "expected_revision": 2,
        "old_path": "/manual/guide.html",
        "new_path": "/book/guide.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 3

    # 所有旧地址都直达最新真实路径
    assert client.get("/redirects").json()["redirects"] == {
        "/docs/guide.html": "/book/guide.html",
        "/legacy/guide.html": "/book/guide.html",
        "/manual/guide.html": "/book/guide.html",
    }
    for old in ("/docs/guide.html", "/legacy/guide.html", "/manual/guide.html"):
        resp = client.get("/site" + old, follow_redirects=False)
        assert resp.status_code == 308
        assert resp.headers["location"] == "/site/book/guide.html"
    assert '<h2 id="intro">Guide</h2>' in client.get("/site/book/guide.html").text


def test_12_href_to_redirect_is_not_a_real_page(client):
    before = snapshot(client)
    # 站内 href 仍按真实页面校验：链接到旧地址（跳转路径）视为目标不存在
    resp = client.post("/publish", json={
        "expected_revision": 3,
        "changes": [{"op": "upsert", "path": "/linker.html",
                     "content": '<a href="/legacy/guide.html">old address</a>'}],
    })
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["source"] == "/linker.html"
    assert e["target"] == "/legacy/guide.html"
    assert e["reason"] == "target page not found"
    assert snapshot(client) == before


# ---------- 撤销与复用 ----------

def test_13_revoke_then_old_address_reusable(client):
    before = snapshot(client)

    # 撤销不存在的跳转 -> 404，状态不变
    resp = client.request("DELETE", "/redirects", json={"path": "/no/such.html"})
    assert resp.status_code == 404
    assert resp.json() == {"detail": "redirect not found", "path": "/no/such.html"}
    assert snapshot(client) == before

    # 撤销成功：不推进修订号；旧地址立即变为未知路径（站点读取 404）
    resp = client.request("DELETE", "/redirects", json={"path": "/legacy/guide.html"})
    assert resp.status_code == 200
    assert resp.json() == {"revoked": True, "path": "/legacy/guide.html", "revision": 3}
    assert client.get("/revision").json() == {"revision": 3}
    assert client.get("/site/legacy/guide.html").status_code == 404

    # 撤销后旧地址可被发布复用
    rev = publish(client, 3, [
        {"op": "upsert", "path": "/legacy/guide.html",
         "content": '<a href="/index.html#top">reused old address</a>'},
    ])
    assert rev == 4
    resp = client.get("/site/legacy/guide.html")
    assert resp.status_code == 200
    assert "reused old address" in resp.text


# ---------- 重启后跳转仍有效 ----------

def test_14_redirects_survive_restart(client):
    expected = {
        "/docs/guide.html": "/book/guide.html",
        "/manual/guide.html": "/book/guide.html",
    }
    assert client.get("/redirects").json()["redirects"] == expected

    # 模拟进程重启：新建 TestClient（重跑启动初始化），数据库文件不变
    with TestClient(app) as restarted:
        assert restarted.get("/redirects").json()["redirects"] == expected
        resp = restarted.get("/site/docs/guide.html", follow_redirects=False)
        assert resp.status_code == 308
        assert resp.headers["location"] == "/site/book/guide.html"


# ---------- 历史快照不受影响 ----------

def test_15_historical_snapshots_unaffected(client):
    # 跳转登记/撤销/搬迁联动都不改写历史修订快照
    rev1 = client.get("/pages", params={"revision": 1}).json()["pages"]
    assert set(rev1) == {"/index.html", "/about.html", "/docs/guide.html"}
    assert '<a href="/docs/guide.html">guide</a>' in rev1["/index.html"]

    rev2 = client.get("/pages", params={"revision": 2}).json()["pages"]
    assert "/manual/guide.html" in rev2 and "/docs/guide.html" not in rev2

    # 跳转从不进入页面快照；当前集合含复用后成为真实页面的旧地址
    current = client.get("/pages").json()
    assert current["revision"] == 4
    assert set(current["pages"]) == {
        "/index.html", "/about.html", "/book/guide.html", "/legacy/guide.html",
    }
