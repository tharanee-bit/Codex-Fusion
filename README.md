# Codex Fusion

**Automatic peer review for Claude Code, powered by your local Codex CLI.**

Codex Fusion makes [Claude Code](https://claude.com/claude-code) automatically consult
[OpenAI Codex CLI](https://github.com/openai/codex) as an independent second opinion on nearly
every non-empty prompt — *without* a slash command, and *without* you typing anything.
It uses Claude Code **hooks**:

- **Before** Claude plans or edits, a `UserPromptSubmit` hook runs Codex **read-only** over your
  repo and injects Codex's independent analysis into Claude's context. Claude then reconciles its
  own plan with Codex's (consensus / disagreements / Codex-only insights) before touching code.
- **After** Claude finishes with changes since the prompt-start baseline, a `Stop` hook runs Codex
  **read-only** over the incremental tracked/untracked review surface. If Codex flags serious
  problems (correctness, security, data-loss, concurrency, broken tests), Claude is asked to address
  them before finalizing.

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
   codex exec --sandbox read-only   ──▶  analysis injected as additionalContext
        │
        ▼
   Claude synthesizes Claude + Codex, then edits
        │
        ▼
   Stop hook ── changes since prompt baseline? ──no──▶ (Claude finishes)
        │ yes
        ▼
   codex exec --sandbox read-only over the diff
        │
        ├─ verdict PASS         ──▶ Claude finishes
        └─ verdict ISSUES_FOUND ──▶ Claude must address them first (blocks once)
```

The UserPromptSubmit hook records a prompt-start review-surface baseline in
`${TMPDIR:-/tmp}/codex-fusion-state/`. The Stop hook compares that baseline to the current
tracked/untracked review surface, so pre-existing dirty work does not trigger an unrelated review,
while an unchanged already-reviewed surface does not get reviewed again on every later Stop.

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
| `CODEX_FUSION_MODEL` | `gpt-5.5` | Codex model to use. Defaults to the strongest available model. |
| `CODEX_FUSION_EFFORT` | `xhigh` | Codex reasoning effort (`low` / `medium` / `high` / `xhigh`). Defaults to extra-high. |
| `CODEX_FUSION_DEBUG=1` | off | Logs gate decisions to `${TMPDIR:-/tmp}/codex-fusion-state/debug.log`. |

> **Strongest model, extra-high effort.** Codex Fusion runs on the best Codex model at `xhigh`
> (extra-high) reasoning effort by default, so the second opinion is as strong as possible. The model
> is pinned in one constant at the top of each hook (`CODEX_MODEL`) and overridable via
> `CODEX_FUSION_MODEL` — bump it when a newer top model ships. If the pinned model isn't available to
> your account, the hook automatically retries once with Codex's own default model so you still get an
> analysis.
>
> This costs latency: most prompts now wait for Codex (typically ~70–180s, longer on big repos or
> diffs) before Claude responds, so the internal timeout is 240s (hook registration timeout 270s) to
> give `xhigh` room to finish rather than being killed. Broader firing also means more prompt and
> diff text is sent through your logged-in Codex CLI. To trade quality for speed, set
> `CODEX_FUSION_EFFORT=high` (or `medium` / `low`), or use `[no-codex]` to skip a given prompt.

### The trigger gate

The `UserPromptSubmit` hook uses a **near-universal** gate: it consults Codex on every non-empty
prompt except explicit `[no-codex]`, pure acknowledgements/greetings like `ok` or `thanks`, nested
Fusion subprocesses, or missing/failed dependencies. Typos, renames, formatting requests, comments,
short questions, and two-word prompts all trigger Codex.

## Safety model

- Codex always runs `--ask-for-approval never --sandbox read-only` — it cannot edit files or run
  destructive commands. The prompt also explicitly tells Codex not to inspect credentials, `.env`,
  tokens, keychains, shell history, or auth files.
- Both hooks **never block** Claude on the no-action path — they always exit 0. If Codex is missing,
  not logged in, times out, or errors, the hook silently skips.
- The `Stop` hook only ever forces Claude to continue (`decision: block`) when Codex explicitly
  returns `CODEX_REVIEW_VERDICT: ISSUES_FOUND`, and it is loop-safe via `stop_hook_active` plus a
  prompt baseline and reviewed-diff hash.
- Internal `timeout` (240s) keeps each Codex call bounded; injected output is truncated.

## Test it

```bash
# Triggers Codex (you'll see "AUTOMATIC CODEX FUSION CONTEXT" injected):
#   Refactor the auth middleware to eliminate the token-refresh race condition.
#   Fix the typo in the README heading.
#   what does this function do?
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
hooks/codex-fusion-userprompt.sh   # UserPromptSubmit hook (pre-edit analysis)
hooks/codex-fusion-stop.sh         # Stop hook (post-diff review)
skills/codex-fusion-auto/SKILL.md  # how Claude synthesizes Claude + Codex
settings.snippet.json              # hooks block to merge (manual install)
install.sh / uninstall.sh          # idempotent installer / remover
```

## License

[MIT](LICENSE)
