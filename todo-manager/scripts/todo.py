#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""todo-manager 数据操作 CLI —— 本技能的唯一数据读写入口。

数据目录解析顺序: --data-dir 参数 > $TODO_MANAGER_HOME 环境变量 > ~/.todo-manager
仅使用 Python 标准库；跨平台 (Windows / macOS / Linux / WSL)。
所有子命令支持 --json 输出结构化结果；退出码非 0 表示失败。
"""

import argparse
import calendar
import json
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 1
PRIORITIES = ("P0", "P1", "P2", "P3")
TASK_STATUSES = ("todo", "in_progress", "blocked", "done", "cancelled")
INFERRABLE = ("title", "priority", "due", "tags", "estimate", "recurrence")
LOCK_TIMEOUT = 5.0
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
REC_RE = re.compile(
    r"^(daily|weekly:(mon|tue|wed|thu|fri|sat|sun)|monthly:([1-9]|[12]\d|3[01])|every:[1-9]\d*:days)$"
)
STATUS_ICON = {"todo": "○", "in_progress": "●", "blocked": "◼", "done": "✓", "cancelled": "✗"}


class TodoError(Exception):
    pass


# ---------------------------------------------------------------- 基础设施

def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_date(s, field="due"):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise TodoError(f"{field} 必须是 YYYY-MM-DD 格式，收到: {s!r}")


def resolve_data_dir(explicit):
    if explicit:
        d = Path(explicit)
    elif os.environ.get("TODO_MANAGER_HOME"):
        d = Path(os.environ["TODO_MANAGER_HOME"])
    else:
        d = Path.home() / ".todo-manager"
    d.mkdir(parents=True, exist_ok=True)
    (d / "archive").mkdir(exist_ok=True)
    cfg = d / "config.json"
    if not cfg.exists():
        atomic_write(cfg, {"schema_version": SCHEMA_VERSION, "default_priority": "P2"})
    return d


def atomic_write(path, obj):
    """先写临时文件再 rename，崩溃不损坏原文件。"""
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Lock:
    """写锁：原子创建锁文件实现（不用 fcntl，保证 Windows 可用）。"""

    def __init__(self, data_dir):
        self.path = data_dir / "todos.json.lock"

    def __enter__(self):
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TodoError(
                        f"获取写锁超时({LOCK_TIMEOUT:.0f}s)：{self.path} 被占用。"
                        "若确认没有其它进程在写入，可手动删除该锁文件后重试。"
                    )
                time.sleep(0.1)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def load_db(data_dir):
    f = data_dir / "todos.json"
    if not f.exists():
        return {"schema_version": SCHEMA_VERSION, "next_id": 1, "tasks": []}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise TodoError(
            f"todos.json 解析失败（{e}）。为避免丢数据不会自动重建；"
            "可根据 journal.jsonl 中的操作记录人工重放恢复。"
        )


def save_db(data_dir, db):
    atomic_write(data_dir / "todos.json", db)


def archive_path(data_dir, year):
    return data_dir / "archive" / f"{year}.json"


def load_archive(data_dir, year):
    f = archive_path(data_dir, year)
    if not f.exists():
        return {"schema_version": SCHEMA_VERSION, "tasks": []}
    return json.loads(f.read_text(encoding="utf-8"))


def journal_append(data_dir, entries):
    with (data_dir / "journal.jsonl").open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def journal_entry(txn, op, task_id=None, before=None, after=None, **extra):
    e = {"ts": now_iso(), "txn": txn, "op": op, "task_id": task_id,
         "before": before, "after": after}
    e.update(extra)
    return e


def snapshot(t):
    return json.loads(json.dumps(t))


def diff(before, after):
    """返回 (变化字段的旧值, 变化字段的新值)。"""
    keys = set(before) | set(after)
    b, a = {}, {}
    for k in keys:
        if before.get(k) != after.get(k):
            b[k] = before.get(k)
            a[k] = after.get(k)
    return b, a


# ---------------------------------------------------------------- 领域逻辑

def find_task(db, tid):
    for t in db["tasks"]:
        if t["id"] == tid:
            return t
    return None


def parse_id(s):
    """返回 (父任务 int id, 子任务 id 字符串或 None)。"""
    s = str(s)
    if re.fullmatch(r"\d+", s):
        return int(s), None
    if re.fullmatch(r"\d+\.\d+", s):
        return int(s.split(".")[0]), s
    raise TodoError(f"无效的任务 ID: {s!r}（应为 12 或 12.1 的形式）")


def not_found(db, s):
    cand = sorted(db["tasks"], key=lambda t: t["updated_at"], reverse=True)[:5]
    lines = "\n".join(f"  #{t['id']} {t['title']}" for t in cand) or "  (任务列表为空)"
    raise TodoError(f"找不到任务 {s}。最近的活跃任务:\n{lines}")


def get_task_or_die(db, id_str):
    tid, sid = parse_id(id_str)
    t = find_task(db, tid)
    if t is None:
        not_found(db, id_str)
    if sid is None:
        return t, None
    for st in t.get("subtasks", []):
        if st["id"] == sid:
            return t, st
    not_found(db, id_str)


def recompute_progress(t):
    subs = t.get("subtasks") or []
    if subs:
        t["progress"] = round(100 * sum(1 for s in subs if s["status"] == "done") / len(subs))


def next_subtask_id(t):
    n = 0
    for st in t.get("subtasks", []):
        n = max(n, int(st["id"].split(".")[1]))
    return f"{t['id']}.{n + 1}"


def validate_priority(p):
    if p not in PRIORITIES:
        raise TodoError(f"priority 必须是 {'/'.join(PRIORITIES)} 之一，收到: {p!r}")


def validate_recurrence(r):
    if r and not REC_RE.fullmatch(r):
        raise TodoError(
            "recurrence 仅支持: daily | weekly:mon..sun | monthly:1..31 | every:N:days，"
            f"收到: {r!r}"
        )


def next_due(rule, from_date):
    """下一期截止日，从完成日推算，严格晚于 from_date。"""
    if rule == "daily":
        return from_date + timedelta(days=1)
    if rule.startswith("weekly:"):
        wd = WEEKDAYS[rule.split(":")[1]]
        delta = (wd - from_date.weekday()) % 7 or 7
        return from_date + timedelta(days=delta)
    if rule.startswith("monthly:"):
        day = int(rule.split(":")[1])
        y, m = from_date.year, from_date.month
        cand = date(y, m, min(day, calendar.monthrange(y, m)[1]))
        if cand <= from_date:
            m += 1
            if m == 13:
                m, y = 1, y + 1
            cand = date(y, m, min(day, calendar.monthrange(y, m)[1]))
        return cand
    if rule.startswith("every:"):
        return from_date + timedelta(days=int(rule.split(":")[1]))
    raise TodoError(f"无法解析 recurrence: {rule!r}")


def finalize_done(db, t, txn, lines, entries):
    """把任务置为 done；若为循环任务则生成下一期。返回新任务或 None。"""
    before = snapshot(t)
    rule = t.get("recurrence")
    t["status"] = "done"
    t["completed_at"] = now_iso()
    for st in t.get("subtasks", []):
        st["status"] = "done"
    t["progress"] = 100
    t["blocked_reason"] = None
    t["recurrence"] = None
    t["updated_at"] = now_iso()
    b, a = diff(before, t)
    entries.append(journal_entry(txn, "done", t["id"], b, a))
    lines.append(f"✓ 已完成 #{t['id']}「{t['title']}」")

    spawned = None
    if rule:
        nid_before = db["next_id"]
        spawned = {
            "id": db["next_id"],
            "title": t["title"],
            "notes": t.get("notes", ""),
            "status": "todo",
            "priority": t["priority"],
            "due": next_due(rule, date.today()).isoformat(),
            "progress": 0,
            "tags": list(t.get("tags", [])),
            "estimate_minutes": t.get("estimate_minutes"),
            "recurrence": rule,
            "inferred": [],
            "blocked_reason": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "completed_at": None,
            "subtasks": [],
        }
        for st in t.get("subtasks", []):
            spawned["subtasks"].append({
                "id": f"{spawned['id']}.{len(spawned['subtasks']) + 1}",
                "title": st["title"], "status": "todo", "due": None,
            })
        db["next_id"] += 1
        db["tasks"].append(spawned)
        entries.append(journal_entry(txn, "recur_spawn", spawned["id"],
                                     None, snapshot(spawned), next_id_before=nid_before))
        lines.append(f"↻ 循环任务已生成下一期 #{spawned['id']}，截止 {spawned['due']}")
    return spawned


# ---------------------------------------------------------------- 渲染

def fmt_due(due_str):
    if not due_str:
        return ""
    d = parse_date(due_str)
    delta = (d - date.today()).days
    if delta < 0:
        return f"过期 {-delta} 天"
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    return f"截止 {d.strftime('%m-%d')}"


def fmt_estimate(minutes):
    if not minutes:
        return ""
    h, m = divmod(int(minutes), 60)
    return "~" + (f"{h}h{m}m" if h and m else f"{h}h" if h else f"{m}m")


def is_overdue(t):
    return bool(t.get("due")) and parse_date(t["due"]) < date.today() \
        and t["status"] not in ("done", "cancelled")


def render_line(t):
    icon = "⚠" if is_overdue(t) else STATUS_ICON[t["status"]]
    parts = [f"{icon} #{t['id']:<3} {t['priority']} {t['title']}"]
    if t.get("due"):
        parts.append(fmt_due(t["due"]))
    subs = t.get("subtasks") or []
    if subs:
        done = sum(1 for s in subs if s["status"] == "done")
        filled = round(4 * done / len(subs))
        parts.append(f"[{'█' * filled}{'░' * (4 - filled)}] {done}/{len(subs)}")
    elif t.get("progress"):
        parts.append(f"{t['progress']}%")
    if t.get("estimate_minutes"):
        parts.append(fmt_estimate(t["estimate_minutes"]))
    if t["status"] == "blocked" and t.get("blocked_reason"):
        parts.append(f"(阻塞: {t['blocked_reason']})")
    return "  ".join(parts)


def render_detail(t):
    lines = [f"#{t['id']} {t['title']}",
             f"  状态: {t['status']}   优先级: {t['priority']}   进度: {t['progress']}%"]
    if t.get("due"):
        lines.append(f"  截止: {t['due']} ({fmt_due(t['due'])})")
    if t.get("tags"):
        lines.append(f"  标签: {', '.join(t['tags'])}")
    if t.get("estimate_minutes"):
        lines.append(f"  预估: {fmt_estimate(t['estimate_minutes'])}")
    if t.get("recurrence"):
        lines.append(f"  循环: {t['recurrence']}")
    if t.get("inferred"):
        lines.append(f"  推断字段: {', '.join(t['inferred'])}")
    if t.get("blocked_reason"):
        lines.append(f"  阻塞原因: {t['blocked_reason']}")
    if t.get("notes"):
        lines.append(f"  备注: {t['notes']}")
    for st in t.get("subtasks", []):
        mark = "✓" if st["status"] == "done" else "○"
        due = f"  ({fmt_due(st['due'])})" if st.get("due") else ""
        lines.append(f"    {mark} {st['id']} {st['title']}{due}")
    return lines


def sort_key(t):
    return (0 if is_overdue(t) else 1,
            PRIORITIES.index(t["priority"]),
            t.get("due") or "9999-12-31",
            t["id"])


# ---------------------------------------------------------------- 子命令

def cmd_add(ns, data_dir):
    if ns.priority:
        validate_priority(ns.priority)
    if ns.due:
        parse_date(ns.due)
    validate_recurrence(ns.recurrence)
    inferred = [s.strip() for s in (ns.inferred or "").split(",") if s.strip()]
    for f in inferred:
        if f not in INFERRABLE:
            raise TodoError(f"--inferred 仅接受 {','.join(INFERRABLE)}，收到: {f!r}")
    if ns.estimate is not None and ns.estimate <= 0:
        raise TodoError("--estimate 必须是正整数（分钟）")

    txn = uuid.uuid4().hex[:8]
    with Lock(data_dir):
        db = load_db(data_dir)
        nid_before = db["next_id"]
        t = {
            "id": db["next_id"],
            "title": ns.title,
            "notes": ns.notes or "",
            "status": "todo",
            "priority": ns.priority or "P2",
            "due": ns.due,
            "progress": 0,
            "tags": [s.strip() for s in (ns.tags or "").split(",") if s.strip()],
            "estimate_minutes": ns.estimate,
            "recurrence": ns.recurrence,
            "inferred": inferred,
            "blocked_reason": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "completed_at": None,
            "subtasks": [],
        }
        for sub in ns.subtask or []:
            t["subtasks"].append({"id": f"{t['id']}.{len(t['subtasks']) + 1}",
                                  "title": sub, "status": "todo", "due": None})
        db["next_id"] += 1
        db["tasks"].append(t)
        save_db(data_dir, db)
        journal_append(data_dir, [journal_entry(txn, "add", t["id"], None,
                                                snapshot(t), next_id_before=nid_before)])
    lines = [f"已添加 #{t['id']}「{t['title']}」 优先级 {t['priority']}"
             + (f"，截止 {t['due']}" if t["due"] else "")
             + (f"，含 {len(t['subtasks'])} 个子任务" if t["subtasks"] else "")]
    if inferred:
        lines.append(f"（推断字段: {', '.join(inferred)}，用户可随时修改）")
    return {"task": t}, lines


def pick_view(db, view):
    tasks = db["tasks"]
    active = [t for t in tasks if t["status"] not in ("done", "cancelled")]
    if view == "today":
        return [t for t in active
                if is_overdue(t)
                or (t.get("due") and parse_date(t["due"]) == date.today())
                or t["status"] == "in_progress"]
    if view == "week":
        week_end = date.today() + timedelta(days=6 - date.today().weekday())
        return [t for t in active
                if (t.get("due") and parse_date(t["due"]) <= week_end)
                or (not t.get("due") and t["priority"] in ("P0", "P1"))]
    if view == "blocked":
        return [t for t in active if t["status"] == "blocked"]
    return list(tasks)


def cmd_list(ns, data_dir):
    db = load_db(data_dir)
    sel = pick_view(db, ns.view)
    if ns.status:
        if ns.status not in TASK_STATUSES:
            raise TodoError(f"--status 必须是 {'/'.join(TASK_STATUSES)} 之一")
        sel = [t for t in sel if t["status"] == ns.status]
    if ns.tag:
        sel = [t for t in sel if ns.tag in t.get("tags", [])]
    if ns.overdue:
        sel = [t for t in sel if is_overdue(t)]
    sel.sort(key=sort_key)

    pending = [t for t in sel if t["status"] not in ("done", "cancelled")]
    overdue_n = sum(1 for t in sel if is_overdue(t))
    header = {"today": "今天", "week": "本周", "blocked": "阻塞中", "all": "全部"}[ns.view]
    lines = [f"📋 {header} ({date.today().isoformat()}) · {len(pending)} 项待办"
             + (f"，{overdue_n} 项过期" if overdue_n else "")]
    lines.append("─" * 38)
    lines += [render_line(t) for t in sel] or ["(没有匹配的任务)"]
    return {"view": ns.view, "count": len(sel), "tasks": sel}, lines


def cmd_show(ns, data_dir):
    db = load_db(data_dir)
    t, st = get_task_or_die(db, ns.id)
    return {"task": t}, render_detail(t)


def apply_status(db, t, new_status, reason, lines, txn, entries):
    if new_status not in TASK_STATUSES:
        raise TodoError(f"status 必须是 {'/'.join(TASK_STATUSES)} 之一，收到: {new_status!r}")
    if new_status == "blocked" and not (reason or t.get("blocked_reason")):
        raise TodoError("status 设为 blocked 时必须提供 --blocked-reason 说明原因")
    if new_status == "done":
        return finalize_done(db, t, txn, lines, entries)
    before = snapshot(t)
    t["status"] = new_status
    t["blocked_reason"] = reason if new_status == "blocked" else None
    if before["status"] in ("done", "cancelled") and new_status not in ("done", "cancelled"):
        t["completed_at"] = None
        recompute_progress(t)
    t["updated_at"] = now_iso()
    b, a = diff(before, t)
    if b:
        entries.append(journal_entry(txn, "update", t["id"], b, a))
    lines.append(f"#{t['id']} 状态: {before['status']} → {new_status}")
    return None


def cmd_update(ns, data_dir):
    txn = uuid.uuid4().hex[:8]
    lines, entries = [], []
    with Lock(data_dir):
        db = load_db(data_dir)
        t, st = get_task_or_die(db, ns.id)

        if st is not None:  # 子任务：仅 title / due / status(todo|done)
            forbidden = [o for o, v in [("--priority", ns.priority), ("--progress", ns.progress),
                                        ("--notes", ns.notes), ("--estimate", ns.estimate),
                                        ("--tags", ns.tags), ("--recurrence", ns.recurrence)]
                         if v is not None]
            if forbidden:
                raise TodoError(f"子任务仅支持 --title/--due/--status，不支持: {', '.join(forbidden)}")
            before = snapshot(t)
            if ns.title:
                st["title"] = ns.title
            if ns.due is not None:
                st["due"] = None if ns.due in ("", "none") else parse_date(ns.due) and ns.due
            if ns.status is not None:
                if ns.status not in ("todo", "done"):
                    raise TodoError("子任务状态只能是 todo 或 done")
                st["status"] = ns.status
                recompute_progress(t)
            t["updated_at"] = now_iso()
            b, a = diff(before, t)
            if b:
                entries.append(journal_entry(txn, "update", t["id"], b, a))
            lines.append(f"已更新子任务 {st['id']}「{st['title']}」")
        else:
            before = snapshot(t)
            explicit = []
            if ns.title:
                t["title"] = ns.title
                explicit.append("title")
            if ns.notes is not None:
                t["notes"] = ns.notes
            if ns.priority is not None:
                validate_priority(ns.priority)
                t["priority"] = ns.priority
                explicit.append("priority")
            if ns.due is not None:
                t["due"] = None if ns.due in ("", "none") else (parse_date(ns.due) and ns.due)
                explicit.append("due")
            if ns.tags is not None:
                t["tags"] = [s.strip() for s in ns.tags.split(",") if s.strip()]
                explicit.append("tags")
            if ns.estimate is not None:
                if ns.estimate <= 0:
                    raise TodoError("--estimate 必须是正整数（分钟）")
                t["estimate_minutes"] = ns.estimate
                explicit.append("estimate")
            if ns.recurrence is not None:
                r = None if ns.recurrence in ("", "none") else ns.recurrence
                validate_recurrence(r)
                t["recurrence"] = r
                explicit.append("recurrence")
            if ns.progress is not None:
                if t.get("subtasks"):
                    raise TodoError(
                        "该任务有子任务，进度由子任务完成比例自动计算，不能手动设置 --progress。"
                        "请改为完成子任务（todo.py done <id>.<n>）。")
                if not 0 <= ns.progress <= 100:
                    raise TodoError("--progress 必须在 0-100 之间")
                t["progress"] = ns.progress
            # 用户显式修改过的字段不再是"推断值"
            t["inferred"] = [f for f in t.get("inferred", []) if f not in explicit]
            spawned = None
            if ns.status is not None and ns.status != before["status"]:
                spawned = apply_status(db, t, ns.status, ns.blocked_reason, lines, txn, entries)
            else:
                t["updated_at"] = now_iso()
            b, a = diff(before, t)
            if spawned:  # finalize_done 已记录 done/recur_spawn 日志
                pass
            elif ns.status is not None and ns.status != before["status"]:
                pass  # apply_status 已记录
            elif b:
                entries.append(journal_entry(txn, "update", t["id"], b, a))
            if not lines:
                lines.append(f"已更新 #{t['id']}「{t['title']}」")
        save_db(data_dir, db)
        journal_append(data_dir, entries)
    return {"task": t}, lines


def cmd_done(ns, data_dir):
    txn = uuid.uuid4().hex[:8]
    lines, entries = [], []
    with Lock(data_dir):
        db = load_db(data_dir)
        t, st = get_task_or_die(db, ns.id)
        if st is not None:
            if st["status"] == "done":
                raise TodoError(f"子任务 {st['id']}「{st['title']}」已经是完成状态")
            before = snapshot(t)
            st["status"] = "done"
            recompute_progress(t)
            t["updated_at"] = now_iso()
            b, a = diff(before, t)
            entries.append(journal_entry(txn, "done", t["id"], b, a))
            done_n = sum(1 for s in t["subtasks"] if s["status"] == "done")
            lines.append(f"✓ 子任务 {st['id']}「{st['title']}」完成，"
                         f"#{t['id']} 进度 {t['progress']}% ({done_n}/{len(t['subtasks'])})")
            if all(s["status"] == "done" for s in t["subtasks"]):
                lines.append(f"  所有子任务已完成。如整个任务已完成，运行: done {t['id']}")
        else:
            if t["status"] == "done":
                raise TodoError(f"#{t['id']}「{t['title']}」已经是完成状态")
            finalize_done(db, t, txn, lines, entries)
        save_db(data_dir, db)
        journal_append(data_dir, entries)
    return {"task": t}, lines


def cmd_subtask(ns, data_dir):
    if ns.subcmd != "add":
        raise TodoError("subtask 目前仅支持 add 子命令")
    txn = uuid.uuid4().hex[:8]
    with Lock(data_dir):
        db = load_db(data_dir)
        t, st = get_task_or_die(db, ns.id)
        if st is not None:
            raise TodoError("不能给子任务再添加子任务（仅支持两层结构）")
        if ns.due:
            parse_date(ns.due)
        before = snapshot(t)
        new = {"id": next_subtask_id(t), "title": ns.title, "status": "todo",
               "due": ns.due}
        t.setdefault("subtasks", []).append(new)
        recompute_progress(t)
        t["updated_at"] = now_iso()
        b, a = diff(before, t)
        save_db(data_dir, db)
        journal_append(data_dir, [journal_entry(txn, "subtask_add", t["id"], b, a)])
    return {"task": t, "subtask": new}, \
        [f"已添加子任务 {new['id']}「{new['title']}」到 #{t['id']}，当前进度 {t['progress']}%"]


def cmd_search(ns, data_dir):
    q = ns.query.lower()

    def match(t):
        if q in t["title"].lower() or q in (t.get("notes") or "").lower():
            return True
        return any(q in st["title"].lower() for st in t.get("subtasks", []))

    db = load_db(data_dir)
    hits = [(t, None) for t in db["tasks"] if match(t)]
    for f in sorted((data_dir / "archive").glob("*.json")):
        arch = json.loads(f.read_text(encoding="utf-8"))
        hits += [(t, f.stem) for t in arch.get("tasks", []) if match(t)]

    lines = [f"🔍 “{ns.query}” 命中 {len(hits)} 项"]
    for t, src in hits:
        tag = f" [归档 {src}]" if src else ""
        lines.append(render_line(t) + tag)
    if not hits:
        lines.append("(无结果)")
    return {"query": ns.query,
            "matches": [dict(t, _source=("archive/" + s if s else "active")) for t, s in hits]}, lines


def cmd_report(ns, data_dir):
    db = load_db(data_dir)
    tasks = db["tasks"]
    active = [t for t in tasks if t["status"] not in ("done", "cancelled")]
    week_start = date.today() - timedelta(days=date.today().weekday())

    def completed_this_week(t):
        if not t.get("completed_at"):
            return False
        return week_start <= datetime.fromisoformat(t["completed_at"]).date() <= date.today()

    done_week = [t for t in tasks if completed_this_week(t)]
    for f in sorted((data_dir / "archive").glob("*.json")):
        arch = json.loads(f.read_text(encoding="utf-8"))
        done_week += [t for t in arch.get("tasks", []) if completed_this_week(t)]

    by_status = {s: sum(1 for t in active if t["status"] == s) for s in ("todo", "in_progress", "blocked")}
    by_prio = {p: sum(1 for t in active if t["priority"] == p) for p in PRIORITIES}
    overdue = [t for t in active if is_overdue(t)]
    est = sum(t.get("estimate_minutes") or 0 for t in active)

    lines = [f"📈 报告 ({date.today().isoformat()}，本周从 {week_start.isoformat()} 起)",
             "─" * 38,
             f"本周完成: {len(done_week)} 项"]
    lines += [f"  ✓ {t['title']}" for t in done_week]
    lines += [f"积压: {len(active)} 项 "
              f"(todo {by_status['todo']} / 进行中 {by_status['in_progress']} / 阻塞 {by_status['blocked']})",
              f"过期: {len(overdue)} 项" + ("" if not overdue else
                                            " — " + ", ".join(f"#{t['id']} {t['title']}" for t in overdue)),
              "优先级分布: " + "  ".join(f"{p}:{n}" for p, n in by_prio.items()),
              f"活跃任务预估总耗时: {fmt_estimate(est) or '0m'}"]
    return {"completed_this_week": done_week, "active": len(active),
            "by_status": by_status, "by_priority": by_prio,
            "overdue": len(overdue), "estimate_minutes_total": est}, lines


def cmd_archive(ns, data_dir):
    before_d = parse_date(ns.before, "--before") if ns.before else None
    txn = uuid.uuid4().hex[:8]
    with Lock(data_dir):
        db = load_db(data_dir)
        movable = []
        for t in db["tasks"]:
            if t["status"] not in ("done", "cancelled"):
                continue
            ref = t.get("completed_at") or t["updated_at"]
            ref_d = datetime.fromisoformat(ref).date()
            if before_d and ref_d > before_d:
                continue
            movable.append((t, ref_d.year))
        if not movable:
            return {"archived": 0}, ["没有可归档的任务（仅 done/cancelled 状态可归档）"]
        moved = {}
        for t, year in movable:
            moved.setdefault(str(year), []).append(snapshot(t))
            db["tasks"].remove(t)
        for year, ts in moved.items():
            arch = load_archive(data_dir, year)
            arch["tasks"].extend(ts)
            atomic_write(archive_path(data_dir, year), arch)
        save_db(data_dir, db)
        journal_append(data_dir, [journal_entry(txn, "archive", None, None,
                                                {"count": len(movable)}, moved=moved)])
    n = len(movable)
    return {"archived": n, "years": sorted(moved)}, \
        [f"已归档 {n} 项到 " + ", ".join(f"archive/{y}.json" for y in sorted(moved))]


def cmd_undo(ns, data_dir):
    jf = data_dir / "journal.jsonl"
    entries = []
    if jf.exists():
        entries = [json.loads(l) for l in jf.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not entries or entries[-1]["op"] == "undo":
        raise TodoError("没有可撤销的操作")
    txn = entries[-1]["txn"]
    group = [e for e in entries if e["txn"] == txn]

    lines = []
    with Lock(data_dir):
        db = load_db(data_dir)
        for e in reversed(group):
            op = e["op"]
            if op in ("add", "recur_spawn"):
                db["tasks"] = [t for t in db["tasks"] if t["id"] != e["task_id"]]
                if e.get("next_id_before") is not None:
                    db["next_id"] = e["next_id_before"]
                lines.append(f"↩ 已移除任务 #{e['task_id']}")
            elif op in ("update", "done", "subtask_add"):
                t = find_task(db, e["task_id"])
                if t is None:
                    raise TodoError(f"撤销失败：任务 #{e['task_id']} 已不在活跃列表（可能已归档）")
                for k, v in (e["before"] or {}).items():
                    if v is None and k not in ("due", "completed_at", "blocked_reason",
                                               "estimate_minutes", "recurrence", "notes"):
                        t.pop(k, None)
                    else:
                        t[k] = v
                lines.append(f"↩ 已回滚 #{t['id']}「{t['title']}」的 {op} 操作")
            elif op == "archive":
                for year, ts in (e.get("moved") or {}).items():
                    arch = load_archive(data_dir, year)
                    ids = {t["id"] for t in ts}
                    arch["tasks"] = [t for t in arch["tasks"] if t["id"] not in ids]
                    atomic_write(archive_path(data_dir, year), arch)
                    db["tasks"].extend(ts)
                lines.append(f"↩ 已取消归档，恢复 {e['after']['count']} 项")
            else:
                raise TodoError(f"无法撤销未知操作类型: {op}")
        save_db(data_dir, db)
        journal_append(data_dir, [journal_entry(uuid.uuid4().hex[:8], "undo",
                                                None, None, None, undone_txn=txn)])
    return {"undone_txn": txn, "ops": [e["op"] for e in group]}, lines


# ---------------------------------------------------------------- 入口

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", help="数据目录（默认 $TODO_MANAGER_HOME 或 ~/.todo-manager）")
    common.add_argument("--json", action="store_true", help="输出 JSON")

    p = argparse.ArgumentParser(prog="todo.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", parents=[common], help="添加任务")
    a.add_argument("title")
    a.add_argument("--due")
    a.add_argument("--priority")
    a.add_argument("--tags")
    a.add_argument("--notes")
    a.add_argument("--estimate", type=int, help="预估耗时（分钟）")
    a.add_argument("--recurrence", help="daily | weekly:mon..sun | monthly:1..31 | every:N:days")
    a.add_argument("--subtask", action="append", help="可重复，添加子任务")
    a.add_argument("--inferred", help="逗号分隔的推断字段名，如 priority,due")
    a.set_defaults(fn=cmd_add)

    l = sub.add_parser("list", parents=[common], help="查看任务视图")
    l.add_argument("--view", choices=["today", "week", "all", "blocked"], default="all")
    l.add_argument("--status")
    l.add_argument("--tag")
    l.add_argument("--overdue", action="store_true")
    l.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", parents=[common], help="任务详情")
    s.add_argument("id")
    s.set_defaults(fn=cmd_show)

    u = sub.add_parser("update", parents=[common], help="更新任务/子任务")
    u.add_argument("id")
    u.add_argument("--title")
    u.add_argument("--due", help="YYYY-MM-DD，或 none 清除")
    u.add_argument("--priority")
    u.add_argument("--status")
    u.add_argument("--progress", type=int)
    u.add_argument("--notes")
    u.add_argument("--tags")
    u.add_argument("--estimate", type=int)
    u.add_argument("--recurrence")
    u.add_argument("--blocked-reason", dest="blocked_reason")
    u.set_defaults(fn=cmd_update)

    d = sub.add_parser("done", parents=[common], help="完成任务或子任务（12 或 12.1）")
    d.add_argument("id")
    d.set_defaults(fn=cmd_done)

    st = sub.add_parser("subtask", parents=[common], help="子任务操作")
    st.add_argument("subcmd", choices=["add"])
    st.add_argument("id", help="父任务 ID")
    st.add_argument("title")
    st.add_argument("--due")
    st.set_defaults(fn=cmd_subtask)

    se = sub.add_parser("search", parents=[common], help="全文搜索（含归档）")
    se.add_argument("query")
    se.set_defaults(fn=cmd_search)

    r = sub.add_parser("report", parents=[common], help="统计报告")
    r.add_argument("--week", action="store_true")
    r.set_defaults(fn=cmd_report)

    ar = sub.add_parser("archive", parents=[common], help="归档 done/cancelled 任务")
    ar.add_argument("--before", help="仅归档完成日期不晚于该日期的任务")
    ar.set_defaults(fn=cmd_archive)

    un = sub.add_parser("undo", parents=[common], help="撤销上一次写操作")
    un.set_defaults(fn=cmd_undo)
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ns = build_parser().parse_args(argv)
    try:
        data_dir = resolve_data_dir(ns.data_dir)
        result, lines = ns.fn(ns, data_dir)
    except TodoError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    if ns.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
