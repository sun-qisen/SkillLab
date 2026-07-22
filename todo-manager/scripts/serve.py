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
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
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
    raw = (proc.stderr or "").strip()
    if raw.startswith("错误: "):
        msg = raw[len("错误: "):]
        if "找不到任务" in msg:
            raise ApiError(404, "task_not_found", msg)
        if "获取写锁超时" in msg:
            raise ApiError(409, "locked", msg)
        # 解析失败、Traceback 等非预期错误 → 500；业务验证错误 → 400
        if any(x in raw for x in ("解析失败", "Traceback", "异常")):
            print(f"todo.py 异常退出({proc.returncode}): {raw}", flush=True)
            raise ApiError(500, "internal", "服务内部错误：todo.py 执行失败，详情见服务日志")
        raise ApiError(400, "invalid_request", msg)
    print(f"todo.py 异常退出({proc.returncode}): {raw}", flush=True)
    raise ApiError(500, "internal", "服务内部错误：todo.py 执行失败，详情见服务日志")


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


def resolve_data_dir(explicit):
    """与 todo.py 相同的解析顺序: 参数 > $TODO_MANAGER_HOME > ~/.todo-manager"""
    if explicit:
        return Path(explicit)
    if os.environ.get("TODO_MANAGER_HOME"):
        return Path(os.environ["TODO_MANAGER_HOME"])
    return Path.home() / ".todo-manager"


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
    try:
        os.chmod(tmp, 0o600)  # 先收紧再上位，避免宽权限窗口；Windows 上尽力而为
    except OSError:
        pass
    os.replace(tmp, p)


def auth_section(cfg):
    return (cfg or {}).get("auth") or None


LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


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
    try:
        if not hmac.compare_digest(sig, expected):
            return False
        return int(gen_s) == auth["generation"] and int(exp_s) > time.time()
    except (TypeError, ValueError):
        return False


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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "todo-serve/" + VERSION
    CLEARABLE = {"due", "recurrence"}  # PATCH 传 null 可清除的字段（对应 CLI 的 "none"）

    # ---- CORS
    def cors_headers(self):
        origin = self.headers.get("Origin")
        if origin and origin in self.server.cors_origins:
            return [("Access-Control-Allow-Origin", origin),
                    ("Access-Control-Allow-Credentials", "true"),
                    ("Vary", "Origin")]
        return []

    # ---- 基础设施
    def log_message(self, fmt, *args):
        print(f"{self.log_date_time_string()} {fmt % args}", flush=True)

    def send_json(self, status, obj, extra_headers=()):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in self.cors_headers():
            self.send_header(k, v)
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, code, message):
        self.send_json(status, {"error": {"code": code, "message": message}})

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

    # ---- 分发
    def handle_one(self, method):
        url = urlparse(self.path)
        self.query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            self.cfg = load_serve_config(self.server.data_dir)
            if not auth_section(self.cfg):
                self.check_host_header()  # 仅无密码模式做 DNS rebinding 防御
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
        except Exception as e:  # 单个请求的异常不打死服务；原文只进日志
            print(f"未处理异常: {type(e).__name__}: {e}", flush=True)
            return self.send_error_json(500, "internal", "服务内部错误，详情见服务日志")

    def do_GET(self):
        self.handle_one("GET")

    def do_POST(self):
        self.handle_one("POST")

    def do_PATCH(self):
        self.handle_one("PATCH")

    def do_DELETE(self):
        self.handle_one("DELETE")

    def do_OPTIONS(self):
        # 跨域预检免认证放行（设计文档 §3）；非白名单来源不给任何 CORS 头
        headers = self.cors_headers()
        self.send_response(204)
        for k, v in headers:
            self.send_header(k, v)
        if headers:
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

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
                             "auth_required": bool(auth_section(self.cfg)),
                             "authenticated": self.is_authenticated()})

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

    def h_add(self):
        b = self.read_body()
        title = b.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ApiError(400, "invalid_request", "title 必填且必须是字符串")
        title = title.strip()
        if "tags" in b and not isinstance(b["tags"], list):
            raise ApiError(400, "invalid_request", "tags 必须是数组")
        if "subtasks" in b and not isinstance(b["subtasks"], list):
            raise ApiError(400, "invalid_request", "subtasks 必须是数组")
        if "inferred" in b and not isinstance(b["inferred"], list):
            raise ApiError(400, "invalid_request", "inferred 必须是数组")
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
        title = b.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ApiError(400, "invalid_request", "title 必填且必须是字符串")
        title = title.strip()
        args = ["subtask", "add", id, title]
        if b.get("due"):
            args += ["--due", str(b["due"])]
        self.send_json(200, run_todo(self.server.data_dir, args))

    def h_subtask_delete(self, id):
        self.send_json(200, run_todo(self.server.data_dir, ["subtask", "delete", id]))

    def h_login(self):
        if not auth_section(self.cfg):
            raise ApiError(400, "auth_not_configured", "服务未设置密码，无需登录")
        b = self.read_body()
        password = b.get("password") or ""
        if not isinstance(password, str):
            raise ApiError(400, "invalid_request", "password 必须是字符串")
        if not verify_password(self.cfg, password):
            time.sleep(1)  # 失败统一慢 1 秒，抵抗暴力猜测；不持锁避免并发失败互相放大挂起时长
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


# (method, 路径正则, Handler 方法名, 是否需要认证)
ROUTES = [
    ("GET", re.compile(r"^/api/status$"), "h_status", False),
    ("GET", re.compile(r"^/api/tasks$"), "h_list", True),
    ("GET", re.compile(r"^/api/tasks/(?P<id>\d+(?:\.\d+)?)$"), "h_show", True),
    ("GET", re.compile(r"^/api/search$"), "h_search", True),
    ("GET", re.compile(r"^/api/report$"), "h_report", True),
    ("POST", re.compile(r"^/api/tasks$"), "h_add", True),
    ("PATCH", re.compile(r"^/api/tasks/(?P<id>\d+(?:\.\d+)?)$"), "h_update", True),
    ("POST", re.compile(r"^/api/tasks/(?P<id>\d+(?:\.\d+)?)/done$"), "h_done", True),
    ("POST", re.compile(r"^/api/tasks/(?P<id>\d+)/subtasks$"), "h_subtask_add", True),
    ("DELETE", re.compile(r"^/api/tasks/(?P<id>\d+\.\d+)$"), "h_subtask_delete", True),
    ("POST", re.compile(r"^/api/auth/login$"), "h_login", False),
    ("POST", re.compile(r"^/api/auth/logout$"), "h_logout", True),
]


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
    if ns.host not in LOCAL_HOSTNAMES and not auth_section(load_serve_config(data_dir)):
        print("错误: 监听非本机地址之前必须先设置访问密码: python serve.py auth set",
              file=sys.stderr)
        return 2
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
