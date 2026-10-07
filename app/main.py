"""FastAPI 入口：静态文档包链接一致性发布 API。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator

from . import db
from .archive import ArchiveError, build_archive, load_snapshot
from .merge import MergeBaselineNotFound, MergeConflict, merge
from .prune import prune
from .publish import INDEX, PublishConflict, PublishValidationError, publish
from .redirects import RedirectValidationError, register_redirect, revoke_redirect
from .relocate import relocate
from .site import rewrite_html_for_site


def _validate_page_path_format(v: str) -> str:
    """页面路径格式：`/` 开头、非空、不含查询串与片段。"""
    if not v.startswith("/"):
        raise ValueError("path must start with '/'")
    if len(v) < 2:
        raise ValueError("path must not be empty")
    if "#" in v or "?" in v:
        raise ValueError("path must not contain query string or fragment")
    return v


class RelocateRequest(BaseModel):
    """页面搬迁：把已发布的旧页面移动到未占用的新路径，并重写受影响的站内 href。"""

    expected_revision: int = Field(ge=0, description="客户端预期的当前修订号，实现乐观锁")
    old_path: str = Field(description="已发布的旧页面路径，必须以 / 开头，且不能是 /index.html")
    new_path: str = Field(description="未占用的新页面路径，必须以 / 开头，且不能与旧路径相同")

    @field_validator("old_path", "new_path")
    @classmethod
    def _validate_relocate_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("path must start with '/'")
        if len(v) < 2:
            raise ValueError("path must not be empty")
        if "#" in v or "?" in v:
            raise ValueError("path must not contain query string or fragment")
        return v


class Change(BaseModel):
    """单条变更：新增/替换页面（upsert）或删除页面（delete）。"""

    op: Literal["upsert", "delete", "put"] = Field(
        description="upsert/put=新增或替换；delete=删除"
    )
    path: str = Field(description="页面路径，必须以 / 开头，如 /docs/intro.html")
    content: str | None = Field(default=None, description="upsert 时的完整 HTML 内容")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("path must start with '/'")
        if len(v) < 2:
            raise ValueError("path must not be empty")
        if "#" in v or "?" in v:
            raise ValueError("path must not contain query string or fragment")
        return v

    @field_validator("content")
    @classmethod
    def _validate_content(cls, v, info):
        op = info.data.get("op")
        if op in ("upsert", "put") and v is None:
            raise ValueError("content is required for upsert")
        return v


class PublishRequest(BaseModel):
    expected_revision: int = Field(ge=0, description="客户端预期的当前修订号，实现乐观锁")
    changes: list[Change] = Field(default_factory=list)


class MergeRequest(BaseModel):
    """并行合入：基于历史基线修订，把沿用发布接口格式的页面变更合入当前快照。"""

    base_revision: int = Field(ge=0, description="本端改动所基于的历史修订号（0 为空基线）")
    changes: list[Change] = Field(default_factory=list)


class PruneRequest(BaseModel):
    """不可达页面清理：删除从 /index.html 与现行跳转目标无法到达的页面。"""

    expected_revision: int = Field(ge=0, description="客户端预期的当前修订号，实现乐观锁")


class RedirectRegisterRequest(BaseModel):
    """登记旧地址跳转：旧地址沿用页面路径格式，目标须是当前真实页面。"""

    path: str = Field(description="旧地址（跳转路径），必须以 / 开头，且未被真实页面或其他跳转占用")
    target: str = Field(description="跳转目标，必须是当前真实页面路径")

    @field_validator("path", "target")
    @classmethod
    def _validate_redirect_path(cls, v: str) -> str:
        return _validate_page_path_format(v)


class RedirectRevokeRequest(BaseModel):
    """撤销旧地址跳转：撤销之后旧地址才可被发布/合入复用。"""

    path: str = Field(description="要撤销的旧地址（跳转路径），必须以 / 开头")

    @field_validator("path")
    @classmethod
    def _validate_revoke_path(cls, v: str) -> str:
        return _validate_page_path_format(v)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="静态文档包链接一致性发布 API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/revision")
def current_revision():
    conn = db.connect()
    try:
        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        return {"revision": row["current_revision"]}
    finally:
        conn.close()


@app.get("/pages")
def list_pages(revision: int | None = None):
    """返回同一修订的完整页面集合（path -> content）。

    不传 revision 时返回当前修订。revision 不存在返回 404。
    """
    conn = db.connect()
    try:
        current = conn.execute(
            "SELECT current_revision FROM meta WHERE id = 1"
        ).fetchone()["current_revision"]
        rev = current if revision is None else revision
        if rev < 0 or rev > current:
            raise HTTPException(status_code=404, detail=f"revision {rev} not found")
        if rev == 0:
            return {"revision": 0, "pages": {}}
        rows = conn.execute(
            "SELECT path, content FROM pages WHERE revision = ? ORDER BY path", (rev,)
        ).fetchall()
        return {"revision": rev, "pages": {row["path"]: row["content"] for row in rows}}
    finally:
        conn.close()


@app.post("/publish")
def publish_endpoint(req: PublishRequest):
    """批量新增/替换/删除页面。校验与发布在同一 SQLite 写事务内完成。"""
    conn = db.connect()
    try:
        new_revision = publish(
            conn,
            expected_revision=req.expected_revision,
            changes=[c.model_dump() for c in req.changes],
        )
        return {"revision": new_revision, "published": True}
    except PublishConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "revision conflict",
                "expected": req.expected_revision,
                "current": exc.current_revision,
            },
        )
    except PublishValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "validation failed", "errors": exc.errors},
        )
    finally:
        conn.close()


@app.post("/merge")
def merge_endpoint(req: MergeRequest):
    """基于历史基线修订的并行变更合入。比对、合入、校验与提交在同一 SQLite 写事务内完成。"""
    conn = db.connect()
    try:
        new_revision = merge(
            conn,
            base_revision=req.base_revision,
            changes=[c.model_dump() for c in req.changes],
        )
        return {"revision": new_revision, "merged": True, "base_revision": req.base_revision}
    except MergeConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "merge conflict",
                "base_revision": req.base_revision,
                "current": exc.current_revision,
                "conflicts": exc.conflicts,
            },
        )
    except MergeBaselineNotFound as exc:
        return JSONResponse(
            status_code=404,
            content={
                "detail": "base revision not found",
                "base_revision": exc.base_revision,
            },
        )
    except PublishValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "validation failed", "errors": exc.errors},
        )
    finally:
        conn.close()


@app.post("/relocate")
def relocate_endpoint(req: RelocateRequest):
    """搬迁单个已发布页面并改写受影响的站内 href。校验与发布在同一 SQLite 写事务内完成。"""
    conn = db.connect()
    try:
        new_revision = relocate(
            conn,
            expected_revision=req.expected_revision,
            old_path=req.old_path,
            new_path=req.new_path,
        )
        return {
            "revision": new_revision,
            "relocated": True,
            "old_path": req.old_path,
            "new_path": req.new_path,
        }
    except PublishConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "revision conflict",
                "expected": req.expected_revision,
                "current": exc.current_revision,
            },
        )
    except PublishValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "validation failed", "errors": exc.errors},
        )
    finally:
        conn.close()


@app.post("/prune")
def prune_endpoint(req: PruneRequest):
    """清理从 /index.html 与现行跳转目标不可达的页面。

    在同一个 SQLite 写事务内：从入口页与所有现行旧址跳转的真实目标出发，
    沿真正的站内 ``<a href>`` 遍历（相对、绝对及目录路径沿用发布时的解析
    规则，查询串与片段不改变目标，外链不参与），删除无法到达的全部页面
    （含互相引用的孤立页面簇），再以跳转守护与整包链接校验兜底。
    没有可删页面时不产生新修订；过期修订号返回 409，校验失败返回 422，
    页面、跳转与修订号均不变。
    """
    conn = db.connect()
    try:
        revision, deleted = prune(conn, expected_revision=req.expected_revision)
        return {"revision": revision, "pruned": bool(deleted), "deleted": deleted}
    except PublishConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "revision conflict",
                "expected": req.expected_revision,
                "current": exc.current_revision,
            },
        )
    except PublishValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "validation failed", "errors": exc.errors},
        )
    finally:
        conn.close()


@app.get("/redirects")
def list_redirects():
    """当前全部旧地址跳转（旧地址 -> 真实页面路径）。跳转是当前状态，不随修订号快照。"""
    conn = db.connect()
    try:
        rows = conn.execute("SELECT path, target FROM redirects ORDER BY path").fetchall()
        return {"redirects": {row["path"]: row["target"] for row in rows}}
    finally:
        conn.close()


@app.post("/redirects")
def register_redirect_endpoint(req: RedirectRegisterRequest):
    """登记旧地址跳转。校验与写入在同一 SQLite 写事务内完成；不推进页面修订号。"""
    conn = db.connect()
    try:
        register_redirect(conn, path=req.path, target=req.target)
        revision = conn.execute(
            "SELECT current_revision FROM meta WHERE id = 1"
        ).fetchone()["current_revision"]
        return {
            "registered": True,
            "path": req.path,
            "target": req.target,
            "revision": revision,
        }
    except RedirectValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "validation failed", "errors": exc.errors},
        )
    finally:
        conn.close()


@app.delete("/redirects")
def revoke_redirect_endpoint(req: RedirectRevokeRequest):
    """撤销旧地址跳转。不推进页面修订号；撤销后旧地址才可被复用。"""
    conn = db.connect()
    try:
        if not revoke_redirect(conn, req.path):
            return JSONResponse(
                status_code=404,
                content={"detail": "redirect not found", "path": req.path},
            )
        revision = conn.execute(
            "SELECT current_revision FROM meta WHERE id = 1"
        ).fetchone()["current_revision"]
        return {"revoked": True, "path": req.path, "revision": revision}
    finally:
        conn.close()


@app.get("/site")
@app.get("/site/{page_path:path}")
def serve_site(page_path: str = ""):
    """站点读取：真实路径返回 HTML；已登记旧地址 308 指向真实路径；未知路径 404。

    ``/site`` 与 ``/site/`` 视为入口页 ``/index.html``；308 的 ``Location``
    指向站点内真实页面的 URL（``/site`` 前缀 + 真实路径）。
    返回的 HTML 中绝对站内 href（``/`` 开头）改写为 ``/site`` 前缀下的同名
    路径（查询串与 ``#片段`` 原文保留），使浏览器点击后仍落在 ``/site``
    可读取的页面上；相对链接、纯片段、纯查询、外部链接与非链接 HTML 原样返回。
    改写只作用于本次响应，``/pages`` 与历史修订快照保持发布原文。
    """
    site_path = "/" + page_path if page_path else INDEX
    conn = db.connect()
    try:
        current = conn.execute(
            "SELECT current_revision FROM meta WHERE id = 1"
        ).fetchone()["current_revision"]
        row = conn.execute(
            "SELECT content FROM pages WHERE revision = ? AND path = ?",
            (current, site_path),
        ).fetchone()
        if row is not None:
            return HTMLResponse(content=rewrite_html_for_site(row["content"]))
        row = conn.execute(
            "SELECT target FROM redirects WHERE path = ?", (site_path,)
        ).fetchone()
        if row is not None:
            return RedirectResponse(url="/site" + row["target"], status_code=308)
        raise HTTPException(status_code=404, detail="page not found")
    finally:
        conn.close()


@app.get("/download")
def download_archive():
    """下载当前修订的离线 ZIP：页面 + 旧址离线入口。

    页面与跳转取自同一个 SQLite 读事务快照；解压后打开 ``index.html``
    即可循站内链接浏览，无需运行服务。尚未首次发布返回 404；任一路径
    无法安全且唯一地映射为归档文件时返回 422 并逐项列出，不输出部分 ZIP。
    只读接口：不推进修订号，不改写 ``/pages`` 原文、历史快照或 ``/site`` 响应。
    """
    conn = db.connect()
    try:
        revision, pages, redirects = load_snapshot(conn)
    finally:
        conn.close()
    if revision == 0:
        return JSONResponse(
            status_code=404,
            content={"detail": "no published revision to download", "revision": 0},
        )
    try:
        data = build_archive(pages, redirects)
    except ArchiveError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "archive export failed", "errors": exc.errors},
        )
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="docs-archive-r{revision}.zip"'
        },
    )
