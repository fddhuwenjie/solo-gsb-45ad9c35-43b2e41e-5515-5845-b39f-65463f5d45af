#!/bin/sh
# 可直接执行的端到端 HTTP 调用示例（依赖 curl）。
# 用法：./examples.sh            # 默认 http://127.0.0.1:8000
#       BASE=http://localhost:8000 ./examples.sh
set -eu

BASE="${BASE:-http://127.0.0.1:8000}"

post_publish() {
  curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/publish" \
    -H 'Content-Type: application/json' -d "$1"
}

post_merge() {
  curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/merge" \
    -H 'Content-Type: application/json' -d "$1"
}

post_relocate() {
  curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/relocate" \
    -H 'Content-Type: application/json' -d "$1"
}

post_redirect() {
  curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/redirects" \
    -H 'Content-Type: application/json' -d "$1"
}

delete_redirect() {
  curl -sS -w '\n  -> HTTP %{http_code}\n' -X DELETE "$BASE/redirects" \
    -H 'Content-Type: application/json' -d "$1"
}

site_status() {
  curl -sS -o /dev/null -w "  $1 -> HTTP %{http_code} Location: %{redirect_url}\n" "$BASE/site$1"
}

echo "== 健康检查"
curl -sS "$BASE/health"; echo

echo "== 当前修订号（初始为 0）"
curl -sS "$BASE/revision"; echo

echo "== 1) 首次发布必须包含 /index.html（预期 422）"
post_publish '{"expected_revision":0,"changes":[{"op":"upsert","path":"/about.html","content":"<p>about</p>"}]}'

echo "== 2) 首次发布：/index.html + /about.html（预期 200，revision=1）"
post_publish '{"expected_revision":0,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1><a href=\"/about.html\">关于</a> <a href=\"https://example.com\">外链不检查</a>"},
  {"op":"upsert","path":"/about.html","content":"<a href=\"/#top\">回到首页锚点</a>"}
]}'

echo "== 3) 删除仍被 index 引用的 about.html（预期 422，列出来源与错误目标）"
post_publish '{"expected_revision":1,"changes":[{"op":"delete","path":"/about.html"}]}'

echo "== 4) 用过期修订号发布（预期 409 conflict）"
post_publish '{"expected_revision":0,"changes":[]}'

echo "== 5) 第二批：替换 index 去掉 about 链接，同时删除 about；再加相对/目录/查询串链接（预期 200，revision=2）"
post_publish '{"expected_revision":1,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1> <a href=\"/docs/\">文档目录</a>"},
  {"op":"delete","path":"/about.html"},
  {"op":"upsert","path":"/docs/index.html","content":"<a href=\"../index.html?x=1#top\">查询参数不改变目标</a>"}
]}'

echo "== 6) 读取当前修订的完整页面集合"
curl -sS "$BASE/pages"; echo

echo "== 7) 读取历史 revision=1 的完整集合"
curl -sS "$BASE/pages?revision=1"; echo

echo "== 并行合入：基线仍是 revision 2（当前也是 2），先看本端视图"

echo "== 8) 其他人并行发布 revision 3：新增 /news.html 并替换 /index.html 加上新闻链接（预期 200，revision=3）"
post_publish '{"expected_revision":2,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1> <a href=\"/docs/\">文档目录</a> <a href=\"/news.html\">新闻</a>"},
  {"op":"upsert","path":"/news.html","content":"<a href=\"/#top\">回到首页</a>"}
]}'

echo "== 9) 并行合入：本端基于过期基线 2，只改未被触碰的 /docs/index.html（预期 200，revision=4，/news.html 原样保留）"
post_merge '{"base_revision":2,"changes":[
  {"op":"upsert","path":"/docs/index.html","content":"<a href=\"../index.html?x=1#top\">文档目录（本端更新）</a>"}
]}'

echo "== 10) 并行合入冲突：本端仍基于基线 2 改已被别人修改的 /index.html（预期 409，列出冲突路径与当前修订号，整批不写入）"
post_merge '{"base_revision":2,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1>本端旧视图</h1>"}
]}'

echo "== 11) 基线修订不存在（预期 404）"
post_merge '{"base_revision":99,"changes":[]}'

echo "== 12) 其他人从 revision 4 发布 revision 5：删除 /news.html 并清掉引用；本端基于旧基线 4 新增仍链接它的页面（预期 422，列出来源与错误目标，内容及修订号不变）"
post_publish '{"expected_revision":4,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1> <a href=\"/docs/\">文档目录</a>"},
  {"op":"delete","path":"/news.html"}
]}'
post_merge '{"base_revision":4,"changes":[
  {"op":"upsert","path":"/mine.html","content":"<a href=\"/news.html\">新闻已被别人删除</a>"}
]}'

echo "== 13) 既有接口不受影响：/publish 过期 expected_revision 仍为 409"
post_publish '{"expected_revision":1,"changes":[]}'

echo "== 14) 页面搬迁：/docs/index.html -> /manual/index.html（预期 200，revision=6；腾出的旧地址在同一事务内自动登记为跳转）"
post_relocate '{"expected_revision":5,"old_path":"/docs/index.html","new_path":"/manual/index.html"}'

echo "== 15) 站点读取：真实路径返回 HTML，旧地址 308 指向真实路径，未知路径 404"
site_status /manual/index.html
site_status /docs/index.html
site_status /unknown.html

echo "== 16) 登记跳转：/legacy/home.html -> /index.html（不推进修订号；随后旧地址 308 可用）"
post_redirect '{"path":"/legacy/home.html","target":"/index.html"}'
curl -sS "$BASE/revision"; echo
site_status /legacy/home.html

echo "== 17) 冲突：发布占用旧地址（跳转路径）-> 422，页面、跳转与修订号均不变"
post_publish '{"expected_revision":6,"changes":[{"op":"upsert","path":"/docs/index.html","content":"<p>占用旧地址</p>"}]}'

echo "== 18) 冲突：删除仍被跳转指向的页面 -> 422（同批先清掉 index 的链接，仅余跳转守护错误）"
post_publish '{"expected_revision":6,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1>"},
  {"op":"delete","path":"/manual/index.html"}
]}'

echo "== 19) 冲突：搬迁到已占用的跳转路径 -> 422"
post_relocate '{"expected_revision":6,"old_path":"/manual/index.html","new_path":"/docs/index.html"}'

echo "== 20) 再次搬迁：/manual/index.html -> /guide/index.html（预期 200，revision=7；既有跳转改指新路径，不形成跳转链）"
post_relocate '{"expected_revision":6,"old_path":"/manual/index.html","new_path":"/guide/index.html"}'
curl -sS "$BASE/redirects"; echo
site_status /docs/index.html
site_status /manual/index.html

echo "== 21) 撤销跳转后才能复用旧地址：撤销 /docs/index.html，再发布同名新页（预期 200，revision=8）"
delete_redirect '{"path":"/docs/index.html"}'
post_publish '{"expected_revision":7,"changes":[
  {"op":"upsert","path":"/docs/index.html","content":"<a href=\"../index.html#top\">旧地址复用</a>"}
]}'
site_status /docs/index.html

echo "== 22) 发布含绝对站内链接（带查询串与片段）的首页（预期 200，revision=9）"
post_publish '{"expected_revision":8,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1> <a href=\"/docs/index.html?lang=zh#body\">文档</a>"},
  {"op":"upsert","path":"/docs/index.html","content":"<p id=\"body\">文档正文</p> <a href=\"../index.html#top\">回首页</a>"}
]}'

echo "== 23) /site 输出：绝对站内 href 已指向 /site 下（查询串与片段保留），点击即可打开"
curl -sS "$BASE/site/index.html"; echo
site_status /docs/index.html

echo "== 24) /pages 与历史快照仍是发布原文（不带 /site 前缀）"
curl -sS "$BASE/pages?revision=9"; echo

echo "== 25) 锚点 title 内含伪 href= 文本：发布目标页（id=intro）与首页（预期 200，revision=10）"
post_publish "$(cat <<'JSON'
{"expected_revision":9,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1> <a title=\"hint href='/docs/bait.html'\" href=\"/docs/guide.html?lang=zh#intro\">指南</a>"},
  {"op":"upsert","path":"/docs/guide.html","content":"<h2 id=\"intro\">Guide</h2>"}
]}
JSON
)"

echo "== 26) /site 输出：只有真正的 href 加 /site 前缀，title 里的伪 href= 原样；点击即可打开"
curl -sS "$BASE/site/index.html"; echo
curl -sS -o /dev/null -w "  /site/docs/guide.html?lang=zh -> HTTP %{http_code}\n" \
  "$BASE/site/docs/guide.html?lang=zh"

echo "== 27) 搬迁目标页 /docs/guide.html -> /manual/guide.html（预期 200，revision=11）：只重指真正的 href，旧址 308 直达新页"
post_relocate '{"expected_revision":10,"old_path":"/docs/guide.html","new_path":"/manual/guide.html"}'
curl -sS "$BASE/site/index.html"; echo
site_status /docs/guide.html
site_status /manual/guide.html

echo "== 28) 下载离线 ZIP（当前修订页面 + 旧址离线入口；不推进修订号）"
curl -sS -o /tmp/docs-archive.zip -w '  -> HTTP %{http_code}, %{size_download} bytes\n' \
  "$BASE/download"
python3 -c "import zipfile; print('\n'.join('  ' + n for n in zipfile.ZipFile('/tmp/docs-archive.zip').namelist()))"
echo "  解压后打开 index.html 即可离线浏览：unzip -o /tmp/docs-archive.zip -d /tmp/docs-offline"
curl -sS "$BASE/revision"; echo

post_prune() {
  curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/prune" \
    -H 'Content-Type: application/json' -d "$1"
}

echo "== 29) 制造不可达页面：新增互相引用的孤立页 /orphan/a.html <-> /orphan/b.html 与无入链的 /orphan/lone.html（预期 200，revision=12）"
post_publish '{"expected_revision":11,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1> <a href=\"/manual/guide.html?lang=zh#intro\">指南</a> <a href=\"/docs/index.html\">文档</a>"},
  {"op":"upsert","path":"/orphan/a.html","content":"<a href=\"/orphan/b.html\">b</a>"},
  {"op":"upsert","path":"/orphan/b.html","content":"<a href=\"a.html\">a</a>"},
  {"op":"upsert","path":"/orphan/lone.html","content":"<p>无人引用</p>"}
]}'

echo "== 30) 清理不可达页面（预期 200，revision=13）：互相引用的孤立簇与无入链页面一并删除；跳转目标 /guide/index.html 保留"
post_prune '{"expected_revision":12}'
curl -sS "$BASE/pages"; echo

echo "== 31) 再次清理：没有可删页面，不产生新修订（deleted 为空，revision 仍为 13）"
post_prune '{"expected_revision":13}'

echo "== 32) 过期修订号清理（预期 409，页面、跳转与修订号均不变）"
post_prune '{"expected_revision":12}'

echo "== 33) 历史快照仍可读取（revision=12 中仍能看到已清理的孤立页）；旧址 308 与站点访问保持可用"
curl -sS "$BASE/pages?revision=12"; echo
site_status /manual/index.html
site_status /guide/index.html

echo "== 当前修订号与最终页面集合"
curl -sS "$BASE/revision"; echo
curl -sS "$BASE/pages"; echo

