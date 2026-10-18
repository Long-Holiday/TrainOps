"""结构化计划输入的离线 CLI 集成测试；只使用临时项目，不连接外部服务。"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/trainops/scripts/trainops.py"
spec = importlib.util.spec_from_file_location("trainops_json_tests", SCRIPT)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def request():
    return {
        "name": "结构化计划",
        "instances": [{"key": "gpu", "name": "GPU:A", "autodl_id": "test-a", "ssh_alias": "gpu-a"}],
        "flows": [{"key": "base", "name": "基线"}, {"key": "report", "name": "汇总"}],
        "tasks": [
            {"key": "eval", "flow": "report", "name": "评估:含冒号", "instance": "local",
             "minutes": None, "deps": ["train"]},
            {"key": "train", "flow": "base", "name": "训练", "instance": "gpu", "minutes": 20},
            {"key": "other", "flow": "base", "name": "独立任务", "instance": "gpu", "minutes": 0, "deps": []},
        ],
    }


class JsonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.store = ops.Store(self.project)

    def cli(self, command, payload=None, *flags, success=True, raw=None):
        if raw is None and payload is not None:
            raw = json.dumps(payload, ensure_ascii=False)
        result = subprocess.run([sys.executable, str(SCRIPT), "-p", str(self.project), command, *flags],
                                input=raw, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        if success and result.stdout.startswith("{"):
            return json.loads(result.stdout)
        return result

    def init(self):
        return self.cli("init", request(), "--from-json", "-")

    def batch(self, payload, *flags, success=True):
        return self.cli("batch", payload, "--from-json", "-", *flags, success=success)

    def apply(self, payload, *flags, success=True):
        return self.cli("apply", payload, "--from-json", "-", *flags, success=success)

    def unchanged(self, payload, fragment=None, *flags):
        before = self.store.path.read_bytes()
        result = self.batch(payload, *flags, success=False)
        self.assertEqual(self.store.path.read_bytes(), before)
        if fragment:
            self.assertIn(fragment, result.stderr)
        return result

    def test_stdin_forward_cross_flow_aliases_and_no_inference(self):
        result = self.init()
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["aliases"], {"instances": {"gpu": "I01"},
                                          "flows": {"base": "F01", "report": "F02"},
                                          "tasks": {"eval": "F02-T01", "train": "F01-T01", "other": "F01-T02"}})
        plan = self.store.read()
        self.assertEqual(plan["tasks"][0]["deps"], ["F01-T01"])
        self.assertEqual(plan["tasks"][0]["name"], "评估:含冒号")
        self.assertEqual(plan["tasks"][1]["deps"], [])
        self.assertEqual(plan["tasks"][2]["deps"], [])
        self.assertEqual(plan["tasks"][2]["duration_minutes"], 0)
        self.assertIsNone(plan["tasks"][0]["duration_minutes"])
        self.assertNotIn("key", plan["tasks"][0])
        self.assertNotIn("inferred", result)

    def test_file_input_and_nullable_new_fields(self):
        payload = request()
        payload["tasks"][0].update(instance=None, not_before=None)
        path = self.root / "input.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.cli("init", None, "--from-json", str(path))
        self.assertIsNone(self.store.read()["tasks"][0]["instance"])
        self.assertIsNone(self.store.read()["tasks"][0]["not_before"])
        self.assertFalse((self.project / "input.json").exists())

    def test_create_update_and_explicit_clear_in_one_revision(self):
        self.init()
        result = self.batch({
            "instances": [{"key": "gpu2", "name": "B", "autodl_id": "test-b", "ssh_alias": "gpu-b"}],
            "flows": [{"key": "extra", "name": "补充"}],
            "tasks": [{"key": "new", "flow": "extra", "name": "新增", "instance": "gpu2",
                       "deps": ["F01-T01"]}],
            "updates": [{"id": "new", "name": "新任务", "instance": "local", "minutes": 5, "deps": []},
                        {"id": "F02-T01", "instance": None, "minutes": None, "not_before": None, "deps": []}],
        })
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["item"]["aliases"]["tasks"], {"new": "F03-T01"})
        self.assertTrue(all(item["cleared"] for item in result["item"]["deps"]))
        plan = self.store.read()
        self.assertEqual(plan["tasks"][-1]["name"], "新任务")
        self.assertEqual(plan["tasks"][-1]["deps"], [])
        self.assertIsNone(plan["tasks"][0]["instance"])
        self.assertEqual(plan["tasks"][1]["duration_minutes"], 20)
        self.unchanged({"updates": [{"id": "new", "note": "别名不跨请求保留"}]}, "updates[0].id")

    def test_final_topology_reversal_and_forward_new_target(self):
        self.init()
        self.batch({"tasks": [{"key": "tail", "flow": "F02", "name": "尾任务", "instance": "local"}],
                    "updates": [{"id": "F01-T01", "deps": ["F02-T01"]},
                                {"id": "F02-T01", "deps": ["tail"]}]})
        plan = self.store.read()
        self.assertEqual(plan["tasks"][1]["deps"], ["F02-T01"])
        self.assertEqual(plan["tasks"][0]["deps"], ["F02-T02"])
        self.assertEqual(plan["revision"], 2)

    def test_done_parent_and_start_child_share_timestamp_in_either_order(self):
        self.init()
        result = self.batch({"updates": [{"id": "F02-T01", "status": "running", "progress": 25},
                                          {"id": "F01-T01", "status": "done"}]})
        self.assertEqual(result["revision"], 2)
        plan = self.store.read()
        child, parent = plan["tasks"][:2]
        self.assertEqual(child["started_at"], parent["finished_at"])
        self.assertEqual(child["progress"], 25)
        self.assertEqual(child["checked_at"], parent["checked_at"])
        self.assertEqual(parent["progress"], 100)
        self.batch({"updates": [{"id": "F02-T01", "status": "done"},
                                {"id": "F01-T02", "status": "running", "deps": ["F02-T01"]}]})
        ops.validate(self.store.read())

    def test_pending_structure_then_done_and_metadata_inspection_semantics(self):
        self.init()
        self.batch({"updates": [{"id": "F01-T02", "instance": "local", "deps": [], "status": "done"},
                                {"id": "F01-T01", "name": "改名", "note": "核实后记录"}]})
        plan = self.store.read()
        self.assertEqual(plan["tasks"][2]["status"], "done")
        self.assertIsNotNone(plan["tasks"][2]["checked_at"])
        self.assertIsNone(plan["tasks"][1]["checked_at"])
        self.batch({"updates": [{"id": "F01-T01", "checked": True}]})
        self.assertIsNotNone(self.store.read()["tasks"][1]["checked_at"])

    def test_done_keeps_existing_progress_normalization_and_pending_reset(self):
        self.init()
        self.batch({"updates": [{"id": "F01-T01", "status": "done", "progress": 20}]})
        self.assertEqual(self.store.read()["tasks"][1]["progress"], 100)
        self.batch({"updates": [{"id": "F01-T01", "status": "pending"}]})
        task = self.store.read()["tasks"][1]
        for field in ("progress", "started_at", "finished_at", "checked_at", "progress_at"):
            self.assertIsNone(task[field])

    def test_concurrent_batches_allocate_against_latest_locked_plan(self):
        self.init()
        command = [sys.executable, str(SCRIPT), "-p", str(self.project), "batch", "--from-json", "-"]
        children = [subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True) for _ in range(2)]
        raw = json.dumps({"tasks": [{"key": "new", "flow": "F01", "name": "并发任务"}]})
        for child in children:
            child.stdin.write(raw)
            child.stdin.close()
            child.stdin = None
        ids = []
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, stderr)
            ids.append(json.loads(stdout)["item"]["aliases"]["tasks"]["new"])
        self.assertEqual(set(ids), {"F01-T03", "F01-T04"})
        self.assertEqual(self.store.read()["revision"], 3)

    def test_running_structural_guards_even_when_stopping(self):
        self.init()
        self.batch({"updates": [{"id": "F01-T01", "status": "running"}]})
        for changes in ({"instance": "local"}, {"deps": ["F01-T02"]},
                        {"not_before": "2020-01-01T00:00:00Z"}):
            with self.subTest(changes=changes):
                self.unchanged({"updates": [{"id": "F01-T01", "status": "blocked", **changes}]}, "运行中不能")
        self.unchanged({"updates": [{"id": "F01-T01", "status": "blocked"},
                                    {"id": "F01-T01", "instance": "local"}]}, "重复更新")

    def test_hard_errors_rollback_all_creates_updates_and_counters(self):
        self.init()
        cases = [
            {"updates": [{"id": "F01-T01", "deps": ["F01-T01"]}]},
            {"updates": [{"id": "F01-T01", "deps": ["F02-T01"]}]},
            {"updates": [{"id": "F02-T01", "status": "running"}]},
            {"updates": [{"id": "F01-T01", "status": "failed"}]},
            {"updates": [{"id": "F01-T01", "status": "running"}, {"id": "F01-T02", "status": "running"}]},
            {"updates": [{"id": "F01-T01", "deps": ["F01-T02", "F01-T02"]}]},
            {"updates": [{"id": "F01-T01", "not_before": "2099-01-01T00:00:00Z", "status": "running"}]},
        ]
        for payload in cases:
            payload["flows"] = [{"key": "newflow", "name": "不应保存"}]
            with self.subTest(payload=payload):
                self.unchanged(payload)
        self.assertEqual(self.store.read()["next_flow"], 3)

    def test_new_object_schema_is_checked_before_allocating_files(self):
        cases = [
            ({"name": "计划", "tasks": [{"flow": "F01", "name": "任务", "deps": False}]}, "tasks[0].deps"),
            ({"name": "计划", "tasks": [{"flow": "F01", "name": "任务", "minutes": True}]}, "tasks[0].minutes"),
            ({"name": "计划", "instances": [{"name": "A", "autodl_id": "a", "ssh_alias": "-a"}]}, "instances[0].ssh_alias"),
            ({"name": "计划", "flows": [{"name": "流程", "next_task": 8}]}, "未知字段"),
            ({"name": "计划", "tasks": [{"name": "缺流程"}]}, "缺少字段"),
            ({"name": "计划", "updates": []}, "未知字段"),
        ]
        for payload, fragment in cases:
            with self.subTest(payload=payload):
                result = self.cli("init", payload, "--from-json", "-", success=False)
                self.assertIn(fragment, result.stderr)
                self.assertFalse(self.project.exists())

    def test_progress_can_be_cleared_and_structure_omission_preserves_deps(self):
        self.init()
        self.batch({"updates": [{"id": "F01-T01", "status": "running", "progress": 50}]})
        self.batch({"updates": [{"id": "F01-T01", "progress": None},
                                {"id": "F02-T01", "note": "保留真实依赖", "checked": False}]})
        plan = self.store.read()
        self.assertIsNone(plan["tasks"][1]["progress"])
        self.assertIsNotNone(plan["tasks"][1]["checked_at"])
        self.assertEqual(plan["tasks"][0]["deps"], ["F01-T01"])
        self.assertIsNone(plan["tasks"][0]["checked_at"])

    def test_schema_rejections_with_field_paths(self):
        self.init()
        bad_updates = [
            ({"id": "F01-T01", "minutes": True}, "updates[0].minutes"),
            ({"id": "F01-T01", "progress": 101}, "updates[0].progress"),
            ({"id": "F01-T01", "deps": False}, "updates[0].deps"),
            ({"id": "F01-T01", "deps": [None]}, "updates[0].deps[0]"),
            ({"id": "F01-T01", "checked": 1}, "updates[0].checked"),
            ({"id": "F01-T01", "status": None}, "updates[0].status"),
            ({"id": "F01-T01", "name": " "}, "updates[0].name"),
            ({"id": "F01-T01", "note": None}, "updates[0].note"),
            ({"id": "F01-T01", "not_before": "2026-01-01"}, "updates[0].not_before"),
            ({"id": "F01-T01", "price": 1}, "未知字段"),
            ({"id": "F01-T01", "started_at": "2026-01-01T00:00:00Z"}, "未知字段"),
            ({"id": "F01-T01"}, "未提供"),
        ]
        for update, fragment in bad_updates:
            with self.subTest(update=update):
                self.unchanged({"updates": [update]}, fragment)
        for payload in ({}, {"updates": []}, {"tasks": None}, {"tasks": [0]}, [],
                        {"name": "不能批量改计划名"}, {"updates": [{"id": "F01-T01", "checked": False}]}):
            with self.subTest(payload=payload):
                self.unchanged(payload)

    def test_alias_rejections_and_wrong_kind_references(self):
        self.init()
        for key in ("local", "I01", "I1", "F01", "F02-T03"):
            with self.subTest(key=key):
                self.unchanged({"flows": [{"key": key, "name": "非法别名"}]}, "flows[0].key")
        self.unchanged({"flows": [{"key": "same", "name": "流程"}],
                        "tasks": [{"key": "same", "flow": "same", "name": "任务"}]}, "tasks[0].key")
        self.unchanged({"flows": [{"key": "newflow", "name": "流程"}],
                        "tasks": [{"flow": "F01", "name": "任务", "instance": "newflow"}]}, "tasks[0].instance")
        self.unchanged({"tasks": [{"flow": "missing", "name": "任务"}]}, "tasks[0].flow")
        self.unchanged({"updates": [{"id": "F01-T01", "deps": ["missing"]}]}, "updates[0].deps[0]")
        self.unchanged({"tasks": [{"key": "t", "flow": "F01", "name": "任务"}],
                        "updates": [{"id": "t", "note": "a"}, {"id": "F01-T03", "note": "b"}]}, "重复更新")

    def test_invalid_json_and_numeric_overflow(self):
        self.init()
        before = self.store.path.read_bytes()
        for raw in ('{', '{"updates":[],"updates":[]}', '{"tasks":[{"name":"a","name":"b"}]}',
                    '{"updates":[{"id":"F01-T01","minutes":NaN}]}',
                    '{"updates":[{"id":"F01-T01","minutes":Infinity}]}',
                    '{"updates":[{"id":"F01-T01","minutes":1e999}]}'):
            with self.subTest(raw=raw):
                self.cli("batch", None, "--from-json", "-", success=False, raw=raw)
                self.assertEqual(before, self.store.path.read_bytes())
        for raw in ('{"updates":[{"id":"F01-T01","note":"\\ud800"}]}',
                    '{"updates":[{"id":"F01-T01","minutes":-Infinity}]}'):
            self.cli("batch", None, "--from-json", "-", success=False, raw=raw)
        command = [sys.executable, str(SCRIPT), "-p", str(self.project), "batch", "--from-json", "-"]
        invalid_stdin = subprocess.run(command, input=b'{"updates": "\xff"}', capture_output=True, timeout=20)
        self.assertEqual(invalid_stdin.returncode, 1)
        self.assertNotIn(b"Traceback", invalid_stdin.stderr)
        self.assertEqual(before, self.store.path.read_bytes())
        invalid = self.root / "invalid.json"
        invalid.write_bytes(b'\xff')
        self.cli("batch", None, "--from-json", str(invalid), success=False)
        self.cli("batch", None, "--from-json", str(self.root / "missing"), success=False)

    def test_mode_exclusion_missing_name_and_no_overwrite(self):
        self.cli("init", None, success=False)
        self.assertFalse(self.project.exists())
        for flag in ("--name", "--instance", "--flow", "--task"):
            self.cli("init", request(), "--from-json", "-", flag, "x", success=False)
        self.init()
        before = self.store.path.read_bytes()
        for flag in ("--instance", "--flow", "--task", "--set", "--dep"):
            self.cli("batch", {}, "--from-json", "-", flag, "x", success=False)
        for flags in ((), ("--dry-run",)):
            self.cli("init", request(), "--from-json", "-", *flags, success=False)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_init_dry_run_no_files_and_preview_ids(self):
        result = self.cli("init", request(), "--from-json", "-", "--dry-run")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["revision"], 0)
        self.assertEqual(result["proposed_revision"], 1)
        self.assertIn("check", result)
        self.assertFalse(self.project.exists())
        result = self.cli("init", request(), "--from-json", "-", "--dry-run", "--brief")
        self.assertIn("未保存", result.stdout)
        self.assertIn("不保留", result.stdout)
        self.assertNotIn("已保存", result.stdout)
        self.assertFalse(self.project.exists())
        bad = request()
        bad["tasks"][1]["deps"] = ["eval"]
        self.cli("init", bad, "--from-json", "-", "--dry-run", success=False)
        self.assertFalse(self.project.exists())

    def test_apply_creates_absent_plan_with_same_aliases_and_sections(self):
        result = self.apply(request())
        self.assertTrue(result["created"])
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["name"], "结构化计划")
        self.assertEqual(result["aliases"], {"instances": {"gpu": "I01"},
                                             "flows": {"base": "F01", "report": "F02"},
                                             "tasks": {"eval": "F02-T01", "train": "F01-T01",
                                                       "other": "F01-T02"}})
        document = self.store.read_document()
        self.assertEqual(document["plan"]["revision"], 1)
        self.assertIsNone(document["config"])
        self.assertEqual(document["experiments"], ops.default_state())
        self.assertEqual(len(document["plan"]["tasks"]), 3)
        plan = document["plan"]
        self.assertEqual(plan["tasks"][0]["deps"], ["F01-T01"])

    def test_apply_accepts_json_file_path(self):
        path = self.root / "apply.json"
        path.write_text(json.dumps(request(), ensure_ascii=False), encoding="utf-8")
        result = self.cli("apply", None, "--from-json", str(path))
        self.assertTrue(result["created"])
        self.assertEqual(result["revision"], 1)
        self.assertEqual(self.store.read()["name"], "结构化计划")

    def test_apply_updates_existing_preserving_sections_wallet_and_one_revision(self):
        self.init()
        document = self.store.read_document()
        document["config"] = ops.default_config("测试")
        document["experiments"]["runs"]["run-1"] = {
            "run_id": "run-1", "version": 1, "task_id": "F01-T01", "status": "running",
            "started_at": "2026-10-14T10:00:00Z", "finished_at": None,
            "intent": "核实真实产物", "sync": {"pending": True},
        }
        document["plan"]["wallet"] = {"assets": 123.0, "voucher": 5.0,
                                      "observed_at": ops.stamp(ops.utcnow())}
        document["plan"]["instances"][0]["price"] = 2.5
        self.store.write_document(document)
        result = self.apply({
            "name": "结构化计划",
            "tasks": [{"key": "new", "flow": "F01", "name": "新增", "deps": ["F01-T02"]}],
            "updates": [{"id": "F01-T01", "note": "已核实"}],
        })
        self.assertFalse(result["created"])
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["name"], "结构化计划")
        self.assertEqual(result["aliases"]["tasks"], {"new": "F01-T03"})
        self.assertEqual(result["updated"], ["F01-T01"])
        after = self.store.read_document()
        self.assertEqual(after["config"], document["config"])
        self.assertEqual(after["experiments"], document["experiments"])
        self.assertEqual(after["plan"]["wallet"], document["plan"]["wallet"])
        self.assertEqual(after["plan"]["instances"][0]["price"], 2.5)
        self.assertEqual(after["plan"]["revision"], 2)
        self.assertEqual(after["plan"]["tasks"][-1]["deps"], ["F01-T02"])
        self.assertEqual(after["plan"]["tasks"][1]["note"], "已核实")

    def test_apply_name_is_create_only_and_clash_is_atomic(self):
        self.init()
        before = self.store.path.read_bytes()
        result = self.apply({"name": "另一个名字",
                             "tasks": [{"key": "new", "flow": "F01", "name": "不应保存"}]},
                            success=False)
        self.assertIn("不会改计划名", result.stderr)
        self.assertEqual(before, self.store.path.read_bytes())
        result = self.apply({"name": "结构化计划"}, success=False)
        self.assertIn("至少需要一项", result.stderr)
        self.assertEqual(before, self.store.path.read_bytes())
        plan = self.store.read()
        self.assertEqual(plan["name"], "结构化计划")
        self.assertEqual(plan["revision"], 1)

    def test_apply_create_requires_name_and_rejects_updates_without_plan_file(self):
        result = self.apply({"tasks": [{"flow": "F01", "name": "任务"}]}, success=False)
        self.assertIn("需要 JSON 的 name", result.stderr)
        self.assertFalse(self.store.path.exists())
        result = self.apply({"name": "计划", "updates": [{"id": "F01-T01", "note": "x"}]},
                            success=False)
        self.assertIn("不能包含 updates", result.stderr)
        self.assertFalse(self.store.path.exists())

    def test_apply_reuses_request_schema_validation(self):
        self.apply({}, success=False)
        self.assertFalse(self.project.exists())
        self.init()
        before = self.store.path.read_bytes()
        result = self.apply({"updates": [{"id": "F01-T01", "price": 1}]}, success=False)
        self.assertIn("未知字段", result.stderr)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_apply_dry_run_never_creates_or_locks(self):
        result = self.apply(request(), "--dry-run")
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["created"])
        self.assertEqual(result["revision"], 0)
        self.assertEqual(result["proposed_revision"], 1)
        self.assertIn("check", result)
        self.assertFalse(self.project.exists())
        result = self.apply(request(), "--dry-run", "--brief")
        self.assertIn("未保存", result.stdout)
        self.assertIn("新建计划", result.stdout)
        self.assertFalse(self.project.exists())
        self.init()
        self.store.lock_path.unlink()
        before = self.store.path.read_bytes()
        files = sorted(self.store.directory.iterdir())
        result = self.apply({"tasks": [{"key": "new", "flow": "F01", "name": "新任务"}],
                             "updates": [{"id": "F01-T01", "minutes": None}]}, "--dry-run")
        self.assertFalse(result["created"])
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["proposed_revision"], 2)
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(files, sorted(self.store.directory.iterdir()))
        self.assertFalse(self.store.lock_path.exists())

    def test_apply_help_and_brief_describe_create_vs_update(self):
        output = subprocess.run([sys.executable, str(SCRIPT), "apply", "--help"],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(output.returncode, 0)
        for fragment in ("无计划", "有计划", "不按 key 持久匹配", "重复提交新增项会再次创建",
                         "不执行或停止训练", "name", "updates", "dry-run"):
            self.assertIn(fragment, output.stdout)
        result = self.apply(request(), "--brief")
        self.assertIn("新建计划", result.stdout)
        self.assertIn("已保存", result.stdout)
        result = self.apply({"updates": [{"id": "F01-T01", "note": "更新"}]}, "--brief")
        self.assertIn("更新已有计划", result.stdout)
        self.assertIn("已保存", result.stdout)

    def test_concurrent_apply_on_absent_creates_exactly_once(self):
        command = [sys.executable, str(SCRIPT), "-p", str(self.project),
                   "apply", "--from-json", "-"]
        children = [subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True) for _ in range(3)]
        raw = json.dumps({"name": "并发计划",
                          "flows": [{"key": "f", "name": "流程"}],
                          "tasks": [{"key": "t", "flow": "f", "name": "任务"}]})
        for child in children:
            child.stdin.write(raw)
            child.stdin.close()
            child.stdin = None
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sorted(item["revision"] for item in results), [1, 2, 3])
        self.assertEqual(sum(bool(item["created"]) for item in results), 1)
        plan = self.store.read()
        self.assertEqual(plan["revision"], 3)
        self.assertEqual(plan["name"], "并发计划")
        self.assertEqual(len(plan["flows"]), 3)
        self.assertEqual(len(plan["tasks"]), 3)

    def test_batch_dry_run_no_lock_revision_or_byte_changes(self):
        self.init()
        self.store.lock_path.unlink()
        before = self.store.path.read_bytes()
        files = sorted(self.store.directory.iterdir())
        payload = {"tasks": [{"key": "new", "flow": "F01", "name": "新任务"}],
                   "updates": [{"id": "F01-T01", "minutes": None}]}
        result = self.batch(payload, "--dry-run")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["proposed_revision"], 2)
        self.assertTrue(result["dry_run"])
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(files, sorted(self.store.directory.iterdir()))
        self.unchanged({"updates": [{"id": "F02-T01", "status": "running"}]}, None, "--dry-run")
        self.assertFalse(self.store.lock_path.exists())
        self.cli("batch", None, "--task", "F01:简写预览:local:5", "--dry-run")
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertFalse(self.store.lock_path.exists())

    def test_non_plan_document_sections_and_prices_preserved(self):
        self.init()
        document = self.store.read_document()
        document["config"] = ops.default_config("测试")
        document["experiments"]["runs"]["run-1"] = {
            "run_id": "run-1", "version": 1, "task_id": "F01-T01", "status": "running",
            "started_at": "2026-10-14T10:00:00Z", "finished_at": None,
            "intent": "核实真实产物", "sync": {"pending": True},
        }
        document["plan"]["instances"][0]["price"] = 2.5
        self.store.write_document(document)
        self.batch({"updates": [{"id": "F01-T01", "note": "保留其他段"}]})
        after = self.store.read_document()
        self.assertEqual(document["config"], after["config"])
        self.assertEqual(document["experiments"], after["experiments"])
        self.assertEqual(after["plan"]["instances"][0]["price"], 2.5)
        self.assertEqual(after["plan"]["revision"], 2)

    def test_brief_alias_limits_and_advisory_checks(self):
        result = self.cli("init", request(), "--from-json", "-", "--brief")
        self.assertIn("已保存", result.stdout)
        self.assertIn("别名映射（共 6 条）", result.stdout)
        self.assertIn("省略", result.stdout)
        self.assertIn("结构检查", result.stdout)
        result = self.batch({"updates": [{"id": "F01-T01", "instance": None}]}, "--brief")
        self.assertIn("已保存", result.stdout)
        self.assertIn("结构问题", result.stdout)
        self.assertIsNone(self.store.read()["tasks"][1]["instance"])
        result = self.batch({"updates": [{"id": "F01-T01", "note": "纯备注"}]})
        self.assertNotIn("check", result)
        self.assertNotIn("check", self.store.read())


if __name__ == "__main__":
    unittest.main()
