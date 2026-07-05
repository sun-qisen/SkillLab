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
| 标签 | 按内容归类（如 工作/生活/学习）。先 `list --view all --json` 看已有标签，**只复用已存在的标签**；确需新标签时在汇报中说明 |
| 子任务 | 一句话包含多个动作（"订机票和酒店"）时拆成子任务，用多个 `--subtask` 传入 |

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
todo.py list --view all       # 全部（含待归档的已完成项）
```

直接把输出转述给用户即可，不要逐字段复读 JSON。用户问模糊问题（"那个客户的事怎么样了"）时先 `search`。

### 3. 更新与完成（"X 做完了"、"X 先搁置"）

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

### 4. 回顾与维护

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

## 错误与恢复

- 脚本报"找不到任务"时会列出最近的活跃任务——对照后和用户确认，不要对错误的 ID 强行操作。
- 报"写锁超时"说明有并发写入，稍候重试一次；仍失败再按错误提示处理。
- 报"todos.json 解析失败"时**不要重建文件**，告知用户可从 `journal.jsonl` 恢复，协助人工重放。

## 参考

- 数据文件格式、字段约束、journal 结构：读 `references/schema.md`（给 UI/程序对接用，日常操作不需要）。
