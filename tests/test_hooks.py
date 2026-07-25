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
SUBAGENT_STOP_HOOK = ROOT / "hooks" / "codex-fusion-subagent-stop.sh"
INSTALL = ROOT / "install.sh"
UNINSTALL = ROOT / "uninstall.sh"


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
                verify_roles = {"subagent-verify", "subagent-claim-verification", "subagent-defect-hunt"}
                if role in review_roles:
                    if role in issue_roles:
                        content = f"CODEX_REVIEW_VERDICT: ISSUES_FOUND\\n- fake.py:1 - issue from {role} - fix it\\n"
                    else:
                        content = f"CODEX_REVIEW_VERDICT: PASS\\n{role} passed\\n"
                elif role in verify_roles:
                    if role in issue_roles:
                        content = f"CODEX_VERIFY_VERDICT: ISSUES_FOUND\\n- claim:1 - unsupported claim caught by {role} - recheck it\\n"
                    else:
                        content = f"CODEX_VERIFY_VERDICT: PASS\\n{role} verified\\n"
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

    def agent_state_file(self, session, agent_id, suffix):
        return self.state_dir() / f"session-{session}.agent-{agent_id}.{suffix}"

    def subagent_payload(self, session, message, agent_id="a1", agent_type="general-purpose", **extra):
        payload = {
            "cwd": str(self.repo),
            "session_id": session,
            "stop_hook_active": False,
            "agent_id": agent_id,
            "agent_type": agent_type,
            "last_assistant_message": message,
        }
        payload.update(extra)
        return payload

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

    # ---- SubagentStop: Codex as adversarial verifier of Claude Code subagents ----

    def test_subagent_verification_passes_and_records_hash(self):
        self.baseline("subpass", prompt="baseline")
        self.modify_repo()
        res = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("subpass", "I refactored README and all tests pass."))
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["systemMessage"], "Codex Fusion: general-purpose subagent adversarially verified (PASS).")
        self.assertNotIn("decision", payload)
        self.assertEqual([entry["role"] for entry in self.read_log()], ["subagent-verify"])
        self.assertTrue(self.agent_state_file("subpass", "a1", "verified").exists())

    def test_subagent_verification_blocks_on_issues(self):
        self.baseline("subissue", prompt="baseline")
        self.modify_repo()
        res = self.run_hook(
            SUBAGENT_STOP_HOOK,
            self.subagent_payload("subissue", "Done: I rewrote the auth layer and every test passes."),
            FAKE_CODEX_ISSUE_ROLES="subagent-verify",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("SUBAGENT ADVERSARIAL VERIFICATION", payload["reason"])
        self.assertIn("unsupported claim caught by subagent-verify", payload["reason"])
        self.assertEqual(self.agent_state_file("subissue", "a1", "blocks").read_text(encoding="utf-8").strip(), "1")

    def test_subagent_verification_never_marks_parent_diff_reviewed(self):
        """The parent Stop review must still run after a subagent was verified."""
        self.baseline("subparent", prompt="baseline")
        self.modify_repo()
        sub = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("subparent", "A" * 400))
        self.assertEqual(sub.returncode, 0, sub.stderr)
        self.assertFalse(
            self.state_file("subparent", "reviewed").exists(),
            "subagent verification must not consume the parent's reviewed-diff hash",
        )
        self.clear_log()
        stop = self.run_hook(STOP_HOOK, {"cwd": str(self.repo), "session_id": "subparent", "stop_hook_active": False})
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertEqual([entry["role"] for entry in self.read_log()], ["single-review"])
        self.assertTrue(self.state_file("subparent", "reviewed").exists())

    def test_subagent_stop_hook_active_and_no_codex_skip(self):
        self.baseline("subskip", prompt="baseline")
        self.modify_repo()
        looping = self.run_hook(
            SUBAGENT_STOP_HOOK,
            self.subagent_payload("subskip", "B" * 400, stop_hook_active=True),
        )
        self.assertEqual(looping.returncode, 0, looping.stderr)
        self.assertEqual(looping.stdout, "")
        self.assertEqual(self.read_log(), [])

        self.run_hook(USERPROMPT_HOOK, {"prompt": "leave it [no-codex]", "cwd": str(self.repo), "session_id": "subskip"})
        self.clear_log()
        opted_out = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("subskip", "B" * 400))
        self.assertEqual(opted_out.returncode, 0, opted_out.stderr)
        self.assertEqual(opted_out.stdout, "")
        self.assertEqual(self.read_log(), [])

    def test_subagent_verify_off_and_nested_skip(self):
        self.baseline("suboff", prompt="baseline")
        self.modify_repo()
        off = self.run_hook(
            SUBAGENT_STOP_HOOK,
            self.subagent_payload("suboff", "C" * 400),
            CODEX_FUSION_SUBAGENT_VERIFY="off",
        )
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertEqual(off.stdout, "")
        self.assertEqual(self.read_log(), [])

        nested = self.run_hook(
            SUBAGENT_STOP_HOOK,
            self.subagent_payload("suboff", "C" * 400),
            CODEX_FUSION_ACTIVE="1",
        )
        self.assertEqual(nested.returncode, 0, nested.stderr)
        self.assertEqual(nested.stdout, "")
        self.assertEqual(self.read_log(), [])

    def test_short_report_on_clean_tree_skips_but_always_mode_verifies(self):
        self.baseline("subgate", prompt="baseline")
        quiet = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("subgate", "ok"))
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertEqual(quiet.stdout, "")
        self.assertEqual(self.read_log(), [], "a trivial report on an unchanged tree is not worth a Codex call")

        forced = self.run_hook(
            SUBAGENT_STOP_HOOK,
            self.subagent_payload("subgate", "ok"),
            CODEX_FUSION_SUBAGENT_VERIFY="always",
        )
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual([entry["role"] for entry in self.read_log()], ["subagent-verify"])

    def test_clean_tree_report_only_subagent_is_still_verified(self):
        """A read-only subagent that changed nothing still gets its claims checked."""
        self.baseline("subreadonly", prompt="baseline")
        res = self.run_hook(
            SUBAGENT_STOP_HOOK,
            self.subagent_payload("subreadonly", "I searched the repo and confirmed there is no retry logic. " * 8),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([entry["role"] for entry in self.read_log()], ["subagent-verify"])
        prompts = self.codex_prompts()
        self.assertTrue(any("no repository changes since the prompt-start baseline" in p for p in prompts))
        self.assertTrue(any("ADVERSARIAL VERIFICATION" in p for p in prompts))

    def test_identical_repeat_payload_is_not_reverified(self):
        self.baseline("subdedup", prompt="baseline")
        self.modify_repo()
        payload = self.subagent_payload("subdedup", "D" * 400)
        first = self.run_hook(SUBAGENT_STOP_HOOK, payload)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(self.read_log()), 1)
        second = self.run_hook(SUBAGENT_STOP_HOOK, payload)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "")
        self.assertEqual(len(self.read_log()), 1, "unchanged report + unchanged diff must not re-run Codex")

    def test_parallel_subagents_get_independent_state(self):
        self.baseline("subpar", prompt="baseline")
        self.modify_repo()
        message = "E" * 400
        for agent_id in ("a1", "a2"):
            res = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("subpar", message, agent_id=agent_id))
            self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(self.read_log()), 2, "identical reports from different agents must each be verified")
        self.assertTrue(self.agent_state_file("subpar", "a1", "verified").exists())
        self.assertTrue(self.agent_state_file("subpar", "a2", "verified").exists())

    def test_missing_agent_id_still_separates_distinct_reports(self):
        self.baseline("subanon", prompt="baseline")
        self.modify_repo()
        for message in ("F" * 400, "G" * 400):
            res = self.run_hook(
                SUBAGENT_STOP_HOOK,
                {
                    "cwd": str(self.repo),
                    "session_id": "subanon",
                    "stop_hook_active": False,
                    "agent_type": "Explore",
                    "last_assistant_message": message,
                },
            )
            self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(self.read_log()), 2)
        anon = list(self.state_dir().glob("session-subanon.agent-anon-*.verified"))
        self.assertEqual(len(anon), 2, "distinct reports without agent_id must not share one state file")

    def test_truncated_private_key_in_report_is_still_redacted(self):
        """A key block whose END marker falls past the truncation point must not leak its body."""
        self.baseline("subcutkey", prompt="baseline")
        self.modify_repo()
        report = (
            "padding line\n" * 1400
            + "-----BEGIN RSA PRIVATE KEY-----\nLEAKEDKEYBODY_MIIEpAIBAAKCAQEA\n"
            + "keybody\n" * 500
            + "-----END RSA PRIVATE KEY-----\n"
        )
        self.assertGreater(len(report), 20000, "the key block must straddle the emitted cap")
        res = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("subcutkey", report))
        self.assertEqual(res.returncode, 0, res.stderr)
        prompts = self.codex_prompts()
        self.assertTrue(prompts)
        joined = "\n".join(prompts)
        self.assertNotIn("LEAKEDKEYBODY", joined, "truncation must not strand a key body past redaction")
        self.assertNotIn("BEGIN RSA PRIVATE KEY", joined)
        # Redaction now runs before the emitted cap, so the whole block still matches the paired rule.
        self.assertIn("[redacted: private key block]", joined)

    def test_private_key_cut_by_the_hard_cap_is_redacted(self):
        """Beyond HARD_CAP there is no END marker left, so the dangling-BEGIN rule must fire."""
        self.baseline("subhardcap", prompt="baseline")
        self.modify_repo()
        report = (
            "x" * 199000
            + "\n-----BEGIN OPENSSH PRIVATE KEY-----\nHARDCAPLEAK_MIIEpAIBAAKCAQEA\n"
            + "keybody\n" * 2000
            + "-----END OPENSSH PRIVATE KEY-----\n"
        )
        self.assertGreater(len(report), 200000, "the key must start below HARD_CAP and end above it")
        res = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("subhardcap", report))
        self.assertEqual(res.returncode, 0, res.stderr)
        joined = "\n".join(self.codex_prompts())
        self.assertNotIn("HARDCAPLEAK", joined)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", joined)

    def test_unchanged_blocked_report_reblocks_from_cache(self):
        """A subagent that stops again with an identical bad report must not slip through."""
        self.baseline("subrepeat", prompt="baseline")
        self.modify_repo()
        payload = self.subagent_payload("subrepeat", "Q" * 400)
        extra = {"FAKE_CODEX_ISSUE_ROLES": "subagent-verify", "CODEX_FUSION_SUBAGENT_BLOCK_LIMIT": "2"}

        first = self.run_hook(SUBAGENT_STOP_HOOK, payload, **extra)
        self.assertEqual(json.loads(first.stdout)["decision"], "block")
        self.assertEqual(len(self.read_log()), 1)
        self.assertFalse(
            self.agent_state_file("subrepeat", "a1", "verified").exists(),
            "an outstanding blocking verdict must not be recorded as settled",
        )

        second = self.run_hook(SUBAGENT_STOP_HOOK, payload, **extra)
        second_payload = json.loads(second.stdout)
        self.assertEqual(second_payload["decision"], "block", "identical unfixed report must block again")
        self.assertIn("unsupported claim caught by subagent-verify", second_payload["reason"])
        self.assertEqual(len(self.read_log()), 1, "the cached verdict must be replayed, not re-run through Codex")

        third = self.run_hook(SUBAGENT_STOP_HOOK, payload, **extra)
        third_payload = json.loads(third.stdout)
        self.assertNotIn("decision", third_payload, "the block cap still bounds the loop")
        self.assertIn("block limit (2) was reached", third_payload["systemMessage"])
        self.assertTrue(self.agent_state_file("subrepeat", "a1", "verified").exists())

    def test_pass_after_block_clears_cached_finding(self):
        self.baseline("subfixed", prompt="baseline")
        self.modify_repo()
        blocked = self.run_hook(
            SUBAGENT_STOP_HOOK,
            self.subagent_payload("subfixed", "R" * 400),
            FAKE_CODEX_ISSUE_ROLES="subagent-verify",
        )
        self.assertEqual(json.loads(blocked.stdout)["decision"], "block")
        self.assertTrue(self.agent_state_file("subfixed", "a1", "finding").exists())

        fixed = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("subfixed", "R" * 400 + " now corrected"))
        self.assertEqual(fixed.returncode, 0, fixed.stderr)
        self.assertNotIn("decision", json.loads(fixed.stdout))
        self.assertFalse(self.agent_state_file("subfixed", "a1", "finding").exists())

    def test_block_limit_surfaces_findings_instead_of_blocking_again(self):
        self.baseline("sublimit", prompt="baseline")
        self.modify_repo()
        extra = {"FAKE_CODEX_ISSUE_ROLES": "subagent-verify", "CODEX_FUSION_SUBAGENT_BLOCK_LIMIT": "1"}
        first = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("sublimit", "H" * 400), **extra)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["decision"], "block")

        second = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("sublimit", "H" * 400 + " revised"), **extra)
        self.assertEqual(second.returncode, 0, second.stderr)
        payload = json.loads(second.stdout)
        self.assertNotIn("decision", payload)
        self.assertIn("block limit (1) was reached", payload["systemMessage"])

    def test_forced_fanout_verification(self):
        self.baseline("subfan", prompt="baseline [subagents]")
        self.modify_repo()
        res = self.run_hook(
            SUBAGENT_STOP_HOOK,
            self.subagent_payload("subfan", "I" * 400),
            FAKE_CODEX_ISSUE_ROLES="subagent-defect-hunt",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertCountEqual(
            [entry["role"] for entry in self.read_log()],
            ["subagent-claim-verification", "subagent-defect-hunt"],
        )
        payload = json.loads(res.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("spawned 2 verification sub-agents; 2/2 succeeded", payload["reason"])

    def test_transient_verify_failure_retries_then_gives_up(self):
        self.baseline("subfail", prompt="baseline")
        self.modify_repo()
        payload = self.subagent_payload("subfail", "J" * 400)
        extra = {"FAKE_CODEX_FAIL_ROLES": "subagent-verify", "CODEX_FUSION_STOP_RETRY_LIMIT": "2"}
        for expected_calls in (2, 4, 4):
            res = self.run_hook(SUBAGENT_STOP_HOOK, payload, **extra)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(res.stdout, "")
            # rc=7 is not the model-arg failure path, so each attempt also burns the no-model retry.
            self.assertEqual(len(self.read_log()), expected_calls)
        self.assertFalse(self.agent_state_file("subfail", "a1", "verified").exists())

    def test_subagent_report_secrets_are_redacted(self):
        self.baseline("subredact", prompt="baseline")
        self.modify_repo()
        report = (
            "Here is what I found while wiring the client:\n"
            "aws id AKIAIOSFODNN7EXAMPLE and github ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
            'api_key = "sup3rs3cr3tvalue123"\n'
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.QWERTYUIOPasdfghjkl\n"
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEAxHUSHVALUE\n-----END RSA PRIVATE KEY-----\n"
        )
        res = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("subredact", report))
        self.assertEqual(res.returncode, 0, res.stderr)
        prompts = self.codex_prompts()
        self.assertTrue(prompts)
        joined = "\n".join(prompts)
        for secret in (
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "sup3rs3cr3tvalue123",
            "QWERTYUIOPasdfghjkl",
            "MIIEpAIBAAKCAQEAxHUSHVALUE",
        ):
            self.assertNotIn(secret, joined, f"{secret} must be redacted before reaching Codex")
        for marker in ("[redacted: aws key id]", "[redacted: github token]", "[redacted: private key block]"):
            self.assertIn(marker, joined)

    def test_subagent_hook_never_reads_transcripts(self):
        self.baseline("subtrans", prompt="baseline")
        self.modify_repo()
        transcript = self.base / "agent-transcript.jsonl"
        transcript.write_text('{"text":"TRANSCRIPTMARKER_DO_NOT_LEAK"}\n', encoding="utf-8")
        res = self.run_hook(
            SUBAGENT_STOP_HOOK,
            self.subagent_payload(
                "subtrans",
                "K" * 400,
                transcript_path=str(transcript),
                agent_transcript_path=str(transcript),
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        prompts = self.codex_prompts()
        self.assertTrue(prompts)
        for prompt in prompts:
            self.assertNotIn("TRANSCRIPTMARKER_DO_NOT_LEAK", prompt)
            self.assertNotIn(str(transcript), prompt)

    def test_huge_subagent_report_is_truncated(self):
        self.baseline("subhuge", prompt="baseline")
        self.modify_repo()
        res = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("subhuge", "L" * 60000))
        self.assertEqual(res.returncode, 0, res.stderr)
        prompts = self.codex_prompts()
        self.assertTrue(any("[... subagent report truncated at 20000 chars ...]" in p for p in prompts))

    def test_huge_subagent_verification_still_blocks(self):
        self.baseline("subhugeverdict", prompt="baseline")
        self.modify_repo()
        res = self.run_hook(
            SUBAGENT_STOP_HOOK,
            self.subagent_payload("subhugeverdict", "M" * 400),
            FAKE_CODEX_ISSUE_ROLES="subagent-verify",
            FAKE_CODEX_BLOAT="200000",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["decision"], "block", "a >128KiB verification must survive the env handoff")
        self.assertIn("unsupported claim caught by subagent-verify", payload["reason"])

    def test_subagent_verification_excludes_sensitive_paths(self):
        self.baseline("subsecret", prompt="baseline")
        (self.repo / "deploy.key").write_text("PRIVATEKEYBYTES\n", encoding="utf-8")
        self.modify_repo()
        res = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("subsecret", "N" * 400))
        self.assertEqual(res.returncode, 0, res.stderr)
        prompts = self.codex_prompts()
        self.assertTrue(prompts)
        for prompt in prompts:
            self.assertNotIn("PRIVATEKEYBYTES", prompt)
        self.assertTrue(any("deploy.key (excluded: sensitive path)" in prompt for prompt in prompts))

    def test_new_turn_clears_stale_subagent_state(self):
        self.baseline("subclear", prompt="baseline")
        self.modify_repo()
        res = self.run_hook(SUBAGENT_STOP_HOOK, self.subagent_payload("subclear", "O" * 400))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(self.agent_state_file("subclear", "a1", "verified").exists())
        self.baseline("subclear", prompt="next turn")
        self.assertEqual(list(self.state_dir().glob("session-subclear.agent-*")), [])

    def test_notify_zero_suppresses_subagent_pass_notice_only(self):
        self.baseline("subquiet", prompt="baseline")
        self.modify_repo()
        quiet = self.run_hook(
            SUBAGENT_STOP_HOOK,
            self.subagent_payload("subquiet", "P" * 400),
            CODEX_FUSION_NOTIFY="0",
        )
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertEqual(quiet.stdout, "")
        self.assertEqual([entry["role"] for entry in self.read_log()], ["subagent-verify"])

        self.clear_log()
        blocked = self.run_hook(
            SUBAGENT_STOP_HOOK,
            self.subagent_payload("subquiet", "P" * 400, agent_id="a2"),
            CODEX_FUSION_NOTIFY="0",
            FAKE_CODEX_ISSUE_ROLES="subagent-verify",
        )
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(json.loads(blocked.stdout)["decision"], "block", "NOTIFY=0 must not hide a blocking verdict")

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
                "SubagentStop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "$HOME/.claude/hooks/codex-fusion-subagent-stop.sh",
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
            "SubagentStop": (
                "$HOME/.claude/hooks/codex-fusion-subagent-stop.sh",
                "Codex Fusion: verifying subagent...",
            ),
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
        self.assertTrue((config_dir / "hooks" / "codex-fusion-subagent-stop.sh").exists())
        self.assertTrue(os.access(config_dir / "hooks" / "codex-fusion-subagent-stop.sh", os.X_OK))
        settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
        expected_status = {
            "UserPromptSubmit": "Codex Fusion: checking Codex...",
            "Stop": "Codex Fusion: reviewing changes...",
            "SubagentStop": "Codex Fusion: verifying subagent...",
        }
        for event in ("UserPromptSubmit", "Stop", "SubagentStop"):
            hooks = [
                hook
                for group in settings["hooks"][event]
                for hook in group["hooks"]
                if "codex-fusion" in hook["command"]
            ]
            self.assertEqual(len(hooks), 1, f"{event} must have exactly one Codex Fusion entry")
            self.assertEqual(hooks[0]["timeout"], 270)
            self.assertEqual(hooks[0]["statusMessage"], expected_status[event])

    def test_uninstall_removes_all_hook_entries_and_scripts(self):
        config_dir = self.base / "claude-uninstall"
        env = self.env(CLAUDE_CONFIG_DIR=str(config_dir))
        installed = subprocess.run([str(INSTALL)], cwd=ROOT, text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        settings_path = config_dir / "settings.json"
        before = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertIn("SubagentStop", before["hooks"])

        removed = subprocess.run([str(UNINSTALL)], cwd=ROOT, text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        after = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertNotIn("hooks", after)
        for script in (
            "codex-fusion-common.sh",
            "codex-fusion-userprompt.sh",
            "codex-fusion-stop.sh",
            "codex-fusion-subagent-stop.sh",
        ):
            self.assertFalse((config_dir / "hooks" / script).exists(), script)

    def test_uninstall_preserves_unrelated_subagent_stop_hooks(self):
        config_dir = self.base / "claude-mixed"
        env = self.env(CLAUDE_CONFIG_DIR=str(config_dir))
        installed = subprocess.run([str(INSTALL)], cwd=ROOT, text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        settings_path = config_dir / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings["hooks"]["SubagentStop"].append(
            {
                "hooks": [
                    {"type": "command", "command": "/usr/local/bin/my-other-hook.sh"},
                    # Substring matching on "codex-fusion" would wrongly delete these user hooks.
                    {"type": "command", "command": "/usr/local/bin/my-codex-fusion-wrapper.sh"},
                    {"type": "command", "command": "/opt/tools/codex-fusion-notifier.sh"},
                ]
            }
        )
        settings_path.write_text(json.dumps(settings), encoding="utf-8")

        removed = subprocess.run([str(UNINSTALL)], cwd=ROOT, text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        after = json.loads(settings_path.read_text(encoding="utf-8"))
        remaining = [h["command"] for g in after["hooks"]["SubagentStop"] for h in g["hooks"]]
        self.assertEqual(
            remaining,
            [
                "/usr/local/bin/my-other-hook.sh",
                "/usr/local/bin/my-codex-fusion-wrapper.sh",
                "/opt/tools/codex-fusion-notifier.sh",
            ],
        )


if __name__ == "__main__":
    unittest.main()
