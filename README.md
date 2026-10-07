# 静态文档包链接一致性发布 API

用 Python + FastAPI + SQLite 实现的静态文档包发布服务。客户端携带**预期修订号**批量
新增、替换或删除以 `/` 开头的 HTML 页面；服务在**同一个 SQLite 写事务**内先试用整批改动，
再核对最终所有页面的站内 `href`，全部通过才提交发布，否则内容与修订号均不变。

## 一键启动

```bash
docker compose up --build -d
```

启动时会自动安装依赖、初始化 SQLite 数据库（表 + 修订号 0）。

- 服务地址：**http://localhost:8000**
- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

数据库存放在 Docker 命名卷 `docs-data` 中，容器/进程重启后数据仍可读取。

本地非 Docker 运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000   # 首次启动自动建表
```

## 规则说明

- **修订号（乐观锁）**：初始为 `0`，每次成功发布 +1。请求必须带 `expected_revision`，
  与当前值不一致时返回 `409 Conflict`，不改变任何内容。
- **入口页**：首次发布（revision 0 → 1）的整批结果必须包含 `/index.html`；
  此后 `/index.html` 不得被删除（可以替换）。违反返回 `422`。
- **批量变更**：`op` 为 `upsert`（新增或替换，别名 `put`）或 `delete`。
  变更按数组顺序在内存中的当前快照上试用，得到最终页面集合后统一校验。
- **链接校验**（仅检查最终集合内所有页面的站内 `<a href>`）：
  - 绝对路径（`/a.html`）与相对路径（`b.html`、`../x.html`）都必须指向现存页面；
  - 目录形式以 `/` 结尾时映射到该目录的 `index.html`（如 `/docs/` → `/docs/index.html`）；
  - `#fragment` 必须是目标页面中现存的元素 `id`（`#` 单独使用指向当前页面）；
  - 查询参数 `?a=b` 不改变目标（`/x.html?v=2` 与 `/x.html` 同一目标）；
  - 链接路径越过站点根目录（如 `/../etc/passwd`、根目录下的 `../x`）判为非法；
  - 外部链接（`http(s)://`、`//host`、`mailto:`、`tel:` 等）不检查。
- 校验失败时 `422` 响应逐项列出 `source`（来源页面）、`target`（错误目标，原始 href）
  与 `reason`（`target page not found` / `target anchor not found` /
  `path escapes site root`）。
- 校验与发布在同一个 `BEGIN IMMEDIATE` 写事务中完成，读接口永远只读到完整提交的快照。

## API

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `GET /revision` | 当前修订号 |
| `GET /pages` | 当前修订的完整页面集合 `{"revision":n,"pages":{路径:内容}}` |
| `GET /pages?revision=N` | 读取第 N 次发布的完整集合；不存在返回 404 |
| `POST /publish` | 批量发布，请求体 `{"expected_revision":n,"changes":[...]}` |
| `POST /merge` | 基于历史基线的并行变更合入，请求体 `{"base_revision":n,"changes":[...]}` |
| `POST /relocate` | 页面搬迁，请求体 `{"expected_revision":n,"old_path":"/a.html","new_path":"/b.html"}` |
| `POST /prune` | 清理不可达页面，请求体 `{"expected_revision":n}`；没有可删页面时不产生新修订 |
| `GET /redirects` | 当前全部旧地址跳转 `{"redirects":{旧地址:真实路径}}` |
| `POST /redirects` | 登记跳转，请求体 `{"path":"/old.html","target":"/real.html"}`；不推进修订号 |
| `DELETE /redirects` | 撤销跳转，请求体 `{"path":"/old.html"}`；不推进修订号 |
| `GET /site/{路径}` | 站点读取：真实路径返回 HTML（绝对站内 href 改写指到 `/site` 下）；旧地址 308 指向真实路径；未知路径 404 |
| `GET /download` | 下载当前修订的离线 ZIP（页面 + 旧址离线入口）；不推进修订号；未发布返回 404 |

## 页面搬迁（`POST /relocate`）

把一个**已发布且不是 `/index.html`** 的旧页面移动到**未占用**的新路径（新旧相同也视为冲突），
并在同一个写事务内完成链接改写与整包校验，成功时生成新修订：

- 旧路径消失，新页面**保留原有正文与元素 `id`**；
- 其他页面中解析后指向旧页的站内 `href` 改指新页（绝对 `href` 用绝对新路径，
  相对 `href` 用相对新路径），查询参数与 `#片段` 原样保留；
- 搬迁页**自身的相对 `href`** 以新位置为基准重定位，使各链接原本指向的页面与片段
  在搬迁后仍可到达；纯片段（`#x`）/纯查询（`?x`）指向本页，无需改写；
- 外部链接与非链接内容（正文文本、注释、其他标签及属性等）一律不改；
- 沿用既有的 href 解析规则与整包链接校验作为兜底：**新路径冲突，或任一链接无法
  维持指向时，整次搬迁失败，内容与修订号均不变（422）**；
- **旧地址联动（同一写事务内）**：腾出的旧路径自动登记为跳转（旧地址 -> 新路径），
  此前指向旧页的既有跳转一并改指新路径——跳转永远直达真实页面，不形成跳转链；
  新路径不得是已占用的跳转路径（`new path is occupied by a redirect`，422）；
- 过期 `expected_revision` 按发布接口同样的冲突规则返回 `409`；
- 成功返回 `{"revision":n,"relocated":true,"old_path":...,"new_path":...}`，
  历史修订快照仍可通过 `GET /pages?revision=N` 读取，原发布与读取接口不受影响。

```bash
# 搬迁页面（成功，revision -> N+1）：其他页面引用与搬迁页自身相对链接一并改写
curl -s -X POST http://localhost:8000/relocate \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1,"old_path":"/docs/guide.html","new_path":"/manual/guide.html"}'
# {"revision":2,"relocated":true,"old_path":"/docs/guide.html","new_path":"/manual/guide.html"}

# 新路径已被占用（含新旧相同）-> 422，内容与修订号不变
curl -s -X POST http://localhost:8000/relocate \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":2,"old_path":"/about.html","new_path":"/index.html"}'

# 搬迁入口页 /index.html -> 422
curl -s -X POST http://localhost:8000/relocate \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":2,"old_path":"/index.html","new_path":"/home.html"}'

# 过期修订号 -> 409（与 /publish 相同的冲突响应）
curl -s -X POST http://localhost:8000/relocate \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1,"old_path":"/about.html","new_path":"/about2.html"}'
```

## 旧地址跳转（登记 / 撤销 / 站点读取）

外部保存的旧页面地址在搬迁后仍应可访问。服务维护一张**跳转表**
（`旧地址 -> 当前真实页面路径`，存于 SQLite，**重启后仍有效**）：

- **站点读取** `GET /site/{路径}`：
  - 真实页面路径 → `200` 返回 HTML 正文（`/site` 与 `/site/` 视为 `/index.html`；
    返回的 HTML 中绝对站内 href 已改写指到 `/site` 下，见下节）；
  - 已登记的旧地址 → `308 Permanent Redirect`，`Location` 指向站点内真实页面的
    URL（`/site` 前缀 + 真实路径），跟随一次即可拿到页面；
  - 未知路径 → `404`。
- **登记** `POST /redirects`：旧地址沿用页面路径格式（`/` 开头、不含 `?`/`#`），
  且**未被真实页面或其他跳转占用**；目标须是**当前真实页面**——因此跳转永不指向
  跳转，不形成跳转链。冲突返回 `422` 并逐项说明（`path is an existing page` /
  `path is already a registered redirect` / `target page not found`）。
- **撤销** `DELETE /redirects`：删除跳转记录；不存在返回 `404`。
  **撤销之后旧地址才可被发布/合入复用。**
- **登记与撤销都不推进页面修订号**（响应附带当前修订号便于核对），只写跳转表。
- **发布/合入守护**：`/publish` 与 `/merge` 的最终页面集合**不得占用跳转路径**
  （`path is occupied by a redirect`），也**不得删除仍被跳转指向的页面**
  （`page is still the target of a redirect`）；违反返回 `422`，
  **页面、跳转与修订号均不变**。
- **搬迁联动**：见上节——搬迁在同一写事务内登记旧路径跳转并改指既有跳转。
- 站内 `href` 校验始终只针对真实页面：链接到旧地址（跳转路径）视为
  `target page not found`。
- 跳转表是当前状态，不随修订号快照：`GET /pages?revision=N` 仍返回原页面集合。

```bash
# 登记跳转（不推进修订号）：旧地址 -> 当前真实页面
curl -s -X POST http://localhost:8000/redirects \
  -H 'Content-Type: application/json' \
  -d '{"path":"/legacy/home.html","target":"/index.html"}'
# {"registered":true,"path":"/legacy/home.html","target":"/index.html","revision":6}

# 访问：真实路径返回 HTML；旧地址 308 指向真实路径；未知路径 404
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/site/index.html        # 200
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  http://localhost:8000/site/legacy/home.html
# 308 http://localhost:8000/site/index.html
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/site/unknown.html      # 404

# 冲突：发布/合入占用旧地址（跳转路径）-> 422，页面、跳转与修订号均不变
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":6,"changes":[{"op":"upsert","path":"/legacy/home.html","content":"<p>x</p>"}]}'
# {"detail":"validation failed","errors":[{"source":"","target":"/legacy/home.html",
#   "reason":"path is occupied by a redirect"}]}

# 冲突：删除仍被跳转指向的页面 -> 422（同批先清掉引用，仅余跳转守护错误）
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":6,"changes":[
        {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1>"},
        {"op":"delete","path":"/manual/index.html"}]}'
# {"detail":"validation failed","errors":[{"source":"/docs/index.html",
#   "target":"/manual/index.html","reason":"page is still the target of a redirect"}]}

# 冲突：搬迁到已占用的跳转路径 -> 422
curl -s -X POST http://localhost:8000/relocate \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":6,"old_path":"/manual/index.html","new_path":"/docs/index.html"}'
# {"detail":"validation failed","errors":[{"source":"","target":"/docs/index.html",
#   "reason":"new path is occupied by a redirect"}]}

# 当前跳转表
curl -s http://localhost:8000/redirects
# {"redirects":{"/docs/index.html":"/guide/index.html","/legacy/home.html":"/index.html", ...}}

# 撤销跳转（不推进修订号）；撤销后旧地址才可复用
curl -s -X DELETE http://localhost:8000/redirects \
  -H 'Content-Type: application/json' \
  -d '{"path":"/legacy/home.html"}'
# {"revoked":true,"path":"/legacy/home.html","revision":7}
```

## 站点读取（`/site`）：绝对站内 href 改写

发布校验保证页面集合内的站内链接都能解析到真实页面，但浏览器打开
`/site/index.html` 后，点击其中的**绝对站内 href**（如
`<a href="/docs/guide.html?lang=zh#intro">`）会请求 `/docs/guide.html`——
不在 `/site` 挂载之下，按路由只能 404。

因此 `/site` 返回 HTML 时，把绝对站内 href（`/` 开头）改写为 `/site`
前缀下的同名路径，**查询串与 `#片段` 原文保留**，点击后仍落在 `/site`
可读取的页面上：

- 相对链接（`docs/a.html`、`../x.html`）、纯片段（`#x`）、纯查询（`?x`）、
  外部链接（`http(s)://`、`//host`、`mailto:` 等）与非链接 HTML
  （正文、注释、其他标签及属性）一律原样返回；
- 改写按 HTML 属性语法定位**真正的 href 属性**（带引号的属性值整体跳过）：
  其他属性值里形似 `href=` 的文本（如
  `<a title="hint href='/docs/bait.html'" href="/docs/guide.html">` 的 title）
  不会被误改，`data-href` 等同名后缀属性也不命中——只有真正的 href 被改写，
  浏览器点击才不会落到 `/site` 挂载之外而 404（复现示例见下节）；
- 改写只作用于本次响应：数据库正文、`GET /pages` 与历史修订快照
  （`GET /pages?revision=N`）始终返回发布原文；
- 搬迁后链接仍到达新页面：存储正文中的 href 已随搬迁改指新路径，
  `/site` 输出时再加前缀；旧址保持一次 `308` 直达 `/site` 下的真实页。

```bash
# 发布首页（含带查询串与片段的绝对站内链接）与真实目标页
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":0,"changes":[
        {"op":"upsert","path":"/index.html",
         "content":"<a href=\"/docs/guide.html?lang=zh#intro\">指南</a>"},
        {"op":"upsert","path":"/docs/guide.html",
         "content":"<h2 id=\"intro\">Guide</h2>"}]}'
# {"revision":1,"published":true}

# /site 返回的 HTML 中，绝对站内 href 指向 /site 下（查询串与片段保留）
curl -s http://localhost:8000/site/index.html
# <a href="/site/docs/guide.html?lang=zh#intro">指南</a>

# 浏览器实际请求的地址（查询串原样携带）直接打开目标页
curl -s -o /dev/null -w '%{http_code}\n' \
  "http://localhost:8000/site/docs/guide.html?lang=zh"          # 200

# /pages 与历史快照仍是发布原文（不带 /site 前缀）
curl -s http://localhost:8000/pages
# {"revision":1,"pages":{"/docs/guide.html":"<h2 id=\"intro\">Guide</h2>",
#  "/index.html":"<a href=\"/docs/guide.html?lang=zh#intro\">指南</a>"}}
```

### 复现：锚点属性值里的伪 `href=` 不会被误改

`<a>` 标签其他属性的值里可能出现形似 `href=` 的文本，例如
`<a title="hint href='/docs/bait.html'" href="/docs/guide.html?lang=zh#intro">`。
`/site` 输出与 `/relocate` 重指都只认**真正的 href 属性**：title 里的伪
`href=` 原样保留，真正的 href 被改写/重指，点击不再 404。服务启动后逐条运行：

```bash
# 1) 发布带 id="intro" 的目标页，以及 title 内含伪 href= 的首页
cat > /tmp/publish.json <<'JSON'
{
  "expected_revision": 0,
  "changes": [
    {"op":"upsert","path":"/docs/guide.html",
     "content":"<h2 id=\"intro\">Guide</h2>"},
    {"op":"upsert","path":"/index.html",
     "content":"<a title=\"hint href='/docs/bait.html'\" href=\"/docs/guide.html?lang=zh#intro\">指南</a>"}
  ]
}
JSON
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' -d @/tmp/publish.json
# {"revision":1,"published":true}

# 2) /site 输出：真正的 href 指向 /site 下（查询串与片段保留），title 原样
curl -s http://localhost:8000/site/index.html
# <a title="hint href='/docs/bait.html'" href="/site/docs/guide.html?lang=zh#intro">指南</a>

# 3) 浏览器实际请求的地址（查询串原样携带）直接打开目标页
curl -s -o /dev/null -w '%{http_code}\n' \
  "http://localhost:8000/site/docs/guide.html?lang=zh"          # 200

# 4) /pages 正文是发布原文（真 href 不带 /site 前缀，title 未动）
curl -s http://localhost:8000/pages
# {"revision":1,"pages":{"/docs/guide.html":"<h2 id=\"intro\">Guide</h2>",
#  "/index.html":"<a title=\"hint href='/docs/bait.html'\" href=\"/docs/guide.html?lang=zh#intro\">指南</a>"}}

# 5) 搬迁目标页：/relocate 只重指真正的 href，查询串与 #片段 保留
curl -s -X POST http://localhost:8000/relocate \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1,"old_path":"/docs/guide.html","new_path":"/manual/guide.html"}'
# {"revision":2,"relocated":true,"old_path":"/docs/guide.html","new_path":"/manual/guide.html"}

# 6) 首页存储正文：真 href 已指新页，title 伪 href= 未动；/site 输出再加前缀
curl -s http://localhost:8000/pages
# ..."/index.html":"<a title=\"hint href='/docs/bait.html'\" href=\"/manual/guide.html?lang=zh#intro\">指南</a>"...
curl -s http://localhost:8000/site/index.html
# <a title="hint href='/docs/bait.html'" href="/site/manual/guide.html?lang=zh#intro">指南</a>

# 7) 首页链接到达新页；旧址一次 308 直达新页
curl -s -o /dev/null -w '%{http_code}\n' \
  "http://localhost:8000/site/manual/guide.html?lang=zh"        # 200
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  http://localhost:8000/site/docs/guide.html
# 308 http://localhost:8000/site/manual/guide.html

# 8) 整包校验失败（如新路径被占用）-> 422，页面、跳转和修订号均不变
curl -s -X POST http://localhost:8000/relocate \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":2,"old_path":"/manual/guide.html","new_path":"/index.html"}'
# {"detail":"validation failed","errors":[{"source":"","target":"/index.html",
#   "reason":"new path already exists"}]}
curl -s http://localhost:8000/revision   # {"revision":2}
```

## 离线 ZIP 下载（`GET /download`）

`GET /download` 把**当前修订**的完整页面集合与**现行旧址跳转**（取自同一个
SQLite 读事务快照）打包成 ZIP 返回，`Content-Disposition` 文件名形如
`docs-archive-r9.zip`。解压后直接打开 `index.html`，无需运行服务即可循站内
链接浏览：

- **页面落盘**：`/index.html` → 归档根部 `index.html`，`/docs/guide.html` →
  `docs/guide.html`，依此类推（ZIP 内一律为正斜杠相对路径）。
- **链接改写**：页面内真正的 `<a href>` 中，绝对（`/a.html`）、相对
  （`b.html`、`../x.html`）与目录形式（`/docs/`）的站内链接一律改写为
  **归档内真实文件的相对地址**——目录形式指向该目录的 `index.html` 文件，
  浏览器从文件系统打开也能到达；查询串与 `#片段` 原文保留。外部链接、
  纯片段、纯查询、非链接内容与其他属性（含属性值里形似 `href=` 的文本）
  一律不改。
- **旧址离线入口**：每个现行跳转 `旧地址 -> 真实页` 在旧地址对应的归档位置
  生成一个入口 HTML，打开时通过
  `location.replace(目标 + location.search + location.hash)` 直达真实页并
  **保留查询串与片段**；`<meta http-equiv="refresh">` 与 `<a>` 是禁用脚本时
  的兜底。
- **明确错误，不输出部分 ZIP**：任一页面/旧址路径无法安全且唯一地映射为
  归档文件（`..` 越过站点根、含危险字符、规范化后撞名、文件与目录前缀冲突
  等），或站内链接无法落盘时，返回 `422` 并逐项列出（与链接校验相同的
  `source`/`target`/`reason` 形状），不产出 ZIP。
- **尚未首次发布**（revision 0）返回 `404`。
- **只读**：下载不推进修订号，不改写 `/pages` 原文、历史修订快照或
  `/site` 响应；同一快照重复下载得到字节一致的 ZIP。

```bash
# 下载（按 Content-Disposition 保存为 docs-archive-rN.zip）
curl -sS -OJ http://localhost:8000/download
# 或指定文件名
curl -sS -o docs-archive.zip http://localhost:8000/download

# 解压后用浏览器打开入口页即可离线浏览
unzip -o docs-archive.zip -d docs-offline
open docs-offline/index.html        # macOS；Linux 用 xdg-open，Windows 直接双击

# 尚未发布时返回明确错误
curl -sS http://localhost:8000/download
# {"detail":"no published revision to download","revision":0}
```

## 并行变更合入（`POST /merge`）

同一文档包的不同客户端可能从**同一个旧修订分别改动**。`/merge` 接收**基线修订号**
`base_revision` 与沿用发布接口格式的页面变更（`changes` 中同样是 `upsert`/`put`/`delete`），
把本端改动合入当前快照：

- **即使基线已过期**，也在**同一个 SQLite 写事务**内逐个比对本批涉及路径在
  **基线快照与当前快照**中的存在性与正文：
  - 基线有、当前无 → `path deleted by another change`；
  - 基线无、当前有 → `path added by another change`；
  - 两边都有但正文不同 → `path modified by another change`；
  - 两边都无，或正文完全相同 → 未被其他人改动，允许合入。
- 任一路径已变化即返回 **409**，响应给出当前修订号 `current` 与冲突路径列表
  `conflicts`（按路径排序），**整批不写入**，内容与修订号均不变。
- 无同路径冲突时，**按提交顺序**把变更应用于**当前快照**：其他人新增或修改、
  而本批未涉及的页面原样保留。随后沿用入口页规则、跳转守护（不得占用跳转路径、
  不得删除仍被跳转指向的页面）与整包站内链接校验——
  若其他页面的并行改动使最终链接失效（例如别人删了某页、本端新页仍链接它），
  返回 **422**，逐项列出 `source`（来源页面）与 `target`（错误目标），
  内容及修订号同样不变。
- 全部通过才生成**一个新修订**（当前修订号 + 1），写入完整快照；
  其他人新增的页面与原有历史快照（含基线）都保留，可用 `GET /pages?revision=N` 读取。
- **基线修订不存在返回 404**（`0` 始终有效，表示空基线；未来修订号视为不存在）。
- 成功返回 `{"revision":n,"merged":true,"base_revision":n}`。
- 既有 `POST /publish` 对过期 `expected_revision` 的 **409** 行为与
  `POST /relocate` 保持不变，仍可正常使用。

```bash
# 假设当前已有 revision 1：/index.html 与 /about.html。
# 其他人先发布了 revision 2：新增 /news.html 并替换 /index.html 加上新闻链接。

# 本端仍基于过期基线 1，只改动未被触碰的 /about.html -> 200，revision -> 3；
# 其他人新增的 /news.html 与其对 /index.html 的改动原样保留
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base_revision": 1,
    "changes": [
      {"op":"upsert","path":"/about.html",
       "content":"<a href=\"/#top\">关于（更新）</a> <a href=\"/news.html\">新闻</a>"}
    ]
  }'
# {"revision":3,"merged":true,"base_revision":1}

# 本端基于基线 1 改动已被别人修改的 /index.html -> 409，列出冲突路径与当前修订号
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{"base_revision":1,"changes":[{"op":"upsert","path":"/index.html","content":"<h1>旧视图</h1>"}]}'
# {"detail":"merge conflict","base_revision":1,"current":3,
#  "conflicts":[{"path":"/index.html","reason":"path modified by another change"}]}

# 基线修订不存在 -> 404
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{"base_revision":99,"changes":[]}'
# {"detail":"base revision not found","base_revision":99}

# 其他人随后删除了 /news.html；本端基于旧基线新增仍链接它的页面
# -> 422，列出来源 /mine.html 与错误目标 /news.html，内容及修订号不变
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{"base_revision":3,"changes":[{"op":"upsert","path":"/mine.html","content":"<a href=\"/news.html\">新闻已下线</a>"}]}'
# {"detail":"validation failed","errors":[
#   {"source":"/mine.html","target":"/news.html","reason":"target page not found"}]}
```

## 不可达页面清理（`POST /prune`）

已发布文档可能留下从首页看不到的页面（改版、搬迁后失去引用的旧页）。
`POST /prune` 携带**预期修订号**，在**同一个 SQLite 写事务**内做可达性
遍历并删除不可达页面：

- **遍历起点**：入口页 `/index.html` 与所有现行旧址跳转的真实目标——
  **跳转目标及其可达页面必须保留**。
- **遍历规则**：沿真正的站内 `<a href>` 逐个解析（属性值里形似 `href=`
  的文本不是真正的 href，不参与遍历）；相对、绝对及目录路径沿用发布时的
  解析规则，查询串与 `#片段` 不改变目标，外部链接不参与。
- **删除范围**：从这些入口无法到达的全部页面——包括互相引用但整体
  不可达的孤立页面簇。
- **结果**：成功返回 `{"revision":n,"pruned":true,"deleted":[...]}`，
  删除清单按路径排序；**没有可删页面时不产生新修订**
  （`{"revision":n,"pruned":false,"deleted":[]}`）。
- **兜底**：删除后的最终页面集合沿用整包站内链接校验与跳转守护；
  过期 `expected_revision` 返回 `409`，校验失败返回 `422`——
  **页面、跳转与修订号均不变**。
- **首次发布前**（revision 0）返回 `422`（`no published revision to prune`）。
- 清理只删除当前修订中的页面：**历史修订快照仍可读取**，发布、站点访问
  与离线下载保持可用。

```bash
# 首次发布：首页可达 /docs/guide.html；/old/a.html 与 /old/b.html 互相引用
# 但首页不可达（孤立页面簇）；/legacy/home.html 首页也不可达，
# 但登记了旧址跳转，属于必须保留的入口
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":0,"changes":[
        {"op":"upsert","path":"/index.html",
         "content":"<a href=\"/docs/guide.html\">指南</a>"},
        {"op":"upsert","path":"/docs/guide.html","content":"<p>guide</p>"},
        {"op":"upsert","path":"/old/a.html",
         "content":"<a href=\"/old/b.html\">b</a>"},
        {"op":"upsert","path":"/old/b.html",
         "content":"<a href=\"a.html\">a</a>"},
        {"op":"upsert","path":"/legacy/home.html","content":"<p>旧首页</p>"}]}'
# {"revision":1,"published":true}
curl -s -X POST http://localhost:8000/redirects \
  -H 'Content-Type: application/json' \
  -d '{"path":"/legacy/index.html","target":"/legacy/home.html"}'

# 清理：互相引用的 /old/a.html 与 /old/b.html 不可达，一并删除；
# 跳转目标 /legacy/home.html 保留
curl -s -X POST http://localhost:8000/prune \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1}'
# {"revision":2,"pruned":true,"deleted":["/old/a.html","/old/b.html"]}

# 再次清理：没有可删页面，不产生新修订
curl -s -X POST http://localhost:8000/prune \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":2}'
# {"revision":2,"pruned":false,"deleted":[]}

# 过期修订号 -> 409，页面、跳转与修订号均不变
curl -s -X POST http://localhost:8000/prune \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1}'
# {"detail":"revision conflict","expected":1,"current":2}

# 历史快照仍可读取（被删页面保留在第 1 修订中）
curl -s "http://localhost:8000/pages?revision=1"
```

## 可执行的 HTTP 调用示例

启动服务后直接运行（仓库内已带可执行脚本）：

```bash
chmod +x examples.sh && ./examples.sh
```

或手动用 curl 逐步体验：

```bash
# 初始状态
curl -s http://localhost:8000/revision            # {"revision":0}

# 首次发布（成功，revision -> 1）
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 0,
    "changes": [
      {"op":"upsert","path":"/index.html",
       "content":"<h1 id=\"top\">首页</h1><a href=\"/about.html\">关于</a>"},
      {"op":"upsert","path":"/about.html",
       "content":"<a href=\"/#top\">回到锚点</a>"}
    ]
  }'

# 删除仍被引用的页面 -> 422，列出来源 /index.html 与错误目标 /about.html
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1,"changes":[{"op":"delete","path":"/about.html"}]}'

# 过期修订号 -> 409
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":0,"changes":[]}'

# 同一批内替换 index（去掉失效链接）并删除 about -> 200，revision -> 2
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 1,
    "changes": [
      {"op":"upsert","path":"/index.html","content":"<h1>首页</h1>"},
      {"op":"delete","path":"/about.html"}
    ]
  }'

# 读取当前完整集合 / 历史集合
curl -s http://localhost:8000/pages
curl -s "http://localhost:8000/pages?revision=1"
```

## 项目结构

```
app/
  db.py          # SQLite 连接、建表（meta/pages/redirects）、修订号元数据
  linkcheck.py   # href 解析（相对/绝对/#片段/查询串/越界）与整批链接校验
  htmlrewrite.py # 字节级 <a href> 改写：按 HTML 属性语法定位真正的 href 属性，只动其值（其他属性值里的伪 href= 不受影响），其余字节原样保留（relocate 与 /site 输出共用）
  publish.py     # 单写事务：试用整批改动 -> 跳转守护 + 校验 -> 提交；冲突/校验错误（/merge 复用其试用与入口页校验）
  merge.py       # 单写事务：基线-当前路径比对 -> 并行合入 -> 跳转守护 + 整包校验 -> 新修订
  relocate.py    # 单写事务：页面搬迁、站内 href 改写（沿用 linkcheck/htmlrewrite）、旧地址跳转联动、整包校验、新修订
  prune.py       # 单写事务：从 /index.html 与现行跳转目标出发沿真正站内 href 遍历，删除不可达页面（含孤立页面簇），跳转守护 + 整包校验兜底；无可删页面不产生新修订
  redirects.py   # 旧地址跳转：登记/撤销（不推进修订号）、发布/合入/搬迁共用的跳转守护
  site.py        # /site 输出改写：绝对站内 href 加 /site 前缀（仅作用于读取响应，不改存储正文）
  archive.py     # 离线 ZIP 归档：同一读事务快照取当前页面与旧址跳转、站内 href 落盘改写、旧址离线入口、安全唯一映射检查
  main.py        # FastAPI 路由与请求模型（/publish、/merge、/relocate、/prune、/redirects、/site 站点读取与 /download 离线下载）
tests/          # pytest 端到端 + 链接解析/搬迁改写/并行合入/旧地址跳转/站点输出改写/离线下载/不可达清理单测
Dockerfile
docker-compose.yml
entrypoint.sh   # 启动时初始化数据库再拉起 uvicorn
examples.sh     # 可执行的 HTTP 调用示例（含 /site 输出改写、旧地址跳转的访问、搬迁与冲突场景）
```

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```
