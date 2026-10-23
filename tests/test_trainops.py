import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import inspect
import re
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

OPEN = build_opener(ProxyHandler({})).open

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/trainops/scripts/trainops.py"
spec = importlib.util.spec_from_file_location("trainops", SCRIPT)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)


def base():
    plan = ops.new_plan("测试")
    ops.add_instance(plan, "GPU A", "test-a", "gpu-a")
    ops.add_instance(plan, "GPU B", "test-b", "gpu-b")
    ops.add_flow(plan, "基线")
    ops.add_flow(plan, "对照")
    return plan


def run_cli(project, *args, success=True, env=None):
    """跑一次 CLI 子进程，返回 CompletedProcess；success 用来声明期望的退出码是否为 0。"""
    environment = {**os.environ, **(env or {})}
    result = subprocess.run([sys.executable, str(SCRIPT), "--project", str(project), *args],
                            text=True, capture_output=True, timeout=20, env=environment)
    if (result.returncode == 0) != success:
        raise AssertionError(f"退出码 {result.returncode}（期望 {'0' if success else '非 0'}）: "
                             f"{result.stdout}{result.stderr}")
    return result


def run_json(project, *args, success=True, env=None):
    result = run_cli(project, *args, success=success, env=env)
    return json.loads(result.stdout) if success and result.stdout.startswith("{") else result


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.plan = base()

    def test_stable_ids_after_rename_cancel_and_new_task(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 10)
        ops.set_task(self.plan, a["id"], {"name": "new", "status": "cancelled"}, now=NOW)
        b = ops.add_task(self.plan, "F01", "B", "I02", 20)
        self.assertEqual(a["id"], "F01-T01")
        self.assertEqual(b["id"], "F01-T02")
        ops.validate(self.plan)

    def test_cross_flow_dependencies_and_cycle(self):
        ops.add_task(self.plan, "F01", "A", "I01", 10)
        ops.add_task(self.plan, "F02", "B", "I02", 10, ["F01-T01"])
        ops.validate(self.plan)
        self.plan["tasks"][0]["deps"] = ["F02-T01"]
        with self.assertRaisesRegex(ops.PlanError, "环"):
            ops.validate(self.plan)

    def test_unknown_self_and_duplicate_deps(self):
        task = ops.add_task(self.plan, "F01", "A", "I01", 10)
        for deps in (["F09-T01"], ["F01-T01"], ["F02-T01", "F02-T01"]):
            task["deps"] = deps
            with self.subTest(deps=deps), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)

    def test_parallel_roots_and_diamond_are_valid_structure(self):
        ops.add_task(self.plan, "F01", "根A", "I01", 10)
        ops.add_task(self.plan, "F01", "根B", "I02", 10)
        ops.add_task(self.plan, "F01", "分支", "local", 5, ["F01-T01"])
        ops.add_task(self.plan, "F01", "汇合", "local", 5, ["F01-T02", "F01-T03"])
        ops.add_task(self.plan, "F02", "结果", "local", 5, ["F01-T04"])
        self.assertTrue(ops.check_structure(self.plan, NOW)["ok"])
        self.plan["tasks"][0]["deps"] = ["F01-T04"]
        with self.assertRaisesRegex(ops.PlanError, "环"):
            ops.check_structure(self.plan, NOW)

    def test_unknown_duration_severity_by_status(self):
        for status in ("pending", "running", "blocked", "done", "failed", "cancelled"):
            with self.subTest(status=status):
                plan = ops.new_plan("待估")
                ops.add_flow(plan, "流程")
                task = ops.add_task(plan, "F01", "任务", "local", None)
                if status != "pending":
                    ops.set_task(plan, task["id"], {"status": "running"}, NOW)
                    ops.set_task(plan, task["id"], {"status": status}, NOW)
                result = ops.check_structure(plan, NOW)
                self.assertEqual(result["ok"], status not in ("running", "blocked"))
                self.assertEqual(any("未设置预计耗时" in x for x in result["warnings"]),
                                 status == "pending")

    def test_pending_unknown_durations_are_counted_across_flows(self):
        plan = base()
        for index in range(15):
            ops.add_task(plan, "F01" if index < 8 else "F02", f"待估{index}", "local", None)
        ops.add_task(plan, "F01", "零耗时", "local", 0)
        for status in ("running", "blocked", "done", "failed", "cancelled"):
            task = ops.add_task(plan, "F02", status, "local", None)
            ops.set_task(plan, task["id"], {"status": "running"}, NOW)
            if status != "running":
                ops.set_task(plan, task["id"], {"status": status}, NOW)
        report = ops.check_structure(plan, NOW)
        self.assertEqual(len(report["warnings"]), 1)
        self.assertTrue(report["warnings"][0].startswith("15 个 pending 任务未设置预计耗时"))
        self.assertEqual(len(report["issues"]), 2)
        self.assertFalse(report["ok"])
        for task in plan["tasks"][16:18]:
            self.assertTrue(any(task["id"] in item for item in report["issues"]))

    def test_duration_summary_follows_actionable_warnings_in_brief(self):
        plan = ops.new_plan("提醒优先")
        ops.add_instance(plan, "GPU", "test", "gpu")
        ops.add_flow(plan, "实验")
        for index in range(15):
            ops.add_task(plan, "F01", f"待估{index}", "I01", None)
        plan["instances"][0]["disk"]["data_used_gb"] = 48
        plan["instances"][0]["disk"]["data_total_gb"] = 50
        report = ops.check_structure(plan, NOW)
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["warnings"]), 2)
        self.assertIn("数据盘已用", report["warnings"][0])
        self.assertIn("15 个 pending", report["warnings"][-1])
        text = ops.brief_plan(plan, NOW)
        self.assertIn("数据盘已用", text)
        self.assertNotIn("自动结算失败", text)
        self.assertNotIn("余额", text)
        self.assertEqual(text.count("未设置预计耗时"), 1)
        warnings_text = text.split("提醒（共 2 条）:", 1)[1].split("完整计划用 show", 1)[0]
        self.assertNotIn("省略", warnings_text)

    def test_batch_spec_distinguishes_omitted_and_explicit_empty_dependencies(self):
        for spec in ("F01:任务", "F01:任务:I01", "F01:任务:I01:?"):
            with self.subTest(spec=spec):
                self.assertIsNone(ops.parse_task_spec(spec)[4])
        for suffix in ("", "-", "none"):
            with self.subTest(suffix=suffix):
                self.assertEqual(ops.parse_task_spec(f"F01:任务:I01:?:{suffix}")[4], [])
        self.assertEqual(ops.parse_task_spec("F01:任务:-:-:F01-T01,F02-T01"),
                         ("F01", "任务", None, None, ["F01-T01", "F02-T01"]))

    def test_parse_dep_spec_distinguishes_clear_and_list(self):
        self.assertEqual(ops.parse_dep_spec("F01-T01=F02-T01,F02-T02"),
                         ("F01-T01", ["F02-T01", "F02-T02"]))
        for raw in ("F01-T01=", "F01-T01=none", "F01-T01=-", "F01-T01= "):
            with self.subTest(raw=raw):
                self.assertEqual(ops.parse_dep_spec(raw), ("F01-T01", []))
        for raw in ("F01-T01", "=F02-T01", ""):
            with self.subTest(raw=raw), self.assertRaises(ops.PlanError):
                ops.parse_dep_spec(raw)

    def test_invalid_numbers_and_dates(self):
        task = ops.add_task(self.plan, "F01", "A", "I01", 10)
        for value in (-1, float("nan"), float("inf"), True, "ten"):
            task["duration_minutes"] = value
            with self.subTest(value=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        task["duration_minutes"] = 0
        ops.validate(self.plan)
        for value in ("2026-10-14", "2026-10-14T10:00:00", "invalid", 123):
            task["not_before"] = value
            with self.subTest(value=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        task["not_before"] = "2026-10-14T10:00:00+08:00"
        task["progress"] = 101
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)

    def test_duplicate_instance_mapping(self):
        for key in ("autodl_id", "ssh_alias"):
            plan = copy.deepcopy(self.plan)
            plan["instances"][1][key] = plan["instances"][0][key]
            with self.assertRaisesRegex(ops.PlanError, "重复"):
                ops.validate(plan)
        self.plan["instances"][0]["ssh_alias"] = "-oProxyCommand=bad"
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)

    def test_running_requires_resource_and_satisfied_deps(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 10)
        b = ops.add_task(self.plan, "F02", "B", "I02", 10, [a["id"]])
        c = ops.add_task(self.plan, "F01", "C", None, 10)
        ops.set_task(self.plan, a["id"], {"status": "running"}, now=NOW)
        ops.validate(self.plan)
        ops.set_task(self.plan, a["id"], {"status": "cancelled"}, NOW)
        ops.set_task(self.plan, a["id"], {"status": "pending"}, NOW)
        ops.set_task(self.plan, c["id"], {"status": "running"}, NOW)
        with self.assertRaisesRegex(ops.PlanError, "实例"):
            ops.validate(self.plan)
        ops.set_task(self.plan, c["id"], {"status": "cancelled"}, NOW)
        ops.set_task(self.plan, c["id"], {"status": "pending"}, NOW)
        ops.set_task(self.plan, b["id"], {"status": "running"}, NOW)
        with self.assertRaisesRegex(ops.PlanError, "尚未完成"):
            ops.validate(self.plan)

    def test_resource_exclusivity_and_running_edit_lock(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 10)
        b = ops.add_task(self.plan, "F02", "B", "I01", 10)
        ops.set_task(self.plan, a["id"], {"status": "running"}, NOW)
        ops.validate(self.plan)
        with self.assertRaisesRegex(ops.PlanError, "运行中不能"):
            ops.set_task(self.plan, a["id"], {"instance": "I02"}, NOW)
        ops.set_task(self.plan, a["id"], {"progress": 5}, NOW)
        self.assertEqual(a["progress"], 5)
        ops.set_task(self.plan, b["id"], {"status": "running"}, NOW)
        with self.assertRaisesRegex(ops.PlanError, "占用"):
            ops.validate(self.plan)

    def test_not_before_and_actual_times(self):
        task = ops.add_task(self.plan, "F01", "A", "I01", 10, not_before=ops.stamp(NOW + timedelta(hours=1)))
        ops.set_task(self.plan, task["id"], {"status": "running"}, NOW)
        with self.assertRaisesRegex(ops.PlanError, "允许开始"):
            ops.validate(self.plan)
        task["not_before"] = None
        ops.set_task(self.plan, task["id"], {"status": "done"}, NOW - timedelta(minutes=1))
        with self.assertRaisesRegex(ops.PlanError, "结束时间"):
            ops.validate(self.plan)

    def test_done_cannot_bypass_running_and_retry_clears_times(self):
        task = ops.add_task(self.plan, "F01", "A", "local", 10)
        with self.assertRaisesRegex(ops.PlanError, "不允许"):
            ops.set_task(self.plan, task["id"], {"status": "done"}, now=NOW)
        ops.set_task(self.plan, task["id"], {"status": "running"}, NOW)
        ops.set_task(self.plan, task["id"], {"status": "failed"}, NOW)
        ops.set_task(self.plan, task["id"], {"status": "pending"}, now=NOW)
        for field in ("started_at", "finished_at", "checked_at", "progress", "progress_at"):
            self.assertIsNone(task[field])
        ops.validate(self.plan)

    def test_completed_dependency_cannot_be_reset_under_running_child(self):
        a = ops.add_task(self.plan, "F01", "A", "local", 10)
        b = ops.add_task(self.plan, "F02", "B", "I02", 10, [a["id"]])
        ops.set_task(self.plan, a["id"], {"status": "running"}, NOW - timedelta(minutes=20))
        ops.set_task(self.plan, a["id"], {"status": "done"}, NOW - timedelta(minutes=10))
        ops.set_task(self.plan, b["id"], {"status": "running"}, NOW)
        ops.set_task(self.plan, a["id"], {"status": "pending"}, now=NOW)
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)

    def test_bad_schema_and_counters(self):
        # 开发阶段不做迁移：任何不等于当前版本的 schema 都直接硬拒并提示备份，绝不猜着读。
        for value in (1, 2, ops.SCHEMA_VERSION + 1):
            self.plan["schema_version"] = value
            with self.subTest(value=value), self.assertRaises(ops.PlanError) as error:
                ops.validate(self.plan)
            self.assertIn("不提供迁移", str(error.exception))
        # 类型都不对的值被更早的守卫拦下：同样是硬拒，但不该说"迁移"
        for value in (True, "1", None):
            self.plan["schema_version"] = value
            with self.subTest(value=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        self.plan["schema_version"] = ops.SCHEMA_VERSION
        self.plan["next_instance"] = 1
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)

    def test_disk_bounds(self):
        item = self.plan["instances"][0]
        for value in (-1, "40", float("inf")):
            item["disk"]["data_used_gb"] = value
            with self.subTest(disk=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        item["disk"]["data_used_gb"] = None
        item["disk"]["data_used_gb"], item["disk"]["data_total_gb"] = 60, 50
        with self.assertRaisesRegex(ops.PlanError, "大于总容量"):
            ops.validate(self.plan)

    def test_mode_bounds(self):
        item = self.plan["instances"][0]
        for value in ("no_gpu", "", 0, True):
            item["mode"] = value
            with self.subTest(mode=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        for value in ("no-gpu", "gpu", None):
            item["mode"] = value
            ops.validate(self.plan)
            self.assertEqual(ops.instance_mode(item), value)

    def test_legacy_plans_without_mode_still_read(self):
        """开机模式是后加的字段：老计划少了它也要照常读，不逼人重建计划。"""
        for item in self.plan["instances"]:
            del item["mode"]
        ops.validate(self.plan)
        self.assertIsNone(ops.instance_mode(self.plan["instances"][0]))
        self.plan["instances"][0]["status"] = "on"
        self.assertIn("[on]", ops.instance_line(self.plan["instances"][0]))

    def test_new_plan_and_instances_have_no_financial_fields(self):
        """余额、结算与实例时价已删除：新计划和新实例都不再生成这些字段。"""
        plan = ops.new_plan("无计费")
        for key in ("wallet", "monitor"):
            self.assertNotIn(key, plan)
        ops.add_instance(plan, "A", "test-a", "gpu-a")
        item = plan["instances"][0]
        for key in ("price", "no_gpu_price"):
            self.assertNotIn(key, item)
        self.assertIn("mode", item, "开机模式仍然保留")
        self.assertIn("disk", item, "三盘用量仍然保留")
        ops.validate(plan)


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.plan = base()

    def estimate(self):
        ops.validate(self.plan)
        return ops.forecast(self.plan, NOW)

    def test_parallel_instances_and_serial_lane(self):
        ops.add_task(self.plan, "F01", "A", "I01", 60)
        ops.add_task(self.plan, "F02", "B", "I02", 30)
        ops.add_task(self.plan, "F02", "C", "I01", 10)
        view = self.estimate()
        self.assertEqual(view["F01-T01"]["finish"], ops.stamp(NOW + timedelta(minutes=60)))
        self.assertEqual(view["F02-T01"]["finish"], ops.stamp(NOW + timedelta(minutes=30)))
        self.assertEqual(view["F02-T02"]["start"], view["F01-T01"]["finish"])

    def test_dependency_scheduled_before_lower_id_on_same_lane(self):
        ops.add_task(self.plan, "F01", "child", "I01", 10, ["F02-T01"])
        ops.add_task(self.plan, "F02", "parent", "I01", 30)
        view = self.estimate()
        self.assertEqual(view["F02-T01"]["start"], ops.stamp(NOW))
        self.assertEqual(view["F01-T01"]["start"], ops.stamp(NOW + timedelta(minutes=30)))

    def test_join_waits_for_both_flows(self):
        ops.add_task(self.plan, "F01", "A", "I01", 60)
        ops.add_task(self.plan, "F02", "B", "I02", 30)
        ops.add_task(self.plan, "F01", "join", "local", 10, ["F01-T01", "F02-T01"])
        self.assertEqual(self.estimate()["F01-T02"]["finish"], ops.stamp(NOW + timedelta(minutes=70)))

    def test_unknown_duration_propagates_dependency_and_lane(self):
        ops.add_task(self.plan, "F01", "unknown", "I01", None)
        ops.add_task(self.plan, "F01", "child", "I02", 10, ["F01-T01"])
        ops.add_task(self.plan, "F02", "same lane", "I01", 10)
        ops.add_task(self.plan, "F02", "other lane", "I02", 10)
        view = self.estimate()
        self.assertIsNone(view["F01-T01"]["finish"])
        self.assertIsNone(view["F01-T02"]["finish"])
        self.assertIsNone(view["F02-T01"]["finish"])
        self.assertIsNotNone(view["F02-T02"]["finish"])

    def test_failed_cancelled_blocked_are_not_completed_deps(self):
        for state in ("failed", "cancelled", "blocked"):
            with self.subTest(state=state):
                plan = base()
                parent = ops.add_task(plan, "F01", "A", "I01", 10)
                ops.add_task(plan, "F02", "B", "I02", 10, [parent["id"]])
                if state == "failed":
                    ops.set_task(plan, parent["id"], {"status": "running"}, NOW)
                    ops.set_task(plan, parent["id"], {"status": state}, NOW)
                else:
                    ops.set_task(plan, parent["id"], {"status": state}, now=NOW)
                ops.validate(plan)
                self.assertIsNone(ops.forecast(plan, NOW)["F02-T01"]["finish"])

    def test_not_before_and_unassigned(self):
        ops.add_task(self.plan, "F01", "A", "I01", 10, not_before=ops.stamp(NOW + timedelta(hours=2)))
        ops.add_task(self.plan, "F02", "B", None, 10)
        view = self.estimate()
        self.assertEqual(view["F01-T01"]["start"], ops.stamp(NOW + timedelta(hours=2)))
        self.assertFalse(view["F01-T01"]["ready"])
        self.assertIsNone(view["F02-T01"]["finish"])
        self.assertFalse(view["F02-T01"]["ready"])

    def test_running_progress_is_sampled_not_recomputed_from_now(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 120)
        ops.set_task(self.plan, a["id"], {"status": "running"}, NOW - timedelta(minutes=30))
        ops.set_task(self.plan, a["id"], {"progress": 50}, NOW - timedelta(minutes=10))
        first = self.estimate()[a["id"]]["finish"]
        later = ops.forecast(self.plan, NOW + timedelta(minutes=5))[a["id"]]["finish"]
        self.assertEqual(first, ops.stamp(NOW + timedelta(minutes=10)))
        self.assertEqual(later, first)

    def test_overdue_and_100_percent_are_unknown_until_verified(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 10)
        ops.add_task(self.plan, "F02", "B", "I01", 10)
        ops.set_task(self.plan, a["id"], {"status": "running"}, NOW - timedelta(minutes=15))
        view = self.estimate()
        self.assertIsNone(view[a["id"]]["finish"])
        self.assertTrue(view[a["id"]]["overdue"])
        self.assertIsNone(view["F02-T01"]["finish"])
        ops.set_task(self.plan, a["id"], {"progress": 100}, NOW)
        self.assertIsNone(self.estimate()[a["id"]]["finish"])

    def test_local_tasks_parallel_and_ready_is_not_auto_started(self):
        ops.add_task(self.plan, "F01", "A", "local", 10)
        ops.add_task(self.plan, "F02", "B", "local", 10)
        view = self.estimate()
        self.assertEqual(view["F01-T01"]["start"], view["F02-T01"]["start"])
        self.assertTrue(view["F01-T01"]["ready"])
        self.assertEqual(self.plan["tasks"][0]["status"], "pending")

    def test_demo_snapshot(self):
        plan = ops.demo_plan(NOW)
        ops.validate(plan)
        before = copy.deepcopy(plan)
        view = ops.snapshot(plan, NOW)
        self.assertEqual(len(view["tasks"]), 7)
        self.assertTrue(all("estimate" in t and "view" in t for t in view["tasks"]))
        self.assertTrue(all("view" in f for f in view["flows"]))
        self.assertNotIn("graph", view, "快照不再下发依赖图几何")
        self.assertNotIn("graph", view["flows"][0], "流程只保留完成结论")
        by_id = {task["id"]: task for task in view["tasks"]}
        self.assertEqual(by_id["F01-T01"]["view"], {"progress": 100}, "done 一律满格")
        self.assertEqual(by_id["F01-T02"]["view"], {"progress": 18}, "登记进度四舍五入取整")
        self.assertEqual(by_id["F01-T03"]["view"], {"progress": 0}, "未知进度不猜")
        self.assertEqual(by_id["F01-T02"]["progress"], 18, "视图不覆盖原 progress")
        self.assertIsNone(by_id["F01-T03"]["progress"])
        self.assertEqual(plan, before, "快照不能写回原计划")
        self.assertFalse(any("estimate" in t for t in plan["tasks"]))
        self.assertFalse(any("bar" in t for t in plan["tasks"]))


class ExecutionQueueTests(unittest.TestCase):
    def setUp(self):
        self.plan = base()

    def add(self, flow="F01", instance="I01", minutes=None, deps=None):
        return ops.add_task(self.plan, flow, "独立实验", instance, minutes,
                            deps=[] if deps is None else deps)

    def queues(self):
        return ops.snapshot(self.plan, NOW)["queues"]

    def lane(self, instance="I01"):
        return next(lane["tasks"] for lane in self.queues()["lanes"]
                    if lane["instance"] == instance)

    def test_wwtp_independent_experiments_form_two_queues_without_dependencies(self):
        ops.add_flow(self.plan, "基线")
        running = self.add(instance="I02", minutes=120)
        ops.set_task(self.plan, running["id"], {"status": "running"}, NOW)
        first = [self.add()["id"] for _ in range(6)]
        first.append(self.add(flow="F02")["id"])
        second = [running["id"]] + [self.add(flow="F03", instance="I02")["id"] for _ in range(7)]
        before = copy.deepcopy(self.plan)
        view = ops.snapshot(self.plan, NOW)
        self.assertEqual([lane["tasks"] for lane in view["queues"]["lanes"]], [first, second])
        self.assertEqual(sum(len(lane["tasks"]) - 1 for lane in view["queues"]["lanes"]), 13)
        self.assertNotIn("graph", view, "独立实验不再生成图几何")
        self.assertEqual(len({key for lane in view["queues"]["lanes"] for key in lane["tasks"]}), 15)
        self.assertEqual(self.plan, before, "展示不能改变 deps、状态或 revision")
        self.assertIsNone(view["tasks"][2]["estimate"]["finish"], "不猜测未知耗时")

    def test_running_task_precedes_lower_numbered_pending(self):
        pending, running = self.add(), self.add()
        ops.set_task(self.plan, running["id"], {"status": "running"}, NOW)
        self.assertEqual(self.lane(), [running["id"], pending["id"]])

    def test_dependency_precedes_number_even_with_unknown_duration(self):
        first, later = self.add(), self.add(flow="F02")
        ops.set_task(self.plan, first["id"], {"deps": [later["id"]]}, NOW)
        self.assertEqual(self.lane(), [later["id"], first["id"]])
        self.assertIn(later["id"], ops.forecast(self.plan, NOW)[first["id"]]["reason"])

    def test_not_before_and_reassignment_follow_forecast(self):
        delayed, ready = self.add(minutes=10), self.add(minutes=10)
        ops.set_task(self.plan, delayed["id"], {"not_before": ops.stamp(NOW + timedelta(hours=1))}, NOW)
        self.assertEqual(self.lane(), [ready["id"], delayed["id"]])
        ops.set_task(self.plan, ready["id"], {"instance": "I02"}, NOW)
        self.assertEqual(self.lane(), [delayed["id"]])
        self.assertEqual(self.lane("I02"), [ready["id"]])

    def test_local_unassigned_and_inactive_are_not_serialized(self):
        local, unassigned = self.add(instance="local"), self.add(instance=None)
        inactive = []
        for status in ("blocked", "done", "failed", "cancelled"):
            task = self.add()
            if status in ("done", "failed"):
                ops.set_task(self.plan, task["id"], {"status": "running"}, NOW)
            ops.set_task(self.plan, task["id"], {"status": status}, NOW)
            inactive.append(task["id"])
        queues = self.queues()
        self.assertEqual(self.lane(), [])
        self.assertEqual([group["tasks"] for group in queues["other"]],
                         [[local["id"]], [unassigned["id"]], inactive])
        ops.set_task(self.plan, inactive[0], {"status": "pending"}, NOW)
        self.assertEqual(self.lane(), [inactive[0]])


class FlowViewTests(unittest.TestCase):
    """流程总进度与任务节点进度由服务端统一下发。"""

    def setUp(self):
        self.plan = base()

    def views(self):
        ops.validate(self.plan)
        return ops.flow_views(self.plan, NOW)

    def finish(self, flow, count, minutes=30, status="done"):
        """往 flow 里塞 count 个同时起止的任务：它们的时间区间完全重叠，旧公式会算错。"""
        for index in range(count):
            task = ops.add_task(self.plan, flow, f"{status}{index}", "I01", minutes)
            if status == "done":
                ops.set_task(self.plan, task["id"], {"status": "running"}, NOW - timedelta(minutes=minutes + 10))
                ops.set_task(self.plan, task["id"], {"status": "done"}, NOW - timedelta(minutes=10))
            else:
                ops.set_task(self.plan, task["id"], {"status": status}, NOW)
        return task

    def test_total_progress_counts_tasks_not_bar_widths(self):
        """5 个并行完成 + 2 个取消：按任务数算，不累加重叠的时间区间。"""
        self.finish("F01", 5, status="done")
        self.finish("F01", 2, status="cancelled")
        view = self.views()["F01"]
        self.assertEqual((view["done"], view["countable"], view["cancelled"], view["total"]), (5, 5, 2, 7))
        self.assertEqual(view["progress"], 100, "取消的不再需要完成，不该把进度压在 100% 以下")

    def test_progress_denominator_is_the_uncancelled_tasks(self):
        self.finish("F01", 2, status="done")
        self.finish("F01", 1, status="cancelled")
        ops.add_task(self.plan, "F01", "待跑", "I01", 30)
        views = self.views()
        self.assertEqual((views["F01"]["done"], views["F01"]["countable"]), (2, 3))
        self.assertEqual(views["F01"]["progress"], 67)

    def test_everything_cancelled_has_no_progress_at_all(self):
        self.finish("F02", 2, status="cancelled")
        views = self.views()
        self.assertEqual(views["F02"]["countable"], 0)
        self.assertIsNone(views["F02"]["progress"], "没有需要完成的任务时不下结论")

    def test_finished_flow_reports_completion_instead_of_pending_estimate(self):
        """已结束的任务没有"预计完成时间"，不该把整个流程说成"完成待估"。"""
        task = ops.add_task(self.plan, "F01", "A", "I01", 30)
        ops.set_task(self.plan, task["id"], {"status": "running"}, NOW)
        ops.set_task(self.plan, task["id"], {"status": "done"}, NOW + timedelta(minutes=30))
        dropped = ops.add_task(self.plan, "F01", "B", "I01", 30, ["F01-T01"])
        ops.set_task(self.plan, dropped["id"], {"status": "cancelled"}, NOW + timedelta(minutes=40))
        views = self.views()
        self.assertEqual(views["F01"]["open"], 0)
        self.assertTrue(views["F01"]["known"])
        self.assertEqual(views["F01"]["finish"], ops.stamp(NOW + timedelta(minutes=30)))

    def test_node_progress_rounds_and_clamps_registered_samples(self):
        self.assertEqual(ops.node_progress({"status": "running", "progress": 18.5}), 19)
        self.assertEqual(ops.node_progress({"status": "running", "progress": 18.4}), 18)
        self.assertEqual(ops.node_progress({"status": "running", "progress": 0.5}), 1)
        self.assertEqual(ops.node_progress({"status": "running", "progress": 99.5}), 100)
        self.assertEqual(ops.node_progress({"status": "running", "progress": -1}), 0)
        self.assertEqual(ops.node_progress({"status": "running", "progress": 101}), 100)

    def test_node_progress_done_is_full_and_unknown_is_zero(self):
        self.assertEqual(ops.node_progress({"status": "done", "progress": None}), 100)
        self.assertEqual(ops.node_progress({"status": "done", "progress": 12}), 100)
        self.assertEqual(ops.node_progress({"status": "running", "progress": None}), 0)
        self.assertEqual(ops.node_progress({"status": "pending", "progress": None}), 0)

    def test_snapshot_progress_does_not_overwrite_planned_progress(self):
        task = ops.add_task(self.plan, "F01", "A", "I01", 30)
        ops.set_task(self.plan, task["id"], {"status": "running", "progress": 18.5}, NOW)
        before = copy.deepcopy(self.plan)
        view = ops.snapshot(self.plan, NOW)
        item = next(t for t in view["tasks"] if t["id"] == task["id"])
        self.assertEqual(item["view"], {"progress": 19})
        self.assertEqual(item["progress"], 18.5, "原 progress 保持登记值")
        self.assertEqual(task["progress"], 18.5)
        self.assertEqual(self.plan, before, "快照不写回原计划")


class RepoSnapshotTests(unittest.TestCase):
    """仓库里提交的演示快照必须和 demo_plan() 结构一致，防止测试与演示数据悄悄分叉。"""

    def test_committed_demo_snapshot_matches_demo_plan(self):
        document = json.loads((ROOT / "examples/demo-project/.agent/experiment.yaml").read_text(encoding="utf-8"))
        committed = document["plan"]
        fresh = ops.demo_plan(NOW)
        fields = ("id", "flow", "name", "instance", "deps", "duration_minutes", "status", "progress", "note")
        hint = ("演示快照已过期：python3 skills/trainops/scripts/trainops.py "
                "-p examples/demo-project demo --force")
        self.assertEqual([[t[f] for f in fields] for t in committed["tasks"]],
                         [[t[f] for f in fields] for t in fresh["tasks"]], hint)
        # 开机模式与三盘用量也是演示数据的一部分：不比对就看不到它们的漂移（时间戳除外）。
        instance_fields = ("id", "name", "status", "mode", "note", "disk")
        self.assertEqual([{f: i[f] for f in instance_fields} for i in committed["instances"]],
                         [{f: i[f] for f in instance_fields} for i in fresh["instances"]], hint)
        for item in fresh["instances"]:
            for field in ("price", "no_gpu_price"):
                self.assertNotIn(field, item, hint)
        self.assertNotIn("wallet", fresh, hint)
        self.assertNotIn("monitor", fresh, hint)
        self.assertEqual([f["name"] for f in committed["flows"]],
                         [f["name"] for f in fresh["flows"]], hint)
        self.assertEqual(committed["schema_version"], ops.SCHEMA_VERSION, hint)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ops.Store(self.project)

    def command(self, *args, success=True, env=None):
        return run_json(self.project, *args, success=success, env=env)

    def setup_plan(self):
        self.command("init", "--name", "实验")
        self.command("instance", "add", "--name", "A", "--autodl-id", "test-a", "--ssh-alias", "gpu-a")
        self.command("flow", "add", "--name", "基线")
        self.command("task", "add", "--flow", "F01", "--name", "train", "--instance", "I01", "--minutes", "60")

    def test_end_to_end_commands(self):
        self.setup_plan()
        self.command("instance", "set", "I01", "--status", "on", "--observed")
        self.command("task", "set", "F01-T01", "--status", "running")
        self.command("task", "set", "F01-T01", "--progress", "20", "--checked")
        view = self.command("show", "--json")
        self.assertEqual(view["tasks"][0]["progress"], 20)
        self.assertIsNotNone(view["tasks"][0]["checked_at"])
        self.assertNotIn("owner", view["tasks"][0])
        self.command("task", "set", "F01-T01", "--status", "done")
        self.command("flow", "set", "F01", "--name", "改名")
        final = self.store.read()
        self.assertEqual(final["tasks"][0]["status"], "done")
        self.assertEqual(final["flows"][0]["id"], "F01")

    def test_progress_accepts_percentages_and_iteration_counts(self):
        self.setup_plan()
        for prefix in (("set",), ("task", "set")):
            for arguments, expected in (
                (("--progress", "35.5"), 35.5),
                (("--progress", "800/7500"), 800 / 7500 * 100),
                (("--iter", "800", "--max-iter", "7500"), 800 / 7500 * 100),
                (("--progress", "0/7500"), 0),
                (("--progress", "7500/7500"), 100),
                (("--iter", "0", "--max-iter", "7500"), 0),
                (("--iter", "7500", "--max-iter", "7500"), 100),
            ):
                with self.subTest(prefix=prefix, arguments=arguments):
                    result = self.command(*prefix, "F01-T01", "--status", "running", *arguments)
                    task = self.store.read()["tasks"][0]
                    self.assertAlmostEqual(result["item"]["progress"], expected)
                    self.assertAlmostEqual(task["progress"], expected)
                    self.assertIsNotNone(task["checked_at"])
                    self.assertIsNotNone(task["progress_at"])
                    self.assertEqual(task["status"], "running")
                    self.assertNotIn("iteration", task)
                    self.assertNotIn("max_iter", task)

    def test_invalid_iteration_progress_does_not_change_plan(self):
        self.setup_plan()
        original = self.store.path.read_bytes()
        invalid = [("--progress", value) for value in (
            "800/0", "0/0", "-1/7500", "800/-7500", "7501/7500",
            "800.5/7500", "800/7500.5", "800/", "/7500", "800/7500/9000",
            "nan/7500", "800/inf", "abc", "nan", "inf", "-1", "101",
        )]
        invalid += [
            ("--iter", "800"), ("--max-iter", "7500"),
            ("--iter", "-1", "--max-iter", "7500"),
            ("--iter", "800", "--max-iter", "0"),
            ("--iter", "800", "--max-iter", "-1"),
            ("--iter", "7501", "--max-iter", "7500"),
            ("--iter", "800.5", "--max-iter", "7500"),
            ("--progress", "20", "--iter", "800", "--max-iter", "7500"),
            ("--progress", "20", "--max-iter", "7500"),
        ]
        for prefix in (("set",), ("task", "set")):
            for arguments in invalid:
                with self.subTest(prefix=prefix, arguments=arguments):
                    result = self.command(*prefix, "F01-T01", "--status", "running", *arguments, success=False)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(original, self.store.path.read_bytes())

    def test_show_marks_ready_overdue_and_stale(self):
        self.setup_plan()
        self.command("instance", "add", "--name", "B", "--autodl-id", "test-b", "--ssh-alias", "gpu-b")
        self.command("task", "set", "F01-T01", "--status", "done")
        # 依赖已完成 + 实例空闲 = 可开工（实例必须已分配，未分配的任务只提示补实例）
        self.command("task", "add", "--flow", "F01", "--name", "评估", "--instance", "I01",
                     "--deps", "F01-T01", "--minutes", "15")
        self.command("task", "add", "--flow", "F01", "--name", "长跑", "--instance", "I02", "--minutes", "60")
        # 直接写入一个 3 小时前开始、2 小时前最后巡检的 running 任务：既超期也未巡检
        plan = self.store.read()
        long_run = plan["tasks"][2]
        plan["tasks"][2] = long_run | {"status": "running", "progress": 20, "deps": [],
                                       "started_at": ops.stamp(ops.utcnow() - timedelta(hours=3)),
                                       "progress_at": ops.stamp(ops.utcnow() - timedelta(hours=3)),
                                       "checked_at": ops.stamp(ops.utcnow() - timedelta(hours=2))}
        self.store.write(plan)
        text = self.command("show").stdout
        self.assertIn("可开工", text)
        self.assertIn("已超期", text)
        self.assertIn("未巡检", text)
        self.assertIn("[running 20% 已超期 未巡检]", text)

    def test_clear_fields_and_rejected_edit_is_atomic(self):
        self.setup_plan()
        self.command("task", "add", "--flow", "F01", "--name", "child", "--instance", "local", "--deps", "F01-T01")
        self.command("task", "set", "F01-T02", "--deps", "", "--instance", "none", "--minutes", "?",
                     "--not-before", "none", "--note=--instance")
        task = self.store.read()["tasks"][1]
        self.assertEqual(task["deps"], [])
        self.assertIsNone(task["instance"])
        original = self.store.path.read_bytes()
        self.command("task", "set", "F01-T01", "--deps", "F01-T01", success=False)
        self.assertEqual(original, self.store.path.read_bytes())

    def test_nan_negative_and_duplicate_instance_rejected(self):
        self.setup_plan()
        original = self.store.path.read_bytes()
        for value in ("nan", "inf", "-2"):
            self.command("task", "set", "F01-T01", "--minutes", value, success=False)
        self.command("task", "set", "F01-T01", "--progress", "nan", success=False)
        self.command("instance", "add", "--name", "duplicate", "--autodl-id", "test-a", "--ssh-alias", "gpu-new", success=False)
        self.assertEqual(original, self.store.path.read_bytes())

    def test_init_and_demo_never_overwrite_existing(self):
        self.setup_plan()
        original = self.store.path.read_bytes()
        self.command("init", "--name", "overwrite", success=False)
        self.command("demo", success=False)
        self.assertEqual(original, self.store.path.read_bytes())
        # demo --force 是唯一的覆盖入口，且只重置为演示数据
        self.command("demo", "--force")
        plan = self.store.read()
        self.assertEqual((len(plan["tasks"]), plan["revision"]), (7, 1))

    def test_corrupt_plan_never_overwritten(self):
        self.setup_plan()
        self.store.path.write_text("not-json", encoding="utf-8")
        self.command("flow", "add", "--name", "lost", success=False)
        self.assertEqual(self.store.path.read_text(), "not-json")

    def test_legacy_financial_fields_stay_readable_but_never_reshape_the_view(self):
        """旧 schema 3 计划里的余额/结算/时价字段忽略即可：训练更新照常，展示不含它们，文件不迁移。"""
        self.setup_plan()
        document = self.store.read_document()
        document["plan"]["wallet"] = {"assets": 128.5, "voucher": 20.0, "observed_at": ops.stamp(NOW)}
        document["plan"]["monitor"] = {"interval_minutes": 30, "last_error": "", "token_ready": False,
                                       "estimate": {"computed_at": ops.stamp(NOW), "revision": 1}}
        document["plan"]["instances"][0].update(price=1.88, no_gpu_price=0.1)
        self.store.write_document(document)
        before = self.store.read_document()["plan"]
        # 训练更新照常可用，且不会因为旧字段被判成坏计划
        self.command("task", "set", "F01-T01", "--status", "running")
        self.command("task", "set", "F01-T01", "--progress", "20")
        after = self.store.read()["tasks"][0]
        self.assertEqual((after["status"], after["progress"]), ("running", 20))
        # 旧文件不被自动迁移：额外字段原样留在磁盘上，只是不再参与校验与展示
        stored = self.store.read_document()["plan"]
        self.assertEqual(stored["wallet"], before["wallet"])
        self.assertEqual(stored["monitor"], before["monitor"])
        self.assertEqual((stored["instances"][0]["price"],
                          stored["instances"][0]["no_gpu_price"]), (1.88, 0.1))
        for view in (ops.snapshot(self.store.read()), run_json(self.project, "show", "--json")):
            for key in ("wallet", "monitor", "credentials"):
                self.assertNotIn(key, view)
            for item in view["instances"]:
                for key in ("price", "no_gpu_price"):
                    self.assertNotIn(key, item)

    def test_running_mapping_locked_but_progress_open(self):
        self.setup_plan()
        self.command("task", "set", "F01-T01", "--status", "running")
        self.command("instance", "set", "I01", "--ssh-alias", "different", success=False)
        self.command("task", "set", "F01-T01", "--progress", "30")
        self.assertEqual(self.store.read()["tasks"][0]["progress"], 30)

    def test_parallel_process_updates_do_not_lose_tasks(self):
        self.setup_plan()
        processes = [subprocess.Popen([sys.executable, str(SCRIPT), "--project", str(self.project),
                     "task", "add", "--flow", "F01", "--name", f"parallel-{i}", "--instance", "local"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(12)]
        for process in processes:
            out, err = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, out + err)
        plan = self.store.read()
        self.assertEqual(len(plan["tasks"]), 13)
        self.assertEqual(len({t["id"] for t in plan["tasks"]}), 13)
        self.assertEqual(plan["revision"], 16)

    def test_parallel_running_sets_are_serialized(self):
        self.setup_plan()
        processes = [subprocess.Popen([sys.executable, str(SCRIPT), "--project", str(self.project),
                     "task", "set", "F01-T01", "--status", "running"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
        for process in processes:
            out, err = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, out + err)
        plan = self.store.read()
        self.assertEqual(plan["tasks"][0]["status"], "running")
        self.assertEqual(plan["revision"], 12)  # 4 次建表 + 8 次更新，一次都没丢

    def test_streamlined_init_batch_and_quick_set(self):
        out = self.command(
            "init", "--name", "极简实验",
            "--instance", "GPU-A:id-a:gpu-a",
            "--instance", "GPU-B:id-b:gpu-b",
            "--flow", "基线", "--flow", "对照",
            "--task", "F01:基线训练:I01:60",
            "--task", "F01:基线评估:I01:15:F01-T01",
            "--task", "F02:对照训练:I02:45:F01-T01",
        )
        self.assertEqual(out["instances"], ["I01", "I02"])
        self.assertEqual(out["flows"], ["F01", "F02"])
        self.assertEqual(len(out["tasks"]), 3)
        # 单条 set 命令即可开工并记录进度
        self.command("set", "F01-T01", "--status", "running", "--progress", "40", "--note", "epoch 4")
        t1 = self.store.read()["tasks"][0]
        self.assertEqual(t1["progress"], 40)
        self.assertIsNotNone(t1["checked_at"])
        check_res = self.command("check")
        self.assertTrue(check_res["ok"])
        # Batch update & direct pending->done transition
        self.command("batch", "--set", "F01-T01:done", "--set", "F02-T01:running:25:ok")
        plan = self.store.read()
        self.assertEqual(plan["tasks"][0]["status"], "done")
        self.assertEqual(plan["tasks"][2]["status"], "running")
        self.assertEqual(plan["tasks"][2]["progress"], 25)
        self.command("set", "F01-T02", "--status", "done")
        self.assertEqual(self.store.read()["tasks"][1]["status"], "done")

    def test_task_fields_stay_minimal(self):
        self.setup_plan()
        self.assertEqual(set(self.store.read()["tasks"][0]), {
            "id", "flow", "name", "instance", "deps", "duration_minutes", "not_before", "status",
            "progress", "started_at", "finished_at", "checked_at", "progress_at", "note"})

    def test_instance_cannot_be_marked_off_while_task_runs(self):
        self.setup_plan()
        self.command("task", "set", "F01-T01", "--status", "running")
        self.command("instance", "set", "I01", "--status", "off", success=False)
        self.command("task", "set", "F01-T01", "--status", "failed")
        self.command("instance", "set", "I01", "--status", "off")

    def test_task_spec_placeholder_skips_middle_fields(self):
        self.setup_plan()
        self.command("batch", "--task", "F01:评估:-:30:F01-T01")
        task = self.store.read()["tasks"][1]
        self.assertIsNone(task["instance"])
        self.assertEqual(task["duration_minutes"], 30)
        self.assertEqual(task["deps"], ["F01-T01"])

    def test_batch_dep_updates_are_atomic_and_use_final_topology(self):
        self.setup_plan()
        self.command("task", "add", "--flow", "F01", "--name", "second", "--instance", "I01",
                     "--minutes", "10", "--deps", "F01-T01")
        self.command("task", "add", "--flow", "F01", "--name", "third", "--instance", "I01",
                     "--minutes", "10", "--deps", "F01-T02")
        revision = self.store.read()["revision"]
        # 目标是从 T01→T02→T03 反转为 T03→T02→T01。前两条单独执行都会先形成
        # T02↔T03 的中间环；整批只在最终状态上校验，因此一次就能落盘。
        result = self.command("batch",
                              "--dep", "F01-T02=F01-T03",
                              "--dep", "F01-T01=F01-T02",
                              "--dep", "F01-T03=")
        plan = self.store.read()
        self.assertEqual([task["deps"] for task in plan["tasks"]],
                         [["F01-T02"], ["F01-T03"], []])
        self.assertEqual(plan["revision"], revision + 1)
        self.assertEqual(result["item"]["deps"], [
            {"id": "F01-T02", "from": ["F01-T01"], "to": ["F01-T03"]},
            {"id": "F01-T01", "to": ["F01-T02"]},
            {"id": "F01-T03", "from": ["F01-T02"], "cleared": True},
        ])
        self.assertTrue(self.command("check")["ok"])

    def test_batch_dep_can_target_a_task_added_in_the_same_batch(self):
        self.setup_plan()
        result = self.command("batch", "--task", "F01:second:I01:10:-",
                              "--dep", "F01-T02=F01-T01")
        self.assertEqual(self.store.read()["tasks"][1]["deps"], ["F01-T01"])
        self.assertNotIn("inferred", result["item"])

    def test_batch_dep_rejects_running_task_without_writing(self):
        self.setup_plan()
        self.command("task", "add", "--flow", "F01", "--name", "next", "--instance", "I01",
                     "--minutes", "10", "--deps", "F01-T01")
        self.command("task", "set", "F01-T01", "--status", "running")
        before = self.store.path.read_bytes()
        result = self.command("batch", "--dep", "F01-T01=F01-T02", success=False)
        self.assertIn("运行中不能改", result.stderr)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_batch_dep_requires_task_equals_dependencies(self):
        self.setup_plan()
        before = self.store.path.read_bytes()
        result = run_cli(self.project, "batch", "--dep", "F01-T01", success=False)
        self.assertIn("任务ID=依赖列表", result.stderr)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_init_and_batch_receipts_expose_inferred_chains(self):
        result = self.command("init", "--name", "推断", "--instance", "A:a:a", "--flow", "基线",
                              "--task", "F01:第一步:I01:10", "--task", "F01:第二步:I01:10",
                              "--task", "F01:独立:I01:10:-")
        self.assertEqual(result["inferred"], [{"id": "F01-T02", "deps": ["F01-T01"]}])
        added = self.command("batch", "--task", "F01:第三步:I01:10")
        self.assertEqual(added["item"]["inferred"], [{"id": "F01-T04", "deps": ["F01-T03"]}])
        brief = run_cli(self.project, "batch", "--task", "F01:第四步:I01:10", "--brief")
        self.assertIn("自动串联（共 1 条）", brief.stdout)
        self.assertIn("F01-T05←F01-T04", brief.stdout)
        self.assertIn("独立任务用 - 显式留空", brief.stdout)

    def test_batch_dep_brief_shows_direction(self):
        self.setup_plan()
        self.command("task", "add", "--flow", "F01", "--name", "second", "--instance", "I01",
                     "--minutes", "10", "--deps", "F01-T01")
        result = run_cli(self.project, "batch", "--dep", "F01-T02=", "--brief")
        self.assertIn("依赖变更（共 1 条）", result.stdout)
        self.assertIn("F01-T02: F01-T01 → 空", result.stdout)
        self.assertIn("结构检查", result.stdout)

    def test_pending_unknown_duration_is_warning_and_strict_succeeds(self):
        self.setup_plan()
        self.command("task", "add", "--flow", "F01", "--name", "待估任务", "--instance", "local")
        result = self.command("check", "--strict")
        self.assertTrue(result["ok"])
        self.assertTrue(any("未设置预计耗时" in item for item in result["warnings"]))
        self.assertNotIn("issues", result)
        self.assertIsNone(self.store.read()["tasks"][1]["duration_minutes"])

    def test_many_pending_durations_are_summarized_in_check_and_write_brief(self):
        self.setup_plan()
        tasks = [f"F01:待估{index}:local:?" for index in range(15)]
        args = [value for task in tasks for value in ("--task", task)]
        receipt = run_cli(self.project, "batch", *args, "--brief").stdout
        self.assertEqual(receipt.count("未设置预计耗时"), 1)
        self.assertIn("15 个 pending", receipt)
        self.assertIn("已保存", receipt)
        report = self.command("check", "--strict")
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("15 个 pending", report["warnings"][0])
        text = run_cli(self.project, "show", "--brief").stdout
        self.assertEqual(text.count("未设置预计耗时"), 1)
        self.assertIn("15 个 pending", text)

    def test_missing_instance_remains_issue_and_strict_fails(self):
        self.setup_plan()
        self.command("task", "add", "--flow", "F01", "--name", "未分配", "--minutes", "5")
        result = self.command("check")
        self.assertFalse(result["ok"])
        self.assertTrue(any("未分配实例" in item for item in result["issues"]))
        strict = self.command("check", "--strict", success=False)
        self.assertFalse(json.loads(strict.stdout)["ok"])
        self.command("task", "set", "F01-T02", "--instance", "local")
        self.assertTrue(self.command("check", "--strict")["ok"])

    def test_init_and_batch_infer_dependencies_only_within_instance_and_flow(self):
        self.command("init", "--name", "并行", "--instance", "A:a:a", "--instance", "B:b:b",
                     "--flow", "基线", "--flow", "评估",
                     "--task", "F01:A:I01:?", "--task", "F01:B:I02:?",
                     "--task", "F01:C:I01:10", "--task", "F01:D:I02:10",
                     "--task", "F02:评估:local:5:F01-T03,F01-T04")
        self.assertEqual([t["deps"] for t in self.store.read()["tasks"]],
                         [[], [], ["F01-T01"], ["F01-T02"], ["F01-T03", "F01-T04"]])
        self.assertTrue(self.command("check", "--strict")["ok"])
        self.command("batch", "--task", "F01:E:I01:5", "--task", "F01:F:I02:5",
                     "--task", "F02:跨实例:I01:5:F01-T04")
        self.assertEqual([t["deps"] for t in self.store.read()["tasks"]][-3:],
                         [["F01-T03"], ["F01-T04"], ["F01-T04"]])

    def test_batch_explicit_empty_dependencies_override_inference(self):
        self.setup_plan()
        for suffix in ("", "-", "none"):
            self.command("batch", "--task", f"F01:独立:I01:5:{suffix}")
            self.assertEqual(self.store.read()["tasks"][-1]["deps"], [])
        self.command("batch", "--task", "F01:未分配:-:5", "--task", "F01:也未分配:-:5",
                     "--flow", "另一流程", "--task", "F02:新流程:I01:5",
                     "--task", "F02:本地:local:5", "--task", "F02:本地后续:local:5")
        self.assertEqual([t["deps"] for t in self.store.read()["tasks"]][-5:],
                         [[], [], [], [], ["F02-T02"]])

    def test_check_time_gap_is_warning_not_issue(self):
        self.setup_plan()
        self.command("task", "add", "--flow", "F01", "--name", "第二步", "--instance", "local",
                     "--minutes", "5", "--deps", "F01-T01")
        self.command("task", "set", "F01-T01", "--status", "done")
        self.command("task", "set", "F01-T02", "--status", "done")
        plan = self.store.read()
        plan["tasks"][0].update(started_at="2026-10-14T09:50:00+00:00", finished_at="2026-10-14T10:00:00+00:00")
        plan["tasks"][1].update(started_at="2026-10-14T10:30:00+00:00", finished_at="2026-10-14T10:35:00+00:00")
        self.store.write(plan)
        result = self.command("check")
        self.assertTrue(result["ok"])
        self.assertIn("时间空隙", result["warnings"][0])
        # compact() 会丢掉空列表，所以结构完好时输出里没有 issues 键
        self.assertNotIn("issues", result)


class DiskAndModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ops.Store(self.project)
        self.command("init", "--name", "实验")
        self.command("instance", "add", "--name", "A", "--autodl-id", "test-a", "--ssh-alias", "gpu-a")

    def command(self, *args, success=True):
        return run_json(self.project, *args, success=success)

    def instance(self):
        return self.store.read()["instances"][0]

    def test_disk_merges_per_mount(self):
        self.command("instance", "set", "I01", "--disk", "data:40/50,fs:5/200")
        item = self.instance()
        self.assertEqual(item["disk"]["data_used_gb"], 40)
        self.assertEqual(item["disk"]["data_total_gb"], 50)
        self.assertEqual(item["disk"]["fs_total_gb"], 200)
        self.assertIsNone(item["disk"]["system_used_gb"])
        self.assertIsNotNone(item["observed_at"])
        self.command("instance", "set", "I01", "--disk", "data:44/50")
        item = self.instance()
        self.assertEqual(item["disk"]["data_used_gb"], 44)
        self.assertEqual(item["disk"]["fs_used_gb"], 5, "没提到的盘不能被静默抹掉")

    def test_disk_none_clears_only_that_mount(self):
        self.command("instance", "set", "I01", "--disk", "sys:12/30,data:40/50")
        self.command("instance", "set", "I01", "--disk", "data:none")
        item = self.instance()
        self.assertIsNone(item["disk"]["data_used_gb"])
        self.assertIsNone(item["disk"]["data_total_gb"])
        self.assertEqual(item["disk"]["system_used_gb"], 12)

    def test_bad_disk_specs_are_rejected_without_writing(self):
        before = self.store.path.read_bytes()
        for spec in ("data:40", "gpu:1/2", "data:40/x", "data:50/40", "data:-1/50", ""):
            with self.subTest(spec=spec):
                self.command("instance", "set", "I01", "--disk", spec, success=False)
                self.assertEqual(before, self.store.path.read_bytes())

    def test_mode_flag_records_the_power_mode(self):
        """无卡/有卡由 agent 从平台侧核实后登记；模式是观测，会刷新观测时间。"""
        self.command("instance", "set", "I01", "--status", "on", "--mode", "no-gpu")
        item = self.instance()
        self.assertEqual(item["mode"], "no-gpu")
        self.assertIsNotNone(item["observed_at"], "开机模式是观测，要刷新观测时间")
        text = self.command("show").stdout
        self.assertIn("无卡准备", text)
        self.assertNotIn("¥", text)
        self.command("instance", "set", "I01", "--mode", "gpu")
        self.assertEqual(self.instance()["mode"], "gpu")
        self.command("instance", "set", "I01", "--mode", "none")
        self.assertIsNone(self.instance()["mode"], "核实不了就先记成未观测，不猜")
        self.command("instance", "set", "I01", "--mode", "no_gpu", success=False)
        for key in ("price", "no_gpu_price"):
            self.assertNotIn(key, self.instance(), "新实例不再生成时价字段")

    def test_financial_commands_and_flags_are_gone(self):
        """余额、时价与结算周期已删除：CLI 不得再接受对应命令或参数。"""
        before = self.store.path.read_bytes()
        cases = (("wallet", "set", "--assets", "1"),
                 ("instance", "set", "I01", "--price", "1.88"),
                 ("instance", "set", "I01", "--no-gpu-price", "0.1"),
                 ("serve", "--interval", "30"))
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = run_cli(self.project, *arguments, success=False)
                self.assertEqual(result.returncode, 2, "argparse 应把已删除的命令/参数判成用法错误")
                self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(before, self.store.path.read_bytes(), "被拒绝的写法不能顺手改计划")


class CheckWarningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ops.Store(self.project)
        self.command("init", "--name", "实验")
        self.command("instance", "add", "--name", "A", "--autodl-id", "test-a", "--ssh-alias", "gpu-a")
        self.command("flow", "add", "--name", "基线")
        self.command("task", "add", "--flow", "F01", "--name", "train", "--instance", "I01", "--minutes", "60")

    def command(self, *args, success=True):
        return run_json(self.project, *args, success=success)

    def test_no_observation_means_no_new_warning(self):
        result = self.command("check")
        self.assertNotIn("warnings", result)

    def test_disk_warnings_only(self):
        self.command("instance", "set", "I01", "--status", "on", "--disk", "data:48/50")
        result = self.command("check")
        text = " ".join(result["warnings"])
        self.assertIn("数据盘已用 48/50G", text)
        for gone in ("时价", "余额", "不足以", "结算"):
            self.assertNotIn(gone, text, "计费相关提醒已随功能删除")
        self.assertTrue(result["ok"], "这些只是提醒，不能把结构检查判成失败")

    def test_full_disk_warning_appears_in_show_text(self):
        self.command("instance", "set", "I01", "--disk", "sys:29/30,data:10/50")
        text = self.command("show").stdout
        self.assertIn("系统 29/30G!", text)
        self.assertIn("数据 10/50G", text)
        self.assertIn("盘 ", text)


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ops.Store(self.project)

    def setup_plan(self):
        return run_json(self.project, "init", "--name", "实验", "--instance", "GPU:id-a:gpu-a",
                        "--flow", "基线", "--task", "F01:训练:I01:60")

    def parse(self, *args):
        return ops.parser().parse_args(["-p", str(self.project), *args])

    def test_leaf_parsers_accept_brief(self):
        commands = [
            ["init", "--name", "实验"], ["batch", "--set", "F01-T01:done"],
            ["batch", "--dep", "F01-T01="], ["show"],
            ["set", "F01-T01", "--checked"], ["task", "set", "F01-T01", "--minutes", "?"],
            ["task", "add", "--flow", "F01", "--name", "任务"],
            ["instance", "add", "--name", "GPU", "--autodl-id", "id-a", "--ssh-alias", "gpu-a"],
            ["instance", "set", "I01", "--status", "on"], ["flow", "add", "--name", "流程"],
            ["flow", "set", "F01", "--name", "改名"],
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(self.parse(*command, "--brief").brief)

    def test_invalid_modes_fail_before_any_project_access(self):
        for command in (("show", "--brief", "--json"), ("check", "--brief")):
            result = run_cli(self.project / "不存在", *command, success=False)
            self.assertEqual(result.returncode, 2)
            self.assertFalse((self.project / "不存在").exists(), "非法组合不得读写项目或联网")

    def test_default_outputs_remain_machine_readable(self):
        result = self.setup_plan()
        self.assertEqual(set(result), {"revision", "name", "instances", "flows", "tasks"})
        update = run_json(self.project, "set", "F01-T01", "--status", "running", "--progress", "0")
        self.assertEqual(set(update), {"revision", "item"})
        self.assertEqual(update["item"]["progress"], 0)
        view = run_json(self.project, "show", "--json")
        self.assertEqual(view["tasks"][0]["deps"], [])
        self.assertEqual(view["tasks"][0]["note"], "")
        self.assertIsNone(view["tasks"][0]["finished_at"])
        self.assertEqual(ops.compact({"a": False, "b": 0, "c": None, "d": []}), {"a": False, "b": 0})
        self.assertIn("F01-T01", run_cli(self.project, "show").stdout)

    def test_empty_and_completed_plans_have_explicit_counts(self):
        plan = ops.new_plan("空计划")
        text = ops.brief_plan(plan, NOW)
        self.assertIn("done 0", text)
        self.assertNotIn("余额", text, "摘要不再包含余额结论")
        self.assertNotIn("¥", text)
        ops.add_flow(plan, "完成流程")
        task = ops.add_task(plan, "F01", "已结束", "local", 10)
        ops.set_task(plan, task["id"], {"status": "running"}, NOW)
        ops.set_task(plan, task["id"], {"status": "done"}, NOW)
        text = ops.brief_plan(plan, NOW)
        self.assertIn("done 1", text)
        self.assertIn("省略 1 个已完成/取消", text)
        self.assertNotIn("F01-T01", text, "历史仍在计划中，但摘要不展开已完成任务")

    def test_ready_candidates_deduplicate_gpu_but_not_local(self):
        plan = base()
        for name, resource, flow in (("GPU一", "I01", "F01"), ("GPU二", "I01", "F01"),
                                     ("本地一", "local", "F02"), ("本地二", "local", "F02")):
            ops.add_task(plan, flow, name, resource, 10)
        text = ops.brief_plan(plan, NOW)
        self.assertIn("依赖就绪 4 个任务", text)
        candidates = text.split("候选（共 3 条）:")[1].split("实例（")[0]
        for task_id in ("F01-T01", "F02-T01", "F02-T02"):
            self.assertIn(task_id, candidates)
        self.assertNotIn("F01-T02", candidates)
        self.assertIn("I01[unknown]", candidates, "依赖就绪不能冒充实例已在线")
        self.assertIn("仍需核实远端", text)

    def test_running_markers_use_forecast_without_guessing(self):
        plan = ops.new_plan("进度待核实")
        ops.add_flow(plan, "训练")
        task = ops.add_task(plan, "F01", "训练", "local", 60)
        ops.set_task(plan, task["id"], {"status": "running"}, NOW - timedelta(hours=2))
        text = ops.brief_plan(plan, NOW)
        self.assertIn("已超期", text)
        self.assertIn("未巡检", text)
        self.assertIn("完成 待估", text)
        ops.set_task(plan, task["id"], {"progress": 100}, NOW)
        text = ops.brief_plan(plan, NOW)
        self.assertIn("进度 100%，待核实", text)
        self.assertIn("running 1", text)
        self.assertIn("done 0", text)

    def test_failed_blocked_cancelled_do_not_ready_their_dependents(self):
        for status in ("failed", "blocked", "cancelled"):
            with self.subTest(status=status):
                plan = ops.new_plan("异常依赖")
                ops.add_flow(plan, "流程")
                first = ops.add_task(plan, "F01", "前置", "local", 10)
                ops.set_task(plan, first["id"], {"status": "running"}, NOW)
                ops.set_task(plan, first["id"], {"status": status}, NOW)
                ops.add_task(plan, "F01", "后续", "local", 10, [first["id"]])
                text = ops.brief_plan(plan, NOW)
                self.assertIn("依赖就绪 0 个任务", text)
                self.assertIn("依赖待完成/待估", text)
                self.assertIn("F01-T02", text)
                self.assertIn(f"{status} 1", text)

    def test_future_start_and_missing_fields_remain_visible(self):
        plan = ops.new_plan("待补数据")
        ops.add_flow(plan, "流程")
        ops.add_task(plan, "F01", "未分配", None, None)
        ops.add_task(plan, "F01", "定时任务", "local", 10,
                     not_before=ops.stamp(NOW + timedelta(hours=1)))
        text = ops.brief_plan(plan, NOW)
        self.assertIn("未分配", text)
        self.assertIn("未设置预计耗时", text)
        self.assertIn("最早开始", text)
        self.assertIn(ops.stamp(NOW + timedelta(hours=1)), text)

    def test_groups_bound_multiline_text_and_disclose_omissions(self):
        entries = [f"F01-T{i:02}: " + "长名称\n" * 100 for i in range(1, 8)]
        lines = ops.brief_group("结构问题", entries)
        self.assertIn("共 7 条", lines[0])
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[-1], "  …省略 4 条")
        self.assertTrue(all(len(line) <= ops.BRIEF_TEXT_LIMIT + 2 for line in lines))
        self.assertTrue(all("\n" not in line for line in lines))
        self.assertTrue(lines[1].endswith("…"))

    def test_structure_diagnostics_share_one_transaction_and_no_extra_read(self):
        self.setup_plan()
        previous = self.store.read()["revision"]
        seen = []
        check = ops.check_structure

        def inspect(plan):
            seen.append(copy.deepcopy(plan))
            return check(plan)

        args = self.parse("set", "F01-T01", "--minutes", "?", "--brief")
        with mock.patch.object(self.store, "read_document", wraps=self.store.read_document) as read, \
                mock.patch.object(ops, "check_structure", side_effect=inspect) as checks:
            result = ops.dispatch(args, self.store)
            self.assertEqual(read.call_count, 1, "仅 Store.change 内的一次读取")
            self.assertEqual(checks.call_count, 1)
        self.assertEqual(result["revision"], previous + 1)
        self.assertEqual(seen[0]["revision"], result["revision"])
        self.assertTrue(result["check"]["ok"])
        self.assertTrue(any("未设置预计耗时" in item for item in result["check"]["warnings"]))
        self.assertIsNone(self.store.read()["tasks"][0]["duration_minutes"])
        self.assertNotIn("check", self.store.read(), "诊断不写入账本 schema")
        self.assertIn("已保存", ops.brief_receipt(args, result))

    def test_show_brief_reads_once(self):
        self.setup_plan()
        args = self.parse("show", "--brief")
        with mock.patch.object(self.store, "read", wraps=self.store.read) as read, \
                mock.patch("builtins.print") as output:
            self.assertIsNone(ops.dispatch(args, self.store))
            self.assertEqual(read.call_count, 1)
        self.assertIn("完整计划用 show", output.call_args.args[0])

    def test_state_progress_checked_and_name_updates_do_not_check_structure(self):
        self.setup_plan()
        for command in (("set", "F01-T01", "--status", "running"),
                        ("set", "F01-T01", "--progress", "25"),
                        ("set", "F01-T01", "--checked"),
                        ("set", "F01-T01", "--name", "改名"),
                        ("batch", "--set", "F01-T01:running:30"),
                        ("instance", "set", "I01", "--disk", "data:1/2")):
            with self.subTest(command=command), mock.patch.object(ops, "check_structure") as checks:
                result = ops.dispatch(self.parse(*command, "--brief"), self.store)
                checks.assert_not_called()
                self.assertNotIn("check", result)

    def test_checked_refreshes_inspection_without_changing_progress_sample(self):
        self.setup_plan()
        run_cli(self.project, "set", "F01-T01", "--status", "running", "--progress", "25")
        before = self.store.read()["tasks"][0]
        later = ops.utcnow() + timedelta(seconds=5)
        with mock.patch.object(ops, "utcnow", return_value=later):
            ops.dispatch(self.parse("set", "F01-T01", "--checked", "--brief"), self.store)
        after = self.store.read()["tasks"][0]
        self.assertEqual(after["progress"], before["progress"])
        self.assertEqual(after["progress_at"], before["progress_at"])
        self.assertEqual(after["checked_at"], ops.stamp(later))
        self.assertFalse(ops.forecast(self.store.read(), later)["F01-T01"]["stale"])

    def test_brief_structure_issues_save_but_hard_failures_remain_atomic(self):
        self.setup_plan()
        result = run_cli(self.project, "set", "F01-T01", "--instance", "none", "--brief")
        self.assertEqual(result.returncode, 0)
        self.assertIn("结构问题", result.stdout)
        self.assertEqual(run_cli(self.project, "check", "--strict", success=False).returncode, 1)
        before = self.store.path.read_bytes()
        result = run_cli(self.project, "set", "F01-T01", "--deps", "F01-T01", "--brief", success=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_batch_checks_only_final_state_and_reports_limited_ids(self):
        self.setup_plan()
        args = self.parse("batch", "--task", "F01:后续:I01:10", "--set", "F01-T01:done",
                          "--set", "F01-T02:running:25", "--brief")
        with mock.patch.object(ops, "check_structure", wraps=ops.check_structure) as check:
            result = ops.dispatch(args, self.store)
            check.assert_called_once()
        self.assertEqual(result["item"]["updated"], ["F01-T01", "F01-T02"])
        self.assertEqual(self.store.read()["tasks"][1]["status"], "running")
        result = run_cli(self.project, "batch", *sum((["--task", f"F01:本地{i}:local:1"]
                                                    for i in range(6)), []), "--brief")
        self.assertIn("任务（共 6 条）", result.stdout)
        self.assertIn("省略 3 条", result.stdout)

    def test_hundred_tasks_brief_is_small_and_read_only(self):
        args = ["init", "--name", "百任务"]
        for i in range(1, 6):
            args.extend(["--instance", f"实例{i}:id-{i}:gpu-{i}", "--flow", f"流程{i}"])
        for i in range(1, 6):
            for j in range(1, 21):
                args.extend(["--task", f"F{i:02}:训练阶段{j}:I{i:02}:60"])
        run_cli(self.project, *args, "--brief")
        args = ["batch"]
        for i in range(1, 6):
            for j in range(1, 19):
                args.extend(["--set", f"F{i:02}-T{j:02}:done"])
            args.extend(["--set", f"F{i:02}-T19:running:50"])
        run_cli(self.project, *args, "--brief")
        before = self.store.path.read_bytes()
        full = run_cli(self.project, "show").stdout
        brief = run_cli(self.project, "show", "--brief").stdout
        self.assertLessEqual(len(brief), len(full) * 0.4)
        self.assertIn("done 90", brief)
        self.assertIn("running 5", brief)
        self.assertIn("运行中（共 5 条）", brief)
        self.assertIn("省略 2 条", brief)
        self.assertNotIn("F01-T01 ", brief)
        self.assertEqual(before, self.store.path.read_bytes(), "查看摘要不得写计划")

    def test_entry_safety_and_reference_links(self):
        entry = ROOT / "skills/trainops/SKILL.md"
        text = entry.read_text()
        for rule in ("也允许 agent 直接编辑", "check --strict", "释放实例前转存", "--brief",
                     "缩短假设到有效结果", "验收", "保留真实依赖", "超出资源授权"):
            self.assertIn(rule, text)
        for gone in ("Token", "时价", "余额", "结算"):
            self.assertNotIn(gone, text, "入口已删除计费/凭据登记要求")
        docs = [ROOT / "README.md", entry, *entry.parent.glob("references/*.md")]
        for doc in docs:
            for link in re.findall(r"\]\(([^)]+)\)", doc.read_text()):
                if "://" in link:
                    continue
                path, _, anchor = link.partition("#")
                target = (doc.parent / path).resolve() if path else doc
                self.assertTrue(target.is_file(), f"无效文档链接: {doc.name} -> {link}")
                if anchor:
                    headings = re.findall(r"^#+ (.+)$", target.read_text(), re.M)
                    anchors = {re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-") for heading in headings}
                    self.assertIn(anchor, anchors, f"无效锚点: {doc.name} -> {link}")


class HTTPTests(unittest.TestCase):
    """本机只读面板：只答 GET，写动词一律不支持，快照与页面都不再包含计费/凭据。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ops.Store(self.temp.name)
        self.store.create(ops.demo_plan(NOW))
        self.server = ops.server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def test_server_takes_only_the_local_store(self):
        signature = inspect.signature(ops.server)
        self.assertEqual(list(signature.parameters), ["store", "port"])
        self.assertEqual(signature.parameters["port"].default, 8765)
        for gone in ("Credential", "cycle", "background_monitor", "api_post",
                     "balance_amounts", "milli_to_yuan", "PRICE_PATH", "TOKEN_PATH"):
            self.assertFalse(hasattr(ops, gone), f"{gone} 已随计费功能删除")

    def test_page_and_snapshot(self):
        with OPEN(self.url) as response:
            page = response.read().decode()
            self.assertIn("TrainOps", page)
            self.assertIn("queue-viewport", page, "面板保留唯一的执行队列视图")
            self.assertIn("flow-index", page)
            self.assertIn("--inst-0", page, "面板为实例分配身份色")
            self.assertIn("var(--inst-color", page, "执行中节点取所属实例的颜色")
            self.assertNotIn("start-node", page, "面板不再显示开始节点")
            self.assertNotIn("diagram-switch", page, "执行队列是单视图，没有视图切换")
            # 流程总进度只留队列上方一处，任务矩形只显示自己的进度
            self.assertNotIn("progress-line", page)
            self.assertIn("textContent", page)
            self.assertNotIn("innerHTML", page)
            for gone in ("Token", "时价", "余额", "代金券", "结算", "/api/token", "/api/instance-price"):
                self.assertNotIn(gone, page, "面板已删除计费/凭据登记入口")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        with OPEN(self.url + "/api/plan") as response:
            plan = json.load(response)
            self.assertEqual(len(plan["tasks"]), 7)
            self.assertIn("estimate", plan["tasks"][0])
            # 流程结论、计数与任务进度都由服务端算好下发，面板只负责显示
            self.assertNotIn("bar", plan["tasks"][0])
            self.assertNotIn("graph", plan, "依赖图几何不再由服务端下发")
            self.assertTrue(all(sorted(task["view"]) == ["progress"] for task in plan["tasks"]))
            self.assertEqual(plan["tasks"][0]["view"], {"progress": 100}, "done 任务满格")
            self.assertNotIn("graph", plan["flows"][0], "流程只下发完成结论")
            self.assertEqual(sorted(plan["flows"][0]["view"]),
                             ["cancelled", "countable", "done", "finish", "known", "open",
                              "progress", "total"])
            self.assertEqual(plan["stats"]["counts"]["done"], 1)
            for gone in ("wallet", "monitor", "credentials"):
                self.assertNotIn(gone, plan, "面板快照不主动生成财务/凭据字段")
            for item in plan["instances"]:
                for gone in ("price", "no_gpu_price"):
                    self.assertNotIn(gone, item)

    def test_snapshot_filters_legacy_financial_fields_without_rewriting(self):
        def seed(plan):
            plan["wallet"] = {"assets": 1.0, "voucher": None, "observed_at": ops.stamp(NOW)}
            plan["monitor"] = {"interval_minutes": 30, "last_error": ""}
            plan["instances"][0].update(price=1.88, no_gpu_price=0.1)
        self.store.change(seed)
        before = self.store.path.read_bytes()
        with OPEN(self.url + "/api/plan") as response:
            plan = json.load(response)
        for key in ("wallet", "monitor", "credentials"):
            self.assertNotIn(key, plan)
        for item in plan["instances"]:
            for key in ("price", "no_gpu_price"):
                self.assertNotIn(key, item)
        self.assertEqual(before, self.store.path.read_bytes(), "只读面板不能重写旧文件")

    def test_api_plan_never_exposes_project_config_or_runs(self):
        """面板快照只该有计划；同一文件里的飞书配置与 Run 历史不能跟着漏出去。"""
        def seed(document):
            document["config"] = {"schema_version": 1, "project": "演示", "lark": {"base_token": "base-x"},
                                  "defaults": {}}
            document["experiments"] = {"schema_version": 1, "runs": {}}
        self.store.update(seed)
        with OPEN(self.url + "/api/plan") as response:
            plan = json.load(response)
        for key in ("config", "experiments", "lark"):
            self.assertNotIn(key, plan)

    def test_arbitrary_paths_and_financial_endpoints_are_gone(self):
        for path in ("/.trainops/plan.json", "/.agent/experiment.yaml", "/../README.md", "/favicon.ico",
                     "/api/instance-price", "/api/token"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                OPEN(self.url + path)
            self.assertEqual(error.exception.code, 404)

    def test_external_host_is_rejected(self):
        for path in ("/", "/api/plan"):
            with self.subTest(path=path):
                request = Request(self.url + path, headers={"Host": "external.example"})
                with self.assertRaises(HTTPError) as error:
                    OPEN(request)
                self.assertEqual(error.exception.code, 403)

    def test_writes_are_unsupported_and_never_touch_the_plan(self):
        """面板没有写处理器：写动词一律由 HTTP 层判为不支持，计划字节与 revision 都不动。"""
        before = self.store.path.read_bytes()
        revision = self.store.read()["revision"]
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for path in ("/", "/api/plan", "/api/instance-price", "/api/token"):
                with self.subTest(method=method, path=path), self.assertRaises(HTTPError) as error:
                    OPEN(Request(self.url + path, data=b"{}", method=method))
                self.assertEqual(error.exception.code, 501, "只读面板没有写处理器，应返回 501")
        with OPEN(self.url) as response:
            self.assertIn("TrainOps", response.read().decode())
        with OPEN(self.url + "/api/plan") as response:
            json.load(response)
        self.assertEqual(before, self.store.path.read_bytes(), "读请求不得写计划")
        self.assertEqual(revision, self.store.read()["revision"])
        self.assertFalse(any(thread.name == "trainops-monitor" for thread in threading.enumerate()),
                         "面板不再启动后台结算线程")

    def test_corruption_is_reported_not_hidden(self):
        self.store.path.write_text("oops")
        with self.assertRaises(HTTPError) as error:
            OPEN(self.url + "/api/plan")
        self.assertEqual(error.exception.code, 500)
        self.assertIn("损坏", json.load(error.exception)["error"])


if __name__ == "__main__":
    unittest.main()
