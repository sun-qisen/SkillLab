# -*- coding: utf-8 -*-
"""serve.py 端到端测试：起真进程、发真 HTTP，穿透 HTTP→子进程→todo.py→文件 全链路。"""
import base64
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
    status, data, _ = s.req("GET", "/api/tasks?view=all&tag=%E5%B7%A5%E4%BD%9C")  # "工作"
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


def test_delete_subtask(server):
    server.req("POST", "/api/tasks", body={"title": "父", "subtasks": ["子1", "子2"]})
    status, data, _ = server.req("DELETE", "/api/tasks/1.1")
    assert status == 200 and data["deleted"] == "1.1"
    status, data, _ = server.req("GET", "/api/tasks/1")
    assert [st["id"] for st in data["task"]["subtasks"]] == ["1.2"]


def test_delete_subtask_missing_404(server):
    server.req("POST", "/api/tasks", body={"title": "父", "subtasks": ["子1"]})
    status, data, _ = server.req("DELETE", "/api/tasks/1.9")
    assert status == 404 and data["error"]["code"] == "task_not_found"


def test_delete_top_level_task_405(server):
    """DELETE 只针对子任务；顶层任务 id 不匹配 DELETE 路由 → 405。"""
    server.req("POST", "/api/tasks", body={"title": "父"})
    status, data, _ = server.req("DELETE", "/api/tasks/1")
    assert status == 405 and data["error"]["code"] == "method_not_allowed"


def test_preflight_allows_delete(cors_server):
    _, _, headers = cors_server.req_raw(
        "OPTIONS", "/api/tasks/1.1", headers={"Origin": "https://todo.example.com",
                                              "Access-Control-Request-Method": "DELETE"})
    assert "DELETE" in headers["Access-Control-Allow-Methods"]


def test_write_invalidates_cache(server):
    _, d1, _ = server.req("GET", "/api/tasks?view=all")
    assert d1["count"] == 0
    time.sleep(0.02)
    server.req("POST", "/api/tasks", body={"title": "经 API 写入"})
    _, d2, _ = server.req("GET", "/api/tasks?view=all")
    assert d2["count"] == 1


def test_add_list_fields_must_be_arrays(server):
    for field in ("tags", "subtasks", "inferred"):
        status, data, _ = server.req("POST", "/api/tasks",
                                     body={"title": "x", field: "字符串"})
        assert status == 400, f"{field} 传字符串应 400"
        assert "数组" in data["error"]["message"]


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


# ---------------------------------------------------------------- Task 5

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


def test_garbage_cookie_returns_401_not_500(auth_server):
    """畸形 cookie 含非 ASCII 字符应返回 401，而非 500。"""
    s, _ = auth_server
    for cookie in ("todo_session=1.1.ÿ", "todo_session=..",
                   "todo_session=", "todo_session=a.b.c"):
        status, _, _ = s.req("GET", "/api/tasks?view=all",
                             headers={"Cookie": cookie})
        assert status == 401, f"cookie {cookie!r} 应 401，实得 {status}"


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


# ---------------------------------------------------------------- Task 7

def test_webui_smoke(server):
    status, text, _ = server.req_raw("GET", "/")
    assert status == 200
    for marker in ('id="app"', 'id="login"', "/api/status", "logicalToday"):
        assert marker in text, f"webui.html 缺少关键片段: {marker}"


# ---------------------------------------------------------------- 终审修复

def test_internal_error_sanitized(tmp_path):
    """todo.py 异常退出（非 TodoError）→ 500 通用中文，不回显 traceback。"""
    data_dir = tmp_path / "data"
    s = Server(data_dir)
    try:
        s.req("POST", "/api/tasks", body={"title": "x"})
        time.sleep(0.02)
        (data_dir / "todos.json").write_text("{损坏的json", encoding="utf-8")
        status, data, _ = s.req("GET", "/api/tasks?view=all")
        assert status == 500
        assert data["error"]["code"] == "internal"
        assert "Traceback" not in data["error"]["message"]
        assert "服务内部错误" in data["error"]["message"]
    finally:
        s.stop()


def test_non_string_body_fields_400(server):
    status, data, _ = server.req("POST", "/api/tasks", body={"title": 123})
    assert status == 400
    server.req("POST", "/api/tasks", body={"title": "父"})
    status, _, _ = server.req("POST", "/api/tasks/1/subtasks", body={"title": 123})
    assert status == 400


def test_login_non_string_password_400(auth_server):
    s, _ = auth_server
    status, _, _ = s.req("POST", "/api/auth/login", body={"password": 123})
    assert status == 400
