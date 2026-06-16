# Codex Fusion

**Automatic peer review for Claude Code, powered by your local Codex CLI.**

Codex Fusion makes [Claude Code](https://claude.com/claude-code) automatically consult
[OpenAI Codex CLI](https://github.com/openai/codex) as an independent second opinion on
non-trivial coding tasks — *without* a slash command, and *without* you typing anything.
It uses Claude Code **hooks**:

- **Before** Claude plans or edits, a `UserPromptSubmit` hook runs Codex **read-only** over your
  repo and injects Codex's independent analysis into Claude's context. Claude then reconciles its
  own plan with Codex's (consensus / disagreements / Codex-only insights) before touching code.
- **After** Claude finishes a complex, file-changing task, a `Stop` hook runs Codex **read-only**
  over the resulting `git diff`. If Codex flags serious problems (correctness, security, data-loss,
  concurrency, broken tests), Claude is asked to address them before finalizing.

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
   UserPromptSubmit hook ── gate? ──no──▶ (silent, Claude proceeds normally)
        │ yes (non-trivial)
        ▼
   codex exec --sandbox read-only   ──▶  analysis injected as additionalContext
        │
        ▼
   Claude synthesizes Claude + Codex, then edits
        │
        ▼
   Stop hook ── task was complex AND git diff non-empty? ──no──▶ (Claude finishes)
        │ yes
        ▼
   codex exec --sandbox read-only over the diff
        │
        ├─ verdict PASS         ──▶ Claude finishes
        └─ verdict ISSUES_FOUND ──▶ Claude must address them first (blocks once)
```

The two hooks coordinate through a small per-session marker file in
`${TMPDIR:-/tmp}/codex-fusion-state/`, so the Stop review only fires for tasks the
UserPromptSubmit gate already judged complex.

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
| `CODEX_FUSION_EFFORT` | `medium` | Codex reasoning effort (`low` / `medium` / `high`). Lower = faster/cheaper. |
| `CODEX_FUSION_DEBUG=1` | off | Logs gate decisions to `${TMPDIR:-/tmp}/codex-fusion-state/debug.log`. |

> **Why `medium`?** Codex's own config default may be `xhigh`, which can take well over a hook's
> timeout for a single analysis. Codex Fusion forces a faster effort per invocation so prompts
> aren't held up. Raise it with `CODEX_FUSION_EFFORT=high` if you want deeper analysis.

### The trigger gate

The `UserPromptSubmit` hook uses an **aggressive** gate: it consults Codex on most substantive
prompts and only skips when a prompt is clearly trivial or conversational — `[no-codex]`, ≤2 words,
a typo/rename/format/lint/comment edit, a greeting, or a short pure question with no coding verb.
To make it *conservative* instead (only fire on clear complex-coding keywords), edit the gate block
in `hooks/codex-fusion-userprompt.sh`.

## Safety model

- Codex always runs `--ask-for-approval never --sandbox read-only` — it cannot edit files or run
  destructive commands. The prompt also explicitly tells Codex not to inspect credentials, `.env`,
  tokens, keychains, shell history, or auth files.
- Both hooks **never block** Claude on the no-action path — they always exit 0. If Codex is missing,
  not logged in, times out, or errors, the hook silently skips.
- The `Stop` hook only ever forces Claude to continue (`decision: block`) when Codex explicitly
  returns `CODEX_REVIEW_VERDICT: ISSUES_FOUND`, and it is loop-safe via `stop_hook_active` — it
  reviews at most once per task.
- Internal `timeout` (100s) keeps each Codex call bounded; injected output is truncated.

## Test it

```bash
# Triggers Codex (you'll see "AUTOMATIC CODEX FUSION CONTEXT" injected):
#   Refactor the auth middleware to eliminate the token-refresh race condition.
# Skips (trivial):           Fix the typo in the README heading.
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
hooks/codex-fusion-userprompt.sh   # UserPromptSubmit hook (pre-edit analysis)
hooks/codex-fusion-stop.sh         # Stop hook (post-diff review)
skills/codex-fusion-auto/SKILL.md  # how Claude synthesizes Claude + Codex
settings.snippet.json              # hooks block to merge (manual install)
install.sh / uninstall.sh          # idempotent installer / remover
```

## License

[MIT](LICENSE)
