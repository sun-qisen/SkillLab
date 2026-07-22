# serve.py 进程内调用 todo —— 消除每请求 fork 子进程 设计文档

日期：2026-07-23
状态：待用户逐节确认

## 1. 背景与范围

`todo-manager` 的可视化参考服务 `todo-manager/scripts/serve.py`（零依赖 HTTP API）当前把每一次
数据读写都实现为**新起一个 `python todo.py …` 子进程**（`run_todo` 用 `subprocess.run`）。这样做
的初衷是「todo.py 是唯一数据写入路径，serve.py 不直接碰 `todos.json`」，但用「起子进程」来实现
这个目标，代价是每请求都要付一次 Python 解释器冷启动 + import 的开销。

本地实测（同一份数据、快机器）：

| 方式 | 每请求 |
|---|---|
| 现在：fork `python todo.py` 子进程 | ~59 ms |
| 进程内 `import todo` 直接调同一套函数 | ~0.49 ms |

约 120 倍差距，且是纯服务器侧 CPU。部署机（`todo.sunmoo.me`，走 Cloudflare Tunnel）还要跑其它
服务，前端后续请求频率还会上升；fork 进程是 per-request 里最贵的动作之一（fork/exec + 瞬时
十几到几十 MB RSS + 页缓存/调度器压力），会随频率线性增长并与同机其它服务抢 CPU/内存。

**范围（只做这一件事）**：把 `serve.py` 的 `run_todo` 从「fork 子进程 + 解析 stdout JSON」改为
「`import todo` 进程内 `parse_args` + 调 `ns.fn(ns, data_dir)` 拿返回 dict」，消除每请求 fork。

**明确不做**：
- 不改 `todo.py`（其已导出 `build_parser` / `resolve_data_dir` / `TodoError` / 各 `cmd_*`，直接复用）。
- 不改 `webui.html`、不改任何 HTTP 契约（状态码、错误码、消息口径全部保持）。
- 不动 `ReadCache`（进程内已很便宜，是否简化留待以后，本次保持外科手术）。
- 不解决前端「肉眼延迟」——那是 Cloudflare 隧道的网络 RTT，与本次 CPU 开销是两笔账。

## 2. 设计

### 2.1 进程内执行

`serve.py` 顶部 `import todo`（与 `serve.py` 同目录，`sys.path[0]` 已含 scripts/，可直接 import）。
`run_todo(data_dir, cli_args)` 改为：

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

要点：
- **不再传 `--json`**：原子进程实现靠 `--json` 让 todo.py 打印 JSON 再解析；进程内直接拿 `ns.fn`
  返回的 `(result, lines)` 元组，取 `result` 即可，省掉一次序列化/反序列化往返。
- **每请求新建 parser**（`todo.build_parser()`）：argparse 未承诺线程安全，`ThreadingHTTPServer`
  多线程并发，per-call 构造 parser（<1ms）换取完全隔离，代价可忽略。
- `cached_todo` / `ReadCache` / 各 handler **不改**：它们照旧调用 `run_todo`。

### 2.2 错误分类对照（与现子进程实现严格等价）

| 场景 | 现子进程实现（解析 stderr） | 进程内实现（捕获异常） | 对外 |
|---|---|---|---|
| 找不到任务 | stderr 含「找不到任务」 | `TodoError` 消息含「找不到任务」 | 404 task_not_found（回显消息）|
| 写锁超时 | stderr 含「获取写锁超时」 | `TodoError` 消息含「获取写锁超时」 | 409 locked（回显消息）|
| todos.json 损坏 | stderr 含「解析失败」 | `TodoError` 消息含「解析失败」 | 500 internal（脱敏，记日志）|
| 业务校验失败（如 priority=P9）| stderr 以「错误: 」起且非上述 | 其它 `TodoError` | 400 invalid_request（回显消息）|
| 非法子命令/视图参数 | 子进程 exit 2、非「错误: 」前缀 | argparse `SystemExit` | 500 internal（脱敏，记日志）|
| 其它非预期崩溃 | 子进程非 0、非「错误: 」前缀 | 任意 `Exception` | 500 internal（脱敏，记日志）|

### 2.3 并发与线程安全

- **写串行化不变**：`todo.Lock` 是文件锁（`O_CREAT|O_EXCL`），同进程内两个线程竞争时，后者拿到
  `FileExistsError` 并自旋等待，语义与跨进程一致。
- **陈旧锁不误伤同进程并发**：进程内锁文件写的是**服务自身 PID**（存活），新加入的 mtime+PID
  失主检测判定「活主且新鲜」→ 不抢锁、正常等待；只有服务崩溃重启后旧 PID 变死才回收。
- **无进程内共享可变状态**：`todo.py` 无模块级可变缓存；`current_now()` 每次读环境变量；各 `cmd_*`
  只经文件读写交互。读为无锁文件读（与原子进程实现一致）。
- **请求级隔离兜底**：失去的是「子进程崩溃不波及服务」这层进程隔离；由 `handle_one` 既有的
  `try/except ApiError / except Exception` + `run_todo` 内的三层兜底把单请求异常收敛为 500，
  服务不被单个请求打死。

## 3. 影响文件

- 修改：`todo-manager/scripts/serve.py`（顶部 `import todo`；删除不再使用的 `import subprocess`；
  重写 `run_todo`）。其余（handler、路由、ReadCache、auth）不动。
- 修改：`tests/test_serve.py`（新增 3 个测试，见下）。
- 不改：`todo.py`、`webui.html`、`references/*`。

## 4. 验收标准

1. 既有 `tests/test_serve.py`（28 项，含 `test_internal_error_sanitized`、各 404/400/500 映射、
   缓存失效、认证、CORS）与 `tests/test_todo.py` **全绿**——这是行为不变的主要保证。
2. 新增测试（TDD 驱动本次重构意图）：
   - `test_run_todo_runs_in_process`：monkeypatch 全局 `subprocess.run` 为抛错，`serve.run_todo`
     仍能完成一次 add 并返回 dict —— 证明**不再 fork**。重构前必失败（现实现会 fork）。
   - `test_run_todo_maps_missing_to_apierror`：`serve.run_todo(..., ["show","999"])` 抛
     `ApiError(status=404, code="task_not_found")` —— 错误映射回归。
   - `test_bad_view_param_keeps_server_alive`：`GET /api/tasks?view=乱写` 返回错误状态且**服务仍存活**
     （紧接一个正常 `GET` 成功）—— 证明 argparse `SystemExit` 被拦下、没打死服务。
3. 起真实服务 + Playwright 端到端复验：切视图、打开/编辑任务、子任务增删、新建任务无回归。
4. 复测 benchmark，确认进程内单次开销回到亚毫秒级。

## 5. 风险与权衡

- **失进程隔离**：见 2.3，用请求级 500 兜底替代，可接受（服务本就是单用户轻量工具）。
- **argparse `SystemExit`**：`SystemExit` 属 `BaseException`，不会被 `except Exception` 捕获，必须
  显式 `except SystemExit`（已在 2.1 处理）。
- **stderr 噪声**：argparse 出错会往真实 stderr 打 usage；仅发生在极少的非法参数场景，进服务日志，
  可接受（不影响对客户端的脱敏响应）。
