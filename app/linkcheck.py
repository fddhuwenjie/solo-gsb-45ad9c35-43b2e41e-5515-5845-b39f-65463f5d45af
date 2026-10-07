"""站内链接提取与校验。

规则：
- 外部链接（http(s)、//、mailto:、tel: 等带 scheme 的）不检查。
- 站内 href 按 ``目标路径 + #片段`` 拆分，查询参数（``?…``）不改变目标。
- 以 ``/`` 开头为绝对路径；否则相对源页面目录解析。
- 目录形式（以 ``/`` 结尾，或当前目录 ``.``）映射到该目录下的
  ``index.html``；无扩展名的普通名称不做隐式推断，需精确匹配。
- 路径越过站点根目录（``..`` 无法继续上溯）视为非法目标。
- 目标页面必须存在于"最终页面集合"；``#id`` 必须是目标页面中
  现存元素的 ``id``。
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import unquote
import posixpath

# 明确不检查的 scheme（外部链接）。
_EXTERNAL_SCHEMES = ("http://", "https://", "ftp://", "mailto:", "tel:", "news:", "irc:")


class _PageAnalysis(HTMLParser):
    """收集页面内所有元素 id 与 <a href>。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        element_id = attr_map.get("id")
        if element_id is not None:
            self.ids.add(element_id)
        if tag == "a" and attr_map.get("href") is not None:
            self.hrefs.append(attr_map["href"])


def analyze(html: str) -> tuple[set[str], list[str]]:
    """返回 (页面内全部 id, 全部 a[href])。"""
    parser = _PageAnalysis()
    parser.feed(html or "")
    parser.close()
    return parser.ids, parser.hrefs


def is_external(href: str) -> bool:
    """判断 href 是否为无需校验的外部链接。"""
    h = href.strip()
    if not h:
        return False
    if h.startswith("//"):  # 协议相对链接，外部资源
        return True
    lowered = h.lower()
    if any(lowered.startswith(s) for s in _EXTERNAL_SCHEMES):
        return True
    # 其他 scheme:xxx（data:、javascript: 等）也按外部/非站内处理
    if "://" in h.split("/", 1)[0]:
        return True
    scheme_part = h.split(":", 1)[0]
    if ":" in h and "/" not in scheme_part and scheme_part.replace("+", "").replace("-", "").replace(".", "").isalpha():
        return True
    return False


def resolve_target(source_path: str, href: str) -> tuple[str | None, str | None, str | None]:
    """把 href 解析为 (目标路径, 片段id, 错误原因)。

    - 返回 (None, None, 原因)：路径越过站点根目录等非法情况。
    - 返回 (路径, None|片段, None)：解析成功，交由调用方核对存在性。
    """
    h = href.strip()
    # 去掉查询参数：?… 不改变目标（在 # 之前的 ? 才是查询串）
    if "#" in h:
        before_frag, fragment = h.split("#", 1)
    else:
        before_frag, fragment = h, ""
    path_part = before_frag.split("?", 1)[0]

    fragment = unquote(fragment) if fragment else None

    if not path_part or path_part == "#":
        return source_path, fragment, None

    # 手工逐段解析：遇到无法上溯的 ".." 即判定越过站点根目录
    # （posixpath.normpath 会把这种 .. 夹断，检测不到越界）。
    if path_part.startswith("/"):
        stack: list[str] = []
        segments = path_part.split("/")
    else:
        stack = [seg for seg in posixpath.dirname(source_path).split("/") if seg and seg != "."]
        segments = path_part.split("/")

    escapes = False
    for seg in segments:
        if seg == "" or seg == ".":
            continue
        if seg == "..":
            if stack:
                stack.pop()
            else:
                escapes = True
                break
        else:
            stack.append(seg)

    if escapes:
        return None, fragment, "path escapes site root"

    resolved = "/" + "/".join(stack)

    # 目录形式（以 / 结尾，或 "." 当前目录）-> index.html（静态站点惯例）。
    # 无扩展名的普通名称不做隐式推断，必须与实际页面路径精确匹配。
    is_directory = path_part.endswith("/") or path_part.rstrip("/") in (".",)
    if is_directory:
        resolved = posixpath.join(resolved, "index.html")

    return unquote(resolved), fragment, None


def check_links(
    pages: dict[str, str],
) -> list[dict[str, str]]:
    """对最终页面集合做整批站内链接一致性校验。

    返回错误列表，每项形如::

        {"source": "/a.html", "target": "/missing.html#x",
         "reason": "target page not found"}
    """
    analysis_cache: dict[str, tuple[set[str], list[str]]] = {}

    def analysis_of(path: str) -> tuple[set[str], list[str]]:
        if path not in analysis_cache:
            analysis_cache[path] = analyze(pages[path])
        return analysis_cache[path]

    errors: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_error(source: str, target: str, reason: str) -> None:
        key = (source, target, reason)
        if key not in seen:
            seen.add(key)
            errors.append({"source": source, "target": target, "reason": reason})

    for source in sorted(pages):
        _ids, hrefs = analysis_of(source)
        for raw_href in hrefs:
            href = raw_href.strip()
            if not href:
                continue
            if is_external(href):
                continue

            target_path, fragment, reason = resolve_target(source, href)
            if reason is not None:
                add_error(source, href, reason)
                continue

            assert target_path is not None
            if target_path not in pages:
                add_error(source, href, "target page not found")
                continue

            if fragment is not None and fragment != "":
                target_ids, _ = analysis_of(target_path)
                if fragment not in target_ids:
                    add_error(source, href, "target anchor not found")

    errors.sort(key=lambda e: (e["source"], e["target"], e["reason"]))
    return errors
