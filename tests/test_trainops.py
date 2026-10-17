import copy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
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
from urllib.error import HTTPError, URLError
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
    """跑一次 CLI 子进程，返回 CompletedProcess；success 用来声明期望的退出码是否为 0。

    env 只用来把接口地址指到本地假服务（TRAINOPS_API_BASE）：凭据只从面板登记的那份内存里取，
    子进程读不到任何 token，也就不必再"指开"开发机上的凭据文件。
    """
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
        plan["monitor"]["last_error"] = "测试结算失败"
        report = ops.check_structure(plan, NOW)
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["warnings"]), 3)
        self.assertIn("数据盘已用", report["warnings"][0])
        self.assertIn("自动结算失败", report["warnings"][1])
        self.assertIn("15 个 pending", report["warnings"][-1])
        text = ops.brief_plan(plan, NOW)
        self.assertIn("数据盘已用", text)
        self.assertIn("自动结算失败", text)
        self.assertEqual(text.count("未设置预计耗时"), 1)
        warnings_text = text.split("提醒（共 3 条）:", 1)[1].split("缺失信息", 1)[0]
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

    def test_monitor_block_is_validated_leniently(self):
        monitor = self.plan["monitor"]
        ops.validate(self.plan)  # 全新计划的 monitor 段必须合法
        for value in (0, 1441, "30", True):
            monitor["interval_minutes"] = value
            with self.subTest(interval=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        monitor["interval_minutes"] = ops.INTERVAL_LIMITS[1]
        for key in ("last_run", "last_ok", "expires_at"):
            monitor[key] = "2026-10-14 10:00"  # 不带时区
            with self.subTest(key=key), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
            monitor[key] = None
        monitor["token_ready"] = "yes"
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)
        monitor["token_ready"] = False
        # 结算快照：缺数据的键可以没有，但有的值必须可信（NaN 会让 json 落盘直接炸）
        monitor["estimate"] = {"computed_at": ops.stamp(NOW), "revision": 1}
        ops.validate(self.plan)
        for key in ("burn_rate", "hours_awake", "plan_gpu_hours", "plan_cost"):
            monitor["estimate"][key] = float("nan")
            with self.subTest(key=key), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
            del monitor["estimate"][key]
        monitor["estimate"]["covers_plan"] = "true"
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)
        monitor["estimate"]["covers_plan"] = None
        del monitor["estimate"]["computed_at"]
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)
        monitor["estimate"] = "oops"
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)

    def test_price_and_disk_bounds(self):
        item = self.plan["instances"][0]
        for value in (-1, 0, "1.88", float("nan"), True):
            item["price"] = value
            with self.subTest(price=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        item["price"] = None
        ops.validate(self.plan)
        for value in (-1, "40", float("inf")):
            item["disk"]["data_used_gb"] = value
            with self.subTest(disk=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        item["disk"]["data_used_gb"] = None
        item["disk"]["data_used_gb"], item["disk"]["data_total_gb"] = 60, 50
        with self.assertRaisesRegex(ops.PlanError, "大于总容量"):
            ops.validate(self.plan)

    def test_mode_and_no_gpu_price_bounds(self):
        item = self.plan["instances"][0]
        for value in ("no_gpu", "", 0, True):
            item["mode"] = value
            with self.subTest(mode=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        for value in ("no-gpu", "gpu", None):
            item["mode"] = value
            ops.validate(self.plan)
        for value in (-1, 0, "0.1", float("inf")):
            item["no_gpu_price"] = value
            with self.subTest(no_gpu_price=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        item["no_gpu_price"] = None
        ops.validate(self.plan)

    def test_plans_without_the_new_fields_still_read(self):
        """开机模式与无卡费率是后加的字段：老计划少了这两个键也要照常读，不逼人重建计划。"""
        for item in self.plan["instances"]:
            del item["mode"], item["no_gpu_price"]
        ops.validate(self.plan)
        self.plan["instances"][0].update(status="on", price=2.0)
        self.plan["instances"][1]["status"] = "off"
        self.plan["wallet"]["assets"] = 100.0
        self.assertEqual(ops.budget(self.plan, NOW)["burn_rate"], 2.0, "缺字段时按 GPU 时价，不是 0")
        self.assertIn("¥2/h", ops.instance_line(self.plan["instances"][0]))


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
        view = ops.snapshot(plan, NOW)
        self.assertEqual(len(view["tasks"]), 7)
        self.assertTrue(all("estimate" in t for t in view["tasks"]))
        self.assertTrue(all("view" in f for f in view["flows"]))
        self.assertIn("graph", view, "整份计划只下发一张主图")
        self.assertNotIn("graph", view["flows"][0], "流程只保留完成结论，不再各自下发子图")
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
        self.assertEqual(view["graph"]["edges"], [])
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


class LayoutTests(unittest.TestCase):
    """依赖层级与流程总进度由服务端统一下发。"""

    def setUp(self):
        self.plan = base()

    def layout(self):
        ops.validate(self.plan)
        return ops.layout(self.plan, NOW)

    def finish(self, flow, count, minutes=30, status="done"):
        """往 flow 里塞 count 个同时起止的任务：它们的时间区间完全重叠，正是旧公式算错的前提。"""
        for index in range(count):
            task = ops.add_task(self.plan, flow, f"{status}{index}", "I01", minutes)
            if status == "done":
                ops.set_task(self.plan, task["id"], {"status": "running"}, NOW - timedelta(minutes=minutes + 10))
                ops.set_task(self.plan, task["id"], {"status": "done"}, NOW - timedelta(minutes=10))
            else:
                ops.set_task(self.plan, task["id"], {"status": status}, NOW)
        return task

    def test_total_progress_counts_tasks_not_bar_widths(self):
        """5 个并行完成 + 2 个取消：旧公式把重叠宽度累加成 187% 再钳到 100%，现在按任务数算。"""
        self.finish("F01", 5, status="done")
        self.finish("F01", 2, status="cancelled")
        graph, views = self.layout()
        view = views["F01"]
        self.assertEqual((view["done"], view["countable"], view["cancelled"], view["total"]), (5, 5, 2, 7))
        self.assertEqual(view["progress"], 100, "取消的不再需要完成，不该把进度压在 100% 以下")
        self.assertEqual(len(graph["nodes"]), 7)

    def test_progress_denominator_is_the_uncancelled_tasks(self):
        self.finish("F01", 2, status="done")
        self.finish("F01", 1, status="cancelled")
        ops.add_task(self.plan, "F01", "待跑", "I01", 30)
        _, views = self.layout()
        self.assertEqual((views["F01"]["done"], views["F01"]["countable"]), (2, 3))
        self.assertEqual(views["F01"]["progress"], 67)

    def test_everything_cancelled_has_no_progress_at_all(self):
        self.finish("F02", 2, status="cancelled")
        _, views = self.layout()
        self.assertEqual(views["F02"]["countable"], 0)
        self.assertIsNone(views["F02"]["progress"], "没有需要完成的任务时不下结论，面板也就不画进度条")

    def test_chain_has_grid_rows_without_a_start_node(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 60)
        b = ops.add_task(self.plan, "F01", "B", "I01", 30, [a["id"]])
        c = ops.add_task(self.plan, "F01", "C", "I01", None, [b["id"]])
        ops.set_task(self.plan, c["id"], {"status": "cancelled"}, NOW)
        graph, _ = self.layout()
        self.assertEqual([(n["id"], n["row"], n["layer"], n["progress"]) for n in graph["nodes"]],
                         [(a["id"], 0, 0, 0), (b["id"], 0, 1, 0), (c["id"], 0, 2, 0)])
        self.assertEqual([(e["source"], e["target"], e["waypoints"]) for e in graph["edges"]],
                         [(a["id"], b["id"], []), (b["id"], c["id"], [])])
        self.assertEqual((graph["columns"], graph["rows"]), (3, 1))
        self.assertFalse(any(n["id"] == "START" for n in graph["nodes"]), "不显示开始节点")

    def test_parallel_fork_join_skip_layer_and_unordered_tasks(self):
        a = ops.add_task(self.plan, "F01", "root", "local", 10, [])
        b = ops.add_task(self.plan, "F01", "left", "local", 10, [a["id"]])
        c = ops.add_task(self.plan, "F01", "right", "local", 10, [a["id"]])
        d = ops.add_task(self.plan, "F01", "join", "local", 10, [b["id"], c["id"], a["id"]])
        e = ops.add_task(self.plan, "F01", "independent", "local", 10, [])
        graph, _ = self.layout()
        expected = graph
        self.plan["tasks"].reverse()
        shuffled, _ = self.layout()
        self.assertEqual(shuffled, expected)
        positions = {n["id"]: n for n in expected["nodes"]}
        self.assertEqual([positions[t["id"]]["layer"] for t in [a, b, c, d, e]], [0, 1, 1, 2, 0])
        self.assertEqual(len(expected["edges"]), 5)
        actual = {(edge["source"], edge["target"]) for edge in expected["edges"]}
        self.assertEqual(actual, {(dep, t["id"]) for t in self.plan["tasks"] for dep in t["deps"]})

    def test_cross_flow_dependencies_join_the_same_graph(self):
        a = ops.add_task(self.plan, "F02", "source", "local", 10, [])
        b = ops.add_task(self.plan, "F01", "target", "local", 10, [a["id"]])
        graph, _ = self.layout()
        positions = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(positions[a["id"]]["layer"], 0)
        self.assertEqual(positions[b["id"]]["layer"], 1, "跨流程依赖同样决定主图层级")
        edges = {(edge["source"], edge["target"]) for edge in graph["edges"]}
        self.assertEqual(edges, {(a["id"], b["id"])})

    def test_topological_order_not_task_identifier_order(self):
        a = ops.add_task(self.plan, "F01", "later", "local", 10, [])
        b = ops.add_task(self.plan, "F01", "earlier", "local", 10, [])
        a["deps"] = [b["id"]]
        graph, _ = self.layout()
        self.assertEqual([n["id"] for n in graph["nodes"]], [b["id"], a["id"]])
        self.assertEqual(ops.node_progress({"status": "done", "progress": 100}), 100)
        self.assertEqual(ops.node_progress({"status": "running", "progress": 18.5}), 19)

    def test_open_tasks_rank_above_finished_nodes(self):
        """未完成任务置顶；完成但仍被未完成任务直接依赖的节点居中；独立完成节点沉底。"""
        independent = ops.add_task(self.plan, "F01", "独立完成", "I01", 30)
        upstream = ops.add_task(self.plan, "F01", "完成的上游", "I01", 30)
        open_task = ops.add_task(self.plan, "F01", "待执行", "I01", 30)
        dropped = ops.add_task(self.plan, "F01", "取消的独立节点", "I01", 30)
        child = ops.add_task(self.plan, "F01", "未完成下游", "I01", 30, [upstream["id"]])
        for task in (independent, upstream):
            ops.set_task(self.plan, task["id"], {"status": "running"}, NOW - timedelta(minutes=40))
            ops.set_task(self.plan, task["id"], {"status": "done"}, NOW - timedelta(minutes=10))
        ops.set_task(self.plan, dropped["id"], {"status": "cancelled"}, NOW)
        graph, _ = self.layout()
        rows = {node["id"]: (node["layer"], node["row"]) for node in graph["nodes"]}
        self.assertEqual(rows[open_task["id"]], (0, 0), "未完成任务占最上面一行")
        self.assertEqual(rows[child["id"]], (1, 0))
        self.assertEqual(rows[upstream["id"]], (0, 1), "仍被未完成任务依赖的完成节点留在中间")
        self.assertGreater(rows[independent["id"]][1], rows[upstream["id"]][1],
                           "独立完成节点沉到下面")
        self.assertGreater(rows[dropped["id"]][1], rows[upstream["id"]][1],
                           "取消与完成一样不再占用上半部")

    def test_only_direct_prerequisites_stay_in_middle_band(self):
        """完成但仍直接喂给未完成任务的节点居中；更早的上游随已完成的链一起下沉。"""
        independent = ops.add_task(self.plan, "F01", "独立完成", "I01", 30)
        ancestor = ops.add_task(self.plan, "F01", "更早的上游", "I01", 30)
        middle = ops.add_task(self.plan, "F01", "直接前置", "I01", 30, [ancestor["id"]])
        child = ops.add_task(self.plan, "F01", "未完成下游", "I01", 30, [middle["id"]])
        for task, start, finish in ((independent, 120, 90), (ancestor, 120, 90), (middle, 80, 50)):
            ops.set_task(self.plan, task["id"], {"status": "running"}, NOW - timedelta(minutes=start))
            ops.set_task(self.plan, task["id"], {"status": "done"}, NOW - timedelta(minutes=finish))
        graph, _ = self.layout()
        rows = {node["id"]: (node["layer"], node["row"]) for node in graph["nodes"]}
        self.assertEqual(rows[middle["id"]], (1, 0), "直接前置仍留在中间带")
        self.assertEqual(rows[independent["id"]], (0, 0))
        self.assertGreater(rows[ancestor["id"]][1], rows[independent["id"]][1],
                           "仅作为传递上游的完成节点与独立节点一起沉底")

    def assert_no_crossings(self, graph):
        points = {node["id"]: (node["layer"], node["row"]) for node in graph["nodes"]}
        segments = []
        for edge in graph["edges"]:
            chain = ([points[edge["source"]]
                      ] + [(point["layer"], point["row"]) for point in edge["waypoints"]]
                     + [points[edge["target"]]])
            for source, target in zip(chain, chain[1:]):
                self.assertEqual(target[0], source[0] + 1, "跨层依赖必须拆成相邻层线段")
                segments.append((source, target))
        for index, (left, right) in enumerate(segments):
            for other_left, other_right in segments[index + 1:]:
                if left[0] != other_left[0]:
                    continue
                if left[1] in (other_left[1], other_right[1]) or right[1] in (other_left[1], other_right[1]):
                    continue
                self.assertGreaterEqual((left[1] - other_left[1]) * (right[1] - other_right[1]), 0,
                                        "相邻层线段不能交叉")

    def test_demo_graph_has_no_crossing_segments(self):
        plan = ops.demo_plan(NOW)
        graph, _ = ops.layout(plan, NOW)
        self.assert_no_crossings(graph)

    def test_fork_join_graph_can_escape_a_crossing_plateau(self):
        """重心排序会停在平台上；相邻交换侧移必须能把这种图排成零交叉。"""
        a = ops.add_task(self.plan, "F01", "a", "local", 10)
        b = ops.add_task(self.plan, "F01", "b", "local", 10)
        c = ops.add_task(self.plan, "F01", "c", "local", 10, [a["id"]])
        d = ops.add_task(self.plan, "F01", "d", "local", 10, [b["id"]])
        e = ops.add_task(self.plan, "F01", "e", "local", 10, [c["id"], d["id"]])
        f = ops.add_task(self.plan, "F01", "f", "local", 10, [a["id"], d["id"]])
        ops.add_task(self.plan, "F01", "g", "local", 10, [e["id"], f["id"]])
        graph, _ = self.layout()
        self.assert_no_crossings(graph)

    def test_finished_flow_reports_completion_instead_of_pending_estimate(self):
        """已结束的任务没有"预计完成时间"，不该把整个流程说成"完成待估"。"""
        task = ops.add_task(self.plan, "F01", "A", "I01", 30)
        ops.set_task(self.plan, task["id"], {"status": "running"}, NOW)
        ops.set_task(self.plan, task["id"], {"status": "done"}, NOW + timedelta(minutes=30))
        dropped = ops.add_task(self.plan, "F01", "B", "I01", 30, ["F01-T01"])
        ops.set_task(self.plan, dropped["id"], {"status": "cancelled"}, NOW + timedelta(minutes=40))
        _, views = self.layout()
        self.assertEqual(views["F01"]["open"], 0)
        self.assertTrue(views["F01"]["known"])
        self.assertEqual(views["F01"]["finish"], ops.stamp(NOW + timedelta(minutes=30)))


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
        # 时价、开机模式与三盘用量也是演示数据的一部分：不比对就看不到它们的漂移（时间戳除外）。
        instance_fields = ("id", "name", "status", "mode", "note", "price", "no_gpu_price", "disk")
        self.assertEqual([{f: i[f] for f in instance_fields} for i in committed["instances"]],
                         [{f: i[f] for f in instance_fields} for i in fresh["instances"]], hint)
        self.assertEqual({k: v for k, v in committed["wallet"].items() if k != "observed_at"},
                         {k: v for k, v in fresh["wallet"].items() if k != "observed_at"}, hint)
        self.assertEqual([f["name"] for f in committed["flows"]],
                         [f["name"] for f in fresh["flows"]], hint)
        self.assertEqual(committed["schema_version"], ops.SCHEMA_VERSION, hint)
        # 演示快照带一份自动结算记录，面板因此开箱就能看到结算徽标：比对静态字段，
        # 时间戳与指纹每次都不同，只比对结论的键集合（键必须和实时口径一致）。
        monitor_fields = ("interval_minutes", "expires_at", "last_error", "token_ready")
        self.assertEqual({f: committed["monitor"][f] for f in monitor_fields},
                         {f: fresh["monitor"][f] for f in monitor_fields}, hint)
        self.assertEqual(sorted(committed["monitor"]["estimate"]),
                         sorted(fresh["monitor"]["estimate"]), hint)


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


class DiskAndPriceTests(unittest.TestCase):
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

    def test_price_is_a_quote_not_an_observation(self):
        """时价现在只能从面板登记，但它依旧只是标价：登记它不刷新观测时间。"""
        self.store.change(lambda plan: ops.set_instance_price(plan, "I01", "price", ops.price_value("1.88")))
        item = self.instance()
        self.assertEqual(item["price"], 1.88)
        self.assertIsNone(item["observed_at"], "时价是标价，不该刷新观测时间")

    def test_price_bounds_reject_zero_and_junk(self):
        """0 和负数一律拒绝：它们会被读成"机器白跑"，而缺数据不能拿 0 顶替。"""
        for value in (0, -1, 0.0, 10001, "nan", "abc", [], {}, True):
            with self.subTest(price=value), self.assertRaises(ops.PlanError):
                ops.price_value(value)
        for cleared in (None, "none", "?", "-", "  "):
            with self.subTest(price=cleared):
                self.assertIsNone(ops.price_value(cleared))
        self.assertEqual(ops.price_value("1.88"), 1.88)

    def test_price_flag_is_gone_from_the_cli(self):
        """时价只有面板一个入口：CLI 参数必须消失，免得两条写路径各自漂移。"""
        result = run_cli(self.project, "instance", "set", "I01", "--price", "1.88", success=False)
        self.assertEqual(result.returncode, 2, "argparse 应该把已删除的 --price 判成用法错误")
        self.assertIn("--price", result.stderr)
        self.assertIsNone(self.instance()["price"], "被拒绝的写法不能顺手把时价写进去")

    def test_old_plan_gains_the_no_gpu_price_when_the_mode_is_recorded(self):
        """老计划没有无卡费率这个键，而读取时按平台默认值算：登记模式时要把它落实成字段，
        否则面板显示"费率未登记"、服务端却按 0.1 计算，两边各说各话。"""
        self.store.change(lambda plan: plan["instances"][0].pop("no_gpu_price", None))
        self.command("instance", "set", "I01", "--mode", "no-gpu")
        self.assertEqual(self.instance()["no_gpu_price"], ops.NO_GPU_PRICE)

    def test_mode_flag_records_the_power_mode(self):
        """无卡/有卡由 agent 从平台侧核实后登记，它和时价是两件事，互不覆盖。"""
        self.command("instance", "set", "I01", "--status", "on", "--mode", "no-gpu")
        item = self.instance()
        self.assertEqual(item["mode"], "no-gpu")
        self.assertEqual(item["no_gpu_price"], ops.NO_GPU_PRICE, "新实例默认带上平台的无卡费率")
        self.assertIsNotNone(item["observed_at"], "开机模式是观测，要刷新观测时间")
        self.assertIn("无卡 ¥0.1/h", self.command("show").stdout)
        self.store.change(lambda plan: ops.set_instance_price(plan, "I01", "price", 1.88))
        self.command("instance", "set", "I01", "--mode", "gpu")
        item = self.instance()
        self.assertEqual(item["mode"], "gpu")
        self.assertEqual(item["price"], 1.88, "切模式不该把另一个档位的价格冲掉")
        self.command("instance", "set", "I01", "--mode", "none")
        self.assertIsNone(self.instance()["mode"], "核实不了就先记成未观测，不猜")
        self.command("instance", "set", "I01", "--mode", "no_gpu", success=False)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.plan = base()
        for item in self.plan["instances"]:
            item["price"] = 2.0

    def test_hours_use_cash_only_and_skip_unknown_pieces(self):
        self.plan["wallet"].update(assets=100.0, voucher=500.0, observed_at=ops.stamp(NOW))
        view = ops.budget(self.plan, NOW)
        # 500 元代金券不该让 100 元现金变成 300 小时
        self.assertEqual(view["assets"], 100.0)
        self.assertEqual(view["voucher"], 500.0)
        self.assertNotIn("burn_rate", view, "没有开机实例就不该有烧钱速度")
        self.assertNotIn("hours_awake", view)

    def test_burn_rate_needs_every_awake_price(self):
        self.plan["wallet"]["assets"] = 100.0
        self.plan["instances"][0]["status"] = "on"
        self.plan["instances"][1].update(status="on", price=None)
        view = ops.budget(self.plan, NOW)
        self.assertNotIn("burn_rate", view, "缺一台时价时，烧钱速度必须是残缺的而不是缩小")
        self.plan["instances"][1]["price"] = 3.0
        view = ops.budget(self.plan, NOW)
        self.assertEqual(view["burn_rate"], 5.0)
        self.assertEqual(view["hours_awake"], 20.0)
        self.plan["instances"][0]["status"] = "off"
        self.assertEqual(ops.budget(self.plan, NOW)["burn_rate"], 3.0)

    def test_plan_cost_follows_lane_end_and_refuses_partial_sums(self):
        self.plan["wallet"]["assets"] = 100.0
        ops.add_task(self.plan, "F01", "训练", "I01", 120)
        ops.add_task(self.plan, "F02", "对照", "I02", 60)
        view = ops.budget(self.plan, NOW)
        # 两台实例各跑各的：2h×2 元 + 1h×2 元 = 6 元，而不是把耗时相加
        self.assertEqual(view["plan_gpu_hours"], 3.0)
        self.assertEqual(view["plan_cost"], 6.0)
        self.assertTrue(view["covers_plan"])
        unknown = ops.add_task(self.plan, "F01", "待估", "I01", None)
        view = ops.budget(self.plan, NOW)
        self.assertNotIn("plan_gpu_hours", view, "有任务完成时间待估时不能给出部分求和")
        self.assertNotIn("plan_cost", view)
        self.assertNotIn("covers_plan", view)
        self.plan["tasks"][2]["status"] = "cancelled"
        self.plan["tasks"][2].update(started_at=ops.stamp(NOW), finished_at=ops.stamp(NOW))
        self.assertIn("plan_cost", ops.budget(self.plan, NOW))

    def test_short_balance_is_reported_as_not_covering(self):
        self.plan["wallet"]["assets"] = 1.0
        ops.add_task(self.plan, "F01", "训练", "I01", 600)
        view = ops.budget(self.plan, NOW)
        self.assertEqual(view["plan_cost"], 20.0)
        self.assertFalse(view["covers_plan"])

    def test_no_gpu_instances_use_the_no_gpu_rate(self):
        """无卡开机不等于免费：按无卡费率算，既不退回 GPU 时价，也不当成 0。"""
        self.plan["wallet"]["assets"] = 100.0
        self.plan["instances"][0].update(status="on", mode="no-gpu")
        self.plan["instances"][1]["status"] = "off"
        view = ops.budget(self.plan, NOW)
        self.assertEqual(view["burn_rate"], ops.NO_GPU_PRICE)
        self.assertEqual(view["hours_awake"], round(100.0 / ops.NO_GPU_PRICE, 1))
        self.plan["instances"][1].update(status="on", mode="no-gpu")
        self.assertEqual(ops.budget(self.plan, NOW)["burn_rate"], 0.2, "两台无卡各自计费")
        # 无卡费率被清空就少给结论：不退回 GPU 时价，也不写 0
        self.plan["instances"][0]["no_gpu_price"] = None
        self.assertNotIn("burn_rate", ops.budget(self.plan, NOW))
        # 开机模式没登记过（老计划）仍按 GPU 时价：宁可高估花费，也不让人以为还能跑很久
        self.plan["instances"][0].update(mode=None, no_gpu_price=0.1)
        self.assertEqual(ops.budget(self.plan, NOW)["burn_rate"], 2.1)

    def test_plan_cost_still_uses_the_gpu_price(self):
        """待跑任务将来是在有卡模式下跑的，计划花费不能拿无卡费率压低。"""
        self.plan["wallet"]["assets"] = 100.0
        ops.add_task(self.plan, "F01", "训练", "I01", 120)
        self.plan["instances"][0].update(status="on", mode="no-gpu")
        self.plan["instances"][1]["status"] = "off"
        view = ops.budget(self.plan, NOW)
        self.assertEqual(view["burn_rate"], ops.NO_GPU_PRICE)
        self.assertEqual(view["plan_cost"], 4.0, "2 小时 × GPU 时价 2 元，而不是无卡费率")


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

    def test_disk_price_and_coverage_warnings(self):
        self.command("instance", "set", "I01", "--status", "on", "--disk", "data:48/50")
        self.command("wallet", "set", "--assets", "100")
        result = self.command("check")
        text = " ".join(result["warnings"])
        self.assertIn("数据盘已用 48/50G", text)
        self.assertIn("未登记时价", text)
        self.assertNotIn("不足以", text, "缺时价时算不出计划花费，不能凭空说余额不够")
        self.assertIn("面板", text, "提示要告诉用户去哪儿登记时价")
        self.store.change(lambda plan: ops.set_instance_price(plan, "I01", "price", 2.0))
        self.command("task", "set", "F01-T01", "--minutes", "6000")
        result = self.command("check")
        text = " ".join(result["warnings"])
        self.assertIn("不足以覆盖", text)
        self.assertNotIn("系统盘", " ".join(result["warnings"]))
        self.assertTrue(result["ok"], "这些只是提醒，不能把结构检查判成失败")

    def test_no_gpu_instance_is_not_asked_for_a_gpu_price(self):
        """无卡开机按无卡费率计费，不该追着要 GPU 时价。"""
        self.command("instance", "set", "I01", "--status", "on", "--mode", "no-gpu")
        self.command("wallet", "set", "--assets", "100")
        text = " ".join(self.command("check")["warnings"])
        self.assertNotIn("未登记时价", text)
        self.assertIn("无卡 ¥0.1/h", self.command("show").stdout)

    def test_full_disk_warning_appears_in_show_text(self):
        self.command("instance", "set", "I01", "--disk", "sys:29/30,data:10/50")
        text = self.command("show").stdout
        self.assertIn("系统 29/30G!", text)
        self.assertIn("数据 10/50G", text)
        self.assertIn("盘 ", text)


class WalletAPITests(unittest.TestCase):
    """余额接口：凭据只进请求头，各类失败被翻译成可读文案，账本除 monitor 段外一个字节都不动。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ops.Store(self.project)
        self.store.create(ops.demo_plan(NOW))
        self.requests = []
        tests = self

        class Handler(BaseHTTPRequestHandler):
            status = 200
            body = json.dumps({"code": "Success", "data": {"assets": 128500, "voucher_balance": 20000}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                tests.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "body": self.rfile.read(length)})
                payload = self.body.encode()
                self.send_response(self.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.handler = Handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def settle(self, token="secret-token-value"):
        """结算只从面板登记的那份内存凭据取 token；测试直接传进来，不经过任何文件或环境变量。"""
        with mock.patch.dict(os.environ, {"TRAINOPS_API_BASE": f"http://127.0.0.1:{self.server.server_port}"}):
            return ops.cycle(self.store, token=token)

    def test_balance_request_shape_and_no_token_leak(self):
        result = self.settle()
        self.assertEqual(self.requests[0]["path"], "/api/v1/dev/wallet/balance")
        self.assertEqual(self.requests[0]["auth"], "secret-token-value", "必须用原始 token，不能加 Bearer 前缀")
        self.assertEqual(self.requests[0]["body"], b"", "只读接口不该带请求体")
        wallet = self.store.read()["wallet"]
        self.assertEqual((wallet["assets"], wallet["voucher"]), (128.5, 20.0))
        self.assertIsNotNone(wallet["observed_at"])
        for leak in (result, self.store.path.read_text(encoding="utf-8")):
            self.assertNotIn("secret-token-value", json.dumps(leak, ensure_ascii=False, default=str))

    def test_payload_failures_are_reported_and_leave_the_ledger_alone(self):
        cases = ((401, json.dumps({"code": "Fail", "msg": "token 无效"}), "HTTP 401"),
                 (200, json.dumps({"code": "Fail", "msg": "余额查询失败", "request_id": "req-1"}), "余额查询失败"),
                 (200, "not json at all", "JSON"),
                 (200, json.dumps({"code": "Success", "data": {}}), "assets"),
                 (200, json.dumps({"code": "Success", "data": {"assets": 12.345}}), "整数"))
        before = self.store.read()
        for status, body, expected in cases:
            with self.subTest(status=status, body=body[:40]):
                self.handler.status, self.handler.body = status, body
                result = self.settle()
                self.assertIn(expected, result["monitor"]["last_error"])
        after = self.store.read()
        for key in ("name", "instances", "flows", "tasks", "wallet"):
            self.assertEqual(after[key], before[key], f"取数失败只能动 monitor 段，不能动 {key}")
        self.assertNotIn("secret-token-value", json.dumps(after, ensure_ascii=False))

    def test_connection_errors_are_translated_not_raised_raw(self):
        def boom(*args, **kwargs):
            raise URLError(ConnectionRefusedError(111, "Connection refused"))

        original, ops.opener = ops.opener, boom
        try:
            with self.assertRaisesRegex(ops.PlanError, "无法连接"):
                ops.api_post("/api/v1/dev/wallet/balance", "secret-token-value")
        finally:
            ops.opener = original

    def test_unreachable_api_does_not_block_local_edits(self):
        # 接口不可达时：失败被记进计划，本地命令照常可用
        with mock.patch.dict(os.environ, {"TRAINOPS_API_BASE": "http://127.0.0.1:9"}):
            result = ops.cycle(self.store, token="secret-token-value")
        self.assertTrue(result["monitor"]["last_error"], "连不上接口必须记进 last_error")
        self.assertNotIn("secret-token-value", json.dumps(result["monitor"], ensure_ascii=False))
        run_cli(self.project, "wallet", "set", "--assets", "12.5", "--voucher", "3")
        run_cli(self.project, "flow", "set", "F01", "--name", "改名")
        self.assertIn("改名", run_cli(self.project, "show").stdout)

    def test_wallet_set_manual_path(self):
        run_cli(self.project, "wallet", "set", "--assets", "12.5", "--voucher", "3")
        wallet = self.store.read()["wallet"]
        self.assertEqual((wallet["assets"], wallet["voucher"]), (12.5, 3))
        self.assertIsNotNone(wallet["observed_at"])
        # 不带任何字段时拒绝：既不改数据，也不假装成功
        run_cli(self.project, "wallet", "set", success=False)
        self.assertEqual(self.store.read()["wallet"]["voucher"], 3)
        run_cli(self.project, "wallet", "set", "--assets", "none")
        self.assertIsNone(self.store.read()["wallet"]["assets"])
        self.assertEqual(self.store.read()["wallet"]["voucher"], 3)
        run_cli(self.project, "wallet", "set", "--assets", "1", "--voucher", "none")
        self.assertIsNone(self.store.read()["wallet"]["voucher"])


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
            ["wallet", "set", "--assets", "0"],
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
        self.assertIn("余额未登记", text)
        self.assertNotIn("¥0.00", text)
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

    def test_budget_missing_data_and_voucher_do_not_become_cash(self):
        plan = ops.demo_plan(NOW)
        plan["wallet"]["assets"] = None
        plan["instances"][0]["price"] = None
        plan["monitor"]["estimate"] = None
        text = ops.brief_plan(plan, NOW)
        self.assertIn("余额未登记", text)
        self.assertIn("未登记时价", text)
        self.assertIn("代金券 ¥20.00（不计入可用时长）", text)
        self.assertNotIn("余额够", text)
        self.assertNotIn("¥0.00", text)
        self.assertNotIn("可用时长（同结算口径）:", text)

    def test_fresh_changed_settlement_does_not_mix_live_plan_cost(self):
        plan = ops.demo_plan(NOW)
        old_cost = plan["monitor"]["estimate"]["plan_cost"]
        plan["instances"][0]["price"] = 999
        new_cost = ops.budget(plan, NOW)["plan_cost"]
        self.assertNotEqual(old_cost, new_cost)
        text = ops.brief_plan(plan, NOW)
        self.assertIn("结算后计划有变更", text)
        self.assertIn(f"¥{old_cost:.2f}", text)
        self.assertNotIn(f"¥{new_cost:.2f}", text)
        self.assertNotIn("实时结论:", text)
        plan["monitor"]["estimate"]["computed_at"] = ops.stamp(NOW - timedelta(minutes=61))
        text = ops.brief_plan(plan, NOW)
        self.assertIn("已过期", text)
        self.assertIn(f"¥{new_cost:.2f}", text)
        self.assertEqual(text.count("实时结论:"), 1)

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

    def test_wallet_brief_preserves_zero_and_clear(self):
        self.setup_plan()
        result = run_cli(self.project, "wallet", "set", "--assets", "0", "--voucher", "10", "--brief")
        self.assertIn("余额: ¥0.00", result.stdout, "真实零余额必须显示，与未知不同")
        self.assertIn("不计入可用时长", result.stdout)
        result = run_cli(self.project, "wallet", "set", "--assets", "none", "--brief")
        self.assertIn("余额: 未登记", result.stdout)

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
        for rule in ("禁止直接 Edit/Write JSON", "用户授权", "释放前先转存", "Token", "--checked", "--brief",
                     "自主优化调度", "验收标准", "硬约束", "未授权费用"):
            self.assertIn(rule, text)
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


class MonitorTests(unittest.TestCase):
    """结算（cycle）：联网在文件锁之外，失败只记录不退出，结论与结算时间写回计划。

    cycle 是面板进程与测试共用的那一个函数：Token 由调用方从面板登记的内存凭据里现取，
    测试直接传参——凭据没有文件、也没有环境变量，这里不需要任何"指开"。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ops.Store(self.project)
        # 用真实当前时刻生成演示计划：cycle() 按真实时钟判断"跑没跑完"，固定时刻的计划会整片逾期
        self.store.create(ops.demo_plan())
        self.requests = []
        self.arrived = None
        self.slow = None
        tests = self

        class Handler(BaseHTTPRequestHandler):
            status = 200
            body = json.dumps({"code": "Success", "data": {"assets": 52000, "voucher_balance": 8000}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                tests.requests.append({"auth": self.headers.get("Authorization"), "body": self.rfile.read(length)})
                if tests.slow is not None:
                    tests.arrived.set()
                    tests.slow.wait(10)  # 卡住联网，用来验证结算没有攥着文件锁
                payload = self.body.encode()
                self.send_response(self.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.token = "settle-token"

    def run_json(self, *args, success=True):
        return run_json(self.project, *args, success=success)

    def settle(self, **kwargs):
        """在测试进程里跑一轮结算；接口地址由本用例的假服务说了算。"""
        with mock.patch.dict(os.environ, {"TRAINOPS_API_BASE": self.api_base()}):
            return ops.cycle(self.store, token=self.token, **kwargs)

    def api_base(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def monitor(self):
        return self.store.read()["monitor"]

    def test_once_settles_balance_and_writes_conclusion(self):
        result = self.settle(interval=15)
        self.assertEqual(result["monitor"]["interval_minutes"], 15)
        self.assertEqual(result["wallet"]["assets"], 52.0)
        self.assertEqual(self.requests[0]["auth"], "settle-token", "必须用原始 token，不能加 Bearer 前缀")
        monitor = self.monitor()
        self.assertEqual(monitor["last_error"], "")
        self.assertTrue(monitor["token_ready"])
        self.assertEqual(monitor["last_ok"], monitor["last_run"])
        estimate = monitor["estimate"]
        self.assertEqual(estimate["plan_hash"], ops.plan_fingerprint(self.store.read()))
        for key in ("burn_rate", "hours_awake", "plan_gpu_hours", "plan_cost", "covers_plan"):
            self.assertIn(key, estimate)
        # show 与面板读的就是这份结论，且明确标出结算时间
        self.assertFalse(result["settlement"]["live"])
        self.assertIn("自动结算于", result["settlement"]["text"])
        shown = self.run_json("show")
        self.assertIn("自动结算于", shown.stdout)
        # token 绝不外泄：结算回执、stdout、计划文件里都不许出现
        for leak in (result, shown, self.store.path.read_text(encoding="utf-8")):
            self.assertNotIn("settle-token", json.dumps(leak, ensure_ascii=False, default=str))

    def test_failure_is_recorded_without_killing_the_schedule(self):
        self.settle()
        good = self.store.read()
        self.handler_status(401, json.dumps({"code": "Fail", "msg": "token 无效"}))
        result = self.settle()
        self.assertIn("HTTP 401", result["monitor"]["last_error"])
        after = self.store.read()
        monitor = after["monitor"]
        self.assertEqual(monitor["last_ok"], good["monitor"]["last_ok"], "失败不该刷新成功时间")
        self.assertEqual(monitor["estimate"], good["monitor"]["estimate"], "失败必须保留上一份结论")
        # 失败那一轮确实落盘了（revision 前进），只是没被记成成功；last_run 秒级精度，不去比它
        self.assertGreater(after["revision"], good["revision"])
        for key in ("instances", "flows", "tasks", "wallet"):
            self.assertEqual(after[key], good[key], f"失败只能动 monitor 段，不能动 {key}")
        # 结论仍然可读，失败被标出来；下一轮成功即恢复正常
        self.assertIn("上次结算失败", self.run_json("show").stdout)
        self.handler_status(200, json.dumps({"code": "Success", "data": {"assets": 52000, "voucher_balance": 8000}}))
        self.assertEqual(self.settle()["monitor"]["last_error"], "")

    def handler_status(self, status, body):
        handler = self.server.RequestHandlerClass
        handler.status, handler.body = status, body

    def test_unreachable_api_keeps_process_alive_and_plan_intact(self):
        with mock.patch.dict(os.environ, {"TRAINOPS_API_BASE": "http://127.0.0.1:9"}):
            ops.cycle(self.store, token=self.token)
        # 具体文案随环境而异（直连是"无法连接"，经代理是 HTTP 5xx），只要求失败被记下
        self.assertTrue(self.monitor()["last_error"], "连不上接口必须记进 last_error")
        self.assertEqual(self.store.read()["wallet"]["assets"], 128.5, "连不上时钱包保持原值")
        # 本地命令照常可用，计划结构没被破坏
        run_cli(self.project, "flow", "set", "F01", "--name", "改名")
        self.assertIn("改名", run_cli(self.project, "show").stdout)

    def test_missing_token_is_not_a_failure(self):
        result = ops.cycle(self.store, token=None)
        self.assertEqual(self.requests, [], "没有 token 就不该发请求")
        # 没登记 Token 不是故障：compact() 会省掉空的 last_error，这里按"没有错误"读
        self.assertEqual(result["monitor"].get("last_error", ""), "")
        self.assertFalse(result["monitor"]["token_ready"])
        self.assertEqual(self.monitor()["last_error"], "", "计划里也不该留下错误")
        self.assertIn("未配置 Token", result["settlement"]["text"])
        # 结论仍然算得出来（用计划里已有的余额），也不会一直报错
        self.assertIsNotNone(result["monitor"]["estimate"])

    def test_stale_settlement_falls_back_to_live_values(self):
        self.settle(interval=1)
        plan = self.store.read()
        plan["monitor"]["estimate"]["computed_at"] = ops.stamp(NOW - timedelta(minutes=10))
        view = ops.settlement(plan, NOW)
        self.assertTrue(view["live"], "超过 2 个周期就回退实时值")
        self.assertIn("已过期", view["text"])
        # 计划改过之后必须标明这份结论不再代表当前计划
        plan["monitor"]["estimate"]["computed_at"] = ops.stamp(NOW)
        self.assertFalse(ops.settlement(plan, NOW)["changed"])
        ops.add_task(plan, "F01", "新任务", "I01", 10)
        view = ops.settlement(plan, NOW)
        self.assertTrue(view["changed"])
        self.assertIn("结算后计划有变更", view["text"])
        # 结算自己更新 last_run 不算"计划有变更"，否则会一直误报
        plan["monitor"]["last_run"] = ops.stamp(NOW + timedelta(minutes=1))
        self.assertTrue(ops.settlement(plan, NOW)["changed"], "任务改动才是真改动")
        clean = self.store.read()
        clean["monitor"]["last_run"] = ops.stamp(NOW + timedelta(minutes=1))
        self.assertFalse(ops.settlement(clean, NOW)["changed"])

    def test_interval_bounds_are_enforced_on_serve(self):
        """结算周期现在只由面板进程的 --interval 决定，越界要在解析阶段就挡下。"""
        for value in ("0", "1441", "abc", "-5"):
            with self.subTest(interval=value), self.assertRaises(SystemExit):
                ops.parser().parse_args(["-p", str(self.project), "serve", "--interval", value])

    def test_cycle_does_not_hold_the_lock_while_online(self):
        """联网在锁外：慢接口期间本地写照常完成，两边的改动都留得住。"""
        report = {}
        self.arrived = threading.Event()  # 假接口收到请求
        self.slow = threading.Event()     # 由本用例放行，让接口一直卡着
        thread = threading.Thread(target=lambda: report.update(
            ops.cycle(self.store, interval=60, token=self.token)), daemon=True)
        with mock.patch.dict(os.environ, {"TRAINOPS_API_BASE": self.api_base()}):
            thread.start()
            self.assertTrue(self.arrived.wait(5), "假接口没被调用")
            run_cli(self.project, "flow", "set", "F01", "--name", "并发改名")  # 联网还没返回就写入
            self.slow.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        final = self.store.read()
        self.assertEqual(final["flows"][0]["name"], "并发改名", "结算不能覆盖并发写")
        self.assertEqual(final["monitor"]["interval_minutes"], 60)
        self.assertEqual(final["wallet"]["assets"], 52.0, "结算自己的结果也要留住")
        self.assertEqual(report["monitor"]["last_error"], "", report)


class MoneyTests(unittest.TestCase):
    def test_milli_to_yuan_rejects_unit_drift(self):
        self.assertEqual(ops.milli_to_yuan(128500, "余额"), 128.5)
        self.assertEqual(ops.milli_to_yuan(0, "余额"), 0)
        for value in (12.5, -1, float("nan"), True, "100", None):
            with self.subTest(value=value), self.assertRaises(ops.PlanError):
                ops.milli_to_yuan(value, "余额")

    def test_balance_amounts_requires_assets(self):
        self.assertEqual(ops.balance_amounts({"data": {"assets": 1000, "voucher_balance": None}}),
                         {"assets": 1.0, "voucher": None})
        for payload in ({}, {"data": None}, {"data": {}}, {"data": {"voucher_balance": 1}}):
            with self.subTest(payload=payload), self.assertRaises(ops.PlanError):
                ops.balance_amounts(payload)

    def test_redirect_with_credentials_is_refused(self):
        with self.assertRaises(ops.PlanError):
            ops.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example/")


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ops.Store(self.temp.name)
        self.store.create(ops.demo_plan(NOW))
        self.credential = ops.Credential()
        self.server = ops.server(self.store, self.credential, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def test_page_and_snapshot(self):
        with OPEN(self.url) as response:
            page = response.read().decode()
            self.assertIn("TrainOps", page)
            self.assertIn("canvas-viewport", page)
            self.assertIn("flow-index", page)
            self.assertIn("--inst-0", page, "面板为实例分配身份色")
            self.assertIn("var(--inst-color", page, "执行中节点取所属实例的颜色")
            self.assertNotIn("start-node", page, "面板不再显示开始节点")
            # 流程总进度只留主图上方一处，任务矩形只显示自己的进度
            self.assertNotIn("progress-line", page)
            self.assertIn("textContent", page)
            self.assertNotIn("innerHTML", page)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        with OPEN(self.url + "/api/plan") as response:
            plan = json.load(response)
            self.assertEqual(len(plan["tasks"]), 7)
            self.assertIn("estimate", plan["tasks"][0])
            # 几何、流程结论、计数都由服务端算好下发，面板只负责打点
            self.assertNotIn("bar", plan["tasks"][0])
            self.assertEqual(sorted(plan["graph"]),
                             ["columns", "edges", "nodes", "rows"])
            self.assertEqual(len(plan["graph"]["nodes"]), 7)
            self.assertFalse(any(node["id"] == "START" for node in plan["graph"]["nodes"]),
                             "主图不再有开始节点")
            self.assertTrue(all("waypoints" in edge for edge in plan["graph"]["edges"]),
                            "跨层依赖必须带中间路点")
            self.assertEqual((plan["graph"]["columns"], plan["graph"]["rows"]), (5, 2))
            self.assertNotIn("graph", plan["flows"][0], "流程不再各自下发子图")
            self.assertEqual(sorted(plan["flows"][0]["view"]),
                             ["cancelled", "countable", "done", "finish", "known", "open",
                              "progress", "total"])
            self.assertEqual(plan["stats"]["counts"]["done"], 1)
            self.assertEqual(plan["credentials"], {"configured": False, "set_at": None},
                             "面板刚起来时没有凭据，状态要照实说")

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

    def test_arbitrary_paths_and_writes_rejected(self):
        for path in ("/.trainops/plan.json", "/.agent/experiment.yaml", "/../README.md", "/favicon.ico"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                OPEN(self.url + path)
            self.assertEqual(error.exception.code, 404)
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method), self.assertRaises(HTTPError) as error:
                OPEN(Request(self.url + "/api/plan", data=b"{}", method=method))
            self.assertEqual(error.exception.code, 405)

    def test_external_host_is_rejected(self):
        request = Request(self.url + "/api/plan", headers={"Host": "external.example"})
        with self.assertRaises(HTTPError) as error:
            OPEN(request)
        self.assertEqual(error.exception.code, 403)

    def post(self, payload, ctype="application/json", origin=None, headers=None, method="POST",
             path="/api/instance-price"):
        """登记时价的请求，默认是面板该发出的那个形状。"""
        head = {"Content-Type": ctype, **(headers or {})}
        if origin:
            head["Origin"] = origin
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return OPEN(Request(self.url + path, data=data, headers=head, method=method))

    def test_price_is_registered_through_the_store(self):
        """面板登记时价：写进去的必须是"标价"，不是一次对机器的观测。"""
        before = self.store.read()
        with self.post({"id": "I01", "price": 2.5}) as response:
            self.assertEqual(response.status, 200)
            plan = json.load(response)
        self.assertEqual(plan["instances"][0]["price"], 2.5)
        self.assertEqual(plan["revision"], before["revision"] + 1, "登记时价也要走同一条落盘路径")
        written = self.store.read()["instances"][0]
        self.assertEqual(written["price"], 2.5)
        self.assertEqual(written["observed_at"], before["instances"][0]["observed_at"],
                         "时价是标价，不该刷新观测时间")
        with self.post({"id": "I01", "price": None}) as response:
            self.assertEqual(response.status, 200)
        self.assertIsNone(self.store.read()["instances"][0]["price"], "清空要真的清空，不是写 0")
        with self.post({"id": "I01", "price": 1.5}, origin=self.url) as response:
            self.assertEqual(response.status, 200, "面板自己发出的同源请求要放行")

    def test_bad_price_requests_never_touch_the_file(self):
        before = self.store.path.read_bytes()
        for payload in ({"id": "I01", "price": 0}, {"id": "I01", "price": -1}, {"id": "I01", "price": "nan"},
                        {"id": "I01", "price": "abc"}, {"id": "I09", "price": 1}, {"id": "I01"},
                        {"price": 1}, {"id": "I01", "price": 1, "note": "顺手改点别的"}, [], "I01",
                        {"id": "I01", "price": 1, "no_gpu_price": 1}, {"id": "I01", "no_gpu_price": 0}):
            with self.subTest(payload=payload), self.assertRaises(HTTPError) as error:
                self.post(payload)
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(before, self.store.path.read_bytes(), "被拒绝的登记不能留下半个文件")

    def test_no_gpu_rate_is_a_field_of_its_own(self):
        """无卡费率与 GPU 时价各写各的：切回有卡时 GPU 时价还在。"""
        with self.post({"id": "I01", "no_gpu_price": 0.2}) as response:
            self.assertEqual(response.status, 200)
        item = self.store.read()["instances"][0]
        self.assertEqual(item["no_gpu_price"], 0.2)
        self.assertEqual(item["price"], 1.88, "改无卡费率不能动 GPU 时价")
        with self.post({"id": "I01", "no_gpu_price": None}) as response:
            self.assertEqual(response.status, 200)
        self.assertIsNone(self.store.read()["instances"][0]["no_gpu_price"])

    def test_price_endpoint_only_takes_local_json_requests(self):
        for ctype in ("text/plain", "application/x-www-form-urlencoded"):
            with self.subTest(ctype=ctype), self.assertRaises(HTTPError) as error:
                self.post({"id": "I01", "price": 1}, ctype=ctype)
            self.assertEqual(error.exception.code, 415)
        with self.assertRaises(HTTPError) as error:
            self.post({"id": "I01", "price": 1}, origin="https://evil.example")
        self.assertEqual(error.exception.code, 403, "跨站页面不能借面板改计划")
        with self.assertRaises(HTTPError) as error:
            self.post({"id": "I01", "price": 1}, headers={"Host": "external.example"})
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            self.post(b"{" + b"x" * 5000)
        self.assertEqual(error.exception.code, 413)
        with self.assertRaises(HTTPError) as error:
            self.post(b"{not json")
        self.assertEqual(error.exception.code, 400)

    def token_post(self, payload, **kwargs):
        """面板登记 Token 的请求形状：接口地址指到死端口，用例不真的联网。"""
        with mock.patch.dict(os.environ, {"TRAINOPS_API_BASE": "http://127.0.0.1:9"}):
            return self.post(payload, path=ops.TOKEN_PATH, **kwargs)

    def test_token_lives_in_memory_only(self):
        """面板登记的 Token 只进服务端内存：计划文件、响应、输出里都不留痕。"""
        before = self.store.read()
        token = "secret-token-value"
        with self.token_post({"token": token}) as response:
            self.assertEqual(response.status, 200)
            plan = json.load(response)
        self.assertTrue(plan["credentials"]["configured"])
        self.assertIsNotNone(plan["credentials"]["set_at"])
        self.assertEqual(self.credential.get(), token)
        self.assertNotIn(token, json.dumps(plan, ensure_ascii=False), "响应里不能回显凭据")
        after = self.store.read()
        for key in ("name", "instances", "flows", "tasks", "wallet"):
            self.assertEqual(after[key], before[key], f"登记 Token 不能动 {key}")
        self.assertNotIn(token, self.store.path.read_text(encoding="utf-8"), "Token 绝不能进计划文件")
        # 接口连不上不算登记失败：凭据照样登记，失败照实写进结算记录，下一轮自己会重试
        self.assertTrue(plan["monitor"]["last_error"], "连不上接口要留下原因")
        # 面板每轮读到的就是这份状态，值本身不出现在 /api/plan 里
        with OPEN(self.url + "/api/plan") as response:
            listed = json.load(response)
        self.assertEqual(listed["credentials"], plan["credentials"])
        self.assertNotIn(token, json.dumps(listed, ensure_ascii=False))
        # null 表示清除：状态回到未登记，内存里也不再留着
        with self.token_post({"token": None}) as response:
            cleared = json.load(response)
        self.assertEqual(cleared["credentials"], {"configured": False, "set_at": None})
        self.assertIsNone(self.credential.get())

    def test_bad_token_requests_never_register_anything(self):
        before = self.store.path.read_bytes()
        for payload in ({"token": ""}, {"token": "   "}, {"token": 123}, {"token": "x" * 600},
                        {"token": "line1\nline2"}, {"token": "a\rb"}, {"token": "tab\there"},
                        {}, {"token": "abc", "note": "顺手改点别的"}, {"token": None, "x": 1}, [], "abc"):
            with self.subTest(payload=str(payload)[:40]), self.assertRaises(HTTPError) as error:
                self.token_post(payload)
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(before, self.store.path.read_bytes(), "被拒绝的登记不能留下半个文件")
        self.assertIsNone(self.credential.get(), "被拒绝的请求不能改内存里的凭据")

    def test_token_endpoint_takes_the_same_three_gates(self):
        for ctype in ("text/plain", "application/x-www-form-urlencoded"):
            with self.subTest(ctype=ctype), self.assertRaises(HTTPError) as error:
                self.token_post({"token": "abc"}, ctype=ctype)
            self.assertEqual(error.exception.code, 415)
        with self.assertRaises(HTTPError) as error:
            self.token_post({"token": "abc"}, origin="https://evil.example")
        self.assertEqual(error.exception.code, 403, "跨站页面不能借面板塞凭据")
        with self.assertRaises(HTTPError) as error:
            self.token_post({"token": "abc"}, headers={"Host": "external.example"})
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            self.token_post(b"{" + b"x" * 5000)
        self.assertEqual(error.exception.code, 413)
        with self.assertRaises(HTTPError) as error:
            self.token_post(b"{not json")
        self.assertEqual(error.exception.code, 400)
        self.assertIsNone(self.credential.get(), "这几道关被拒的请求都不能登记凭据")
        with self.token_post({"token": "abc"}, origin=self.url) as response:
            self.assertEqual(response.status, 200, "面板自己发出的同源请求要放行")
        self.assertEqual(self.credential.get(), "abc")

    def test_only_the_two_panel_endpoints_are_writable(self):
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method), self.assertRaises(HTTPError) as error:
                self.post({"id": "I01", "price": 1}, method=method)
            self.assertEqual(error.exception.code, 405)
        with self.assertRaises(HTTPError) as error:  # 时价接口本身不读
            OPEN(self.url + "/api/instance-price")
        self.assertEqual(error.exception.code, 404)
        with self.assertRaises(HTTPError) as error:  # Token 只登记，不读回
            OPEN(self.url + "/api/token")
        self.assertEqual(error.exception.code, 404)
        with self.assertRaises(HTTPError) as error:  # 已知读取路径上写入 = 方法不允许
            self.post({"id": "I01", "price": 1}, path="/api/plan")
        self.assertEqual(error.exception.code, 405)
        with self.assertRaises(HTTPError) as error:
            self.post({"token": "abc"}, path="/api/nope")
        self.assertEqual(error.exception.code, 404)

    def test_corruption_is_reported_not_hidden(self):
        self.store.path.write_text("oops")
        with self.assertRaises(HTTPError) as error:
            OPEN(self.url + "/api/plan")
        self.assertEqual(error.exception.code, 500)
        self.assertIn("损坏", json.load(error.exception)["error"])


if __name__ == "__main__":
    unittest.main()
