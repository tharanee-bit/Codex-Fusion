import json
import os
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
USERPROMPT_HOOK = ROOT / "hooks" / "codex-fusion-userprompt.sh"
STOP_HOOK = ROOT / "hooks" / "codex-fusion-stop.sh"
INSTALL = ROOT / "install.sh"


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.bin = self.base / "bin"
        self.home = self.base / "home"
        self.tmpdir = self.base / "tmp"
        self.log = self.base / "codex-log.jsonl"
        self.bin.mkdir()
        self.home.mkdir()
        self.tmpdir.mkdir()
        self.repo.mkdir()
        self._init_repo()
        self._write_fake_codex()

    def tearDown(self):
        self.tmp.cleanup()

    def _init_repo(self):
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, stdout=subprocess.DEVNULL)
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
            cwd=self.repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def _write_fake_codex(self):
        fake = self.bin / "codex"
        fake.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                import time

                role = os.environ.get("CODEX_FUSION_AGENT_ROLE", "")
                has_model = "-m" in sys.argv or "--model" in sys.argv
                out = None
                for i, arg in enumerate(sys.argv):
                    if arg in ("-o", "--output-last-message") and i + 1 < len(sys.argv):
                        out = sys.argv[i + 1]
                    elif arg.startswith("--output-last-message="):
                        out = arg.split("=", 1)[1]

                log = os.environ.get("FAKE_CODEX_LOG")
                if log:
                    with open(log, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"role": role, "has_model": has_model, "argv": sys.argv, "time": time.time()}) + "\\n")

                if os.environ.get("FAKE_CODEX_FAIL_MODEL") == "1" and has_model:
                    sys.exit(2)

                fail_roles = {r for r in os.environ.get("FAKE_CODEX_FAIL_ROLES", "").split(",") if r}
                if role in fail_roles:
                    sys.exit(7)

                delay = float(os.environ.get("FAKE_CODEX_SLEEP", "0") or "0")
                if delay:
                    time.sleep(delay)

                issue_roles = {r for r in os.environ.get("FAKE_CODEX_ISSUE_ROLES", "").split(",") if r}
                review_roles = {"single-review", "correctness", "security-data-loss-concurrency", "tests-regression"}
                if role in review_roles:
                    if role in issue_roles:
                        content = f"CODEX_REVIEW_VERDICT: ISSUES_FOUND\\n- fake.py:1 - issue from {role} - fix it\\n"
                    else:
                        content = f"CODEX_REVIEW_VERDICT: PASS\\n{role} passed\\n"
                else:
                    content = f"analysis from {role}\\n"

                if out:
                    with open(out, "w", encoding="utf-8") as f:
                        f.write(content)
                sys.exit(0)
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)

    def env(self, **extra):
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin}:{env.get('PATH', '')}",
                "HOME": str(self.home),
                "TMPDIR": str(self.tmpdir),
                "FAKE_CODEX_LOG": str(self.log),
                "CODEX_FUSION_TIMEOUT": "5",
            }
        )
        env.update(extra)
        return env

    def run_hook(self, hook, payload, **extra_env):
        return subprocess.run(
            [str(hook)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=self.repo,
            env=self.env(**extra_env),
            timeout=20,
        )

    def read_log(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def clear_log(self):
        self.log.write_text("", encoding="utf-8")

    def state_file(self, session, suffix):
        return self.tmpdir / "codex-fusion-state" / f"session-{session}.{suffix}"

    def baseline(self, session, prompt="baseline [subagents]"):
        res = self.run_hook(USERPROMPT_HOOK, {"prompt": prompt, "cwd": str(self.repo), "session_id": session})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.clear_log()

    def modify_repo(self):
        (self.repo / "README.md").write_text("hello\nchanged\n", encoding="utf-8")

    def test_simple_prompt_uses_single_agent(self):
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "what does this function do?", "cwd": str(self.repo), "session_id": "single"},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertIn("AUTOMATIC CODEX FUSION CONTEXT", payload["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(payload["systemMessage"], "Codex Fusion: Codex consulted successfully.")
        self.assertEqual([entry["role"] for entry in self.read_log()], ["single"])

    def test_auto_fanout_for_high_risk_prompt(self):
        prompt = "Implement the auth database migration plan.\nFix the race condition.\nAdd tests.\nReview security."
        res = self.run_hook(USERPROMPT_HOOK, {"prompt": prompt, "cwd": str(self.repo), "session_id": "auto"})
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("sub-agent fanout", context)
        self.assertIn("spawned 3 sub-agents; 3/3 succeeded", context)
        self.assertEqual(payload["systemMessage"], "Codex Fusion: spawned 3 sub-agents; 3/3 succeeded.")
        self.assertEqual([entry["role"] for entry in self.read_log()], ["planner", "skeptic", "verifier"])

    def test_forced_fanout_reports_reduced_spawn_count(self):
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "tiny request [subagents]", "cwd": str(self.repo), "session_id": "twospawn"},
            CODEX_FUSION_MAX_AGENTS="2",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("spawned 2 sub-agents; 2/2 succeeded", context)
        self.assertEqual(payload["systemMessage"], "Codex Fusion: spawned 2 sub-agents; 2/2 succeeded.")
        self.assertEqual([entry["role"] for entry in self.read_log()], ["planner", "skeptic"])

    def test_notify_zero_suppresses_userprompt_system_message_only(self):
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "what does this function do?", "cwd": str(self.repo), "session_id": "notifyoff"},
            CODEX_FUSION_NOTIFY="0",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertNotIn("systemMessage", payload)
        self.assertIn("AUTOMATIC CODEX FUSION CONTEXT", payload["hookSpecificOutput"]["additionalContext"])
        self.assertEqual([entry["role"] for entry in self.read_log()], ["single"])

    def test_ci_substring_does_not_force_prompt_fanout(self):
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "Make a decision about database naming", "cwd": str(self.repo), "session_id": "decision"},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([entry["role"] for entry in self.read_log()], ["single"])

    def test_fanout_runs_agents_in_parallel(self):
        start = time.monotonic()
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "tiny request [subagents]", "cwd": str(self.repo), "session_id": "parallel"},
            FAKE_CODEX_SLEEP="0.45",
        )
        elapsed = time.monotonic() - start
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertLess(elapsed, 1.2)
        self.assertCountEqual([entry["role"] for entry in self.read_log()], ["planner", "skeptic", "verifier"])

    def test_prompt_markers_force_and_disable_fanout(self):
        forced = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "tiny request [subagents]", "cwd": str(self.repo), "session_id": "forced"},
        )
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual([entry["role"] for entry in self.read_log()], ["planner", "skeptic", "verifier"])

        self.clear_log()
        single = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "Implement auth migration [no-subagents]", "cwd": str(self.repo), "session_id": "nosub"},
            CODEX_FUSION_SUBAGENTS="always",
        )
        self.assertEqual(single.returncode, 0, single.stderr)
        self.assertEqual([entry["role"] for entry in self.read_log()], ["single"])

    def test_no_codex_and_nested_skip(self):
        skipped = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "Refactor auth [no-codex]", "cwd": str(self.repo), "session_id": "skip"},
        )
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.assertEqual(skipped.stdout, "")
        self.assertEqual(self.read_log(), [])

        nested = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "Refactor auth", "cwd": str(self.repo), "session_id": "nested"},
            CLAUDE_FUSION_ACTIVE="1",
        )
        self.assertEqual(nested.returncode, 0, nested.stderr)
        self.assertEqual(nested.stdout, "")
        self.assertEqual(self.read_log(), [])

    def test_model_fallback_retries_without_model_arg(self):
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "what does this function do?", "cwd": str(self.repo), "session_id": "fallback"},
            FAKE_CODEX_FAIL_MODEL="1",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = self.read_log()
        self.assertEqual([entry["role"] for entry in calls], ["single", "single"])
        self.assertEqual([entry["has_model"] for entry in calls], [True, False])

    def test_stop_fanout_pass_stores_reviewed_hash(self):
        self.baseline("pass")
        self.modify_repo()
        res = self.run_hook(STOP_HOOK, {"cwd": str(self.repo), "session_id": "pass", "stop_hook_active": False})
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["systemMessage"], "Codex Fusion: spawned 3 review sub-agents; all 3 passed.")
        self.assertCountEqual(
            [entry["role"] for entry in self.read_log()],
            ["correctness", "security-data-loss-concurrency", "tests-regression"],
        )
        self.assertTrue(self.state_file("pass", "reviewed").exists())

    def test_notify_zero_suppresses_stop_fanout_pass_system_message(self):
        self.baseline("stopnotifyoff")
        self.modify_repo()
        res = self.run_hook(
            STOP_HOOK,
            {"cwd": str(self.repo), "session_id": "stopnotifyoff", "stop_hook_active": False},
            CODEX_FUSION_NOTIFY="0",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")
        self.assertCountEqual(
            [entry["role"] for entry in self.read_log()],
            ["correctness", "security-data-loss-concurrency", "tests-regression"],
        )
        self.assertTrue(self.state_file("stopnotifyoff", "reviewed").exists())

    def test_db_substring_does_not_force_stop_fanout(self):
        self.baseline("dbg", prompt="baseline")
        (self.repo / "README.md").write_text("hello\ndbg marker\n", encoding="utf-8")
        res = self.run_hook(STOP_HOOK, {"cwd": str(self.repo), "session_id": "dbg", "stop_hook_active": False})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([entry["role"] for entry in self.read_log()], ["single-review"])
        self.assertTrue(self.state_file("dbg", "reviewed").exists())

    def test_stop_issues_found_blocks_and_stores_reviewed_hash(self):
        self.baseline("issues")
        self.modify_repo()
        res = self.run_hook(
            STOP_HOOK,
            {"cwd": str(self.repo), "session_id": "issues", "stop_hook_active": False},
            FAKE_CODEX_ISSUE_ROLES="security-data-loss-concurrency",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("spawned 3 review sub-agents; 3/3 succeeded", payload["reason"])
        self.assertIn("security-data-loss-concurrency", payload["reason"])
        self.assertTrue(self.state_file("issues", "reviewed").exists())

    def test_stop_partial_fanout_failure_retries_without_storing_reviewed_hash(self):
        self.baseline("partial")
        self.modify_repo()
        payload = {"cwd": str(self.repo), "session_id": "partial", "stop_hook_active": False}
        extra = {"FAKE_CODEX_FAIL_ROLES": "tests-regression", "CODEX_FUSION_STOP_RETRY_LIMIT": "2"}

        first = self.run_hook(STOP_HOOK, payload, **extra)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, "")
        self.assertFalse(self.state_file("partial", "reviewed").exists())
        self.assertEqual(len(self.read_log()), 4)

        second = self.run_hook(STOP_HOOK, payload, **extra)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(self.state_file("partial", "reviewed").exists())
        self.assertEqual(len(self.read_log()), 8)

        third = self.run_hook(STOP_HOOK, payload, **extra)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertFalse(self.state_file("partial", "reviewed").exists())
        self.assertEqual(len(self.read_log()), 8)

    def test_installer_copies_common_hook_and_is_idempotent(self):
        config_dir = self.base / "claude"
        hooks_dir = config_dir / "hooks"
        config_dir.mkdir()
        (config_dir / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": str(hooks_dir / "codex-fusion-userprompt.sh"),
                                        "timeout": 30,
                                    }
                                ]
                            }
                        ],
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": str(hooks_dir / "codex-fusion-stop.sh"),
                                        "timeout": 30,
                                    }
                                ]
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        env = self.env(CLAUDE_CONFIG_DIR=str(config_dir))
        for _ in range(2):
            res = subprocess.run([str(INSTALL)], cwd=ROOT, text=True, capture_output=True, env=env, timeout=20)
            self.assertEqual(res.returncode, 0, res.stderr)

        self.assertTrue((config_dir / "hooks" / "codex-fusion-common.sh").exists())
        self.assertTrue((config_dir / "hooks" / "codex-fusion-userprompt.sh").exists())
        self.assertTrue((config_dir / "hooks" / "codex-fusion-stop.sh").exists())
        settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
        expected_status = {
            "UserPromptSubmit": "Codex Fusion: checking Codex...",
            "Stop": "Codex Fusion: reviewing changes...",
        }
        for event in ("UserPromptSubmit", "Stop"):
            hooks = [
                hook
                for group in settings["hooks"][event]
                for hook in group["hooks"]
                if "codex-fusion" in hook["command"]
            ]
            self.assertEqual(len(hooks), 1)
            self.assertEqual(hooks[0]["timeout"], 270)
            self.assertEqual(hooks[0]["statusMessage"], expected_status[event])


if __name__ == "__main__":
    unittest.main()
