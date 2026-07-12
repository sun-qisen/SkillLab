# todo-manager 可视化参考服务 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 todo-manager 技能实现零依赖可视化参考服务：serve.py（HTTP API + 密码认证 + CORS）+ webui.html（内置单文件页面）+ api.md（HTTP 契约）+ SKILL.md 部署工作流。

**Architecture:** serve.py 用纯标准库 ThreadingHTTPServer 实现，一切数据读写翻译为 `python todo.py <子命令> --json` 子进程调用（薄封装，唯一写入路径），读端点以 todos.json 的 mtime 为失效信号做内存缓存。认证为管理员密码（PBKDF2 哈希存 serve.json）+ 无状态 HMAC 会话 cookie。设计依据：`docs/superpowers/specs/2026-07-12-todo-manager-visualization-design.md`（下称"设计文档"）。

**Tech Stack:** Python 3 标准库（http.server / subprocess / hashlib / hmac / secrets）、原生 HTML/CSS/JS 单文件页面、pytest（仅开发期）。

## Global Constraints

- **运行时零第三方依赖**：serve.py 与 webui.html 只用 Python 标准库 / 原生浏览器能力，不 pip install 任何东西，不引任何 CDN 资源。
- **todo.py 一行不改**（`todo-manager/scripts/todo.py`）。
- 开发机是 Windows：命令用 `python`（不是 python3）；一切文件读写显式 `encoding="utf-8"`；给 todo.py 的子进程环境加 `PYTHONIOENCODING=utf-8`。
- 测试命令统一：`python -m pytest tests/test_serve.py -v`（在仓库根 `E:\MyProject\skill-lab` 执行）。
- **不执行 `git commit` / `git push`**：用户全局规则要求只在用户明确要求时提交，且同一需求合成一个提交。本计划所有任务均不含提交步骤；全部完成后由用户决定提交。
- 面向用户的文案（错误信息、页面文字、文档）用中文。

---

### Task 1: 测试基架 + serve.py 骨架（/api/status、静态分发、日志、错误封装）

**Files:**
- Create: `todo-manager/scripts/serve.py`
- Create: `tests/test_serve.py`

**Interfaces:**
- Produces（后续所有任务依赖）:
  - serve.py 模块级：`VERSION: str`、`CONTRACT_VERSION: int`、`SCRIPT_DIR/TODO_PY/WEBUI: Path`、`class ApiError(Exception)`（属性 `status:int, code:str, message:str`）、`resolve_data_dir(explicit) -> Path`、`ROUTES: list[tuple[method, regex, handler_name, needs_auth]]`、`build_parser()`、`main(argv=None) -> int`
  - Handler 方法：`send_json(status, obj, extra_headers=())`、`send_error_json(status, code, message)`、`is_authenticated() -> bool`（本任务为恒 True 的桩，Task 5 替换）、`h_index`、`h_status`
  - 测试侧：`free_port()`、`class Server`（属性 `base/port/proc`，方法 `req(method, path, body=None, headers=None) -> (status:int, data:dict, headers)`、`req_raw(method, path, headers=None) -> (status, text, headers)`、`stop()`）、fixture `server`（无密码、临时数据目录）、`todo(data_dir, *args) -> dict`（直接调 todo.py 造数据）

- [ ] **Step 1: 写测试基架与首批失败测试**

创建 `tests/test_serve.py`：

```python
# -*- coding: utf-8 -*-
"""serve.py 端到端测试：起真进程、发真 HTTP，穿透 HTTP→子进程→todo.py→文件 全链路。"""
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "todo-manager" / "scripts"
SERVE = SCRIPTS / "serve.py"
TODO = SCRIPTS / "todo.py"
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def todo(data_dir, *args):
    """直接调 todo.py 造种子数据。"""
    r = subprocess.run(
        [sys.executable, str(TODO), *args, "--json", "--data-dir", str(data_dir)],
        capture_output=True, text=True, encoding="utf-8", env=ENV)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


class Server:
    def __init__(self, data_dir, args=()):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVE), "--port", str(self.port),
             "--data-dir", str(data_dir), *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", env=ENV)
        deadline = time.time() + 10
        while True:
            try:
                self.req("GET", "/api/status")
                return
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError("serve.py 提前退出:\n" + self.proc.stdout.read())
                if time.time() > deadline:
                    self.stop()
                    raise RuntimeError("serve.py 未在 10s 内就绪")
                time.sleep(0.05)

    def req(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        r = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers

    def req_raw(self, method, path, headers=None):
        r = urllib.request.Request(self.base + path, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8"), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8"), e.headers

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def server(tmp_path):
    s = Server(tmp_path / "data")
    yield s
    s.stop()


# ---------------------------------------------------------------- Task 1

def test_status_endpoint(server):
    status, data, _ = server.req("GET", "/api/status")
    assert status == 200
    assert data["contract_version"] == 1
    assert data["auth_required"] is False
    assert data["authenticated"] is True
    assert "version" in data


def test_index_serves_html(server):
    status, text, headers = server.req_raw("GET", "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")


def test_unknown_path_404_json(server):
    status, data, _ = server.req("GET", "/api/nope")
    assert status == 404
    assert data["error"]["code"] == "not_found"


def test_wrong_method_405(server):
    status, data, _ = server.req("PATCH", "/api/status")
    assert status == 405
    assert data["error"]["code"] == "method_not_allowed"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_serve.py -v`
Expected: 4 个测试全部 FAIL/ERROR（serve.py 不存在，Server 启动即 RuntimeError）。

- [ ] **Step 3: 写 serve.py 骨架**

创建 `todo-manager/scripts/serve.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""todo-manager 可视化参考服务 —— 零依赖 HTTP API + 密码认证 + CORS + 内置页面。

用法:
  python serve.py [--host 127.0.0.1] [--port 3007] [--data-dir 路径] [--cors-origin 地址 ...]
  python serve.py auth set [--password-stdin] [--data-dir 路径]
  python serve.py auth unset [--data-dir 路径]

HTTP 契约见 references/api.md；数据读写全部经同目录 todo.py 子进程（唯一写入路径）。
仅使用 Python 标准库。
"""
import argparse
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "1.0"
CONTRACT_VERSION = 1
SCRIPT_DIR = Path(__file__).resolve().parent
TODO_PY = SCRIPT_DIR / "todo.py"
WEBUI = SCRIPT_DIR / "webui.html"


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def resolve_data_dir(explicit):
    """与 todo.py 相同的解析顺序: 参数 > $TODO_MANAGER_HOME > ~/.todo-manager"""
    if explicit:
        return Path(explicit)
    if os.environ.get("TODO_MANAGER_HOME"):
        return Path(os.environ["TODO_MANAGER_HOME"])
    return Path.home() / ".todo-manager"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "todo-serve/" + VERSION

    # ---- 基础设施
    def log_message(self, fmt, *args):
        print(f"{self.log_date_time_string()} {fmt % args}", flush=True)

    def send_json(self, status, obj, extra_headers=()):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, code, message):
        self.send_json(status, {"error": {"code": code, "message": message}})

    def is_authenticated(self):
        return True  # Task 5 实现真实校验；未设密码时恒为已认证

    # ---- 分发
    def handle_one(self, method):
        url = urlparse(self.path)
        self.query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if method == "GET" and url.path == "/":
                return self.h_index()
            for m, pattern, name, needs_auth in ROUTES:
                match = pattern.match(url.path)
                if match and m == method:
                    if needs_auth and not self.is_authenticated():
                        raise ApiError(401, "unauthorized", "需要登录或提供密码凭证")
                    return getattr(self, name)(**match.groupdict())
            if any(p.match(url.path) for _, p, _, _ in ROUTES):
                return self.send_error_json(405, "method_not_allowed",
                                            f"{url.path} 不支持 {method}")
            return self.send_error_json(404, "not_found", f"没有这个路径: {url.path}")
        except ApiError as e:
            return self.send_error_json(e.status, e.code, e.message)
        except Exception as e:  # 单个请求的异常不打死服务
            return self.send_error_json(500, "internal", f"{type(e).__name__}: {e}")

    def do_GET(self):
        self.handle_one("GET")

    def do_POST(self):
        self.handle_one("POST")

    def do_PATCH(self):
        self.handle_one("PATCH")

    # ---- 端点
    def h_index(self):
        if WEBUI.exists():
            body = WEBUI.read_bytes()
        else:
            body = "<h1>todo-manager</h1><p>webui.html 缺失</p>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def h_status(self):
        self.send_json(200, {"version": VERSION, "contract_version": CONTRACT_VERSION,
                             "auth_required": False, "authenticated": True})


# (method, 路径正则, Handler 方法名, 是否需要认证)
ROUTES = [
    ("GET", re.compile(r"^/api/status$"), "h_status", False),
]


def build_parser():
    p = argparse.ArgumentParser(description="todo-manager 可视化参考服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=3007)
    p.add_argument("--data-dir", help="数据目录（默认 $TODO_MANAGER_HOME 或 ~/.todo-manager）")
    p.add_argument("--cors-origin", action="append", default=[],
                   help="允许跨域的前端地址，可重复；默认不发 CORS 头（仅同源）")
    return p


def main(argv=None):
    ns = build_parser().parse_args(argv)
    data_dir = resolve_data_dir(ns.data_dir)
    server = ThreadingHTTPServer((ns.host, ns.port), Handler)
    server.data_dir = data_dir
    server.cors_origins = ns.cors_origin
    print(f"todo-manager 可视化服务: http://{ns.host}:{ns.port}  数据目录: {data_dir}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_serve.py -v`
Expected: 4 passed。（webui.html 还不存在，`h_index` 走缺失占位分支，仍返回 text/html。）

---

### Task 2: CLI 桥 + 全部读端点 + mtime 缓存

**Files:**
- Modify: `todo-manager/scripts/serve.py`
- Test: `tests/test_serve.py`

**Interfaces:**
- Consumes: Task 1 的 `ApiError`、`ROUTES`、`Handler.send_json`、测试基架。
- Produces:
  - `run_todo(data_dir, cli_args: list[str]) -> dict`——成功返回 todo.py 的 `--json` 输出；失败按 stderr 内容抛 `ApiError`（404/409/400）。**Task 3 的写端点直接复用。**
  - `cached_todo(data_dir, cli_args) -> dict`——带 mtime 缓存的 run_todo，仅读端点使用。
  - `CACHE: ReadCache`、`logical_today_str() -> str`
  - Handler 方法 `h_list/h_show/h_search/h_report`；ROUTES 增 4 条 GET。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_serve.py`：

```python
# ---------------------------------------------------------------- Task 2

@pytest.fixture
def seeded(tmp_path):
    """预置 2 个任务后再起服务。"""
    data_dir = tmp_path / "data"
    todo(data_dir, "add", "写周报", "--priority", "P1", "--due", "2026-07-17",
         "--tags", "工作", "--subtask", "收集数据", "--subtask", "成文")
    todo(data_dir, "add", "买猫粮", "--priority", "P3")
    s = Server(data_dir)
    yield s, data_dir
    s.stop()


def test_list_all(seeded):
    s, _ = seeded
    status, data, _ = s.req("GET", "/api/tasks?view=all")
    assert status == 200
    assert data["count"] == 2
    titles = {t["title"] for t in data["tasks"]}
    assert titles == {"写周报", "买猫粮"}


def test_list_filters_passthrough(seeded):
    s, _ = seeded
    status, data, _ = s.req("GET", "/api/tasks?view=all&tag=工作")
    assert status == 200
    assert data["count"] == 1 and data["tasks"][0]["title"] == "写周报"


def test_show_task(seeded):
    s, _ = seeded
    status, data, _ = s.req("GET", "/api/tasks/1")
    assert status == 200
    assert data["task"]["id"] == 1
    assert len(data["task"]["subtasks"]) == 2


def test_show_missing_task_404(seeded):
    s, _ = seeded
    status, data, _ = s.req("GET", "/api/tasks/999")
    assert status == 404
    assert data["error"]["code"] == "task_not_found"
    assert "找不到任务" in data["error"]["message"]


def test_search(seeded):
    s, _ = seeded
    status, data, _ = s.req("GET", "/api/search?q=%E5%91%A8%E6%8A%A5")  # "周报"
    assert status == 200
    assert len(data["matches"]) == 1


def test_search_requires_q(seeded):
    s, _ = seeded
    status, data, _ = s.req("GET", "/api/search")
    assert status == 400


def test_report(seeded):
    s, _ = seeded
    status, data, _ = s.req("GET", "/api/report")
    assert status == 200
    assert data["active"] == 2
    assert set(data["by_priority"]) == {"P0", "P1", "P2", "P3"}


def test_cache_invalidated_by_external_write(seeded):
    """外部（CLI）写入改变 todos.json mtime 后，GET 必须拿到新数据。"""
    s, data_dir = seeded
    _, d1, _ = s.req("GET", "/api/tasks?view=all")
    assert d1["count"] == 2
    time.sleep(0.02)  # 保证 mtime 变化可观测
    todo(data_dir, "add", "第三个任务")
    _, d2, _ = s.req("GET", "/api/tasks?view=all")
    assert d2["count"] == 3
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_serve.py -v -k "list or show or search or report or cache"`
Expected: 新增测试全部 FAIL（404 not_found：路由不存在）。

- [ ] **Step 3: 实现 CLI 桥、缓存与读端点**

在 serve.py 顶部 import 区加入 `subprocess`、`threading` 和 `from datetime import datetime, timedelta`。在 `ApiError` 定义之后加：

```python
DAY_CUTOFF_HOUR = 4  # 与 todo.py 逻辑日口径一致；仅用于缓存键，业务判断始终在 todo.py 内


def logical_today_str():
    return (datetime.now() - timedelta(hours=DAY_CUTOFF_HOUR)).date().isoformat()


def run_todo(data_dir, cli_args):
    """调 todo.py 子进程；成功返回其 --json 输出，失败按 stderr 分类抛 ApiError。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(TODO_PY), *cli_args, "--json", "--data-dir", str(data_dir)],
        capture_output=True, text=True, encoding="utf-8", env=env)
    if proc.returncode == 0:
        return json.loads(proc.stdout)
    msg = (proc.stderr or "").strip()
    if msg.startswith("错误: "):
        msg = msg[len("错误: "):]
    if "找不到任务" in msg:
        raise ApiError(404, "task_not_found", msg)
    if "获取写锁超时" in msg:
        raise ApiError(409, "locked", msg)
    raise ApiError(400, "invalid_request", msg or f"todo.py 退出码 {proc.returncode}")


class ReadCache:
    """读端点缓存：todos.json 的 mtime 一变即整体失效（设计文档 §1）。

    一切写操作（含 archive/undo）最终都改写 todos.json，故其 mtime 是数据变化的
    可靠信号。缓存键额外带逻辑日，跨过凌晨 4 点日界线后 today/week 视图自动失效。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._data = {}

    @staticmethod
    def token(data_dir):
        try:
            return os.stat(data_dir / "todos.json").st_mtime_ns
        except OSError:
            return -1

    def get(self, data_dir, key):
        with self._lock:
            if self._token == self.token(data_dir):
                return self._data.get(key)
            return None

    def put(self, data_dir, key, value, token_before):
        with self._lock:
            if self.token(data_dir) != token_before:
                return  # 子进程执行期间数据变了，放弃本次缓存
            if self._token != token_before:
                self._token, self._data = token_before, {}
            self._data[key] = value


CACHE = ReadCache()


def cached_todo(data_dir, cli_args):
    key = (logical_today_str(), *cli_args)
    hit = CACHE.get(data_dir, key)
    if hit is not None:
        return hit
    token = ReadCache.token(data_dir)
    result = run_todo(data_dir, cli_args)
    CACHE.put(data_dir, key, result, token)
    return result
```

在 Handler 的端点区加读端点：

```python
    def h_list(self):
        args = ["list", "--view", self.query.get("view", "all")]
        if "status" in self.query:
            args += ["--status", self.query["status"]]
        if "tag" in self.query:
            args += ["--tag", self.query["tag"]]
        if self.query.get("overdue") in ("1", "true"):
            args.append("--overdue")
        if self.query.get("include_done") in ("1", "true"):
            args.append("--include-done")
        self.send_json(200, cached_todo(self.server.data_dir, args))

    def h_show(self, id):
        self.send_json(200, cached_todo(self.server.data_dir, ["show", id]))

    def h_search(self):
        q = self.query.get("q", "").strip()
        if not q:
            raise ApiError(400, "invalid_request", "缺少查询参数 q")
        self.send_json(200, cached_todo(self.server.data_dir, ["search", q]))

    def h_report(self):
        self.send_json(200, cached_todo(self.server.data_dir, ["report"]))
```

ROUTES 替换为：

```python
ROUTES = [
    ("GET", re.compile(r"^/api/status$"), "h_status", False),
    ("GET", re.compile(r"^/api/tasks$"), "h_list", True),
    ("GET", re.compile(r"^/api/tasks/(?P<id>\d+(?:\.\d+)?)$"), "h_show", True),
    ("GET", re.compile(r"^/api/search$"), "h_search", True),
    ("GET", re.compile(r"^/api/report$"), "h_report", True),
]
```

- [ ] **Step 4: 运行全部测试确认通过**

Run: `python -m pytest tests/test_serve.py -v`
Expected: 全部 passed（含 Task 1 的 4 个）。

---

### Task 3: 写端点（新增 / 更新 / 完成 / 子任务）

**Files:**
- Modify: `todo-manager/scripts/serve.py`
- Test: `tests/test_serve.py`

**Interfaces:**
- Consumes: Task 2 的 `run_todo`。
- Produces: Handler 方法 `read_body() -> dict`、`h_add/h_update/h_done/h_subtask_add`；ROUTES 增 4 条。**Task 7 的页面按这些端点的请求格式调用。**

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_serve.py`：

```python
# ---------------------------------------------------------------- Task 3

def test_add_task_full_fields(server):
    status, data, _ = server.req("POST", "/api/tasks", body={
        "title": "订机票和酒店", "priority": "P1", "due": "2026-07-20",
        "tags": ["生活", "旅行"], "notes": "出差", "estimate": 30,
        "subtasks": ["订机票", "订酒店"], "inferred": ["priority", "due"]})
    assert status == 200
    t = data["task"]
    assert t["id"] == 1 and t["priority"] == "P1" and t["tags"] == ["生活", "旅行"]
    assert [st["title"] for st in t["subtasks"]] == ["订机票", "订酒店"]
    assert set(t["inferred"]) == {"priority", "due"}


def test_add_requires_title(server):
    status, data, _ = server.req("POST", "/api/tasks", body={"title": "  "})
    assert status == 400


def test_add_invalid_priority_maps_400(server):
    status, data, _ = server.req("POST", "/api/tasks",
                                 body={"title": "x", "priority": "P9"})
    assert status == 400
    assert "priority" in data["error"]["message"]


def test_patch_update_and_clear_due(server):
    server.req("POST", "/api/tasks", body={"title": "改我", "due": "2026-07-20"})
    status, data, _ = server.req("PATCH", "/api/tasks/1",
                                 body={"title": "改好了", "priority": "P0"})
    assert status == 200 and data["task"]["title"] == "改好了"
    status, data, _ = server.req("PATCH", "/api/tasks/1", body={"due": None})
    assert status == 200 and data["task"]["due"] is None


def test_patch_null_only_for_clearable(server):
    server.req("POST", "/api/tasks", body={"title": "x"})
    status, data, _ = server.req("PATCH", "/api/tasks/1", body={"title": None})
    assert status == 400


def test_patch_empty_body_400(server):
    server.req("POST", "/api/tasks", body={"title": "x"})
    status, _, _ = server.req("PATCH", "/api/tasks/1", body={})
    assert status == 400


def test_patch_missing_task_404(server):
    status, _, _ = server.req("PATCH", "/api/tasks/42", body={"title": "x"})
    assert status == 404


def test_done_task_and_subtask(server):
    server.req("POST", "/api/tasks", body={"title": "父任务", "subtasks": ["子1", "子2"]})
    status, data, _ = server.req("POST", "/api/tasks/1.1/done")
    assert status == 200 and data["task"]["progress"] == 50
    status, data, _ = server.req("POST", "/api/tasks/1/done")
    assert status == 200 and data["task"]["status"] == "done"
    assert data["task"]["progress"] == 100


def test_add_subtask(server):
    server.req("POST", "/api/tasks", body={"title": "父"})
    status, data, _ = server.req("POST", "/api/tasks/1/subtasks",
                                 body={"title": "新子任务", "due": "2026-07-15"})
    assert status == 200
    assert data["subtask"]["id"] == "1.1" and data["subtask"]["due"] == "2026-07-15"


def test_write_invalidates_cache(server):
    _, d1, _ = server.req("GET", "/api/tasks?view=all")
    assert d1["count"] == 0
    time.sleep(0.02)
    server.req("POST", "/api/tasks", body={"title": "经 API 写入"})
    _, d2, _ = server.req("GET", "/api/tasks?view=all")
    assert d2["count"] == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_serve.py -v -k "add or patch or done or subtask or write"`
Expected: 新增测试全部 FAIL（405 或 404：路由不存在）。

- [ ] **Step 3: 实现写端点**

Handler 中加入：

```python
    CLEARABLE = {"due", "recurrence"}  # PATCH 传 null 可清除的字段（对应 CLI 的 "none"）

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError(400, "invalid_request", "请求体不是合法 JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_request", "请求体必须是 JSON 对象")
        return body

    @staticmethod
    def as_int(value, field):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ApiError(400, "invalid_request", f"{field} 必须是整数")
        return str(value)

    def h_add(self):
        b = self.read_body()
        title = (b.get("title") or "").strip()
        if not title:
            raise ApiError(400, "invalid_request", "title 必填")
        args = ["add", title]
        for field, flag in (("due", "--due"), ("priority", "--priority"),
                            ("notes", "--notes"), ("recurrence", "--recurrence")):
            if b.get(field):
                args += [flag, str(b[field])]
        if b.get("tags"):
            args += ["--tags", ",".join(map(str, b["tags"]))]
        if b.get("estimate") is not None:
            args += ["--estimate", self.as_int(b["estimate"], "estimate")]
        for st in b.get("subtasks") or []:
            args += ["--subtask", str(st)]
        if b.get("inferred"):
            args += ["--inferred", ",".join(map(str, b["inferred"]))]
        self.send_json(200, run_todo(self.server.data_dir, args))

    def h_update(self, id):
        b = self.read_body()
        if not b:
            raise ApiError(400, "invalid_request", "PATCH 请求体为空，没有要更新的字段")
        args = ["update", id]
        text_fields = {"title": "--title", "notes": "--notes", "priority": "--priority",
                       "status": "--status", "due": "--due", "recurrence": "--recurrence",
                       "blocked_reason": "--blocked-reason"}
        for field, flag in text_fields.items():
            if field in b:
                v = b[field]
                if v is None:
                    if field not in self.CLEARABLE:
                        raise ApiError(400, "invalid_request",
                                       f"{field} 不支持传 null 清除（可清除: due/recurrence）")
                    v = "none"
                args += [flag, str(v)]
        if "tags" in b:
            if not isinstance(b["tags"], list):
                raise ApiError(400, "invalid_request", "tags 必须是数组")
            args += ["--tags", ",".join(map(str, b["tags"]))]
        if "estimate" in b:
            args += ["--estimate", self.as_int(b["estimate"], "estimate")]
        if "progress" in b:
            args += ["--progress", self.as_int(b["progress"], "progress")]
        self.send_json(200, run_todo(self.server.data_dir, args))

    def h_done(self, id):
        self.send_json(200, run_todo(self.server.data_dir, ["done", id]))

    def h_subtask_add(self, id):
        b = self.read_body()
        title = (b.get("title") or "").strip()
        if not title:
            raise ApiError(400, "invalid_request", "title 必填")
        args = ["subtask", "add", id, title]
        if b.get("due"):
            args += ["--due", str(b["due"])]
        self.send_json(200, run_todo(self.server.data_dir, args))
```

ROUTES 增补（保持已有条目，追加）：

```python
    ("POST", re.compile(r"^/api/tasks$"), "h_add", True),
    ("PATCH", re.compile(r"^/api/tasks/(?P<id>\d+(?:\.\d+)?)$"), "h_update", True),
    ("POST", re.compile(r"^/api/tasks/(?P<id>\d+(?:\.\d+)?)/done$"), "h_done", True),
    ("POST", re.compile(r"^/api/tasks/(?P<id>\d+)/subtasks$"), "h_subtask_add", True),
]
```

- [ ] **Step 4: 运行全部测试确认通过**

Run: `python -m pytest tests/test_serve.py -v`
Expected: 全部 passed。

---

### Task 4: 密码凭证存储（`auth set` / `auth unset` / serve.json）

**Files:**
- Modify: `todo-manager/scripts/serve.py`
- Test: `tests/test_serve.py`

**Interfaces:**
- Consumes: Task 1 的 `resolve_data_dir`、`build_parser`、`main`。
- Produces（Task 5 直接使用）:
  - `PBKDF2_ITERATIONS = 600_000`
  - `serve_config_path(data_dir) -> Path`、`load_serve_config(data_dir) -> dict|None`、`write_serve_config(data_dir, cfg)`
  - `auth_section(cfg) -> dict|None`、`hash_password(password, salt_hex, iterations) -> str`
  - `cmd_auth_set(data_dir, password) -> int`、`cmd_auth_unset(data_dir) -> int`
  - CLI：`serve.py auth set [--password-stdin] [--data-dir]`、`serve.py auth unset [--data-dir]`
  - serve.json 结构（设计文档 §3）：`{"schema_version":1, "auth":{"algo","iterations","salt","hash","generation"}, "session":{"secret","ttl_days"}}`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_serve.py`：

```python
# ---------------------------------------------------------------- Task 4

def serve_cli(data_dir, *args, stdin=None):
    return subprocess.run(
        [sys.executable, str(SERVE), *args, "--data-dir", str(data_dir)],
        input=stdin, capture_output=True, text=True, encoding="utf-8", env=ENV)


def set_password(data_dir, password):
    r = serve_cli(data_dir, "auth", "set", "--password-stdin", stdin=password + "\n")
    assert r.returncode == 0, r.stderr


def read_serve_json(data_dir):
    return json.loads((Path(data_dir) / "serve.json").read_text(encoding="utf-8"))


def test_auth_set_creates_config(tmp_path):
    set_password(tmp_path, "s3cret")
    cfg = read_serve_json(tmp_path)
    a = cfg["auth"]
    assert a["algo"] == "pbkdf2_sha256" and a["iterations"] == 600000
    assert a["generation"] == 1
    assert len(bytes.fromhex(a["salt"])) == 16 and len(bytes.fromhex(a["hash"])) == 32
    assert "s3cret" not in json.dumps(cfg)  # 绝不存明文
    assert len(bytes.fromhex(cfg["session"]["secret"])) == 32
    assert cfg["session"]["ttl_days"] == 30


def test_auth_set_again_bumps_generation_keeps_secret(tmp_path):
    set_password(tmp_path, "one")
    secret1 = read_serve_json(tmp_path)["session"]["secret"]
    set_password(tmp_path, "two")
    cfg = read_serve_json(tmp_path)
    assert cfg["auth"]["generation"] == 2
    assert cfg["session"]["secret"] == secret1  # 换密码不换会话密钥，靠 generation 失效


def test_auth_set_rejects_empty(tmp_path):
    r = serve_cli(tmp_path, "auth", "set", "--password-stdin", stdin="\n")
    assert r.returncode != 0


def test_auth_unset(tmp_path):
    set_password(tmp_path, "x")
    r = serve_cli(tmp_path, "auth", "unset")
    assert r.returncode == 0
    assert "auth" not in read_serve_json(tmp_path)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_serve.py -v -k auth`
Expected: 新增测试 FAIL（argparse 不认识 auth 子命令，退出码 2）。

- [ ] **Step 3: 实现凭证存储与 CLI**

serve.py import 区加 `getpass`、`hashlib`、`secrets`。`resolve_data_dir` 之后加：

```python
PBKDF2_ITERATIONS = 600_000


def serve_config_path(data_dir):
    return data_dir / "serve.json"


def load_serve_config(data_dir):
    p = serve_config_path(data_dir)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def write_serve_config(data_dir, cfg):
    data_dir.mkdir(parents=True, exist_ok=True)
    p = serve_config_path(data_dir)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)  # POSIX 收紧权限；Windows 上尽力而为
    except OSError:
        pass


def auth_section(cfg):
    return (cfg or {}).get("auth") or None


def hash_password(password, salt_hex, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt_hex), iterations).hex()


def cmd_auth_set(data_dir, password):
    password = password.rstrip("\r\n")
    if not password:
        print("错误: 密码不能为空", file=sys.stderr)
        return 1
    cfg = load_serve_config(data_dir) or {"schema_version": 1}
    generation = (auth_section(cfg) or {}).get("generation", 0) + 1
    salt = secrets.token_hex(16)
    cfg["auth"] = {"algo": "pbkdf2_sha256", "iterations": PBKDF2_ITERATIONS,
                   "salt": salt,
                   "hash": hash_password(password, salt, PBKDF2_ITERATIONS),
                   "generation": generation}
    cfg.setdefault("session", {"secret": secrets.token_hex(32), "ttl_days": 30})
    write_serve_config(data_dir, cfg)
    print("密码已设置；此前签发的所有会话已失效。")
    return 0


def cmd_auth_unset(data_dir):
    cfg = load_serve_config(data_dir)
    if not auth_section(cfg):
        print("本来就未设置密码。")
        return 0
    cfg.pop("auth")
    write_serve_config(data_dir, cfg)
    print("密码已移除；服务此后只能监听 127.0.0.1。")
    return 0
```

`build_parser` 与 `main` 改为：

```python
def build_parser():
    p = argparse.ArgumentParser(description="todo-manager 可视化参考服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=3007)
    p.add_argument("--data-dir", help="数据目录（默认 $TODO_MANAGER_HOME 或 ~/.todo-manager）")
    p.add_argument("--cors-origin", action="append", default=[],
                   help="允许跨域的前端地址，可重复；默认不发 CORS 头（仅同源）")
    sub = p.add_subparsers(dest="cmd")
    auth = sub.add_parser("auth", help="管理访问密码")
    auth.add_argument("action", choices=["set", "unset"])
    auth.add_argument("--password-stdin", action="store_true",
                      help="从标准输入读密码（供 Agent 非交互调用）")
    auth.add_argument("--data-dir", help="数据目录")
    return p


def main(argv=None):
    ns = build_parser().parse_args(argv)
    data_dir = resolve_data_dir(ns.data_dir)
    if ns.cmd == "auth":
        if ns.action == "set":
            if ns.password_stdin:
                password = sys.stdin.readline()
            else:
                password = getpass.getpass("设置访问密码: ")
            return cmd_auth_set(data_dir, password)
        return cmd_auth_unset(data_dir)
    server = ThreadingHTTPServer((ns.host, ns.port), Handler)
    server.data_dir = data_dir
    server.cors_origins = ns.cors_origin
    print(f"todo-manager 可视化服务: http://{ns.host}:{ns.port}  数据目录: {data_dir}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
```

- [ ] **Step 4: 运行全部测试确认通过**

Run: `python -m pytest tests/test_serve.py -v`
Expected: 全部 passed。

---

### Task 5: 认证执行层（401 / 登录 / cookie / Basic / 启动拒绝 / Host 校验）

**Files:**
- Modify: `todo-manager/scripts/serve.py`
- Test: `tests/test_serve.py`

**Interfaces:**
- Consumes: Task 4 的全部凭证函数；Task 1 的 ROUTES needs_auth 标记（本任务开始生效）。
- Produces:
  - `verify_password(cfg, password) -> bool`、`sign_session(cfg, expires:int, generation:int) -> str`、`verify_session(cfg, value:str) -> bool`
  - `LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}`
  - Handler：真实 `is_authenticated`、`check_host_header`、`h_login`、`h_logout`；`h_status` 返回真实 auth 状态
  - 启动检查：`--host` 非本机且未设密码 → 退出码 2
  - cookie 名 `todo_session`，格式 `<过期时间戳>.<generation>.<HMAC十六进制>`（**Task 7 页面、Task 8 文档按此描述**）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_serve.py`：

```python
# ---------------------------------------------------------------- Task 5

import base64


def basic(user, password):
    tok = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {tok}"}


def make_cookie(data_dir, expires, generation=None):
    cfg = read_serve_json(data_dir)
    gen = cfg["auth"]["generation"] if generation is None else generation
    payload = f"{expires}.{gen}"
    sig = hmac.new(cfg["session"]["secret"].encode(), payload.encode(),
                   hashlib.sha256).hexdigest()
    return f"todo_session={payload}.{sig}"


@pytest.fixture
def auth_server(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    set_password(data_dir, "开门芝麻")
    s = Server(data_dir)
    yield s, data_dir
    s.stop()


def test_no_credentials_401(auth_server):
    s, _ = auth_server
    status, data, _ = s.req("GET", "/api/tasks?view=all")
    assert status == 401 and data["error"]["code"] == "unauthorized"


def test_status_and_index_open_without_auth(auth_server):
    s, _ = auth_server
    status, data, _ = s.req("GET", "/api/status")
    assert status == 200
    assert data["auth_required"] is True and data["authenticated"] is False
    status, _, _ = s.req_raw("GET", "/")
    assert status == 200  # 静态壳不含数据，可无认证加载


def test_login_flow_and_cookie(auth_server):
    s, _ = auth_server
    status, data, headers = s.req("POST", "/api/auth/login", body={"password": "开门芝麻"})
    assert status == 200 and data["authenticated"] is True
    set_cookie = headers["Set-Cookie"]
    assert "todo_session=" in set_cookie and "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie and "Secure" not in set_cookie
    cookie = set_cookie.split(";")[0]
    status, data, _ = s.req("GET", "/api/tasks?view=all", headers={"Cookie": cookie})
    assert status == 200
    status, data, _ = s.req("GET", "/api/status", headers={"Cookie": cookie})
    assert data["authenticated"] is True


def test_login_wrong_password_401_throttled(auth_server):
    s, _ = auth_server
    t0 = time.monotonic()
    status, data, _ = s.req("POST", "/api/auth/login", body={"password": "猜的"})
    assert status == 401
    assert time.monotonic() - t0 >= 1.0  # 失败节流


def test_secure_flag_behind_tls_proxy(auth_server):
    s, _ = auth_server
    _, _, headers = s.req("POST", "/api/auth/login",
                          body={"password": "开门芝麻"},
                          headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in headers["Set-Cookie"]


def test_tampered_and_expired_cookie_rejected(auth_server):
    s, data_dir = auth_server
    good = make_cookie(data_dir, int(time.time()) + 3600)
    status, _, _ = s.req("GET", "/api/tasks?view=all", headers={"Cookie": good})
    assert status == 200
    tampered = good[:-4] + ("0000" if not good.endswith("0000") else "1111")
    status, _, _ = s.req("GET", "/api/tasks?view=all", headers={"Cookie": tampered})
    assert status == 401
    expired = make_cookie(data_dir, int(time.time()) - 10)
    status, _, _ = s.req("GET", "/api/tasks?view=all", headers={"Cookie": expired})
    assert status == 401


def test_password_change_invalidates_sessions(auth_server):
    s, data_dir = auth_server
    old = make_cookie(data_dir, int(time.time()) + 3600)  # generation=1
    set_password(data_dir, "新密码")                        # generation=2
    status, _, _ = s.req("GET", "/api/tasks?view=all", headers={"Cookie": old})
    assert status == 401


def test_basic_auth_any_username(auth_server):
    s, _ = auth_server
    for user in ("admin", "随便谁", ""):
        status, _, _ = s.req("GET", "/api/tasks?view=all",
                             headers=basic(user, "开门芝麻"))
        assert status == 200, f"用户名 {user!r} 应被忽略"
    status, _, _ = s.req("GET", "/api/tasks?view=all", headers=basic("admin", "错密码"))
    assert status == 401


def test_logout_clears_cookie(auth_server):
    s, data_dir = auth_server
    cookie = make_cookie(data_dir, int(time.time()) + 3600)
    status, data, headers = s.req("POST", "/api/auth/logout",
                                  headers={"Cookie": cookie})
    assert status == 200 and "Max-Age=0" in headers["Set-Cookie"]


def test_public_host_requires_password(tmp_path):
    r = subprocess.run(
        [sys.executable, str(SERVE), "--host", "0.0.0.0", "--port", str(free_port()),
         "--data-dir", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=15)
    assert r.returncode == 2
    assert "auth set" in r.stderr


def test_host_header_rejected_when_no_password(server):
    """无密码模式的 DNS rebinding 防御：陌生 Host 一律 403。"""
    status, data, _ = server.req("GET", "/api/tasks?view=all",
                                 headers={"Host": "evil.example.com"})
    assert status == 403
    status, _, _ = server.req("GET", "/api/tasks?view=all",
                              headers={"Host": "127.0.0.1:9999"})
    assert status == 200  # 本机 Host 带端口正常


def test_host_header_not_checked_with_password(auth_server):
    """已设密码时不校验 Host（反代场景 Host 是公网域名），认证本身是防线。"""
    s, _ = auth_server
    status, _, _ = s.req("GET", "/api/tasks?view=all",
                         headers={"Host": "api.example.com", **basic("admin", "开门芝麻")})
    assert status == 200
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_serve.py -v -k "401 or login or cookie or basic or host or logout or secure or invalidates"`
Expected: 新增测试全部 FAIL（此时任何请求都被放行 / login 路由不存在 / 启动不拒绝）。

- [ ] **Step 3: 实现认证执行层**

serve.py import 区加 `base64`、`hmac`、`threading`（若已加则跳过）、`time`。加模块级：

```python
LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}
FAIL_LOCK = threading.Lock()  # 登录失败节流串行化


def verify_password(cfg, password):
    auth = auth_section(cfg)
    if not auth:
        return False
    digest = hash_password(password, auth["salt"], auth["iterations"])
    return hmac.compare_digest(digest, auth["hash"])


def sign_session(cfg, expires, generation):
    payload = f"{expires}.{generation}"
    sig = hmac.new(cfg["session"]["secret"].encode(), payload.encode(),
                   hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session(cfg, value):
    auth = auth_section(cfg)
    if not (auth and (cfg or {}).get("session") and value):
        return False
    parts = value.split(".")
    if len(parts) != 3:
        return False
    exp_s, gen_s, sig = parts
    expected = hmac.new(cfg["session"]["secret"].encode(),
                        f"{exp_s}.{gen_s}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        return int(gen_s) == auth["generation"] and int(exp_s) > time.time()
    except ValueError:
        return False
```

Handler：删除恒 True 的 `is_authenticated` 桩，替换为真实实现，并在 `handle_one` 的 try 块开头加配置装载与 Host 校验：

```python
    # handle_one 的 try: 之后、路由匹配之前插入——
            self.cfg = load_serve_config(self.server.data_dir)
            if not auth_section(self.cfg):
                self.check_host_header()  # 仅无密码模式做 DNS rebinding 防御
```

```python
    def is_authenticated(self):
        if not auth_section(self.cfg):
            return True  # 未设密码 = 仅本机模式，SSH/本机登录即认证
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
            except Exception:
                return False
            _user, _, password = decoded.partition(":")  # 用户名忽略，只看密码
            return verify_password(self.cfg, password)
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "todo_session":
                return verify_session(self.cfg, v)
        return False

    def check_host_header(self):
        host = self.headers.get("Host", "")
        hostname = (urlparse("//" + host).hostname or "").lower()
        if hostname not in LOCAL_HOSTNAMES:
            raise ApiError(403, "forbidden_host",
                           f"未设密码模式只接受本机访问，拒绝 Host: {host!r}")

    def h_login(self):
        if not auth_section(self.cfg):
            raise ApiError(400, "auth_not_configured", "服务未设置密码，无需登录")
        b = self.read_body()
        password = b.get("password") or ""
        if not verify_password(self.cfg, password):
            with FAIL_LOCK:
                time.sleep(1)  # 失败统一慢 1 秒，抵抗暴力猜测
            raise ApiError(401, "bad_password", "密码不正确")
        ttl = int(self.cfg["session"].get("ttl_days", 30)) * 86400
        expires = int(time.time()) + ttl
        value = sign_session(self.cfg, expires, auth_section(self.cfg)["generation"])
        cookie = f"todo_session={value}; Max-Age={ttl}; Path=/; HttpOnly; SameSite=Lax"
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            cookie += "; Secure"
        self.send_json(200, {"authenticated": True}, extra_headers=[("Set-Cookie", cookie)])

    def h_logout(self):
        cookie = "todo_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
        self.send_json(200, {"authenticated": False},
                       extra_headers=[("Set-Cookie", cookie)])
```

`h_status` 替换为：

```python
    def h_status(self):
        self.send_json(200, {"version": VERSION, "contract_version": CONTRACT_VERSION,
                             "auth_required": bool(auth_section(self.cfg)),
                             "authenticated": self.is_authenticated()})
```

ROUTES 增补两条（追加在 h_status 条目之后）：

```python
    ("POST", re.compile(r"^/api/auth/login$"), "h_login", False),
    ("POST", re.compile(r"^/api/auth/logout$"), "h_logout", True),
```

`main` 的 serve 分支在创建 ThreadingHTTPServer 之前加启动检查：

```python
    if ns.host not in LOCAL_HOSTNAMES and not auth_section(load_serve_config(data_dir)):
        print("错误: 监听非本机地址之前必须先设置访问密码: python serve.py auth set",
              file=sys.stderr)
        return 2
```

注意：`test_host_header_rejected_when_no_password` 里带自定义 Host 的请求，`urllib` 会照发；无密码 fixture（`server`）的所有既有测试默认 Host 是 `127.0.0.1:端口`，不受影响。

- [ ] **Step 4: 运行全部测试确认通过**

Run: `python -m pytest tests/test_serve.py -v`
Expected: 全部 passed（此前任务的测试都不该被认证破坏——它们用的 `server` fixture 未设密码）。

---

### Task 6: CORS

**Files:**
- Modify: `todo-manager/scripts/serve.py`
- Test: `tests/test_serve.py`

**Interfaces:**
- Consumes: Task 1 的 `server.cors_origins`（启动参数已就位）、`send_json`。
- Produces: Handler 的 `cors_headers()`、`do_OPTIONS`；send_json 自动附带 CORS 头。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_serve.py`：

```python
# ---------------------------------------------------------------- Task 6

@pytest.fixture
def cors_server(tmp_path):
    s = Server(tmp_path / "data",
               args=("--cors-origin", "https://todo.example.com",
                     "--cors-origin", "http://localhost:5173"))
    yield s
    s.stop()


def test_no_cors_headers_by_default(server):
    _, _, headers = server.req("GET", "/api/status",
                               headers={"Origin": "https://todo.example.com"})
    assert headers.get("Access-Control-Allow-Origin") is None


def test_preflight_allowed_origin(cors_server):
    status, _, headers = cors_server.req_raw(
        "OPTIONS", "/api/tasks", headers={"Origin": "https://todo.example.com",
                                          "Access-Control-Request-Method": "POST"})
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == "https://todo.example.com"
    assert headers["Access-Control-Allow-Credentials"] == "true"
    assert "PATCH" in headers["Access-Control-Allow-Methods"]
    assert "Authorization" in headers["Access-Control-Allow-Headers"]


def test_second_origin_also_allowed(cors_server):
    _, _, headers = cors_server.req("GET", "/api/status",
                                    headers={"Origin": "http://localhost:5173"})
    assert headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert headers["Vary"] == "Origin"


def test_unlisted_origin_gets_nothing(cors_server):
    _, _, headers = cors_server.req("GET", "/api/status",
                                    headers={"Origin": "https://evil.example.com"})
    assert headers.get("Access-Control-Allow-Origin") is None
    status, _, headers = cors_server.req_raw(
        "OPTIONS", "/api/tasks", headers={"Origin": "https://evil.example.com"})
    assert status == 204
    assert headers.get("Access-Control-Allow-Origin") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_serve.py -v -k cors or preflight or origin`
Expected: `test_no_cors_headers_by_default` 可能已通过（尚无 CORS 代码），其余 FAIL（OPTIONS 无处理，501）。

- [ ] **Step 3: 实现 CORS**

Handler 加：

```python
    def cors_headers(self):
        origin = self.headers.get("Origin")
        if origin and origin in self.server.cors_origins:
            return [("Access-Control-Allow-Origin", origin),
                    ("Access-Control-Allow-Credentials", "true"),
                    ("Vary", "Origin")]
        return []

    def do_OPTIONS(self):
        # 跨域预检免认证放行（设计文档 §3）；非白名单来源不给任何 CORS 头
        headers = self.cors_headers()
        self.send_response(204)
        for k, v in headers:
            self.send_header(k, v)
        if headers:
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()
```

`send_json` 里 `for k, v in extra_headers:` 之前插入一行，让一切 JSON 响应自动带 CORS 头：

```python
        for k, v in self.cors_headers():
            self.send_header(k, v)
```

`req_raw` 对 204 无响应体的 OPTIONS 正常工作（`read()` 返回空串）。

- [ ] **Step 4: 运行全部测试确认通过**

Run: `python -m pytest tests/test_serve.py -v`
Expected: 全部 passed。

---

### Task 7: webui.html 内置单文件页面

**Files:**
- Create: `todo-manager/scripts/webui.html`
- Test: `tests/test_serve.py`（冒烟）

**Interfaces:**
- Consumes: Task 2/3/5 的全部端点（契约见各任务 Produces 与设计文档 §2）；`GET /` 由 Task 1 的 `h_index` 分发本文件。
- Produces: 完整可用的移动优先单页应用。功能面 = 设计文档 §4（视图/卡片/快速操作/详情编辑/新增/搜索/报表/AI 推断标记/登录视图）。

- [ ] **Step 1: 写冒烟测试（失败）**

追加到 `tests/test_serve.py`：

```python
# ---------------------------------------------------------------- Task 7

def test_webui_smoke(server):
    status, text, _ = server.req_raw("GET", "/")
    assert status == 200
    for marker in ('id="app"', 'id="login"', "/api/status", "logicalToday"):
        assert marker in text, f"webui.html 缺少关键片段: {marker}"
```

Run: `python -m pytest tests/test_serve.py::test_webui_smoke -v`
Expected: FAIL（返回的是缺失占位 HTML）。

- [ ] **Step 2: 创建 webui.html**

创建 `todo-manager/scripts/webui.html`，完整内容如下（零外部资源；「逻辑日」口径与 CLI 一致）：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>todo-manager</title>
<style>
:root{--p0:#d33c3c;--p1:#e08a00;--p2:#2f6fd6;--p3:#8892a0;--bg:#f4f5f7;--card:#fff;--ink:#22262c;--mut:#6b7480;--line:#e3e6ea}
*{box-sizing:border-box}
body{margin:0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink)}
button{font:inherit;cursor:pointer}
header{position:sticky;top:0;z-index:5;background:var(--card);display:flex;gap:.5rem;align-items:center;padding:.55rem .8rem;border-bottom:1px solid var(--line)}
header h1{font-size:1rem;margin:0;white-space:nowrap}
#q{flex:1;min-width:0;padding:.4rem .6rem;border:1px solid var(--line);border-radius:8px}
nav{display:flex;gap:.3rem;padding:.5rem .8rem;overflow-x:auto;max-width:680px;margin:0 auto}
nav button{border:0;background:transparent;padding:.35rem .8rem;border-radius:999px;white-space:nowrap}
nav button.on{background:var(--ink);color:#fff}
main{max-width:680px;margin:0 auto;padding:0 .8rem 6rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:.6rem .75rem;margin:.55rem 0}
.row{display:flex;gap:.6rem;align-items:flex-start}
.row>input[type=checkbox]{width:1.25rem;height:1.25rem;margin-top:.15rem;flex:none}
.t{flex:1;min-width:0}
.title{overflow-wrap:anywhere;text-align:left}
.done .title{text-decoration:line-through;color:var(--mut)}
.meta{display:flex;flex-wrap:wrap;gap:.45rem;font-size:.8rem;color:var(--mut);margin-top:.15rem;align-items:center}
.pri{font-weight:700}
.P0{color:var(--p0)}.P1{color:var(--p1)}.P2{color:var(--p2)}.P3{color:var(--p3)}
.due.over{color:var(--p0);font-weight:700}
.due.today{color:var(--p1);font-weight:700}
.tag{background:var(--bg);border-radius:6px;padding:0 .4rem}
.ai{border:1px solid var(--line);border-radius:6px;padding:0 .3rem;font-size:.72rem}
.bar{height:4px;background:var(--bg);border-radius:2px;margin-top:.4rem;overflow:hidden}
.bar i{display:block;height:100%;background:var(--p2)}
.subs{margin:.4rem 0 0;padding-left:1.9rem;font-size:.9rem;display:grid;gap:.15rem}
.subs label{display:flex;gap:.4rem;align-items:center}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.55rem}
.stats .card{margin:0;text-align:center}
.stats b{font-size:1.5rem;display:block}
#fab{position:fixed;right:1rem;bottom:1rem;width:3.2rem;height:3.2rem;border-radius:50%;border:0;background:var(--ink);color:#fff;font-size:1.6rem}
dialog{border:1px solid var(--line);border-radius:12px;width:min(94vw,460px);padding:1rem}
dialog::backdrop{background:rgba(0,0,0,.35)}
form.grid{display:grid;gap:.55rem}
form.grid label{display:grid;gap:.15rem;font-size:.82rem;color:var(--mut)}
form.grid input,form.grid select,form.grid textarea{font:inherit;padding:.4rem .55rem;border:1px solid var(--line);border-radius:8px;color:var(--ink)}
.actions{display:flex;gap:.5rem;justify-content:flex-end;margin-top:.3rem}
.primary{background:var(--ink);color:#fff;border:0;border-radius:8px;padding:.45rem 1rem}
.ghost{background:none;border:1px solid var(--line);border-radius:8px;padding:.45rem .8rem}
#login{max-width:320px;margin:18vh auto;padding:0 1rem;display:grid;gap:.6rem}
#toast{position:fixed;left:50%;bottom:5.2rem;transform:translateX(-50%);background:var(--ink);color:#fff;border-radius:8px;padding:.5rem .9rem;opacity:0;transition:opacity .2s;max-width:90vw;pointer-events:none}
.mut{color:var(--mut)}
.linkish{background:none;border:0;color:var(--p2);padding:0}
</style>
</head>
<body>
<div id="login" hidden>
  <h1>📋 todo-manager</h1>
  <input id="pw" type="password" placeholder="访问密码" autocomplete="current-password">
  <button id="loginBtn" class="primary">登录</button>
</div>

<div id="app" hidden>
  <header>
    <h1>📋 todo</h1>
    <input id="q" type="search" placeholder="搜索（含归档），回车确认…">
    <button id="logoutBtn" class="linkish" hidden>登出</button>
  </header>
  <nav id="tabs">
    <button data-v="today" class="on">今天</button>
    <button data-v="week">本周</button>
    <button data-v="blocked">阻塞</button>
    <button data-v="all">全部</button>
    <button data-v="report">报表</button>
  </nav>
  <main id="list"></main>
  <button id="fab" title="新增任务">＋</button>
</div>

<dialog id="dlgAdd"><form class="grid" method="dialog">
  <h3 style="margin:.1rem 0">新增任务</h3>
  <label>标题 *<input name="title" required></label>
  <label>优先级<select name="priority"><option value="">P2（默认）</option>
    <option>P0</option><option>P1</option><option>P2</option><option>P3</option></select></label>
  <label>截止日期<input name="due" type="date"></label>
  <label>标签（逗号分隔）<input name="tags"></label>
  <label>循环<input name="recurrence" placeholder="daily / weekly:mon / monthly:15 / every:3:days"></label>
  <label>预估分钟<input name="estimate" type="number" min="1"></label>
  <label>子任务（每行一个）<textarea name="subtasks" rows="2"></textarea></label>
  <label>备注<textarea name="notes" rows="2"></textarea></label>
  <div class="actions"><button class="ghost" value="cancel" formnovalidate>取消</button>
    <button class="primary" value="ok">添加</button></div>
</form></dialog>

<dialog id="dlgEdit"><form class="grid" method="dialog">
  <h3 style="margin:.1rem 0">编辑 <span id="editId"></span></h3>
  <label>标题<input name="title"></label>
  <label>状态<select name="status"><option>todo</option><option>in_progress</option>
    <option>blocked</option><option>done</option><option>cancelled</option></select></label>
  <label id="reasonRow" hidden>阻塞原因<input name="blocked_reason"></label>
  <label>优先级<select name="priority"><option>P0</option><option>P1</option>
    <option>P2</option><option>P3</option></select></label>
  <label>截止日期（清空 = 移除）<input name="due" type="date"></label>
  <label>标签（逗号分隔）<input name="tags"></label>
  <label>循环（留空 = 移除）<input name="recurrence"></label>
  <label>预估分钟<input name="estimate" type="number" min="1"></label>
  <label>进度 %（有子任务时自动计算，不可编辑）<input name="progress" type="number" min="0" max="100"></label>
  <label>备注<textarea name="notes" rows="2"></textarea></label>
  <label>新增子任务（保存时一并提交）<input name="newSubtask"></label>
  <div class="actions"><button class="ghost" value="cancel" formnovalidate>取消</button>
    <button class="primary" value="ok">保存</button></div>
</form></dialog>

<div id="toast"></div>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let VIEW='today', INCLUDE_DONE=false, AUTH={auth_required:false,authenticated:true}, EDITING=null;

function toast(msg){const t=$('#toast');t.textContent=msg;t.style.opacity=1;
  clearTimeout(t._h);t._h=setTimeout(()=>t.style.opacity=0,2600)}

async function api(path,opts={}){
  const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});
  if(r.status===401){showLogin();throw new Error('需要登录')}
  const d=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(d.error?.message||('HTTP '+r.status));
  return d;
}

// 逻辑日：凌晨 0:00–3:59 属前一天，与 CLI 口径一致（api.md「日期口径」）
function logicalToday(){const d=new Date(Date.now()-4*3600*1000);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`}
function esc(s){return String(s??'').replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function ai(t,f){return (t.inferred||[]).includes(f)
  ?' <span class="ai" title="该字段由 AI 推断">AI</span>':''}
function dueHtml(t){if(!t.due)return'';const today=logicalToday();
  const active=t.status!=='done'&&t.status!=='cancelled';
  const cls=active&&t.due<today?'due over':active&&t.due===today?'due today':'due';
  return `<span class="${cls}">📅 ${t.due}</span>`+ai(t,'due')}

function taskCard(t,readonly=false){
  const doneish=t.status==='done'||t.status==='cancelled';
  const subs=(t.subtasks||[]).map(st=>`<label>
    <input type="checkbox" data-done="${st.id}" ${st.status==='done'?'checked':''}
      ${st.status==='done'||readonly?'disabled':''}> <span>${esc(st.title)}</span></label>`).join('');
  return `<div class="card ${doneish?'done':''}"><div class="row">
    <input type="checkbox" data-done="${t.id}" ${doneish?'checked':''} ${doneish||readonly?'disabled':''}>
    <div class="t">
      <div>${readonly?`<span class="title">${esc(t.title)}</span>`
        :`<button class="linkish title" data-edit="${t.id}">${esc(t.title)}</button>`}${ai(t,'title')}</div>
      <div class="meta">
        <span>#${t.id}</span>
        <span class="pri ${t.priority}">${t.priority}</span>${ai(t,'priority')}
        ${dueHtml(t)}
        ${t.status==='in_progress'?'<span>● 进行中</span>':''}
        ${t.status==='cancelled'?'<span>✗ 已取消</span>':''}
        ${t.status==='blocked'?`<span class="due over">◼ ${esc(t.blocked_reason||'阻塞')}</span>`:''}
        ${t.recurrence?`<span>🔁 ${esc(t.recurrence)}</span>`+ai(t,'recurrence'):''}
        ${t.estimate_minutes?`<span>~${t.estimate_minutes}m</span>`+ai(t,'estimate'):''}
        ${(t.tags||[]).map(x=>`<span class="tag">${esc(x)}</span>`).join('')}${(t.tags||[]).length?ai(t,'tags'):''}
        ${t._source&&t._source!=='active'?`<span class="tag">${esc(t._source)}</span>`:''}
      </div>
      ${t.progress?`<div class="bar"><i style="width:${t.progress}%"></i></div>`:''}
      ${subs?`<div class="subs">${subs}</div>`:''}
    </div>
    ${!readonly&&t.status==='todo'?`<button class="ghost" data-start="${t.id}">开始</button>`:''}
  </div></div>`;
}

async function refresh(){
  if(VIEW==='report')return renderReport();
  const p=new URLSearchParams({view:VIEW});
  if(VIEW==='all'&&INCLUDE_DONE)p.set('include_done','1');
  const d=await api('/api/tasks?'+p);
  const extra=VIEW==='all'?`<label class="mut" style="font-size:.85rem;display:block;margin:.5rem 0">
    <input id="incDone" type="checkbox" ${INCLUDE_DONE?'checked':''}> 含已完成/已取消</label>`:'';
  $('#list').innerHTML=extra+(d.tasks.length?d.tasks.map(t=>taskCard(t)).join('')
    :'<p class="mut">（没有任务）</p>');
}

async function renderReport(){
  const r=await api('/api/report');
  $('#list').innerHTML=`<div class="stats" style="margin:.55rem 0">
    <div class="card"><b>${r.completed_this_week.length}</b>本周完成</div>
    <div class="card"><b>${r.active}</b>积压</div>
    <div class="card"><b>${r.overdue}</b>过期</div>
    <div class="card"><b>${Math.round(r.estimate_minutes_total/6)/10}h</b>预估剩余</div></div>
  <div class="card">优先级：${Object.entries(r.by_priority)
    .map(([p,n])=>`<span class="pri ${p}">${p}</span> ${n}`).join('　')}</div>
  <div class="card">状态：todo ${r.by_status.todo} ／ 进行中 ${r.by_status.in_progress} ／ 阻塞 ${r.by_status.blocked}</div>
  ${r.completed_this_week.length?`<div class="card">${
    r.completed_this_week.map(t=>`<div>✓ ${esc(t.title)}</div>`).join('')}</div>`:''}`;
}

async function doSearch(q){
  const d=await api('/api/search?q='+encodeURIComponent(q));
  $('#list').innerHTML=`<p class="mut" style="margin:.6rem 0">“${esc(q)}” 命中 ${d.matches.length} 项</p>`
    +d.matches.map(t=>taskCard(t,t._source!=='active')).join('');
}

$('#list').addEventListener('click',async e=>{
  const el=e.target;
  try{
    if(el.dataset.done){el.disabled=true;
      await api(`/api/tasks/${el.dataset.done}/done`,{method:'POST'});toast('已完成');await refresh()}
    else if(el.dataset.start){
      await api(`/api/tasks/${el.dataset.start}`,{method:'PATCH',
        body:JSON.stringify({status:'in_progress'})});await refresh()}
    else if(el.dataset.edit){await openEdit(el.dataset.edit)}
    else if(el.id==='incDone'){INCLUDE_DONE=el.checked;await refresh()}
  }catch(err){toast(err.message);refresh().catch(()=>{})}
});

$('#tabs').addEventListener('click',e=>{
  const b=e.target.closest('button');if(!b)return;
  $$('#tabs button').forEach(x=>x.classList.toggle('on',x===b));
  VIEW=b.dataset.v;$('#q').value='';refresh().catch(err=>toast(err.message))});

$('#q').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&e.target.value.trim())
    doSearch(e.target.value.trim()).catch(err=>toast(err.message))});

$('#fab').onclick=()=>{$('#dlgAdd form').reset();$('#dlgAdd').showModal()};
$('#dlgAdd form').addEventListener('submit',async e=>{
  if(e.submitter?.value==='cancel')return;
  e.preventDefault();
  const f=new FormData(e.target),b={title:String(f.get('title')).trim()};
  if(f.get('priority'))b.priority=f.get('priority');
  if(f.get('due'))b.due=f.get('due');
  const tags=String(f.get('tags')).split(/[,，]/).map(s=>s.trim()).filter(Boolean);
  if(tags.length)b.tags=tags;
  if(String(f.get('recurrence')).trim())b.recurrence=String(f.get('recurrence')).trim();
  if(f.get('estimate'))b.estimate=+f.get('estimate');
  const subs=String(f.get('subtasks')).split('\n').map(s=>s.trim()).filter(Boolean);
  if(subs.length)b.subtasks=subs;
  if(String(f.get('notes')).trim())b.notes=String(f.get('notes')).trim();
  try{const d=await api('/api/tasks',{method:'POST',body:JSON.stringify(b)});
    $('#dlgAdd').close();toast(`已添加 #${d.task.id}`);await refresh()}
  catch(err){toast(err.message)}
});

async function openEdit(id){
  const d=await api('/api/tasks/'+id);EDITING=d.task;
  const f=$('#dlgEdit form');f.reset();
  $('#editId').textContent='#'+EDITING.id;
  f.elements.title.value=EDITING.title;
  f.elements.status.value=EDITING.status;
  f.elements.blocked_reason.value=EDITING.blocked_reason||'';
  f.elements.priority.value=EDITING.priority;
  f.elements.due.value=EDITING.due||'';
  f.elements.tags.value=(EDITING.tags||[]).join(',');
  f.elements.recurrence.value=EDITING.recurrence||'';
  f.elements.estimate.value=EDITING.estimate_minutes||'';
  f.elements.progress.value=EDITING.progress??'';
  f.elements.progress.disabled=(EDITING.subtasks||[]).length>0;
  f.elements.notes.value=EDITING.notes||'';
  $('#reasonRow').hidden=EDITING.status!=='blocked';
  $('#dlgEdit').showModal();
}
$('#dlgEdit [name=status]').addEventListener('change',
  e=>{$('#reasonRow').hidden=e.target.value!=='blocked'});
$('#dlgEdit form').addEventListener('submit',async e=>{
  if(e.submitter?.value==='cancel')return;
  e.preventDefault();
  const f=new FormData(e.target),t=EDITING,b={};
  const title=String(f.get('title')).trim();
  if(title&&title!==t.title)b.title=title;
  if(f.get('status')!==t.status)b.status=f.get('status');
  if((b.status||t.status)==='blocked'&&String(f.get('blocked_reason')).trim())
    b.blocked_reason=String(f.get('blocked_reason')).trim();
  if(f.get('priority')!==t.priority)b.priority=f.get('priority');
  const due=f.get('due')||null;if(due!==(t.due||null))b.due=due;
  const tags=String(f.get('tags')).split(/[,，]/).map(s=>s.trim()).filter(Boolean);
  if(tags.join(',')!==(t.tags||[]).join(','))b.tags=tags;
  const rec=String(f.get('recurrence')).trim()||null;
  if(rec!==(t.recurrence||null))b.recurrence=rec;
  if(f.get('estimate')&&+f.get('estimate')!==t.estimate_minutes)b.estimate=+f.get('estimate');
  const pEl=$('#dlgEdit [name=progress]');
  if(!pEl.disabled&&pEl.value!==''&&+pEl.value!==t.progress)b.progress=+pEl.value;
  if(f.get('notes')!==(t.notes||''))b.notes=f.get('notes');
  try{
    if(Object.keys(b).length)
      await api('/api/tasks/'+t.id,{method:'PATCH',body:JSON.stringify(b)});
    const ns=String(f.get('newSubtask')).trim();
    if(ns)await api(`/api/tasks/${t.id}/subtasks`,{method:'POST',
      body:JSON.stringify({title:ns})});
    $('#dlgEdit').close();toast('已保存');await refresh();
  }catch(err){toast(err.message)}
});

function showLogin(){$('#app').hidden=true;$('#login').hidden=false;$('#pw').focus()}
function showApp(){$('#login').hidden=true;$('#app').hidden=false;
  $('#logoutBtn').hidden=!AUTH.auth_required;
  refresh().catch(err=>toast(err.message))}
$('#loginBtn').onclick=async()=>{
  try{await api('/api/auth/login',{method:'POST',
    body:JSON.stringify({password:$('#pw').value})});
    AUTH.authenticated=true;$('#pw').value='';showApp()}
  catch(err){toast(err.message)}};
$('#pw').addEventListener('keydown',e=>{if(e.key==='Enter')$('#loginBtn').click()});
$('#logoutBtn').onclick=async()=>{
  try{await api('/api/auth/logout',{method:'POST'})}catch(e){}
  showLogin()};

(async function init(){
  try{AUTH=await api('/api/status');
    if(AUTH.auth_required&&!AUTH.authenticated)showLogin();else showApp()}
  catch(err){toast('无法连接服务: '+err.message)}
})();
</script>
</body>
</html>
```

- [ ] **Step 3: 运行全部测试确认通过**

Run: `python -m pytest tests/test_serve.py -v`
Expected: 全部 passed（含 `test_webui_smoke`；`test_index_serves_html` 现在走真实文件分支）。

- [ ] **Step 4: 手动快速核对（开发期抽查，完整人工验证在 Task 10）**

Run: `python todo-manager/scripts/serve.py --data-dir %TEMP%\todo-webui-check --port 3007`
浏览器开 `http://127.0.0.1:3007`：新增一个带子任务的任务 → today/all 视图切换 → 勾选子任务（进度条变化）→ 编辑改优先级 → 搜索 → 报表。每步无 JS 报错（F12 控制台）。结束后 Ctrl+C 停服务。

---

### Task 8: references/api.md（HTTP 契约）

**Files:**
- Create: `todo-manager/references/api.md`
- Modify: `todo-manager/references/schema.md`（末尾加一行指引）

**Interfaces:**
- Consumes: Task 1–7 全部已实现行为（文档必须与实现一致，写完后逐条对照测试）。
- Produces: 契约文档；SKILL.md（Task 9）引用它。

- [ ] **Step 1: 写 api.md**

创建 `todo-manager/references/api.md`，完整内容：

````markdown
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
````

- [ ] **Step 2: schema.md 末尾加指引**

在 `todo-manager/references/schema.md` 的「UI 对接建议」一节末尾追加一行：

```markdown
- HTTP 对接：`scripts/serve.py` 提供现成的 HTTP API（含认证与 CORS），契约见 `api.md`——自行开发前端时优先走 API，不必直读文件。
```

- [ ] **Step 3: 契约与实现一致性核对**

逐条对照：api.md 每个端点的示例请求/响应字段名，与 `tests/test_serve.py` 中对应测试的断言一致；`contract_version`、cookie 名、默认端口、CLI 参数名与 serve.py 源码一致。发现不一致，以实现+测试为准修文档。

Run: `python -m pytest tests/test_serve.py -v`
Expected: 全部 passed（本任务只改文档，测试不应变化）。

---

### Task 9: SKILL.md 增补「可视化」部署工作流

**Files:**
- Modify: `todo-manager/SKILL.md`

**Interfaces:**
- Consumes: Task 4/5 的 CLI（auth set/unset、--host、--cors-origin）、Task 8 的 api.md。
- Produces: Agent 可执行的部署决策树（设计文档 §6 的落地文本）。

- [ ] **Step 1: 在 SKILL.md 的「## 错误与恢复」之前插入新节**

完整插入以下内容（保持既有章节不动）：

```markdown
## 可视化（网页查看与管理）

用户说「要可视化 / 网页看任务 / 手机上看」时，启动本技能自带的参考服务
`scripts/serve.py`（零依赖，`python scripts/serve.py` 即可运行），按下面的决策树执行，
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
   - 云服务器 + 已有反向代理和证书：`python scripts/serve.py`（保持 127.0.0.1），
     在反代上加一段转发到 3007 的配置并重载；
   - 云服务器裸环境：`python scripts/serve.py --host 0.0.0.0`，如实告知链接是
     http 无加密、建议日后加反代；安全组需放行端口（Agent 无云商权限时提示用户
     在控制台开一次）；
   - 家用电脑：`python scripts/serve.py --host 0.0.0.0`，交付局域网地址
     `http://<内网IP>:3007`，说明仅同 Wi-Fi 可用；用户要外网访问则需内网穿透
     （Tailscale/frp 等），属于另行处理的事项。
3. **自检后才交付链接**：
   `curl -s -o NUL -w "%{http_code}" http://127.0.0.1:3007/api/tasks` 应为 401；
   `curl -s -o NUL -w "%{http_code}" -u admin:密码 同URL` 应为 200。
4. 常驻（云服务器）：用 systemd 或环境等价物让服务自启、自拉起。

**密码维护**：用户忘了或想换 → 直接重跑 `auth set`（旧会话全部立即失效），没有
找回流程。用户不想要密码了（回到仅本机）→ `python scripts/serve.py auth unset`。

**边界**：undo / archive 等维护操作不在页面里，用户在对话里说即可（走 CLI）。
```

- [ ] **Step 2: 核对**

通读 SKILL.md 全文一遍：新节与既有内容无重复、无冲突；`<skill_dir>` 写法与文件里既有约定一致。

Run: `python -m pytest tests/test_serve.py -v`
Expected: 全部 passed（纯文档任务，回归确认而已）。

---

### Task 10: 全量验证（自动化 + 人工场景走查）

**Files:**
- 无新文件；产出验证结论。

**Interfaces:**
- Consumes: 全部前序任务。

- [ ] **Step 1: 全量自动化测试**

Run: `python -m pytest tests/ -v`
Expected: test_todo.py + test_serve.py 全部 passed（确认对 todo.py 零改动、无回归）。

- [ ] **Step 2: 场景①人工走查（无密码本机）**

```
python todo-manager/scripts/serve.py --data-dir %TEMP%\todo-verify --port 3007
```

浏览器（可用 Playwright 工具代替人手）逐项核对，全程 F12 控制台无报错：

1. 打开 `http://127.0.0.1:3007` → 直接进主界面（无登录页），today 视图默认选中；
2. ＋ 新增「买菜」（P2，今天到期，标签"生活"，子任务"列清单/去超市"）→ today 视图出现，due 高亮为今天色；
3. 勾选子任务"列清单" → 进度条 50%；
4. 点标题打开编辑 → 改优先级 P1、状态 in_progress → 保存 → 卡片显示"进行中"；
5. 编辑 → 状态 blocked → 出现"阻塞原因"框 → 填"等家人清单"保存 → 阻塞视图能看到；
6. 新增第二个任务「交周报」（recurrence=weekly:mon，due=下周一）→ all 视图两条；
7. 勾选完成「买菜」→ all 视图勾"含已完成"可见（划线样式）；
8. 搜索"周报" → 命中 1 条；
9. 报表 tab → 本周完成 ≥1、积压/优先级分布数字合理；
10. 手机宽度（响应式检查，DevTools 切 375px）布局不破。

- [ ] **Step 3: 场景③认证流人工走查（模拟）**

```
echo test123 | python todo-manager/scripts/serve.py auth set --password-stdin --data-dir %TEMP%\todo-verify
python todo-manager/scripts/serve.py --data-dir %TEMP%\todo-verify --port 3007
```

1. 刷新页面 → 出现登录视图；输错密码 → 提示"密码不正确"（且明显有 ~1 秒延迟）；
2. 输对 test123 → 进入主界面，数据还在；刷新页面 → 仍是登录态（cookie 生效）；
3. 点"登出" → 回到登录视图；
4. `curl -s -o NUL -w "%{http_code}" http://127.0.0.1:3007/api/tasks` → 401；
   `curl -s -o NUL -w "%{http_code}" -u admin:test123 同URL` → 200；
   `curl -s -o NUL -w "%{http_code}" -u 随便:test123 同URL` → 200（用户名任意）。

- [ ] **Step 4: 汇报**

向用户汇报：测试数（自动化全绿）、两个场景走查结果截图/要点、已知限制（无
undo/archive 端点、无实时推送——均为设计决定）。**不执行 git commit**，提醒用户
验收后可要求一次性提交（提交信息格式：`[todo-manager] feat: 可视化参考服务（serve.py + webui + api 契约）`）。

---

## Self-Review 结论（写计划时已核）

- **Spec 覆盖**：设计文档 §1（骨架/桥/缓存/错误映射→Task 1-2）、§2（路由→Task 2/3、契约→Task 8）、§3（凭证→Task 4、执行→Task 5、CORS→Task 6、DNS rebinding→Task 5）、§4（页面→Task 7）、§5（CLI→Task 1/4）、§6（SKILL.md→Task 9）、§7（测试→各任务+Task 10）、§8（布局→各任务文件路径）。无遗漏。
- **类型一致性**：`run_todo/cached_todo/auth_section/verify_password/sign_session` 等签名在产出任务与消费任务间逐一核对一致；ROUTES 自 Task 1 起即为 4 元组，避免后期全表改动。
- **占位符扫描**：无 TBD/TODO；所有代码步骤均给出完整代码。
```
