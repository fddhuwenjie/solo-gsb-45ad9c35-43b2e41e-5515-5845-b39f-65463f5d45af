"""字节级 ``<a href>`` 改写工具：只动真正 href 属性的值，其余字节原样保留。

页面搬迁（:mod:`app.relocate`）与站点读取输出（:mod:`app.site`）共用同一套
机制：用 HTMLParser 定位每个 ``<a>`` 开始标签的精确字节范围，再在标签原文内
按 HTML 属性语法扫描出**真正的 href 属性**并仅替换其值——正文、注释、其他
标签、属性排版与 HTML 实体都不受影响；其他属性值里出现的 ``href=`` 文本
（如 ``title="hint href='/docs/bait.html'"``）只是普通字符串，不会被误认。
"""

from __future__ import annotations

from html.parser import HTMLParser

# HTML 空白字符（标签内属性分隔符）。
_WS = "\t\n\f\r "


class _AnchorCollector(HTMLParser):
    """收集页面内每个 ``<a>`` 开始标签的精确字节范围与标签原文。"""

    def __init__(self, line_starts: list[int]) -> None:
        super().__init__(convert_charrefs=True)
        self._line_starts = line_starts
        # (起始偏移, 结束偏移, 标签原文)
        self.anchors: list[tuple[int, int, str]] = []

    def _abs_offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def _record(self, tag_text: str) -> None:
        start = self._abs_offset()
        self.anchors.append((start, start + len(tag_text), tag_text))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._record(self.get_starttag_text())

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._record(self.get_starttag_text())


def _line_starts(html: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(html):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _find_href_value_span(tag_text: str) -> tuple[int, int] | None:
    """在 ``<a>`` 标签原文中定位真正 href 属性值（不含引号）的范围。

    按 HTML 属性语法逐项扫描：属性之间以空白分隔，带引号的属性值整体跳过，
    因此其他属性值内出现的 ``href=`` 文本（如
    ``title="hint href='/docs/bait.html'"``）不会被误认为 href 属性，
    ``data-href`` 之类同名后缀的属性也不会命中。属性名大小写不敏感；
    重复 href 取第一个（与浏览器行为一致）。无 href 属性或无值 href
    （``<a href>``）返回 None。
    """
    n = len(tag_text)
    i = 1 if tag_text.startswith("<") else 0
    while i < n and tag_text[i] not in _WS + "/>":
        i += 1  # 跳过标签名
    while i < n:
        while i < n and tag_text[i] in _WS:
            i += 1
        if i >= n or tag_text[i] == ">":
            return None
        if tag_text[i] == "/":  # "/>" 结束；孤立的 "/" 按解析错误忽略
            i += 1
            continue
        name_start = i
        while i < n and tag_text[i] not in _WS + "/=>":
            i += 1
        name = tag_text[name_start:i].lower()
        while i < n and tag_text[i] in _WS:
            i += 1
        if i >= n or tag_text[i] != "=":
            if name == "href":
                return None  # 无值 href（<a href>）：没有可改写的值
            continue
        i += 1  # 跳过 "="
        while i < n and tag_text[i] in _WS:
            i += 1
        if i < n and tag_text[i] in "\"'":
            quote = tag_text[i]
            value_start = i + 1
            end = tag_text.find(quote, value_start)
            value_end = n if end == -1 else end
            i = n if end == -1 else end + 1
        else:
            value_start = i
            while i < n and tag_text[i] not in _WS + ">":
                i += 1
            value_end = i
        if name == "href":
            return value_start, value_end
    return None


def _replace_href_in_tag(tag_text: str, span: tuple[int, int], new_value: str) -> str:
    """把标签原文中 span 范围内的 href 值替换为新值，引号与值内首尾空白保留。"""
    start, end = span
    inner = tag_text[start:end]
    leading = inner[: len(inner) - len(inner.lstrip())]
    trailing = inner[len(inner.rstrip()) :]
    return tag_text[:start] + leading + new_value + trailing + tag_text[end:]


def rewrite_anchor_hrefs(html: str, rewrite) -> str:
    """逐个改写页面内 ``<a>`` 标签真正 href 属性的值。

    ``rewrite(raw_href)`` 收到保留 HTML 实体与空白的原始值，返回新的
    href 文本（去除首尾空白后的形式）或 ``None``（不改）。
    除真正 href 属性值之外的任何字节（正文、注释、其他标签、其他属性及其
    值——包括属性值内形似 ``href=`` 的文本、属性排版、HTML 实体）都原样
    保留；无任何 href 变化时返回原字符串。
    """
    collector = _AnchorCollector(_line_starts(html))
    collector.feed(html or "")
    collector.close()

    edits: list[tuple[int, int, str]] = []
    for start, end, tag_text in collector.anchors:
        span = _find_href_value_span(tag_text)
        if span is None:
            continue
        raw = tag_text[span[0]:span[1]]
        new_href = rewrite(raw)
        if new_href is not None and new_href != raw.strip():
            edits.append((start, end, _replace_href_in_tag(tag_text, span, new_href)))

    if not edits:
        return html

    out = html
    for start, end, new_tag in reversed(edits):
        out = out[:start] + new_tag + out[end:]
    return out
