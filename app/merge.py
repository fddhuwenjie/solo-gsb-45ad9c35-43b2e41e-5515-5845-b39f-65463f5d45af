"""并行变更合入事务：基于历史基线修订，把本端改动三路合入当前快照。

同一文档包的不同客户端可能从同一个旧修订分别改动。合入语义：

- 请求携带 ``base_revision``（本端改动所基于的历史修订）与沿用发布接口
  格式的页面变更（``upsert``/``put``/``delete``）。
- 即使基线已过期，也在**同一个 SQLite 写事务**内，逐个比对本端涉及路径
  在基线快照与当前快照中的存在性与正文；仅当路径未被其他人改动
  （不存在或正文完全相同）时才允许合入，否则整批拒绝（409），不写入。
- 无同路径冲突时，按提交顺序把变更应用于**当前**快照（其他人新增/修改
  的页面原样保留），再沿用入口页规则与整包站内链接校验；其他人的并行
  改动若使合入后的最终链接失效，同样整批拒绝（422），逐项列出来源与
  错误目标，内容与修订号均不变。
- 全部通过才生成一个新修订完整快照；历史修订快照（含基线）保留可读取。
"""

from __future__ import annotations

import sqlite3

from .linkcheck import check_links
from .publish import (
    PublishValidationError,
    _load_current_pages,
    _write_new_revision,
    apply_changes,
    check_entry_page,
)
from .redirects import load_redirects, redirect_guard_errors


class MergeBaselineNotFound(Exception):
    """基线修订号不存在（0 始终有效，表示空基线）。"""

    def __init__(self, base_revision: int) -> None:
        self.base_revision = base_revision
        super().__init__(f"base revision {base_revision} not found")


class MergeConflict(Exception):
    """本端涉及的路径已被其他并行改动修改；整批不合入。"""

    def __init__(self, current_revision: int, conflicts: list[dict[str, str]]) -> None:
        self.current_revision = current_revision
        self.conflicts = conflicts
        super().__init__(
            f"parallel merge conflict at revision {current_revision}: "
            f"{[c['path'] for c in conflicts]}"
        )


def _path_conflicts(
    base: dict[str, str], current: dict[str, str], paths: list[str]
) -> list[dict[str, str]]:
    """逐个比对涉及路径在基线与当前快照中的存在性与正文。

    路径在两个快照中都不存在，或正文完全相同，则未被其他人改动；
    否则报告冲突并说明原因。
    """
    conflicts: list[dict[str, str]] = []
    for path in sorted(paths):
        in_base = path in base
        in_current = path in current
        if in_base != in_current:
            if in_base:
                reason = "path deleted by another change"
            else:
                reason = "path added by another change"
        elif in_base and base[path] != current[path]:
            reason = "path modified by another change"
        else:
            continue
        conflicts.append({"path": path, "reason": reason})
    return conflicts


def _revision_has_snapshot(conn: sqlite3.Connection, revision: int) -> bool:
    """该修订号是否存在快照行（空发布也会复制整包快照，故有行）。"""
    row = conn.execute(
        "SELECT 1 FROM pages WHERE revision = ? LIMIT 1", (revision,)
    ).fetchone()
    return row is not None


def merge(
    conn: sqlite3.Connection,
    base_revision: int,
    changes: list[dict[str, str]],
) -> int:
    """在单个写事务内完成基线比对、合入、校验与新修订提交。

    成功返回新修订号（当前修订号 + 1）。基线不存在抛
    :class:`MergeBaselineNotFound`；同路径被并行改动抛
    :class:`MergeConflict`（整批不写入）；入口页或链接校验失败抛
    :class:`PublishValidationError`（事务回滚，内容与修订号均不变）。
    """
    try:
        # IMMEDIATE：立即获取写锁；基线比对、合入、校验与提交之间
        # 不会有其他写事务插入。
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        current_rev = row["current_revision"]
        if base_revision < 0 or base_revision > current_rev:
            raise MergeBaselineNotFound(base_revision)

        # revision 0 是始终有效的空基线；其余修订必须存在快照行
        # （current_rev 必然有快照行，无需额外查询）。
        if base_revision != 0 and not _revision_has_snapshot(conn, base_revision):
            raise MergeBaselineNotFound(base_revision)

        base_pages = (
            {} if base_revision == 0 else _load_current_pages(conn, base_revision)
        )
        current_pages = _load_current_pages(conn, current_rev)

        # 1) 即使基线已过期，也逐个比对本批涉及路径的存在性与正文；
        #    同一路径在批次内出现多次只报告一次。
        touched = list(dict.fromkeys(change["path"] for change in changes))
        conflicts = _path_conflicts(base_pages, current_pages, touched)
        if conflicts:
            raise MergeConflict(current_rev, conflicts)

        # 2) 无同路径冲突：按提交顺序把变更应用于当前快照——
        #    其他人新增/修改但本批未涉及的页面原样保留。
        pages = apply_changes(current_pages, changes)

        # 3) 沿用入口页规则、跳转守护（不得占用跳转路径、不得删除仍被跳转
        #    指向的页面）与整包站内链接校验（兜底其他人并行改动造成的失效
        #    链接，逐项列出来源与错误目标）。
        check_entry_page(pages, current_rev)
        errors = redirect_guard_errors(load_redirects(conn), pages) + check_links(pages)
        if errors:
            raise PublishValidationError(errors)

        # 4) 全部通过：生成一个新修订完整快照；历史快照保留可读取。
        new_revision = _write_new_revision(conn, current_rev, pages)
        conn.commit()
        return new_revision
    except Exception:
        conn.rollback()
        raise
