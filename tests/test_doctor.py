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
        for name in ("codex-fusion-common.sh", "codex-fusion-userprompt.sh", "codex-fusion-stop.sh"):
            shutil.copy2(ROOT / "hooks" / name, hooks / name)
        (hooks / "codex-fusion-userprompt.sh").chmod(0o755)
        (hooks / "codex-fusion-stop.sh").chmod(0o755)
        shutil.copy2(ROOT / "skills" / "codex-fusion-auto" / "SKILL.md", skill / "SKILL.md")
        self.write_settings()

    def _install_clf(self):
        peer_hooks = self.peer_repo / "hooks"
        peer_skill = self.peer_repo / "skills" / "claude-fusion-auto"
        peer_hooks.mkdir(parents=True)
        peer_skill.mkdir(parents=True)
        (peer_hooks / "claude-fusion-common.sh").write_text(FAKE_CLF_COMMON, encoding="utf-8")
        for name in ("claude-fusion-userprompt.sh", "claude-fusion-stop.sh"):
            (peer_hooks / name).write_text(FAKE_CLF_SCRIPT, encoding="utf-8")
        (peer_skill / "SKILL.md").write_text("# fake skill\n", encoding="utf-8")

        hooks = self.codex_dir / "hooks"
        skill = self.codex_dir / "skills" / "claude-fusion-auto"
        hooks.mkdir(parents=True)
        skill.mkdir(parents=True)
        for name in ("claude-fusion-common.sh", "claude-fusion-userprompt.sh", "claude-fusion-stop.sh"):
            shutil.copy2(peer_hooks / name, hooks / name)
        (hooks / "claude-fusion-userprompt.sh").chmod(0o755)
        (hooks / "claude-fusion-stop.sh").chmod(0o755)
        shutil.copy2(peer_skill / "SKILL.md", skill / "SKILL.md")
        self.write_hooks_json()
        self.write_config_toml()

    def _write_fake_tools(self):
        for name in ("claude", "codex"):
            fake = self.bin / name
            fake.write_text(FAKE_TOOL, encoding="utf-8")
            fake.chmod(0o755)

    @staticmethod
    def _entry(command, timeout):
        return {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}

    def write_settings(self, ups_timeout=270, stop_timeout=270, include_stop=True, duplicate_ups=False):
        ups = str(self.claude_dir / "hooks" / "codex-fusion-userprompt.sh")
        stop = str(self.claude_dir / "hooks" / "codex-fusion-stop.sh")
        hooks = {"UserPromptSubmit": [self._entry(ups, ups_timeout)]}
        if duplicate_ups:
            hooks["UserPromptSubmit"].append(self._entry(ups, ups_timeout))
        if include_stop:
            hooks["Stop"] = [self._entry(stop, stop_timeout)]
        (self.claude_dir / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    def write_hooks_json(self, timeout=660):
        ups = str(self.codex_dir / "hooks" / "claude-fusion-userprompt.sh")
        stop = str(self.codex_dir / "hooks" / "claude-fusion-stop.sh")
        payload = {"hooks": {"UserPromptSubmit": [self._entry(ups, timeout)], "Stop": [self._entry(stop, timeout)]}}
        (self.codex_dir / "hooks.json").write_text(json.dumps(payload), encoding="utf-8")

    def write_config_toml(self, trusted=True):
        lines = ['model = "gpt-5.5"\n']
        if trusted:
            hooks_json = self.codex_dir / "hooks.json"
            for frag in ("user_prompt_submit", "stop"):
                lines.append(f'[hooks.state."{hooks_json}:{frag}:0:0"]\ntrusted_hash = "sha256:test"\n')
        (self.codex_dir / "config.toml").write_text("\n".join(lines), encoding="utf-8")

    def env(self, **extra):
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("CLAUDE_FUSION_", "CODEX_FUSION_", "FAKE_CLAUDE_")):
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
        for check in ("cf.files.parity", "clf.files.parity", "clf.safemode", "clf.trust"):
            self.assertTrue(self.lines(res, "PASS", check), f"{check} not PASS:\n{res.stdout}")

    def test_missing_stop_registration_fails(self):
        self.write_settings(include_stop=False)
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "cf.hooks.stop"), res.stdout)

    def test_duplicate_registration_warns(self):
        self.write_settings(duplicate_ups=True)
        res = self.run_doctor()
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertTrue(self.lines(res, "WARN", "cf.hooks.userprompt"), res.stdout)

    def test_timeout_not_above_internal_fails(self):
        self.write_settings(ups_timeout=60)
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "cf.hooks.timeout"), res.stdout)

    def test_env_override_raises_internal_timeout(self):
        # 270s registered vs CODEX_FUSION_TIMEOUT=400: the doctor must compute the effective
        # internal timeout from env exactly as the hooks do, not from the shipped default.
        res = self.run_doctor(CODEX_FUSION_TIMEOUT="400")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "cf.hooks.timeout"), res.stdout)

    def test_installed_file_drift_fails(self):
        target = self.claude_dir / "hooks" / "codex-fusion-userprompt.sh"
        target.write_text(target.read_text(encoding="utf-8") + "\n# local drift\n", encoding="utf-8")
        res = self.run_doctor()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.lines(res, "FAIL", "cf.files.parity"), res.stdout)

    def test_missing_exec_bit_fails(self):
        (self.codex_dir / "hooks" / "claude-fusion-stop.sh").chmod(0o644)
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

    def test_peer_repo_missing_degrades_gracefully(self):
        res = self.run_doctor(CLAUDE_FUSION_REPO=str(self.base / "nowhere"))
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assert_no_fail(res)
        self.assertTrue(self.lines(res, "INFO", "cross.peer"), res.stdout)
        self.assertTrue(self.lines(res, "INFO", "clf.files.parity"), res.stdout)

    def test_strict_promotes_warn_to_exit_1(self):
        self.write_config_toml(trusted=False)
        res = self.run_doctor("--strict")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assert_no_fail(res)

    def test_skip_probes_passes_without_binaries_probed(self):
        res = self.run_doctor("--skip-probes")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assert_no_fail(res)
        self.assertTrue(self.lines(res, "INFO", "clf.safemode"), res.stdout)

    def test_doctor_is_read_only(self):
        before = self.snapshot()
        res = self.run_doctor()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.snapshot(), before, "the doctor must not create, modify, or delete any file")


if __name__ == "__main__":
    unittest.main()
