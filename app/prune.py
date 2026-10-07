"""不可达页面清理事务：从首页与现行跳转目标出发遍历可达页面，删除其余页面。

已发布文档可能留下从首页看不到的页面（历史改版、搬迁遗留等）。清理语义：

- **入口集合**：``/index.html`` 与所有现行旧址跳转的**真实目标**——跳转目标
  及其可达页面必须保留。
- **遍历规则**：沿页面内真正的站内 ``<a href>`` 遍历；相对、绝对及目录路径
  沿用发布时的解析规则（:func:`app.linkcheck.resolve_target`），查询串与
  ``#片段`` 不改变目标，外部链接不参与。
- **删除**：从入口不可达的全部页面——包括互相引用形成闭环的孤立页面——
  在同一个 SQLite 写事务内删除，并生成新修订快照；历史修订快照保留可读取。
- **兜底**：删除后的页面集合仍须通过既有整包链接校验与跳转守护；失败时整体
  回滚，页面、跳转与修订号均不变。
- **边界**：没有可删页面时不产生新修订；首次发布前（revision 0）明确报错；
  过期 ``expected_revision`` 按既有冲突规则处理（409）。
"""

from __future__ import annotations

import sqlite3

from .linkcheck import analyze, check_links, is_external, resolve_target
from .publish import (
    INDEX,
    PublishConflict,
    PublishValidationError,
    _load_current_pages,
    _write_new_revision,
)
from .redirects import load_redirects, redirect_guard_errors


class PruneNotPublished(Exception):
    """首次发布前没有可清理的页面集合。"""

    def __init__(self) -> None:
        super().__init__("no published revision to prune")


def reachable_pages(pages: dict[str, str], entries: set[str]) -> set[str]:
    """从入口页面出发，沿站内 ``<a href>`` 可达的页面集合（发布时的解析规则）。

    相对、绝对及目录路径按 :func:`resolve_target` 解析；查询串与 ``#片段``
    不改变目标；外部链接与无法解析的 href 不产生新目标。
    """
    seen: set[str] = set()
    stack = [path for path in entries if path in pages]
    while stack:
        source = stack.pop()
        if source in seen:
            continue
        seen.add(source)
        _ids, hrefs = analyze(pages[source])
        for raw_href in hrefs:
            href = raw_href.strip()
            if not href or is_external(href):
                continue
            target, _fragment, reason = resolve_target(source, href)
            if reason is not None or target is None:
                continue
            if target in pages and target not in seen:
                stack.append(target)
    return seen


def prune(conn: sqlite3.Connection, expected_revision: int) -> tuple[int, list[str]]:
    """在单个写事务内删除不可达页面、校验并提交新修订。

    成功返回 ``(结果修订号, 按路径排序的删除清单)``；没有可删页面时不推进
    修订号，返回 ``(当前修订号, [])``。过期修订号抛 :class:`PublishConflict`；
    首次发布前抛 :class:`PruneNotPublished`；删除后校验失败抛
    :class:`PublishValidationError`，事务回滚，页面、跳转与修订号均不变。
    """
    try:
        # IMMEDIATE：立即获取写锁，遍历、校验与提交之间不会有其他写事务插入。
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        current = row["current_revision"]
        if expected_revision != current:
            raise PublishConflict(current)
        if current == 0:
            raise PruneNotPublished()

        pages = _load_current_pages(conn, current)
        redirects = load_redirects(conn)

        # 1) 入口：首页 + 所有现行旧址跳转的真实目标（跳转目标及其可达页面必须保留）。
        entries = {INDEX, *redirects.values()}
        keep = reachable_pages(pages, entries)

        # 2) 不可达页面（含互相引用形成闭环的孤立页面）全部删除，清单按路径排序。
        deleted = sorted(path for path in pages if path not in keep)
        if not deleted:
            conn.commit()  # 没有可删页面：不产生新修订
            return current, []

        remaining = {path: content for path, content in pages.items() if path in keep}

        # 3) 删除后的页面集合仍须通过既有跳转守护与整包链接校验；
        #    失败时整体回滚，页面、跳转与修订号均不变。
        errors = redirect_guard_errors(redirects, remaining) + check_links(remaining)
        if errors:
            raise PublishValidationError(errors)

        # 4) 全部通过：写入新修订快照并推进修订号（仍在同一事务内）。
        new_revision = _write_new_revision(conn, current, remaining)
        conn.commit()
        return new_revision, deleted
    except Exception:
        conn.rollback()
        raise
