#!/usr/bin/env python3
"""Local training plan ledger. The only outbound call is the read-only AutoDL wallet lookup
used by `wallet fetch` and by the scheduled `monitor` settlement; no instance commands,
no model API."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:
    fcntl = None

# 版本号只用来拒绝看不懂的文件：开发阶段不提供迁移，旧版一律明确报错而不是猜着读。
SCHEMA_VERSION = 3
STATUSES = ("pending", "running", "blocked", "done", "failed", "cancelled")
INSTANCE_STATUSES = ("unknown", "off", "starting", "on", "unreachable")
AWAKE = ("on", "starting")
DEFAULT_INTERVAL_MINUTES = 30
INTERVAL_LIMITS = (1, 1440)
# 时价是元/小时的标价：0 与负数会被读成"不花钱"，宁可拒绝也不让账目失真（validate 与面板接口共用）。
PRICE_LIMITS = (0.001, 10000)
# 无卡开机（含持续请求等待 GPU）仍按小时计费，只是费率低：AutoDL 目前是 0.1 元/小时。
# 它和 GPU 时价是两个字段，因为两种开机模式会来回切换，各自的价格不该互相冲掉。
NO_GPU_PRICE = 0.1
MODES = ("gpu", "no-gpu")
# 结算快照只装"算得出的结论"：缺数据的键不落盘，也不拿 0 顶替（与 budget() 同一约定）。
ESTIMATE_KEYS = ("burn_rate", "hours_awake", "plan_gpu_hours", "plan_cost", "covers_plan")
ERROR_LIMIT = 200
BRIEF_LIMIT = 3
BRIEF_TEXT_LIMIT = 120
KEEP = object()  # 区分"没传这个参数"与"显式清空"
# 磁盘容量一律以 GB 存进字段名，避免"这个数字是字节还是 G"的歧义（同 duration_minutes 的做法）。
DISK_FIELDS = ("system_used_gb", "system_total_gb", "data_used_gb", "data_total_gb",
               "fs_used_gb", "fs_total_gb")
DISK_MOUNTS = (("sys", "system", "系统盘"), ("data", "data", "数据盘"), ("fs", "fs", "文件存储"))
# --disk 里用短盘名（sys 而不是 system），映射到字段前缀与中文名。
DISK_PREFIXES = {prefix: (field, label) for prefix, field, label in DISK_MOUNTS}
DISK_SHORT = {"系统盘": "系统", "数据盘": "数据", "文件存储": "存储"}
FULL_RATIO = 0.9
API_BALANCE_PATH = "/api/v1/dev/wallet/balance"
# 面板唯一可写的端点：登记时价。其余路径全是只读，写动词一律 405。
PRICE_PATH = "/api/instance-price"
BODY_LIMIT = 4096
TERMINAL = ("done", "failed", "cancelled")
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
        "monitor": {"interval_minutes": DEFAULT_INTERVAL_MINUTES, "last_run": None, "last_ok": None,
                    "last_error": "", "token_ready": False, "expires_at": None, "estimate": None},
        "wallet": {"assets": None, "voucher": None, "observed_at": None},
    }


def validate_estimate(estimate):
    """结算快照是机器写的派生数据：键可缺（缺数据就没有结论），但存在的值必须可信。"""
    if not isinstance(estimate, dict):
        raise PlanError("结算快照必须是对象")
    date(estimate["computed_at"])
    integer(estimate["revision"], "结算 revision", 0)
    text(estimate.get("plan_hash") or "", "结算指纹")
    for key, label, high in (("burn_rate", "烧钱速度", 10000), ("hours_awake", "可跑小时", 1_000_000),
                             ("plan_gpu_hours", "剩余 GPU 小时", 1_000_000), ("plan_cost", "预估花费", 1_000_000)):
        if estimate.get(key) is not None:
            number(estimate[key], label, high=high)
    if estimate.get("covers_plan") is not None and not isinstance(estimate["covers_plan"], bool):
        raise PlanError("结算快照的 covers_plan 必须是布尔值")


def validate_monitor(monitor):
    if monitor is None:
        return
    if not isinstance(monitor, dict):
        raise PlanError("monitor 段必须是对象")
    interval = monitor.get("interval_minutes")
    if interval is not None:
        integer(interval, "结算周期", INTERVAL_LIMITS[0])
        if interval > INTERVAL_LIMITS[1]:
            raise PlanError(f"结算周期必须在 {INTERVAL_LIMITS[0]} 到 {INTERVAL_LIMITS[1]} 分钟之间")
    for key, label in (("last_run", "上次结算时间"), ("last_ok", "上次成功时间"), ("expires_at", "结算到期时间")):
        if monitor.get(key) is not None:
            date(monitor[key])
    text(monitor.get("last_error") or "", "结算错误")
    if monitor.get("token_ready") is not None and not isinstance(monitor["token_ready"], bool):
        raise PlanError("token_ready 必须是布尔值")
    if monitor.get("estimate") is not None:
        validate_estimate(monitor["estimate"])


def validate(plan):
    """Validate both incoming edits and files loaded from disk; never repair silently."""
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int:
        raise PlanError("计划格式/版本不支持；请备份并核对文件，不会自动覆盖")
    if plan["schema_version"] != SCHEMA_VERSION:
        raise PlanError(f"计划 schema {plan['schema_version']} 与当前 {SCHEMA_VERSION} 不符"
                        "（开发阶段不提供迁移）；请备份并核对文件，不会自动覆盖")
    try:
        text(plan["name"], "项目名称", True)
        integer(plan["revision"], "revision", 0)
        date(plan["updated_at"])
        integer(plan["next_instance"], "next_instance")
        integer(plan["next_flow"], "next_flow")
        for collection in ("instances", "flows", "tasks"):
            if not isinstance(plan[collection], list):
                raise PlanError(f"{collection} 必须是列表")
        validate_monitor(plan["monitor"])
        wallet = plan["wallet"]
        for key, label in (("assets", "余额"), ("voucher", "代金券")):
            if wallet[key] is not None:
                number(wallet[key], label, high=1_000_000)
        if wallet["observed_at"] is not None:
            date(wallet["observed_at"])
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
            if item["price"] is not None:
                number(item["price"], "时价", low=PRICE_LIMITS[0], high=PRICE_LIMITS[1])
            # 开机模式与无卡费率是后加的字段：老计划没有这两个键，缺了就按"未观测 + 平台默认无卡费率"读，
            # 因此不用升 schema 版本，也不会逼人重建计划。
            if item.get("mode") is not None and item["mode"] not in MODES:
                raise PlanError("开机模式只能是 gpu（有卡）或 no-gpu（无卡准备）")
            if item.get("no_gpu_price") is not None:
                number(item["no_gpu_price"], "无卡费率", low=PRICE_LIMITS[0], high=PRICE_LIMITS[1])
            disk = item["disk"]
            for key in DISK_FIELDS:
                if disk[key] is not None:
                    number(disk[key], key, high=1_000_000)
            for _, field, label in DISK_MOUNTS:
                used, total = disk[f"{field}_used_gb"], disk[f"{field}_total_gb"]
                if used is not None and total is not None and used > total:
                    raise PlanError(f"{label}的已用容量大于总容量，请核对 df 输出")
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
            text(task["note"], "note")
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
                if not task["started_at"] or resource is None:
                    raise PlanError(f"{task_id} 必须有实际开始时间和实例（本地任务用 local）")
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


def sync_directory(directory):
    """os.replace 之后 fsync 目录，让改名本身也落盘；个别文件系统不支持时忽略。"""
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


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
            sync_directory(self.directory)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def create(self, plan, force=False):
        with self.lock():
            if self.path.exists() and not force:
                raise PlanError("计划已存在，不会覆盖；请使用原计划、指定另一个目录，或对演示数据用 demo --force")
            plan["revision"] = 1
            self.write(plan)
        return plan

    def change(self, operation):
        with self.lock():
            plan = self.read()
            # 先自增再执行：操作里读 plan["revision"] 得到的必须是这次落盘后的编号
            # （结算快照要记 revision，用来判断"结算之后计划有没有变过"）。
            plan["revision"] += 1
            result = operation(plan)
            plan["updated_at"] = stamp()
            self.write(plan)
            return {"revision": plan["revision"], "item": result}


def add_instance(plan, name, autodl_id, ssh_alias):
    item = {"id": f"I{plan['next_instance']:02d}", "name": name,
            "autodl_id": autodl_id, "ssh_alias": ssh_alias, "status": "unknown",
            "observed_at": None, "note": "", "price": None, "mode": None,
            "no_gpu_price": NO_GPU_PRICE,
            "disk": dict.fromkeys(DISK_FIELDS)}
    plan["next_instance"] += 1
    plan["instances"].append(item)
    return item


def instance_mode(item):
    """开机模式：no-gpu（无卡开机，含持续请求等待 GPU）或 gpu。没有这个键 = 未观测。

    用 .get 而不是直取：新增字段要保持向后兼容，老计划少了这两个键也要照常读得出来。
    """
    mode = item.get("mode")
    return mode if mode in MODES else None


def instance_rate(item):
    """这台实例现在每小时的费率（元/小时）：无卡按无卡费率，其余按 GPU 时价。

    模式未观测时按 GPU 时价算——宁可高估花费，也不让人以为余额还能跑很久。
    费率没登记就返回 None：缺数据就缺结论，绝不拿 0 顶替（无卡不等于免费，0 更会误导）。
    """
    if instance_mode(item) == "no-gpu":
        return item.get("no_gpu_price", NO_GPU_PRICE)
    return item["price"]


def add_flow(plan, name):
    item = {"id": f"F{plan['next_flow']:02d}", "name": name, "next_task": 1}
    plan["next_flow"] += 1
    plan["flows"].append(item)
    return item


def add_task(plan, flow_id, name, instance=None, duration=None, deps=None, not_before=None):
    flow = find(plan, "flows", flow_id)
    item = {"id": f"{flow_id}-T{flow['next_task']:02d}", "flow": flow_id, "name": name,
            "instance": instance, "deps": deps or [], "duration_minutes": duration,
            "not_before": not_before, "status": "pending", "progress": None,
            "started_at": None, "finished_at": None, "checked_at": None, "progress_at": None,
            "note": ""}
    flow["next_task"] += 1
    plan["tasks"].append(item)
    return item


def set_task(plan, task_id, changes, now=None):
    now = now or utcnow()
    task = find(plan, "tasks", task_id)
    before = task["status"]
    after = changes.get("status", before)
    if before == "running":
        for key in ("instance", "deps", "not_before"):
            if key in changes and changes[key] != task[key]:
                raise PlanError("运行中不能改实例/依赖/开始限制；先核实远程进程已停止，再标为 blocked 或结束")
    if after != before and after not in TRANSITIONS[before]:
        raise PlanError(f"不允许 {before} → {after}；重试先改为 pending")
    task.update(changes)
    if after == "running" and before != "running":
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
        task.update(started_at=None, finished_at=None, progress=None,
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
        # stale 只对执行中的任务有意义：其余状态没有"需要重新巡检"的概念。
        results[task["id"]] = {"start": stamp(start) if start else None,
                               "finish": stamp(end) if end else None,
                               "reason": reason, "overdue": overdue, "ready": False,
                               "stale": task["status"] == "running" and (
                                   not task["checked_at"]
                                   or (now - date(task["checked_at"])).total_seconds() > 3600)}
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


def budget(plan, now=None, estimates=None):
    """余额 × 当前费率 × 剩余计划能推出的全部结论：缺数据就缺结论，绝不用 0 顶替。

    费率按实例当前的开机模式取：无卡开机按无卡费率（0.1 元/小时，不是免费），有卡按 GPU 时价。
    金额一律只按现金余额 assets 计算，代金券单独展示——否则「2 元现金 + 500 元代金券」
    会得出「还能跑 1 小时」这种吓人的错误结论。
    """
    now = now or utcnow()
    estimates = forecast(plan, now) if estimates is None else estimates
    wallet = plan["wallet"]
    assets = wallet["assets"]
    rates = {item["id"]: instance_rate(item) for item in plan["instances"]}
    result = {}
    if assets is not None:
        result["assets"] = round(assets, 2)
        if wallet["voucher"]:
            result["voucher"] = round(wallet["voucher"], 2)
        if wallet["observed_at"]:
            result["observed_at"] = wallet["observed_at"]
        awake = [item for item in plan["instances"] if item["status"] in AWAKE]
        # 任一开机实例缺费率，烧钱速度就是残缺的；低估会让人以为还能跑很久，所以宁可不下结论。
        if awake and all(rates[item["id"]] is not None for item in awake):
            result["burn_rate"] = round(sum(rates[item["id"]] for item in awake), 4)
            result["hours_awake"] = round(assets / result["burn_rate"], 1)
    # 剩余计划按"实例最后完成时间"衡量：AutoDL 按开机时长计费，这比累加任务耗时更贴近真实花销。
    lanes, unknown = {}, False
    for task in plan["tasks"]:
        resource = task["instance"]
        if resource in (None, "local") or task["status"] in TERMINAL:
            continue
        finish = estimates[task["id"]]["finish"]
        if finish is None:
            unknown = True
            continue
        moment = date(finish)
        if resource not in lanes or moment > lanes[resource]:
            lanes[resource] = moment
    if lanes and not unknown:
        hours = {key: max(0.0, (moment - now).total_seconds() / 3600) for key, moment in lanes.items()}
        result["plan_gpu_hours"] = round(sum(hours.values()), 1)
        # 这里故意用 GPU 时价而不是 instance_rate()：待跑的任务将来是在有卡模式下跑的，
        # 按无卡费率去估会把花费压低（"余额够"的假结论），缺 GPU 时价就少给结论。
        prices = {item["id"]: item["price"] for item in plan["instances"]}
        if all(prices.get(key) is not None for key in hours):
            result["plan_cost"] = round(sum(hours[key] * prices[key] for key in hours), 2)
            if assets is not None:
                result["covers_plan"] = assets >= result["plan_cost"]
    return result


def estimate_view(plan, now=None):
    """结算快照 = 实时预算里"算得出的那几个结论"；缺数据就缺键，不拿 0 顶替。"""
    view = budget(plan, now)
    return {key: view[key] for key in ESTIMATE_KEYS if key in view}


def plan_fingerprint(plan):
    """结算结论只由这些字段推出，用它们的指纹判断"结算之后计划有没有变过"。

    不能拿 revision 当判据：monitor 自己每轮都要更新 last_run，用完 revision 会永远显示"有变更"。
    """
    # 开机模式与无卡费率同样进指纹：它们会改变 burn_rate/hours_awake，改了就该判成"结算后计划有变更"。
    # 老计划缺这两个键，所以用 .get 读成 None，而不是让指纹报错。
    payload = [[item.get(key) for key in ("id", "status", "mode", "price", "no_gpu_price")]
               for item in plan["instances"]]
    payload += [[task[key] for key in ("id", "status", "deps", "instance", "duration_minutes",
                                       "not_before", "started_at", "finished_at", "progress",
                                       "progress_at")] for task in plan["tasks"]]
    payload.append([plan["wallet"][key] for key in ("assets", "voucher", "observed_at")])
    blob = json.dumps(sorted(payload, key=str), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def settlement_summary(view):
    """把一份预算结论压成一句话：结算记录、show 与面板共用同一份文案，不各写一套。"""
    parts = []
    if view.get("plan_gpu_hours") is not None:
        owed = f"跑完剩余计划 {view['plan_gpu_hours']:g} GPU 小时"
        if view.get("plan_cost") is not None:
            owed += f"、约 ¥{view['plan_cost']:.2f}"
        parts.append(owed)
    if view.get("covers_plan") is not None:
        parts.append("余额够" if view["covers_plan"] else "余额不足")
    return " · ".join(parts) or "数据不足，暂无结论"


def settlement(plan, now=None):
    """show 与面板共用的结算视图：文案与"是否醒目"都在服务端定死，展示方不做判断。

    有新鲜结算（≤ 2 个周期）就用结算结论并注明结算时间；过期或从未结算则回退实时预算
    并说明原因——绝不同时给出两个互相矛盾的数字。
    """
    now = now or utcnow()
    monitor = plan["monitor"]
    estimate = monitor.get("estimate")
    interval = monitor.get("interval_minutes") or DEFAULT_INTERVAL_MINUTES
    view = {"live": True, "over": False, "text": "", "values": {}, "computed_at": None, "age": None,
            "changed": False, "error": monitor.get("last_error") or "",
            "token_ready": bool(monitor.get("token_ready"))}
    if estimate:
        computed = date(estimate["computed_at"])  # age_text 收 ISO 字符串，这里要的是时刻本身
        view["computed_at"], view["age"] = estimate["computed_at"], age_text(estimate["computed_at"], now)
        if (now - computed).total_seconds() <= 2 * interval * 60:
            view["live"] = False
            view["values"] = {key: estimate[key] for key in ESTIMATE_KEYS if key in estimate}
            # 结算之后再改过计划，这份结论就不再代表当前计划——必须标出来，别让人当真。
            view["changed"] = estimate.get("plan_hash") != plan_fingerprint(plan)
            view["text"] = f"自动结算于 {view['age']} · {settlement_summary(estimate)}"
            view["over"] = estimate.get("covers_plan") is False
        else:
            view["text"] = f"自动结算已过期（{view['age']}，超过 2 个周期）· 当前为实时值"
    else:
        view["text"] = "尚未自动结算 · 运行 trainops monitor 开启定时结算"
    if view["live"]:
        view["values"] = estimate_view(plan, now)
    if view["error"]:
        view["text"] += " · 上次结算失败"
        view["over"] = True
    if not view["token_ready"]:
        view["text"] += " · 未配置 Token"
    if view["changed"]:
        view["text"] += " · 结算后计划有变更"
    return view


def snapshot(plan, now=None):
    now = now or utcnow()
    estimates = forecast(plan, now)
    return {**plan, "generated_at": stamp(now), "budget": budget(plan, now, estimates),
            "settlement": settlement(plan, now),
            "tasks": [{**task, "estimate": estimates[task["id"]]} for task in plan["tasks"]]}


def demo_plan(now=None):
    now = now or utcnow()
    plan = new_plan("双实例实验 · 演示（不会连接服务器）")
    add_instance(plan, "基线 / GPU A", "DEMO-NOT-REAL-A", "demo-gpu-a")
    add_instance(plan, "消融 / GPU B", "DEMO-NOT-REAL-B", "demo-gpu-b")
    # 演示数据一律是编造的固定值：不连服务器、不假装是真实观测。
    disks = ({"system_used_gb": 12.4, "system_total_gb": 30, "data_used_gb": 40.2, "data_total_gb": 50,
              "fs_used_gb": 5, "fs_total_gb": 200},
             {"system_used_gb": 9.1, "system_total_gb": 30, "data_used_gb": 22.5, "data_total_gb": 50,
              "fs_used_gb": 5, "fs_total_gb": 200})
    for instance, price, disk in zip(plan["instances"], (1.88, 1.58), disks):
        instance.update(status="on", mode="gpu", observed_at=stamp(now), note="演示占位，非真实实例",
                        price=price, disk=disk)
    plan["wallet"] = {"assets": 128.5, "voucher": 20.0, "observed_at": stamp(now)}
    add_flow(plan, "基线模型")
    add_flow(plan, "消融对照")
    add_flow(plan, "结果汇总")
    add_task(plan, "F01", "数据与代码就绪", "local", 10)
    set_task(plan, "F01-T01", {"status": "running"}, now - timedelta(minutes=30))
    set_task(plan, "F01-T01", {"status": "done"}, now - timedelta(minutes=20))
    add_task(plan, "F01", "基线训练", "I01", 120, ["F01-T01"])
    set_task(plan, "F01-T02", {"status": "running"}, now - timedelta(minutes=20))
    set_task(plan, "F01-T02", {"progress": 18, "checked_at": stamp(now)}, now)
    add_task(plan, "F01", "基线评估", "I01", 15, ["F01-T02"])
    add_task(plan, "F02", "消融训练", "I02", 90, ["F01-T01"])
    set_task(plan, "F02-T01", {"status": "running"}, now - timedelta(minutes=20))
    set_task(plan, "F02-T01", {"progress": 15, "checked_at": stamp(now)}, now)
    add_task(plan, "F02", "消融评估", "I02", 15, ["F02-T01"])
    add_task(plan, "F03", "比较指标并整理报告", "local", 20, ["F01-T03", "F02-T02"])
    add_task(plan, "F03", "根据结果设计下一轮", "local", None, ["F03-T01"])
    # 演示里也放一份自动结算记录（生成演示的这一刻算出来的），面板因此能直接看到结算徽标。
    # token_ready 与余额一样是编造的演示值：fresh clone 里没有 .env，不该在演示面板上催配置。
    plan["revision"] = 1
    plan["monitor"].update(last_run=stamp(now), last_ok=stamp(now), token_ready=True)
    plan["monitor"]["estimate"] = {"computed_at": stamp(now), "revision": 1,
                                   "plan_hash": plan_fingerprint(plan), **estimate_view(plan, now)}
    return plan


def check_structure(plan, now=None, *, amounts=True):
    """Structural check for flow continuity, dependency links, and resource/duration readiness.

    issues 是需要修正的结构缺陷（ok 为假），warnings 只是观察到的现象，不影响 ok。
    """
    validate(plan)
    issues, warnings = [], []
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
                        warnings.append(f"{task['id']}: 实际开始时间晚于前置依赖完成时间 {int(gap_min)} 分钟，存在时间空隙")
    # 以下三条都只在"数据已记录且有问题"时才出现：没有观测就不该凭空冒出提醒。
    for item in plan["instances"]:
        for _, field, label in DISK_MOUNTS:
            used, total = item["disk"][f"{field}_used_gb"], item["disk"][f"{field}_total_gb"]
            if used is None or not total or used / total < FULL_RATIO:
                continue
            warnings.append(f"{item['id']}: {label}已用 {used:g}/{total:g}G"
                            f"（{used / total * 100:.0f}%），请先清理或扩容再开新任务")
    view = budget(plan, now)
    if view.get("assets") is not None:
        missing = [item["id"] for item in plan["instances"]
                   if item["status"] in AWAKE and instance_rate(item) is None]
        if missing:
            warnings.append("未登记时价（" + ", ".join(missing) +
                            "），无法把余额换算成可用时长；请打开面板（serve）在实例卡上登记")
        if view.get("covers_plan") is False:
            detail = "实时预算提醒：余额不足以覆盖剩余计划"
            if amounts:
                detail = f"余额 ¥{view['assets']:.2f} 不足以覆盖剩余计划（预估 ¥{view['plan_cost']:.2f}）"
            warnings.append(detail + "，请充值或缩减计划")
    # 自动结算相关提醒：只报"正在计费却没人算账"和"算了但失败了"这两种要处置的情况。
    if plan["monitor"].get("last_error"):
        warnings.append("自动结算失败：" + plan["monitor"]["last_error"]
                        + "；处理后运行 monitor --once 立即重算")
    if any(item["status"] in AWAKE for item in plan["instances"]) and settlement(plan, now)["live"]:
        warnings.append("有实例开机计费，但自动结算未在运行；用 trainops monitor（或 serve --interval）"
                        "定时重算余额与剩余计划的 GPU 小时")
    return {"ok": not issues, "flows": len(plan["flows"]), "tasks": len(plan["tasks"]),
            "issues": issues, "warnings": warnings}


def api_base():
    return os.environ.get("TRAINOPS_API_BASE", "https://api.autodl.com").rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """带 token 的请求不接受重定向：urllib 会把 Authorization 一起转发到新地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PlanError("AutoDL 接口返回了重定向；为避免 token 被转发到其他地址已中止")


def opener():
    return urllib.request.build_opener(NoRedirect())


def api_post(path, token, timeout=20.0):
    """唯一的外部调用：只读接口，无请求体。token 只进请求头，绝不写进计划、输出或异常文本。"""
    request = urllib.request.Request(api_base() + path, data=b"", method="POST")
    request.add_header("Authorization", token)
    try:
        with opener().open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise PlanError(f"AutoDL 接口返回 HTTP {exc.code}；请检查 token 是否正确、是否已完成实名认证") from None
    except PlanError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise PlanError(f"无法连接 AutoDL 接口（{getattr(exc, 'reason', exc)}）；本地计划未受影响") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PlanError(f"AutoDL 接口返回的不是合法 JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise PlanError("AutoDL 接口返回格式异常")
    if payload.get("code") != "Success":
        message = str(payload.get("msg") or "").strip() or "未知错误"
        request_id = str(payload.get("request_id") or "").strip()
        raise PlanError(f"AutoDL 接口返回失败: {message}" + (f"（request_id={request_id}）" if request_id else ""))
    return payload


def milli_to_yuan(value, label):
    """官方约定：余额/代金券都是"千分之一元"的整数；不是整数就说明单位变了，宁可报错也不写错 1000 倍。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"AutoDL 返回的{label}不是数字")
    if not math.isfinite(value) or value < 0:
        raise PlanError(f"AutoDL 返回的{label}异常: {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise PlanError(f"AutoDL 返回的{label} {value!r} 不是整数；官方约定为千分之一元的整数，"
                        "单位可能已变化，请先核对文档")
    return round(value / 1000, 2)


def balance_amounts(payload):
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("assets") is None:
        raise PlanError("AutoDL 余额接口未返回 assets 字段")
    amounts = {"assets": milli_to_yuan(data["assets"], "余额")}
    voucher = data.get("voucher_balance")
    amounts["voucher"] = None if voucher is None else milli_to_yuan(voucher, "代金券")
    return amounts


def env_path():
    """Token 从仓库根目录的 .env 读；TRAINOPS_ENV 可指向别处（测试或另一套凭据）。"""
    override = os.environ.get("TRAINOPS_ENV", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[3] / ".env"


def load_env(path):
    """只认 KEY=VALUE 与整行 # 注释，只取 AUTODL_TOKEN 一个键；文件不存在不算错误。

    值里的 # 可能是 token 的一部分，因此不做行内注释剥离；只剥成对的引号。
    """
    try:
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanError(f"无法读取 {path}: {exc}") from None
    values = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def resolve_token(explicit=None):
    """--token > 环境变量 AUTODL_TOKEN > 仓库根 .env；只在内存中使用，绝不回显。"""
    if explicit and explicit.strip():
        return explicit.strip()
    token = os.environ.get("AUTODL_TOKEN", "").strip()
    if token:
        return token
    path = env_path()
    token = (load_env(path).get("AUTODL_TOKEN") or "").strip()
    if token:
        try:
            if path.stat().st_mode & 0o077:
                print(f"TrainOps: 提醒 {path} 权限过松，建议 chmod 600", file=sys.stderr)
        except OSError:
            pass
        return token
    raise PlanError(f"未配置开发者 Token：请设置环境变量 AUTODL_TOKEN，或在 {path} 里写入一行 "
                    "AUTODL_TOKEN=...（格式见仓库根的 .env.example）。"
                    "Token 获取路径：控制台 → 设置 → 开发者Token")


def set_wallet(plan, changes, observed_at=None):
    """余额只能从这一处写入：无论是接口拉取还是手工登记，观测时间都由这里统一盖章。

    observed_at 用取数那一刻的时间：结算在文件锁外联网，盖章时间必须早于落盘时间。
    """
    plan["wallet"].update(changes)
    plan["wallet"]["observed_at"] = observed_at or stamp()
    return plan["wallet"]


def wallet_fetch(store, args):
    """联网取数一律在文件锁之外完成，落盘只用一次 store.change()：超时不会卡住并发的写进程。"""
    number(args.timeout, "超时秒数", low=0.5, high=300)
    token = resolve_token(args.token)
    fetched_at = stamp()
    amounts = balance_amounts(api_post(API_BALANCE_PATH, token, timeout=args.timeout))
    if args.dry_run:
        return {"wallet": amounts, "saved": False}
    result = store.change(lambda plan: set_wallet(plan, amounts, fetched_at))
    return {"revision": result["revision"], "wallet": result["item"], "saved": True}


def sanitize_error(exc):
    """last_error 会落盘也会显示：压成单行并截断，别把多行堆栈写进计划文件。"""
    return " ".join(str(exc).split())[:ERROR_LIMIT]


def cycle(store, interval=None, token=None, timeout=20.0, expires_at=KEEP):
    """一轮自动结算：联网在文件锁之外，落盘只用一次 store.change()。

    取数失败（断网、401、超时）只记进 last_error 并保留上一份结论，不抛出——
    定时进程不该因为一次断网就退出；没配 token 更不算故障（那是"没开自动余额"）。
    """
    now = utcnow()
    error, amounts, observed_at, resolved = "", None, None, None
    try:
        resolved = resolve_token(token)
    except PlanError:
        resolved = None
    if resolved:
        try:
            observed_at = stamp(now)
            amounts = balance_amounts(api_post(API_BALANCE_PATH, resolved, timeout=timeout))
        except PlanError as exc:
            error = sanitize_error(exc)

    def operation(plan):
        monitor = plan["monitor"]
        if amounts is not None:
            set_wallet(plan, amounts, observed_at)
        if interval is not None:
            monitor["interval_minutes"] = interval
        if expires_at is not KEEP:
            monitor["expires_at"] = expires_at
        monitor["last_run"] = stamp(now)
        monitor["token_ready"] = resolved is not None
        monitor["last_error"] = error
        if not error:
            monitor["last_ok"] = stamp(now)
            # 结算与写入同一次 change：revision 就是这份结论对应的版本，之后任何改动都能对上。
            monitor["estimate"] = {"computed_at": stamp(now), "revision": plan["revision"],
                                   "plan_hash": plan_fingerprint(plan), **estimate_view(plan, now)}
        return {"wallet": dict(plan["wallet"]), "monitor": dict(monitor),
                "settlement": settlement(plan)}

    result = store.change(operation)
    return {"revision": result["revision"], **result["item"]}


def run_monitor(store, args, sleep=time.sleep, monotonic=time.monotonic):
    """前台结算循环；--once 只跑一轮并交给 main 输出 JSON，方便挂到系统 cron。

    节奏用单调时钟对齐，免得每轮的网络耗时把周期越拖越长；--expires-at 到点自动退出，
    不留守护进程。
    """
    interval = getattr(args, "interval", None)
    expires = getattr(args, "expires_at", KEEP)
    deadline = date(expires) if isinstance(expires, str) else None
    if args.once:
        return cycle(store, interval, args.token, args.timeout, expires)
    interval_minutes = interval
    try:
        while True:
            started = monotonic()
            report = cycle(store, interval, args.token, args.timeout, expires)
            interval_minutes = report["monitor"]["interval_minutes"]
            error = report["monitor"]["last_error"]
            print(f"TrainOps {stamp()} · {report['settlement']['text']}" + (f" | {error}" if error else ""),
                  flush=True)
            if deadline is not None and utcnow() + timedelta(minutes=interval_minutes) >= deadline:
                print(f"TrainOps: 已到 --expires-at {expires}，结算进程退出", flush=True)
                return None
            # 周期从本轮开始时刻算起，把网络耗时抵消掉，长期跑不会越漂越慢。
            sleep(max(1.0, interval_minutes * 60 - (monotonic() - started)))
    except KeyboardInterrupt:
        print("TrainOps: 收到中断，结算进程停止（计划文件已是最新）", flush=True)
        return None


def background_monitor(store, interval, token=None, timeout=20.0):
    """面板进程内的结算线程：daemon 线程，失败只记进计划，绝不把面板拖死。"""
    def loop():
        while True:
            try:
                cycle(store, interval, token, timeout)
            except (PlanError, OSError, ValueError) as exc:  # 计划被删/写坏也不该掀掉面板
                print(f"TrainOps: 结算失败 {sanitize_error(exc)}", file=sys.stderr, flush=True)
            time.sleep(interval * 60)

    thread = threading.Thread(target=loop, daemon=True, name="trainops-monitor")
    thread.start()
    return thread


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

    def local(self):
        """只认 127.0.0.1/localhost：外部域名解析到本机也不能读走或改写计划（防 DNS rebinding）。"""
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.server.server_port == 80:
            hosts.update({"127.0.0.1", "localhost"})
        return self.headers.get("Host", "").lower() in hosts

    def do_GET(self):
        if not self.local():
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

    def do_POST(self):
        """唯一写入口：面板登记时价。其余一切都还是只读。

        页面只监听本机，但请求可能来自别处，所以三道关：Host 白名单、Origin 必须是本机同源、
        Content-Type 必须是 application/json（跨站 <form> 发不出这个类型，跨站 JSON fetch 要先过预检，
        而本服务不答 OPTIONS、不发 CORS 头）。落盘照旧只走 store.change()，不新增第二条写路径。
        """
        if not self.local():
            self.respond(403, {"error": "只允许本机 Host，不能通过外部域名修改计划"})
            return
        path = urlsplit(self.path).path
        if path != PRICE_PATH:
            # 在已知的读取路径上写是"方法不允许"，不存在的路径才是 404。
            if path in ("/", "/api/plan"):
                self.reject_write()
            else:
                self.respond(404, {"error": "not found"})
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {
                f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
            self.respond(403, {"error": "只接受本机面板发出的请求"})
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self.respond(415, {"error": "登记时价只接受 application/json"})
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.respond(400, {"error": "请求缺少合法的 Content-Length"})
            return
        if not 0 <= size <= BODY_LIMIT:
            self.respond(413, {"error": f"请求体过大，最多 {BODY_LIMIT} 字节"})
            return
        try:
            payload = json.loads(self.rfile.read(size))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.respond(400, {"error": f"请求体不是合法 JSON：{exc}"})
            return
        # 键集必须恰好是这两种之一：多一个键就报错，免得"顺手改点别的"从这里溜进计划。
        keys = set(payload) if isinstance(payload, dict) else set()
        if keys not in ({"id", "price"}, {"id", "no_gpu_price"}):
            self.respond(400, {"error": '请求体必须恰好是 {"id": 实例编号, "price": 元/小时或 null}'
                                        '（无卡费率用 no_gpu_price）'})
            return
        field = "price" if "price" in keys else "no_gpu_price"
        try:
            plan = self.store.read()
        except (PlanError, OSError, OverflowError) as exc:  # 计划损坏是服务端问题，不能报成 400
            self.respond(500, {"error": str(exc)})
            return
        try:
            instance_id = payload["id"]
            text(instance_id, "实例编号", required=True)
            price = price_value(payload[field])
            find(plan, "instances", instance_id)
        except PlanError as exc:
            self.respond(400, {"error": str(exc)})
            return
        try:
            self.store.change(lambda current: set_instance_price(current, instance_id, field, price))
            self.respond(200, snapshot(self.store.read()))
        except (PlanError, OSError, OverflowError) as exc:
            self.respond(500, {"error": str(exc)})

    def reject_write(self):
        self.respond(405, {"error": "只有时价能在面板上登记，其余修改请通过 agent/CLI"})

    do_PUT = do_PATCH = do_DELETE = reject_write

    def log_message(self, format, *args):
        pass


def server(store, port=8765):
    return ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, store=store))


def nullable(value):
    """none / ? / - 都表示"不设置"，方便在简写里跳过中间字段。"""
    return None if value in ("none", "?", "-") else value


def duration(value):
    if value in ("none", "?", "-"):
        return None
    try:
        result = float(value)
        number(result, "预计分钟数")
        return result
    except (ValueError, PlanError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def interval_arg(value):
    """结算周期（分钟）：在解析阶段就拒绝非法值，别等落盘时才发现。"""
    if not re.fullmatch(r"\d+", value.strip()):
        raise argparse.ArgumentTypeError(f"结算周期应为分钟数，实际为 {value!r}")
    result = int(value)
    try:
        integer(result, "结算周期", INTERVAL_LIMITS[0])
        if result > INTERVAL_LIMITS[1]:
            raise PlanError(f"结算周期必须在 {INTERVAL_LIMITS[0]} 到 {INTERVAL_LIMITS[1]} 分钟之间")
    except PlanError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return result


def moment(value):
    """带时区的 ISO 时间参数；none / - / ? 表示清空。"""
    value = nullable(value)
    if value is None:
        return None
    try:
        return stamp(date(value))
    except PlanError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def dependencies(value):
    return [] if value.strip() in ("", "-") else [part.strip() for part in value.split(",") if part.strip()]


def gigabytes(value, label):
    if value in ("none", "?", "-", ""):
        return None
    try:
        result = float(value)
    except ValueError as exc:
        raise PlanError(f"{label}应为 GB 数字，实际为 {value!r}") from exc
    number(result, label, high=1_000_000)
    return result


def disk_changes(spec):
    """'data:40/50,fs:5/200' → 字段字典。按盘名逐项合并：没提到的盘保持原值，不会被清空。"""
    changes = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        prefix, sep, value = entry.partition(":")
        prefix, value = prefix.strip().lower(), value.strip()
        if not sep or prefix not in DISK_PREFIXES:
            raise PlanError(f"磁盘写成 '盘名:已用/总量'（盘名取 {'/'.join(DISK_PREFIXES)}），实际为 {entry!r}")
        field, label = DISK_PREFIXES[prefix]
        if value in ("none", "?", "-"):
            changes[f"{field}_used_gb"] = changes[f"{field}_total_gb"] = None
            continue
        used, slash, total = value.partition("/")
        if not slash:
            raise PlanError(f"{label}应写成 '已用/总量'（如 {prefix}:40/50），实际为 {value!r}")
        changes[f"{field}_used_gb"] = gigabytes(used.strip(), f"{label}已用")
        changes[f"{field}_total_gb"] = gigabytes(total.strip(), f"{label}总量")
    if not changes:
        raise PlanError("--disk 未提供任何磁盘，格式如 --disk \"data:40/50,fs:5/200\"")
    return changes


def disk_arg(value):
    try:
        return disk_changes(value)
    except PlanError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def price_value(raw):
    """时价（元/小时）或 None（未登记）。none/?/-/空串表示清空。

    面板接口是唯一写入口，所以这里必须把住边界：0、负数、nan 都拒绝——缺数据不能拿 0 顶替，
    而"时价 0"会让人以为机器白跑。落盘前 validate() 会再查一遍。
    """
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() in ("", "none", "?", "-"):
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise PlanError(f"时价应为元/小时数字，实际为 {raw!r}")
    try:
        result = float(raw)
    except ValueError as exc:
        raise PlanError(f"时价应为元/小时数字，实际为 {raw!r}") from exc
    number(result, "时价", low=PRICE_LIMITS[0], high=PRICE_LIMITS[1])
    return result


def set_instance_price(plan, instance_id, field, price):
    """登记时价（元/小时），price 为 None 表示清空。

    field 只能是 price（GPU 时价）或 no_gpu_price（无卡费率）。时价是"标价"，不构成一次对机器的观测，
    因此不刷新 observed_at——那个字段记录的是"上次有人看过这台机器"。
    """
    if field not in ("price", "no_gpu_price"):
        raise PlanError(f"只能登记 price 或 no_gpu_price，实际为 {field!r}")
    item = find(plan, "instances", instance_id)
    item[field] = price
    return item


def money_arg(value):
    if value in ("none", "?", "-", ""):
        return None
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"金额应为元数字，实际为 {value!r}") from exc
    try:
        number(result, "金额", high=1_000_000)
    except PlanError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return round(result, 2)


def parse_instance_spec(spec):
    parts = [part.strip() for part in spec.split(":", 2)]
    if len(parts) != 3 or not all(parts):
        raise PlanError(f"实例简写格式应为 '名称:autodl_id:ssh_alias'，实际为 {spec!r}")
    return parts[0], parts[1], parts[2]


def parse_task_spec(spec):
    """'流程ID:名称[:实例[:分钟数[:依赖]]]'；只想给靠后的字段时，前面的用 - 占位。"""
    parts = (spec.split(":", 4) + [""] * 5)[:5]
    parts = [part.strip() for part in parts]
    if not parts[0] or not parts[1]:
        raise PlanError(f"任务简写格式应为 '流程ID:名称[:实例[:分钟数[:依赖]]]'（跳过中间字段用 -），实际为 {spec!r}")
    return parts[0], parts[1], nullable(parts[2]) if parts[2] else None, \
        duration(parts[3]) if parts[3] else None, dependencies(parts[4])


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


def disk_text(disk):
    """压成一行 '系统 12/30G·数据 40/50G'；≥90% 的盘打 ! ，让巡检的 agent 一眼看到。"""
    chunks = []
    for _, field, label in DISK_MOUNTS:
        used, total = disk[f"{field}_used_gb"], disk[f"{field}_total_gb"]
        if used is None and total is None:
            continue
        name = DISK_SHORT[label]
        if used is not None and total:
            chunks.append(f"{name} {used:g}/{total:g}G" + ("!" if used / total >= FULL_RATIO else ""))
        elif used is not None:
            chunks.append(f"{name} {used:g}G")
        else:
            chunks.append(f"{name} ?/{total:g}G")
    return "·".join(chunks)


def instance_line(item):
    extras = []
    # 无卡开机没有 GPU 在计费，报的是另一档费率；模式没登记过就还是照 GPU 时价显示。
    if instance_mode(item) == "no-gpu":
        extras.append(f"无卡 ¥{instance_rate(item):g}/h" if instance_rate(item) is not None
                      else "无卡（费率未登记）")
    elif item["price"] is not None:
        extras.append(f"¥{item['price']:g}/h")
    disks = disk_text(item["disk"])
    if disks:
        extras.append("盘 " + disks)
    suffix = (" " + " ".join(extras)) if extras else ""
    return (f"{item['id']} {item['name']}({item['ssh_alias']}/{item['autodl_id']})"
            f"[{item['status']}]{suffix}")


def budget_parts(view):
    """余额能推出的结论压成一行；缺数据就少说一句，绝不用 0 顶替。"""
    entry = view["budget"]
    parts = []
    if entry.get("assets") is not None:
        parts.append(f"余额 ¥{entry['assets']:.2f}")
        if entry.get("observed_at"):
            parts.append("观测于 " + age_text(entry["observed_at"]))
        if entry.get("voucher"):
            parts.append(f"代金券 ¥{entry['voucher']:.2f}（不计入可用时长）")
        missing = [item["id"] for item in view["instances"]
                   if item["status"] in AWAKE and instance_rate(item) is None]
        if entry.get("hours_awake") is not None:
            parts.append(f"按开机费率 ¥{entry['burn_rate']:g}/h 还能跑 {entry['hours_awake']:g} 小时")
        elif missing:
            parts.append("未登记时价（" + ",".join(missing) + "），无法把余额换算成可用时长，请在面板登记")
    if entry.get("plan_gpu_hours") is not None:
        owed = f"跑完剩余计划需 {entry['plan_gpu_hours']:g} GPU 小时"
        if entry.get("plan_cost") is not None:
            owed += f"、预估 ¥{entry['plan_cost']:.2f}"
        parts.append(owed)
    if entry.get("covers_plan") is not None:
        parts.append("余额足以覆盖" if entry["covers_plan"] else "余额不足以覆盖剩余计划，请充值或缩减计划")
    return parts


def age_text(moment, now=None):
    """余额是易腐数据：给人看'3 小时前'，比让人自己换算 ISO 时间戳更有用。"""
    seconds = max(0, int(((now or utcnow()) - date(moment)).total_seconds()))
    if seconds < 90:
        return "刚刚"
    if seconds < 5400:
        return f"{seconds // 60} 分钟前"
    if seconds < 36 * 3600:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def brief_text(value, limit=BRIEF_TEXT_LIMIT):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def brief_group(label, entries):
    if not entries:
        return []
    lines = [f"{label}（共 {len(entries)} 条）:"]
    lines.extend("  " + brief_text(entry) for entry in entries[:BRIEF_LIMIT])
    if len(entries) > BRIEF_LIMIT:
        lines.append(f"  …省略 {len(entries) - BRIEF_LIMIT} 条")
    return lines


def brief_finance(wallet, view, now=None):
    parts = []
    if wallet["assets"] is not None:
        parts.append(f"余额 ¥{wallet['assets']:.2f}")
    else:
        parts.append("余额未登记，无法判断现金覆盖情况")
    if wallet.get("observed_at"):
        parts.append("观测于 " + age_text(wallet["observed_at"], now))
    if wallet.get("voucher"):
        parts.append(f"代金券 ¥{wallet['voucher']:.2f}（不计入可用时长）")
    lines = ["钱包: " + " | ".join(parts)]
    values = view["values"]
    if values.get("hours_awake") is not None:
        lines.append(f"可用时长（同结算口径）: {values['hours_awake']:g} 小时")
    lines.append("结算: " + brief_text(view["text"], 240))
    if view["live"]:
        lines.append("实时结论: " + settlement_summary(values))
    if view["changed"]:
        lines.append("当前展示为变更前的结算；请 monitor --once 重算后决策")
    if view["error"]:
        lines.append("结算错误: " + brief_text(view["error"], ERROR_LIMIT))
    return lines


def brief_plan(plan, now=None):
    now = now or utcnow()
    view = snapshot(plan, now)
    # 诊断保留实时风险，但不再展开另一套金额，避免与仍有效的旧结算混淆。
    report = check_structure(plan, now, amounts=False)
    tasks = sorted(view["tasks"], key=lambda task: natural_id(task["id"]))
    instances = {item["id"]: item for item in view["instances"]}
    counts = {status: 0 for status in STATUSES}
    for task in tasks:
        counts[task["status"]] += 1
    lines = [f"{brief_text(view['name'])} · rev {view['revision']}",
             "任务: " + " | ".join(f"{status} {counts[status]}" for status in STATUSES)]
    lines.extend(brief_finance(plan["wallet"], view["settlement"], now))

    def task_line(task):
        eta = task["estimate"]
        resource = task["instance"]
        if resource in instances:
            resource += f"[{instances[resource]['status']}]"
        progress = f" {task['progress']:g}%" if task["progress"] is not None else ""
        marks = [label for key, label in (("overdue", "已超期"), ("stale", "未巡检")) if eta[key]]
        if task["not_before"] and date(task["not_before"]) > now:
            marks.append("最早开始 " + task["not_before"])
        detail = " · ".join([eta["reason"], *marks]).strip(" ·")
        return (f"{task['id']} [{task['status']}{progress}] {resource or '未分配'} "
                f"完成 {eta['finish'] or '待估'} | {detail} | {task['name']}")

    running = [task for task in tasks if task["status"] == "running"]
    attention = [task for task in tasks if task["status"] in ("failed", "blocked")
                 or (task["status"] not in TERMINAL and (
                     task["estimate"]["overdue"] or task["estimate"]["stale"]
                     or task["estimate"]["finish"] is None
                     or (task["not_before"] and date(task["not_before"]) > now)))]
    lines.extend(brief_group("运行中", [task_line(task) for task in running]))
    lines.extend(brief_group("待处置/待估", [task_line(task) for task in attention]))
    ready = sorted((task for task in tasks if task["estimate"]["ready"]),
                   key=lambda task: (task["estimate"]["start"], natural_id(task["id"])))
    candidates, seen = [], set()
    for task in ready:
        resource = task["instance"]
        if resource != "local" and resource in seen:
            continue
        seen.add(resource)
        candidates.append(task_line(task))
    lines.append(f"依赖就绪 {len(ready)} 个任务（GPU 每实例列一个候选，仍需核实远端）")
    lines.extend(brief_group("候选", candidates))
    if not ready and any(task["status"] not in TERMINAL for task in tasks):
        lines.append("当前无依赖就绪候选，等待运行任务、依赖或开始时间条件；详情用 show")
    lines.extend(brief_group("实例", [instance_line(item) for item in sorted(
        view["instances"], key=lambda item: natural_id(item["id"]))]))
    lines.extend(brief_group("结构问题", report["issues"]))
    lines.extend(brief_group("提醒", report["warnings"]))
    remaining_resources = {task["instance"] for task in tasks if task["status"] not in TERMINAL}
    missing = [f"{item['id']}: 未登记时价，对应费用/可用时长待估（可在面板登记）" for item in view["instances"]
               if instance_rate(item) is None and (item["status"] in AWAKE or item["id"] in remaining_resources)]
    lines.extend(brief_group("缺失信息", missing))
    hidden = counts["done"] + counts["cancelled"]
    if hidden:
        lines.append(f"已省略 {hidden} 个已完成/取消任务的历史明细（取消不满足下游依赖）")
    lines.append("完整计划用 show；完整问题与提醒用 check；机器快照用 show --json")
    return "\n".join(lines)


def brief_receipt(args, result):
    operation = args.operation_name
    saved = result.get("saved", True)
    lines = [f"已保存 · rev {result['revision']}" if saved else "仅预览，未保存"]
    if operation == "monitor":
        lines.extend(brief_finance(result["wallet"], result["settlement"]))
        lines.append("仅余额结算，不代表远端训练已巡检")
    elif operation == "wallet":
        wallet = result.get("wallet", result.get("item"))
        for key, label in (("assets", "余额"), ("voucher", "代金券（不计入可用时长）")):
            lines.append(f"{label}: ¥{wallet[key]:.2f}" if wallet.get(key) is not None else f"{label}: 未登记")
        if wallet.get("observed_at"):
            lines.append("余额观测于 " + age_text(wallet["observed_at"]))
    elif operation in ("init", "batch"):
        item = result if operation == "init" else result["item"]
        for key, label in (("instances", "实例"), ("flows", "流程"), ("tasks", "任务"), ("updated", "更新")):
            lines.extend(brief_group(label, item.get(key, [])))
    else:
        item = result["item"]
        fields = []
        for key, label in (("status", "状态"), ("progress", "进度"), ("instance", "实例"),
                           ("duration_minutes", "耗时"), ("deps", "依赖"), ("not_before", "开始限制"),
                           ("name", "名称"), ("note", "备注"), ("mode", "开机模式"), ("disk", "磁盘")):
            if hasattr(args, key) and (getattr(args, key) is not None or key in (
                    "instance", "duration_minutes", "deps", "not_before", "mode", "disk")):
                value = item.get(key)  # 开机模式等后加字段在老计划里可能还没有这个键
                if value is None:
                    value = "待估" if key == "duration_minutes" else "未登记"
                if key == "progress":
                    value = f"{value:g}%" if value is not None else "待估"
                elif key == "disk":
                    value = disk_text(value) or "未登记"
                fields.append(f"{label} {brief_text(value, 60)}")
        if getattr(args, "checked", False):
            fields.append("已记录真实巡检")
        lines.append(f"{item['id']}: " + " | ".join(fields or [brief_text(item.get("name", "已更新"))]))
    if "check" in result:
        report = result["check"]
        lines.append(f"结构检查: {len(report['issues'])} 个问题，{len(report['warnings'])} 个提醒（已保存）")
        lines.extend(brief_group("结构问题", report["issues"]))
        lines.extend(brief_group("提醒", report["warnings"]))
    if operation in ("init", "batch") or "check" in result:
        lines.append("完整计划用 show；完整问题与提醒用 check")
    return "\n".join(lines)


def structural_edit(args):
    if args.operation_name == "batch":
        return bool(args.instance or args.flow or args.task)
    if getattr(args, "action", None) == "add":
        return True
    return args.operation_name in ("set", "task") and any(
        hasattr(args, key) for key in ("instance", "duration_minutes", "deps", "not_before"))


def compact(data):
    if isinstance(data, dict):
        return {k: compact(v) for k, v in data.items() if v not in ("", None, [])}
    if isinstance(data, list):
        return [compact(v) for v in data]
    return data


def add_task_args(target, is_set=False):
    default = argparse.SUPPRESS if is_set else None
    target.add_argument("--instance", type=nullable, default=default, help="I01 / local / none（- 同 none）")
    target.add_argument("--minutes", dest="duration_minutes", type=duration, default=default, help="预计耗时；? 表示待估")
    target.add_argument("--deps", type=dependencies, default=default, help="逗号分隔；空字符串清空")
    target.add_argument("--not-before", type=nullable, default=default, help="带时区时间；none 清空")
    if is_set:
        target.add_argument("--status", choices=STATUSES)
        target.add_argument("--progress", type=float)
        target.add_argument("--checked", action="store_true", help="记录实际巡检时间")
        target.add_argument("--note")


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--version", action="version", version=f"%(prog)s schema {SCHEMA_VERSION}")
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
    demo = commands.add_parser("demo", help="在空目录生成演示计划，无远程操作")
    demo.add_argument("--force", action="store_true", help="覆盖已有计划（仅演示数据，慎用）")
    check = commands.add_parser("check", help="执行结构性检查（流程连续性、依赖链、实例与耗时完整性）")
    check.add_argument("--strict", action="store_true", help="存在结构问题时以退出码 1 结束，便于脚本判断")
    show = commands.add_parser("show", help="读取最新计划和时间估算")
    show_output = show.add_mutually_exclusive_group()
    show_output.add_argument("--json", action="store_true")
    show_output.add_argument("--brief", action="store_true", help="只看当前行动摘要，分组限量并注明省略数")
    serve = commands.add_parser("serve", help="仅本机面板（唯一可写处是登记时价），Ctrl+C 停止")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--interval", type=interval_arg, default=None,
                       help="顺带在这个进程里跑自动结算（分钟）；不传则只做面板")
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
    instance_set.add_argument("--mode", type=nullable, default=argparse.SUPPRESS,
                              help="开机模式：gpu 有卡 / no-gpu 无卡准备（含持续请求等待）；none 表示未观测")
    instance_set.add_argument("--note")
    instance_set.add_argument("--observed", action="store_true", help="记录本次真实观测时间")
    instance_set.add_argument("--disk", type=disk_arg, default=argparse.SUPPRESS,
                              help="如 \"data:40/50,fs:5/200\"：逐盘合并，none 清空该盘")
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
    monitor = commands.add_parser("monitor", help="自动结算：定时拉取余额并重算剩余计划的 GPU 小时与花费")
    monitor.add_argument("--interval", type=interval_arg, default=None,
                         help=f"结算周期（分钟，{INTERVAL_LIMITS[0]}–{INTERVAL_LIMITS[1]}）；默认沿用计划里的值")
    monitor.add_argument("--once", action="store_true", help="只结算一轮就退出并输出 JSON，供系统 cron 调用")
    monitor.add_argument("--expires-at", type=moment, default=argparse.SUPPRESS,
                         help="到点自动退出（带时区时间）；none 清除")
    monitor.add_argument("--token", help="临时覆盖 .env 与 AUTODL_TOKEN；不会写进计划或输出")
    monitor.add_argument("--timeout", type=float, default=20.0)
    monitor.add_argument("--strict", action="store_true", help="最近一次结算失败时以退出码 1 结束，便于 cron 告警")
    wallet = commands.add_parser("wallet", help="账户余额：fetch 走只读接口自动拉取，set 手工登记")
    wallet_actions = wallet.add_subparsers(dest="action", required=True)
    wallet_fetch_parser = wallet_actions.add_parser("fetch", help="手动拉一次余额：只读接口，不下发任何实例指令")
    wallet_fetch_parser.add_argument("--token", help="覆盖 .env 与 AUTODL_TOKEN；不会写进计划或输出")
    wallet_fetch_parser.add_argument("--timeout", type=float, default=20.0)
    wallet_fetch_parser.add_argument("--dry-run", action="store_true", help="只打印，不写入计划")
    wallet_set = wallet_actions.add_parser("set", help="无 token / 断网时人工登记余额")
    wallet_set.add_argument("--assets", type=money_arg, default=argparse.SUPPRESS, help="余额（元）；none 清空")
    wallet_set.add_argument("--voucher", type=money_arg, default=argparse.SUPPRESS, help="代金券（元）；单独展示不计入可用时长")
    for target in (init, batch, instance_add, instance_set, flow_add, flow_set, task_add,
                   task_set, quick_set, wallet_fetch_parser, wallet_set, monitor):
        target.add_argument("--brief", action="store_true", help="输出紧凑文本回执，不改变保存及退出码语义")
    return root


def apply_batch_specs(plan, instances=(), flows=(), tasks=(), updates=()):
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
        if changes.get("status") == "done" and target["status"] in ("pending", "blocked"):
            set_task(plan, task_id, {"status": "running", "checked_at": changes["checked_at"]})
        set_task(plan, task_id, changes)
        added["updated"].append(task_id)
    return added


def dispatch(args, store):
    if args.operation_name == "init":
        plan = new_plan(args.name)
        apply_batch_specs(plan, args.instance, args.flow, args.task)
        report = check_structure(plan) if args.brief else None
        created = store.create(plan)
        result = {"revision": created["revision"], "name": created["name"],
                  "instances": [i["id"] for i in created["instances"]],
                  "flows": [f["id"] for f in created["flows"]],
                  "tasks": [t["id"] for t in created["tasks"]]}
        if report is not None:
            result["check"] = report
        return result
    if args.operation_name == "demo":
        created = store.create(demo_plan(), force=args.force)
        return {"revision": created["revision"], "name": created["name"],
                "tasks": [t["id"] for t in created["tasks"]]}
    if args.operation_name == "check":
        return check_structure(store.read())
    if args.operation_name == "monitor":
        store.read()  # 先确认项目里有合法计划；没有计划就不该凭空建 .trainops 目录
        return run_monitor(store, args)
    if args.operation_name == "wallet":
        store.read()  # 先确认项目里有合法计划，免得白白发一次网络请求
        if args.action == "fetch":
            return wallet_fetch(store, args)
        if not (hasattr(args, "assets") or hasattr(args, "voucher")):
            raise PlanError("wallet set 至少需要 --assets 或 --voucher（none 表示清空）")
        changes = {key: getattr(args, key) for key in ("assets", "voucher") if hasattr(args, key)}
        return store.change(lambda plan: set_wallet(plan, changes))
    if args.operation_name == "show":
        plan = store.read()
        if args.brief:
            print(brief_plan(plan))
            return None
        view = snapshot(plan)
        if args.json:
            return view
        print(f"{view['name']} · rev {view['revision']} · {view['updated_at']}")
        if view["instances"]:
            print("实例: " + " | ".join(instance_line(i) for i in view["instances"]))
        if view["budget"]:
            print("预算: " + " | ".join(budget_parts(view)))
        print("结算: " + view["settlement"]["text"]
              + (f" | {view['settlement']['error']}" if view["settlement"]["error"] else ""))
        for flow in view["flows"]:
            print(f"\n{flow['id']} {flow['name']}")
            for task in view["tasks"]:
                if task["flow"] == flow["id"]:
                    eta = task["estimate"]
                    prog = f" {task['progress']:g}%" if task["progress"] is not None else ""
                    # 与 Web 面板一致的关键提醒，文本模式下也要一眼可见
                    marks = (" 可开工" if eta["ready"] else "") + \
                        (" 已超期" if eta["overdue"] else "") + \
                        (" 未巡检" if eta["stale"] else "")
                    deps = f" | 依赖:{','.join(task['deps'])}" if task["deps"] else ""
                    mins = f" | {task['duration_minutes']:g}m" if task["duration_minutes"] is not None else " | ?m"
                    note = f" | {task['note']}" if task["note"] else ""
                    print(f"  {task['id']} [{task['status']}{prog}{marks}] {task['name']} | "
                          f"{task['instance'] or '未分配'}{mins}{deps} | "
                          f"完成 {eta['finish'] or '待估'} {eta['reason']}{note}".rstrip())
        return None
    if args.operation_name == "serve":
        store.read()
        if not 0 <= args.port <= 65535:
            raise PlanError("端口必须在 0–65535 之间")
        if args.interval is not None:
            background_monitor(store, args.interval)
        with server(store, args.port) as httpd:
            print(f"TrainOps http://127.0.0.1:{httpd.server_port} · 本机面板（仅时价可登记），不是后台训练调度器"
                  + (f" · 每 {args.interval} 分钟自动结算" if args.interval is not None else ""), flush=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
        return None

    def operation(plan):
        if args.operation_name == "batch":
            if not (args.instance or args.flow or args.task or args.updates):
                raise PlanError("batch 至少需要提供 --instance、--flow、--task 或 --set 之一")
            return apply_batch_specs(plan, args.instance, args.flow, args.task, args.updates)
        if args.operation_name == "instance":
            if args.action == "add":
                return add_instance(plan, args.name, args.autodl_id, args.ssh_alias)
            item = find(plan, "instances", args.id)
            busy = any(t["instance"] == args.id and t["status"] == "running" for t in plan["tasks"])
            if busy and ((args.autodl_id is not None and args.autodl_id != item["autodl_id"])
                         or (args.ssh_alias is not None and args.ssh_alias != item["ssh_alias"])):
                raise PlanError("实例仍有运行任务，不能更换 ID/SSH 映射")
            if busy and args.status in ("off", "unreachable"):
                raise PlanError(f"{args.id} 上仍有运行中的任务，请先核实远端并更新任务状态，再标为关机或不可达")
            for key in ("name", "autodl_id", "ssh_alias", "status", "note"):
                if getattr(args, key) is not None:
                    item[key] = getattr(args, key)
            if hasattr(args, "mode"):
                # 开机模式不能走上面那个循环：null 也是有意义的值（"还没核实过"）。
                item["mode"] = args.mode
                # 老计划里没有无卡费率这个键，而读的时候按平台默认值算；这里把它落实成字段，
                # 免得面板显示"费率未登记"、服务端却按 0.1 计算，两边各说各话。
                item.setdefault("no_gpu_price", NO_GPU_PRICE)
            if hasattr(args, "disk"):
                item["disk"].update(args.disk)  # 逐盘合并：没提到的盘保持原值，不会被静默抹掉
            if args.observed or args.status is not None or hasattr(args, "disk") or hasattr(args, "mode"):
                item["observed_at"] = stamp()
            return item
        if args.operation_name == "flow":
            if args.action == "add":
                return add_flow(plan, args.name)
            item = find(plan, "flows", args.id)
            item["name"] = args.name
            return item
        if getattr(args, "action", "set") == "add":
            return add_task(plan, args.flow, args.name, args.instance, args.duration_minutes, args.deps, args.not_before)
        changes = {key: value for key, value in vars(args).items()
                   if key in ("name", "status", "progress", "note") and value is not None}
        for key in ("instance", "duration_minutes", "deps", "not_before"):
            if hasattr(args, key):
                changes[key] = getattr(args, key)
        if args.checked or args.progress is not None or args.status in ("running", "done"):
            changes["checked_at"] = stamp()
        if not changes:
            raise PlanError("未提供要更新的字段")
        target = find(plan, "tasks", args.id)
        if changes.get("status") == "done" and target["status"] in ("pending", "blocked"):
            set_task(plan, args.id, {"status": "running", "checked_at": changes.get("checked_at")})
        return set_task(plan, args.id, changes)

    report = None

    def checked_operation(plan):
        nonlocal report
        item = operation(plan)
        if getattr(args, "brief", False) and structural_edit(args):
            report = check_structure(plan)
        return item

    result = store.change(checked_operation)
    if report is not None:
        result["check"] = report
    return result


def main(argv=None):
    raw = sys.argv[1:] if argv is None else argv
    cli = parser()
    args = cli.parse_args(raw)
    if args.operation_name == "monitor" and args.brief and not args.once:
        cli.error("monitor --brief 仅用于 --once；循环模式已有单行结算日志")
    try:
        result = dispatch(args, Store(args.project))
    except (PlanError, OSError, OverflowError) as exc:
        print(f"TrainOps: {exc}", file=sys.stderr)
        return 1
    if result is not None:
        if getattr(args, "brief", False):
            print(brief_receipt(args, result))
        elif args.operation_name == "show" and args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        else:
            print(json.dumps(compact(result), ensure_ascii=False, allow_nan=False))
    # 默认沿用退出码 0（agent 读 JSON 即可），--strict 让脚本/CI 能直接判断检查结果。
    if args.operation_name == "check" and args.strict and not result["ok"]:
        return 1
    if args.operation_name == "monitor" and args.strict and result and result["monitor"]["last_error"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
