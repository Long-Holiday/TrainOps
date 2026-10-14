#!/usr/bin/env python3
"""Local training plans. No remote commands, model API, or background scheduler."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:
    fcntl = None

SCHEMA_VERSION = 1
STATUSES = ("pending", "running", "blocked", "done", "failed", "cancelled")
INSTANCE_STATUSES = ("unknown", "off", "starting", "on", "unreachable")
TERMINAL = ("done", "failed", "cancelled")
EVIDENCE = ("command", "commit", "session", "log", "output")
TRANSITIONS = {
    "pending": {"running", "blocked", "cancelled"},
    "running": {"done", "failed", "blocked", "cancelled"},
    "blocked": {"pending", "running", "cancelled"},
    "failed": {"pending", "cancelled"},
    "cancelled": {"pending"},
    "done": {"pending"},
}


class PlanError(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(value=None):
    return (value or utcnow()).isoformat(timespec="seconds")


def date(value):
    if not isinstance(value, str):
        raise PlanError("时间必须是带时区的 ISO 8601 字符串")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result
    except (ValueError, OverflowError) as exc:
        raise PlanError(f"无效时间 {value!r}，例如 2026-10-14T18:00:00+08:00") from exc


def number(value, label, low=0, high=1_000_000):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{label} 必须是数字")
    if not math.isfinite(value) or not low <= value <= high:
        raise PlanError(f"{label} 必须在 {low} 到 {high} 之间且为有限数字")


def text(value, label, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise PlanError(f"{label} 必须是{'非空' if required else ''}字符串")


def integer(value, label, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PlanError(f"{label} 必须是大于等于 {minimum} 的整数")


def natural_id(value):
    return tuple(int(part) for part in re.findall(r"\d+", value))


def find(plan, collection, item_id):
    for item in plan[collection]:
        if item["id"] == item_id:
            return item
    raise PlanError(f"找不到 {item_id}")


def new_plan(name):
    return {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "revision": 0,
        "updated_at": stamp(),
        "next_instance": 1,
        "next_flow": 1,
        "instances": [],
        "flows": [],
        "tasks": [],
        "monitor": {"schedule_id": "", "expires_at": None, "note": "未配置自动巡检"},
    }


def validate(plan):
    """Validate both incoming edits and files loaded from disk; never repair silently."""
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] != SCHEMA_VERSION:
        raise PlanError("计划格式/版本不支持；请备份并核对文件，不会自动覆盖")
    try:
        text(plan["name"], "项目名称", True)
        integer(plan["revision"], "revision", 0)
        date(plan["updated_at"])
        integer(plan["next_instance"], "next_instance")
        integer(plan["next_flow"], "next_flow")
        for collection in ("instances", "flows", "tasks"):
            if not isinstance(plan[collection], list):
                raise PlanError(f"{collection} 必须是列表")
        monitor = plan["monitor"]
        text(monitor["schedule_id"], "巡检编号")
        text(monitor["note"], "巡检说明")
        if monitor["expires_at"] is not None:
            date(monitor["expires_at"])
        ids, autodl_ids, aliases = set(), set(), set()
        for item in plan["instances"]:
            item_id = item["id"]
            if not isinstance(item_id, str) or not re.fullmatch(r"I\d{2,}", item_id) or item_id in ids:
                raise PlanError("实例编号无效或重复")
            ids.add(item_id)
            if not 0 < int(item_id[1:]) < plan["next_instance"]:
                raise PlanError("实例计数器无效")
            for key in ("name", "autodl_id", "ssh_alias"):
                text(item[key], key, True)
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", item["ssh_alias"]):
                raise PlanError("SSH 别名只能包含字母、数字、下划线、点和连字符，且不能以 - 开头")
            if item["autodl_id"] in autodl_ids or item["ssh_alias"] in aliases:
                raise PlanError("AutoDL ID 或 SSH 别名重复，不能重复登记同一实例")
            autodl_ids.add(item["autodl_id"])
            aliases.add(item["ssh_alias"])
            if item["status"] not in INSTANCE_STATUSES:
                raise PlanError("实例状态无效")
            text(item["note"], "实例备注")
            if item["observed_at"] is not None:
                date(item["observed_at"])
        instances = ids
        flows = {}
        for flow in plan["flows"]:
            flow_id = flow["id"]
            if not isinstance(flow_id, str) or not re.fullmatch(r"F\d{2,}", flow_id) or flow_id in flows:
                raise PlanError("流程编号无效或重复")
            if not 0 < int(flow_id[1:]) < plan["next_flow"]:
                raise PlanError("流程计数器无效")
            text(flow["name"], "流程名称", True)
            integer(flow["next_task"], "next_task")
            flows[flow_id] = flow
        tasks = {}
        busy = {}
        for task in plan["tasks"]:
            task_id = task["id"]
            if not isinstance(task_id, str) or not re.fullmatch(r"F\d{2,}-T\d{2,}", task_id) or task_id in tasks:
                raise PlanError("任务编号无效或重复")
            if task["flow"] not in flows or not task_id.startswith(task["flow"] + "-T"):
                raise PlanError(f"{task_id} 的流程无效")
            if not 0 < int(task_id.split("-T")[1]) < flows[task["flow"]]["next_task"]:
                raise PlanError("任务计数器无效")
            text(task["name"], "任务名称", True)
            resource = task["instance"]
            if resource not in (None, "local") and resource not in instances:
                raise PlanError(f"{task_id} 引用了不存在的实例")
            if task["status"] not in STATUSES:
                raise PlanError(f"{task_id} 状态无效")
            if not isinstance(task["deps"], list) or any(not isinstance(x, str) for x in task["deps"]):
                raise PlanError(f"{task_id} 依赖必须是编号列表")
            if len(task["deps"]) != len(set(task["deps"])):
                raise PlanError(f"{task_id} 有重复依赖")
            if task["duration_minutes"] is not None:
                number(task["duration_minutes"], "预计分钟数")
            if task["progress"] is not None:
                number(task["progress"], "进度", high=100)
            for key in ("owner", "note", *EVIDENCE):
                text(task[key], key)
            for key in ("not_before", "started_at", "finished_at", "checked_at", "progress_at"):
                if task[key] is not None:
                    date(task[key])
            if task["started_at"] and task["finished_at"] and date(task["finished_at"]) < date(task["started_at"]):
                raise PlanError(f"{task_id} 结束时间早于开始时间")
            if task["status"] == "pending" and (task["started_at"] or task["finished_at"]):
                raise PlanError(f"{task_id} 待执行状态不能有实际起止时间")
            if task["status"] not in TERMINAL and task["finished_at"] is not None:
                raise PlanError(f"{task_id} 未结束但存在结束时间")
            if task["status"] in TERMINAL and not task["finished_at"]:
                raise PlanError(f"{task_id} 缺少结束时间")
            if task["status"] == "done" and task["progress"] != 100:
                raise PlanError(f"{task_id} 已完成状态的进度必须为 100")
            if task["status"] in ("running", "done"):
                if not task["owner"].strip() or not task["started_at"] or resource is None:
                    raise PlanError(f"{task_id} 必须有 owner、实际开始时间和实例（本地任务用 local）")
                if task["not_before"] and date(task["started_at"]) < date(task["not_before"]):
                    raise PlanError(f"{task_id} 尚未到允许开始时间")
            if task["status"] == "running" and resource != "local":
                if resource in busy:
                    raise PlanError(f"{resource} 已被 {busy[resource]} 占用")
                busy[resource] = task_id
            tasks[task_id] = task
        for task in tasks.values():
            for dep_id in task["deps"]:
                if dep_id == task["id"] or dep_id not in tasks:
                    raise PlanError(f"{task['id']} 依赖不存在或依赖自身: {dep_id}")
                dep = tasks[dep_id]
                if task["status"] in ("running", "done"):
                    if dep["status"] != "done":
                        raise PlanError(f"{task['id']} 的依赖 {dep_id} 尚未完成")
                    if date(task["started_at"]) < date(dep["finished_at"]):
                        raise PlanError(f"{task['id']} 开始时间早于依赖 {dep_id} 完成时间")
        # Kahn's algorithm avoids recursion limits on long training pipelines.
        degrees = {key: len(t["deps"]) for key, t in tasks.items()}
        children = {key: [] for key in tasks}
        for task in tasks.values():
            for dep_id in task["deps"]:
                children[dep_id].append(task["id"])
        queue = [key for key, count in degrees.items() if count == 0]
        visited = 0
        while queue:
            key = queue.pop()
            visited += 1
            for child in children[key]:
                degrees[child] -= 1
                if degrees[child] == 0:
                    queue.append(child)
        if visited != len(tasks):
            raise PlanError("依赖形成环，计划未保存")
    except (KeyError, TypeError, AttributeError) as exc:
        raise PlanError(f"计划字段缺失或类型错误: {exc}") from exc


class Store:
    def __init__(self, project):
        self.root = Path(project).expanduser().resolve()
        self.directory = self.root / ".trainops"
        self.path = self.directory / "plan.json"

    @contextmanager
    def lock(self):
        if fcntl is None:
            raise PlanError("需要 POSIX 文件锁；Windows 请在 WSL 中运行")
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "plan.lock").open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read(self):
        try:
            plan = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PlanError("尚无计划，请先 init 或在空目录运行 demo") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PlanError(f"计划文件损坏，未覆盖: {exc}") from exc
        validate(plan)
        return plan

    def write(self, plan):
        validate(plan)
        path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".plan-", suffix=".tmp", delete=False) as handle:
                path = Path(handle.name)
                json.dump(plan, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(path, self.path)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def create(self, plan):
        with self.lock():
            if self.path.exists():
                raise PlanError("计划已存在，不会覆盖；请使用原计划或指定另一个目录")
            plan["revision"] = 1
            self.write(plan)
        return plan

    def change(self, operation):
        with self.lock():
            plan = self.read()
            result = operation(plan)
            plan["revision"] += 1
            plan["updated_at"] = stamp()
            self.write(plan)
            return {"revision": plan["revision"], "item": result}


def add_instance(plan, name, autodl_id, ssh_alias):
    item = {"id": f"I{plan['next_instance']:02d}", "name": name,
            "autodl_id": autodl_id, "ssh_alias": ssh_alias, "status": "unknown",
            "observed_at": None, "note": ""}
    plan["next_instance"] += 1
    plan["instances"].append(item)
    return item


def add_flow(plan, name):
    item = {"id": f"F{plan['next_flow']:02d}", "name": name, "next_task": 1}
    plan["next_flow"] += 1
    plan["flows"].append(item)
    return item


def add_task(plan, flow_id, name, instance=None, duration=None, deps=None, not_before=None):
    flow = find(plan, "flows", flow_id)
    item = {"id": f"{flow_id}-T{flow['next_task']:02d}", "flow": flow_id, "name": name,
            "instance": instance, "deps": deps or [], "duration_minutes": duration,
            "not_before": not_before, "status": "pending", "owner": "", "progress": None,
            "started_at": None, "finished_at": None, "checked_at": None, "progress_at": None,
            "note": "", **{key: "" for key in EVIDENCE}}
    flow["next_task"] += 1
    plan["tasks"].append(item)
    return item


def set_task(plan, task_id, changes, actor=None, now=None):
    now = now or utcnow()
    task = find(plan, "tasks", task_id)
    before = task["status"]
    after = changes.get("status", before)
    if before == "running":
        if actor != task["owner"]:
            raise PlanError(f"{task_id} 已被 {task['owner']} 认领；运行中更新须提供同一 --owner")
        for key in ("instance", "deps", "not_before", "owner"):
            if key in changes and changes[key] != task[key]:
                raise PlanError("运行中不能改实例/依赖/开始限制/owner；先核实远程进程已停止，再标为 blocked 或结束")
    if after != before and after not in TRANSITIONS[before]:
        raise PlanError(f"不允许 {before} → {after}；重试先改为 pending")
    task.update(changes)
    if after == "running" and before != "running":
        if not actor or not actor.strip():
            raise PlanError("开始任务须提供 --owner（本次 agent/会话唯一名称）")
        if task["owner"] and task["owner"] != actor:
            raise PlanError(f"任务属于 {task['owner']}；请先在未运行状态下明确更新 owner")
        task["owner"] = actor
        task["started_at"] = stamp(now)
        task["finished_at"] = None
        task["checked_at"] = changes.get("checked_at")
        task["progress"] = None
        task["progress_at"] = None
    if after != before and after in TERMINAL:
        task["finished_at"] = stamp(now)
        if after == "done":
            task["progress"] = 100
            task["progress_at"] = stamp(now)
    if after == "pending" and before != "pending":
        task.update(started_at=None, finished_at=None, owner="", progress=None,
                    progress_at=None, checked_at=None)
    if "progress" in changes and after == "running":
        task["progress"] = changes["progress"]
        task["progress_at"] = stamp(now)
    return task


def forecast(plan, now=None):
    """Deterministic dependency-ready scheduling with one lane per GPU instance."""
    now = now or utcnow()
    tasks = {t["id"]: t for t in plan["tasks"]}
    results, lanes = {}, {}
    active = {t["instance"]: t["id"] for t in tasks.values()
              if t["status"] == "running" and t["instance"] != "local"}

    def record(task, start=None, end=None, reason="", overdue=False):
        results[task["id"]] = {"start": stamp(start) if start else None,
                               "finish": stamp(end) if end else None,
                               "reason": reason, "overdue": overdue,
                               "ready": False, "stale": not task["checked_at"] or
                               (now - date(task["checked_at"])).total_seconds() > 3600}
        return end

    for task in sorted(tasks.values(), key=lambda t: natural_id(t["id"])):
        status = task["status"]
        if status == "done":
            record(task, date(task["started_at"]), date(task["finished_at"]), "实际完成")
        elif status in ("failed", "cancelled", "blocked"):
            record(task, date(task["started_at"]) if task["started_at"] else None,
                   reason={"failed": "失败，等待处理", "cancelled": "已取消，不满足下游依赖",
                           "blocked": "人工标记阻塞"}[status])
        elif status == "running":
            start, end = date(task["started_at"]), None
            if task["duration_minutes"] is not None:
                end = start + timedelta(minutes=task["duration_minutes"])
            progress = task["progress"]
            if progress is not None and 0 < progress < 100 and task["progress_at"]:
                observed = date(task["progress_at"])
                if observed > start:
                    end = start + (observed - start) * (100 / progress)
            overdue = end is not None and end <= now
            if progress == 100:
                end = None
                reason = "进度 100%，待核实进程退出与产物"
            elif overdue:
                end = None
                reason = "已超过估计，需重估"
            else:
                reason = "按进度/耗时估计" if end else "运行中，待估耗时"
            record(task, start, end, reason, overdue)
            if task["instance"] != "local":
                lanes[task["instance"]] = end

    pending = {key for key in tasks if key not in results}
    while pending:
        candidates = []
        for key in sorted(pending, key=natural_id):
            task = tasks[key]
            if not all(dep in results for dep in task["deps"]):
                continue
            unknown = [dep for dep in task["deps"] if results[dep]["finish"] is None]
            if unknown:
                candidates.append((now, natural_id(key), key, "依赖待完成/待估: " + ", ".join(unknown)))
                continue
            earliest = max([now] + [date(results[dep]["finish"]) for dep in task["deps"]])
            if task["not_before"]:
                earliest = max(earliest, date(task["not_before"]))
            resource = task["instance"]
            if resource is None:
                candidates.append((earliest, natural_id(key), key, "未分配实例（本地任务设 local）"))
            elif resource in lanes and lanes[resource] is None:
                candidates.append((earliest, natural_id(key), key, "实例前序任务完成时间待估"))
            else:
                earliest = max(earliest, lanes.get(resource) or now)
                candidates.append((earliest, natural_id(key), key, ""))
        if not candidates:
            raise PlanError("依赖无法排程，请检查环或引用")
        start, _, key, reason = min(candidates)
        task = tasks[key]
        resource = task["instance"]
        if reason:
            record(task, reason=reason)
        else:
            duration = task["duration_minutes"]
            end = start + timedelta(minutes=duration) if duration is not None else None
            record(task, start, end, "" if end else "预计耗时待估")
            results[key]["ready"] = (all(tasks[d]["status"] == "done" for d in task["deps"])
                                      and (not task["not_before"] or date(task["not_before"]) <= now)
                                      and resource not in active)
            if resource != "local":
                lanes[resource] = end
        pending.remove(key)
    return results


def snapshot(plan, now=None):
    now = now or utcnow()
    estimates = forecast(plan, now)
    return {**plan, "generated_at": stamp(now),
            "tasks": [{**task, "estimate": estimates[task["id"]]} for task in plan["tasks"]]}


def demo_plan(now=None):
    now = now or utcnow()
    plan = new_plan("双实例实验 · 演示（不会连接服务器）")
    add_instance(plan, "基线 / GPU A", "DEMO-NOT-REAL-A", "demo-gpu-a")
    add_instance(plan, "消融 / GPU B", "DEMO-NOT-REAL-B", "demo-gpu-b")
    for instance in plan["instances"]:
        instance.update(status="on", observed_at=stamp(now), note="演示占位，非真实实例")
    add_flow(plan, "基线模型")
    add_flow(plan, "消融对照")
    add_flow(plan, "结果汇总")
    add_task(plan, "F01", "数据与代码就绪", "local", 10)
    set_task(plan, "F01-T01", {"status": "running"}, "demo-agent-a", now - timedelta(minutes=30))
    set_task(plan, "F01-T01", {"status": "done"}, "demo-agent-a", now - timedelta(minutes=20))
    add_task(plan, "F01", "基线训练", "I01", 120, ["F01-T01"])
    set_task(plan, "F01-T02", {"status": "running", "session": "tmux: F01-T02",
                             "log": "runs/F01-T02/train.log", "output": "runs/F01-T02/",
                             "commit": "demo-only", "command": "python train.py --config baseline.yaml"},
             "demo-agent-a", now - timedelta(minutes=20))
    set_task(plan, "F01-T02", {"progress": 18, "checked_at": stamp(now)}, "demo-agent-a", now)
    add_task(plan, "F01", "基线评估", "I01", 15, ["F01-T02"])
    add_task(plan, "F02", "消融训练", "I02", 90, ["F01-T01"])
    set_task(plan, "F02-T01", {"status": "running", "log": "runs/F02-T01/train.log"},
             "demo-agent-b", now - timedelta(minutes=20))
    set_task(plan, "F02-T01", {"progress": 15, "checked_at": stamp(now)}, "demo-agent-b", now)
    add_task(plan, "F02", "消融评估", "I02", 15, ["F02-T01"])
    add_task(plan, "F03", "比较指标并整理报告", "local", 20, ["F01-T03", "F02-T02"])
    add_task(plan, "F03", "根据结果设计下一轮", "local", None, ["F03-T01"])
    plan["monitor"]["note"] = "仅演示数据；没有创建巡检任务或实际训练"
    return plan


def check_structure(plan):
    """Structural check for flow continuity, dependency links, and resource/duration readiness."""
    validate(plan)
    issues = []
    tasks_by_id = {t["id"]: t for t in plan["tasks"]}
    for flow in plan["flows"]:
        flow_tasks = [t for t in plan["tasks"] if t["flow"] == flow["id"]]
        if not flow_tasks:
            issues.append(f"{flow['id']}({flow['name']}): 流程为空，无任务节点")
            continue
        for idx, task in enumerate(flow_tasks):
            if idx > 0 and not task["deps"]:
                issues.append(f"{task['id']}: 缺少前置依赖，会导致流程甘特图块断裂（建议依赖 {flow_tasks[idx - 1]['id']}）")
            if task["status"] not in TERMINAL:
                if task["instance"] is None:
                    issues.append(f"{task['id']}: 未分配实例（I01.. 或 local）")
                if task["duration_minutes"] is None:
                    issues.append(f"{task['id']}: 未设置预计耗时（duration_minutes），完成时间待估")
            if task["started_at"] and task["deps"]:
                dep_finishes = [date(tasks_by_id[d]["finished_at"]) for d in task["deps"] if tasks_by_id[d]["finished_at"]]
                if dep_finishes:
                    gap_min = (date(task["started_at"]) - max(dep_finishes)).total_seconds() / 60
                    if gap_min > 5:
                        issues.append(f"{task['id']}: 实际开始时间晚于前置依赖完成时间 {int(gap_min)} 分钟，存在时间空隙")
    return {"ok": not issues, "flows": len(plan["flows"]), "tasks": len(plan["tasks"]), "issues": issues}


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, store, **kwargs):
        self.store = store
        super().__init__(*args, **kwargs)

    def respond(self, status, data, content_type="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.server.server_port == 80:
            allowed_hosts.update({"127.0.0.1", "localhost"})
        if self.headers.get("Host", "").lower() not in allowed_hosts:
            self.respond(403, {"error": "只允许本机 Host，不能通过外部域名访问计划"})
            return
        try:
            if urlsplit(self.path).path == "/api/plan":
                self.respond(200, snapshot(self.store.read()))
            elif urlsplit(self.path).path == "/":
                page = Path(__file__).resolve().parent.parent / "web" / "index.html"
                self.respond(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self.respond(404, {"error": "not found"})
        except (PlanError, OSError, OverflowError) as exc:
            self.respond(500, {"error": str(exc)})

    def reject_write(self):
        self.respond(405, {"error": "只读页面，请通过 agent/CLI 修改计划"})

    do_POST = do_PUT = do_PATCH = do_DELETE = reject_write

    def log_message(self, format, *args):
        pass


def server(store, port=8765):
    return ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, store=store))


def nullable(value):
    return None if value in ("none", "?") else value


def duration(value):
    if value in ("none", "?"):
        return None
    try:
        result = float(value)
        number(result, "预计分钟数")
        return result
    except (ValueError, PlanError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def dependencies(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_instance_spec(spec):
    parts = [part.strip() for part in spec.split(":", 2)]
    if len(parts) != 3 or not all(parts):
        raise PlanError(f"实例简写格式应为 '名称:autodl_id:ssh_alias'，实际为 {spec!r}")
    return parts[0], parts[1], parts[2]


def parse_task_spec(spec):
    parts = [part.strip() for part in spec.split(":", 4)]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise PlanError(f"任务简写格式应为 '流程ID:名称[:实例[:分钟数[:依赖]]]'，实际为 {spec!r}")
    flow_id, name = parts[0], parts[1]
    instance = nullable(parts[2]) if len(parts) > 2 and parts[2] else None
    mins = duration(parts[3]) if len(parts) > 3 and parts[3] else None
    deps = dependencies(parts[4]) if len(parts) > 4 and parts[4] else []
    return flow_id, name, instance, mins, deps


def parse_set_spec(spec):
    parts = [part.strip() for part in spec.split(":", 3)]
    if len(parts) < 2 or not parts[0]:
        raise PlanError(f"更新简写格式应为 '任务ID:状态[:进度[:备注]]'，实际为 {spec!r}")
    task_id = parts[0]
    changes = {}
    if parts[1]:
        if parts[1] not in STATUSES:
            raise PlanError(f"状态无效: {parts[1]!r}")
        changes["status"] = parts[1]
    if len(parts) > 2 and parts[2] and parts[2] not in ("none", "?"):
        try:
            changes["progress"] = float(parts[2])
        except ValueError as exc:
            raise PlanError(f"无效进度: {parts[2]!r}") from exc
    if len(parts) > 3 and parts[3]:
        changes["note"] = parts[3]
    if not changes:
        raise PlanError(f"未在 {spec!r} 中提供有效更新字段")
    return task_id, changes


def compact(data):
    if isinstance(data, dict):
        return {k: compact(v) for k, v in data.items() if v not in ("", None, [])}
    if isinstance(data, list):
        return [compact(v) for v in data]
    return data


def add_task_args(target, is_set=False):
    default = argparse.SUPPRESS if is_set else None
    target.add_argument("--instance", type=nullable, default=default, help="I01 / local / none")
    target.add_argument("--minutes", dest="duration_minutes", type=duration, default=default, help="预计耗时；? 表示待估")
    target.add_argument("--deps", type=dependencies, default=default, help="逗号分隔；空字符串清空")
    target.add_argument("--not-before", type=nullable, default=default, help="带时区时间；none 清空")
    if is_set:
        target.add_argument("--status", choices=STATUSES)
        target.add_argument("--owner", help="认领/操作当前任务的 agent 名称（默认 agent）")
        target.add_argument("--progress", type=float)
        target.add_argument("--checked", action="store_true", help="记录实际巡检时间")
        target.add_argument("--note")
        for key in EVIDENCE:
            target.add_argument("--" + key)


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("-p", "--project", default=".", help="使用项目目录，不是能力包目录")
    commands = root.add_subparsers(dest="operation_name", required=True)
    init = commands.add_parser("init", help="新建计划（可同时批量传入实例、流程、任务）")
    init.add_argument("--name", required=True)
    init.add_argument("--instance", action="append", default=[], help="名称:autodl_id:ssh_alias")
    init.add_argument("--flow", action="append", default=[], help="流程名称")
    init.add_argument("--task", action="append", default=[], help="F01:任务名[:实例[:分钟数[:依赖]]]")
    batch = commands.add_parser("batch", help="单次命令批量新增实例/流程/任务或更新状态")
    batch.add_argument("--instance", action="append", default=[], help="名称:autodl_id:ssh_alias")
    batch.add_argument("--flow", action="append", default=[], help="流程名称")
    batch.add_argument("--task", action="append", default=[], help="F01:任务名[:实例[:分钟数[:依赖]]]")
    batch.add_argument("--set", dest="updates", action="append", default=[], help="F01-T01:状态[:进度[:备注]]")
    batch.add_argument("--owner", help="批量更新使用的 owner（默认 agent）")
    commands.add_parser("demo", help="在空目录生成演示计划，无远程操作")
    commands.add_parser("check", help="执行结构性检查（流程连续性、依赖链、实例与耗时完整性）")
    show = commands.add_parser("show", help="读取最新计划和时间估算")
    show.add_argument("--json", action="store_true")
    serve = commands.add_parser("serve", help="仅本机只读页面，Ctrl+C 停止")
    serve.add_argument("--port", type=int, default=8765)
    instance = commands.add_parser("instance").add_subparsers(dest="action", required=True)
    instance_add = instance.add_parser("add")
    instance_add.add_argument("--name", required=True)
    instance_add.add_argument("--autodl-id", required=True)
    instance_add.add_argument("--ssh-alias", required=True)
    instance_set = instance.add_parser("set")
    instance_set.add_argument("id")
    instance_set.add_argument("--name")
    instance_set.add_argument("--autodl-id")
    instance_set.add_argument("--ssh-alias")
    instance_set.add_argument("--status", choices=INSTANCE_STATUSES)
    instance_set.add_argument("--note")
    instance_set.add_argument("--observed", action="store_true", help="记录本次真实观测时间")
    flow = commands.add_parser("flow").add_subparsers(dest="action", required=True)
    flow_add = flow.add_parser("add")
    flow_add.add_argument("--name", required=True)
    flow_set = flow.add_parser("set")
    flow_set.add_argument("id")
    flow_set.add_argument("--name", required=True)
    task = commands.add_parser("task").add_subparsers(dest="action", required=True)
    task_add = task.add_parser("add")
    task_add.add_argument("--flow", required=True)
    task_add.add_argument("--name", required=True)
    add_task_args(task_add, is_set=False)
    task_set = task.add_parser("set")
    task_set.add_argument("id")
    task_set.add_argument("--name")
    add_task_args(task_set, is_set=True)
    quick_set = commands.add_parser("set", help="task set 的简写命令")
    quick_set.add_argument("id")
    quick_set.add_argument("--name")
    add_task_args(quick_set, is_set=True)
    monitor = commands.add_parser("monitor", help="登记已有调度器信息，不创建定时任务")
    monitor.add_argument("--schedule-id", default="")
    monitor.add_argument("--expires-at", type=nullable)
    monitor.add_argument("--note", required=True)
    return root


def apply_batch_specs(plan, instances=(), flows=(), tasks=(), updates=(), owner=None):
    added = {"instances": [], "flows": [], "tasks": [], "updated": []}
    for spec in instances:
        name, autodl_id, ssh_alias = parse_instance_spec(spec)
        added["instances"].append(add_instance(plan, name, autodl_id, ssh_alias)["id"])
    for flow_name in flows:
        added["flows"].append(add_flow(plan, flow_name)["id"])
    for spec in tasks:
        flow_id, name, inst, mins, deps = parse_task_spec(spec)
        if not deps:
            prev = [t["id"] for t in plan["tasks"] if t["flow"] == flow_id]
            if prev:
                deps = [prev[-1]]
        added["tasks"].append(add_task(plan, flow_id, name, inst, mins, deps)["id"])
    for spec in updates:
        task_id, changes = parse_set_spec(spec)
        changes["checked_at"] = stamp()
        target = find(plan, "tasks", task_id)
        actor = owner or "agent"
        if changes.get("status") == "done" and target["status"] in ("pending", "blocked"):
            set_task(plan, task_id, {"status": "running", "checked_at": changes["checked_at"]}, actor)
        set_task(plan, task_id, changes, actor)
        added["updated"].append(task_id)
    return added


def dispatch(args, store):
    if args.operation_name == "init":
        plan = new_plan(args.name)
        apply_batch_specs(plan, args.instance, args.flow, args.task)
        created = store.create(plan)
        return {"revision": created["revision"], "name": created["name"],
                "instances": [i["id"] for i in created["instances"]],
                "flows": [f["id"] for f in created["flows"]],
                "tasks": [t["id"] for t in created["tasks"]]}
    if args.operation_name == "demo":
        created = store.create(demo_plan())
        return {"revision": created["revision"], "name": created["name"],
                "tasks": [t["id"] for t in created["tasks"]]}
    if args.operation_name == "check":
        return check_structure(store.read())
    if args.operation_name == "show":
        view = snapshot(store.read())
        if args.json:
            return view
        print(f"{view['name']} · rev {view['revision']} · {view['updated_at']}")
        if view["instances"]:
            print("实例: " + " | ".join(
                f"{i['id']} {i['name']}({i['ssh_alias']}/{i['autodl_id']})[{i['status']}]"
                for i in view["instances"]
            ))
        mon = view["monitor"]
        if mon["schedule_id"] or mon["note"]:
            print(f"巡检: {mon['schedule_id'] or '未登记'} · {mon['note']}")
        for flow in view["flows"]:
            print(f"\n{flow['id']} {flow['name']}")
            for task in view["tasks"]:
                if task["flow"] == flow["id"]:
                    eta = task["estimate"]
                    prog = f" {task['progress']:g}%" if task["progress"] is not None else ""
                    deps = f" | 依赖:{','.join(task['deps'])}" if task["deps"] else ""
                    mins = f" | {task['duration_minutes']:g}m" if task["duration_minutes"] is not None else " | ?m"
                    note = f" | {task['note']}" if task["note"] else ""
                    print(f"  {task['id']} [{task['status']}{prog}] {task['name']} | "
                          f"{task['instance'] or '未分配'}{mins}{deps} | "
                          f"完成 {eta['finish'] or '待估'} {eta['reason']}{note}".rstrip())
        return None
    if args.operation_name == "serve":
        store.read()
        if not 0 <= args.port <= 65535:
            raise PlanError("端口必须在 0–65535 之间")
        with server(store, args.port) as httpd:
            print(f"TrainOps http://127.0.0.1:{httpd.server_port} · 只读计划，不是后台训练调度器", flush=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
        return None

    def operation(plan):
        if args.operation_name == "batch":
            if not (args.instance or args.flow or args.task or args.updates):
                raise PlanError("batch 至少需要提供 --instance、--flow、--task 或 --set 之一")
            return apply_batch_specs(plan, args.instance, args.flow, args.task, args.updates, args.owner)
        if args.operation_name == "instance":
            if args.action == "add":
                return add_instance(plan, args.name, args.autodl_id, args.ssh_alias)
            item = find(plan, "instances", args.id)
            if any(t["instance"] == args.id and t["status"] == "running" for t in plan["tasks"]):
                if ((args.autodl_id is not None and args.autodl_id != item["autodl_id"])
                        or (args.ssh_alias is not None and args.ssh_alias != item["ssh_alias"])):
                    raise PlanError("实例仍有运行任务，不能更换 ID/SSH 映射")
            for key in ("name", "autodl_id", "ssh_alias", "status", "note"):
                if getattr(args, key) is not None:
                    item[key] = getattr(args, key)
            if args.observed or args.status is not None:
                item["observed_at"] = stamp()
            return item
        if args.operation_name == "flow":
            if args.action == "add":
                return add_flow(plan, args.name)
            item = find(plan, "flows", args.id)
            item["name"] = args.name
            return item
        if args.operation_name == "monitor":
            plan["monitor"] = {"schedule_id": args.schedule_id, "expires_at": args.expires_at, "note": args.note}
            return plan["monitor"]
        if getattr(args, "action", "set") == "add":
            return add_task(plan, args.flow, args.name, args.instance, args.duration_minutes, args.deps, args.not_before)
        changes = {key: value for key, value in vars(args).items()
                   if key in ("name", "status", "owner", "progress", "note", *EVIDENCE) and value is not None}
        for key in ("instance", "duration_minutes", "deps", "not_before"):
            if hasattr(args, key):
                changes[key] = getattr(args, key)
        if args.checked or args.progress is not None or args.status in ("running", "done"):
            changes["checked_at"] = stamp()
        if not changes:
            raise PlanError("未提供要更新的字段")
        target = find(plan, "tasks", args.id)
        actor = args.owner or "agent"
        if changes.get("status") == "done" and target["status"] in ("pending", "blocked"):
            set_task(plan, args.id, {"status": "running", "checked_at": changes.get("checked_at")}, actor)
        return set_task(plan, args.id, changes, actor)

    return store.change(operation)


def main(argv=None):
    raw = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(raw)
    try:
        result = dispatch(args, Store(args.project))
        if result is not None:
            if args.operation_name == "show" and args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            else:
                print(json.dumps(compact(result), ensure_ascii=False, allow_nan=False))
        return 0
    except (PlanError, OSError, OverflowError) as exc:
        print(f"TrainOps: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
