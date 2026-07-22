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
                import signal
                import sys
                import time

                role = os.environ.get("CODEX_FUSION_AGENT_ROLE", "")
                if os.environ.get("FAKE_CODEX_IGNORE_TERM") == "1":
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
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

                bloat = int(os.environ.get("FAKE_CODEX_BLOAT", "0") or "0")
                if bloat:
                    content += "X" * bloat + "\\n"

                if out:
                    with open(out, "w", encoding="utf-8") as f:
                        f.write(content)
                sys.exit(0)
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)
        # The hooks force-prepend $HOME/.local/bin:...:/usr/local/bin:/usr/bin:/bin to PATH, so a
        # real codex in a system dir would beat self.bin. Install the shim at the temp home's
        # .local/bin (first PATH entry) so the fake always wins regardless of the host machine.
        home_bin = self.home / ".local" / "bin"
        home_bin.mkdir(parents=True, exist_ok=True)
        home_fake = home_bin / "codex"
        home_fake.write_text(fake.read_text(encoding="utf-8"), encoding="utf-8")
        home_fake.chmod(0o755)

    def env(self, **extra):
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("CODEX_FUSION_", "CLAUDE_FUSION_", "FAKE_CODEX_")):
                env.pop(key)
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

    def state_dir(self):
        return self.tmpdir / f"codex-fusion-state-{os.getuid()}"

    def state_file(self, session, suffix):
        return self.state_dir() / f"session-{session}.{suffix}"

    def codex_prompts(self):
        return [entry["argv"][-1] for entry in self.read_log()]

    def baseline(self, session, prompt="baseline [subagents]"):
        res = self.run_hook(USERPROMPT_HOOK, {"prompt": prompt, "cwd": str(self.repo), "session_id": session})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.clear_log()

    def modify_repo(self):
        (self.repo / "README.md").write_text("hello\nchanged\n", encoding="utf-8")

    def commit_all(self, message):
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", message],
            cwd=self.repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )

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
        argv = self.read_log()[0]["argv"]
        for token in ("--sandbox", "read-only", "--ask-for-approval", "never", "exec"):
            self.assertIn(token, argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.6-sol")
        self.assertIn("model_reasoning_effort=xhigh", argv)

    def test_model_and_effort_overrides_are_preserved(self):
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "what does this function do?", "cwd": str(self.repo), "session_id": "override"},
            CODEX_FUSION_MODEL="custom-model",
            CODEX_FUSION_EFFORT="high",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        argv = self.read_log()[0]["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "custom-model")
        self.assertIn("model_reasoning_effort=high", argv)

    def test_auto_fanout_for_high_risk_prompt(self):
        prompt = "Implement the auth database migration plan.\nFix the race condition.\nAdd tests.\nReview security."
        res = self.run_hook(USERPROMPT_HOOK, {"prompt": prompt, "cwd": str(self.repo), "session_id": "auto"})
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("sub-agent fanout", context)
        self.assertIn("spawned 3 sub-agents; 3/3 succeeded", context)
        self.assertEqual(payload["systemMessage"], "Codex Fusion: spawned 3 sub-agents; 3/3 succeeded.")
        self.assertCountEqual([entry["role"] for entry in self.read_log()], ["planner", "skeptic", "verifier"])

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
        self.assertCountEqual([entry["role"] for entry in self.read_log()], ["planner", "skeptic"])

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
        self.assertCountEqual([entry["role"] for entry in self.read_log()], ["planner", "skeptic", "verifier"])

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
        for entry in calls:
            for token in ("--sandbox", "read-only", "--ask-for-approval", "never", "exec"):
                self.assertIn(token, entry["argv"], "read-only contract must hold on the fallback attempt too")

    def test_low_remaining_budget_skips_model_fallback(self):
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "what does this function do?", "cwd": str(self.repo), "session_id": "lowbudget"},
            FAKE_CODEX_FAIL_MODEL="1",
            CODEX_FUSION_BUDGET="4",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")
        self.assertLessEqual(len(self.read_log()), 1, "an insufficient remainder must not start the fallback")

    def test_single_call_is_capped_by_whole_hook_budget(self):
        start = time.monotonic()
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "what does this function do?", "cwd": str(self.repo), "session_id": "budgetsingle"},
            FAKE_CODEX_SLEEP="5",
            CODEX_FUSION_TIMEOUT="10",
            CODEX_FUSION_BUDGET="2",
        )
        elapsed = time.monotonic() - start
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")
        self.assertLess(elapsed, 3.5)
        self.assertLessEqual(len(self.read_log()), 1)

    def test_term_ignoring_child_is_killed_after_grace(self):
        start = time.monotonic()
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "what does this function do?", "cwd": str(self.repo), "session_id": "hardkill"},
            FAKE_CODEX_SLEEP="30",
            FAKE_CODEX_IGNORE_TERM="1",
            CODEX_FUSION_TIMEOUT="10",
            CODEX_FUSION_BUDGET="16",
        )
        elapsed = time.monotonic() - start
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")
        self.assertLess(elapsed, 8.0, "GNU timeout -k grace must hard-kill a TERM-ignoring child")

    def test_fanout_workers_share_the_whole_hook_deadline(self):
        start = time.monotonic()
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "tiny request [subagents]", "cwd": str(self.repo), "session_id": "budgetfanout"},
            FAKE_CODEX_SLEEP="5",
            CODEX_FUSION_TIMEOUT="10",
            CODEX_FUSION_BUDGET="2",
        )
        elapsed = time.monotonic() - start
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")
        self.assertLess(elapsed, 3.5)
        self.assertLessEqual(len(self.read_log()), 3)

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

    def test_baseline_excludes_tracked_env_change(self):
        (self.repo / ".env").write_text("API_KEY=old\n", encoding="utf-8")
        self.commit_all("add env")
        (self.repo / ".env").write_text("API_KEY=SECRET_VALUE_XYZ\n", encoding="utf-8")
        (self.repo / "README.md").write_text("hello\nvisible change\n", encoding="utf-8")
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "what does this function do?", "cwd": str(self.repo), "session_id": "envx"},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        baseline = self.state_file("envx", "baseline").read_text(encoding="utf-8")
        self.assertIn("visible change", baseline)
        self.assertNotIn("API_KEY", baseline)
        self.assertIn("sensitive, oversized, and binary paths are excluded", baseline)
        prompts = self.codex_prompts()
        self.assertTrue(prompts)
        for prompt in prompts:
            self.assertNotIn("API_KEY", prompt)
        self.assertTrue(any("[redacted sensitive path]" in prompt for prompt in prompts))

    def test_stop_excludes_untracked_key_file(self):
        self.baseline("keyfile")
        (self.repo / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\nHUSHHUSH\n", encoding="utf-8")
        self.modify_repo()
        res = self.run_hook(STOP_HOOK, {"cwd": str(self.repo), "session_id": "keyfile", "stop_hook_active": False})
        self.assertEqual(res.returncode, 0, res.stderr)
        prompts = self.codex_prompts()
        self.assertTrue(prompts)
        for prompt in prompts:
            self.assertNotIn("HUSHHUSH", prompt)
        self.assertTrue(any("server.pem (excluded: sensitive path)" in prompt for prompt in prompts))

    def test_oversized_untracked_file_capped(self):
        self.baseline("bigfile")
        (self.repo / "big.txt").write_text("A" * 300000, encoding="utf-8")
        self.modify_repo()
        res = self.run_hook(STOP_HOOK, {"cwd": str(self.repo), "session_id": "bigfile", "stop_hook_active": False})
        self.assertEqual(res.returncode, 0, res.stderr)
        prompts = self.codex_prompts()
        self.assertTrue(any("big.txt (excluded: 300000 bytes exceeds 204800 cap)" in prompt for prompt in prompts))
        for prompt in prompts:
            self.assertNotIn("AAAAAAAAAA", prompt)

    def test_binary_untracked_file_marker(self):
        self.baseline("binfile")
        (self.repo / "blob.bin").write_bytes(b"\x00\x01\x02payload")
        self.modify_repo()
        res = self.run_hook(STOP_HOOK, {"cwd": str(self.repo), "session_id": "binfile", "stop_hook_active": False})
        self.assertEqual(res.returncode, 0, res.stderr)
        prompts = self.codex_prompts()
        self.assertTrue(any("blob.bin (excluded: binary)" in prompt for prompt in prompts))

    def test_codex_fusion_exclude_extends_denylist(self):
        self.baseline("customx")
        (self.repo / "notes.customsecret").write_text("TOPSECRET\n", encoding="utf-8")
        self.modify_repo()
        res = self.run_hook(
            STOP_HOOK,
            {"cwd": str(self.repo), "session_id": "customx", "stop_hook_active": False},
            CODEX_FUSION_EXCLUDE="*.customsecret",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        prompts = self.codex_prompts()
        self.assertTrue(any("notes.customsecret (excluded: sensitive path)" in prompt for prompt in prompts))
        for prompt in prompts:
            self.assertNotIn("TOPSECRET", prompt)

    def test_commit_during_turn_still_reviewed(self):
        self.baseline("midcommit", prompt="baseline")
        (self.repo / "README.md").write_text("hello\ncommitted change\n", encoding="utf-8")
        self.commit_all("mid-turn commit")
        res = self.run_hook(STOP_HOOK, {"cwd": str(self.repo), "session_id": "midcommit", "stop_hook_active": False})
        self.assertEqual(res.returncode, 0, res.stderr)
        prompts = self.codex_prompts()
        self.assertTrue(prompts, "stop review did not run after a mid-turn commit")
        self.assertTrue(any("committed change" in prompt for prompt in prompts))
        self.assertTrue(self.state_file("midcommit", "reviewed").exists())

    def test_commit_during_turn_dirty_at_prompt(self):
        (self.repo / "README.md").write_text("hello\nearly edit\n", encoding="utf-8")
        self.baseline("dirtycommit", prompt="baseline")
        self.commit_all("commit early edit")
        (self.repo / "README.md").write_text("hello\nearly edit\nlate edit\n", encoding="utf-8")
        res = self.run_hook(STOP_HOOK, {"cwd": str(self.repo), "session_id": "dirtycommit", "stop_hook_active": False})
        self.assertEqual(res.returncode, 0, res.stderr)
        prompts = self.codex_prompts()
        self.assertTrue(prompts)
        joined = "\n".join(prompts)
        self.assertIn("+late edit", joined)
        self.assertNotIn("-+early edit", joined)

    def test_unresolvable_base_sha_falls_back_to_head(self):
        self.baseline("badsha")
        head_file = self.state_file("badsha", "head")
        self.assertTrue(head_file.exists())
        head_file.write_text("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n", encoding="utf-8")
        self.modify_repo()
        res = self.run_hook(STOP_HOOK, {"cwd": str(self.repo), "session_id": "badsha", "stop_hook_active": False})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(self.codex_prompts())
        self.assertTrue(self.state_file("badsha", "reviewed").exists())

    def test_nongit_cwd_warns_once_per_session(self):
        plain = self.base / "plaindir"
        plain.mkdir()
        first = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "what does this function do?", "cwd": str(plain), "session_id": "nogit"},
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        payload = json.loads(first.stdout)
        self.assertIn("not a git repository", payload["systemMessage"])
        self.assertIn("Codex consulted successfully", payload["systemMessage"])
        second = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "what does this function do?", "cwd": str(plain), "session_id": "nogit"},
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        payload2 = json.loads(second.stdout)
        self.assertNotIn("not a git repository", payload2.get("systemMessage", ""))

    def test_nongit_cwd_warns_on_skip_paths(self):
        plain = self.base / "plainskip"
        plain.mkdir()
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "Refactor auth [no-codex]", "cwd": str(plain), "session_id": "nogitskip"},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(set(payload.keys()), {"systemMessage"})
        self.assertIn("not a git repository", payload["systemMessage"])
        self.assertEqual(self.read_log(), [])

    def test_state_dir_unusable_fails_open(self):
        state_path = self.state_dir()
        state_path.write_text("not a directory", encoding="utf-8")
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "what does this function do?", "cwd": str(self.repo), "session_id": "nostate"},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertIn("AUTOMATIC CODEX FUSION CONTEXT", payload["hookSpecificOutput"]["additionalContext"])
        stop = self.run_hook(STOP_HOOK, {"cwd": str(self.repo), "session_id": "nostate", "stop_hook_active": False})
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertEqual(stop.stdout, "")

    def test_truncation_marker_on_large_diff(self):
        self.baseline("bigdiff", prompt="baseline")
        lines = "".join(f"line {i} padding padding padding\n" for i in range(1500))
        (self.repo / "README.md").write_text("hello\n" + lines, encoding="utf-8")
        res = self.run_hook(STOP_HOOK, {"cwd": str(self.repo), "session_id": "bigdiff", "stop_hook_active": False})
        self.assertEqual(res.returncode, 0, res.stderr)
        prompts = self.codex_prompts()
        self.assertTrue(any("[... incremental diff truncated at 20000 bytes ...]" in prompt for prompt in prompts))

    def test_clean_tree_stop_skips_review(self):
        self.baseline("cleantree", prompt="baseline")
        res = self.run_hook(STOP_HOOK, {"cwd": str(self.repo), "session_id": "cleantree", "stop_hook_active": False})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")
        self.assertEqual(self.read_log(), [], "unchanged surface must not trigger a review")
        self.assertFalse(self.state_file("cleantree", "reviewed").exists())

    def test_subdir_cwd_still_excludes_root_secrets(self):
        (self.repo / ".env").write_text("ROOT=old\n", encoding="utf-8")
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / "code.py").write_text("print('v1')\n", encoding="utf-8")
        self.commit_all("add env and subdir")
        (self.repo / ".env").write_text("ROOT=ROOTSECRET_LEAK\n", encoding="utf-8")
        (sub / "code.py").write_text("print('v2 visible')\n", encoding="utf-8")
        res = self.run_hook(
            USERPROMPT_HOOK,
            {"prompt": "what does this function do?", "cwd": str(sub), "session_id": "subdir"},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        baseline = self.state_file("subdir", "baseline").read_text(encoding="utf-8")
        self.assertIn("v2 visible", baseline)
        self.assertNotIn("ROOTSECRET_LEAK", baseline, "cwd in a subdirectory must not bypass the exclude pathspecs")
        for prompt in self.codex_prompts():
            self.assertNotIn("ROOTSECRET_LEAK", prompt)

    def test_huge_review_still_blocks(self):
        self.baseline("hugereview", prompt="baseline")
        self.modify_repo()
        res = self.run_hook(
            STOP_HOOK,
            {"cwd": str(self.repo), "session_id": "hugereview", "stop_hook_active": False},
            FAKE_CODEX_ISSUE_ROLES="single-review",
            FAKE_CODEX_BLOAT="200000",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["decision"], "block", "a >128KiB review must not be dropped by the env handoff")
        self.assertIn("issue from single-review", payload["reason"])
        self.assertTrue(self.state_file("hugereview", "reviewed").exists())

    def test_installer_norm_matching_no_duplicates(self):
        config_dir = self.home / ".claude"
        config_dir.mkdir(parents=True)
        snippet = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "$HOME/.claude/hooks/codex-fusion-userprompt.sh",
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
                                "command": "$HOME/.claude/hooks/codex-fusion-stop.sh",
                                "timeout": 30,
                            }
                        ]
                    }
                ],
            }
        }
        (config_dir / "settings.json").write_text(json.dumps(snippet), encoding="utf-8")
        env = self.env()
        env.pop("CLAUDE_CONFIG_DIR", None)
        res = subprocess.run([str(INSTALL)], cwd=ROOT, text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(res.returncode, 0, res.stderr)
        settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
        expected = {
            "UserPromptSubmit": ("$HOME/.claude/hooks/codex-fusion-userprompt.sh", "Codex Fusion: checking Codex..."),
            "Stop": ("$HOME/.claude/hooks/codex-fusion-stop.sh", "Codex Fusion: reviewing changes..."),
        }
        for event, (command, status) in expected.items():
            hooks = [
                hook
                for group in settings["hooks"][event]
                for hook in group["hooks"]
                if "codex-fusion" in hook["command"]
            ]
            self.assertEqual(len(hooks), 1)
            self.assertEqual(hooks[0]["command"], command)
            self.assertEqual(hooks[0]["timeout"], 270)
            self.assertEqual(hooks[0]["statusMessage"], status)

    def test_installer_skips_write_when_unchanged(self):
        config_dir = self.base / "claude2"
        env = self.env(CLAUDE_CONFIG_DIR=str(config_dir))
        first = subprocess.run([str(INSTALL)], cwd=ROOT, text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(first.returncode, 0, first.stderr)
        settings_path = config_dir / "settings.json"
        before = settings_path.read_text(encoding="utf-8")
        second = subprocess.run([str(INSTALL)], cwd=ROOT, text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("nothing to change", second.stdout)
        self.assertEqual(settings_path.read_text(encoding="utf-8"), before)
        self.assertEqual(list(config_dir.glob(".settings.*")), [])
        self.assertEqual(list(config_dir.glob("*.codex-fusion.bak")), [])

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
