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

ANTIGRAVITY_SCHEMA = "https://antigravity.google/schemas/v1/plugin.json"
ANTIGRAVITY_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")
PLUGIN_NAME = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
# 插件根的清单是唯一的清单——这些路径一旦复活，说明又长出了别家的打包方式。
OTHER_AGENT_PATHS = (".claude-plugin", ".codex-plugin", "gemini-extension.json", "GEMINI.md",
                     "AGENTS.md", "antigravity", "CLAUDE.md")
ENTRY_FILES = (ROOT / "rules/trainops.md",)
ENTRY_LIMIT = 40  # 入口文件只做指针，细则留在 SKILL.md；超了说明有人在写第二份契约
# 三条不变量在入口文件里必须逐字一致，否则同一个约束会长出三种说法。
INVARIANTS = ("是唯一事实来源，只经 CLI 修改", "缺数据就缺结论", "改变目标、放弃实验或增加费用前先确认")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def frontmatter(path):
    """只解析 SKILL.md 顶部的简单 frontmatter：这里是 name/description 级别的键值，不引 YAML。"""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
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
    body = Path(path).read_text(encoding="utf-8")
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
        self.assertTrue(manifest["description"].strip(), "清单 description 是对外唯一的说明，不能为空")
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
    def test_entry_keeps_task_startup_order(self):
        """入口要写死开工顺序：起面板 + 默认无卡准备。

        这两步曾整体缺失：agent 装完插件后直接请求有卡开机，用户看不到面板也没有无卡准备。
        删掉任一条都算回归，所以在这里钉住；入口优先清晰简洁，不设字符硬门槛。
        """
        body = SKILL.read_text(encoding="utf-8")
        self.assertIn("## 开工顺序", body, "入口缺少开工顺序，agent 会跳过面板与无卡准备")
        self.assertIn("serve --port 8765 --interval 30", body, "入口必须给出面板启动命令")
        self.assertIn("power_on_no_gpu", body, "入口必须把无卡准备写成默认路线")

    def test_entry_requires_panel_as_resident_background_task(self):
        """面板要以后台任务常驻，不能靠 shell 的 `&`。

        早先入口只写「放后台」，对 harness 等于没写：调用方照样用 `&` 起，父 shell 一收尾
        面板就被带走，结算跟着停，用户页面失联。两句都钉住，删任一句都算回归。
        """
        body = SKILL.read_text(encoding="utf-8")
        self.assertIn("后台常驻任务", body, "入口必须要求面板以后台常驻任务起")
        self.assertIn("别用 `&` 或前台", body, "入口必须排除 shell 的 `&` 与前台两种起法")

    def test_entry_pins_failure_escalation(self):
        """故障口径：自愈有上限，第 5 次后先关机止付。

        用户给的是一条预授权：先报现象 → 自愈最多 5 次 → 仍无法继续就先关机止付。
        缺上限会变成无限重试（实例一直计费），缺关机就是没有出口，两句都钉住。
        """
        body = SKILL.read_text(encoding="utf-8")
        self.assertIn("最多 5 次", body, "入口必须写死自愈次数上限")
        self.assertIn("关机止付", body, "入口必须写明第 5 次仍无法继续就先关机")

    def test_entry_keeps_experiment_logging_in_monitoring_loop(self):
        body = SKILL.read_text(encoding="utf-8")
        for phrase in ("references/experiments.md", "experiment init", "experiment start",
                       "experiment update", "experiment sync", "未指定则自动创建", "飞书失败不阻断"):
            self.assertIn(phrase, body)
        reference = (SKILL_DIR / "references/experiments.md").read_text(encoding="utf-8")
        for phrase in (".agent/experiment.yaml", "lark-cli", "人工决策", "JSON 兼容 YAML 1.2",
                       "关机止付", "Run ID", "extra_metrics", "get_subscribe", "订阅注册不等于监听已运行", "全局概况"):
            self.assertIn(phrase, reference)

    def test_logging_init_precedes_preparation_and_training(self):
        body = SKILL.read_text(encoding="utf-8")
        self.assertLess(body.index("**面板**"), body.index("**日志初始化**"))
        self.assertLess(body.index("**日志初始化**"), body.index("**无卡准备**"))
        self.assertLess(body.index("**无卡准备**"), body.index("**训练与记录**"))
        for phrase in ("主动执行", "全局概况", "自动订阅", "extra_metrics", "订阅注册不等于监听已运行"):
            self.assertIn(phrase, body)

    def test_autodl_guidance_covers_specs_and_read_only_wait(self):
        body = SKILL.read_text(encoding="utf-8")
        reference = (SKILL_DIR / "references/autodl.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for document in (body, reference, readme):
            for phrase in ("instance_status", "gpu_name", "gpu_memory_gb", "cpu_cores",
                           "null", "单卡", "wait_for_job", "job_status", "phase"):
                self.assertIn(phrase, document)
        for phrase in ("timeout_seconds=300", "0–300", "1024³", "vCPU",
                       "取消等待请求均不会取消后台开机任务", "不增加上游轮询或开机请求",
                       "不把等待超时当作开机失败或成功", "不额外查规格"):
            self.assertIn(phrase, reference)
        protocol = (SKILL_DIR / "references/protocol.md").read_text(encoding="utf-8")
        self.assertIn("wait_for_job", protocol)
        self.assertIn("等待超时或取消等待不取消后台任务", protocol)

    def test_frontmatter_matches_directory_and_limits(self):
        meta = frontmatter(SKILL)
        self.assertEqual(meta["name"], SKILL_DIR.name, "技能名必须与目录名一致，否则客户端找不到")
        self.assertRegex(meta["name"], PLUGIN_NAME, "技能名只允许小写字母数字与单连字符")
        self.assertLessEqual(len(meta["name"]), 64)
        self.assertTrue(meta["description"].strip(), "description 是技能被检索到的唯一依据，不能为空")
        self.assertLessEqual(len(meta["description"]), 1024, "description 上限 1024 字符")

    def test_links_and_anchors_resolve(self):
        for path in [SKILL, *sorted((SKILL_DIR / "references").glob("*.md")), ROOT / "README.md",
                     *ENTRY_FILES]:
            for target in links(path):
                name, _, anchor = target.partition("#")
                target_path = (path.parent / name).resolve() if name else path
                self.assertTrue(target_path.exists(), f"{path.relative_to(ROOT)} 的链接指向不存在的位置：{target}")
                if anchor:
                    headings = re.findall(r"^#{1,6}\s+(.+)$", target_path.read_text(encoding="utf-8"), re.M)
                    self.assertIn(anchor, [slug(h) for h in headings],
                                  f"{path.relative_to(ROOT)} 的锚点找不到对应标题：{target}")

    def test_script_depth_is_stable(self):
        """脚本与插件根之间固定三层：脚本靠自身位置回推 references/ 与 web/ 面板。

        凭据不在这里——它只从面板登记进内存，不再跟着仓库走，所以挪动只会弄丢文档与页面。
        """
        spec = importlib.util.spec_from_file_location("trainops_layout", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(SCRIPT.resolve().parents[3], ROOT,
                         "trainops.py 必须位于 <插件根>/skills/trainops/scripts/ 下")
        page = SCRIPT.resolve().parent.parent / "web" / "index.html"
        self.assertTrue(page.is_file(), f"面板必须还在 {page.relative_to(ROOT)}")


class EntryFileTests(unittest.TestCase):
    def test_entry_files_are_thin_pointers(self):
        for path in ENTRY_FILES:
            body = path.read_text(encoding="utf-8")
            where = path.relative_to(ROOT)
            self.assertIn("skills/trainops/SKILL.md", body, f"{where} 必须指向能力契约 SKILL.md")
            self.assertLessEqual(len(body.splitlines()), ENTRY_LIMIT,
                                 f"{where} 太长：入口文件只做指针，细则写在 SKILL.md 里")
            self.assertNotRegex(body, r"/home/|/Users/|[A-Z]:\\|/tmp/",
                                f"{where} 里不能出现绝对路径，否则换台机器就失效")
            for phrase in INVARIANTS:
                self.assertIn(phrase, body, f"{where} 缺少不变量「{phrase}」：三处写法必须一致")


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
        """用临时 HOME 真装一遍：不碰用户自己的 Antigravity 配置，也不改本仓库。

        Antigravity 是按目录名为键管理插件的，装的又正好是插件根本身（skills/ 与 rules/
        都在这一层），所以这里顺带验证"装完还能找到技能与规则"这条路径。
        """
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
