"""TrainOps 共享常量与小型校验工具。

计划、结算、面板与实验日志都从这里取同一套常量和时钟，避免拆分模块后各写一份。
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
# 磁盘容量一律以 GB 存进字段名，避免"这个数字是字节还是 G"的歧义（同 duration_minutes 的做法）。
DISK_FIELDS = ("system_used_gb", "system_total_gb", "data_used_gb", "data_total_gb",
               "fs_used_gb", "fs_total_gb")
DISK_MOUNTS = (("sys", "system", "系统盘"), ("data", "data", "数据盘"), ("fs", "fs", "文件存储"))
# --disk 里用短盘名（sys 而不是 system），映射到字段前缀与中文名。
DISK_PREFIXES = {prefix: (field, label) for prefix, field, label in DISK_MOUNTS}
DISK_SHORT = {"系统盘": "系统", "数据盘": "数据", "文件存储": "存储"}
FULL_RATIO = 0.9
API_BALANCE_PATH = "/api/v1/dev/wallet/balance"
# 面板可写的两个端点：登记时价（写计划）与登记 Token（只存进程内存）。其余路径全是只读，写动词一律 405。
PRICE_PATH = "/api/instance-price"
TOKEN_PATH = "/api/token"
BODY_LIMIT = 4096
# Token 是控制台发的开发者凭据，长度远超实际需要；上限只为挡住往内存里塞垃圾。
TOKEN_LIMIT = 512
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
    "STATUSES", "INSTANCE_STATUSES", "AWAKE", "DEFAULT_INTERVAL_MINUTES", "INTERVAL_LIMITS",
    "PRICE_LIMITS", "NO_GPU_PRICE", "MODES", "ESTIMATE_KEYS", "ERROR_LIMIT", "BRIEF_LIMIT",
    "BRIEF_TEXT_LIMIT", "DISK_FIELDS", "DISK_MOUNTS", "DISK_PREFIXES", "DISK_SHORT",
    "FULL_RATIO", "API_BALANCE_PATH", "PRICE_PATH", "TOKEN_PATH", "BODY_LIMIT",
    "TOKEN_LIMIT", "TERMINAL", "TRANSITIONS", "PlanError", "utcnow", "stamp", "date",
    "number", "text", "integer", "natural_id", "find",
]
