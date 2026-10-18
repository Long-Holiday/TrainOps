"""实验记录：所有飞书操作都用假 CLI，不读取开发机授权或访问网络。"""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/trainops/scripts/trainops.py"
spec = importlib.util.spec_from_file_location("experiment_ops", SCRIPT)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class FakeLark:
    def __init__(self):
        self.calls = []
        self.records = []
        self.failure = None
        self.after_write = None
        self.fields = [{"id": f"fld{i}", "name": name, "type": kind}
                       for i, (name, kind) in enumerate(ops.EXPERIMENT_FIELDS.values())]

    def __call__(self, command, **options):
        self.calls.append((command, copy.deepcopy(options)))
        if self.failure == command:
            raise ops.PlanError("lark-cli 超时；请稍后补录")
        if command == "+url-resolve":
            return {"base_token": "base-existing", "table_id": "tbl-existing"}
        if command == "+base-create":
            self.fields = [{"id": f"fld{i}", **copy.deepcopy(field)}
                           for i, field in enumerate(options["fields"])]
            return {"base": {"base_token": "base-new", "url": "https://example.feishu.cn/base/base-new"},
                    "table": {"id": "tbl-new"}}
        if command == "+field-list":
            return {"fields": copy.deepcopy(self.fields), "has_more": False}
        if command == "+field-create":
            self.fields.extend(copy.deepcopy(options["json"]))
            return {"items": [{"status": "created"} for _ in options["json"]]}
        if command == "+record-list":
            wanted = options["filter_json"]["conditions"][0][2]
            return {"records": [copy.deepcopy(r) for r in self.records if r["fields"]["Run ID"] == wanted],
                    "has_more": False}
        if command == "+record-upsert":
            if "record_id" in options:
                record = next(r for r in self.records if r["record_id"] == options["record_id"])
                record["fields"].update(options["json"])
            else:
                record = {"record_id": f"rec{len(self.records) + 1}", "fields": copy.deepcopy(options["json"])}
                self.records.append(record)
            if self.after_write:
                self.after_write()
            return {"record": copy.deepcopy(record)}
        raise AssertionError(command)


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ops.Store(self.root)
        plan = ops.new_plan("真实实验")
        ops.add_instance(plan, "GPU", "a", "ssh-a")
        ops.add_flow(plan, "基线")
        ops.add_task(plan, "F01", "训练", "I01", 10)
        self.store.create(plan)
        self.original_plan = copy.deepcopy(self.store.read())
        self.log = ops.ExperimentLog(self.store)
        self.fake = FakeLark()
        self.log.call = self.fake

    def initialize(self):
        return self.log.init()

    def start(self, run_id="run-1", **patch):
        return self.log.run(task_id="F01-T01", run_id=run_id, patch=patch)

    def assert_plan_unchanged(self):
        self.assertEqual(self.store.read(), self.original_plan)

    def test_plan_and_experiment_sections_share_one_file(self):
        self.assertEqual(self.store.path, self.root / ".agent/experiment.yaml")
        document = self.store.read_document()
        self.assertIsNone(document["config"])
        self.assertEqual(document["experiments"], {"schema_version": 1, "runs": {}})
        revision = self.store.read()["revision"]
        self.initialize()
        self.start()
        document = self.store.read_document()
        self.assertIsNotNone(document["config"])
        self.assertIn("run-1", document["experiments"]["runs"])
        self.assertEqual(document["plan"]["revision"], revision, "记录 Run 不能推进计划 revision")

    def test_auto_create_config_persistence_and_resume(self):
        self.initialize()
        self.assertEqual(self.log.config_path, self.root / ".agent/experiment.yaml")
        self.assertEqual(self.log.config()["lark"]["base_token"], "base-new")
        created = self.fake.calls[0]
        self.assertEqual(created[0], "+base-create")
        run_id_field = created[1]["fields"][0]
        self.assertEqual((run_id_field["name"], run_id_field["type"]), ("Run ID", "text"))
        self.assertIn("description", run_id_field)
        field_names = {field["name"] for field in created[1]["fields"]}
        self.assertTrue({"人工决策", "决策人", "决策时间", "创建时间", "最后更新时间"} <= field_names)
        self.log.init(settings={"settings": {"monitor_minutes": 20}})
        self.log.init(settings={"defaults": {"direction": "max"}})
        self.assertEqual(sum(c == "+base-create" for c, _ in self.fake.calls), 1)
        self.assertEqual(self.log.config()["settings"]["monitor_minutes"], 20)
        self.assertEqual(self.log.config()["defaults"]["direction"], "max")
        self.assert_plan_unchanged()

    def test_existing_url_never_creates_base(self):
        self.log.init(url="https://example.feishu.cn/base/base-existing?table=tbl-existing")
        self.assertFalse(any(c == "+base-create" for c, _ in self.fake.calls))
        self.assertEqual(self.log.config()["lark"]["table_id"], "tbl-existing")
        self.fake.failure = "+field-list"
        with self.assertRaises(ops.PlanError):
            self.log.init()
        self.assertFalse(any(c == "+base-create" for c, _ in self.fake.calls))

    def test_uncertain_creation_no_blind_retry(self):
        self.fake.failure = "+base-create"
        with self.assertRaises(ops.PlanError):
            self.log.init()
        self.fake.failure = None
        with self.assertRaisesRegex(ops.PlanError, "不确定"):
            self.log.init()
        self.assertEqual(sum(c == "+base-create" for c, _ in self.fake.calls), 1)
        self.log.init(url="https://example.feishu.cn/base/recovered?table=tbl-existing")
        self.assertNotIn("creation_uncertain", self.log.state())

    def test_missing_cli_can_retry_after_install_without_false_uncertainty(self):
        self.log.call = mock.Mock(side_effect=ops.LarkNotStarted("未安装"))
        with self.assertRaises(ops.LarkNotStarted):
            self.log.init(settings={"defaults": {"direction": "min"}})
        self.assertNotIn("creation_uncertain", self.log.state())
        self.assertEqual(self.log.config()["defaults"]["direction"], "min")
        self.log.call = self.fake
        self.log.init()
        self.assertEqual(self.log.config()["lark"]["base_token"], "base-new")

    def test_created_base_saved_before_field_verification(self):
        self.fake.failure = "+field-list"
        with self.assertRaises(ops.PlanError):
            self.log.init()
        self.assertTrue(self.log.config_path.exists())
        self.fake.failure = None
        self.log.init()
        self.assertEqual(sum(c == "+base-create" for c, _ in self.fake.calls), 1)

    def test_corrupt_config_not_overwritten(self):
        for content in ("project: yaml-not-supported\n", "{broken", "[]"):
            self.store.path.write_text(content)
            with self.assertRaises(ops.PlanError):
                self.log.init()
            self.assertEqual(self.store.path.read_text(), content)
        self.assertFalse(self.fake.calls)

    def test_secret_keys_and_invalid_settings_rejected_before_network(self):
        for setting in ({"settings": {"access_token": "secret"}}, {"defaults": {"direction": "maximize"}},
                        {"lark": {"fields": {"run_id": "人工决策"}}}, {"defaults": {"metric_value": 1}}):
            with self.assertRaises(ops.PlanError):
                self.log.init(settings=setting)
        self.assertFalse(self.fake.calls)

    def test_start_without_lark_config_still_saves_and_can_be_shown(self):
        result = ops.experiment_dispatch(ops.argparse.Namespace(
            action="start", task="F01-T01", run_id="offline", json='{"intent":"本地留痕"}'), self.store)
        self.assertTrue(result["local_saved"])
        self.assertFalse(result["ok"])
        shown = self.log.show()
        self.assertIsNone(shown["config"])
        self.assertTrue(shown["runs"]["offline"]["pending"])
        self.assert_plan_unchanged()

    def test_unknown_patch_fields_report_invalid_and_allowed_fields(self):
        original = self.store.path.read_bytes()
        for patch in ({"notes": "说明"}, {"notes": "说明", "run_id": "new", "task_id": "F01-T01"}):
            with self.subTest(patch=patch), self.assertRaises(ops.PlanError) as error:
                self.log.run(task_id="F01-T01", patch=patch)
            invalid_fields = ", ".join(sorted(repr(key) for key in patch))
            allowed_fields = ", ".join(sorted(ops.RUN_UPDATE))
            self.assertEqual(str(error.exception),
                             f"实验记录含未知或不可修改字段: {invalid_fields}；允许字段: [{allowed_fields}]")
            self.assertEqual(self.store.path.read_bytes(), original)
        self.assertFalse(self.fake.calls)

    def test_invalid_defaults_report_only_allowed_default_fields(self):
        original = self.store.path.read_bytes()
        with self.assertRaises(ops.PlanError) as error:
            self.log.init(settings={"defaults": {"notes": "说明", "intent": "默认意图"}})
        self.assertEqual(str(error.exception),
                         "实验记录含未知或不可修改字段: 'intent', 'notes'；允许字段: "
                         "[dataset_version, direction, eval_protocol, metric_name]")
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertFalse(self.fake.calls)

    def test_invalid_patch_is_atomic_and_unknown_task_rejected(self):
        self.initialize()
        self.start()
        original = self.log.state_path.read_bytes()
        for patch in ({"metric_value": 2}, {"direction": "unknown"}, {"task_id": "F01-T99"},
                      {"suggestion": "x" * 8001}, {"checked_at": "yesterday"}):
            with self.assertRaises(ops.PlanError):
                self.log.run(run_id="run-1", patch=patch)
            self.assertEqual(self.log.state_path.read_bytes(), original)
        for value in ('{"metric_value":NaN}', '[]', 'null'):
            with self.assertRaises(ops.PlanError):
                ops.experiment_object(value)
        with self.assertRaises(ops.PlanError):
            self.log.run(task_id="F99-T99")
        self.assertEqual(self.log.state_path.read_bytes(), original)

    def test_explicit_run_times_preserve_real_window(self):
        self.initialize()
        started = "2026-10-17T19:40:00+08:00"
        finished = "2026-10-17T21:05:00+08:00"
        record = self.start(started_at=started)
        self.assertEqual(record["started_at"], started)
        self.log.run(run_id="run-1", patch={"status": "done", "finished_at": finished})
        record = self.log.state()["runs"]["run-1"]
        self.assertEqual(record["started_at"], started)
        self.assertEqual(record["finished_at"], finished)
        with self.assertRaisesRegex(ops.PlanError, "不能早于"):
            self.log.run(run_id="run-1", patch={"finished_at": "2026-10-17T19:00:00+08:00"})
        with self.assertRaises(ops.PlanError):
            self.start(run_id="run-2", started_at="2026-10-17T19:40:00")
        self.assertNotIn("run-2", self.log.state()["runs"])

    def test_switch_target_never_reuses_old_record_id(self):
        self.initialize()
        self.start()
        self.log.sync()
        old = self.log.state()["runs"]["run-1"]["sync"]["target"]
        self.fake.records = []  # 新表为空；旧表记录仍然存在但不在新表查询结果中
        self.log.init(url="https://example.feishu.cn/base/base-existing?table=tbl-existing")
        self.log.sync()
        writes = [options for command, options in self.fake.calls if command == "+record-upsert"]
        self.assertNotIn("record_id", writes[-1])
        self.assertNotEqual(old, self.log.state()["runs"]["run-1"]["sync"]["target"])

    def test_incomplete_resolved_url_cannot_fall_back_to_old_table(self):
        self.initialize()
        original = self.log.config_path.read_bytes()
        fake = self.fake
        def call(command, **options):
            if command == "+url-resolve":
                return {"table_id": "tbl-new"}
            return fake(command, **options)
        self.log.call = call
        with self.assertRaises(ops.PlanError):
            self.log.init(url="https://example.feishu.cn/base/missing")
        self.assertEqual(self.log.config_path.read_bytes(), original)

    def test_field_failure_is_saved_for_retry_and_query_failure_never_creates(self):
        self.initialize()
        self.start()
        self.fake.failure = "+field-list"
        self.assertFalse(self.log.sync()["ok"])
        self.assertIn("error", self.log.state()["runs"]["run-1"]["sync"])
        self.fake.failure = "+record-list"
        self.assertFalse(self.log.sync()["ok"])
        self.assertFalse(any(c == "+record-upsert" for c, _ in self.fake.calls))

    def test_matrix_record_responses_create_update_and_deduplicate(self):
        self.initialize()
        self.start()
        def call(command, **options):
            result = self.fake(command, **options)
            if command == "+record-list":
                rows = result["records"]
                return {"record_id_list": [r["record_id"] for r in rows],
                        "data": [[r["fields"]["Run ID"]] for r in rows], "has_more": False}
            if command == "+record-upsert":
                return {"created": "record_id" not in options,
                        "record": {"record_id_list": [result["record"]["record_id"]]}}
            return result
        self.log.call = call
        self.assertTrue(self.log.sync()["ok"])
        self.assertEqual(self.log.state()["runs"]["run-1"]["sync"]["record_id"], "rec1")
        self.assertEqual(self.log.sync()["runs"][0]["status"], "unchanged")
        self.log.run(run_id="run-1", patch={"suggestion": "复现"})
        self.assertTrue(self.log.sync()["ok"])
        writes = [kw for cmd, kw in self.fake.calls if cmd == "+record-upsert"]
        self.assertNotIn("record_id", writes[0])
        self.assertEqual(writes[1]["record_id"], "rec1")
        self.assertEqual(len(self.fake.records), 1)
        self.assertFalse(self.log.show()["runs"]["run-1"]["pending"])
        self.assert_plan_unchanged()

    def test_record_list_data_object_fallback(self):
        self.initialize()
        self.start()
        def call(command, **options):
            result = self.fake(command, **options)
            if command == "+record-list":
                result["data"] = result.pop("records")
            return result
        self.log.call = call
        self.assertTrue(self.log.sync()["ok"])
        self.log.run(run_id="run-1", patch={"suggestion": "复现"})
        self.assertTrue(self.log.sync()["ok"])
        self.assertEqual(len(self.fake.records), 1)

    def test_matrix_pagination_detects_duplicate_rows(self):
        self.initialize()
        self.start()
        offsets = []
        def call(command, **options):
            if command == "+record-list":
                offsets.append(options["offset"])
                return {"record_id_list": ["rec" + str(options["offset"])],
                        "data": [["run-1"]], "has_more": options["offset"] == 0}
            return self.fake(command, **options)
        self.log.call = call
        result = self.log.sync()
        self.assertFalse(result["ok"])
        self.assertEqual(offsets, [0, 1])
        self.assertIn("冲突", result["runs"][0]["error"])
        self.assertFalse(any(cmd == "+record-upsert" for cmd, _ in self.fake.calls))

    def test_malformed_record_lists_never_write(self):
        self.initialize()
        self.start()
        for response in ({"record_id_list": None}, {"record_id_list": "rec1"},
                         {"record_id_list": [None]}, {"record_id_list": [""]},
                         {"record_id_list": [123]}, {"data": [["run-1"]]},
                         {"records": [None]}, {"records": [{"record_id": 123}]},
                         {"record_id_list": [], "has_more": True}):
            with self.subTest(response=response):
                self.log.call = lambda command, **options: (
                    response if command == "+record-list" else self.fake(command, **options))
                self.assertFalse(self.log.sync()["ok"])
                self.assertFalse(any(cmd == "+record-upsert" for cmd, _ in self.fake.calls))

    def test_invalid_upsert_ids_and_ignored_fields_stay_pending(self):
        self.initialize()
        self.start()
        for response in ({"record": None}, {"record": {"record_id_list": []}},
                         {"record": {"record_id_list": "rec1"}},
                         {"record": {"record_id_list": [None]}},
                         {"record": {"record_id_list": ["rec1", "rec2"]}},
                         {"record": {"record_id": 123}},
                         {"record": {"record_id_list": ["rec1"]}, "ignored_fields": ["主指标值"]}):
            with self.subTest(response=response):
                self.log.call = lambda command, **options: (
                    response if command == "+record-upsert" else self.fake(command, **options))
                self.assertFalse(self.log.sync()["ok"])
                self.assertTrue(self.log.show()["runs"]["run-1"]["pending"])
                self.assertNotIn("fingerprint", self.log.state()["runs"]["run-1"]["sync"])

    def test_attempt_history_and_explicit_resume(self):
        self.initialize()
        first = self.start(intent="基线", change="无")
        self.assertEqual(first["status"], "running")
        self.assertEqual(self.start(), first)
        with self.assertRaisesRegex(ops.PlanError, "活动 Run"):
            self.start("run-2")
        self.log.run(run_id="run-1", patch={"status": "failed", "error": "OOM"})
        second = self.start("run-2")
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(self.log.state()["runs"]["run-1"]["error"], "OOM")
        with self.assertRaises(ops.PlanError):
            self.log.run(run_id="run-1", patch={"status": "running"})
        self.assert_plan_unchanged()

    def test_metrics_need_evidence_and_never_guessed(self):
        self.initialize()
        self.start()
        for patch in ({"metric_value": 0.8}, {"metric_value": float("nan")},
                      {"metric_value": True}, {"human_decision": "approved"}, {"run_id": "new"}):
            with self.assertRaises(ops.PlanError):
                self.log.run(run_id="run-1", patch=patch)
        self.log.run(run_id="run-1", patch={"metric_name": "accuracy", "metric_value": 0.8,
                                            "evidence": "ssh-a:/root/autodl-fs/results/metrics.json", "status": "done"})
        self.assertTrue(self.log.sync()["ok"])
        self.assertEqual(self.fake.records[0]["fields"]["主指标值"], 0.8)
        self.assertNotIn("人工决策", self.fake.records[0]["fields"])
        self.log.run(run_id="run-1", patch={"metric_value": None})
        self.log.sync()
        self.assertIsNone(self.fake.records[0]["fields"]["主指标值"])

    def test_deduplication_unchanged_and_human_fields_preserved(self):
        self.initialize()
        self.start()
        self.log.sync()
        self.fake.records[0]["fields"]["人工决策"] = "保留"
        self.log.run(run_id="run-1", patch={})
        self.assertEqual(self.log.sync()["runs"][0]["status"], "unchanged")
        self.log.run(run_id="run-1", patch={"suggestion": "复现"})
        self.log.sync()
        self.assertEqual(len(self.fake.records), 1)
        self.assertEqual(self.fake.records[0]["fields"]["人工决策"], "保留")
        writes = [kw for cmd, kw in self.fake.calls if cmd == "+record-upsert"]
        self.assertNotIn("record_id", writes[0])
        self.assertEqual(writes[1]["record_id"], "rec1")
        self.assertNotIn("fields", writes[0]["json"])
        self.assertFalse(self.log.show()["runs"]["run-1"]["pending"])

    def test_lost_create_response_recovers_by_query(self):
        self.initialize()
        self.start()
        def lost():
            raise ops.PlanError("写入响应丢失")
        self.fake.after_write = lost
        self.assertFalse(self.log.sync()["ok"])
        self.assertEqual(len(self.fake.records), 1)
        self.fake.after_write = None
        self.assertTrue(self.log.sync()["ok"])
        self.assertEqual(len(self.fake.records), 1)

    def test_duplicate_remote_rows_block_write(self):
        self.initialize()
        self.start()
        self.fake.records = [{"record_id": rid, "fields": {"Run ID": "run-1"}} for rid in ("a", "b")]
        result = self.log.sync()
        self.assertFalse(result["ok"])
        self.assertIn("冲突", result["runs"][0]["error"])
        self.assertFalse(any(c == "+record-upsert" for c, _ in self.fake.calls))

    def test_failed_sync_does_not_mutate_plan_and_is_retried(self):
        self.initialize()
        self.start()
        self.fake.failure = "+record-upsert"
        self.assertFalse(self.log.sync()["ok"])
        self.assertTrue(self.log.show()["runs"]["run-1"]["pending"])
        self.fake.failure = None
        self.assertTrue(self.log.sync()["ok"])
        self.assert_plan_unchanged()

    def test_field_type_mapping_and_human_column_guard(self):
        self.initialize()
        self.start()
        config = self.log.config()
        config["lark"]["fields"] = {"intent": "fld5"}
        self.log.save_config(config)
        self.log.fields(config)
        self.fake.fields[0]["type"] = "formula"
        self.assertFalse(self.log.sync()["ok"])
        self.assertFalse(self.fake.records)

    def test_field_id_cannot_bypass_human_column_protection(self):
        self.initialize()
        self.start()
        config = self.log.config()
        config["lark"]["fields"] = {"suggestion": "fld-human"}
        self.log.save_config(config)
        self.fake.fields.append({"id": "fld-human", "name": "人工决策", "type": "text"})
        self.assertFalse(self.log.sync()["ok"])
        self.assertFalse(self.fake.records)

    def test_concurrent_update_is_pending_not_lost(self):
        self.initialize()
        self.start()
        def update():
            self.log.run(run_id="run-1", patch={"suggestion": "新结论"})
        self.fake.after_write = update
        self.log.sync()
        self.assertTrue(self.log.show()["runs"]["run-1"]["pending"])
        self.assertEqual(self.log.state()["runs"]["run-1"]["suggestion"], "新结论")
        self.fake.after_write = None
        self.log.sync()
        self.assertFalse(self.log.show()["runs"]["run-1"]["pending"])

    def test_sync_mutex_and_network_outside_data_and_plan_locks(self):
        self.initialize()
        self.start()
        real_call = self.fake
        def call(command, **options):
            # 配置文件只有一把锁：网络期间它必须能随时拿到，否则巡检会被飞书拖住。
            with self.store.lock():
                self.store.read()
            return real_call(command, **options)
        self.log.call = call
        self.assertTrue(self.log.sync()["ok"])
        with self.log.lock("sync"):
            with self.assertRaisesRegex(ops.PlanError, "已在进行"):
                self.log.sync()

    def test_extra_metrics_declarations_create_columns_not_results(self):
        result = self.log.init(settings={"extra_metrics": {"Boundary_F1": None}})
        created = next(options for command, options in self.fake.calls if command == "+base-create")
        self.assertEqual((created["name"], created["table_name"]), ("真实实验 · 实验日志", "Run 记录"))
        metric_field = next(field for field in created["fields"]
                            if field["name"] == "extra_metrics.Boundary_F1")
        self.assertEqual((metric_field["name"], metric_field["type"]),
                         ("extra_metrics.Boundary_F1", "number"))
        self.assertIn("description", metric_field)
        self.assertTrue(result["ok"])
        self.assertNotIn("extra_metrics", self.start())
        self.log.init(settings={"extra_metrics": {"Topology_Error": None}})
        self.assertEqual(set(self.log.config()["extra_metrics"]), {"Boundary_F1", "Topology_Error"})
        self.assertEqual(sum(command == "+base-create" for command, _ in self.fake.calls), 1)
        self.assertTrue(all(command.startswith("+") for command, _ in self.fake.calls))
        self.assert_plan_unchanged()

    def test_extra_metrics_merge_clear_deduplicate_and_reload(self):
        self.initialize()
        self.start(extra_metrics={"Foreground_IoU": 0.91, "Boundary_F1": 0.85}, evidence="metrics.json")
        self.assertTrue(self.log.sync()["ok"])
        self.assertEqual(self.fake.records[0]["fields"]["extra_metrics.Boundary_F1"], 0.85)
        self.log.run(run_id="run-1", patch={"extra_metrics": {"Boundary_F1": None, "Topology_Error": 0}})
        self.assertTrue(self.log.show()["runs"]["run-1"]["pending"])
        self.assertTrue(self.log.sync()["ok"])
        fields = self.fake.records[0]["fields"]
        self.assertEqual(fields["extra_metrics.Foreground_IoU"], 0.91)
        self.assertIsNone(fields["extra_metrics.Boundary_F1"])
        self.assertEqual(fields["extra_metrics.Topology_Error"], 0)
        self.assertEqual(self.log.sync()["runs"][0]["status"], "unchanged")
        reloaded = ops.ExperimentLog(self.store).show()
        self.assertFalse(reloaded["runs"]["run-1"]["pending"])
        self.assertEqual(reloaded["runs"]["run-1"]["extra_metrics"]["Foreground_IoU"], 0.91)
        self.assert_plan_unchanged()

    def test_invalid_extra_metrics_and_evidence_are_atomic(self):
        self.initialize()
        self.start()
        before = self.store.path.read_bytes()
        for metrics in (None, [], {"": 1}, {" x": 1}, {"x\n": 1}, {"x" * 101: 1},
                        {"x": True}, {"x": 10 ** 400}, {"x": "0.8"}, {"x": float("nan")}, {"x": float("inf")},
                        {"access_token": 1}, {"x": 0.8}):
            with self.subTest(metrics=metrics), self.assertRaises(ops.PlanError):
                self.log.run(run_id="run-1", patch={"extra_metrics": metrics})
            self.assertEqual(self.store.path.read_bytes(), before)
        self.log.run(run_id="run-1", patch={"extra_metrics": {"x": 1}, "evidence": "eval.json"})
        before = self.store.path.read_bytes()
        with self.assertRaises(ops.PlanError):
            self.log.run(run_id="run-1", patch={"evidence": None})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.log.run(run_id="run-1", patch={"extra_metrics": {"x": None}, "evidence": None})
        for settings in ({"extra_metrics": {"x": 0.8}}, {"defaults": {"extra_metrics": {"x": None}}},
                         {"lark": {"fields": {"intent": "extra_metrics.x"}}}):
            with self.assertRaises(ops.PlanError):
                self.log.init(settings=settings)

    def test_historical_extra_metrics_union_and_type_conflicts(self):
        self.start(extra_metrics={"old": 0.5}, evidence="eval.json")
        self.log.run(run_id="run-1", patch={"status": "done"})
        self.start(run_id="run-2", extra_metrics={"new": 0.7}, evidence="eval.json")
        self.initialize()
        names = [field["name"] for field in self.fake.fields]
        self.assertIn("extra_metrics.old", names)
        self.assertIn("extra_metrics.new", names)
        self.fake.fields = [field for field in self.fake.fields if field["name"] == "extra_metrics.new"
                            or not field["name"].startswith("extra_metrics.")]
        self.assertTrue(self.log.sync("run-2")["ok"])
        self.assertIn("extra_metrics.old", [field["name"] for field in self.fake.fields])
        self.assertNotIn("extra_metrics.old", self.fake.records[0]["fields"])
        next(field for field in self.fake.fields if field["name"] == "extra_metrics.new")["type"] = "text"
        self.assertFalse(self.log.sync("run-2")["ok"])

    def test_dynamic_field_partial_failure_recovers_without_duplicates(self):
        self.initialize()
        self.start(extra_metrics={"a": 1, "b": 2}, evidence="eval.json")
        fake = self.fake
        def partial(command, **options):
            if command == "+field-create":
                fake.fields.append(copy.deepcopy(options["json"][0]))
                raise ops.PlanError("字段创建响应丢失")
            return fake(command, **options)
        self.log.call = partial
        self.assertFalse(self.log.sync()["ok"])
        self.assertFalse(fake.records)
        self.log.call = fake
        self.assertTrue(self.log.sync()["ok"])
        names = [field["name"] for field in fake.fields]
        self.assertEqual(names.count("extra_metrics.a"), 1)
        self.assertEqual(names.count("extra_metrics.b"), 1)
        self.assert_plan_unchanged()

    def test_dynamic_field_duplicate_and_id_mapping_protection(self):
        self.initialize()
        self.start(extra_metrics={"x": 1}, evidence="eval.json")
        self.fake.fields.extend([{"name": "extra_metrics.x", "type": "number"}] * 2)
        self.assertFalse(self.log.sync()["ok"])
        self.assertFalse(self.fake.records)
        self.fake.fields = [field for field in self.fake.fields if field["name"] != "extra_metrics.x"]
        self.fake.fields.append({"id": "fld-dynamic", "name": "extra_metrics.x", "type": "text"})
        config = self.log.config()
        config["lark"]["fields"] = {"intent": "fld-dynamic"}
        self.log.save_config(config)
        self.assertFalse(self.log.sync()["ok"])
        self.assertFalse(self.fake.records)

    def test_init_sync_and_target_switch_without_event_subscription(self):
        result = self.initialize()
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertNotIn("subscription", result)
        self.assertNotIn("warnings", result)
        self.assertNotIn("subscription", self.log.state())
        self.start()
        synced = self.log.sync()
        self.assertTrue(synced["ok"])
        self.assertNotIn("subscription", synced)
        self.assertNotIn("subscription", self.log.show())
        result = self.log.init(url="https://example.feishu.cn/base/existing?table=tbl-existing")
        self.assertTrue(result["ok"])
        self.assertTrue(self.log.sync()["ok"])
        self.assertEqual(result["config"]["lark"]["base_token"], "base-existing")
        self.assertTrue(all(command.startswith("+") for command, _ in self.fake.calls))
        self.assertEqual(sum(command == "+base-create" for command, _ in self.fake.calls), 1)
        self.assert_plan_unchanged()

    def test_legacy_subscription_state_is_ignored_and_preserved(self):
        for status in ("pending", "subscribed"):
            with self.subTest(status=status):
                legacy = {"base_token": "base-old", "status": status, "error": "旧订阅错误"}
                def save(document):
                    document.setdefault("experiments", ops.default_state())["subscription"] = legacy
                self.store.update(save)
                self.assertTrue(self.initialize()["ok"])
                if not self.log.state()["runs"]:
                    self.start()
                result = self.log.sync()
                self.assertTrue(result["ok"])
                self.assertNotIn("subscription", result)
                self.assertNotIn("subscription", self.log.show())
                self.assertEqual(self.log.state()["subscription"], legacy)
                self.assert_plan_unchanged()

    def test_init_cli_fatal_errors_still_exit_nonzero(self):
        for failure in ("+base-create", "+field-list", "config"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                store = ops.Store(Path(temp))
                store.create(ops.new_plan("初始化失败"))
                log = ops.ExperimentLog(store)
                fake = FakeLark()
                fake.failure = failure
                log.call = fake
                if failure == "config":
                    store.path.write_text("{broken")
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(ops, "ExperimentLog", return_value=log), \
                        mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr):
                    code = ops.main(["-p", temp, "experiment", "init"])
                self.assertEqual(code, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("TrainOps:", stderr.getvalue())

    def test_call_is_argv_and_errors_do_not_echo_secrets(self):
        real = ops.ExperimentLog(self.store)
        response = subprocess.CompletedProcess([], 0, '{"ok":true,"data":{"record":{"id":"rec1"}}}', '')
        with mock.patch.object(ops.subprocess, "run", return_value=response) as run:
            real.call("+record-upsert", base_token="resource", table_id="tbl1", json={"实验意图": "x; touch /tmp/no"})
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["lark-cli", "base", "+record-upsert"])
        self.assertEqual(argv[argv.index("--as") + 1], "user")
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(json.loads(argv[argv.index("--json") + 1])["实验意图"], "x; touch /tmp/no")
        for response in (subprocess.CompletedProcess([], 1, '{"access_token":"SECRET"}', 'SECRET'),
                         subprocess.CompletedProcess([], 0, '{"ok":false,"error":"SECRET"}', 'SECRET'),
                         subprocess.CompletedProcess([], 0, 'SECRET', '')):
            with mock.patch.object(ops.subprocess, "run", return_value=response):
                with self.assertRaises(ops.PlanError) as exc:
                    real.call("+field-list")
            self.assertNotIn("SECRET", str(exc.exception))

    def test_field_and_record_pagination(self):
        self.initialize()
        self.start()
        fake = self.fake
        def paginated(command, **options):
            if command == "+field-list":
                start = options["offset"]
                return {"fields": fake.fields[start:start+12], "has_more": start + 12 < len(fake.fields)}
            if command == "+record-list":
                return {"records": [{"record_id": "a", "fields": {"Run ID": "run-1"}}], "has_more": True}
            return fake(command, **options)
        self.log.call = paginated
        result = self.log.sync()
        self.assertFalse(result["ok"])
        self.assertIn("冲突", result["runs"][0]["error"])


class SubprocessTests(unittest.TestCase):
    def test_start_without_run_id_generates_and_persists_unique_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            def cli(*args, code=0):
                result = subprocess.run([sys.executable, str(SCRIPT), "-p", temp, *args],
                                        text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                return json.loads(result.stdout)

            cli("init", "--name", "测试", "--flow", "基线", "--task", "F01:训练")
            run_ids = []
            for _ in range(2):
                result = cli("experiment", "start", "--task", "F01-T01", code=1)
                self.assertTrue(result["local_saved"])
                run_id = result["run"]["run_id"]
                self.assertRegex(run_id, r"^[0-9a-f]{32}$")
                shown = cli("experiment", "show")
                self.assertEqual(shown["runs"][run_id]["task_id"], "F01-T01")
                cli("experiment", "update", run_id, "--json", '{"status":"done"}', code=1)
                run_ids.append(run_id)
            self.assertEqual(len(set(run_ids)), 2)
            self.assertEqual(set(cli("experiment", "show")["runs"]), set(run_ids))

    def test_start_unknown_field_reports_actionable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ops.Store(Path(temp))
            plan = ops.new_plan("测试")
            ops.add_flow(plan, "基线")
            ops.add_task(plan, "F01", "训练")
            store.create(plan)
            original = store.path.read_bytes()
            result = subprocess.run([
                sys.executable, str(SCRIPT), "-p", temp, "experiment", "start",
                "--task", "F01-T01", "--json", '{"notes":"说明"}'],
                text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("'notes'", result.stderr)
            self.assertIn(f"允许字段: [{', '.join(sorted(ops.RUN_UPDATE))}]", result.stderr)
            self.assertEqual(store.path.read_bytes(), original)

    def test_fake_cli_end_to_end_without_real_auth(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bindir = root / "bin"
            bindir.mkdir()
            executable = bindir / "lark-cli"
            # 每次调用独立进程；状态在测试临时目录，不触碰用户 HOME 或真实授权。
            executable.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
root = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
command = args[1]
def arg(name): return args[args.index(name)+1]
state = root / "remote.json"
rows = json.loads(state.read_text()) if state.exists() else []
if command == "+base-create":
    (root / "fields.json").write_text(arg("--fields"))
    data = {"base_token":"base-test", "table_id":"tbl-test", "url":"https://example.feishu.cn/base/base-test?table=tbl-test"}
elif command == "+field-create":
    fields = root / "fields.json"
    fields.write_text(json.dumps(json.loads(fields.read_text()) + json.loads(arg("--json"))))
    data = {}
elif command == "+field-list": data = {"fields":json.loads((root / "fields.json").read_text()), "has_more":False}
elif command == "+record-list":
    wanted = json.loads(arg("--filter-json"))["conditions"][0][2]
    matches = [r for r in rows if r["fields"]["Run ID"] == wanted]
    data = {"record_id_list":[r["record_id"] for r in matches],
            "data":[[r["fields"]["Run ID"]] for r in matches], "has_more":False}
elif command == "+record-upsert":
    if (root / "fail").exists(): sys.exit(3)
    payload = json.loads(arg("--json"))
    if "--record-id" in args:
        row = next(r for r in rows if r["record_id"] == arg("--record-id"))
        row["fields"].update(payload)
    else:
        row = {"record_id":"rec"+str(len(rows)+1), "fields":payload}
        rows.append(row)
    state.write_text(json.dumps(rows))
    data = {"created":"--record-id" not in args, "record":{"record_id_list":[row["record_id"]]}}
else: sys.exit(4)
print(json.dumps({"ok":True, "data":data}))
''')
            executable.chmod(0o755)
            env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"]}
            project = root / "project"
            def cli(*args, code=0):
                result = subprocess.run([sys.executable, str(SCRIPT), "-p", str(project), *args],
                                        env=env, text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                return json.loads(result.stdout)
            cli("init", "--name", "测试", "--flow", "基线", "--task", "F01:训练")
            config_file = project / ".agent/experiment.yaml"
            original = json.loads(config_file.read_text())["plan"]
            # Initialization must succeed under bash -e without an event API.
            result = subprocess.run([
                "bash", "-e", "-c",
                '\"$1\" \"$2\" -p \"$3\" experiment init; printf "continued\\n"',
                "test-init", sys.executable, str(SCRIPT), str(project)],
                env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            lines = result.stdout.splitlines()
            self.assertEqual(lines[-1], "continued")
            initialized = json.loads("\n".join(lines[:-1]))
            self.assertTrue(initialized["ok"])
            self.assertTrue(initialized["verified"])
            self.assertNotIn("warnings", initialized)
            self.assertNotIn("subscription", initialized)
            self.assertEqual(initialized["config"]["lark"]["base_token"], "base-test")
            recovered = cli("experiment", "init", "--json", '{"extra_metrics":{"Boundary_F1":null}}')
            self.assertNotIn("warnings", recovered)
            self.assertTrue(recovered["ok"])
            self.assertNotIn("subscription", recovered)
            cli("experiment", "start", "--task", "F01-T01", "--run-id", "run-a", "--json", '{"intent":"基线"}')
            (bindir / "fail").touch()
            failed = cli("experiment", "update", "run-a", "--json", '{"status":"failed","error":"OOM"}', code=1)
            self.assertTrue(failed["local_saved"])
            self.assertFalse(failed["ok"])
            (bindir / "fail").unlink()
            cli("experiment", "update", "run-a", "--json",
                '{"extra_metrics":{"Boundary_F1":0.85,"Topology_Error":0.03},"evidence":"metrics.json"}')
            cli("experiment", "sync")
            remote = json.loads((bindir / "remote.json").read_text())[0]["fields"]
            self.assertEqual(remote["extra_metrics.Boundary_F1"], 0.85)
            self.assertEqual(remote["extra_metrics.Topology_Error"], 0.03)
            shown = cli("experiment", "show")
            self.assertFalse(shown["runs"]["run-a"]["pending"])
            self.assertEqual(shown["runs"]["run-a"]["status"], "failed")
            self.assertEqual(len(json.loads((bindir / "remote.json").read_text())), 1)
            self.assertEqual(json.loads(config_file.read_text())["plan"], original)


if __name__ == "__main__":
    unittest.main()
