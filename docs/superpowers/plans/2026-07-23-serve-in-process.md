# serve.py 进程内调用 todo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `serve.py` 的数据读写从「每请求 fork `python todo.py` 子进程」改为「`import todo` 进程内调用同一套函数」，消除每请求的解释器冷启动开销，HTTP 契约与行为保持不变。

**Architecture:** 只改 `serve.py` 一个函数 `run_todo`：由 `subprocess.run(... --json ...)` + 解析 stdout，改为 `todo.build_parser().parse_args(...)` + `ns.fn(ns, data_dir)` 直接取返回 dict；错误分类从「解析子进程 stderr」等价迁移为「捕获 `todo.TodoError` / `SystemExit` / `Exception` 按消息映射」。`todo.py`、`webui.html`、`ReadCache`、各 handler 均不动。

**Tech Stack:** Python 3 标准库（零第三方依赖）；pytest（仅开发期），既有 `tests/test_serve.py` 走真 HTTP、`tests/test_todo.py` 走真 CLI。

**Spec:** `docs/superpowers/specs/2026-07-23-serve-in-process-design.md`

## Global Constraints

- `serve.py` / `todo.py` 只用 Python 标准库；跨平台（Windows 原生 Python 必须通过全部测试）。
- 测试一律用 pytest 的 `tmp_path` 临时目录，绝不触碰真实数据目录（`~/.todo-manager`）。
- **HTTP 契约不变**：所有现有状态码、错误码、错误消息口径保持；`tests/test_serve.py` 全 28 项必须继续通过。
- **用户 git 规则（覆盖 skill 默认流程）：任何任务都不执行 `git commit` / `git push`。** 全部完成并验证后向用户汇报，由用户决定是否提交（本次合成一个提交，格式 `[todo-manager] <type>: <单行标题>`）。
- 运行测试命令（仓库根目录 `E:\MyProject\skill-lab`）：
  - `python -m pytest tests/test_serve.py -q`
  - `python -m pytest tests/test_todo.py -q`

---

### Task 1: `run_todo` 进程内化（错误映射 + SystemExit 兜底）

**Files:**
- Modify: `todo-manager/scripts/serve.py`（`import` 区约 13-29 行；`run_todo` 约 51-72 行）
- Modify: `tests/test_serve.py`（顶部加 `import serve` 支持；文件末尾加 3 个测试）

**Interfaces:**
- Consumes（来自 `todo.py`，均已存在）：
  - `todo.build_parser() -> argparse.ArgumentParser`
  - `todo.resolve_data_dir(explicit: str) -> pathlib.Path`
  - `todo.TodoError`（异常类型）
  - `ns.fn(ns, data_dir) -> tuple[dict, list[str]]`（各 `cmd_*`，返回 `(result, lines)`）
- Produces（`serve.py`，签名不变，供既有 handler / cached_todo 调用）：
  - `run_todo(data_dir, cli_args) -> dict`（进程内执行；失败抛 `ApiError`）

- [ ] **Step 1: 写失败测试**

在 `tests/test_serve.py` 顶部 import 段（`import pytest` 之后）加入对 `serve` 模块的直接导入：

```python
sys.path.insert(0, str(SCRIPTS))  # 允许在测试进程内直接 import serve/todo
import serve  # noqa: E402
```

在文件**末尾**追加：

```python
# ---------------------------------------------------------------- 进程内化

def test_run_todo_runs_in_process(monkeypatch, tmp_path):
    """run_todo 必须进程内调用 todo，不再 fork 子进程。"""
    import subprocess as sp
    def boom(*a, **k):
        raise AssertionError("run_todo 不应 fork 子进程，应进程内调用 todo")
    monkeypatch.setattr(sp, "run", boom)
    result = serve.run_todo(tmp_path / "d", ["add", "进程内任务"])
    assert result["task"]["title"] == "进程内任务"


def test_run_todo_maps_missing_to_apierror(tmp_path):
    """业务错误经进程内异常正确映射为 ApiError（找不到任务 → 404）。"""
    serve.run_todo(tmp_path / "d", ["add", "x"])
    with pytest.raises(serve.ApiError) as ei:
        serve.run_todo(tmp_path / "d", ["show", "999"])
    assert ei.value.status == 404 and ei.value.code == "task_not_found"


def test_bad_view_param_keeps_server_alive(server):
    """非法 view 触发 argparse SystemExit，必须被拦为错误响应且服务不死。"""
    status, _, _ = server.req("GET", "/api/tasks?view=乱写")
    assert status >= 400
    status, data, _ = server.req("GET", "/api/tasks?view=all")  # 服务仍存活
    assert status == 200 and "tasks" in data
```

- [ ] **Step 2: 运行新测试，确认 `test_run_todo_runs_in_process` 失败**

Run: `python -m pytest tests/test_serve.py::test_run_todo_runs_in_process -q`
Expected: FAIL —— 现 `run_todo` 调 `subprocess.run` 触发 `boom` 的 `AssertionError`。

- [ ] **Step 3: 重写 `run_todo` 为进程内调用**

在 `serve.py` 顶部 import 区**删除** `import subprocess`（重构后不再使用），并加入 `import todo`（放在标准库 import 之后即可，例如 `import time` 附近）：

```python
import todo
```

把 `run_todo`（现为 `subprocess.run(...)` 版本）整体替换为：

```python
def run_todo(data_dir, cli_args):
    """进程内调用 todo：复用其 argparse 定义与 cmd_* 函数，返回结构化结果 dict。
    保持与原子进程实现相同的错误分类；argparse 失败与非预期异常一律降级为 500。"""
    try:
        ns = todo.build_parser().parse_args([*cli_args, "--data-dir", str(data_dir)])
        result, _lines = ns.fn(ns, todo.resolve_data_dir(ns.data_dir))
        return result
    except todo.TodoError as e:
        msg = str(e)
        if "找不到任务" in msg:
            raise ApiError(404, "task_not_found", msg)
        if "获取写锁超时" in msg:
            raise ApiError(409, "locked", msg)
        if "解析失败" in msg:  # todos.json 损坏属数据/内部错误，不回显原文
            print(f"todo 数据异常: {msg}", flush=True)
            raise ApiError(500, "internal", "服务内部错误，详情见服务日志")
        raise ApiError(400, "invalid_request", msg)  # 其余为业务校验错误
    except SystemExit:  # argparse 遇非法参数会 sys.exit；进程内必须拦下，不能打死服务
        print(f"todo 参数解析失败: {cli_args}", flush=True)
        raise ApiError(500, "internal", "服务内部错误，详情见服务日志")
    except Exception as e:  # 兜底：任何非预期异常都不外泄细节
        print(f"todo 执行异常: {type(e).__name__}: {e}", flush=True)
        raise ApiError(500, "internal", "服务内部错误，详情见服务日志")
```

> 注意：`ApiError` 本身是 `Exception` 子类，但它是在 `except` 块内 `raise` 的，不会被同级
> `except Exception` 再次捕获，会正确向外传播。`SystemExit` 属 `BaseException`，必须显式捕获。

- [ ] **Step 4: 运行 3 个新测试，确认通过**

Run: `python -m pytest tests/test_serve.py::test_run_todo_runs_in_process tests/test_serve.py::test_run_todo_maps_missing_to_apierror tests/test_serve.py::test_bad_view_param_keeps_server_alive -q`
Expected: 3 passed。

- [ ] **Step 5: 运行完整回归套件，确认全绿**

Run: `python -m pytest tests/test_serve.py tests/test_todo.py -q`
Expected: 全部 passed（`test_serve.py` 原 28 项 + 新 3 项；`test_todo.py` 原有项）。特别确认
`test_internal_error_sanitized`（损坏 todos.json → 500 脱敏）、各 404/400 映射、缓存失效项通过。

- [ ] **Step 6: 不提交**（遵守 Global Constraints 的 git 规则，停在工作区）

---

### Task 2: 端到端 & 性能复验（无自动化断言，人工验证）

**Files:** 无代码改动；起真实服务 + 浏览器 + benchmark。

**Interfaces:** 无。

- [ ] **Step 1: 起服务并做功能端到端复验**

用临时数据目录起 `serve.py`，浏览器（或 Playwright）走一遍：切视图（概览/今天/本周/阻塞/全部）、
新建带标签任务（确认无垃圾子任务）、打开编辑、编辑弹窗删除子任务 + 新增子任务、完成任务/子任务。
Expected: 全部功能与重构前一致，无报错。

- [ ] **Step 2: 复测 per-request 开销**

以进程内方式对 `list` 连续调用 20 次取均值。
Expected: 亚毫秒级（对照 spec §1 的 ~0.49ms），远低于原子进程方式的数十毫秒。

- [ ] **Step 3: 向用户汇报，提议单个 commit message**

Expected: 汇报改动与验证结果，提议如 `[todo-manager] perf: run todo in-process instead of forking a subprocess per request`，等用户决定是否提交。

## Self-Review

- **Spec coverage**：§2.1 进程内执行 → Task 1 Step 3；§2.2 错误分类 → Task 1 Step 3 的映射 + Step 5 回归（`test_internal_error_sanitized` 覆盖 500/解析失败，既有 404/400 测试覆盖映射）；§2.3 并发/线程安全 → 每请求新建 parser（Step 3）+ 既有认证/缓存测试；§4 验收 → Task 1 Step 4-5 + Task 2。
- **Placeholder scan**：无 TODO/占位；每个代码步骤给出完整代码与确切命令、预期输出。
- **Type consistency**：`run_todo(data_dir, cli_args) -> dict`、`ApiError(status, code, message)`、
  `ns.fn(ns, data_dir) -> (result, lines)` 在 spec 与各任务中一致；测试引用的 `serve.run_todo` /
  `serve.ApiError` 与实现一致。
