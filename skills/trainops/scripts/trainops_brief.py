"""CLI 的 brief / 文本展示格式。"""

from trainops_base import *
from trainops_graph import age_text, check_structure, settlement_summary, snapshot
from trainops_model import instance_mode, instance_rate


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
        lines.append("当前展示为变更前的结算；等面板下一轮结算后再决策")
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
    lines = [f"已保存 · rev {result['revision']}"]
    if operation == "wallet":
        wallet = result.get("wallet", result.get("item"))
        for key, label in (("assets", "余额"), ("voucher", "代金券（不计入可用时长）")):
            lines.append(f"{label}: ¥{wallet[key]:.2f}" if wallet.get(key) is not None else f"{label}: 未登记")
        if wallet.get("observed_at"):
            lines.append("余额观测于 " + age_text(wallet["observed_at"]))
    elif operation in ("init", "batch"):
        item = result if operation == "init" else result["item"]
        for key, label in (("instances", "实例"), ("flows", "流程"), ("tasks", "任务"),
                           ("updated", "更新"), ("deps", "依赖变更"), ("inferred", "自动串联")):
            entries = item.get(key, [])
            if key == "deps":
                entries = [f"{e['id']}: {','.join(e['from']) or '空'} → {','.join(e['to']) or '空'}"
                           for e in entries]
            elif key == "inferred":
                entries = [f"{e['id']}←{','.join(e['deps'])}" for e in entries]
            lines.extend(brief_group(label, entries))
        if item.get("inferred"):
            lines.append("自动串联只表示同流程同实例的默认顺序；独立任务用 - 显式留空")
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




def compact(data):
    if isinstance(data, dict):
        return {k: compact(v) for k, v in data.items() if v not in ("", None, [])}
    if isinstance(data, list):
        return [compact(v) for v in data]
    return data



__all__ = [
    "disk_text", "instance_line", "budget_parts", "brief_text", "brief_group",
    "brief_finance", "brief_plan", "brief_receipt", "compact",
]
