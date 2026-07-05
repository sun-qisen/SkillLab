# TODO Manager Skill — 设计文档

日期：2026-07-05
状态：已由用户逐节确认

## 1. 目标与定位

一个 Claude Code Agent skill，作为**个人全局 GTD 助手**：用户用自然语言记录、查询、更新待办事项，Agent 自动补全缺失的优先级、截止日期等字段。任务库跨项目、跨会话共享。

核心需求：优先级、截止日期、进度管理、状态管理、子任务；用户通常只给出事项本身，元数据由 Agent 推断补全；存储既要方便 Agent 实时查看，也要为未来 UI 可视化做准备。

已确认的关键决策：

- 定位：个人全局任务管理（非按项目隔离）。
- 补全策略：Agent 推断后**直接写入**，标记为推断值，汇报时告知，事后可改。
- 状态/进度模型：状态机 + 自动进度（子任务完成比例聚合）。
- 子任务：两层结构（任务 + 一级子任务），不支持更深嵌套。
- 触发方式：自然语言自动触发（无需斜杠命令）。
- 存储方案：JSON 单一数据源 + CLI 脚本操作层（方案 A）。
- 扩展功能：循环任务、周报统计、时间估算、操作日志 + 撤销、全文搜索。
- 明确不做：会话启动提醒 hook、任务依赖（blocked_by 自动联动）、导出功能、斜杠命令入口。
- **可移植性**：遵循开放的 Agent Skills 规范（SKILL.md + YAML frontmatter），不依赖任何 Claude Code 专有能力，可直接安装到 Hermes、Openclaw 等支持 Agent Skills 的宿主；各宿主共享同一份任务数据。
- 实现阶段使用 skill-creator 技能完成创建与评估。

## 2. 总体架构

### 2.1 Skill 结构

开发目录 `todo_manager/`。结构遵循开放的 Agent Skills 规范，安装位置由宿主决定（Claude Code：`~/.claude/skills/todo-manager/`；Hermes/Openclaw：各自的 skills 目录），skill 内部一律用相对路径引用自身文件：

```
todo-manager/
├── SKILL.md              # YAML frontmatter (name/description) + Agent 行为准则（推断规则、汇报格式）
├── scripts/
│   └── todo.py           # 唯一的数据读写入口（Python 3，标准库实现，无第三方依赖）
└── references/
    └── schema.md         # 数据 schema 文档，供未来 UI 开发对接
```

**可移植性约束**：

- SKILL.md 只使用规范通用字段（`name`、`description`），不使用 Claude Code 专有扩展；正文指令措辞与宿主无关（只假设宿主能执行 shell 命令、读文件）。
- 宿主最低要求：能运行 `python3 scripts/todo.py ...`；不依赖 hooks、MCP、斜杠命令等宿主特性。
- `todo.py` 跨平台（Windows / macOS / Linux / WSL）：路径处理用 pathlib，文件锁不用 fcntl（用锁文件原子创建实现），输出避免依赖终端特性。

### 2.2 数据目录

与 skill 代码分离，卸载/升级 skill 不影响数据。位置与宿主无关，Claude Code、Hermes、Openclaw 读写同一份数据。解析顺序：`--data-dir` 参数 > `TODO_MANAGER_HOME` 环境变量 > 默认 `~/.todo-manager/`：

```
~/.todo-manager/
├── todos.json            # 活跃任务，唯一数据源
├── archive/2026.json     # 已完成/取消任务按年归档，结构与 todos.json 相同
├── journal.jsonl         # 追加式操作日志（支持 undo 与灾难恢复）
└── config.json           # 用户偏好（时区、默认优先级等），首次运行自动生成
```

注意：Windows 原生环境与 WSL 的 `~` 不同。若在两边混用，可将 `TODO_MANAGER_HOME` 指向同一路径（如 WSL 侧设为 `/mnt/c/Users/<name>/.todo-manager`）以共享数据。

### 2.3 数据流

```
用户自然语言 ("帮我记一下…" / "我今天要做什么" / "12 号任务做完了")
  → Agent 依据 SKILL.md 识别意图、推断缺失字段
  → 调用 todo.py 子命令（判断留给 Agent，读写只走脚本）
  → 脚本校验、原子写入、追加 journal、返回格式化结果
  → Agent 向用户汇报（标注哪些值是推断的）
```

Agent 永远不直接读写 JSON 文件。未来 UI 直接读取 `todos.json`（schema 有版本号），与 Agent 共享同一数据源。

## 3. 数据模型

### 3.1 todos.json

```json
{
  "schema_version": 1,
  "next_id": 13,
  "tasks": [
    {
      "id": 12,
      "title": "给客户回邮件",
      "notes": "关于 Q3 合同续签",
      "status": "in_progress",
      "priority": "P1",
      "due": "2026-07-08",
      "progress": 50,
      "tags": ["工作"],
      "estimate_minutes": 30,
      "recurrence": null,
      "inferred": ["priority", "due"],
      "blocked_reason": null,
      "created_at": "2026-07-05T10:30:00+08:00",
      "updated_at": "2026-07-05T14:00:00+08:00",
      "completed_at": null,
      "subtasks": [
        { "id": "12.1", "title": "整理合同要点", "status": "done" },
        { "id": "12.2", "title": "起草回复", "status": "todo", "due": null }
      ]
    }
  ]
}
```

### 3.2 字段规则

- **id**：自增整数，由 `next_id` 分配，归档后不复用。子任务 ID 为 `父ID.序号`（如 `12.1`）。
- **status（任务）**：`todo → in_progress → done`，另有 `blocked`（需 `blocked_reason`）与 `cancelled`。任意状态可转 `cancelled`；`done`/`cancelled` 可被重新打开（转回 `todo`）。
- **status（子任务）**：仅 `todo / done` 两态。
- **priority**：`P0` 紧急重要 / `P1` 重要 / `P2` 常规（默认）/ `P3` 有空再说。
- **progress**：0–100 整数。有子任务时由脚本强制自动计算 = round(done 子任务数 / 子任务总数 × 100)，手动设置报错；无子任务时可手动设置。状态转 `done` 时自动置 100。
- **due**：`YYYY-MM-DD` 或 `null`（无时间线索时不瞎编）。子任务可选 `due`。
- **estimate_minutes**：预估耗时（分钟），可选。
- **recurrence**：`null` 或规则字符串，支持的语法见 3.3。
- **inferred**：字符串数组，记录 Agent 推断的字段名。用户显式修改某字段时，脚本自动将其移出该数组。
- **时间戳**：ISO 8601 带时区。

### 3.3 循环任务

`recurrence` 支持的规则（刻意保持小集合）：

- `"daily"` — 每天
- `"weekly:mon"` … `"weekly:sun"` — 每周某天
- `"monthly:15"` — 每月某日（超出当月天数取月末）
- `"every:N:days"` — 每 N 天

行为：循环任务被 `done` 时，脚本自动生成下一期任务（新 id，`due` 按规则从**完成日**推算，继承 title/priority/tags/estimate/recurrence，子任务重置为全 `todo`；新任务的 `recurrence` 保留，原任务的清空）。原任务保持 `done` 状态留在 `todos.json`，与普通任务一样等待 `archive` 时移入归档。生成动作写入 journal 并在命令输出中告知。

### 3.4 journal.jsonl

每次写操作追加一行：

```json
{"ts": "2026-07-05T14:00:00+08:00", "op": "update", "task_id": 12, "before": {...}, "after": {...}}
```

`op` ∈ add / update / done / subtask_add / archive / undo / recur_spawn。`before/after` 只记录变化的字段（add 的 before 为 null）。用途：`undo` 回滚上一步、数据损坏时人工重放恢复。

## 4. Agent 行为（SKILL.md 内容要点）

### 4.1 触发

SKILL.md 的 description 覆盖自然语言场景：记录待办（"帮我记一下/别忘了提醒我"）、查询（"我今天要做什么/这周有什么安排"）、更新（"X 做完了/X 先搁置"）、回顾（"这周完成了什么"）。description 采用中英双语关键词，保证不同宿主、不同语言输入下都能可靠触发。不提供斜杠命令。

### 4.2 推断规则

| 缺失字段 | 策略 |
|---|---|
| 优先级 | 语义信号：含"紧急/马上/别忘了"→ P0–P1；日常琐事 → P2（默认）；"有空/回头"→ P3；涉及客户、老板、明确 deadline 的上调一级 |
| 截止日期 | 相对时间词换算为具体日期（"周五前"→ 本周五）；"尽快"→ 明天；"最近/这周"→ 本周五；无任何时间线索 → 留空 |
| 标签 | 从内容归类，**只复用已存在的标签**（首次使用某标签需在汇报中说明），避免标签爆炸 |
| 子任务 | 一句话含多个动作（"订机票和酒店"）时拆为子任务，写入后告知 |

所有推断字段通过 `--inferred` 参数写入 `inferred` 数组。

### 4.3 汇报格式

写入后一句话确认，明确标注推断值，例如：

> 已记录 #16「给客户回邮件」，优先级 P1、截止 07-08（这两项是我推断的，随时可改）。

查询类请求直接转述 `list` 视图输出，不逐字段复读 JSON。

## 5. CLI 命令面

所有子命令支持 `--json`（结构化输出）与默认人类可读输出；退出码非 0 表示失败，错误信息含正确用法提示。

```
todo.py add "标题" [--due YYYY-MM-DD] [--priority P0..P3] [--tags a,b]
        [--notes "..."] [--estimate MIN] [--recurrence RULE]
        [--subtask "..."]... [--inferred priority,due,...]
todo.py list [--view today|week|all|blocked] [--status S] [--tag T] [--overdue]
todo.py show <id>
todo.py update <id> [--title|--due|--priority|--status|--progress|--notes|--estimate|--recurrence|--blocked-reason ...]
                               # id 为子任务（12.1）时仅支持 --title/--due/--status(todo|done)
todo.py done <id>              # id 可为 12 或 12.1；重算父进度；循环任务生成下一期
todo.py subtask add <id> "标题" [--due ...]
todo.py search "关键词"        # 搜标题/备注/子任务，含归档，标注来源
todo.py report [--week]        # 完成情况、积压、优先级分布、预估耗时合计
todo.py archive [--before DATE]
todo.py undo                   # 回滚 journal 中最后一次写操作
```

`list --view today` 输出示例（Agent 高频入口，token 友好）：

```
📋 今天 (2026-07-05) · 3 项待办，1 项过期
──────────────────────────────────────
⚠ #9  P0 交税务申报        过期 2 天
● #12 P1 给客户回邮件      截止 07-08  [██░░] 1/2  ~30m
○ #15 P2 预约体检          今天
```

视图规则：`today` = 过期 + 今日到期 + in_progress；`week` = 本周内到期 + 无日期的 P0/P1；排序按 过期 > 优先级 > 截止日。

## 6. 错误处理

- **原子写入**：先写临时文件再 rename；崩溃不损坏 `todos.json`。
- **写前校验**：按 schema 校验（非法状态、非法日期、对有子任务的任务手动设 progress 等直接报错并提示正确用法），Agent 依据错误信息自我纠正。
- **并发保护**：写操作持有 `todos.json.lock` 文件锁，获取超时 5 秒报错。
- **损坏兜底**：解析失败不静默重建，报错并提示可从 `journal.jsonl` 重放恢复。
- **模糊操作**：`done/update/show` 的 id 不存在时报错并列出标题相近的候选任务；脚本与 Agent 都不猜着删改。
- **undo 边界**：journal 为空或最后一条为 `undo` 时报"没有可撤销的操作"。

## 7. 测试策略

- **todo.py 单元测试（pytest）**：CRUD、进度聚合、状态机约束、循环任务生成（含月末边界）、undo 重放、归档、搜索、并发锁、原子写入（模拟中断）。测试数据目录用 `--data-dir` 注入临时目录，绝不触碰真实数据。
- **Skill 行为评估**：用 skill-creator 的 eval 方式覆盖典型场景——"帮我记一下 X"触发 add 且推断合理、"今天要做什么"触发 today 视图、"X 做完了"正确定位任务并 done。
- **测试数据隔离**：测试通过 `--data-dir` 注入临时目录，绝不触碰真实数据。
- 无第三方运行时依赖（Python 标准库），pytest 仅为开发期依赖。
- **跨平台验证**：单元测试需在 Windows 原生 Python 下通过（开发机环境），锁与原子写入逻辑不得依赖 POSIX 专有 API。

## 8. 未来 UI 对接（本期不实现，仅保证就绪）

- UI 读取数据目录（默认 `~/.todo-manager/`）下的 `todos.json` 与 `archive/*.json`，schema 见 `references/schema.md`，以 `schema_version` 做兼容判断。
- UI 若需写入，复用 `todo.py`（可 `todo.py --json` 作为进程级 API），与 Agent 共享同一写入路径、锁与 journal。
- `inferred` 数组供 UI 以弱化样式提示"该值为 AI 推断"。
