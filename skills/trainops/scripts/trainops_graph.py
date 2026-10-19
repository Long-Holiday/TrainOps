"""排程、流程汇总、余额与结算这些派生计算。

所有函数都只读计划，不写文件；面板、CLI 与结算线程共用同一份口径。
"""

from datetime import timedelta
import hashlib
import json
import math

from trainops_base import *
from trainops_model import instance_rate, validate


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
            # 观测多久之前是算出来的，不是展示方算的：面板与 CLI 共用 age_text，只有一份口径。
            result["observed_age"] = age_text(wallet["observed_at"], now)
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


def estimate_view(plan, now=None, estimates=None):
    """结算快照 = 实时预算里"算得出的那几个结论"；缺数据就缺键，不拿 0 顶替。"""
    view = budget(plan, now, estimates)
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


def settlement(plan, now=None, estimates=None, live_values=None):
    """show 与面板共用的结算视图：文案与"是否醒目"都在服务端定死，展示方不做判断。

    有新鲜结算（≤ 2 个周期）就用结算结论并注明结算时间；过期或从未结算则回退实时预算
    并说明原因——绝不同时给出两个互相矛盾的数字。调用方已经算过预算时把
    estimates / live_values 传进来，避免同一份计划在一轮请求里重复排程与结算。
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
        view["text"] = "尚未自动结算 · 开面板（serve --interval）并在面板上登记 Token 后自动结算"
    if view["live"]:
        view["values"] = ({key: live_values[key] for key in ESTIMATE_KEYS if key in live_values}
                          if live_values is not None else estimate_view(plan, now, estimates))
    if view["error"]:
        view["text"] += " · 上次结算失败"
        view["over"] = True
    if not view["token_ready"]:
        view["text"] += " · 未配置 Token"
    if view["changed"]:
        view["text"] += " · 结算后计划有变更"
    return view


def snapshot(plan, now=None):
    """面板与 show --json 看到的完整视图：账本 + 排程 + 流程汇总 + 预算 + 结算。

    凡是"算出来的"都在这里算完：面板不做业务计算，只把字段摆到页面上。
    任务节点进度放在 view.progress，原 progress 字段保持计划里的登记值不变。
    """
    now = now or utcnow()
    estimates, sequence = _forecast(plan, now)
    views = flow_views(plan, now, estimates)
    spend = budget(plan, now, estimates)
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
    return {**plan, "generated_at": stamp(now), "budget": spend,
            "settlement": settlement(plan, now, estimates, spend),
            "stats": {"flows": len(plan["flows"]), "tasks": len(plan["tasks"]), "counts": counts},
            "instances": [{**item, "view": {"running": sorted(running.get(item["id"], []), key=natural_id),
                                            "queued": queued.get(item["id"], 0)}}
                          for item in plan["instances"]],
            "flows": [{**flow, "view": views[flow["id"]]}
                      for flow in plan["flows"]],
            "queues": execution_queues(plan, sequence),
            "tasks": [{**task, "estimate": estimates[task["id"]],
                       "view": {"progress": node_progress(task)}}
                      for task in plan["tasks"]]}


def check_structure(plan, now=None, *, amounts=True):
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
    # 以下三条都只在"数据已记录且有问题"时才出现：没有观测就不该凭空冒出提醒。
    for item in plan["instances"]:
        for _, field, label in DISK_MOUNTS:
            used, total = item["disk"][f"{field}_used_gb"], item["disk"][f"{field}_total_gb"]
            if used is None or not total or used / total < FULL_RATIO:
                continue
            warnings.append(f"{item['id']}: {label}已用 {used:g}/{total:g}G"
                            f"（{used / total * 100:.0f}%），请先清理或扩容再开新任务")
    estimates = forecast(plan, now)
    view = budget(plan, now, estimates)
    if view.get("assets") is not None:
        missing = [item["id"] for item in plan["instances"]
                   if item["status"] in AWAKE and instance_rate(item) is None]
        if missing:
            warnings.append("未登记时价（" + ", ".join(missing) +
                            "），无法把余额换算成可用时长；请打开面板（serve）在实例行上登记")
        if view.get("covers_plan") is False:
            detail = "实时预算提醒：余额不足以覆盖剩余计划"
            if amounts:
                detail = f"余额 ¥{view['assets']:.2f} 不足以覆盖剩余计划（预估 ¥{view['plan_cost']:.2f}）"
            warnings.append(detail + "，请充值或缩减计划")
    # 自动结算相关提醒：只报"正在计费却没人算账"和"算了但失败了"这两种要处置的情况。
    if plan["monitor"].get("last_error"):
        warnings.append("自动结算失败：" + plan["monitor"]["last_error"]
                        + "；面板进程会按周期重试，Token 填错或过期就在面板上重新登记")
    if any(item["status"] in AWAKE for item in plan["instances"]) and \
            settlement(plan, now, estimates, view)["live"]:
        warnings.append("有实例开机计费，但自动结算未在运行；开着面板（serve --interval）"
                        "才会定时重算余额与剩余计划的 GPU 小时")
    if pending_unknown_duration:
        warnings.append(f"{pending_unknown_duration} 个 pending 任务未设置预计耗时（duration_minutes），"
                        "完成时间待估；可在首个 checkpoint 后补估")
    return {"ok": not issues, "flows": len(plan["flows"]), "tasks": len(plan["tasks"]),
            "issues": issues, "warnings": warnings}


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



__all__ = [
    "forecast", "rounded", "node_progress", "flow_views", "budget", "estimate_view",
    "plan_fingerprint", "settlement_summary", "settlement", "snapshot", "check_structure",
    "age_text",
]
