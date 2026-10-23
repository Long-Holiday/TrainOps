"""TrainOps 共享常量与小型校验工具。

计划、面板与实验日志都从这里取同一套常量和时钟，避免拆分模块后各写一份。
"""

import math
import re
from datetime import datetime, timezone

# 版本号只用来拒绝看不懂的文件：开发阶段不提供迁移，旧版一律明确报错而不是猜着读。
SCHEMA_VERSION = 3
# 一个实验项目只有一份配置文件：.agent/experiment.yaml。它把项目配置、计划账本和
# Run 历史装进同一个 JSON 兼容 YAML 文档，三个段各自保留自己的 schema 版本。
FILE_VERSION = 1
CONFIG_VERSION = 1
EXPERIMENT_STATE_VERSION = 1
STATUSES = ("pending", "running", "blocked", "done", "failed", "cancelled")
INSTANCE_STATUSES = ("unknown", "off", "starting", "on", "unreachable")
MODES = ("gpu", "no-gpu")
BRIEF_LIMIT = 3
BRIEF_TEXT_LIMIT = 120
# 磁盘容量一律以 GB 存进字段名，避免"这个数字是字节还是 G"的歧义（同 duration_minutes 的做法）。
DISK_FIELDS = ("system_used_gb", "system_total_gb", "data_used_gb", "data_total_gb",
               "fs_used_gb", "fs_total_gb")
DISK_MOUNTS = (("sys", "system", "系统盘"), ("data", "data", "数据盘"), ("fs", "fs", "文件存储"))
# --disk 里用短盘名（sys 而不是 system），映射到字段前缀与中文名。
DISK_PREFIXES = {prefix: (field, label) for prefix, field, label in DISK_MOUNTS}
DISK_SHORT = {"系统盘": "系统", "数据盘": "数据", "文件存储": "存储"}
FULL_RATIO = 0.9
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


__all__ = [
    "SCHEMA_VERSION", "FILE_VERSION", "CONFIG_VERSION", "EXPERIMENT_STATE_VERSION",
    "STATUSES", "INSTANCE_STATUSES", "MODES", "BRIEF_LIMIT", "BRIEF_TEXT_LIMIT",
    "DISK_FIELDS", "DISK_MOUNTS", "DISK_PREFIXES", "DISK_SHORT", "FULL_RATIO",
    "TERMINAL", "TRANSITIONS", "PlanError", "utcnow", "stamp", "date", "number",
    "text", "integer", "natural_id", "find",
]
