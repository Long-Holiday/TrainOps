"""Antigravity 插件清单与入口文件的结构检查。

这些清单不在计划账本的数据流上，CLI 的输出路径碰不到它们，所以单独盯：
插件根的清单必须能被 Antigravity 认出、引用的路径必须存在且不越出插件根，
入口文件（`rules/trainops.md`）只做指针，不复制细则。

这个插件**只服务 Antigravity CLI**：别家的清单与入口文件不该再出现（有回归用例盯着）。
外部校验器（`agy`）装了就用它验一遍，没装跳过而不是失败。
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills/trainops"
SKILL = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "scripts/trainops.py"
REFERENCES = SKILL_DIR / "references"
TRAINPILOT = REFERENCES / "trainpilot.md"

ANTIGRAVITY_SCHEMA = "https://antigravity.google/schemas/v1/plugin.json"
ANTIGRAVITY_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")
PLUGIN_NAME = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
# 插件根的清单是唯一的清单——这些路径一旦复活，说明又长出了别家的打包方式。
OTHER_AGENT_PATHS = (".claude-plugin", ".codex-plugin", "gemini-extension.json", "GEMINI.md",
                     "AGENTS.md", "antigravity", "CLAUDE.md")
ENTRY_FILES = (ROOT / "rules/trainops.md",)
ENTRY_LIMIT = 40  # 入口文件只做指针，细则留在 SKILL.md；超了说明有人在写第二份契约
# 入口只保留能直接指导行为的关键合同；同一句约束不要求在所有文档逐字复读。
INVARIANTS = ("唯一事实来源", "直接编辑", "缺数据就缺结论", "请求决策")
# 计划不再被写成“只能经 CLI 修改”；程序内的写入口与 agent 直接编辑是两件事。
WRITE_PROHIBITIONS = ("只过 CLI", "只经 CLI 修改")
# 否定式提及（如“不调用 ack_plan”“不要求 #fix”）不是仍在启用旧协议。
LEGACY_PLAN_QUEUE = re.compile(r"next_task_plans|plan_id")
LEGACY_ACK_CALL = re.compile(r"(?<!不)调用\s*`?ack_plan")
GROUP_PULL = re.compile(r"(?<!不)(?:拉取|读取|轮询)[^。\n]{0,8}群消息")
FIX_TAG = re.compile(r"(?<!不)要求\s*#fix")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read(path):
    return Path(path).read_text(encoding="utf-8")


def frontmatter(path):
    """只解析 SKILL.md 顶部的简单 frontmatter：这里是 name/description 级别的键值，不引 YAML。"""
    lines = read(path).splitlines()
    if not lines or lines[0].strip() != "---":
        raise AssertionError(f"{path} 缺少 frontmatter 起始行 ---")
    keys = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return keys
        key, sep, value = line.partition(":")
        if sep:
            keys[key.strip()] = value.strip().strip("'\"")
    raise AssertionError(f"{path} 的 frontmatter 没有结束行 ---")


def slug(heading):
    """按 GitHub 的口径粗算锚点：小写、去掉标点、空格转连字符；中文按原样保留。"""
    text = heading.strip().lower()
    text = re.sub(r"[^\w一-鿿\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def links(path):
    """列出正文里的相对链接（含 #锚点），跳过外部 URL 与代码块里的内容。"""
    body = read(path)
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    body = re.sub(r"`[^`]*`", "", body)
    return [target for target in re.findall(r"\]\(([^)\s]+)\)", body)
            if not re.match(r"^[a-z]+://|^mailto:|^#", target)]


class ManifestTests(unittest.TestCase):
    def test_antigravity_manifest_is_named_and_versioned(self):
        manifest = load(ROOT / "plugin.json")
        self.assertEqual(manifest["$schema"], ANTIGRAVITY_SCHEMA,
                         "插件根的 $schema 必须是 Antigravity 的清单地址，写错会被拒绝加载")
        self.assertRegex(manifest["name"], ANTIGRAVITY_NAME,
                         "Antigravity 的插件名只能用字母数字与 - _")
        description = manifest["description"].strip()
        self.assertTrue(description, "清单 description 是对外唯一的说明，不能为空")
        for phrase in WRITE_PROHIBITIONS:
            self.assertNotIn(phrase, description,
                             "清单不应再把计划写成只能经 CLI 修改；直接编辑是允许的")
        self.assertIn("直接编辑", description, "清单应说明允许校验通过的直接编辑")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$",
                         "插件版本用三段式；它和 `trainops.py --version` 的计划 schema 版本是两件事")

    def test_only_antigravity_packaging_exists(self):
        """只服务 Antigravity CLI：别家的清单与入口文件都不该再出现。"""
        for name in OTHER_AGENT_PATHS:
            self.assertFalse((ROOT / name).exists(),
                             f"插件只服务 Antigravity，不该再有 {name}（要加回来先想清楚为谁加）")

    def test_skills_directory_only_exposes_immediate_children(self):
        skills = ROOT / "skills"
        self.assertTrue(skills.is_dir(), "Antigravity 规定技能放 skills/<名称>/SKILL.md")
        found = sorted(child.name for child in skills.iterdir() if (child / "SKILL.md").is_file())
        self.assertEqual(found, ["trainops"], "skills/ 的直接子目录才是技能，深层目录不会被搜索")
        self.assertFalse(skills.is_symlink(), "技能目录要留在插件根里，别再拿软链绕一层")
        self.assertTrue((skills / "trainops/SKILL.md").is_file(), "SKILL.md 必须是普通文件而不是链接")

    def test_rules_directory_holds_the_entry_file(self):
        rules = sorted(path.name for path in (ROOT / "rules").glob("*.md"))
        self.assertEqual(rules, ["trainops.md"],
                         "插件根的 rules/ 是 Antigravity 载入规则的唯一位置，只放入口这一份")


class SkillTests(unittest.TestCase):
    def test_frontmatter_matches_directory_and_limits(self):
        meta = frontmatter(SKILL)
        self.assertEqual(meta["name"], SKILL_DIR.name, "技能名必须与目录名一致，否则客户端找不到")
        self.assertRegex(meta["name"], PLUGIN_NAME, "技能名只允许小写字母数字与单连字符")
        self.assertLessEqual(len(meta["name"]), 64)
        self.assertTrue(meta["description"].strip(), "description 是技能被检索到的唯一依据，不能为空")
        self.assertLessEqual(len(meta["description"]), 1024, "description 上限 1024 字符")

    def test_links_and_anchors_resolve(self):
        for path in [SKILL, *sorted(REFERENCES.glob("*.md")), ROOT / "README.md", *ENTRY_FILES]:
            for target in links(path):
                name, _, anchor = target.partition("#")
                target_path = (path.parent / name).resolve() if name else path
                self.assertTrue(target_path.exists(),
                                f"{path.relative_to(ROOT)} 的链接指向不存在的位置：{target}")
                if anchor:
                    headings = re.findall(r"^#{1,6}\s+(.+)$", read(target_path), re.M)
                    self.assertIn(anchor, [slug(h) for h in headings],
                                  f"{path.relative_to(ROOT)} 的锚点找不到对应标题：{target}")

    def test_script_depth_is_stable(self):
        """脚本与插件根之间固定三层：脚本靠自身位置回推 references/ 与 web/ 面板。"""
        spec = importlib.util.spec_from_file_location("trainops_layout", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(SCRIPT.resolve().parents[3], ROOT,
                         "trainops.py 必须位于 <插件根>/skills/trainops/scripts/ 下")
        page = SCRIPT.resolve().parent.parent / "web" / "index.html"
        self.assertTrue(page.is_file(), f"面板必须还在 {page.relative_to(ROOT)}")

    def test_entry_keeps_panel_resident_and_no_gpu_default(self):
        """开工默认路线：面板后台常驻，先无卡准备再申请有卡。"""
        body = read(SKILL)
        for phrase in ("后台常驻任务", "serve --port 8765 --interval 30", "别用 `&` 或前台"):
            self.assertIn(phrase, body, "入口必须要求面板以后台常驻任务起，不能靠 shell 的 & 或前台")
        for phrase in ("power_on_no_gpu", "0.5 核 CPU、2GB 内存、无 GPU"):
            self.assertIn(phrase, body, "入口必须把无卡准备写成默认路线并说明资源上限")

    def test_entry_pins_failure_escalation(self):
        """故障口径：自愈有上限，第 5 次后先关机止付。"""
        body = read(SKILL)
        for phrase in ("最多 5 次", "关机止付", "failed", "blocked"):
            self.assertIn(phrase, body, "入口必须写死自愈上限与关机止付出口")
        protocol = read(REFERENCES / "protocol.md")
        for phrase in ("先报告", "最多自愈 5 次", "5 次是上限不是配额",
                       "预授权", "power_off", "instance_status"):
            self.assertIn(phrase, protocol, "恢复协议必须保留自愈与止付关机的安全边界")

    def test_plan_editing_allows_direct_edit_with_validation(self):
        """计划推荐 CLI，但允许 agent 直接编辑；两条路径都要保留校验与并发保护。"""
        skill = read(SKILL)
        for phrase in ("apply --from-json - --brief", "aliases", "deps: []", "--dry-run",
                       "直接编辑", "先读最新文档并备份", "check --strict"):
            self.assertIn(phrase, skill, "入口必须同时给出标准化 JSON 与可校验的直接编辑路径")
        reference = read(REFERENCES / "cli.md")
        for phrase in ("推荐 CLI", "直接编辑", "原子替换", "保留已有任务/实例/流程编号",
                       "check --strict", "不通过编辑绕过运行中改派保护"):
            self.assertIn(phrase, reference, "CLI 参考必须保留直接编辑的锁、原子写与校验要求")
        for phrase in ("JSON 不自动串联", "前向引用", "deps: []", "--dry-run", "aliases"):
            self.assertIn(phrase, reference, "CLI 参考必须保留结构化输入的边界")

    def test_trainpilot_uses_direct_dialogue_without_remote_plan_queue(self):
        """TrainPilot 不再承接下一任务计划或群消息方案；否定式提及不算启用。"""
        skill = read(SKILL)
        reference = read(TRAINPILOT)
        for name, document in (("SKILL.md", skill), ("trainpilot.md", reference)):
            with self.subTest(document=name):
                self.assertNotRegex(document, LEGACY_PLAN_QUEUE,
                                    "文档不应再维护 next_task_plans/plan_id 远端计划队列")
                self.assertNotRegex(document, LEGACY_ACK_CALL,
                                    "文档不应再指示调用 ack_plan")
                self.assertNotRegex(document, GROUP_PULL,
                                    "文档不应再指示从群消息拉取方案")
                self.assertNotRegex(document, FIX_TAG,
                                    "文档不应再要求 #fix")
                self.assertIn("直接对话", document, "新任务与等待中的方案应回到用户直接对话")
        self.assertIn("不接收飞书下一步计划", skill)
        self.assertIn("不接收或返回飞书下一步计划", reference)
        self.assertIn("不调用 ack_plan", reference)
        self.assertIn("不拉取群消息", reference)
        self.assertIn("不要求 #fix", reference)
        self.assertIn("保持当前现场", reference)
        self.assertIn("取消任务并关机", reference)

    def test_single_recovery_ack_and_terminal_reports(self):
        """终态直接上报；自行修复只用一次 ACK 恢复汇报，不重复 milestone。"""
        execution = read(REFERENCES / "execution.md")
        for phrase in ("终态直接 report completed/failed", "ACK 合并汇报",
                       "不重复发恢复 milestone", "通知不可用", "不阻断安全处置"):
            self.assertIn(phrase, execution, "通知闭环必须保留单次恢复 ACK 与安全处置优先")
        skill = read(SKILL)
        self.assertIn("修复结果只汇报一次", skill)
        self.assertIn("重大结果及时通知", skill)
        reference = read(TRAINPILOT)
        self.assertIn("唯一恢复汇报", reference)
        self.assertIn("直接报告终态", reference)

    def test_fixed_cron_and_monitoring_are_explicit(self):
        """启动训练后实际登记整点/半点巡检，而不是只承诺监控。"""
        skill = read(SKILL)
        for phrase in ('CronExpression="0,30 * * * *"', "schedule(", "整点半点",
                       "IsDaemon=false", "用户另定周期则遵从"):
            self.assertIn(phrase, skill, "入口必须给出固定巡检的实际调度写法")
        execution = read(REFERENCES / "execution.md")
        for phrase in ("整点/半点宿主调度任务", "不得只承诺定时检查", "新会话先核实已有调度"):
            self.assertIn(phrase, execution, "巡检要求必须落在实际调度与接管核实上")

    def test_experiment_logging_is_wired(self):
        skill = read(SKILL)
        for phrase in ("references/experiments.md", "experiment init", "experiment start",
                       "experiment update", "experiment sync", "未指定则自动创建",
                       "飞书失败不阻断", "先读后写"):
            self.assertIn(phrase, skill, "入口必须保留实验日志的主动初始化与先读后写")
        reference = read(REFERENCES / "experiments.md")
        for phrase in (".agent/experiment.yaml", "lark-cli", "先读后写", "Run 记录",
                       "JSON 兼容 YAML", "人工决策", "extra_metrics",
                       "不替代 MCP `trainpilot`"):
            self.assertIn(phrase, reference, "实验日志参考必须保留 Run 留痕与同步边界")

    def test_execution_chain_keeps_sync_and_cleanup(self):
        reference = read(REFERENCES / "execution.md")
        steps = ("1. **核实目标与工作区**", "2. **本地修改与验证**",
                 "3. **提交并推送**", "4. **远端拉取并核对**", "5. **可恢复启动**")
        self.assertEqual(sorted(reference.index(step) for step in steps),
                         [reference.index(step) for step in steps],
                         "执行闭环必须保持核实→修改→推送→远端核对→启动的顺序")
        for phrase in ("不在服务器直接修改受版本管理的代码", "不强制推送",
                       "source /etc/network_turbo", "tmux/screen",
                       "不向 `plan` 塞命令和日志", "任务完成或用户取消",
                       "定时巡检、资源等待唤醒", "cancel_job", "不等于关机",
                       "实例最终状态", "任务面板可以保留", "不清理其他任务的调度"):
            self.assertIn(phrase, reference)

    def test_autodl_reference_keeps_safety_boundaries(self):
        """只读等待、取消语义与费用边界留在 AutoDL 参考，不要求所有文档复读。"""
        reference = read(REFERENCES / "autodl.md")
        for phrase in ("0.5 核 CPU、2GB 内存、无 GPU", "power_on_no_gpu",
                       "wait_for_job(job_id, timeout_seconds=15)", "0–300",
                       "switch_mode", "cancel_job", "resulting_status",
                       "list_instances", "instance_uuids", "1–100", "5 路并发",
                       "suggested_next_poll_seconds", "定时调度器",
                       "price: null", "不代表免费", "不能用切成无卡代替关机"):
            self.assertIn(phrase, reference)
        self.assertNotIn("next_retry_in_seconds", reference)

    def test_guidance_does_not_require_event_subscription(self):
        for path in (SKILL, REFERENCES / "experiments.md", REFERENCES / "protocol.md",
                     ROOT / "rules/trainops.md"):
            body = read(path)
            for phrase in ("自动订阅", "get_subscribe", "docs:event:subscribe"):
                self.assertNotIn(phrase, body)

    def test_readme_contracts_are_current(self):
        """README 面向用户，必须与当前合同一致，不能留着旧禁令或旧预算说法。"""
        body = read(ROOT / "README.md")
        for phrase in ("apply --from-json", "agent 也可先读后写直接编辑", "check --strict",
                       "计划写入口", "面板只额外允许时价接口",
                       "零第三方 Python 依赖", "只服务 Antigravity CLI",
                       "只用原生 HTML/CSS/JS", "node tests/test_web.cjs",
                       "只存在面板进程的内存里",
                       "科学对照实验保留变量控制", "工程修复可按根因成组调整"):
            self.assertIn(phrase, body, f"README 缺少当前合同：{phrase}")
        for phrase in ("2000 字符预算", "禁止用 Edit/Write", "一次只改一个变量", *WRITE_PROHIBITIONS):
            self.assertNotIn(phrase, body, f"README 仍保留过期说法：{phrase}")


class EntryFileTests(unittest.TestCase):
    def test_entry_files_are_thin_pointers(self):
        for path in ENTRY_FILES:
            body = read(path)
            where = path.relative_to(ROOT)
            self.assertIn("skills/trainops/SKILL.md", body, f"{where} 必须指向能力契约 SKILL.md")
            self.assertLessEqual(len(body.splitlines()), ENTRY_LIMIT,
                                 f"{where} 太长：入口文件只做指针，细则写在 SKILL.md 里")
            self.assertNotRegex(body, r"/home/|/Users/|[A-Z]:\\|/tmp/",
                                f"{where} 里不能出现绝对路径，否则换台机器就失效")
            for phrase in INVARIANTS:
                self.assertIn(phrase, body, f"{where} 缺少关键合同「{phrase}」")
            for phrase in WRITE_PROHIBITIONS:
                self.assertNotIn(phrase, body, f"{where} 不应再保留只许 CLI 的绝对禁令")


@unittest.skipUnless(shutil.which("agy"), "未安装 Antigravity CLI，跳过官方校验器")
class AntigravityValidatorTests(unittest.TestCase):
    def test_official_validator_accepts_plugin(self):
        result = subprocess.run([shutil.which("agy"), "plugin", "validate", str(ROOT),
                                 "--strict", "--json"], text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"agy plugin validate 未通过：{result.stdout}{result.stderr}")
        report = json.loads(result.stdout)
        self.assertEqual(report["findings"], [], "agy 对插件报了问题")
        self.assertEqual(report["components"]["skills"], 1, "Antigravity 没在插件根找到技能")
        self.assertEqual(report["components"]["rules"], 1,
                         "插件根的 rules/ 没被认成规则，Antigravity 就读不到入口那三条不变量")

    def test_install_into_sandbox_home_stages_plugin_root(self):
        """用临时 HOME 真装一遍：不碰用户自己的 Antigravity 配置，也不改本仓库。"""
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home}
            agy = shutil.which("agy")
            subprocess.run([agy, "plugin", "list", "--json"], text=True, capture_output=True,
                           timeout=120, env=env)
            if not (Path(home) / ".gemini/config").is_dir():
                self.skipTest("agy 不按 HOME 找配置目录，跳过安装用例以免污染本机")
            installed = subprocess.run([agy, "plugin", "install", str(ROOT)],
                                       text=True, capture_output=True, timeout=180, env=env)
            self.assertEqual(installed.returncode, 0,
                             f"agy plugin install 失败：{installed.stdout}{installed.stderr}")
            target = Path(home) / ".gemini/config/plugins/trainops"
            self.assertTrue((target / "skills/trainops/SKILL.md").is_file(),
                            f"装完没带上技能：{sorted(p.name for p in target.iterdir())}")
            self.assertTrue((target / "rules/trainops.md").is_file(), "装完没带上规则入口")


if __name__ == "__main__":
    unittest.main()
