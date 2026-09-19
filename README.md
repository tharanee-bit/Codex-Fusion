# Codex Fusion

**Automatic peer review for Claude Code, powered by your local Codex CLI.**

Codex Fusion makes [Claude Code](https://claude.com/claude-code) automatically consult
[OpenAI Codex CLI](https://github.com/openai/codex) as an independent second opinion on nearly
every non-empty prompt — *without* a slash command, and *without* you typing anything.
It uses Claude Code **hooks**:

- **Before** Claude plans or edits, a `UserPromptSubmit` hook runs Codex **read-only** over your
  repo and injects Codex's independent analysis into Claude's context. For broader or riskier
  prompts, it can automatically fan out to bounded `planner`, `skeptic`, and `verifier` Codex
  sub-agents before synthesizing their reports for Claude.
- **After** Claude finishes with changes since the prompt-start baseline, a `Stop` hook runs Codex
  **read-only** over the incremental tracked/untracked review surface. Larger or high-risk diffs can
  fan out to `correctness`, `security-data-loss-concurrency`, and `tests-regression` reviewers. If
  any reviewer flags serious problems, Claude is asked to address them before finalizing.
- **When a Claude Code subagent finishes**, a `SubagentStop` hook runs Codex **read-only** as an
  *adversarial verifier* of that subagent's final report — before the report reaches its parent.
  Codex is told to assume the report may be wrong and to try to refute it against the repository:
  fabricated paths or APIs, claims contradicted by the code, unsupported "done / tests pass"
  assertions, work the report claims but never made, and serious defects in changes the subagent
  did make. If Codex finds concrete evidence of a problem, the subagent is blocked and must fix or
  refute the findings first.

Claude stays the editor and the final judge. Codex only advises, reviews, and verifies — it is
always **read-only** and never edits your files.

> **No credential games.** Codex Fusion shells out to the official `codex exec` CLI that you are
> already logged into. It does **not** use browser cookies, ChatGPT scraping, private APIs, token
> extraction, or inspection of `~/.codex/auth.json`.

---

## How it works

```
                    ┌─────────────────────────── you type a prompt
                    ▼
   UserPromptSubmit hook ── pure ack / [no-codex] / nested? ──yes──▶ (silent)
        │ no
        ▼
   1 or more codex exec --sandbox read-only peers
        │
        ▼
   synthesized analysis injected as additionalContext + visible systemMessage
        │
        ▼
   Claude synthesizes Claude + Codex, then edits
        │
        ├───────────────── Claude spawns a subagent ─────────────────┐
        │                                                            ▼
        │                                        SubagentStop hook ── [no-codex] / trivial
        │                                                │           report + clean tree? ──▶ (silent)
        │                                                ▼ no
        │                                        codex exec --sandbox read-only
        │                                        adversarial verifier over the
        │                                        subagent's report + the diff
        │                                                │
        │                                        ├─ PASS         ──▶ report goes to the parent
        │                                        └─ ISSUES_FOUND ──▶ subagent must fix/refute first
        │                                                            (bounded blocks per subagent)
        ▼
   Stop hook ── changes since prompt baseline? ──no──▶ (Claude finishes)
        │ yes
        ▼
   1 or more codex exec --sandbox read-only reviewers over the diff
        │
        ├─ verdict PASS         ──▶ Claude finishes
        └─ verdict ISSUES_FOUND ──▶ Claude must address them first (blocks once)
```

The UserPromptSubmit hook records a prompt-start review-surface baseline plus the prompt-time
`HEAD` commit in `${TMPDIR:-/tmp}/codex-fusion-state-<uid>/` (per-user, mode 0700, ownership
validated before use; an old shared `/tmp/codex-fusion-state` dir from earlier versions can be
deleted). The Stop hook diffs the working tree against that prompt-time commit and compares the
result to the baseline, so pre-existing dirty work does not trigger an unrelated review, commits
made mid-turn still get reviewed, and an unchanged already-reviewed surface does not get reviewed
again on every later Stop. If a Stop review path fails transiently, the unchanged diff is retried a
bounded number of times and then skipped until the diff changes.

Subagent verification uses that same prompt-start baseline, but **all of its state is keyed by
`agent_id`**, never by the session alone. Two consequences matter:

- Parallel subagents cannot race or clobber each other's verification state.
- A subagent verification **never** marks the parent turn's diff as reviewed, so the final `Stop`
  review of the whole turn still runs. (This is a real trap: reusing the Stop hook's reviewed-diff
  hash here would silently suppress the end-of-turn review.)

Each subagent costs at most one Codex call per distinct *(report + diff)* pair. A subagent that stops
again having changed nothing does not pay for a second identical Codex run — but it does not slip
through either: the blocking verdict is cached and replayed, so an unfixed report keeps blocking up
to `CODEX_FUSION_SUBAGENT_BLOCK_LIMIT` times. Per-agent state is cleared at the start of every new
turn.

Every path that declines to block still emits the **full findings** as a `systemMessage`, and that
message is not suppressed by `CODEX_FUSION_NOTIFY=0` — that knob hides *success* notices, and
unresolved findings are the opposite. Two such paths exist: hitting the block cap, and an unusable
state directory. In the latter case the block counter cannot be persisted, so the cap is
unenforceable and the hook declines to block at all rather than risk repeating without bound (the
`Stop` hook fails safe here only because it requires a baseline file, which this hook deliberately
does not).

If the working directory is not a git repository (for example a folder that merely contains
repositories), the Stop diff review cannot run; Codex Fusion tells you so once per session via a
visible `systemMessage` instead of failing silently. Open a concrete repository folder to get the
post-diff review back. Subagent verification still runs without a diff — verifying a read-only
subagent's *claims* is worthwhile even when nothing changed on disk.

Claude Code also shows a live hook status message while Codex Fusion runs. After a successful
pre-prompt consult, Codex Fusion emits a human-facing `systemMessage`; fanout notices include the
actual spawned sub-agent count and how many returned usable output. Fanout Stop reviews emit the
same count when they block on `ISSUES_FOUND`, and emit a PASS notice when all spawned review agents
pass. Subagent verification emits its own notice naming the verified agent type, for example
`Codex Fusion: Explore subagent adversarially verified (PASS).`

## Requirements

- **Claude Code** (CLI, desktop, web, or IDE extension) with user-level hooks support.
- **Codex CLI** installed and signed in: `codex login status` should report logged in.
- **python3**, **git**, **bash**, and GNU coreutils (`timeout`, `mktemp`, `base64`).
  `jq` is *not* required — JSON is handled with python3.

Tested on Linux / WSL2 and macOS. The hooks stay compatible with bash 3.2, which is what
`#!/usr/bin/env bash` resolves to on a stock macOS.

## Install

```bash
git clone https://github.com/tharanee-bit/Codex-Fusion.git
cd Codex-Fusion
./install.sh
```

`install.sh` copies the hook scripts and skill into `~/.claude/` and **merges** the three hooks into
`~/.claude/settings.json` non-destructively (it backs the file up to
`settings.json.codex-fusion.bak` first, and is idempotent — re-running won't duplicate entries).
It respects `CLAUDE_CONFIG_DIR` if you set it.

New hooks load at session start, so **restart Claude Code / reload the window**, then run `/hooks`
to confirm `UserPromptSubmit`, `Stop`, and `SubagentStop` list the Codex Fusion scripts. The
`SubagentStop` entry is registered without a matcher, so it covers every subagent type including
custom and plugin-scoped ones. If your Claude Code build is too old to show `SubagentStop`, upgrade
it — the other two hooks keep working either way.

### Manual install

Copy `hooks/*.sh` into `~/.claude/hooks/` (and `chmod +x` them), copy
`skills/codex-fusion-auto/` into `~/.claude/skills/`, then merge `settings.snippet.json` into the
`hooks` object of `~/.claude/settings.json`.

## Configuration

| Knob | Default | Effect |
|---|---|---|
| `[no-codex]` in your prompt | — | Skips Codex entirely for that prompt. |
| `[subagents]` or `[codex-subagents]` in your prompt | — | Forces bounded sub-agent fanout for that prompt and its Stop review. |
| `[no-subagents]` in your prompt | — | Keeps that prompt and its Stop review on the single-Codex path. |
| `CODEX_FUSION_MODEL` | `gpt-5.6-luna` | Codex model to use. Defaults to the strongest available model. |
| `CODEX_FUSION_EFFORT` | `max` | Codex reasoning effort (`low` / `medium` / `high` / `xhigh` / `max`). |
| `CODEX_FUSION_FALLBACK_EFFORT` | `xhigh` | Reasoning effort for the degraded retry. The retry always keeps the same model. |
| `CODEX_FUSION_SUBAGENTS` | `auto` | Sub-agent policy: `auto`, `off`, or `always`. Prompt markers still select per-turn behavior. |
| `CODEX_FUSION_SUBAGENT_VERIFY` | `auto` | Adversarial verification of Claude Code subagents: `auto`, `off`, or `always`. `off` disables the `SubagentStop` Codex call entirely. |
| `CODEX_FUSION_SUBAGENT_MIN_CHARS` | `200` | In `auto` mode, a subagent whose tree is unchanged is verified only when its report is at least this many bytes. A subagent that touched the tree is always verified. |
| `CODEX_FUSION_SUBAGENT_BLOCK_LIMIT` | `2` | Maximum times one subagent can be blocked by verification. At the cap, findings are surfaced as a `systemMessage` instead. |
| `CODEX_FUSION_MAX_AGENTS` | `4` | Hard cap for hook-launched Codex agents and any bounded internal delegation contract. |
| `CODEX_FUSION_TIMEOUT` | `180` | Per-agent timeout in seconds. The Claude hook registration timeout remains 270s. |
| `CODEX_FUSION_BUDGET` | `250` | Whole-hook wall-clock budget in seconds. Every primary/fallback attempt is capped by the shared remaining budget. Keep this below the 270s Claude hook registration. |
| `CODEX_FUSION_STOP_RETRY_LIMIT` | `2` | Number of transient failed Stop review / subagent verification attempts for unchanged input before skipping. |
| `CODEX_FUSION_NOTIFY` | `1` | Set to `0` to suppress human-facing success `systemMessage` notices. Context injection, blocking review reasons, unresolved subagent verification findings, and the non-git-repo warning still work. |
| `CODEX_FUSION_EXCLUDE` | — | Extra space-separated globs to exclude from every review surface, on top of the built-in sensitive-path denylist (globs containing spaces are unsupported). |
| `CODEX_FUSION_MAX_FILE_BYTES` | `204800` | Per-file size cap for untracked files embedded in the review surface; larger files appear as an exclusion marker only. |
| `CODEX_FUSION_DEBUG=1` | off | Logs gate decisions to `${TMPDIR:-/tmp}/codex-fusion-state-<uid>/debug.log`. |

> **Strongest model, max effort.** Claude Code is the orchestrator here, so the adversarial
> verifier is deliberately cross-family: `gpt-5.6-luna` at `max` reasoning effort by default. The
> model is pinned in the shared hook helper (`CODEX_MODEL`) and overridable via
> `CODEX_FUSION_MODEL` — bump it when a newer top model ships. If the pinned model fails, the hook
> retries once with the **same** model at `CODEX_FUSION_FALLBACK_EFFORT` (`xhigh`): a degraded
> retry relaxes effort only, so it can never silently swap in a different adversarial verifier.
> Lower `CODEX_FUSION_EFFORT` to `high` or `medium` for a faster (shallower, cheaper) review.
>
> This costs latency: most prompts now wait for Codex before Claude responds. Fanout runs agents in
> parallel, but it can still multiply Codex usage. The per-agent timeout is 180s and all workers share
> one 250s whole-hook deadline, including a 5s hard-kill grace and reserved result-processing time,
> below the 270s registration timeout.
> Broader firing also means more prompt and diff
> text is sent through your logged-in Codex CLI. Raise reasoning effort with
> `CODEX_FUSION_EFFORT=xhigh`. To trade quality for speed, set `CODEX_FUSION_SUBAGENTS=off`, lower
> `CODEX_FUSION_EFFORT` to `medium` or `low`, or use `[no-codex]` / `[no-subagents]` for a given
> prompt.
>
> **Subagent verification is the most expensive knob.** A `SubagentStop` fires once per subagent, so a
> turn where Claude fans out to 8 subagents costs up to 8 additional Codex calls, running concurrently
> as those subagents finish. That is why verification defaults to **one** Codex agent per subagent
> rather than a fanout — it only splits into `subagent-claim-verification` + `subagent-defect-hunt`
> when you pass `[subagents]` or set `CODEX_FUSION_SUBAGENTS=always`. Set
> `CODEX_FUSION_SUBAGENT_VERIFY=off` to turn the whole layer off.

### The trigger gate

The `UserPromptSubmit` hook uses a **near-universal** gate: it consults Codex on every non-empty
prompt except explicit `[no-codex]`, pure acknowledgements/greetings like `ok` or `thanks`, nested
Fusion subprocesses, or missing/failed dependencies. Typos, renames, formatting requests, comments,
short questions, and two-word prompts all trigger Codex.

The `SubagentStop` gate is deliberately near-universal too. A subagent that changed the working tree
is **always** verified. A subagent that only reported (an `Explore` or research agent, say) is
verified whenever its report is at least `CODEX_FUSION_SUBAGENT_MIN_CHARS` bytes — a one-word "ok"
on an unchanged tree is not worth a Codex call. `[no-codex]` on the turn's prompt, `stop_hook_active`,
nested Fusion subprocesses, and `CODEX_FUSION_SUBAGENT_VERIFY=off` all skip it.

Sub-agent fanout is automatic by default but bounded. Pre-prompt fanout runs when forced by marker,
when `CODEX_FUSION_SUBAGENTS=always`, or when a simple score sees enough breadth/risk signals such
as long prompts, multi-line plans, implementation/review/migration keywords, auth/security/database/
concurrency terms, or a multi-file dirty tree. Stop fanout runs when forced, always-enabled, or when
the incremental diff is large, spans at least three files, or touches high-risk areas such as auth,
security, database/schema/migrations, concurrency, or tests.

The live `statusMessage` is static while the hook runs. Dynamic sub-agent counts appear after fanout
finishes, for example: `Codex Fusion: spawned 3 sub-agents; 3/3 succeeded.`

## Safety model

- Codex always runs `--ask-for-approval never --sandbox read-only` — it cannot edit files or run
  destructive commands.
- **Sensitive paths never reach Codex.** Review surfaces, diffs, and `git status` output are
  filtered at the source against a denylist of secret-bearing paths (env files, keys and
  certificates, `credentials*`/`secrets*`, shell history, `.netrc`/`.npmrc`/`.pypirc`,
  `auth.json`, SQLite databases, `.ssh`/`.aws`/`.gnupg` contents — extensible via
  `CODEX_FUSION_EXCLUDE`). Excluded files appear only as an `(excluded: ...)` marker; oversized
  and binary untracked files are likewise replaced with markers. The prompt additionally tells
  Codex not to inspect credentials, but the guarantee is the source-level exclusion, not that
  instruction. Known limit: the filter is path-based, so if a turn renames a secret file to a
  non-denylisted name, the content appears in the diff under its new name.
- **Subagent reports are redacted, not path-filtered.** A subagent's final message is free-form model
  text, so the path denylist cannot protect it. Before that text reaches Codex it is run through a
  secret-shaped-value redactor (private-key blocks, AWS key ids, GitHub / Slack / Google /
  OpenAI-style tokens, JWTs, `Authorization:` headers, and `key|secret|token|password = …`
  assignments). Redaction runs **before** truncation, and an unterminated `BEGIN … PRIVATE KEY`
  block is redacted through end-of-text: cutting a key block above its `END` marker would otherwise
  strand the key body somewhere no paired pattern matches. Known limit: this is pattern-based and
  best-effort — a secret in an unusual shape can still get through. The hook reads only
  `last_assistant_message`; it never opens `transcript_path` or `agent_transcript_path`.
- The subagent report is handed to Codex inside explicit untrusted-data delimiters, with an
  instruction to treat it strictly as a claim to verify and never as instructions to follow — it is
  model-generated text and therefore a prompt-injection surface.
- State lives in a per-user, mode-0700 directory whose ownership is verified before every use, so a
  hostile co-tenant on shared `/tmp` cannot pre-create or poison it.
- Hook-launched sub-agents are separate read-only `codex exec` subprocesses with separate output
  files. Fanout roles do not recursively spawn nested agents; they return delegation requests instead.
- Both hooks **never block** Claude on the no-action path — they always exit 0. If Codex is missing,
  not logged in, times out, or errors, the hook silently skips.
- The `Stop` hook only ever forces Claude to continue (`decision: block`) when Codex explicitly
  returns `CODEX_REVIEW_VERDICT: ISSUES_FOUND`, and it is loop-safe via `stop_hook_active` plus a
  prompt baseline, reviewed-diff hash, and bounded failed-review retry counter.
- The `SubagentStop` hook only blocks on an explicit `CODEX_VERIFY_VERDICT: ISSUES_FOUND`, and Codex
  is told that failing to *confirm* a claim is not grounds for `ISSUES_FOUND`. It is loop-safe via
  `stop_hook_active`, a per-agent verified-payload hash, a bounded retry counter, and a hard
  per-subagent block cap.
- Internal `timeout` keeps each Codex call bounded; injected output is truncated.

## Test it

```bash
# Triggers Codex (you'll see "AUTOMATIC CODEX FUSION CONTEXT" injected):
#   Refactor the auth middleware to eliminate the token-refresh race condition.
#   Fix the typo in the README heading.
#   what does this function do?
# Forces fanout:              Refactor the auth middleware [subagents]
# Forces single Codex:        Refactor the auth middleware [no-subagents]
# Skips (pure ack):          thanks!
# Skips (escape hatch):      Refactor the payment retry logic [no-codex]
```

You can also exercise a hook directly without Claude:

```bash
echo '{"prompt":"Refactor the auth module to fix a race condition","cwd":"'"$PWD"'","session_id":"t1"}' \
  | CODEX_FUSION_DEBUG=1 ~/.claude/hooks/codex-fusion-userprompt.sh
```

Subagent verification needs the baseline from that first call, then replays a subagent's report:

```bash
echo '{"cwd":"'"$PWD"'","session_id":"t1","stop_hook_active":false,"agent_id":"a1",
       "agent_type":"general-purpose",
       "last_assistant_message":"Done. I removed the retry loop in client.py and all tests pass."}' \
  | CODEX_FUSION_DEBUG=1 ~/.claude/hooks/codex-fusion-subagent-stop.sh
```

The repo's own test suite runs without Codex installed (it uses a fake `codex` shim):

```bash
python3 -m unittest tests.test_hooks tests.test_doctor
```

## Health check

```bash
./bin/harness-doctor              # full check; add --strict to fail on warnings, --skip-probes for a fast pass
```

A read-only doctor for the whole harness. For Claude Fusion it queries
`codex plugin list --marketplace claude-fusion --json`, discovers the installed version dynamically,
and validates enabled state, the versioned plugin cache against its marketplace payload, all three
`UserPromptSubmit` / `SubagentStop` / `Stop` registrations, scripts and executable bits, the skill,
read-only flags, and plugin-prefixed trust keys. `--skip-probes` (or a failed CLI query) falls back to
config/cache inspection; legacy standalone hooks are checked only when plugin mode is absent. An
empty legacy `hooks.json` alone is inactive, while any orphaned legacy script is reported as a partial
legacy installation. The Codex Fusion side checks all three of its own `UserPromptSubmit` /
`SubagentStop` / `Stop` 270s registrations against the 250s whole-hook budget, plus installed-file
parity, syntax, binaries, flags, and state hygiene.
It never writes anything.
Exit 0 = healthy, 1 = at least one FAIL.

The most valuable time to run it: after updating Claude Code / Codex, after editing either repo,
or whenever the harness feels quiet — most failure modes here are silent by design (the hooks
guarantee they never block), so this is the tool that makes them visible.

## Uninstall

```bash
./uninstall.sh
```

Removes the three hook entries from `settings.json` (leaving a `*.codex-fusion.bak` backup) and
deletes the installed hook scripts and skill. Your other hooks and settings are untouched, including
unrelated `SubagentStop` hooks of your own.

## Layout

```
hooks/codex-fusion-common.sh         # shared Codex runner, fanout gates, and helpers
hooks/codex-fusion-userprompt.sh     # UserPromptSubmit hook (pre-edit analysis)
hooks/codex-fusion-stop.sh           # Stop hook (post-diff review)
hooks/codex-fusion-subagent-stop.sh  # SubagentStop hook (adversarial subagent verification)
skills/codex-fusion-auto/SKILL.md    # how Claude synthesizes Claude + Codex
bin/harness-doctor                   # read-only health check for both Fusion directions
settings.snippet.json                # hooks block to merge (manual install)
install.sh / uninstall.sh            # idempotent installer / remover
tests/test_hooks.py                  # end-to-end hook tests against a fake codex CLI
tests/test_doctor.py                 # harness-doctor tests
```

## License

[MIT](LICENSE)
