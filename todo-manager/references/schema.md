# todo-manager 数据契约（schema v1）

本文档是数据目录的完整格式说明，供 UI/可视化程序或其它工具对接。日常任务操作请使用
`scripts/todo.py`，不需要读本文档。

## 数据目录

解析顺序：`--data-dir` 参数 > `$TODO_MANAGER_HOME` 环境变量 > `~/.todo-manager/`。

```
<data-dir>/
├── todos.json        # 活跃任务（唯一数据源）
├── archive/
│   └── <YYYY>.json   # 已归档任务，按完成年份分文件
├── journal.jsonl     # 追加式操作日志
└── config.json       # 用户偏好
```

跨环境共享（如 Windows 原生 + WSL）：把 `TODO_MANAGER_HOME` 指到同一路径。

## todos.json

```json
{
  "schema_version": 1,
  "next_id": 13,
  "tasks": [ <Task>, ... ]
}
```

- `schema_version`：整数。消费方应校验此值；未来不兼容变更会递增。
- `next_id`：下一个可分配的任务 ID。归档不回收 ID。

### Task 对象

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | 自增，全局唯一（含归档） |
| `title` | string | 标题 |
| `notes` | string | 备注，可为空串 |
| `status` | string | `todo` / `in_progress` / `blocked` / `done` / `cancelled` |
| `priority` | string | `P0`（紧急重要）/ `P1`（重要）/ `P2`（常规，默认）/ `P3`（有空再说） |
| `due` | string\|null | `YYYY-MM-DD`，无截止日期为 null |
| `progress` | int | 0-100。**有子任务时为派生值**：round(已完成子任务数/总数×100)，写入端强制此规则；无子任务时可手动设置；status 变为 done 时置 100 |
| `tags` | string[] | 标签 |
| `estimate_minutes` | int\|null | 预估耗时（分钟） |
| `recurrence` | string\|null | 循环规则，见下文 |
| `inferred` | string[] | 由 AI 推断（而非用户明示）的字段名集合，取值 `title/priority/due/tags/estimate/recurrence`。用户显式修改某字段后会被移出。UI 建议对这些字段用弱化样式提示"AI 推断" |
| `blocked_reason` | string\|null | 仅 status=blocked 时有值 |
| `created_at` / `updated_at` | string | ISO 8601 带时区 |
| `completed_at` | string\|null | 完成时间；重新打开后清空 |
| `subtasks` | Subtask[] | 子任务，仅一层 |

### Subtask 对象

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | `"<父ID>.<序号>"`，如 `"12.1"`，序号从 1 递增不回收 |
| `title` | string | 标题 |
| `status` | string | 仅 `todo` / `done` |
| `due` | string\|null | 可选截止日期 |

### 状态机

- 主流转：`todo → in_progress → done`；任意状态可转 `blocked`（须有 blocked_reason）或 `cancelled`。
- `done`/`cancelled` 可重新打开（转回 todo/in_progress），此时 `completed_at` 清空、进度按子任务重算。
- 任务被置 `done` 时：`completed_at` 写入、`progress=100`、所有未完成子任务一并置 done。

### 循环规则（recurrence）

语法（仅以下四种）：

- `daily` — 每天
- `weekly:mon` … `weekly:sun` — 每周某天
- `monthly:1..31` — 每月某日；日期超出当月天数时取月末
- `every:N:days` — 每 N 天

行为：循环任务完成时，写入端自动生成下一期任务——新 id，`due` 从**完成日**推算（严格晚于完成日），
继承 title/notes/priority/tags/estimate，子任务全部重置为 todo（新 id），`recurrence` 移到新任务上，
原任务的清空。原任务保持 done 留在 todos.json，等 `archive` 时归档。

## archive/<YYYY>.json

```json
{ "schema_version": 1, "tasks": [ <Task>, ... ] }
```

按 `completed_at`（缺失时 `updated_at`）的年份分文件。Task 结构与 todos.json 完全相同。

## journal.jsonl

每行一个 JSON 对象，追加式，记录所有写操作，用于撤销与灾难恢复：

```json
{"ts": "2026-07-05T14:00:00+08:00", "txn": "a3f29c01", "op": "update",
 "task_id": 12, "before": {"priority": "P2"}, "after": {"priority": "P0"}}
```

- `txn`：一次命令调用的分组 ID（一条命令可能产生多条日志，如 done + recur_spawn）。
- `op`：`add` / `update` / `done` / `subtask_add` / `archive` / `recur_spawn` / `undo`。
- `before` / `after`：仅包含变化的字段（add/recur_spawn 的 before 为 null，after 为完整任务）。
- `add`/`recur_spawn` 额外带 `next_id_before`；`archive` 额外带 `moved`（按年份分组的完整任务快照）；
  `undo` 额外带 `undone_txn`。
- `undo` 撤销**最后一个 txn 组**的全部操作；连续 undo 不支持（最后一条已是 undo 时报错）。

## config.json

```json
{ "schema_version": 1, "default_priority": "P2" }
```

## UI 对接建议

- 只读展示：直接读 `todos.json` + `archive/*.json`，先校验 `schema_version`。
- 写入：复用 `scripts/todo.py`（子进程调用，`--json` 拿结构化结果），与 Agent 共享同一套
  校验、文件锁（`todos.json.lock`）与 journal，避免两套写入逻辑分叉。
- 监听变化：watch `todos.json` 的 mtime 即可（原子替换写入，不会读到半截文件）。
