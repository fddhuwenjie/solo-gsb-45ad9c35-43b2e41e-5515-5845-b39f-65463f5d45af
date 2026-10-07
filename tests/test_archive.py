"""离线 ZIP 下载（/download）端到端与归档构建单测。运行：pytest tests/test_archive.py"""

from __future__ import annotations

import io
import os
import posixpath
import re
import tempfile
import zipfile

import pytest

# 必须在导入 app 之前指定数据库文件
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DOCS_DB_PATH"] = _TMP.name

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.archive import ArchiveError, build_archive  # noqa: E402
from app.linkcheck import analyze, is_external  # noqa: E402
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


def fetch_zip(client):
    resp = client.get("/download")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    return resp, zipfile.ZipFile(io.BytesIO(resp.content))


def snapshot(client):
    """当前 (修订号, 页面集合, 跳转表)：用于校验下载失败后三者均不变。"""
    revision = client.get("/revision").json()["revision"]
    pages = client.get("/pages").json()["pages"]
    redirects = client.get("/redirects").json()["redirects"]
    return revision, pages, redirects


def _strip_comments(html: str) -> str:
    """去掉 HTML 注释（注释里的伪链接不属于可点击锚点，归档原样保留）。"""
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


INDEX_HTML = (
    '<html><body>\n'
    '<h1 id="top">首页</h1>\n'
    '<a href="/docs/guide.html?lang=zh#intro">绝对+查询+片段</a>\n'
    '<a href="about.html">相对</a>\n'
    '<a href="/docs/">目录形式</a>\n'
    '<a href="#top">纯片段</a>\n'
    '<a href="?v=1">纯查询</a>\n'
    '<a href="https://example.com/x">外链</a>\n'
    '<a href="//cdn.example.com/lib.js">协议相对</a>\n'
    '<a href="mailto:a@b.com">邮件</a>\n'
    '<a title="hint href=\'/docs/bait.html\'" href="/docs/detail.html">伪href</a>\n'
    '<img src="/docs/guide.html">\n'
    '<!-- <a href="/docs/guide.html">注释里的链接</a> -->\n'
    '</body></html>'
)

ABOUT_HTML = (
    '<a href="/index.html#top">home abs</a>'
    '<a href="index.html">home rel</a>'
)

GUIDE_HTML = (
    '<h2 id="intro">Guide</h2>'
    '<a href="../index.html#top">home rel</a>'
    '<a href="/about.html">about abs</a>'
    '<a href="detail.html">detail rel</a>'
)

DETAIL_HTML = (
    '<a href="guide.html#intro">back</a>'
    '<a href="/">root dir</a>'
)

DOCS_INDEX_HTML = '<a href="../index.html">up</a>'


# ---------- 尚未首次发布 ----------

def test_01_download_before_first_publish_404(client):
    assert client.get("/revision").json() == {"revision": 0}
    resp = client.get("/download")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "no published revision to download", "revision": 0}


# ---------- 建景 ----------

def test_02_publish_site(client):
    rev = publish(client, 0, [
        {"op": "upsert", "path": "/index.html", "content": INDEX_HTML},
        {"op": "upsert", "path": "/about.html", "content": ABOUT_HTML},
        {"op": "upsert", "path": "/docs/guide.html", "content": GUIDE_HTML},
        {"op": "upsert", "path": "/docs/detail.html", "content": DETAIL_HTML},
        {"op": "upsert", "path": "/docs/index.html", "content": DOCS_INDEX_HTML},
    ])
    assert rev == 1


def test_03_register_redirect_and_relocate(client):
    # 登记旧地址跳转（不推进修订号）
    resp = client.post("/redirects", json={"path": "/legacy/home.html", "target": "/index.html"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 1

    # 搬迁产生第二个旧地址：/docs/detail.html -> /manual/detail.html
    resp = client.post("/relocate", json={
        "expected_revision": 1,
        "old_path": "/docs/detail.html",
        "new_path": "/manual/detail.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2
    assert client.get("/redirects").json()["redirects"] == {
        "/docs/detail.html": "/manual/detail.html",
        "/legacy/home.html": "/index.html",
    }


# ---------- 下载：布局与响应头 ----------

def test_04_download_headers_and_layout(client):
    resp, zf = fetch_zip(client)
    assert resp.headers["content-disposition"] == (
        'attachment; filename="docs-archive-r2.zip"'
    )
    # 页面落盘为同名相对路径；旧址位置是离线入口文件
    assert set(zf.namelist()) == {
        "index.html",
        "about.html",
        "docs/guide.html",
        "docs/index.html",
        "manual/detail.html",
        "docs/detail.html",   # 旧址离线入口
        "legacy/home.html",   # 旧址离线入口
    }


def test_05_index_links_rewritten_to_archive_relative(client):
    _, zf = fetch_zip(client)
    body = zf.read("index.html").decode("utf-8")
    # 绝对/相对/目录形式站内 href -> 归档内真实文件的相对地址，查询串与片段保留
    assert '<a href="docs/guide.html?lang=zh#intro">绝对+查询+片段</a>' in body
    assert '<a href="about.html">相对</a>' in body
    assert '<a href="docs/index.html">目录形式</a>' in body  # 目录形式指向真实文件
    # 伪 href 的 title 原样，真正的 href 被改写
    assert '<a title="hint href=\'/docs/bait.html\'" href="manual/detail.html">伪href</a>' in body
    # 纯片段/纯查询/外部链接原样
    for kept in (
        '<a href="#top">纯片段</a>',
        '<a href="?v=1">纯查询</a>',
        '<a href="https://example.com/x">外链</a>',
        '<a href="//cdn.example.com/lib.js">协议相对</a>',
        '<a href="mailto:a@b.com">邮件</a>',
    ):
        assert kept in body
    # 非链接内容原样
    assert '<img src="/docs/guide.html">' in body
    assert '<!-- <a href="/docs/guide.html">注释里的链接</a> -->' in body
    # 归档内不应再出现绝对站内 href（// 协议相对外链与注释中的伪链接除外）
    assert not re.search(r'href="/(?!/)', _strip_comments(body))


def test_06_subpage_links_rewritten(client):
    _, zf = fetch_zip(client)
    guide = zf.read("docs/guide.html").decode("utf-8")
    assert '<a href="../index.html#top">home rel</a>' in guide
    assert '<a href="../about.html">about abs</a>' in guide          # 绝对 -> 相对
    assert '<a href="../manual/detail.html">detail rel</a>' in guide  # 跟随搬迁结果

    detail = zf.read("manual/detail.html").decode("utf-8")
    assert '<a href="../docs/guide.html#intro">back</a>' in detail
    assert '<a href="../index.html">root dir</a>' in detail           # 根目录 -> 入口文件

    docs_index = zf.read("docs/index.html").decode("utf-8")
    assert '<a href="../index.html">up</a>' in docs_index

    about = zf.read("about.html").decode("utf-8")
    assert '<a href="index.html#top">home abs</a>' in about
    assert '<a href="index.html">home rel</a>' in about


def test_07_redirect_offline_entries(client):
    _, zf = fetch_zip(client)
    # 旧址离线入口：脚本直达真实页并保留查询串与片段；meta refresh 与 <a> 兜底
    legacy = zf.read("legacy/home.html").decode("utf-8")
    assert 'window.location.replace("../index.html" + window.location.search + window.location.hash)' in legacy
    assert '<meta http-equiv="refresh" content="0; url=../index.html">' in legacy
    assert '<a href="../index.html">../index.html</a>' in legacy

    moved = zf.read("docs/detail.html").decode("utf-8")
    assert 'window.location.replace("../manual/detail.html" + window.location.search + window.location.hash)' in moved
    assert '<meta http-equiv="refresh" content="0; url=../manual/detail.html">' in moved
    assert '<a href="../manual/detail.html">../manual/detail.html</a>' in moved


def test_08_archive_crawlable_from_index(client):
    """模拟离线浏览：从 index.html 出发循站内链接可到达全部页面文件。"""
    _, zf = fetch_zip(client)
    names = set(zf.namelist())
    seen: set[str] = set()
    stack = ["index.html"]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        html = _strip_comments(zf.read(name).decode("utf-8"))
        _ids, hrefs = analyze(html)  # 与发布校验同一口径解析真正的 <a href>
        for href in hrefs:
            if is_external(href):
                continue
            path_part = href.split("#", 1)[0].split("?", 1)[0]
            if not path_part:
                continue  # 纯片段/纯查询：留在当前文件
            assert not path_part.startswith("/"), f"{name}: 仍是绝对 href {href}"
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), path_part))
            assert target in names, f"{name} -> {href} 指向归档外"
            stack.append(target)
    # 所有真实页面都可从入口循链到达（旧址入口不被页面链接，单独核对）
    assert {
        "index.html", "about.html", "docs/guide.html",
        "docs/index.html", "manual/detail.html",
    } <= seen


# ---------- 只读与确定性 ----------

def test_09_download_is_readonly_and_deterministic(client):
    before = snapshot(client)
    site_before = client.get("/site/index.html").text
    rev1_before = client.get("/pages", params={"revision": 1}).json()

    resp1, _ = fetch_zip(client)
    resp2, _ = fetch_zip(client)
    assert resp1.content == resp2.content  # 同一快照重复下载字节一致

    # 不推进修订号，不改写 /pages 原文、历史快照、跳转表与 /site 响应
    assert snapshot(client) == before
    assert client.get("/site/index.html").text == site_before
    assert client.get("/pages", params={"revision": 1}).json() == rev1_before


# ---------- 无法安全且唯一映射：明确错误，不输出部分 ZIP ----------

def test_10_path_escaping_site_root_rejected(client):
    before = snapshot(client)
    rev = before[0]
    publish(client, rev, [
        {"op": "upsert", "path": "/a/../../b.html", "content": "<p>escape</p>"},
    ])
    resp = client.get("/download")
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/json")  # 不是 ZIP
    e = resp.json()["errors"][0]
    assert e["target"] == "/a/../../b.html"
    assert e["reason"] == "path escapes site root"
    # 清理后恢复可下载；失败期间修订号、页面与跳转均未被下载接口改变
    publish(client, rev + 1, [{"op": "delete", "path": "/a/../../b.html"}])
    assert snapshot(client) == (before[0] + 2, {**before[1]}, before[2])
    fetch_zip(client)


def test_11_normalized_collision_rejected(client):
    before = snapshot(client)
    rev = before[0]
    # /a/../index.html 规范化后与 /index.html 撞名
    publish(client, rev, [
        {"op": "upsert", "path": "/a/../index.html", "content": "<p>dup</p>"},
    ])
    resp = client.get("/download")
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["reason"] == "paths map to the same archive file"
    assert {e["source"], e["target"]} == {"/index.html", "/a/../index.html"}
    publish(client, rev + 1, [{"op": "delete", "path": "/a/../index.html"}])
    fetch_zip(client)


def test_12_file_directory_conflict_rejected(client):
    before = snapshot(client)
    rev = before[0]
    # /docs 落盘为文件 docs，与既有 docs/guide.html 的目录冲突
    publish(client, rev, [
        {"op": "upsert", "path": "/docs", "content": "<p>file</p>"},
    ])
    resp = client.get("/download")
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["reason"] == "archive file conflicts with directory of another entry"
    assert e["source"] == "/docs"
    publish(client, rev + 1, [{"op": "delete", "path": "/docs"}])
    fetch_zip(client)


def test_13_unsafe_characters_rejected(client):
    before = snapshot(client)
    rev = before[0]
    publish(client, rev, [
        {"op": "upsert", "path": '/a"b.html', "content": "<p>quote</p>"},
        {"op": "upsert", "path": "/a\\b.html", "content": "<p>backslash</p>"},
    ])
    resp = client.get("/download")
    assert resp.status_code == 422
    reasons = {(e["target"], e["reason"]) for e in resp.json()["errors"]}
    assert ('/a"b.html', "path is not a safe archive file name") in reasons
    assert ("/a\\b.html", "path is not a safe archive file name") in reasons
    publish(client, rev + 1, [
        {"op": "delete", "path": '/a"b.html'},
        {"op": "delete", "path": "/a\\b.html"},
    ])
    fetch_zip(client)


def test_14_unsafe_redirect_path_rejected(client):
    before = snapshot(client)
    # 旧址路径本身无法安全映射 -> 同样整体失败
    resp = client.post("/redirects", json={"path": "/../legacy.html", "target": "/index.html"})
    assert resp.status_code == 200, resp.text
    resp = client.get("/download")
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["target"] == "/../legacy.html"
    assert e["reason"] == "path escapes site root"
    resp = client.request("DELETE", "/redirects", json={"path": "/../legacy.html"})
    assert resp.status_code == 200
    fetch_zip(client)
    # 下载失败与恢复过程中修订号、页面集合未变（跳转表除登记/撤销外未变）
    assert snapshot(client) == before


# ---------- build_archive 单测（兜底分支） ----------

def test_unit_build_archive_link_to_missing_page():
    # 绕过发布校验构造：站内链接目标不在页面集合 -> 明确错误，不产出 ZIP
    pages = {"/index.html": '<a href="/ghost.html">ghost</a>'}
    with pytest.raises(ArchiveError) as excinfo:
        build_archive(pages, {})
    assert excinfo.value.errors == [
        {"source": "/index.html", "target": "/ghost.html", "reason": "target page not found"}
    ]


def test_unit_build_archive_redirect_target_missing():
    pages = {"/index.html": "<p>home</p>"}
    with pytest.raises(ArchiveError) as excinfo:
        build_archive(pages, {"/old.html": "/ghost.html"})
    assert excinfo.value.errors == [
        {"source": "/old.html", "target": "/ghost.html", "reason": "target page not found"}
    ]


def test_unit_build_archive_query_and_fragment_preserved():
    pages = {
        "/index.html": '<a href="/d/?a=1&amp;b=2#frag">dir</a>',
        "/d/index.html": "<p>d</p>",
    }
    data = build_archive(pages, {})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        body = zf.read("index.html").decode("utf-8")
    # 目录形式指向真实文件；查询串（含 HTML 实体）与片段原文保留
    assert '<a href="d/index.html?a=1&amp;b=2#frag">dir</a>' in body


def test_unit_build_archive_deterministic():
    pages = {"/index.html": "<p>home</p>"}
    redirects = {"/old.html": "/index.html"}
    assert build_archive(pages, redirects) == build_archive(pages, redirects)
