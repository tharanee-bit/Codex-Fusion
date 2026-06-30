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

Claude stays the editor and the final judge. Codex only advises and reviews — it is always
**read-only** and never edits your files.

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
        ▼
   Stop hook ── changes since prompt baseline? ──no──▶ (Claude finishes)
        │ yes
        ▼
   1 or more codex exec --sandbox read-only reviewers over the diff
        │
        ├─ verdict PASS         ──▶ Claude finishes
        └─ verdict ISSUES_FOUND ──▶ Claude must address them first (blocks once)
```

The UserPromptSubmit hook records a prompt-start review-surface baseline in
`${TMPDIR:-/tmp}/codex-fusion-state/`. The Stop hook compares that baseline to the current
tracked/untracked review surface, so pre-existing dirty work does not trigger an unrelated review,
while an unchanged already-reviewed surface does not get reviewed again on every later Stop.
If a Stop review path fails transiently, the unchanged diff is retried a bounded number of times and
then skipped until the diff changes.

Claude Code also shows a live hook status message while Codex Fusion runs. After a successful
pre-prompt consult, Codex Fusion emits a human-facing `systemMessage`; fanout notices include the
actual spawned sub-agent count and how many returned usable output. Fanout Stop reviews emit the
same count when they block on `ISSUES_FOUND`, and emit a PASS notice when all spawned review agents
pass.

## Requirements

- **Claude Code** (CLI, desktop, web, or IDE extension) with user-level hooks support.
- **Codex CLI** installed and signed in: `codex login status` should report logged in.
- **python3**, **git**, **bash**, and GNU coreutils (`timeout`, `mktemp`, `base64`).
  `jq` is *not* required — JSON is handled with python3.

Tested on Linux / WSL2.

## Install

```bash
git clone https://github.com/tharanee-bit/Codex-Fusion.git
cd Codex-Fusion
./install.sh
```

`install.sh` copies the hook scripts and skill into `~/.claude/` and **merges** the two hooks into
`~/.claude/settings.json` non-destructively (it backs the file up to
`settings.json.codex-fusion.bak` first, and is idempotent — re-running won't duplicate entries).
It respects `CLAUDE_CONFIG_DIR` if you set it.

New hooks load at session start, so **restart Claude Code / reload the window**, then run `/hooks`
to confirm `UserPromptSubmit` and `Stop` list the Codex Fusion scripts.

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
| `CODEX_FUSION_MODEL` | `gpt-5.5` | Codex model to use. Defaults to the strongest available model. |
| `CODEX_FUSION_EFFORT` | `xhigh` | Codex reasoning effort (`low` / `medium` / `high` / `xhigh`). Defaults to extra-high. |
| `CODEX_FUSION_SUBAGENTS` | `auto` | Sub-agent policy: `auto`, `off`, or `always`. Prompt markers still select per-turn behavior. |
| `CODEX_FUSION_MAX_AGENTS` | `4` | Hard cap for hook-launched Codex agents and any bounded internal delegation contract. |
| `CODEX_FUSION_TIMEOUT` | `180` | Per-agent timeout in seconds. The Claude hook registration timeout remains 270s. |
| `CODEX_FUSION_STOP_RETRY_LIMIT` | `2` | Number of transient failed Stop review attempts for an unchanged diff before skipping. |
| `CODEX_FUSION_NOTIFY` | `1` | Set to `0` to suppress human-facing success `systemMessage` notices. Context injection and blocking review reasons still work. |
| `CODEX_FUSION_DEBUG=1` | off | Logs gate decisions to `${TMPDIR:-/tmp}/codex-fusion-state/debug.log`. |

> **Strongest model, extra-high effort.** Codex Fusion runs on the best Codex model at `xhigh`
> (extra-high) reasoning effort by default, so the second opinion is as strong as possible. The model
> is pinned in one constant at the top of each hook (`CODEX_MODEL`) and overridable via
> `CODEX_FUSION_MODEL` — bump it when a newer top model ships. If the pinned model isn't available to
> your account, the hook automatically retries once with Codex's own default model so you still get an
> analysis.
>
> This costs latency: most prompts now wait for Codex before Claude responds. Fanout runs agents in
> parallel, but it can still multiply Codex usage. The per-agent timeout is 180s (hook registration
> timeout 270s), leaving room for parallel aggregation. Broader firing also means more prompt and diff
> text is sent through your logged-in Codex CLI. To trade quality for speed, set
> `CODEX_FUSION_SUBAGENTS=off`, set `CODEX_FUSION_EFFORT=high` (or `medium` / `low`), or use
> `[no-codex]` / `[no-subagents]` for a given prompt.

### The trigger gate

The `UserPromptSubmit` hook uses a **near-universal** gate: it consults Codex on every non-empty
prompt except explicit `[no-codex]`, pure acknowledgements/greetings like `ok` or `thanks`, nested
Fusion subprocesses, or missing/failed dependencies. Typos, renames, formatting requests, comments,
short questions, and two-word prompts all trigger Codex.

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
  destructive commands. The prompt also explicitly tells Codex not to inspect credentials, `.env`,
  tokens, keychains, shell history, or auth files.
- Hook-launched sub-agents are separate read-only `codex exec` subprocesses with separate output
  files. Fanout roles do not recursively spawn nested agents; they return delegation requests instead.
- Both hooks **never block** Claude on the no-action path — they always exit 0. If Codex is missing,
  not logged in, times out, or errors, the hook silently skips.
- The `Stop` hook only ever forces Claude to continue (`decision: block`) when Codex explicitly
  returns `CODEX_REVIEW_VERDICT: ISSUES_FOUND`, and it is loop-safe via `stop_hook_active` plus a
  prompt baseline, reviewed-diff hash, and bounded failed-review retry counter.
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

## Uninstall

```bash
./uninstall.sh
```

Removes the two hook entries from `settings.json` (leaving a `*.codex-fusion.bak` backup) and
deletes the installed hook scripts and skill. Your other hooks and settings are untouched.

## Layout

```
hooks/codex-fusion-common.sh       # shared Codex runner, fanout gates, and helpers
hooks/codex-fusion-userprompt.sh   # UserPromptSubmit hook (pre-edit analysis)
hooks/codex-fusion-stop.sh         # Stop hook (post-diff review)
skills/codex-fusion-auto/SKILL.md  # how Claude synthesizes Claude + Codex
settings.snippet.json              # hooks block to merge (manual install)
install.sh / uninstall.sh          # idempotent installer / remover
```

## License

[MIT](LICENSE)
