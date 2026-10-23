"""排程与流程汇总的派生计算。

所有函数都只读计划，不写文件；面板与 CLI 共用同一份口径。
"""

from datetime import timedelta
import math

from trainops_base import *
from trainops_model import validate


def _forecast(plan, now=None):
    """Return estimates and their shared dependency-ready traversal order."""
    now = now or utcnow()
    tasks = {t["id"]: t for t in plan["tasks"]}
    order = {key: natural_id(key) for key in tasks}
    results, lanes, sequence = {}, {}, []
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
        if task["status"] in ("running", "pending"):
            sequence.append(task["id"])
        return end

    for task in sorted(tasks.values(), key=lambda t: order[t["id"]]):
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
        for key in sorted(pending, key=order.__getitem__):
            task = tasks[key]
            if not all(dep in results for dep in task["deps"]):
                continue
            unknown = [dep for dep in task["deps"] if results[dep]["finish"] is None]
            if unknown:
                candidates.append((now, order[key], key, "依赖待完成/待估: " + ", ".join(unknown)))
                continue
            earliest = max([now] + [date(results[dep]["finish"]) for dep in task["deps"]])
            if task["not_before"]:
                earliest = max(earliest, date(task["not_before"]))
            resource = task["instance"]
            if resource is None:
                candidates.append((earliest, order[key], key, "未分配实例（本地任务设 local）"))
            elif resource in lanes and lanes[resource] is None:
                candidates.append((earliest, order[key], key, "实例前序任务完成时间待估"))
            else:
                earliest = max(earliest, lanes.get(resource) or now)
                candidates.append((earliest, order[key], key, ""))
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
    return results, sequence


def forecast(plan, now=None):
    """Deterministic dependency-ready scheduling with one lane per GPU instance."""
    return _forecast(plan, now)[0]


def execution_queues(plan, sequence):
    """只读候选队列：共享 forecast 的遍历顺序，不把资源排队写成 deps。"""
    tasks = {task["id"]: task for task in plan["tasks"]}
    lanes = {item["id"]: [] for item in plan["instances"]}
    local, unassigned, inactive = [], [], []
    for key in sequence:
        resource = tasks[key]["instance"]
        if resource == "local":
            local.append(key)
        elif resource is None:
            unassigned.append(key)
        else:
            lanes[resource].append(key)
    for task in sorted(plan["tasks"], key=lambda task: natural_id(task["id"])):
        if task["status"] not in ("running", "pending"):
            inactive.append(task["id"])
    return {"lanes": [{"instance": key, "tasks": value} for key, value in lanes.items()],
            "other": [{"label": label, "tasks": ids} for label, ids in (
                ("本地任务（不独占 GPU，不表示串行）", local),
                ("未分配实例（顺序未确定）", unassigned),
                ("已结束 / 需处理（不在执行队列）", inactive)) if ids]}


def rounded(value):
    """四舍五入取整：面板要的是整数百分比，这里统一口径，别让各处各写一套。"""
    return int(math.floor(value + 0.5))


def node_progress(task):
    """节点进度：done 一律满格，其余取登记过的真实进度；没进度就是 0，不猜。"""
    if task["status"] == "done":
        return 100
    if task["progress"] is None:
        return 0
    return max(0, min(100, rounded(task["progress"])))


def flow_views(plan, now=None, estimates=None):
    """各流程的完成结论：计数、总进度、预计完成时间与是否都有估计。

    总进度是已完成 ÷ 未取消任务，不累加任务时间；取消不计入分母，全部取消时不下结论。
    known 只检查尚未结束的任务，已结束的任务没有"预计完成时间"。
    """
    now = now or utcnow()
    estimates = forecast(plan, now) if estimates is None else estimates
    grouped = {flow["id"]: [] for flow in plan["flows"]}
    for task in plan["tasks"]:
        grouped[task["flow"]].append(task)
    views = {}
    for flow in plan["flows"]:
        tasks = grouped[flow["id"]]
        countable = [task for task in tasks if task["status"] != "cancelled"]
        done = [task for task in tasks if task["status"] == "done"]
        open_tasks = [task for task in tasks if task["status"] not in TERMINAL]
        finishes = [date(estimates[task["id"]]["finish"]) for task in tasks
                    if estimates[task["id"]]["finish"]]
        views[flow["id"]] = {
            "total": len(tasks), "countable": len(countable), "done": len(done),
            "cancelled": len(tasks) - len(countable), "open": len(open_tasks),
            "progress": rounded(100 * len(done) / len(countable)) if countable else None,
            "known": all(estimates[task["id"]]["finish"] for task in open_tasks),
            "finish": stamp(max(finishes)) if finishes else None,
        }
    return views


def snapshot(plan, now=None):
    """面板与 show --json 看到的完整视图：账本 + 排程 + 流程汇总。

    凡是"算出来的"都在这里算完：面板不做业务计算，只把字段摆到页面上。
    任务节点进度放在 view.progress，原 progress 字段保持计划里的登记值不变。
    """
    now = now or utcnow()
    estimates, sequence = _forecast(plan, now)
    views = flow_views(plan, now, estimates)
    counts = {status: 0 for status in STATUSES}
    running, queued = {}, {}
    for task in plan["tasks"]:
        counts[task["status"]] += 1
        resource = task["instance"]
        if resource in (None, "local"):
            continue
        if task["status"] == "running":
            running.setdefault(resource, []).append(task["id"])
        elif task["status"] in ("pending", "blocked"):
            queued[resource] = queued.get(resource, 0) + 1
    fields = ("schema_version", "name", "revision", "updated_at", "next_instance", "next_flow")
    instance_fields = ("id", "name", "autodl_id", "ssh_alias", "status", "observed_at", "note", "mode", "disk")
    return {**{key: plan[key] for key in fields}, "generated_at": stamp(now),
            "stats": {"flows": len(plan["flows"]), "tasks": len(plan["tasks"]), "counts": counts},
            "instances": [{**{key: item[key] for key in instance_fields if key in item},
                           "view": {"running": sorted(running.get(item["id"], []), key=natural_id),
                                    "queued": queued.get(item["id"], 0)}}
                          for item in plan["instances"]],
            "flows": [{**flow, "view": views[flow["id"]]}
                      for flow in plan["flows"]],
            "queues": execution_queues(plan, sequence),
            "tasks": [{**task, "estimate": estimates[task["id"]],
                       "view": {"progress": node_progress(task)}}
                      for task in plan["tasks"]]}


def check_structure(plan, now=None):
    """Structural check for dependency topology and resource/duration readiness.

    issues 是需要修正的结构缺陷（ok 为假），warnings 只是观察到的现象，不影响 ok。
    """
    now = now or utcnow()
    validate(plan)
    issues, warnings = [], []
    pending_unknown_duration = 0
    tasks_by_id = {t["id"]: t for t in plan["tasks"]}
    grouped = {flow["id"]: [] for flow in plan["flows"]}
    for task in plan["tasks"]:
        grouped[task["flow"]].append(task)
    for flow in plan["flows"]:
        flow_tasks = grouped[flow["id"]]
        if not flow_tasks:
            issues.append(f"{flow['id']}({flow['name']}): 流程为空，无任务节点")
            continue
        for task in flow_tasks:
            if task["status"] not in TERMINAL:
                if task["instance"] is None:
                    issues.append(f"{task['id']}: 未分配实例（I01.. 或 local）")
                if task["duration_minutes"] is None:
                    if task["status"] == "pending":
                        pending_unknown_duration += 1
                    else:
                        issues.append(f"{task['id']}: 未设置预计耗时（duration_minutes），完成时间待估")
            if task["started_at"] and task["deps"]:
                dep_finishes = [date(tasks_by_id[d]["finished_at"]) for d in task["deps"] if tasks_by_id[d]["finished_at"]]
                if dep_finishes:
                    gap_min = (date(task["started_at"]) - max(dep_finishes)).total_seconds() / 60
                    if gap_min > 5:
                        warnings.append(f"{task['id']}: 实际开始时间晚于前置依赖完成时间 {int(gap_min)} 分钟，存在时间空隙")
    # 没有磁盘观测时不凭空给出容量提醒。
    for item in plan["instances"]:
        for _, field, label in DISK_MOUNTS:
            used, total = item["disk"][f"{field}_used_gb"], item["disk"][f"{field}_total_gb"]
            if used is None or not total or used / total < FULL_RATIO:
                continue
            warnings.append(f"{item['id']}: {label}已用 {used:g}/{total:g}G"
                            f"（{used / total * 100:.0f}%），请先清理或扩容再开新任务")
    if pending_unknown_duration:
        warnings.append(f"{pending_unknown_duration} 个 pending 任务未设置预计耗时（duration_minutes），"
                        "完成时间待估；可在首个 checkpoint 后补估")
    return {"ok": not issues, "flows": len(plan["flows"]), "tasks": len(plan["tasks"]),
            "issues": issues, "warnings": warnings}


__all__ = [
    "forecast", "rounded", "node_progress", "flow_views", "snapshot", "check_structure",
]
