"""计划文档的构造、校验，以及实例/流程/任务这几类可独立测试的纯操作。"""

import copy
import re

from trainops_base import *


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
    }


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
            if item.get("mode") is not None and item["mode"] not in MODES:
                raise PlanError("开机模式只能是 gpu（有卡）或 no-gpu（无卡准备）")
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

def add_instance(plan, name, autodl_id, ssh_alias):
    item = {"id": f"I{plan['next_instance']:02d}", "name": name,
            "autodl_id": autodl_id, "ssh_alias": ssh_alias, "status": "unknown",
            "observed_at": None, "note": "", "mode": None,
            "disk": dict.fromkeys(DISK_FIELDS)}
    plan["next_instance"] += 1
    plan["instances"].append(item)
    return item


def instance_mode(item):
    """开机模式：no-gpu（无卡开机，含持续请求等待 GPU）或 gpu。没有这个键 = 未观测。

    用 .get 保持兼容：老计划没有模式时按未观测读取。
    """
    mode = item.get("mode")
    return mode if mode in MODES else None


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


def default_config(project):
    return {"schema_version": CONFIG_VERSION, "project": project, "lark": {}, "defaults": {}}


def default_state():
    return {"schema_version": EXPERIMENT_STATE_VERSION, "runs": {}}


def new_document(plan, config=None, state=None):
    """一个实验项目的完整配置文档。

    config 为 None 表示还没跑过 experiment init；计划命令与面板都不需要它。
    """
    return {"schema_version": FILE_VERSION,
            "config": copy.deepcopy(config) if config is not None else None,
            "plan": plan,
            "experiments": copy.deepcopy(state) if state is not None else default_state()}


__all__ = [
    "new_plan", "validate", "add_instance",
    "instance_mode", "add_flow", "add_task", "default_config",
    "default_state", "new_document",
]
