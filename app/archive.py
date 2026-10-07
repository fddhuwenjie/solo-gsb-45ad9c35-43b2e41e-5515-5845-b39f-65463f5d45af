"""离线 ZIP 归档：把当前修订的页面与现行旧址跳转打包成可下载的静态站点。

归档布局（ZIP 内一律为正斜杠相对路径）：

- 页面 ``/index.html`` → 归档根部 ``index.html``；``/docs/guide.html`` →
  ``docs/guide.html``，依此类推。解压后直接打开 ``index.html`` 即可循站内
  链接浏览，无需运行服务。
- 页面内**真正的** ``<a href>`` 中，绝对（``/a.html``）、相对（``b.html``、
  ``../x.html``）与目录形式（``/docs/``）的站内链接一律改写为**归档内真实
  文件的相对地址**（目录形式指向该目录的 ``index.html`` 文件，浏览器从文件
  系统打开也能到达），查询串与 ``#片段`` 原文保留；外部链接、纯片段、纯查询、
  非链接内容与其他属性（含属性值内形似 ``href=`` 的文本）一律不改——复用
  :mod:`app.htmlrewrite` 的字节级改写与 :mod:`app.linkcheck` 的解析规则。
- 每个现行旧址跳转 ``旧地址 -> 真实页`` 在旧地址对应的归档位置生成一个
  **离线入口** HTML：打开时 ``location.replace(目标 + location.search +
  location.hash)`` 直达真实页并保留查询串与片段；``<meta refresh>`` 与
  ``<a>`` 是禁用脚本时的兜底。

安全与唯一性：页面/旧址路径必须能**安全且唯一**地映射为归档文件——逐段规范化
（空段与 ``.`` 忽略、``..`` 上溯但不得越过站点根），段内不得含危险字符
（``\\ : " ' < > | *`` 与控制字符，不得以点或空格结尾）；规范化后不得撞名，
也不得出现文件与目录前缀冲突。任一映射失败、或任一站内链接无法落盘时抛
:class:`ArchiveError`，调用方整体报错，**不输出部分 ZIP**。

下载是只读操作：不推进修订号，不改写数据库正文、历史快照或 ``/site`` 响应。
"""

from __future__ import annotations

import io
import posixpath
import sqlite3
import zipfile
from html import unescape

from .htmlrewrite import rewrite_anchor_hrefs
from .linkcheck import is_external, resolve_target

# 归档文件路径段中禁止出现的字符：zip/Windows 安全（\ : | *），
# 以及不破坏 HTML 属性与脚本引用的引号/尖括号（" ' < >）。
_FORBIDDEN_SEGMENT_CHARS = set('\\:"\'<>|*')

# ZIP 条目使用固定时间戳：同一快照重复下载得到字节一致的归档。
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)

# 旧址离线入口模板：脚本直达真实页并保留当前 URL 的查询串与片段；
# <meta refresh> 与 <a> 为无脚本环境的兜底。{url} 是归档内相对地址，
# 路径段已经过危险字符检查，不含引号/尖括号，可安全嵌入。
_REDIRECT_ENTRY_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url={url}">
<title>页面已搬迁</title>
<script>window.location.replace("{url}" + window.location.search + window.location.hash);</script>
</head>
<body>
<p>此地址为旧页面入口，内容已搬迁至 <a href="{url}">{url}</a>。</p>
</body>
</html>
"""


class ArchiveError(Exception):
    """路径无法安全且唯一地映射为归档文件，或站内链接无法落盘。

    携带与链接校验相同形状的错误列表（``source``/``target``/``reason``）；
    抛出时不产出任何 ZIP 内容。
    """

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("archive export failed")


def load_snapshot(conn: sqlite3.Connection) -> tuple[int, dict[str, str], dict[str, str]]:
    """在同一个读事务内取得 (当前修订号, 当前修订页面集合, 现行旧址跳转)。

    页面取自 ``revision = 当前修订号`` 的完整快照；跳转表是当前状态
    （不随修订号快照）。单个读事务保证三者来自同一数据库快照。
    """
    try:
        conn.execute("BEGIN")
        revision = conn.execute(
            "SELECT current_revision FROM meta WHERE id = 1"
        ).fetchone()["current_revision"]
        pages: dict[str, str] = {}
        if revision > 0:
            rows = conn.execute(
                "SELECT path, content FROM pages WHERE revision = ? ORDER BY path",
                (revision,),
            ).fetchall()
            pages = {row["path"]: row["content"] for row in rows}
        redirects = {
            row["path"]: row["target"]
            for row in conn.execute(
                "SELECT path, target FROM redirects ORDER BY path"
            ).fetchall()
        }
        conn.commit()
        return revision, pages, redirects
    except Exception:
        conn.rollback()
        raise


def _archive_name(path: str) -> tuple[str | None, str | None]:
    """把站点路径规范化为归档内相对文件名，返回 (文件名, 错误原因)。

    逐段处理：空段与 ``.`` 忽略；``..`` 上溯一段，越过站点根则不可映射；
    段内不得含危险字符、不得以点或空格结尾；结果为空（不是文件）不可映射。
    """
    stack: list[str] = []
    for seg in path.split("/")[1:]:  # 首个元素是前导 "/" 留下的空串
        if seg == "" or seg == ".":
            continue
        if seg == "..":
            if not stack:
                return None, "path escapes site root"
            stack.pop()
            continue
        if (
            any(ch in _FORBIDDEN_SEGMENT_CHARS or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in seg)
            or seg.endswith(".")
            or seg.endswith(" ")
        ):
            return None, "path is not a safe archive file name"
        stack.append(seg)
    if not stack:
        return None, "path is not a safe archive file name"
    return "/".join(stack), None


def _map_entries(
    pages: dict[str, str], redirects: dict[str, str]
) -> tuple[dict[str, str], dict[str, str], list[dict[str, str]]]:
    """计算 页面路径/旧址 -> 归档归档文件名，并做唯一性与文件/目录冲突检查。"""
    errors: list[dict[str, str]] = []
    page_files: dict[str, str] = {}
    redirect_files: dict[str, str] = {}

    for path in sorted(pages):
        name, reason = _archive_name(path)
        if name is None:
            errors.append({"source": "", "target": path, "reason": reason})
        else:
            page_files[path] = name
    for path in sorted(redirects):
        name, reason = _archive_name(path)
        if name is None:
            errors.append({"source": "", "target": path, "reason": reason})
        else:
            redirect_files[path] = name

    # 唯一性：两个站点路径（页面或旧址）不得映射到同一归档文件。
    owner: dict[str, str] = {}
    for files in (page_files, redirect_files):
        for path in sorted(files):
            name = files[path]
            if name in owner:
                errors.append(
                    {
                        "source": owner[name],
                        "target": path,
                        "reason": "paths map to the same archive file",
                    }
                )
            else:
                owner[name] = path

    # 文件/目录冲突：任一归档文件不得是另一归档文件的目录前缀
    # （解压时同一位置不能既是文件又是目录）。
    for name in sorted(owner):
        parts = name.split("/")
        for i in range(1, len(parts)):
            prefix = "/".join(parts[:i])
            if prefix in owner:
                errors.append(
                    {
                        "source": owner[prefix],
                        "target": owner[name],
                        "reason": "archive file conflicts with directory of another entry",
                    }
                )
    return page_files, redirect_files, errors


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


def _archive_relative(from_file: str, to_file: str) -> str:
    """归档内相对地址：从 from_file 所在目录指向 to_file。"""
    return posixpath.relpath(to_file, posixpath.dirname(from_file) or ".")


def _rewrite_page_hrefs(
    source: str,
    html: str,
    page_files: dict[str, str],
    errors: list[dict[str, str]],
) -> str:
    """把页面内站内 ``<a href>`` 改写为归档内真实文件的相对地址。

    绝对、相对与目录形式的站内链接统一解析到目标页面后，改写为源文件到
    目标归档文件的相对地址，查询串与 ``#片段`` 原文保留；外部链接、纯片段、
    纯查询与其余内容不改。无法解析或目标不在页面集合时记录错误（调用方
    汇总后整体失败，不输出部分 ZIP）。
    """
    source_file = page_files[source]

    def rewrite(raw: str) -> str | None:
        h = raw.strip()
        if not h or is_external(h):
            return None
        path_part, query, fragment = _split_href(h)
        if path_part == "":
            return None  # 纯片段/纯查询：指向本页，离线同样有效
        target, _fragment, reason = resolve_target(source, unescape(h))
        if reason is not None:
            errors.append({"source": source, "target": h, "reason": reason})
            return None
        name = page_files.get(target)
        if name is None:
            # 已发布快照经过整包链接校验，正常不会发生；兜底不输出部分 ZIP。
            errors.append({"source": source, "target": h, "reason": "target page not found"})
            return None
        return _archive_relative(source_file, name) + query + fragment

    return rewrite_anchor_hrefs(html, rewrite)


def _sorted_unique(errors: list[dict[str, str]]) -> list[dict[str, str]]:
    """错误去重并按 (source, target, reason) 排序，与链接校验输出一致。"""
    seen: dict[tuple[str, str, str], dict[str, str]] = {}
    for e in errors:
        seen.setdefault((e["source"], e["target"], e["reason"]), e)
    return [seen[key] for key in sorted(seen)]


def build_archive(pages: dict[str, str], redirects: dict[str, str]) -> bytes:
    """把当前修订页面集合与现行旧址跳转打包成离线 ZIP，返回字节串。

    任一路径无法安全且唯一地映射为归档文件、或任一站内链接无法落盘时抛
    :class:`ArchiveError`——此时不产出任何 ZIP 内容。
    """
    page_files, redirect_files, errors = _map_entries(pages, redirects)
    if errors:
        raise ArchiveError(_sorted_unique(errors))

    entries: dict[str, str] = {}
    link_errors: list[dict[str, str]] = []

    # 1) 页面：站内 href 改写为归档内相对地址后落盘。
    for path in sorted(pages):
        entries[page_files[path]] = _rewrite_page_hrefs(
            path, pages[path], page_files, link_errors
        )

    # 2) 旧址跳转：生成离线入口，打开时直达真实页并保留查询串与片段。
    for path in sorted(redirects):
        target_name = page_files.get(redirects[path])
        if target_name is None:
            # 跳转守护保证目标是当前真实页面；兜底不输出部分 ZIP。
            link_errors.append(
                {"source": path, "target": redirects[path], "reason": "target page not found"}
            )
            continue
        url = _archive_relative(redirect_files[path], target_name)
        entries[redirect_files[path]] = _REDIRECT_ENTRY_TEMPLATE.format(url=url)

    if link_errors:
        raise ArchiveError(_sorted_unique(link_errors))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, entries[name].encode("utf-8"))
    return buf.getvalue()
