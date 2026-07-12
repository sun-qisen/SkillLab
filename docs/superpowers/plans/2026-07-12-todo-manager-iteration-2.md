# todo-manager 迭代 2（四项使用问题修复）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 todo-manager 实际使用中发现的四个问题：逻辑日（凌晨 4 点日界线）、list 默认排除已完成、循环任务 next_due 锚定原 due、SKILL.md 增加行程安排建议提示词。

**Architecture:** 全部改动落在现有三个文件 + 测试：`todo.py` 引入可注入时间源（`$TODO_MANAGER_NOW`）与 `logical_today()` 并替换所有 `date.today()` 调用点；`pick_view` 的 all 视图改为仅活跃任务；`finalize_done` 的循环推算改为以原 due 为锚点逐期推进。SKILL.md / schema.md 同步行为口径。

**Tech Stack:** Python 3 标准库（零第三方依赖）；pytest（仅开发期）通过 subprocess 跑真实 CLI。

**Spec:** `docs/superpowers/specs/2026-07-12-todo-manager-iteration-2-design.md`

## Global Constraints

- `todo.py` 只用 Python 标准库；跨平台（Windows 原生 Python 必须通过全部测试）。
- 测试一律走 subprocess + `--data-dir` 临时目录，绝不触碰真实数据目录。
- 数据时间戳（created_at / updated_at / completed_at / journal ts）保持**真实时间**语义（可被 `$TODO_MANAGER_NOW` 冻结）；逻辑日只影响"哪一天"的判断与渲染。
- **用户 git 规则（覆盖 skill 默认流程）：任何任务都不执行 `git commit` / `git push`。** 全部任务完成并验证后向用户汇报，由用户决定是否提交（整个迭代合成一个提交，格式 `[todo-manager] <type>: <单行标题>`）。
- 运行测试命令（仓库根目录）：`python -m pytest tests/test_todo.py -v`

---

### Task 1: 可注入时间源（current_now / $TODO_MANAGER_NOW）+ 测试基建

**Files:**
- Modify: `todo-manager/scripts/todo.py`（常量区 + `now_iso()`，约 21-40 行）
- Modify: `tests/test_todo.py`（`run/ok/fail/runj` helpers，15-36 行）

**Interfaces:**
- Consumes: 无（首个任务）。
- Produces:
  - `todo.py` 内 `current_now() -> datetime`（带时区的"当前时刻"，环境变量 `TODO_MANAGER_NOW`（ISO 8601）可冻结，非法值静默回退真实时间）；
  - `DAY_CUTOFF_HOUR = 4` 常量；
  - 测试 helpers `run/ok/fail/runj` 均新增关键字参数 `now=None`（默认注入 `f"{TODAY.isoformat()}T12:00:00"`），后续任务的测试直接用 `ok(tmp, ..., now="2026-07-11T15:00:00")` 形式冻结时间。

- [ ] **Step 1: 写失败测试**

在 `tests/test_todo.py` 的 `read_db` 之后新增分节与测试：

```python
# ---------------------------------------------------------------- 时间源

def test_frozen_time_source(tmp_path):
    ok(tmp_path, "add", "冻结时间", now="2026-03-01T10:00:00")
    t = read_db(tmp_path)["tasks"][0]
    assert t["created_at"].startswith("2026-03-01T10:00:00")
```

同时改造 helpers（`run` 增加 `now` 关键字参数并注入环境变量，`ok/fail/runj` 透传）：

```python
def run(tmp, *args, now=None):
    env = dict(os.environ)
    env.pop("TODO_MANAGER_HOME", None)
    env["TODO_MANAGER_NOW"] = now or f"{TODAY.isoformat()}T12:00:00"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--data-dir", str(tmp)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)


def ok(tmp, *args, now=None):
    r = run(tmp, *args, now=now)
    assert r.returncode == 0, f"命令失败: {args}\nstderr: {r.stderr}"
    return r


def fail(tmp, *args, now=None):
    r = run(tmp, *args, now=now)
    assert r.returncode != 0, f"预期失败但成功了: {args}\nstdout: {r.stdout}"
    return r


def runj(tmp, *args, now=None):
    return json.loads(ok(tmp, *args, "--json", now=now).stdout)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_todo.py::test_frozen_time_source -v`
Expected: FAIL（`created_at` 是真实当前时间，不是 2026-03-01）

- [ ] **Step 3: 最小实现**

`todo-manager/scripts/todo.py`：在常量区（`STATUS_ICON` 之后）加 `DAY_CUTOFF_HOUR`，并把 `now_iso()` 改为从 `current_now()` 推导：

```python
DAY_CUTOFF_HOUR = 4  # 凌晨 4 点前算前一天（逻辑日）
```

```python
def current_now():
    """当前本地时间。$TODO_MANAGER_NOW (ISO 8601) 可冻结时间，仅供测试/调试。"""
    raw = os.environ.get("TODO_MANAGER_NOW")
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.astimezone()
        except ValueError:
            pass  # 非法值忽略，回退真实时间
    return datetime.now().astimezone()


def now_iso():
    return current_now().isoformat(timespec="seconds")
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_todo.py -v`
Expected: 全部 PASS（31 旧 + 1 新）

---

### Task 2: logical_today() 并替换全部 date.today() 调用点

**Files:**
- Modify: `todo-manager/scripts/todo.py`（`current_now` 之后加 helper；`fmt_due`、`is_overdue`、`pick_view`、`cmd_list`、`cmd_report` 内的 `date.today()`）
- Test: `tests/test_todo.py`

**Interfaces:**
- Consumes: Task 1 的 `current_now()`、`DAY_CUTOFF_HOUR`、测试 `now=` 参数。
- Produces: `logical_day(dt: datetime) -> date` 与 `logical_today() -> date`；此后**所有"哪一天"判断**（today/week 视图、过期、"今天/明天"渲染、report 周界）都走逻辑日。Task 4 的循环推算依赖 `logical_today()`。

- [ ] **Step 1: 写失败测试**

新增分节（**用固定绝对日期而非 TODAY 相对日期**——冻结日期远离真实运行日，旧代码按真实自然日必然失败，且测试结果不随运行时刻漂移）：

```python
# ---------------------------------------------------------------- 逻辑日（4 点日界线）

def test_logical_day_before_cutoff(tmp_path):
    ok(tmp_path, "add", "今晚到期", "--due", "2026-09-10")
    at = "2026-09-11T02:30:00"  # 次日凌晨 2:30，逻辑日仍是 09-10
    tasks = runj(tmp_path, "list", "--view", "today", now=at)["tasks"]
    assert [t["id"] for t in tasks] == [1]  # 仍算"今日到期"，且未过期
    r = ok(tmp_path, "show", "1", now=at)
    assert "今天" in r.stdout and "过期" not in r.stdout


def test_logical_day_at_cutoff(tmp_path):
    ok(tmp_path, "add", "今晚到期", "--due", "2026-09-10")
    r = ok(tmp_path, "show", "1", now="2026-09-11T04:00:00")  # 4 点整，新的一天
    assert "过期 1 天" in r.stdout


def test_logical_day_tomorrow_rendering(tmp_path):
    ok(tmp_path, "add", "明天到期", "--due", "2026-09-11")
    r = ok(tmp_path, "show", "1", now="2026-09-11T01:00:00")  # 凌晨 1 点
    assert "明天" in r.stdout  # "明天" = 09-11 当前自然日，而非 09-12


def test_report_counts_late_night_completion(tmp_path):
    mon_1am = "2026-07-13T01:00:00"  # 周一凌晨 1 点 = 逻辑日周日 07-12
    ok(tmp_path, "add", "深夜完成", now="2026-07-12T20:00:00")
    ok(tmp_path, "done", "1", now=mon_1am)
    rep = runj(tmp_path, "report", now=mon_1am)
    assert len(rep["completed_this_week"]) == 1  # 算进 07-06 起的那一周
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_todo.py -k "logical_day or late_night" -v`
Expected: 4 条全部 FAIL——旧代码用真实自然日，与冻结的 2026-09/07 月日期对不上（today 视图为空、渲染为"截止 09-10"而非"今天"、report 计数为 0）。

- [ ] **Step 3: 实现**

`todo.py` 在 `current_now()` 之后加：

```python
def logical_day(dt):
    """把一个时刻映射到逻辑日：凌晨 DAY_CUTOFF_HOUR 点前算前一天。"""
    return (dt - timedelta(hours=DAY_CUTOFF_HOUR)).date()


def logical_today():
    return logical_day(current_now())
```

替换调用点（共 5 处函数）：

```python
def fmt_due(due_str):
    if not due_str:
        return ""
    d = parse_date(due_str)
    delta = (d - logical_today()).days   # 原 date.today()
    ...


def is_overdue(t):
    return bool(t.get("due")) and parse_date(t["due"]) < logical_today() \
        and t["status"] not in ("done", "cancelled")
```

`pick_view` 中 today/week 分支：

```python
    if view == "today":
        return [t for t in active
                if is_overdue(t)
                or (t.get("due") and parse_date(t["due"]) == logical_today())
                or t["status"] == "in_progress"]
    if view == "week":
        today = logical_today()
        week_end = today + timedelta(days=6 - today.weekday())
        ...
```

`cmd_list` 头部：`f"📋 {header} ({logical_today().isoformat()}) · ..."`。

`cmd_report`：

```python
    week_start = logical_today() - timedelta(days=logical_today().weekday())

    def completed_this_week(t):
        if not t.get("completed_at"):
            return False
        d = logical_day(datetime.fromisoformat(t["completed_at"]))
        return week_start <= d <= logical_today()
```

（`fmt_due` 被 `render_line`/`render_detail`/子任务渲染复用，改一处全覆盖。`finalize_done` 里的 `date.today()` 留给 Task 4 一并改。）

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_todo.py -v`
Expected: 全部 PASS（默认注入正午时刻，旧测试不受日界线影响）

---

### Task 3: list 默认排除已完成（--include-done）

**Files:**
- Modify: `todo-manager/scripts/todo.py`（`pick_view`、`cmd_list`、`build_parser` 的 list 子命令）
- Test: `tests/test_todo.py`

**Interfaces:**
- Consumes: 无新依赖。
- Produces: `pick_view(db, view, include_all=False)`（第三个位置参数）；CLI 新旗标 `--include-done`（dest `include_done`）。`--view all` 语义 = 全部**活跃**任务；`today/week/blocked` 行为不变。

- [ ] **Step 1: 写失败测试**

```python
def test_list_all_excludes_done_by_default(tmp_path):
    ok(tmp_path, "add", "做完的")
    ok(tmp_path, "add", "还没做")
    ok(tmp_path, "done", "1")
    assert [t["id"] for t in runj(tmp_path, "list")["tasks"]] == [2]
    ids = {t["id"] for t in runj(tmp_path, "list", "--include-done")["tasks"]}
    assert ids == {1, 2}


def test_list_status_done_auto_includes(tmp_path):
    ok(tmp_path, "add", "做完的")
    ok(tmp_path, "done", "1")
    assert [t["id"] for t in runj(tmp_path, "list", "--status", "done")["tasks"]] == [1]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_todo.py -k "excludes_done or auto_includes" -v`
Expected: 第一条 FAIL（默认带出 #1）；第二条此时 PASS（旧语义本就包含），作回归保护。

- [ ] **Step 3: 实现**

```python
def pick_view(db, view, include_all=False):
    tasks = db["tasks"]
    active = [t for t in tasks if t["status"] not in ("done", "cancelled")]
    if view == "today":
        ...  # 不变
    if view == "week":
        ...  # 不变
    if view == "blocked":
        return [t for t in active if t["status"] == "blocked"]
    return list(tasks) if include_all else list(active)
```

`cmd_list` 开头：

```python
    db = load_db(data_dir)
    include_all = ns.include_done or ns.status in ("done", "cancelled")
    sel = pick_view(db, ns.view, include_all)
```

`build_parser` 的 list 子命令加：

```python
    l.add_argument("--include-done", action="store_true",
                   help="all 视图包含已完成/已取消任务")
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_todo.py -v`
Expected: 全部 PASS

---

### Task 4: 循环任务 next_due 以原 due 为锚点、逐期推进到未来

**Files:**
- Modify: `todo-manager/scripts/todo.py`（`next_due` docstring、`finalize_done`）
- Test: `tests/test_todo.py`

**Interfaces:**
- Consumes: Task 2 的 `logical_today()`。
- Produces: `finalize_done` 生成的下一期 `due` = 以原任务 due 为锚（无 due 则逻辑日今天）按规则推算，`while nd <= logical_today()` 逐期推进。`next_due(rule, from_date)` 本身语义不变（严格晚于 from_date）。

- [ ] **Step 1: 写失败测试**

（2026-07-11 是周六、07-12 周日、07-13 周一）

```python
def test_recurrence_early_completion_keeps_anchor(tmp_path):
    sat = "2026-07-11T15:00:00"
    ok(tmp_path, "add", "周日回顾", "--recurrence", "weekly:sun",
       "--due", "2026-07-12", now=sat)
    ok(tmp_path, "done", "1", now=sat)  # 周六提前完成周日截止的任务
    new = next(t for t in read_db(tmp_path)["tasks"] if t["id"] == 2)
    assert new["due"] == "2026-07-19"  # 下一期仍是下周日，而不是"明天"


def test_recurrence_late_completion_rolls_forward(tmp_path):
    ok(tmp_path, "add", "浇花", "--recurrence", "every:3:days",
       "--due", "2026-07-01", now="2026-07-01T09:00:00")
    ok(tmp_path, "done", "1", now="2026-07-12T15:00:00")  # 拖到 7/12 才完成
    new = next(t for t in read_db(tmp_path)["tasks"] if t["id"] == 2)
    assert new["due"] == "2026-07-13"  # 保持 4/7/10/13 节拍，且不生成即过期


def test_recurrence_no_due_falls_back_to_completion_day(tmp_path):
    sat = "2026-07-11T15:00:00"
    ok(tmp_path, "add", "无日期周报", "--recurrence", "weekly:mon", now=sat)
    ok(tmp_path, "done", "1", now=sat)
    new = next(t for t in read_db(tmp_path)["tasks"] if t["id"] == 2)
    assert new["due"] == "2026-07-13"  # 从完成日（周六）推算下一个周一
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_todo.py -k "keeps_anchor or rolls_forward or falls_back" -v`
Expected: 前两条 FAIL（现逻辑从完成日推算：分别得 07-12 与 07-15）；第三条 PASS（回退路径本就如此），作回归保护。

- [ ] **Step 3: 实现**

`next_due` docstring 改为：`"""按规则推算下一期截止日，严格晚于 from_date。"""`

`finalize_done` 中 `if rule:` 分支，生成 spawned 之前计算 `nd`，并把 `"due": next_due(rule, date.today()).isoformat(),` 换成 `"due": nd.isoformat(),`：

```python
    spawned = None
    if rule:
        anchor = parse_date(t["due"]) if t.get("due") else logical_today()
        nd = next_due(rule, anchor)
        while nd <= logical_today():  # 拖延补做：逐期推进，保持原节拍
            nd = next_due(rule, nd)
        nid_before = db["next_id"]
        spawned = {
            ...
            "due": nd.isoformat(),
            ...
        }
```

（`finalize_done` 不修改 `t["due"]`，锚点在函数内任意位置读取都安全；实现时把 `anchor/nd` 计算放在构造 `spawned` 字典之前即可。）

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_todo.py -v`
Expected: 全部 PASS（旧循环测试均为无 due 任务，走回退路径，结果不变）

---

### Task 5: SKILL.md（排程建议 + 日界线 + 视图说明）与 schema.md 同步

**Files:**
- Modify: `todo-manager/SKILL.md`
- Modify: `todo-manager/references/schema.md`

**Interfaces:**
- Consumes: Task 3 的 `--include-done` 旗标名、Task 4 的推算行为、Task 2 的日界线语义（文档必须与实现一致）。
- Produces: 无代码接口；Agent 行为提示词。

- [ ] **Step 1: SKILL.md — 推断规则处加入日界线说明**

在"### 1. 记录任务"的推断规则表格之后、`--inferred` 段落之前插入：

```markdown
> **日界线**：凌晨 0:00–4:00 属于"前一天"。这段时间里用户说的"今天/明天/后天/周 X"按前
> 一天的语境换算（如 7/12 凌晨 1 点说"明天"= 7/12，而不是 7/13）；CLI 的今天/过期判断
> 同样按 4 点日界线，两边口径一致。
```

- [ ] **Step 2: SKILL.md — 更新查询视图说明**

"### 2. 查询"代码块中 `--view all` 行改为：

```
todo.py list --view all       # 全部活跃任务（--include-done 连已完成/已取消一起看）
```

- [ ] **Step 3: SKILL.md — 新增行程安排建议小节并重排节号**

在"### 2. 查询"之后插入新小节，原"### 3. 更新与完成"、"### 4. 回顾与维护"顺延为 4、5：

```markdown
### 3. 行程安排建议（"今天怎么安排"、"先做哪个"、"帮我排一下"）

仅在用户发出这类**排程请求**时进入；普通"今天要做什么"只列清单，不强行附带建议。

先 `list --view today --json` 拿任务，结合优先级、截止日期、预估耗时给出建议顺序。

**并行分析三原则**：

1. 真并行 = 某个任务存在**固有等待间隙**（浸泡、下载、等回复、机器自行运行）时，把另一
   个任务穿插进间隙——如"泡鞋的 20 分钟里清理扫地机器人"；
2. 需要连续专注操作的任务是串行的，不要建议同时进行；
3. 互相独立 ≠ 可并行——没有真实的穿插机会就不硬凑，直接给串行顺序。

**输出**：建议顺序 + 穿插点（若有）+ 一句话理由，不长篇大论。
```

- [ ] **Step 4: schema.md — 循环行为与日期口径**

"### 循环规则（recurrence）"的"行为："段替换为：

```markdown
行为：循环任务完成时，写入端自动生成下一期任务——新 id，`due` 以**原任务的 due 为锚点**
按规则推算（原任务无 due 时从完成日的逻辑日推算）；结果不晚于完成日（逻辑日）时逐期推进，
保证严格晚于完成日且保持原节拍。继承 title/notes/priority/tags/estimate，子任务全部重置为
todo（新 id），`recurrence` 移到新任务上，原任务的清空。原任务保持 done 留在 todos.json，
等 `archive` 时归档。
```

"## UI 对接建议"之前新增：

```markdown
## 日期口径（逻辑日）

CLI 的"今天 / 过期 / 本周"判断使用**逻辑日**：凌晨 0:00–3:59 属于前一天，4:00 起算新一天
（`DAY_CUTOFF_HOUR = 4`）。数据中的时间戳仍是真实时间；UI 若要与 CLI 判断一致（如高亮
"今天到期"），应采用同一口径。环境变量 `TODO_MANAGER_NOW`（ISO 8601）可冻结 CLI 的
"当前时间"，仅供测试。
```

- [ ] **Step 5: 一致性验证 + 全量回归**

Run: `python todo-manager/scripts/todo.py list --help`
Expected: 帮助文本含 `--include-done`，与 SKILL.md 描述一致。

Run: `python -m pytest tests/test_todo.py -v`
Expected: 全部 PASS（文档任务零代码变更，此步为收尾门禁）。

完成后向用户汇报四项修复的验收结果（对照 spec 第 7 节验收标准），**不执行 git commit**，等用户指令后把整个迭代合成一个提交（`[todo-manager] <type>: <单行标题>`）。
