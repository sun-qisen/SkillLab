# todo-manager 可视化参考服务（serve.py）—— 设计文档

日期：2026-07-12
状态：设计已与用户逐节确认，待实施。

## 0. 背景、范围与完成标准

todo-manager 技能以 CLI（`scripts/todo.py`）管理个人任务数据，数据契约见
`references/schema.md`。本次迭代为技能增加**可视化参考服务**：任何技能使用者都能让
Agent 一键部署一个网页界面，在浏览器（含手机）里查看和管理任务；同时提供正式的
HTTP 契约，供使用者自行开发定制前端。

**四种使用场景**（通用性的验收标准，全部开箱可用、零二次开发）：

| 场景 | 访问方式 | 监听 | 密码 | 用户动作 |
|---|---|---|---|---|
| ① 本机 | 本机浏览器 localhost | 127.0.0.1 | 不需要 | 点链接 |
| ② SSH 隧道 | 数据在远程机器，`ssh -L` 后本地浏览器 | 127.0.0.1 | 不需要 | 建隧道后点链接 |
| ③ 设备访问 | 手机等其他设备（经公网或局域网） | 0.0.0.0 或反代 | **必须** | 打开链接 → 登录页输密码 |
| ④ 自建前端 | 使用者自己开发、部署在别处的前端 | 同③ | **必须** | 前端自带凭证处理 |

场景①②免认证的依据：127.0.0.1 只有已登录本机的人可达，SSH 密钥即认证。
场景④的前端开发是二次开发，但认证直接复用内置密码登录，前端本身预期由 AI Agent
依据 `api.md` 完成（vibe coding），使用者不参与开发。

**本次交付四件互相咬合的产物**：

1. `scripts/serve.py` —— 零依赖 HTTP API + 密码认证 + CORS + 静态页面分发；
2. `scripts/webui.html` —— 内置单文件页面；
3. `references/api.md` —— HTTP 契约，第一读者是 AI Agent；
4. `SKILL.md` 增补「可视化」部署工作流（Agent 决策树）。

**完成标准**：自动化测试通过 + 本机跑 serve.py 在浏览器走通场景①全部日常操作 +
设密码模拟场景③登录流。服务器侧基础设施（反代/证书/systemd 常驻）不在本次范围，
部署时由数据所在机器上的 Agent 依 SKILL.md 执行。

**核心取舍（已定案）**：

- **安全边界在 API，不在页面**：前端是任何人可加载的静态文件，不构成防线；防线是
  服务端对每个数据请求验证凭证。
- serve.py 与 todo.py 的集成方式：**全部走子进程**（`python todo.py <子命令> --json`），
  读写皆然。todo.py 零改动；校验、文件锁、journal、逻辑日口径天然一致，不存在第二套
  读写逻辑。每请求 100–200ms 进程开销对单用户无感，读端点另有 mtime 缓存（§1）。
  已否决：import todo.py 内部函数（把内部实现升格为稳定接口，回归风险大于收益）；
  读直读 JSON 文件（today/week 视图与 4 点逻辑日需在 serve.py 重实现，制造第二套逻辑）。
- 实现底座：**纯 Python 标准库**（ThreadingHTTPServer），不用框架。零依赖是技能身份
  的一部分（`python serve.py` 一条命令在任何使用者机器上能跑，无需 pip install，
  依赖面为零也是安全策略的一部分）。FastAPI 的头号价值（声明式校验）在薄封装架构下
  天然贬值——校验是 todo.py 的职责；代价仅是约 200–300 行一次性路由/解析水管代码。
  若未来出现真实性能需求，按 api.md 契约替换实现，消费方零改动。
- 路由风格：REST，映射到 CLI 子命令。
- 认证是本服务分内事、自己处理而非外包给反向代理：反代方案无法覆盖没有反代的
  使用者，且会无差别拦截跨域预检请求。反代（若有）职责仅为 TLS 终止与转发。

## 1. serve.py 整体结构

单文件，纯标准库，四个组成部分：

1. **HTTP 服务**：`ThreadingHTTPServer` + RequestHandler 按路由表分发。
   默认监听 `127.0.0.1:3007`。
2. **CLI 桥**：API 请求翻译成 `python todo.py <子命令> --json --data-dir <同一数据目录>`
   子进程调用（todo.py 按 serve.py 同目录定位，二者同版本分发）。退出码非 0 时把
   stderr 信息包装成 JSON 错误响应。
3. **认证层**：路由分发前统一检查（§3）；静态页面壳与 OPTIONS 预检放行，数据端点全部校验。
4. **静态分发**：`GET /` 返回 webui.html（启动时读入内存）。

**mtime 读缓存**：一切写操作（含 archive/undo）最终都改写 `todos.json`，其 mtime 是
数据变化的可靠信号。CLI 桥的 GET 端点（`/api/status`、auth 端点不经桥、不缓存）以
「查询签名 → todo.py 输出」缓存于内存，todos.json mtime 变化即整体失效。缓存的是
todo.py 原始输出，不引入第二套逻辑；频繁刷新时仅首次请求真正跑子进程。写操作不缓存。

**错误响应**统一 `{"error": {"code": "...", "message": "..."}}`；message 直接取 todo.py
的 stderr 中文信息，可原样展示给人。状态码映射：200 成功 / 400 参数或校验错误 /
401 未认证 / 404 任务不存在 / 409 写锁超时（提示稍后重试）/ 500 其它。

**日志**：stdout，一行一请求（时间、方法、路径、状态码）。

## 2. API 路由契约（api.md 骨架）

**通用约定**：

- 路径前缀 `/api/`，不带版本号（单消费者，YAGNI）。契约版本写在 api.md 并由
  `GET /api/status` 返回，不兼容变更时升版本。
- 请求体一律 JSON。成功响应 = todo.py `--json` 输出原样透传（如 `{"task": {...}}`、
  `{"view","count","tasks"}`）；Task 字段结构以 `schema.md` 为准，api.md 引用不重复定义。
- `tags`/`subtasks`/`inferred` 在 JSON 中是数组，serve.py 转成 CLI 参数格式。
- **清除字段语义**：PATCH 显式传 `null`（如 `"due": null`）= 清除（映射 CLI `--due none`）；
  不传 = 不动。

**路由表**：

| 方法 + 路径 | 对应 CLI | 认证 | 说明 |
|---|---|---|---|
| `GET /api/status` | — | 免 | `{version, contract_version, auth_required, authenticated}`，页面据此决定是否显示登录视图 |
| `POST /api/auth/login` | — | 免 | body `{password}`，成功签发会话 cookie |
| `POST /api/auth/logout` | — | 需 | 清除会话 cookie |
| `GET /api/tasks?view=&status=&tag=&overdue=&include_done=` | `list` | 需 | 参数与 CLI 一一对应 |
| `POST /api/tasks` | `add` | 需 | title 必填；due/priority/tags/notes/estimate/recurrence/subtasks/inferred 可选 |
| `GET /api/tasks/{id}` | `show` | 需 | |
| `PATCH /api/tasks/{id}` | `update` | 需 | 任意 update 支持的字段 |
| `POST /api/tasks/{id}/done` | `done` | 需 | `{id}` 可为 `12` 或 `12.1`（子任务） |
| `POST /api/tasks/{id}/subtasks` | `subtask add` | 需 | body `{title, due?}` |
| `GET /api/search?q=` | `search` | 需 | 含归档 |
| `GET /api/report` | `report` | 需 | |

**v1 不暴露**：`archive`、`undo`（维护操作留给 Agent 走 CLI）。api.md 注明
「维护操作暂不在 HTTP 契约内，需要时升契约版本」。API 面与页面功能面严格对齐。

**api.md 的写法标准**：一个零上下文的 AI Agent 只读这份文档就能做出能跑的前端。
每条路由给完整请求/响应实例；认证给两条可抄代码路径（浏览器：login → cookie →
`fetch(..., {credentials: 'include'})`；脚本：`Authorization` 头）；专设「自建前端对接」
执行清单（见 §3 CORS、§6 场景④）；列常见坑（漏 `credentials: 'include'`、SameSite
限制、把 CORS 报错误判为认证失败）。

## 3. 认证与 CORS

**密码存储**：`python serve.py auth set`（交互输入，或 `--password-stdin` 供 Agent
管道传入）写 `<data-dir>/serve.json`（权限 0600，`auth set` 生成维护，人和 Agent 都不手编）：

```json
{ "schema_version": 1,
  "auth": { "algo": "pbkdf2_sha256", "iterations": 600000,
            "salt": "...", "hash": "...", "generation": 1 },
  "session": { "secret": "<随机32字节>", "ttl_days": 30 } }
```

PBKDF2-HMAC-SHA256（60 万次迭代）+ `hmac.compare_digest` 恒时比较。`session.secret`
首次 `auth set` 随机生成，用于签 cookie。

**激活规则**：

- serve.json 有 auth 段 → 每个数据请求都校验，**不论监听地址**（场景③中 serve.py
  可能在 127.0.0.1 上由反代转发，本地监听同样必须校验）；
- 未设密码 + `--host` 非 127.0.0.1 → 拒绝启动，提示先 `auth set`。

**会话 cookie**（无状态、无数据库）：

- 登录成功签发 `todo_session=<过期时间戳>.<generation>.<HMAC-SHA256(secret, 前两段)>`，
  有效期 `ttl_days`（默认 30 天）。
- 属性 `HttpOnly; SameSite=Lax; Path=/`；请求经 TLS（直连 https 或
  `X-Forwarded-Proto: https`）时加 `Secure`。
- SameSite=Lax 覆盖同站前端（同注册域的子域名间 cookie 可随凭证请求携带）；
  真正跨站的自建前端改用 `Authorization` 头，api.md 注明。
- 登出 = 清 cookie。**重跑 `auth set`（改密码）→ generation +1 → 已签发会话立即全部
  失效**，即全端登出。忘记密码不需要找回流程：Agent 在数据所在机器上有 shell，
  用户说一声即重设。
- 登录失败节流：失败后延迟 1 秒再响应，对抗公网暴力猜测。

**程序化旁路**：每个请求先看 `Authorization: Basic`。**校验只看密码，用户名忽略**
（Basic 协议格式要求该字段存在而已，本系统无用户概念）。文档示例统一
`curl -u admin:密码`，并注明「用户名任意」。

**登录视图**：不做独立服务端登录页。webui.html 启动先调免认证的 `GET /api/status`，
`auth_required && !authenticated` 则渲染密码框 + 登录按钮，成功后切主界面。
满足「静态壳可无认证加载、数据端点全部受保护」。

**CORS**（纯浏览器机制，只在场景④参与）：

- 默认不发 CORS 头（仅同源）。场景①②③中页面由 serve.py 自己分发、与 API 同源，
  CORS 概念全程不出现，用户无感。
- `--cors-origin <前端地址>` 可重复传多次（如自建前端开发期同时放行 localhost
  调试源与线上源）。设置后：OPTIONS 预检免认证放行；正式响应对白名单内的来源反射
  `Access-Control-Allow-Origin` + `Access-Control-Allow-Credentials: true` + `Vary: Origin`；
  名单外来源不反射。
- 不做「反射任意 Origin」的所谓通用实现：CORS 声明回答的是「哪些网站是这个 API 的
  合法前端」，该信息只有部署者有；"通用" = 删除白名单，是策略错误而非技术便利。
  该参数不进标准部署问答（§6），只出现在 api.md「自建前端对接」一节。
- 更换登录方式（OAuth 等）属于进阶二次开发：二开者自备认证网关前置于 serve.py
  （此时 serve.py 以无密码本地模式运行，信任前置层），本服务为此需要做的事为零，
  api.md 记为进阶配方即可。

**DNS rebinding 防御**：**仅未设密码模式**（必然 127.0.0.1 监听）校验请求 Host 头
必须是 localhost / 127.0.0.1 / [::1]，不符拒绝——防恶意网页把自有域名解析到
127.0.0.1 绕过同源策略打本机无认证服务。已设密码时不做 Host 校验：rebinding 请求
不会携带真实站点的 cookie（cookie 按域隔离）也没有密码，自然 401；且反代场景下
Host 是公网域名，serve.py 无法预知，强校验反而误伤。

## 4. 内置单文件页面（webui.html）

**形态**：serve.py 同目录单 HTML，样式脚本全内联、零外部资源（断网可用）、零构建、
原生 JS（无框架，几百行）。移动端优先的响应式布局。同时充当 api.md 的活参考实现，
供开发自建前端的 Agent 阅读学习对接方式。

**功能面**（日常操作全进页面，维护操作留给 Agent）：

- 视图切换：today / week / blocked / all（默认 today；all 可勾选「含已完成」）。
- 任务卡片：标题、优先级色标、截止日（过期红、今天高亮）、进度条、标签、子任务展开。
- 快速操作：卡片上勾选完成（任务/子任务）、开始（→ in_progress）；详情面板改
  状态/优先级/截止日/标题/备注/标签/预估，加子任务，设阻塞原因。
- 新增：输入框 + 展开可选字段（优先级/日期/标签/循环/子任务）。
- 搜索：调 `/api/search`，含归档（归档任务只读展示）。
- 报表：report 数据渲染统计视图（本周完成、积压、过期、优先级分布）。
- 推断字段弱化样式：`inferred` 字段加「AI 推断」小标记。
- 登录视图：见 §3。

**日期口径**：过期/今天判断在页面 JS 实现 4 点逻辑日规则（凌晨 0:00–3:59 属前一天，
见 schema.md），与 CLI 一致。这是唯一在前端重复的读逻辑，api.md 注明该口径供一切
前端遵循。

**不做**：undo/archive、拖拽排序、离线缓存、多主题、WebSocket/轮询实时同步
（单用户无协同需求；操作后重拉 + 手动刷新足够）。

## 5. CLI 参数面

```
python serve.py                          # 启动，默认 127.0.0.1:3007
    --host 0.0.0.0                       # 非 127.0.0.1 且未设密码 → 拒绝启动
    --port 3007
    --data-dir <路径>                     # 解析顺序与 todo.py 一致：参数 > $TODO_MANAGER_HOME > ~/.todo-manager
    --cors-origin <地址>                  # 可重复
python serve.py auth set                 # 设置/修改密码（交互或 --password-stdin）
python serve.py auth unset               # 移除密码，回到仅本机模式
```

## 6. SKILL.md 部署工作流（Agent 决策树）

用户说「要可视化 / 网页看任务」时 Agent 照此执行，用户只回答问题：

1. **问访问方式**（一次只问这一个）：
   - (a) 本机浏览器 → `python serve.py`，交付 `http://127.0.0.1:3007`；
   - (b) 数据在远程机器、人在本地浏览器 → 交付 SSH 隧道命令 + localhost 链接；
   - (c) 手机/其他设备 → 步骤 2；
   - (d) 对接自建前端 → 步骤 3。
2. **设备访问（场景③）**：请用户设访问密码 → `auth set` → 按环境分支：
   - 云服务器：已有反代+证书 → 监听 127.0.0.1 挂反代；裸环境 → `--host 0.0.0.0`，
     如实提醒无 TLS、建议加反代；
   - 家用电脑：`--host 0.0.0.0`，交付局域网地址 `http://<内网IP>:3007`，说明仅同
     Wi-Fi 可用、外网需内网穿透（Tailscale/frp 等，超出开箱流程，用户需要时另行处理）；
   - **自检**：无凭证 curl → 401，带凭证 → 200，两条都过才交付链接。
3. **自建前端（场景④）**：确认前端地址——**用户没说就主动问**「你的前端部署在哪个
   网址」→ 启动参数加 `--cors-origin <地址>` 重启 → 自检（OPTIONS 预检放行 +
   带凭证跨域请求 200）→ 告知完成。用户在全流程只提供密码（若未设）与前端网址
   （若 Agent 不知道）两项非技术输入；CORS 等术语仅在用户主动询问时用人话解释。
4. **密码维护**：忘记/更换 → 重跑 `auth set`，旧会话自动全失效。
5. **一次性例外**：云安全组开端口、DNS 解析等 Agent 无云商凭证的操作，提示用户在
   控制台做一次。
6. **常驻**：云服务器部署后用 systemd（或环境等价物）自启、自拉起。

## 7. 测试策略

新增 `tests/test_serve.py`（pytest，与现有 test_todo.py 同风格；pytest 是开发期依赖，
不违反运行时零依赖）。**真端到端**：每测试起真 serve.py 进程（随机端口 + 临时数据
目录），标准库 urllib 发真 HTTP，穿透「HTTP → 子进程 → todo.py → 文件」全链路。

- 认证：未设密码绑非 127.0.0.1 拒启；设密码后无凭证 401 / 登录种 cookie / 篡改与
  过期 cookie 拒绝 / 改密码后旧 cookie 全失效 / Basic 头放行且用户名任意 / 失败节流。
- 路由：每端点正常路径 + 错误映射（404、400、PATCH null 清除字段）。
- CORS：无 flag 无头；有 flag 预检放行、白名单内反射、名单外不反射。
- 缓存：写后 GET 拿到新数据（mtime 失效生效）。
- 防御：Host 头校验。
- 页面：冒烟断言 `GET /` 返回 HTML；功能走人工验证（完成标准见 §0）。

## 8. 文件布局

```
todo-manager/
├── SKILL.md              # 增补「可视化」一节（§6）
├── references/
│   ├── schema.md         # 基本不动，加一行指向 api.md
│   └── api.md            # 新：HTTP 契约
└── scripts/
    ├── todo.py           # 零改动
    ├── serve.py          # 新
    └── webui.html        # 新
tests/
└── test_serve.py         # 新
```
