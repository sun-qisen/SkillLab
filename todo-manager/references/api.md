# todo-manager HTTP API 契约（contract v1）

本文档是 `scripts/serve.py` 的对外契约，**第一读者是 AI Agent**：一个没有任何上下文的
Agent 只读本文档即可开发出能对接的前端。任务对象（Task）的字段结构见 `schema.md`，
本文不重复定义。内置页面 `scripts/webui.html` 是本契约的活参考实现。

启动：`python scripts/serve.py`（默认 `127.0.0.1:3007`，`--host/--port/--data-dir` 可调）。
运行时零第三方依赖。

## 通用约定

- 前缀 `/api/`。请求体一律 JSON（UTF-8）。契约版本由 `GET /api/status` 返回，
  不兼容变更会递增 `contract_version`。
- 成功响应 = todo.py `--json` 输出原样透传；失败响应统一
  `{"error": {"code": "...", "message": "..."}}`，message 是可直接展示给人的中文。
- 状态码：200 成功；400 参数/校验错误；401 未认证；403 拒绝的 Host（仅无密码模式）；
  404 任务不存在；405 方法不支持；409 写锁忙（稍后重试）；500 内部错误。
- PATCH 语义：出现的字段才更新；`due`/`recurrence` 可传 `null` 清除，其余字段不接受 null。
- 日期口径（逻辑日）：过期/今天的判断按「凌晨 0:00–3:59 属前一天」，前端如需本地判断
  （如高亮今天到期）必须采用同一口径，与 CLI/本 API 保持一致。

## 认证

服务有两种状态，由 `GET /api/status` 的 `auth_required` 区分：

- **未设密码**：只监听 127.0.0.1，所有端点直接可用（本机即信任）。
- **已设密码**（`python scripts/serve.py auth set` 设置）：除 `/`、`/api/status`、
  `/api/auth/login`、OPTIONS 预检外，一切请求必须携带凭证，否则 401。

两条可抄的凭证路径：

**浏览器（会话 cookie）**——登录一次，之后 cookie 自动携带：

```js
await fetch(base + '/api/auth/login', {
  method: 'POST', headers: {'Content-Type': 'application/json'},
  credentials: 'include',                 // 跨域前端必须；同源可省
  body: JSON.stringify({password: '访问密码'})
});
// 之后所有请求带 credentials: 'include' 即可
const r = await fetch(base + '/api/tasks?view=today', {credentials: 'include'});
```

cookie 名 `todo_session`，HttpOnly、SameSite=Lax、默认 30 天。经 TLS 反代时自动加
Secure（识别 `X-Forwarded-Proto: https`）。改密码会让所有已签发会话立即失效。
`POST /api/auth/logout` 登出。

**脚本 / 服务端（HTTP Basic）**——每个请求直接带密码，无需登录：

```bash
curl -u admin:访问密码 http://127.0.0.1:3007/api/tasks?view=today
```

用户名任意（示例统一用 admin），校验只看密码。

## 端点

### GET /api/status （免认证）

```json
{"version": "1.0", "contract_version": 1, "auth_required": true, "authenticated": false}
```

前端启动时先调它：`auth_required && !authenticated` → 先展示登录界面。

### POST /api/auth/login （免认证）

请求 `{"password": "..."}`。成功 200 `{"authenticated": true}` + `Set-Cookie`；
失败 401（服务端对失败有约 1 秒节流）。

### POST /api/auth/logout

200 `{"authenticated": false}`，同时清除 cookie。

### GET /api/tasks

查询参数（全部可选）：`view=today|week|all|blocked`（默认 all）、`status`、`tag`、
`overdue=1`、`include_done=1`。响应：

```json
{"view": "today", "count": 2, "tasks": [ <Task>, ... ]}
```

### POST /api/tasks

```json
{"title": "订机票和酒店", "priority": "P1", "due": "2026-07-20",
 "tags": ["生活"], "notes": "出差", "estimate": 30,
 "subtasks": ["订机票", "订酒店"], "recurrence": "weekly:mon",
 "inferred": ["priority", "due"]}
```

仅 `title` 必填。响应 `{"task": <Task>}`。`inferred` 用于标注哪些字段是 AI 推断的。

### GET /api/tasks/{id}

`{id}` 是整数任务 ID。响应 `{"task": <Task>}`；不存在 → 404。

### PATCH /api/tasks/{id}

可更新字段：`title`、`notes`、`priority`、`status`、`due`、`recurrence`、
`blocked_reason`、`tags`（数组，整体替换）、`estimate`（正整数）、`progress`（0-100，
有子任务的任务不可手动设，会 400）。示例——阻塞一个任务并清除截止日期：

```json
{"status": "blocked", "blocked_reason": "等设计稿", "due": null}
```

响应 `{"task": <Task>}`（更新后的完整任务）。

### POST /api/tasks/{id}/done

完成任务或子任务：`{id}` 形如 `12`（任务）或 `12.1`（子任务）。无请求体。
响应 `{"task": <Task>}`；循环任务完成时自动生成下一期（journal 可见 recur_spawn）。

### POST /api/tasks/{id}/subtasks

`{id}` 是父任务整数 ID。请求 `{"title": "...", "due": "2026-07-15"}`（due 可选）。
响应 `{"task": <父Task>, "subtask": <Subtask>}`。

### GET /api/search?q=关键词

全文搜索（标题/备注/子任务标题，含归档）。响应：

```json
{"query": "机票", "matches": [ <Task + "_source": "active"|"archive/2026">, ... ]}
```

`_source` 非 `active` 的是归档任务，只读，不要对其发写请求。

### GET /api/report

```json
{"completed_this_week": [<Task>...], "active": 5, "overdue": 1,
 "by_status": {"todo": 3, "in_progress": 1, "blocked": 1},
 "by_priority": {"P0": 0, "P1": 2, "P2": 2, "P3": 1},
 "estimate_minutes_total": 180}
```

### 维护操作（不在 HTTP 契约内）

`archive`、`undo` 属维护操作，v1 不提供 HTTP 端点，由 Agent 通过 CLI 执行；
将来需要时升 contract_version 再加。

## 自建前端对接（场景④，给部署 Agent 的执行清单）

前端与本 API 不同源时（前端部署在别的域名/端口），浏览器的同源策略要求本服务显式
放行该来源，否则前端 JS 的请求会被浏览器拦截（CORS 错误）：

1. 确定前端地址（origin，形如 `https://todo.example.com`，只到端口为止无路径）。
   部署前端的 Agent 自己知道；不知道就**问用户**「你的前端部署在哪个网址」。
2. serve.py 启动参数加 `--cors-origin <该地址>`（可重复多次，开发期可同时放行
   `http://localhost:5173` 这类调试源）后重启服务。
3. 自检两条：

```bash
# 预检应 204 且回显 Access-Control-Allow-Origin
curl -si -X OPTIONS -H "Origin: https://todo.example.com" \
     -H "Access-Control-Request-Method: POST" https://<API地址>/api/tasks | head -8
# 带凭证真实请求应 200
curl -su admin:密码 https://<API地址>/api/tasks?view=all
```

4. 常见坑：
   - 前端 fetch 忘带 `credentials: 'include'` → 登录成功但后续请求仍 401；
   - cookie SameSite=Lax：前端与 API **同注册域**（如都在 `*.example.com`）时 cookie
     正常；完全不同域名的前端请改用 `Authorization` 头携带密码；
   - 浏览器控制台的 CORS 报错是「来源未放行」，不是认证失败——回到第 2 步。

## 进阶配方：替换登录方式（OAuth 等）

想用第三方登录（飞书/GitHub 等 OAuth）替代内置密码：自建一个认证网关（必须有
服务端组件，app_secret 不能进前端），前置在 serve.py 之前；serve.py 以未设密码的
127.0.0.1 本地模式运行，信任前置层已把关。本服务对此零改动、无感知。

## 性能说明

每个未命中缓存的请求经一次 todo.py 子进程（约 100–200ms）；读端点按 todos.json 的
mtime 缓存，重复刷新为内存直出。单用户场景无需关心。若外部程序绕过 API 直接改数据
文件（不推荐），mtime 变化同样会让缓存失效。
