"""CLI 简写与参数值解析。"""

import argparse
import re

from trainops_base import *


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


def iteration_progress(current, total):
    if current < 0 or total <= 0 or current > total:
        raise PlanError("迭代数须满足 0 <= iter <= max-iter，且 max-iter > 0")
    return current / total * 100


def progress_arg(value):
    try:
        if "/" in value:
            parts = value.split("/")
            if len(parts) != 2 or not all(re.fullmatch(r"[0-9]+", part.strip()) for part in parts):
                raise PlanError("进度应为 0–100 的百分比或 当前迭代数/总迭代数（如 800/7500）")
            return iteration_progress(*(int(part) for part in parts))
        result = float(value)
        number(result, "进度", high=100)
        return result
    except (ValueError, PlanError, OverflowError) as exc:
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


def parse_instance_spec(spec):
    parts = [part.strip() for part in spec.split(":", 2)]
    if len(parts) != 3 or not all(parts):
        raise PlanError(f"实例简写格式应为 '名称:autodl_id:ssh_alias'，实际为 {spec!r}")
    return parts[0], parts[1], parts[2]


def parse_task_spec(spec):
    """任务简写；省略依赖返回 None，显式空依赖返回 []，中间字段用 - 占位。"""
    fields = spec.split(":", 4)
    parts = [part.strip() for part in (fields + [""] * 5)[:5]]
    deps = None if len(fields) < 5 else ([] if parts[4] == "none" else dependencies(parts[4]))
    if not parts[0] or not parts[1]:
        raise PlanError(f"任务简写格式应为 '流程ID:名称[:实例[:分钟数[:依赖]]]'（跳过中间字段用 -），实际为 {spec!r}")
    return parts[0], parts[1], nullable(parts[2]) if parts[2] else None, \
        duration(parts[3]) if parts[3] else None, deps


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


def parse_dep_spec(spec):
    """'F03-T04=F02-T06,F03-T03' → (任务ID, 依赖列表)；等号右侧为空、none 或 - 表示清空。"""
    task_id, sep, raw = spec.partition("=")
    task_id = task_id.strip()
    if not sep or not task_id:
        raise PlanError(f"依赖更新应写成 '任务ID=依赖列表'（右侧为空表示清空），实际为 {spec!r}")
    raw = raw.strip()
    return task_id, [] if raw in ("", "none", "-") else dependencies(raw)


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


def structural_edit(args):
    if args.operation_name == "batch":
        return bool(args.instance or args.flow or args.task or getattr(args, "dep_updates", []))
    if getattr(args, "action", None) == "add":
        return True
    return args.operation_name in ("set", "task") and any(
        hasattr(args, key) for key in ("instance", "duration_minutes", "deps", "not_before"))


__all__ = [
    "nullable", "duration", "dependencies", "gigabytes", "disk_changes",
    "disk_arg", "parse_instance_spec",
    "parse_task_spec", "parse_set_spec", "parse_dep_spec", "structural_edit",
]
