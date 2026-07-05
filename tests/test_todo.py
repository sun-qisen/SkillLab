# -*- coding: utf-8 -*-
"""todo.py 单元测试：通过真实 CLI（subprocess）验证，覆盖 spec 第 7 节要求。"""
import calendar
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "todo-manager" / "scripts" / "todo.py"
TODAY = date.today()


def run(tmp, *args):
    env = dict(os.environ)
    env.pop("TODO_MANAGER_HOME", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--data-dir", str(tmp)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)


def ok(tmp, *args):
    r = run(tmp, *args)
    assert r.returncode == 0, f"命令失败: {args}\nstderr: {r.stderr}"
    return r


def fail(tmp, *args):
    r = run(tmp, *args)
    assert r.returncode != 0, f"预期失败但成功了: {args}\nstdout: {r.stdout}"
    return r


def runj(tmp, *args):
    return json.loads(ok(tmp, *args, "--json").stdout)


def read_db(tmp):
    return json.loads((Path(tmp) / "todos.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- add

def test_add_defaults_and_inferred(tmp_path):
    t = runj(tmp_path, "add", "给客户回邮件", "--inferred", "priority,due")["task"]
    assert t["id"] == 1
    assert t["status"] == "todo"
    assert t["priority"] == "P2"
    assert t["due"] is None
    assert t["progress"] == 0
    assert t["inferred"] == ["priority", "due"]
    db = read_db(tmp_path)
    assert db["schema_version"] == 1 and db["next_id"] == 2


def test_add_with_subtasks(tmp_path):
    t = runj(tmp_path, "add", "订机票和酒店", "--subtask", "订机票", "--subtask", "订酒店")["task"]
    assert [s["id"] for s in t["subtasks"]] == ["1.1", "1.2"]
    assert all(s["status"] == "todo" for s in t["subtasks"])


def test_add_validation_errors(tmp_path):
    assert "priority" in fail(tmp_path, "add", "x", "--priority", "P9").stderr
    assert "YYYY-MM-DD" in fail(tmp_path, "add", "x", "--due", "明天").stderr
    assert "recurrence" in fail(tmp_path, "add", "x", "--recurrence", "fortnightly").stderr
    assert "--inferred" in fail(tmp_path, "add", "x", "--inferred", "mood").stderr
    assert not (tmp_path / "todos.json").exists()  # 失败不落盘


# ---------------------------------------------------------------- list 视图

def seed_views(tmp):
    ok(tmp, "add", "过期任务", "--due", (TODAY - timedelta(days=2)).isoformat())          # 1
    ok(tmp, "add", "今天到期", "--due", TODAY.isoformat())                                # 2
    ok(tmp, "add", "进行中无日期")                                                        # 3
    ok(tmp, "update", "3", "--status", "in_progress")
    ok(tmp, "add", "下月的事", "--due", (TODAY + timedelta(days=40)).isoformat())         # 4
    ok(tmp, "add", "无日期P1", "--priority", "P1")                                        # 5
    ok(tmp, "add", "被阻塞", "--due", TODAY.isoformat())                                  # 6
    ok(tmp, "update", "6", "--status", "blocked", "--blocked-reason", "等审批")


def test_list_today_view(tmp_path):
    seed_views(tmp_path)
    ids = {t["id"] for t in runj(tmp_path, "list", "--view", "today")["tasks"]}
    assert ids == {1, 2, 3, 6}  # 过期 + 今日 + 进行中；不含远期和无日期P1


def test_list_week_and_blocked(tmp_path):
    seed_views(tmp_path)
    week_ids = {t["id"] for t in runj(tmp_path, "list", "--view", "week")["tasks"]}
    assert {1, 2, 5, 6} <= week_ids and 4 not in week_ids  # 无日期P1 在周视图
    blocked = runj(tmp_path, "list", "--view", "blocked")["tasks"]
    assert [t["id"] for t in blocked] == [6]


def test_list_sorting_overdue_first(tmp_path):
    seed_views(tmp_path)
    tasks = runj(tmp_path, "list", "--view", "today")["tasks"]
    assert tasks[0]["id"] == 1  # 过期的排最前


# ---------------------------------------------------------------- done 与进度

def test_done_subtask_recomputes_progress(tmp_path):
    ok(tmp_path, "add", "大任务", "--subtask", "a", "--subtask", "b")
    ok(tmp_path, "done", "1.1")
    t = runj(tmp_path, "show", "1")["task"]
    assert t["progress"] == 50
    assert t["status"] == "todo"  # 父任务状态不自动变
    r = ok(tmp_path, "done", "1.2")
    assert "所有子任务已完成" in r.stdout
    assert runj(tmp_path, "show", "1")["task"]["progress"] == 100


def test_done_task_finalizes(tmp_path):
    ok(tmp_path, "add", "任务", "--subtask", "没做完的")
    ok(tmp_path, "done", "1")
    t = runj(tmp_path, "show", "1")["task"]
    assert t["status"] == "done" and t["progress"] == 100
    assert t["completed_at"] is not None
    assert t["subtasks"][0]["status"] == "done"  # 未完成子任务一并完成


def test_done_twice_errors(tmp_path):
    ok(tmp_path, "add", "任务")
    ok(tmp_path, "done", "1")
    assert "已经是完成状态" in fail(tmp_path, "done", "1").stderr


def test_manual_progress_rules(tmp_path):
    ok(tmp_path, "add", "有子任务", "--subtask", "a")
    assert "自动计算" in fail(tmp_path, "update", "1", "--progress", "30").stderr
    ok(tmp_path, "add", "无子任务")
    ok(tmp_path, "update", "2", "--progress", "60")
    assert runj(tmp_path, "show", "2")["task"]["progress"] == 60
    assert fail(tmp_path, "update", "2", "--progress", "150").returncode == 1


# ---------------------------------------------------------------- update / 状态机

def test_blocked_requires_reason_and_clears(tmp_path):
    ok(tmp_path, "add", "任务")
    assert "blocked-reason" in fail(tmp_path, "update", "1", "--status", "blocked").stderr
    ok(tmp_path, "update", "1", "--status", "blocked", "--blocked-reason", "等依赖")
    assert runj(tmp_path, "show", "1")["task"]["blocked_reason"] == "等依赖"
    ok(tmp_path, "update", "1", "--status", "in_progress")
    assert runj(tmp_path, "show", "1")["task"]["blocked_reason"] is None


def test_reopen_done_task(tmp_path):
    ok(tmp_path, "add", "任务")
    ok(tmp_path, "done", "1")
    ok(tmp_path, "update", "1", "--status", "todo")
    t = runj(tmp_path, "show", "1")["task"]
    assert t["completed_at"] is None and t["status"] == "todo"


def test_explicit_update_clears_inferred(tmp_path):
    ok(tmp_path, "add", "任务", "--priority", "P1", "--due", TODAY.isoformat(),
       "--inferred", "priority,due")
    ok(tmp_path, "update", "1", "--priority", "P0")
    assert runj(tmp_path, "show", "1")["task"]["inferred"] == ["due"]


def test_update_due_none_clears(tmp_path):
    ok(tmp_path, "add", "任务", "--due", TODAY.isoformat())
    ok(tmp_path, "update", "1", "--due", "none")
    assert runj(tmp_path, "show", "1")["task"]["due"] is None


def test_subtask_update_restrictions(tmp_path):
    ok(tmp_path, "add", "任务", "--subtask", "a")
    assert "子任务仅支持" in fail(tmp_path, "update", "1.1", "--priority", "P0").stderr
    ok(tmp_path, "update", "1.1", "--status", "done")
    assert runj(tmp_path, "show", "1")["task"]["progress"] == 100
    assert "todo 或 done" in fail(tmp_path, "update", "1.1", "--status", "blocked").stderr


def test_subtask_add_and_two_level_limit(tmp_path):
    ok(tmp_path, "add", "任务", "--subtask", "a")
    ok(tmp_path, "done", "1.1")
    ok(tmp_path, "subtask", "add", "1", "新子任务")
    t = runj(tmp_path, "show", "1")["task"]
    assert [s["id"] for s in t["subtasks"]] == ["1.1", "1.2"]
    assert t["progress"] == 50  # 新增 todo 子任务后重算
    assert "两层" in fail(tmp_path, "subtask", "add", "1.1", "孙任务").stderr


def test_id_not_found_lists_candidates(tmp_path):
    ok(tmp_path, "add", "唯一的任务")
    r = fail(tmp_path, "done", "99")
    assert "找不到任务" in r.stderr and "唯一的任务" in r.stderr
    assert "无效的任务 ID" in fail(tmp_path, "done", "abc").stderr


# ---------------------------------------------------------------- 循环任务

def test_recurrence_daily_spawn(tmp_path):
    ok(tmp_path, "add", "每日站会", "--recurrence", "daily", "--subtask", "记录纪要")
    r = ok(tmp_path, "done", "1")
    assert "下一期" in r.stdout
    db = read_db(tmp_path)
    old = next(t for t in db["tasks"] if t["id"] == 1)
    new = next(t for t in db["tasks"] if t["id"] == 2)
    assert old["status"] == "done" and old["recurrence"] is None  # 原任务留在活跃文件
    assert new["due"] == (TODAY + timedelta(days=1)).isoformat()
    assert new["recurrence"] == "daily" and new["status"] == "todo"
    assert new["subtasks"][0]["id"] == "2.1" and new["subtasks"][0]["status"] == "todo"


def test_recurrence_weekly_and_monthly_month_end(tmp_path):
    ok(tmp_path, "add", "交周报", "--recurrence", "weekly:mon")
    ok(tmp_path, "done", "1")
    new = next(t for t in read_db(tmp_path)["tasks"] if t["id"] == 2)
    d = date.fromisoformat(new["due"])
    assert d.weekday() == 0 and 1 <= (d - TODAY).days <= 7  # 下一个周一，严格晚于今天

    ok(tmp_path, "add", "月底对账", "--recurrence", "monthly:31")
    ok(tmp_path, "done", "3")
    new = next(t for t in read_db(tmp_path)["tasks"] if t["id"] == 4)
    d = date.fromisoformat(new["due"])
    assert d > TODAY and d.day == min(31, calendar.monthrange(d.year, d.month)[1])


def test_recurrence_every_n_days(tmp_path):
    ok(tmp_path, "add", "浇花", "--recurrence", "every:3:days")
    ok(tmp_path, "done", "1")
    new = next(t for t in read_db(tmp_path)["tasks"] if t["id"] == 2)
    assert new["due"] == (TODAY + timedelta(days=3)).isoformat()


# ---------------------------------------------------------------- undo

def test_undo_add_restores_next_id(tmp_path):
    ok(tmp_path, "add", "误加的任务")
    ok(tmp_path, "undo")
    db = read_db(tmp_path)
    assert db["tasks"] == [] and db["next_id"] == 1
    assert "没有可撤销的操作" in fail(tmp_path, "undo").stderr  # 最后一条是 undo


def test_undo_update(tmp_path):
    ok(tmp_path, "add", "任务", "--priority", "P2")
    ok(tmp_path, "update", "1", "--priority", "P0", "--due", TODAY.isoformat())
    ok(tmp_path, "undo")
    t = runj(tmp_path, "show", "1")["task"]
    assert t["priority"] == "P2" and t["due"] is None


def test_undo_done_with_recurrence_removes_spawned(tmp_path):
    ok(tmp_path, "add", "交周报", "--recurrence", "weekly:mon")
    ok(tmp_path, "done", "1")
    assert len(read_db(tmp_path)["tasks"]) == 2
    ok(tmp_path, "undo")  # 撤销整个 txn：done + recur_spawn
    db = read_db(tmp_path)
    assert len(db["tasks"]) == 1 and db["next_id"] == 2
    t = db["tasks"][0]
    assert t["status"] == "todo" and t["recurrence"] == "weekly:mon" and t["completed_at"] is None


# ---------------------------------------------------------------- 归档与搜索

def test_archive_and_undo(tmp_path):
    ok(tmp_path, "add", "做完的")
    ok(tmp_path, "add", "没做完的")
    ok(tmp_path, "done", "1")
    r = runj(tmp_path, "archive")
    assert r["archived"] == 1
    db = read_db(tmp_path)
    assert [t["id"] for t in db["tasks"]] == [2]
    year = str(TODAY.year)
    arch = json.loads((tmp_path / "archive" / f"{year}.json").read_text(encoding="utf-8"))
    assert arch["tasks"][0]["id"] == 1
    ok(tmp_path, "undo")
    assert {t["id"] for t in read_db(tmp_path)["tasks"]} == {1, 2}
    arch = json.loads((tmp_path / "archive" / f"{year}.json").read_text(encoding="utf-8"))
    assert arch["tasks"] == []


def test_archive_nothing(tmp_path):
    ok(tmp_path, "add", "进行中")
    assert runj(tmp_path, "archive")["archived"] == 0


def test_search_covers_fields_and_archive(tmp_path):
    ok(tmp_path, "add", "回邮件", "--notes", "关于合同续签")
    ok(tmp_path, "add", "出差准备", "--subtask", "订机票")
    ok(tmp_path, "add", "已归档的报销")
    ok(tmp_path, "done", "3")
    ok(tmp_path, "archive")
    assert [m["id"] for m in runj(tmp_path, "search", "合同")["matches"]] == [1]      # notes
    assert [m["id"] for m in runj(tmp_path, "search", "机票")["matches"]] == [2]      # 子任务
    m = runj(tmp_path, "search", "报销")["matches"]
    assert len(m) == 1 and m[0]["_source"].startswith("archive/")                     # 归档


# ---------------------------------------------------------------- report / 锁 / 原子性

def test_report_counts(tmp_path):
    ok(tmp_path, "add", "完成了", "--estimate", "30")
    ok(tmp_path, "add", "积压中", "--priority", "P0", "--estimate", "45",
       "--due", (TODAY - timedelta(days=1)).isoformat())
    ok(tmp_path, "done", "1")
    r = runj(tmp_path, "report")
    assert len(r["completed_this_week"]) == 1
    assert r["active"] == 1 and r["overdue"] == 1
    assert r["by_priority"]["P0"] == 1
    assert r["estimate_minutes_total"] == 45  # 只计活跃任务


def test_write_lock_timeout(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "todos.json.lock").write_text("12345")
    r = fail(tmp_path, "add", "会被锁住")
    assert "超时" in r.stderr and "todos.json.lock" in r.stderr


def test_corrupted_db_not_rebuilt(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "todos.json").write_text("{broken", encoding="utf-8")
    r = fail(tmp_path, "list")
    assert "解析失败" in r.stderr and "journal" in r.stderr
    assert (tmp_path / "todos.json").read_text(encoding="utf-8") == "{broken"  # 未被覆盖


def test_failed_write_leaves_db_untouched(tmp_path):
    ok(tmp_path, "add", "任务")
    before = (tmp_path / "todos.json").read_text(encoding="utf-8")
    fail(tmp_path, "update", "1", "--priority", "P9")
    assert (tmp_path / "todos.json").read_text(encoding="utf-8") == before
