import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "bin" / "harness-doctor"

FAKE_TOOL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # fake claude/codex shim for harness-doctor tests
    import os
    import sys
    import json

    if sys.argv[1:] == ["plugin", "list", "--marketplace", "claude-fusion", "--json"]:
        marker = os.environ.get("FAKE_PLUGIN_PROBE_MARKER")
        if marker:
            open(marker, "w", encoding="utf-8").write("called\\n")
        if os.environ.get("FAKE_CODEX_PLUGIN_LIST_FAIL") == "1":
            sys.exit(9)
        if os.environ.get("FAKE_CODEX_PLUGIN_SCHEMA") == "malformed":
            print(json.dumps({"installed": {}, "available": None}))
            sys.exit(0)
        installed = []
        if os.environ.get("FAKE_CODEX_PLUGIN_ABSENT") != "1":
            installed.append({
                "pluginId": "claude-fusion@claude-fusion",
                "name": "claude-fusion",
                "marketplaceName": "claude-fusion",
                "version": os.environ.get("FAKE_PLUGIN_VERSION", "9.8.7"),
                "installed": True,
                "enabled": True,
                "source": {"source": "local", "path": os.environ["FAKE_PLUGIN_SOURCE"]},
            })
        print(json.dumps({"installed": installed, "available": []}))
        sys.exit(0)
    if "--version" in sys.argv:
        print("fake-tool 1.0")
        sys.exit(0)
    if "--help" in sys.argv:
        if os.environ.get("FAKE_CLAUDE_NO_SAFE_MODE") == "1":
            print("Usage: claude [options]\\n  --model <model>")
        else:
            print("Usage: claude [options]\\n  --model <model>\\n  --safe-mode")
        sys.exit(0)
    sys.exit(0)
    """
)

# A syntactically valid stand-in for the Claude Fusion hook sources; carries the literal strings
# the doctor greps for (recursion flags on common, read-only invocation flags on common).
FAKE_CLF_COMMON = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # fake claude-fusion-common.sh for doctor tests
    # references: CLAUDE_FUSION_ACTIVE CODEX_FUSION_ACTIVE
    # invocation: --permission-mode plan --no-session-persistence
    true
    """
)
FAKE_CLF_SCRIPT = "#!/usr/bin/env bash\n# fake claude fusion hook for doctor tests\ntrue\n"
PLUGIN_VERSION = "9.8.7"


class DoctorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.bin = self.base / "bin"
        self.home = self.base / "home"
        self.tmpdir = self.base / "tmp"
        self.claude_dir = self.base / "claude"
        self.codex_dir = self.base / "codex"
        self.peer_repo = self.base / "peer" / "Claude Fusion"
        self.plugin_repo = self.peer_repo / "plugins/claude-fusion"
        self.plugin_source = self.codex_dir / ".tmp/marketplaces/claude-fusion/plugins/claude-fusion"
        self.plugin_cache = self.codex_dir / f"plugins/cache/claude-fusion/claude-fusion/{PLUGIN_VERSION}"
        for d in (self.bin, self.home, self.tmpdir):
            d.mkdir(parents=True)
        self._install_cf()
        self._install_clf()
        self._write_fake_tools()

    def tearDown(self):
        self.tmp.cleanup()

    def _install_cf(self):
        hooks = self.claude_dir / "hooks"
        skill = self.claude_dir / "skills" / "codex-fusion-auto"
        hooks.mkdir(parents=True)
        skill.mkdir(parents=True)
        for name in (
            "codex-fusion-common.sh",
            "codex-fusion-userprompt.sh",
            "codex-fusion-stop.sh",
            "codex-fusion-subagent-stop.sh",
        ):
            shutil.copy2(ROOT / "hooks" / name, hooks / name)
        for name in ("codex-fusion-userprompt.sh", "codex-fusion-stop.sh", "codex-fusion-subagent-stop.sh"):
            (hooks / name).chmod(0o755)
        shutil.copy2(ROOT / "skills" / "codex-fusion-auto" / "SKILL.md", skill / "SKILL.md")
        self.write_settings()

    def _install_clf(self):
        hooks = self.plugin_source / "hooks"
        skill = self.plugin_source / "skills" / "claude-fusion-auto"
        manifest_dir = self.plugin_source / ".codex-plugin"
        hooks.mkdir(parents=True)
        skill.mkdir(parents=True)
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "plugin.json").write_text(
            json.dumps({"name": "claude-fusion", "version": PLUGIN_VERSION}), encoding="utf-8")
        (hooks / "claude-fusion-common.sh").write_text(FAKE_CLF_COMMON, encoding="utf-8")
        for name in ("claude-fusion-userprompt.sh", "claude-fusion-subagent-stop.sh", "claude-fusion-stop.sh"):
            (hooks / name).write_text(FAKE_CLF_SCRIPT, encoding="utf-8")
            (hooks / name).chmod(0o755)
        (hooks / "hooks.json").write_text(json.dumps(self.plugin_hooks()), encoding="utf-8")
        (skill / "SKILL.md").write_text("# fake skill\n", encoding="utf-8")
        shutil.copytree(self.plugin_source, self.plugin_cache, copy_function=shutil.copy2)
        shutil.copytree(self.plugin_source, self.plugin_repo, copy_function=shutil.copy2)
        self.codex_dir.mkdir(parents=True, exist_ok=True)
        (self.codex_dir / "hooks.json").write_text("{}", encoding="utf-8")
        self.write_config_toml()

    def _write_fake_tools(self):
        for name in ("claude", "codex"):
            fake = self.bin / name
            fake.write_text(FAKE_TOOL, encoding="utf-8")
            fake.chmod(0o755)

    @staticmethod
    def _entry(command, timeout, hook_type="command"):
        return {"hooks": [{"type": hook_type, "command": command, "timeout": timeout}]}

    def write_settings(self, ups_timeout=270, stop_timeout=270, include_stop=True, duplicate_ups=False,
                       substop_timeout=270, include_substop=True):
        ups = str(self.claude_dir / "hooks" / "codex-fusion-userprompt.sh")
        stop = str(self.claude_dir / "hooks" / "codex-fusion-stop.sh")
        substop = str(self.claude_dir / "hooks" / "codex-fusion-subagent-stop.sh")
        hooks = {"UserPromptSubmit": [self._entry(ups, ups_timeout)]}
        if duplicate_ups:
            hooks["UserPromptSubmit"].append(self._entry(ups, ups_timeout))
        if include_substop:
            hooks["SubagentStop"] = [self._entry(substop, substop_timeout)]
        if include_stop:
            hooks["Stop"] = [self._entry(stop, stop_timeout)]
        (self.claude_dir / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    def plugin_hooks(self, timeout=660, include_subagent=True, hook_type="command", duplicate_event=None):
        scripts = {
            "UserPromptSubmit": "claude-fusion-userprompt.sh",
            "Stop": "claude-fusion-stop.sh",
        }
        if include_subagent:
            scripts["SubagentStop"] = "claude-fusion-subagent-stop.sh"
        hooks = {
            event: [self._entry("${PLUGIN_ROOT}/hooks/" + script, timeout, hook_type)]
            for event, script in scripts.items()
        }
        if duplicate_event in hooks:
            hooks[duplicate_event].append(hooks[duplicate_event][0])
        return {"hooks": hooks}

    def write_plugin_hooks(self, timeout=660, include_subagent=True, hook_type="command", duplicate_event=None):
        payload = self.plugin_hooks(timeout=timeout, include_subagent=include_subagent,
                                    hook_type=hook_type, duplicate_event=duplicate_event)
        for root in (self.plugin_source, self.plugin_cache, self.plugin_repo):
            (root / "hooks/hooks.json").write_text(json.dumps(payload), encoding="utf-8")

    def write_config_toml(self, trusted=True):
        lines = ['model = "gpt-5.6-sol"\n', '[plugins."claude-fusion@claude-fusion"]\nenabled = true\n']
        if trusted:
            for frag in ("user_prompt_submit", "subagent_stop", "stop"):
                lines.append(
                    f'[hooks.state."claude-fusion@claude-fusion:hooks/hooks.json:{frag}:0:0"]\n'
                    'trusted_hash = "sha256:test"\n'
                )
        (self.codex_dir / "config.toml").write_text("\n".join(lines), encoding="utf-8")

    def env(self, **extra):
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("CLAUDE_FUSION_", "CODEX_FUSION_", "FAKE_CLAUDE_", "FAKE_CODEX_", "FAKE_PLUGIN_")):
                env.pop(key)
        env.update(
            {
                # No real ~/.local/bin on PATH and HOME inside the sandbox, so the fake shims always
                # win over any claude/codex installed on the host machine.
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "HOME": str(self.home),
                "TMPDIR": str(self.tmpdir),
                "CLAUDE_CONFIG_DIR": str(self.claude_dir),
                "CODEX_HOME": str(self.codex_dir),
                "CLAUDE_FUSION_REPO": str(self.peer_repo),
                "FAKE_PLUGIN_SOURCE": str(self.plugin_source),
                "FAKE_PLUGIN_VERSION": PLUGIN_VERSION,
            }
        )
        env.update(extra)
        return env

    def run_doctor(self, *args, **extra_env):
        return subprocess.run(
            [str(DOCTOR), *args],
            text=True,
            capture_output=True,
            cwd=self.base,
            env=self.env(**extra_env),
            timeout=60,
        )

    def lines(self, res, level, check):
        return [l for l in res.stdout.splitlines() if l.startswith(level) and check in l]

    def assert_no_fail(self, res):
        fails = [l for l in res.stdout.splitlines() if l.startswith("FAIL")]
        self.assertEqual(fails, [], res.stdout)

    def snapshot(self):
        state = {}
        for path in sorted(self.base.rglob("*")):
            st = path.lstat()
            digest = ""
            if stat.S_ISREG(st.st_mode):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            state[str(path)] = (stat.S_IFMT(st.st_mode), st.st_mode & 0o777, digest)
        return state

    def test_healthy_install_exits_zero(self):
        res = self.run_doctor()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assert_no_fail(res)
        self.assertIn("0 fail", res.stdout)
        for check in ("cf.files.parity", "clf.plugin.parity", "clf.plugin.source-parity", "clf.safemode"):
            self.assertTrue(self.lines(res, "PASS", check), f"{check} not PASS:\n{res.stdout}")
        self.assertFalse(self.lines(res, "PASS", "clf.trust"), res.stdout)
        trust = self.lines(res, "INFO", "clf.trust")
        self.assertTrue(trust, res.stdout)
        self.assertIn("freshness is unverified", trust[0])
        self.assertIn("/hooks", trust[0])
        self.assertIn(f"dynamically discovered version {PLUGIN_VERSION}", res.stdout)

    def test_missing_stop_registration_fails(self):
        self.write_settings(include_stop=False)
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "cf.hooks.stop"), res.stdout)

    def test_duplicate_registration_warns(self):
        self.write_settings(duplicate_ups=True)
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "cf.hooks.userprompt"), res.stdout)

    def test_timeout_not_above_internal_fails(self):
        self.write_settings(ups_timeout=60)
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "cf.hooks.timeout"), res.stdout)

    def test_per_call_override_is_safe_because_whole_hook_budget_caps_it(self):
        res = self.run_doctor(CODEX_FUSION_TIMEOUT="400")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertFalse(self.lines(res, "FAIL", "cf.hooks.timeout"), res.stdout)

    def test_whole_hook_budget_above_registration_fails(self):
        res = self.run_doctor(CODEX_FUSION_BUDGET="300")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "cf.hooks.timeout"), res.stdout)

    def test_whole_hook_budget_below_registration_is_safe(self):
        res = self.run_doctor(CODEX_FUSION_BUDGET="266")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertFalse(self.lines(res, "FAIL", "cf.hooks.timeout"), res.stdout)

    def test_whole_hook_budget_equal_to_registration_fails(self):
        res = self.run_doctor(CODEX_FUSION_BUDGET="270")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "cf.hooks.timeout"), res.stdout)

    def test_installed_file_drift_fails(self):
        target = self.claude_dir / "hooks" / "codex-fusion-userprompt.sh"
        target.write_text(target.read_text(encoding="utf-8") + "\n# local drift\n", encoding="utf-8")
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "cf.files.parity"), res.stdout)

    def test_missing_exec_bit_fails(self):
        (self.plugin_cache / "hooks" / "claude-fusion-subagent-stop.sh").chmod(0o644)
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "clf.files.installed"), res.stdout)

    def test_claude_without_safe_mode_fails(self):
        res = self.run_doctor(FAKE_CLAUDE_NO_SAFE_MODE="1")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "clf.safemode"), res.stdout)

    def test_safe_mode_disabled_env_downgrades_to_warn(self):
        res = self.run_doctor(FAKE_CLAUDE_NO_SAFE_MODE="1", CLAUDE_FUSION_SAFE_MODE="0")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertTrue(self.lines(res, "WARN", "clf.safemode"), res.stdout)

    def test_missing_codex_binary_fails(self):
        (self.bin / "codex").unlink()
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "cf.bin.codex"), res.stdout)

    def test_untrusted_hooks_warns(self):
        self.write_config_toml(trusted=False)
        res = self.run_doctor()
        self.assertEqual(res.returncode, 0, res.stdout)
        warn = self.lines(res, "WARN", "clf.trust")
        self.assertTrue(warn, res.stdout)
        self.assertIn("/hooks", warn[0])

    def test_plugin_cache_drift_fails_parity(self):
        target = self.plugin_cache / "hooks" / "claude-fusion-stop.sh"
        target.write_text(target.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "clf.plugin.parity"), res.stdout)

    def test_canonical_source_drift_fails_parity(self):
        target = self.plugin_repo / "hooks" / "claude-fusion-stop.sh"
        target.write_text(target.read_text(encoding="utf-8") + "# source drift\n", encoding="utf-8")
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "clf.plugin.source-parity"), res.stdout)

    def test_missing_subagent_registration_fails(self):
        self.write_plugin_hooks(include_subagent=False)
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "clf.hooks.subagent-stop"), res.stdout)

    def test_duplicate_plugin_registration_fails(self):
        self.write_plugin_hooks(duplicate_event="SubagentStop")
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "clf.hooks.subagent-stop"), res.stdout)

    def test_extra_plugin_command_fails_exact_shape(self):
        payload = self.plugin_hooks()
        payload["hooks"]["Stop"].append(self._entry("/tmp/unexpected-hook", 660))
        for root in (self.plugin_source, self.plugin_cache, self.plugin_repo):
            (root / "hooks/hooks.json").write_text(json.dumps(payload), encoding="utf-8")
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "clf.hooks.stop"), res.stdout)

    def test_unexpected_plugin_event_fails_exact_event_set(self):
        payload = self.plugin_hooks()
        payload["hooks"]["PreToolUse"] = [self._entry("/tmp/unexpected-hook", 660)]
        for root in (self.plugin_source, self.plugin_cache, self.plugin_repo):
            (root / "hooks/hooks.json").write_text(json.dumps(payload), encoding="utf-8")
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "clf.hooks.events"), res.stdout)

    def test_matching_registration_must_be_command_type(self):
        self.write_plugin_hooks(hook_type="prompt")
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "clf.hooks.userprompt"), res.stdout)

    def test_cli_failure_falls_back_to_config_and_cache(self):
        res = self.run_doctor(FAKE_CODEX_PLUGIN_LIST_FAIL="1")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assert_no_fail(res)
        self.assertIn("config/cache fallback", res.stdout)

    def test_malformed_cli_schema_falls_back_without_type_error(self):
        res = self.run_doctor(FAKE_CODEX_PLUGIN_SCHEMA="malformed")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assert_no_fail(res)
        self.assertIn("JSON schema invalid", res.stdout)
        self.assertIn("config/cache fallback", res.stdout)

    def test_unsafe_plugin_version_is_rejected(self):
        res = self.run_doctor(FAKE_PLUGIN_VERSION="..")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "clf.plugin.version"), res.stdout)

    def test_cache_symlink_escape_is_rejected(self):
        outside = self.base / "outside-cache"
        outside.mkdir()
        shutil.rmtree(self.plugin_cache)
        self.plugin_cache.symlink_to(outside, target_is_directory=True)
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        failure = self.lines(res, "FAIL", "clf.plugin.cache")
        self.assertTrue(failure, res.stdout)
        self.assertIn("escapes", failure[0])

    def test_cli_source_outside_marketplace_payload_is_rejected(self):
        outside = self.base / "outside-source"
        outside.mkdir()
        res = self.run_doctor(FAKE_PLUGIN_SOURCE=str(outside))
        self.assertEqual(res.returncode, 1, res.stdout)
        failure = self.lines(res, "FAIL", "clf.plugin.marketplace")
        self.assertTrue(failure, res.stdout)
        self.assertIn("outside expected", failure[0])

    def test_claude_internal_timeout_is_capped_at_630(self):
        res = self.run_doctor(CLAUDE_FUSION_TIMEOUT="900")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assert_no_fail(res)
        self.assertIn("timeout=630s", res.stdout)

    def test_strict_promotes_warn_to_exit_1(self):
        self.write_config_toml(trusted=False)
        res = self.run_doctor("--strict")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assert_no_fail(res)

    def test_skip_probes_passes_without_binaries_probed(self):
        marker = self.base / "plugin-probe-called"
        res = self.run_doctor("--skip-probes", FAKE_PLUGIN_PROBE_MARKER=str(marker))
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assert_no_fail(res)
        self.assertTrue(self.lines(res, "INFO", "clf.safemode"), res.stdout)
        self.assertFalse(marker.exists(), "--skip-probes must not invoke codex plugin list")

    def test_empty_legacy_hooks_json_is_inactive(self):
        shutil.rmtree(self.codex_dir / "plugins")
        shutil.rmtree(self.codex_dir / ".tmp")
        self.write_config_toml(trusted=False)
        config = self.codex_dir / "config.toml"
        config.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
        res = self.run_doctor(FAKE_CODEX_PLUGIN_ABSENT="1")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assert_no_fail(res)
        self.assertTrue(self.lines(res, "INFO", "clf"), res.stdout)

    def test_orphan_legacy_script_is_partial_install(self):
        shutil.rmtree(self.codex_dir / "plugins")
        shutil.rmtree(self.codex_dir / ".tmp")
        (self.codex_dir / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
        hooks = self.codex_dir / "hooks"
        hooks.mkdir()
        (hooks / "claude-fusion-common.sh").write_text(FAKE_CLF_COMMON, encoding="utf-8")
        res = self.run_doctor(FAKE_CODEX_PLUGIN_ABSENT="1")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "WARN", "clf.mode"), res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "clf.files.installed"), res.stdout)

    def test_doctor_is_read_only(self):
        before = self.snapshot()
        res = self.run_doctor()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.snapshot(), before, "the doctor must not create, modify, or delete any file")


if __name__ == "__main__":
    unittest.main()
