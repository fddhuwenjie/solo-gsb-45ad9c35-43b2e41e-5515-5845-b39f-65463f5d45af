"""旧地址跳转：登记、撤销、站点读取与搬迁/发布联动。

跳转表 ``redirects``（旧地址 -> 当前真实页面路径）是**当前状态**，不随修订号快照：

- 登记：旧地址沿用页面路径格式（``/`` 开头、不含 ``?``/``#``），且未被真实页面
  或其他跳转占用；目标须是**当前真实页面**。因此跳转永远不会指向另一个跳转，
  不形成跳转链。
- 撤销：删除跳转记录；撤销之后旧地址才可被发布/合入复用。
- 登记与撤销只写 ``redirects`` 表，**不推进页面修订号**；跳转保存在 SQLite
  （文件型数据库）中，进程重启后仍有效。
- 发布/合入/搬迁在同一写事务内调用 :func:`redirect_guard_errors` 做跳转守护：
  最终页面集合不得占用跳转（旧地址）路径，也不得删除仍被跳转指向的页面；
  失败时整体回滚，页面、跳转与修订号均不变。
"""

from __future__ import annotations

import sqlite3


class RedirectValidationError(Exception):
    """登记/撤销跳转校验失败；跳转、页面与修订号均不变。"""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("redirect validation failed")


def load_redirects(conn: sqlite3.Connection) -> dict[str, str]:
    """读取当前全部跳转（旧地址 -> 真实页面路径）。"""
    rows = conn.execute("SELECT path, target FROM redirects").fetchall()
    return {row["path"]: row["target"] for row in rows}


def redirect_guard_errors(
    redirects: dict[str, str], pages: dict[str, str]
) -> list[dict[str, str]]:
    """跳转守护：最终页面集合不得占用跳转路径，不得删除仍被跳转指向的页面。

    返回与链接校验相同形状的错误列表（``source``/``target``/``reason``）；
    空列表表示通过。
    """
    errors: list[dict[str, str]] = []
    for path in sorted(redirects):
        if path in pages:
            errors.append(
                {"source": "", "target": path, "reason": "path is occupied by a redirect"}
            )
    for path in sorted(redirects):
        target = redirects[path]
        if target not in pages:
            errors.append(
                {
                    "source": path,
                    "target": target,
                    "reason": "page is still the target of a redirect",
                }
            )
    return errors


def _current_pages(conn: sqlite3.Connection) -> dict[str, str]:
    """当前修订的完整页面集合（与 publish._load_current_pages 同一口径）。"""
    current = conn.execute(
        "SELECT current_revision FROM meta WHERE id = 1"
    ).fetchone()["current_revision"]
    rows = conn.execute("SELECT path, content FROM pages WHERE revision = ?", (current,)).fetchall()
    return {row["path"]: row["content"] for row in rows}


def register_redirect(conn: sqlite3.Connection, path: str, target: str) -> None:
    """在单个写事务内登记旧地址跳转；不推进页面修订号。

    旧地址被真实页面或其他跳转占用、或目标不是当前真实页面时抛
    :class:`RedirectValidationError`，事务回滚，跳转、页面与修订号均不变。
    """
    try:
        # IMMEDIATE：立即获取写锁，校验与写入之间不会有其他写事务插入。
        conn.execute("BEGIN IMMEDIATE")

        pages = _current_pages(conn)
        redirects = load_redirects(conn)

        errors: list[dict[str, str]] = []
        if path in pages:
            errors.append(
                {"source": "", "target": path, "reason": "path is an existing page"}
            )
        if path in redirects:
            errors.append(
                {"source": "", "target": path, "reason": "path is already a registered redirect"}
            )
        if target not in pages:
            # 目标必须是真实页面（跳转路径不是页面），因此跳转不会指向跳转。
            errors.append(
                {"source": path, "target": target, "reason": "target page not found"}
            )
        if errors:
            raise RedirectValidationError(errors)

        conn.execute("INSERT INTO redirects (path, target) VALUES (?, ?)", (path, target))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def revoke_redirect(conn: sqlite3.Connection, path: str) -> bool:
    """在单个写事务内撤销旧地址跳转；返回跳转是否存在。不推进页面修订号。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM redirects WHERE path = ?", (path,))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
