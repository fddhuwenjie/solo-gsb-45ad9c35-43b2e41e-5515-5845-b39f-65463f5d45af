"""发布事务：整批试用 -> 链接核对 -> 提交，全部在同一个 SQLite 写事务内完成。"""

from __future__ import annotations

import sqlite3

from .linkcheck import check_links
from .redirects import load_redirects, redirect_guard_errors

INDEX = "/index.html"


class PublishConflict(Exception):
    """客户端携带的 expected_revision 已过期。"""

    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__(f"expected revision is stale; current revision is {current_revision}")


class PublishValidationError(Exception):
    """整批改动试用后校验不通过；内容与修订号保持不变。"""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("publish validation failed")


def _load_current_pages(conn: sqlite3.Connection, revision: int) -> dict[str, str]:
    rows = conn.execute("SELECT path, content FROM pages WHERE revision = ?", (revision,)).fetchall()
    return {row["path"]: row["content"] for row in rows}


def apply_changes(pages: dict[str, str], changes: list[dict[str, str]]) -> dict[str, str]:
    """按数组顺序在内存快照上试用整批改动，返回最终页面集合（不写库）。

    ``op`` 为 ``upsert``（别名 ``put``）或 ``delete``；未知操作抛
    :class:`PublishValidationError`。
    """
    pages = dict(pages)
    for change in changes:
        op = change["op"]
        path = change["path"]
        if op in ("upsert", "put"):
            pages[path] = change["content"]
        elif op == "delete":
            pages.pop(path, None)
        else:  # pragma: no cover - 入口处已枚举校验
            raise PublishValidationError(
                [{"source": path, "target": "", "reason": f"unknown operation: {op}"}]
            )
    return pages


def check_entry_page(pages: dict[str, str], current_revision: int) -> None:
    """入口页规则：首次发布必须包含 /index.html；此后不得删除入口。"""
    if current_revision == 0:
        if INDEX not in pages:
            raise PublishValidationError(
                [{"source": "", "target": INDEX, "reason": "first publish must include /index.html"}]
            )
    elif INDEX not in pages:
        raise PublishValidationError(
            [{"source": "", "target": INDEX, "reason": "entry page /index.html must not be deleted"}]
        )


def _write_new_revision(
    conn: sqlite3.Connection, current: int, pages: dict[str, str]
) -> int:
    """写入新修订完整快照并推进修订号（调用方负责事务边界）。"""
    new_revision = current + 1
    conn.executemany(
        "INSERT INTO pages (revision, path, content) VALUES (?, ?, ?)",
        [(new_revision, path, content) for path, content in sorted(pages.items())],
    )
    conn.execute("UPDATE meta SET current_revision = ? WHERE id = 1", (new_revision,))
    return new_revision


def publish(
    conn: sqlite3.Connection,
    expected_revision: int,
    changes: list[dict[str, str]],
) -> int:
    """在单个写事务内完成整批试用、链接校验与发布。

    成功返回新修订号（旧修订号 + 1）。过期修订号抛 :class:`PublishConflict`；
    校验失败抛 :class:`PublishValidationError`，事务回滚，
    已发布内容与修订号均不变。
    """
    try:
        # IMMEDIATE：立即获取写锁（事务内的读也是可重复读），
        # 校验与提交之间不会有其他写事务插入。
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        current = row["current_revision"]
        if expected_revision != current:
            raise PublishConflict(current)

        # 1) 在已发布页面上试用整批改动，得到最终页面集合（不写库）。
        pages = apply_changes(_load_current_pages(conn, current), changes)

        # 2) 入口页规则：首次发布必须包含 /index.html；此后不得删除入口。
        check_entry_page(pages, current)

        # 3) 跳转守护：最终页面集合不得占用跳转（旧地址）路径，
        #    也不得删除仍被跳转指向的页面。
        # 4) 核对最终所有页面的站内 href（相对、绝对、#片段；查询参数不影响目标）。
        errors = redirect_guard_errors(load_redirects(conn), pages) + check_links(pages)
        if errors:
            raise PublishValidationError(errors)

        # 5) 全部通过：写入新修订快照并推进修订号（仍在同一事务内）。
        new_revision = _write_new_revision(conn, current, pages)
        conn.commit()
        return new_revision
    except Exception:
        conn.rollback()
        raise
