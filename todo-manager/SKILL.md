---
name: todo-manager
description: >-
  个人待办/任务管理（GTD 助手）。只要用户想记录、查询、更新、完成或回顾任何待办事项、
  任务、提醒，就使用本技能——例如"帮我记一下…""别忘了…""我今天/这周要做什么""那个任务
  做完了""某某事先搁置""这周完成了什么"，即使用户没有说"待办"或"TODO"这个词。
  Personal todo & task management (GTD). Use this skill whenever the user wants to capture,
  list, update, complete, or review tasks, chores, or reminders — "remind me to...",
  "add a todo", "what's on my plate today", "mark X done", "weekly review" — even if they
  never say the word "todo". Supports priorities, due dates, subtasks, progress, recurring
  tasks, search, reports, and undo.
---

# todo-manager

管理用户的个人任务库。任务数据在**数据目录**（解析顺序：`$TODO_MANAGER_HOME` 环境变量 > `~/.todo-manager/`），
跨项目、跨会话、跨宿主共享同一份数据。

## 铁律：只通过 CLI 读写数据

一切读写都通过本技能的 `scripts/todo.py` 完成，**永远不要直接读写数据目录下的 JSON 文件**。
脚本负责字段校验、原子写入、并发锁、进度自动计算和操作日志——手改 JSON 会绕过所有这些保护。

运行方式（`<skill_dir>` 是本技能所在目录；Windows 用 `python`，其余平台用 `python3`）：

```
python <skill_dir>/scripts/todo.py <子命令> ...
```

每个子命令都支持 `--json`（结构化输出）。命令失败时退出码非 0，错误信息会说明正确用法——
按提示修正后重试即可，不要绕过脚本手改数据。

## 工作流程

### 1. 记录任务（用户说"帮我记一下…"、"别忘了…"）

用户通常只给出事项本身。**先推断缺失字段，再一次性写入**：

| 缺失字段 | 推断规则 |
|---|---|
| 优先级 | "紧急/马上/别忘了" → P0-P1；日常琐事 → P2（默认）；"有空/回头" → P3；涉及客户、老板或有明确 deadline 的上调一级 |
| 截止日期 | 相对时间换算成具体日期（"周五前" → 本周五的 YYYY-MM-DD）；"尽快" → 明天；"最近/这周" → 本周五；**没有任何时间线索就留空，不要编造** |
| 标签 | 按内容归类（如 工作/生活/学习）。先 `list --view all --include-done --json` 看已有标签，**只复用已存在的标签**；确需新标签时在汇报中说明 |
| 子任务 | 一句话包含多个动作（"订机票和酒店"）时拆成子任务，用多个 `--subtask` 传入 |

> **日界线**：凌晨 0:00–4:00 属于"前一天"。这段时间里用户说的"今天/明天/后天/周 X"按前
> 一天的语境换算（如 7/12 凌晨 1 点说"明天"= 7/12，而不是 7/13）；CLI 的今天/过期判断
> 同样按 4 点日界线，两边口径一致。

把推断出的字段名通过 `--inferred` 声明，让数据里留下"这是猜的"标记：

```
todo.py add "给客户回邮件" --due 2026-07-08 --priority P1 --tags 工作 --inferred priority,due
```

**汇报格式**：一句话确认，并明确标注推断值。例如：

> 已记录 #16「给客户回邮件」，优先级 P1、截止 07-08（这两项是我推断的，随时可以改）。

### 2. 查询（"我今天要做什么"、"这周有什么安排"）

```
todo.py list --view today     # 过期 + 今日到期 + 进行中（最常用）
todo.py list --view week      # 本周到期 + 无日期的 P0/P1
todo.py list --view blocked   # 被阻塞的任务
todo.py list --view all       # 全部活跃任务（--include-done 连已完成/已取消一起看）
```

直接把输出转述给用户即可，不要逐字段复读 JSON。用户问模糊问题（"那个客户的事怎么样了"）时先 `search`。

### 3. 行程安排建议（"今天怎么安排"、"先做哪个"、"帮我排一下"）

仅在用户发出这类**排程请求**时进入；普通"今天要做什么"只列清单，不强行附带建议。

先 `list --view today --json` 拿任务，结合优先级、截止日期、预估耗时给出建议顺序。

**并行分析三原则**：

1. 真并行 = 某个任务存在**固有等待间隙**（浸泡、下载、等回复、机器自行运行）时，把另一
   个任务穿插进间隙——如"泡鞋的 20 分钟里清理扫地机器人"；
2. 需要连续专注操作的任务是串行的，不要建议同时进行；
3. 互相独立 ≠ 可并行——没有真实的穿插机会就不硬凑，直接给串行顺序。

**输出**：建议顺序 + 穿插点（若有）+ 一句话理由，不长篇大论。

### 4. 更新与完成（"X 做完了"、"X 先搁置"）

先定位任务：用户给了编号直接用；只有描述时先 `search "关键词"` 找到 ID，多个候选时和用户确认，**不要猜**。

```
todo.py done 12               # 完成任务（循环任务会自动生成下一期）
todo.py done 12.1             # 完成子任务，父任务进度自动重算
todo.py update 12 --due 2026-07-10 --priority P0
todo.py update 12 --status blocked --blocked-reason "等设计稿"
todo.py update 12 --status in_progress
todo.py subtask add 12 "确认收件人名单"
```

注意：有子任务的任务不能手动设 `--progress`（进度由子任务自动计算），脚本会报错提醒。
用户显式修改过的字段会自动从"推断值"标记中移除。

### 5. 回顾与维护

```
todo.py report                # 本周完成、积压、过期、优先级分布、预估耗时
todo.py archive               # 把 done/cancelled 移入年度归档，保持活跃列表干净
todo.py search "关键词"        # 全文搜索，含归档
todo.py undo                  # 撤销上一次写操作（推断错了、误操作时用）
```

完成回顾对话后，若活跃列表里堆了较多已完成任务，主动运行 `archive` 并告知用户。

## 常见对话示例

**例 1** — 用户："帮我记一下，下周三之前要给房东转房租，挺急的"
1. 推断：due = 下周三的日期，priority = P1（有明确期限且用户说急）
2. `todo.py add "给房东转房租" --due <下周三> --priority P1 --tags 生活 --inferred tags`
3. 回复：已记录 #N「给房东转房租」，P1、截止 <日期>（标签"生活"是我推断的）。

**例 2** — 用户："机票订好了"（此前有任务 #12「订机票和酒店」含子任务）
1. `todo.py search "机票"` 定位到子任务 12.1
2. `todo.py done 12.1`
3. 回复：已完成子任务「订机票」，#12 进度 50%（还剩：订酒店）。

**例 3** — 用户："每周一要交周报，帮我设成固定任务"
1. `todo.py add "交周报" --recurrence weekly:mon --due <下周一> --priority P1 --inferred priority`
2. 完成后脚本会自动生成下一期，无需用户重复添加。

## 可视化（网页查看与管理）

用户说「要可视化 / 网页看任务 / 手机上看」时，启动本技能自带的参考服务
`scripts/serve.py`（零依赖，`python <skill_dir>/scripts/serve.py` 即可运行），按下面的决策树执行，
一次只问一个问题。HTTP 契约见 `references/api.md`。

**第 1 问——在哪访问？**

- **(a) 就在这台机器的浏览器**：`python <skill_dir>/scripts/serve.py` →
  交付 `http://127.0.0.1:3007`。无需密码。
- **(b) 数据在远程机器、人在本地浏览器**：远程机器上照 (a) 启动，交付隧道命令
  `ssh -L 3007:127.0.0.1:3007 用户@远程机器` + 本地链接 `http://127.0.0.1:3007`。无需密码。
- **(c) 手机或其他设备**：走「设备访问」流程 ↓。
- **(d) 对接用户自建的前端**：走 `references/api.md` 的「自建前端对接」执行清单
  （核心：拿到前端地址——用户没说就主动问；`--cors-origin <地址>` 重启；两条 curl 自检）。

**设备访问流程（必须有密码）**：

1. 请用户设一个访问密码，然后执行（密码经 stdin 传入，不进命令行历史）：
   `echo 密码 | python <skill_dir>/scripts/serve.py auth set --password-stdin`
2. 按环境启动：
   - 云服务器 + 已有反向代理和证书：`python <skill_dir>/scripts/serve.py`（保持 127.0.0.1），
     在反代上加一段转发到 3007 的配置并重载；
   - 云服务器裸环境：`python <skill_dir>/scripts/serve.py --host 0.0.0.0`，如实告知链接是
     http 无加密、建议日后加反代；安全组需放行端口（Agent 无云商权限时提示用户
     在控制台开一次）；
   - 家用电脑：`python <skill_dir>/scripts/serve.py --host 0.0.0.0`，交付局域网地址
     `http://<内网IP>:3007`，说明仅同 Wi-Fi 可用；用户要外网访问则需内网穿透
     （Tailscale/frp 等），属于另行处理的事项。
3. **自检后才交付链接**：
   `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3007/api/tasks` 应为 401；
   `curl -s -o /dev/null -w "%{http_code}" -u admin:密码 同URL` 应为 200。
4. 常驻（云服务器）：用 systemd 或环境等价物让服务自启、自拉起。

**密码维护**：用户忘了或想换 → 直接重跑 `auth set`（旧会话全部立即失效），没有
找回流程。用户不想要密码了（回到仅本机）→ `python <skill_dir>/scripts/serve.py auth unset`。

**边界**：undo / archive 等维护操作不在页面里，用户在对话里说即可（走 CLI）。

## 错误与恢复

- 脚本报"找不到任务"时会列出最近的活跃任务——对照后和用户确认，不要对错误的 ID 强行操作。
- 报"写锁超时"说明有并发写入，稍候重试一次；仍失败再按错误提示处理。
- 报"todos.json 解析失败"时**不要重建文件**，告知用户可从 `journal.jsonl` 恢复，协助人工重放。

## 参考

- 数据文件格式、字段约束、journal 结构：读 `references/schema.md`（给 UI/程序对接用，日常操作不需要）。
