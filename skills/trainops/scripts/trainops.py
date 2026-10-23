#!/usr/bin/env python3
"""Local training plan ledger and experiment index.
Experiment indexing uses the separately authenticated lark-cli.
No instance commands, no model API.

实现按职责拆分在同目录的 trainops_*.py：
- trainops_base   常量、时钟、通用校验
- trainops_model  计划文档、校验、实例/流程/任务操作
- trainops_store  文件锁与原子写
- trainops_graph  排程与流程汇总
- trainops_web    本机只读执行队列面板
- trainops_experiments  实验配置、Run 历史与飞书同步
- trainops_json   结构化 JSON 计划输入与别名
- trainops_specs  CLI 简写解析
- trainops_brief  CLI 文本输出
- trainops_cli    argparse 命令树

本文件保留入口、计划状态机的写操作与命令分派；测试仍可只导入这一个模块。
"""

import argparse
import json
import subprocess  # 供测试替换 lark-cli 调用；实际调用在 trainops_experiments
import sys
from datetime import timedelta
from pathlib import Path as _Path

# 以脚本方式运行时 Python 已把脚本目录放进 sys.path；测试用 spec_from_file_location
# 直接加载本文件时不会，所以这里补一次，保证同目录的拆分模块总能被找到。
_SCRIPT_DIR = str(_Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import trainops_base as _base
from trainops_base import *
from trainops_model import *
from trainops_store import Store, sync_directory, validate_document
from trainops_graph import check_structure, flow_views, forecast, node_progress, rounded, snapshot
from trainops_web import Handler, server
from trainops_experiments import (
    EXPERIMENT_FIELDS, RUN_STATUSES, RUN_TEXT, RUN_UPDATE, ExperimentLog, LarkNotStarted,
    experiment_dispatch as _experiment_dispatch, experiment_object, reject_experiment_secrets,
    reject_json_constant,
    validate_extra_metrics, validate_experiment_config, validate_run_patch,
    validate_experiment_state,
)
from trainops_specs import (
    dependencies, disk_arg, disk_changes, duration, gigabytes, iteration_progress, nullable,
    parse_dep_spec, parse_instance_spec, parse_set_spec, parse_task_spec,
    structural_edit,
)
from trainops_brief import (
    brief_group, brief_plan, brief_receipt, brief_text, compact,
    disk_text, instance_line,
)
from trainops_cli import add_task_args, parser
from trainops_json import apply_request, read_request, structural_request


def stamp(value=None):
    """入口时钟：测试替换本模块的 utcnow 时，这里派生的时间戳也要跟着变。"""
    return _base.stamp(value if value is not None else utcnow())


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
    for instance, disk in zip(plan["instances"], disks):
        instance.update(status="on", mode="gpu", observed_at=stamp(now), note="演示占位，非真实实例",
                        disk=disk)
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
    plan["revision"] = 1
    return plan


def experiment_dispatch(args, store):
    """入口包装：让 tests / 调用方替换本模块的 ExperimentLog 时仍然生效。"""
    return _experiment_dispatch(args, store, ExperimentLog)


def apply_batch_specs(plan, instances=(), flows=(), tasks=(), updates=(), dep_updates=()):
    added = {"instances": [], "flows": [], "tasks": [], "updated": [], "deps": [], "inferred": []}
    for spec in instances:
        name, autodl_id, ssh_alias = parse_instance_spec(spec)
        added["instances"].append(add_instance(plan, name, autodl_id, ssh_alias)["id"])
    for flow_name in flows:
        added["flows"].append(add_flow(plan, flow_name)["id"])
    for spec in tasks:
        flow_id, name, inst, mins, deps = parse_task_spec(spec)
        inferred = deps is None
        if inferred:
            prev = [t["id"] for t in plan["tasks"]
                    if t["flow"] == flow_id and inst is not None and t["instance"] == inst]
            deps = prev[-1:]
        item = add_task(plan, flow_id, name, inst, mins, deps)
        added["tasks"].append(item["id"])
        if inferred and item["deps"]:
            added["inferred"].append({"id": item["id"], "deps": list(item["deps"])})
    for spec in updates:
        task_id, changes = parse_set_spec(spec)
        changes["checked_at"] = stamp()
        target = find(plan, "tasks", task_id)
        if changes.get("status") == "done" and target["status"] in ("pending", "blocked"):
            set_task(plan, task_id, {"status": "running", "checked_at": changes["checked_at"]})
        set_task(plan, task_id, changes)
        added["updated"].append(task_id)
    for spec in dep_updates:
        task_id, deps = parse_dep_spec(spec)
        task = find(plan, "tasks", task_id)
        before = list(task["deps"])
        set_task(plan, task_id, {"deps": deps})
        entry = {"id": task_id, "from": before, "to": list(task["deps"])}
        if not entry["to"]:
            entry["cleared"] = True
        added["deps"].append(entry)
    return added


def dispatch(args, store):
    if args.operation_name == "experiment":
        return experiment_dispatch(args, store)
    payload = None
    dry_run = getattr(args, "dry_run", False)
    if args.operation_name in ("init", "batch"):
        if args.from_json is not None:
            if (args.instance or args.flow or args.task or getattr(args, "name", None) is not None
                    or getattr(args, "updates", []) or getattr(args, "dep_updates", [])):
                raise PlanError("--from-json 不与 --name/--instance/--flow/--task/--set/--dep 混用")
            payload = read_request(args.from_json, args.operation_name)
        elif args.operation_name == "init" and args.name is None:
            raise PlanError("init 需要 --name 或 --from-json")
    if args.operation_name == "apply":
        payload = read_request(args.from_json, "apply")
        report = None

        def create_plan():
            # 只有文件不存在时才会走到这里；存在性判断与提交都在 Store.apply 的同一把锁内。
            if "name" not in payload:
                raise PlanError("apply 新建计划需要 JSON 的 name 字段；已有计划时 name 可省略")
            return new_plan(payload["name"])

        def operation(plan, created):
            nonlocal report
            if created:
                if payload.get("updates"):
                    raise PlanError("apply 新建计划时不能包含 updates（没有可更新的任务）")
            else:
                name = payload.get("name")
                if name is not None and name != plan["name"]:
                    raise PlanError(f"apply 不会改计划名（当前为 {plan['name']}）；"
                                    "请去掉 name，或改成与现有名称一致")
                if not any(payload.get(key) for key in ("instances", "flows", "tasks", "updates")):
                    raise PlanError("apply 更新已有计划至少需要一项新增或更新；name 不会改动现有计划")
            added = apply_request(plan, payload, set_task)
            if args.brief or dry_run:
                report = check_structure(plan)
            return {**added, "name": plan["name"]}

        outcome = store.apply(create_plan, operation, dry_run=dry_run)
        result = {"created": outcome["created"], "revision": outcome["revision"], **outcome["item"]}
        if dry_run:
            result.update(dry_run=True, proposed_revision=result["revision"] + 1)
        if report is not None:
            result["check"] = report
        return result
    if args.operation_name == "init":
        plan = new_plan(payload["name"] if payload is not None else args.name)
        added = (apply_request(plan, payload, set_task) if payload is not None else
                 apply_batch_specs(plan, args.instance, args.flow, args.task))
        report = check_structure(plan) if args.brief or dry_run else None
        created = store.create(plan, dry_run=dry_run)
        result = {"revision": created["revision"], "name": created["name"],
                  "instances": [i["id"] for i in created["instances"]],
                  "flows": [f["id"] for f in created["flows"]],
                  "tasks": [t["id"] for t in created["tasks"]],
                  "inferred": added["inferred"]}
        if payload is not None:
            result["aliases"] = added["aliases"]
        if dry_run:
            result.update(dry_run=True, proposed_revision=1)
        if report is not None:
            result["check"] = report
        return result
    if args.operation_name == "demo":
        created = store.create(demo_plan(), force=args.force)
        return {"revision": created["revision"], "name": created["name"],
                "tasks": [t["id"] for t in created["tasks"]]}
    if args.operation_name == "check":
        return check_structure(store.read())
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
        with server(store, args.port) as httpd:
            print(f"TrainOps http://127.0.0.1:{httpd.server_port} · 本机只读执行队列面板", flush=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
        return None

    def operation(plan):
        if args.operation_name == "batch":
            if payload is not None:
                return apply_request(plan, payload, set_task)
            if not (args.instance or args.flow or args.task or args.updates or args.dep_updates):
                raise PlanError("batch 至少需要提供 --instance、--flow、--task、--set 或 --dep 之一")
            return apply_batch_specs(plan, args.instance, args.flow, args.task, args.updates,
                                     args.dep_updates)
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
        structural = structural_request(payload) if payload is not None else structural_edit(args)
        if dry_run or (getattr(args, "brief", False) and structural):
            report = check_structure(plan)
        return item

    result = store.change(checked_operation, dry_run=dry_run)
    if dry_run:
        result.update(dry_run=True, proposed_revision=result["revision"] + 1)
    if report is not None:
        result["check"] = report
    return result


def main(argv=None):
    raw = sys.argv[1:] if argv is None else argv
    cli = parser()
    args = cli.parse_args(raw)
    try:
        if getattr(args, "iteration", None) is not None or getattr(args, "max_iter", None) is not None:
            if args.iteration is None or args.max_iter is None:
                raise PlanError("--iter 和 --max-iter 必须同时提供")
            if args.progress is not None:
                raise PlanError("--progress 不能与 --iter/--max-iter 同时使用")
            args.progress = iteration_progress(args.iteration, args.max_iter)
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
    if args.operation_name == "experiment" and result is not None and result.get("ok") is False:
        return 1
    # 默认沿用退出码 0（agent 读 JSON 即可），--strict 让脚本/CI 能直接判断检查结果。
    if args.operation_name == "check" and args.strict and not result["ok"]:
        return 1
    return 0


__all__ = [
    *_base.__all__, "new_plan", "validate", "default_config", "default_state", "new_document",
    "add_instance", "instance_mode", "add_flow", "add_task", "set_task", "forecast", "rounded",
    "node_progress", "flow_views", "snapshot", "check_structure", "demo_plan", "Store",
    "validate_document", "sync_directory", "Handler", "server", "ExperimentLog",
    "EXPERIMENT_FIELDS", "RUN_STATUSES", "RUN_TEXT", "RUN_UPDATE", "LarkNotStarted",
    "experiment_dispatch", "experiment_object", "reject_experiment_secrets",
    "validate_extra_metrics", "validate_experiment_config", "validate_run_patch",
    "validate_experiment_state", "reject_json_constant", "nullable", "duration", "iteration_progress",
    "dependencies", "gigabytes", "disk_changes", "disk_arg", "parse_instance_spec", "parse_task_spec",
    "parse_set_spec", "parse_dep_spec", "structural_edit", "brief_text", "brief_group", "brief_plan",
    "brief_receipt", "disk_text", "instance_line", "compact", "add_task_args", "parser", "dispatch", "main",
]


if __name__ == "__main__":
    sys.exit(main())
