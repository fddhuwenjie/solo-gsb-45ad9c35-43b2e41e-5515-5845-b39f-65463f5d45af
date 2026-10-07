"""端到端/单元测试。运行：pip install -r requirements.txt pytest && pytest"""

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
from app.linkcheck import is_external, resolve_target  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    db.init_db()
    with TestClient(app) as c:
        yield c


# ---------- 链接解析纯函数 ----------

@pytest.mark.parametrize(
    "href,expected",
    [
        ("https://example.com/x", True),
        ("http://a.com", True),
        ("//cdn.example/lib.js", True),
        ("mailto:a@b.com", True),
        ("tel:+123", True),
        ("/a.html", False),
        ("a.html", False),
        ("#section", False),
        ("/dir/?x=1#y", False),
    ],
)
def test_is_external(href, expected):
    assert is_external(href) is expected


def test_resolve_absolute_and_relative():
    assert resolve_target("/index.html", "/docs/a.html") == ("/docs/a.html", None, None)
    assert resolve_target("/docs/a.html", "b.html") == ("/docs/b.html", None, None)
    assert resolve_target("/docs/a.html", "../index.html") == ("/index.html", None, None)
    assert resolve_target("/docs/a.html", "/x?a=1#top") == ("/x", "top", None)
    assert resolve_target("/docs/a.html", "#sec") == ("/docs/a.html", "sec", None)
    assert resolve_target("/docs/a.html", "/dir/") == ("/dir/index.html", None, None)
    assert resolve_target("/docs/a.html", "?x=1#frag") == ("/docs/a.html", "frag", None)


def test_resolve_escape():
    assert resolve_target("/a.html", "/../etc/passwd")[2] == "path escapes site root"
    assert resolve_target("/index.html", "../escape.html")[2] == "path escapes site root"
    assert resolve_target("/docs/sub/a.html", "../../../escape.html")[2] == "path escapes site root"
    # 恰好回到根不算越界
    assert resolve_target("/docs/sub/a.html", "../../escape.html") == ("/escape.html", None, None)


# ---------- HTTP 端到端 ----------

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_initial_state(client):
    assert client.get("/revision").json() == {"revision": 0}
    assert client.get("/pages").json() == {"revision": 0, "pages": {}}


def test_first_publish_requires_index(client):
    resp = client.post("/publish", json={
        "expected_revision": 0,
        "changes": [{"op": "upsert", "path": "/about.html", "content": "<p>x</p>"}],
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "first publish must include /index.html"
    # 修订号不变
    assert client.get("/revision").json() == {"revision": 0}


def test_first_publish_success(client):
    html = '<html><body><h1 id="top">Home</h1><a href="/about.html">about</a><a href="https://ext.com">ext</a></body></html>'
    about = '<html><body><h2 id="s2">About</h2><a href="/#top">home top</a></body></html>'
    resp = client.post("/publish", json={
        "expected_revision": 0,
        "changes": [
            {"op": "upsert", "path": "/index.html", "content": html},
            {"op": "upsert", "path": "/about.html", "content": about},
        ],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revision": 1, "published": True}

    pages = client.get("/pages").json()
    assert pages["revision"] == 1
    assert set(pages["pages"]) == {"/index.html", "/about.html"}


def test_bad_path_rejected(client):
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [{"op": "upsert", "path": "relative.html", "content": "x"}],
    })
    assert resp.status_code == 422


def test_broken_link_blocks_publish(client):
    # 新页面引用不存在页面 -> 整批拒绝，内容与修订号不变
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [
            {"op": "upsert", "path": "/new.html",
             "content": '<a href="/ghost.html">ghost</a><a href="/about.html#nope">bad anchor</a>'},
        ],
    })
    assert resp.status_code == 422
    errs = {(e["source"], e["reason"]) for e in resp.json()["errors"]}
    assert ("/new.html", "target page not found") in errs
    assert ("/new.html", "target anchor not found") in errs
    assert client.get("/revision").json() == {"revision": 1}
    assert "/new.html" not in client.get("/pages").json()["pages"]


def test_delete_referenced_page_blocks(client):
    # 删除 about.html，但 index.html 仍引用它 -> 报旧页引用被删页面
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [{"op": "delete", "path": "/about.html"}],
    })
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["source"] == "/index.html"
    assert e["target"] == "/about.html"
    assert e["reason"] == "target page not found"
    assert client.get("/revision").json() == {"revision": 1}


def test_delete_index_blocked(client):
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [
            {"op": "delete", "path": "/about.html"},
            {"op": "delete", "path": "/index.html"},
        ],
    })
    assert resp.status_code == 422
    assert "must not be deleted" in resp.json()["errors"][0]["reason"]


def test_replace_index_without_link_errors_ok(client):
    # 同时替换 index（去掉 about 链接）并删除 about -> 成功
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [
            {"op": "upsert", "path": "/index.html",
             "content": '<html><body><a href="https://ext.com">ext only</a></body></html>'},
            {"op": "delete", "path": "/about.html"},
        ],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2
    assert set(client.get("/pages").json()["pages"]) == {"/index.html"}


def test_query_string_does_not_change_target(client):
    # /index.html?v=2# 与 /index.html?x 查询串不应影响目标判定
    resp = client.post("/publish", json={
        "expected_revision": 2,
        "changes": [
            {"op": "upsert", "path": "/q.html",
             "content": '<a href="/index.html?v=2">query</a>'},
        ],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 3


def test_relative_links_and_directory_index(client):
    resp = client.post("/publish", json={
        "expected_revision": 3,
        "changes": [
            {"op": "upsert", "path": "/docs/guide.html",
             "content": '<a href="../index.html">up</a> <a href="/">dir</a>'},
        ],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 4


def test_path_escape_blocked(client):
    resp = client.post("/publish", json={
        "expected_revision": 4,
        "changes": [
            {"op": "upsert", "path": "/evil.html",
             "content": '<a href="/../etc/passwd">x</a>'},
        ],
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "path escapes site root"


def test_stale_revision_conflict(client):
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [{"op": "upsert", "path": "/x.html", "content": "<p>x</p>"}],
    })
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == "revision conflict"
    assert body["expected"] == 1
    assert body["current"] == 4
    assert client.get("/revision").json() == {"revision": 4}


def test_historical_revision_readable(client):
    rev3 = client.get("/pages", params={"revision": 3}).json()
    assert rev3["revision"] == 3
    assert set(rev3["pages"]) == {"/index.html", "/q.html"}

    missing = client.get("/pages", params={"revision": 99})
    assert missing.status_code == 404


def test_upsert_alias_put(client):
    resp = client.post("/publish", json={
        "expected_revision": 4,
        "changes": [{"op": "put", "path": "/putalias.html", "content": "<p>p</p>"}],
    })
    assert resp.status_code == 200


def test_removed_anchor_blocks_publish(client):
    # 现状（revision 5）：/index.html 含 id="top"，/q.html 以 /index.html?v=2 引用（无片段）。
    # 替换 index 去掉 id="top"，再新增引用该锚点的页面 -> anchor 错误，整体拒绝。
    body_before = client.get("/pages", params={"revision": 5}).json()
    assert "/putalias.html" in body_before["pages"]
    resp = client.post("/publish", json={
        "expected_revision": 5,
        "changes": [
            {"op": "upsert", "path": "/index.html",
             "content": '<h1>no ids anymore</h1>'},
            {"op": "upsert", "path": "/anchor-ref.html",
             "content": '<a href="/index.html#top">top</a>'},
        ],
    })
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["source"] == "/anchor-ref.html"
    assert e["target"] == "/index.html#top"
    assert e["reason"] == "target anchor not found"
    assert client.get("/revision").json() == {"revision": 5}


def test_empty_changes_is_valid_empty_publish(client):
    # 空变更集同样经历整批校验；内容不变，作为一次合法空发布推进修订号。
    before = client.get("/pages").json()
    resp = client.post("/publish", json={"expected_revision": 5, "changes": []})
    assert resp.status_code == 200
    assert resp.json()["revision"] == 6
    after = client.get("/pages").json()
    assert after["pages"] == before["pages"]
