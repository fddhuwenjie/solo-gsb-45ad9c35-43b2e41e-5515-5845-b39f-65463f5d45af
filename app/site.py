"""站点读取（``/site``）输出前的 href 改写。

发布校验保证页面集合内的站内链接都能解析到真实页面；但浏览器从
``/site/index.html`` 点击**绝对站内 href**（如
``<a href="/docs/guide.html?lang=zh#intro">``）会请求 ``/docs/guide.html``——
不在 ``/site`` 挂载之下，按现有路由只能 404。

因此 ``/site`` 返回 HTML 时，把绝对站内 href（``/`` 开头）改写为 ``/site``
前缀下的同名路径，查询串与 ``#片段`` 原文保留，点击后仍落在 ``/site``
可读取的页面上；相对链接、纯片段、纯查询、外部链接与非链接 HTML 一律不改。

改写只发生在读取响应上：数据库中的正文、``GET /pages`` 返回的内容与历史
修订快照都保持发布时的原文。
"""

from __future__ import annotations

from .htmlrewrite import rewrite_anchor_hrefs
from .linkcheck import is_external

# /site 路由的挂载前缀：绝对站内 href 改写后指向该前缀下的同名路径。
SITE_PREFIX = "/site"


def _prefix_absolute_href(raw: str) -> str | None:
    """绝对站内 href 加 ``/site`` 前缀；其余（相对/纯片段/纯查询/外部）返回 None。"""
    h = raw.strip()
    if not h or is_external(h) or not h.startswith("/"):
        return None
    return SITE_PREFIX + h


def rewrite_html_for_site(html: str) -> str:
    """返回供 ``/site`` 输出的 HTML：仅绝对站内 href 指到 ``/site`` 下。

    查询串与 ``#片段`` 随 href 原文保留；相对链接、纯片段、纯查询、外部链接
    与非链接 HTML（正文、注释、其他标签及属性）一律不改。
    """
    return rewrite_anchor_hrefs(html, _prefix_absolute_href)
