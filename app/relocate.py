"""页面搬迁事务：移动单个已发布页面，并在同一写事务内改写受影响的站内 href。

搬迁语义：
- 旧路径（已发布且非 ``/index.html``）消失，新路径（未占用）接管同一正文与元素 id。
- 其他页面中解析后指向旧页的站内 href 改指新页（绝对 href 用绝对新路径，
  相对 href 用相对新路径），原始查询串与 ``#片段`` 文本原样保留。
- 搬迁页自身的相对 href 以新位置为基准重定位，使其原本指向的页面与片段仍可到达；
  纯片段（``#x``）/纯查询（``?x``）指向本页，无需改动；绝对 href 与位置无关，
  仅当其指向旧页本身（绝对自指）时改指新页。
- 外部链接、非链接内容（正文文本、注释、其他标签的属性等）一律不改。
- 旧地址联动（同一写事务内）：腾出的旧路径登记为跳转（旧地址 -> 新路径），
  此前指向旧页的既有跳转一并改指新路径——跳转永远直达真实页面，不形成跳转链；
  新路径不得是已占用的跳转路径。

沿用 :mod:`app.linkcheck` 的既有解析规则与 :mod:`app.htmlrewrite` 的字节级
href 改写，并在搬迁后的最终页面集合上再跑一次整包链接校验与跳转守护作为兜底：
新路径冲突或任一链接无法维持指向时整体回滚，
页面、跳转与修订号均不变。成功时在同一事务内生成新修订快照，历史修订仍可读取。
"""

from __future__ import annotations

import posixpath
import sqlite3
from html import unescape

from .htmlrewrite import rewrite_anchor_hrefs
from .linkcheck import check_links, is_external, resolve_target
from .publish import INDEX, PublishConflict, PublishValidationError, _load_current_pages
from .redirects import load_redirects, redirect_guard_errors


def _split_href(href: str) -> tuple[str, str, str]:
    """把 href 拆成 (路径部分, 含?的查询串原文, 含#的片段原文)，后两者保留原文。"""
    if "#" in href:
        before, fragment = href.split("#", 1)
        fragment = "#" + fragment
    else:
        before, fragment = href, ""
    if "?" in before:
        path_part, query = before.split("?", 1)
        query = "?" + query
    else:
        path_part, query = before, ""
    return path_part, query, fragment


def _is_directory_form(path_part: str) -> bool:
    """与 linkcheck.resolve_target 的目录判定保持一致。"""
    return path_part.endswith("/") or path_part.rstrip("/") == "."


def _repoint_other_page_href(source: str, old: str, new: str, href: str) -> str | None:
    """其他页面：解析后指向旧页的站内 href 改指新页；其余不改。

    ``href`` 是标签原文中的值（HTML 实体、查询串、片段均未解码），
    目标判定用反转义后的值，输出保留原始查询串与片段文本。
    """
    h = href.strip()
    if not h or is_external(h):
        return None
    target, _fragment, reason = resolve_target(source, unescape(h))
    if reason is not None or target != old:
        return None

    path_part, query, fragment = _split_href(h)
    if path_part.startswith("/"):
        base = new
    else:
        base = posixpath.relpath(new, posixpath.dirname(source))
    return base + query + fragment


def _rebase_moved_page_href(old: str, new: str, href: str) -> str | None:
    """搬迁页自身：以新位置为基准重定位相对 href；绝对 href 仅自指时改指新页。"""
    h = href.strip()
    if not h or is_external(h):
        return None

    path_part, query, fragment = _split_href(h)
    if path_part == "":
        return None  # #片段 / ?查询 / 空路径：仍指向本页，片段随 id 原样保留

    target, _fragment, reason = resolve_target(old, unescape(h))
    if reason is not None:
        return None  # 现有快照本就通过校验，不会发生；留给整包校验兜底

    new_dir = posixpath.dirname(new)

    if path_part.startswith("/"):
        # 绝对路径与页面位置无关；只有指向旧页自身的绝对链接需要改指新页。
        return new + query + fragment if target == old else None

    # 相对 href：以新页面所在目录为基准重新计算。
    if _is_directory_form(path_part):
        if target == old:
            # 以目录形式自指（如旧页是某目录的 index.html，href="."）：
            # 新位置是普通文件，直接给出相对到新页自身的文件名。
            base = posixpath.basename(new)
        else:
            rel = posixpath.relpath(posixpath.dirname(target), new_dir)
            base = "." if rel == "." else rel + "/"
    else:
        base = posixpath.relpath(target, new_dir)
    return base + query + fragment


def relocate(
    conn: sqlite3.Connection,
    expected_revision: int,
    old_path: str,
    new_path: str,
) -> int:
    """在单个写事务内完成页面搬迁、链接改写、跳转联动、整包校验与新修订提交。

    成功返回新修订号，同时把腾出的旧路径登记为跳转、并把此前指向旧页的
    既有跳转改指新路径（不形成跳转链）。过期修订号抛 :class:`PublishConflict`；
    新路径冲突（已占用，含新旧相同、含已占用的跳转路径）、旧页不存在、旧页为
    入口页或搬迁后链接无法维持指向时抛 :class:`PublishValidationError`，
    事务回滚，页面、跳转与修订号均不变。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        current = row["current_revision"]
        if expected_revision != current:
            raise PublishConflict(current)

        pages = _load_current_pages(conn, current)
        redirects = load_redirects(conn)

        pre_errors: list[dict[str, str]] = []
        if old_path == INDEX:
            pre_errors.append(
                {"source": "", "target": old_path, "reason": "entry page /index.html cannot be relocated"}
            )
        if old_path not in pages:
            pre_errors.append(
                {"source": "", "target": old_path, "reason": "source page not found"}
            )
        if new_path in pages:
            pre_errors.append(
                {"source": "", "target": new_path, "reason": "new path already exists"}
            )
        if new_path in redirects:
            pre_errors.append(
                {"source": "", "target": new_path, "reason": "new path is occupied by a redirect"}
            )
        if pre_errors:
            raise PublishValidationError(pre_errors)

        # 1) 改写其他页面指向旧页的站内 href。
        new_pages: dict[str, str] = {}
        for path, content in pages.items():
            if path == old_path:
                continue
            new_pages[path] = rewrite_anchor_hrefs(
                content, lambda h, s=path: _repoint_other_page_href(s, old_path, new_path, h)
            )

        # 2) 以新位置重定位搬迁页自身的相对 href，正文与 id 随内容原样保留。
        moved_content = rewrite_anchor_hrefs(
            pages[old_path], lambda h: _rebase_moved_page_href(old_path, new_path, h)
        )
        new_pages[new_path] = moved_content

        # 3) 沿用整包链接校验兜底：任一链接无法维持指向即整体失败。
        link_errors = check_links(new_pages)
        if link_errors:
            raise PublishValidationError(link_errors)

        # 4) 跳转联动（同一事务内）：腾出的旧路径登记为跳转；此前指向旧页的
        #    既有跳转改指新路径，跳转永远直达真实页面，不形成跳转链。
        new_redirects = {
            path: (new_path if target == old_path else target)
            for path, target in redirects.items()
        }
        new_redirects[old_path] = new_path
        guard_errors = redirect_guard_errors(new_redirects, new_pages)
        if guard_errors:  # 兜底：上述预检已保证不会发生
            raise PublishValidationError(guard_errors)

        # 5) 写入新修订快照、跳转联动并推进修订号（仍在同一事务内）。
        new_revision = current + 1
        conn.executemany(
            "INSERT INTO pages (revision, path, content) VALUES (?, ?, ?)",
            [(new_revision, path, content) for path, content in sorted(new_pages.items())],
        )
        conn.execute("UPDATE redirects SET target = ? WHERE target = ?", (new_path, old_path))
        conn.execute("INSERT INTO redirects (path, target) VALUES (?, ?)", (old_path, new_path))
        conn.execute("UPDATE meta SET current_revision = ? WHERE id = 1", (new_revision,))
        conn.commit()
        return new_revision
    except Exception:
        conn.rollback()
        raise
