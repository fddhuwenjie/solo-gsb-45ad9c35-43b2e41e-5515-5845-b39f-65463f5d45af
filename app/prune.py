"""不可达页面清理事务：从入口页与现行跳转目标出发遍历站内链接，删除不可达页面。

已发布文档可能留下从首页看不到的页面（改版、搬迁后失去引用的旧页）。清理语义：

- **遍历起点**：入口页 ``/index.html`` 与所有现行旧址跳转的真实目标——
  跳转目标及其可达页面必须保留。
- **遍历规则**：沿真正的站内 ``<a href>`` 逐个解析（复用
  :mod:`app.linkcheck` 的页面分析，属性值里形似 ``href=`` 的文本不是真正的
  href，不参与遍历）；相对、绝对及目录路径沿用发布时的解析规则，查询串与
  ``#片段`` 不改变目标，外部链接不参与。
- **删除范围**：从入口无法到达的全部页面——包括互相引用但整体不可达的
  孤立页面簇。
- **结果**：没有可删页面时不产生新修订；否则删除后的最终页面集合沿用
  跳转守护与整包站内链接校验兜底，全部通过才写入新修订。
- 过期 ``expected_revision`` 或校验失败时整体回滚，页面、跳转与修订号
  均不变；首次发布前（revision 0）明确报错。全部在**同一个 SQLite 写事务**
  内完成；历史修订快照仍可读取。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from .linkcheck import analyze, check_links, is_external, resolve_target
from .publish import (
    INDEX,
    PublishConflict,
    PublishValidationError,
    _load_current_pages,
    _write_new_revision,
)
from .redirects import load_redirects, redirect_guard_errors


def reachable_pages(pages: dict[str, str], seeds: Iterable[str]) -> set[str]:
    """从种子页面出发，沿真正的站内 ``<a href>`` 可达的页面集合。

    解析规则与发布校验一致：相对、绝对及目录路径按
    :func:`app.linkcheck.resolve_target` 解析；查询串与 ``#片段`` 不改变
    目标；外部链接与无法解析（越过站点根等）的 href 不参与遍历。
    不在页面集合中的种子被忽略。
    """
    seen: set[str] = set()
    stack = [seed for seed in seeds if seed in pages]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        _ids, hrefs = analyze(pages[path])
        for raw_href in hrefs:
            href = raw_href.strip()
            if not href or is_external(href):
                continue
            target, _fragment, reason = resolve_target(path, href)
            if reason is not None or target is None:
                continue
            if target in pages and target not in seen:
                stack.append(target)
    return seen


def prune(
    conn: sqlite3.Connection,
    expected_revision: int,
) -> tuple[int, list[str]]:
    """在单个写事务内完成可达性遍历、不可达页面删除、兜底校验与新修订提交。

    返回 ``(结果修订号, 按路径排序的删除清单)``；没有可删页面时不产生
    新修订，返回 ``(当前修订号, [])``。过期修订号抛
    :class:`PublishConflict`；首次发布前或删除后校验失败抛
    :class:`PublishValidationError`，事务回滚，页面、跳转与修订号均不变。
    """
    try:
        # IMMEDIATE：立即获取写锁（事务内的读也是可重复读），
        # 遍历、校验与提交之间不会有其他写事务插入。
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        current = row["current_revision"]
        if expected_revision != current:
            raise PublishConflict(current)
        if current == 0:
            raise PublishValidationError(
                [{"source": "", "target": "", "reason": "no published revision to prune"}]
            )

        pages = _load_current_pages(conn, current)
        redirects = load_redirects(conn)

        # 1) 遍历起点：入口页 /index.html + 所有现行旧址跳转的真实目标
        #    （跳转目标及其可达页面必须保留）。
        keep = reachable_pages(pages, {INDEX, *redirects.values()})

        # 2) 从入口无法到达的全部页面（含互相引用的孤立页面簇）即为删除集。
        deleted = sorted(set(pages) - keep)
        if not deleted:
            # 没有可删页面：不产生新修订（事务内无写入，直接提交只读事务）。
            conn.commit()
            return current, []

        final = {path: content for path, content in pages.items() if path in keep}

        # 3) 兜底：删除后的最终页面集合沿用跳转守护与整包站内链接校验。
        errors = redirect_guard_errors(redirects, final) + check_links(final)
        if errors:
            raise PublishValidationError(errors)

        # 4) 全部通过：写入新修订快照并推进修订号（仍在同一事务内）。
        new_revision = _write_new_revision(conn, current, final)
        conn.commit()
        return new_revision, deleted
    except Exception:
        conn.rollback()
        raise
