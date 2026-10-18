"""结构化计划输入：严格校验、请求内别名与两阶段任务引用；不直接读写账本。"""

import json
from pathlib import Path
import re
import sys

from trainops_base import PlanError, STATUSES, date, find, number, stamp, text, utcnow
from trainops_model import add_flow, add_instance, add_task


CREATES = {
    "instances": ({"key", "name", "autodl_id", "ssh_alias"}, {"name", "autodl_id", "ssh_alias"}),
    "flows": ({"key", "name"}, {"name"}),
    "tasks": ({"key", "flow", "name", "instance", "minutes", "deps", "not_before"}, {"flow", "name"}),
}
UPDATE_FIELDS = {"id", "name", "status", "progress", "instance", "minutes", "deps", "not_before", "note", "checked"}
STRUCTURE_FIELDS = {"instance", "duration_minutes", "deps", "not_before"}
SYSTEM_ID = re.compile(r"(?:I\d+|F\d+(?:-T\d+)?)\Z")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanError(f"JSON 对象字段重复: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise PlanError(f"JSON 不接受 {value}，待估值用 null")


def object_fields(value, allowed, required, path):
    if not isinstance(value, dict):
        raise PlanError(f"{path} 必须是对象")
    unknown = value.keys() - allowed
    missing = required - value.keys()
    if unknown:
        raise PlanError(f"{path} 未知字段: {', '.join(sorted(unknown))}")
    if missing:
        raise PlanError(f"{path} 缺少字段: {', '.join(sorted(missing))}")


def input_text(value, label, required=False):
    text(value, label, required)
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise PlanError(f"{label} 必须是有效的 Unicode 字符串") from exc


def validate_fields(item, path):
    for field, value in item.items():
        label = f"{path}.{field}"
        if field in ("key", "id", "flow", "name", "autodl_id", "ssh_alias", "status"):
            input_text(value, label, True)
        elif field == "note":
            input_text(value, label)
        elif field == "instance":
            if value is not None:
                input_text(value, label, True)
        elif field in ("minutes", "progress"):
            if value is not None:
                number(value, label, high=100 if field == "progress" else 1_000_000)
        elif field == "deps":
            if not isinstance(value, list):
                raise PlanError(f"{label} 必须是引用列表，清空用 []")
            for index, ref in enumerate(value):
                input_text(ref, f"{label}[{index}]", True)
        elif field == "not_before":
            if value is not None:
                try:
                    date(value)
                except PlanError as exc:
                    raise PlanError(f"{label}: {exc}") from exc
        elif field == "checked" and type(value) is not bool:
            raise PlanError(f"{label} 必须是布尔值")
    if "status" in item and item["status"] not in STATUSES:
        raise PlanError(f"{path}.status 必须是 {' / '.join(STATUSES)}")
    if "ssh_alias" in item and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", item["ssh_alias"]):
        raise PlanError(f"{path}.ssh_alias 只能包含字母、数字、下划线、点和连字符，且不能以 - 开头")


def validate_request(payload, operation):
    allowed = set(CREATES)
    if operation in ("init", "apply"):
        allowed.add("name")
    if operation != "init":
        allowed.add("updates")
    object_fields(payload, allowed, {"name"} if operation == "init" else set(), "JSON")
    if operation == "init":
        input_text(payload["name"], "name", True)
    elif operation == "apply" and "name" in payload:
        input_text(payload["name"], "name", True)
    keys = set()
    for collection in (*CREATES, "updates"):
        if collection not in payload:
            continue
        entries = payload[collection]
        if not isinstance(entries, list):
            raise PlanError(f"{collection} 必须是列表")
        allowed, required = CREATES.get(collection, (UPDATE_FIELDS, {"id"}))
        for index, item in enumerate(entries):
            path = f"{collection}[{index}]"
            object_fields(item, allowed, required, path)
            validate_fields(item, path)
            if collection == "updates" and not (item.keys() - {"id", "checked"} or item.get("checked")):
                raise PlanError(f"{path} 未提供要更新的字段")
            if "key" in item:
                key = item["key"]
                if key in keys or key == "local" or SYSTEM_ID.fullmatch(key):
                    raise PlanError(f"{path}.key 重复或使用了保留名称: {key}")
                keys.add(key)
    if operation == "batch" and not any(payload.values()):
        raise PlanError("JSON batch 至少需要一项新增或更新")
    if operation == "apply" and not any(payload.values()):
        raise PlanError("JSON apply 至少需要 name、instances、flows、tasks 或 updates 之一")
    return payload


def read_request(source, operation):
    try:
        if source == "-":
            raw = sys.stdin.buffer.read().decode("utf-8") if hasattr(sys.stdin, "buffer") else sys.stdin.read()
        else:
            raw = Path(source).expanduser().read_text(encoding="utf-8")
        payload = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PlanError(f"无效 JSON 输入: {exc}") from exc
    return validate_request(payload, operation)


def structural_request(payload):
    return any(payload.get(key) for key in CREATES) or any(
        set(item) & {"instance", "minutes", "deps", "not_before"} for item in payload.get("updates", []))


def apply_request(plan, payload, set_task, now=None):
    """分配全部编号后解析引用；结构先于状态，整批完成后由 Store 校验并提交。"""
    now = now or utcnow()
    aliases = {kind: {} for kind in CREATES}
    added = {key: [] for key in (*CREATES, "updated", "deps", "inferred")}
    added["aliases"] = aliases

    def register(kind, spec, item):
        added[kind].append(item["id"])
        if "key" in spec:
            aliases[kind][spec["key"]] = item["id"]

    def resolve(kind, ref, path):
        if kind == "instances" and ref in (None, "local"):
            return ref
        item_id = aliases[kind].get(ref, ref)
        try:
            return find(plan, kind, item_id)["id"]
        except PlanError as exc:
            raise PlanError(f"{path} 找不到 {kind} 引用: {ref}") from exc

    for spec in payload.get("instances", []):
        register("instances", spec, add_instance(plan, spec["name"], spec["autodl_id"], spec["ssh_alias"]))
    for spec in payload.get("flows", []):
        register("flows", spec, add_flow(plan, spec["name"]))
    for index, spec in enumerate(payload.get("tasks", [])):
        path = f"tasks[{index}]"
        item = add_task(plan, resolve("flows", spec["flow"], path + ".flow"), spec["name"],
                        resolve("instances", spec.get("instance"), path + ".instance"),
                        spec.get("minutes"), [], spec.get("not_before"))
        register("tasks", spec, item)
    for index, (spec, task_id) in enumerate(zip(payload.get("tasks", []), added["tasks"])):
        deps = [resolve("tasks", ref, f"tasks[{index}].deps[{i}]") for i, ref in enumerate(spec.get("deps", []))]
        set_task(plan, task_id, {"deps": deps}, now=now)

    updates, seen = [], set()
    for index, spec in enumerate(payload.get("updates", [])):
        path = f"updates[{index}]"
        task_id = resolve("tasks", spec["id"], path + ".id")
        if task_id in seen:
            raise PlanError(f"{path}.id 重复更新同一任务: {task_id}")
        seen.add(task_id)
        changes = {("duration_minutes" if key == "minutes" else key): value
                   for key, value in spec.items() if key not in ("id", "checked")}
        if "instance" in changes:
            changes["instance"] = resolve("instances", changes["instance"], path + ".instance")
        if "deps" in changes:
            changes["deps"] = [resolve("tasks", ref, f"{path}.deps[{i}]") for i, ref in enumerate(changes["deps"])]
            entry = {"id": task_id, "from": list(find(plan, "tasks", task_id)["deps"]), "to": changes["deps"]}
            if not entry["to"]:
                entry["cleared"] = True
            added["deps"].append(entry)
        updates.append((task_id, changes, spec.get("checked", False)))

    for task_id, changes, _ in updates:
        structure = {key: value for key, value in changes.items() if key in STRUCTURE_FIELDS}
        set_task(plan, task_id, structure, now=now)
    for task_id, changes, checked in updates:
        state = {key: value for key, value in changes.items() if key not in STRUCTURE_FIELDS}
        if checked or "progress" in state or state.get("status") in ("running", "done"):
            state["checked_at"] = stamp(now)
        if state.get("status") == "done" and find(plan, "tasks", task_id)["status"] in ("pending", "blocked"):
            set_task(plan, task_id, {"status": "running", "checked_at": state.get("checked_at")}, now=now)
        set_task(plan, task_id, state, now=now)
        added["updated"].append(task_id)
    return added
