#!/usr/bin/env bash
# codex-fusion-userprompt.sh  (Claude Code UserPromptSubmit hook)
# Auto-consult OpenAI Codex CLI (ChatGPT login, READ-ONLY) as an independent peer on
# non-trivial coding prompts and inject its analysis into Claude's context.
# GUARANTEE: never blocks Claude — always exits 0. Escape hatch: include [no-codex].
set +e
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

STATE_DIR="${TMPDIR:-/tmp}/codex-fusion-state"
dbg(){ [ "${CODEX_FUSION_DEBUG:-0}" = "1" ] && { mkdir -p "$STATE_DIR" 2>/dev/null; printf '%s UPS: %s\n' "$$" "$*" >>"$STATE_DIR/debug.log"; }; }

PY="/usr/bin/python3"; [ -x "$PY" ] || PY="$(command -v python3 2>/dev/null)"
[ -x "$PY" ] || { dbg "no python3"; exit 0; }
# Resolve the codex binary, then put its real bin dir on PATH so codex's bundled
# runtime (e.g. node) is reachable even in a minimal hook shell.
CODEX_BIN="$(command -v codex 2>/dev/null)"
if [ ! -x "$CODEX_BIN" ]; then
  for c in "$HOME/.local/bin/codex" "$HOME/bin/codex" "/usr/local/bin/codex" "$HOME/.npm-global/bin/codex"; do
    [ -x "$c" ] && { CODEX_BIN="$c"; break; }
  done
fi
[ -x "$CODEX_BIN" ] || { dbg "no codex"; exit 0; }
_cx_dir="$(dirname "$("$PY" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CODEX_BIN" 2>/dev/null || echo "$CODEX_BIN")")"
case ":$PATH:" in *":$_cx_dir:"*) ;; *) export PATH="$_cx_dir:$PATH";; esac

CODEX_TIMEOUT=240   # xhigh effort needs more headroom than high; killed calls skip silently
MAX_CHARS=12000
# Codex Fusion runs on the strongest Codex model at extra-high (xhigh) reasoning effort (override via env).
CODEX_MODEL="${CODEX_FUSION_MODEL:-gpt-5.5}"      # highest Codex model; bump when a newer top model ships
CODEX_REASONING="${CODEX_FUSION_EFFORT:-xhigh}"  # extra-high effort; xhigh is slower (see CODEX_TIMEOUT)

INPUT="$(cat)"; [ -n "$INPUT" ] || exit 0
# Parse prompt/cwd/session_id in one python call; base64 so newlines survive the shell.
FIELDS="$(printf '%s' "$INPUT" | "$PY" -c '
import sys,json,base64
try: d=json.load(sys.stdin)
except Exception: d={}
for k in ("prompt","cwd","session_id"):
    sys.stdout.write(base64.b64encode(str(d.get(k,"") or "").encode()).decode()+"\n")
' 2>/dev/null)"
PROMPT="$(printf '%s' "$FIELDS" | sed -n 1p | base64 -d 2>/dev/null)"
CWD="$(printf '%s' "$FIELDS" | sed -n 2p | base64 -d 2>/dev/null)"
SESSION_ID="$(printf '%s' "$FIELDS" | sed -n 3p | base64 -d 2>/dev/null)"
[ -n "$PROMPT" ] || exit 0
[ -d "$CWD" ] || CWD="$PWD"

# --- escape hatch ---
case "$PROMPT" in *"[no-codex]"*) dbg "skip: [no-codex]"; exit 0;; esac

# --- AGGRESSIVE gate: trigger unless clearly trivial / conversational / tiny ---
WORDS="$(printf '%s' "$PROMPT" | wc -w | tr -d ' ')"; WORDS="${WORDS:-0}"
[ "$WORDS" -lt 3 ] 2>/dev/null && { dbg "skip: too short ($WORDS)"; exit 0; }

TRIVIAL_RE='(^|[^a-z])(thanks|thank you|thx|ok|okay|cool|nice|great|got it|hi|hello|hey|yo|sup|yes|no|sure|nvm|never ?mind|lgtm)([^a-z]|$)|fix(ing)? (a |the )?typo|\btypo\b|^[[:space:]]*rename\b|\brewor[dk]|\bwording\b|^[[:space:]]*format\b|formatting|reindent|indentation|whitespace|\blint(ing)?\b|prettier|one[- ]?liner|spelling|capitali[sz]|(add|fix|update|edit) (a |the )?comment'
printf '%s' "$PROMPT" | grep -iqE "$TRIVIAL_RE" && { dbg "skip: trivial"; exit 0; }

ACTION_RE='\b(implement|build|create|write|add|fix|debug|refactor|migrat|optimi[sz]|design|review|change|update|integrat|deploy|test|rewrite|redesign|secure|harden|delete|remove|configure|set ?up|wire|generate|scaffold|investigate|diagnose|profile)\b'
QUESTION_RE='^[[:space:]]*(what|why|how|when|who|where|which|is|are|was|were|does|do|did|can|could|should|would|will|explain|describe|summari[sz]e|tell me|define|meaning of)\b'
if [ "$WORDS" -lt 16 ] && printf '%s' "$PROMPT" | grep -iqE "$QUESTION_RE" && ! printf '%s' "$PROMPT" | grep -iqE "$ACTION_RE"; then
  dbg "skip: short question"; exit 0
fi
# -> everything else TRIGGERS Codex

GITSTATUS="$(git -C "$CWD" status --short 2>/dev/null | head -c 4000)"
[ -n "$GITSTATUS" ] || GITSTATUS="(clean or not a git repository)"

CODEX_PROMPT="$(cat <<EOF
You are Codex acting as an independent coding peer for Claude Code.

You are running automatically from a Claude Code hook.
You are in read-only mode.
Do not edit files.
Do not run destructive commands.
Do not inspect credentials, tokens, .env files, keychains, shell history, or auth files.
Focus only on the user's coding task and the repository context.

User task:
$PROMPT

Repository:
$CWD

Quick repo state:
$GITSTATUS

Return a concise analysis with:
1. Problem understanding
2. Recommended approach
3. Files or modules likely involved
4. Edge cases
5. Tests/checks Claude should run
6. Security or data-loss risks
7. Assumptions
8. Concise implementation strategy
9. Anything Claude should be skeptical about

Keep the response under 1200 words.
Do not produce a full patch unless asked.
Prefer conservative, minimal changes.
EOF
)"

LASTMSG="$(mktemp 2>/dev/null)" || exit 0
trap 'rm -f "$LASTMSG"' EXIT

# Run Codex read-only on the strongest model at xhigh (extra-high) effort. If the pinned
# model is unavailable, fall back once to Codex's own default model so analysis still happens.
MODEL_ARGS=(); [ -n "$CODEX_MODEL" ] && MODEL_ARGS=(-m "$CODEX_MODEL")
run_codex() {
  timeout "$CODEX_TIMEOUT" "$CODEX_BIN" "${MODEL_ARGS[@]}" -c model_reasoning_effort="$CODEX_REASONING" \
    --ask-for-approval never exec \
    -C "$CWD" --sandbox read-only --color never --skip-git-repo-check \
    -o "$LASTMSG" "$CODEX_PROMPT" </dev/null >/dev/null 2>&1
}
dbg "running codex (model=${CODEX_MODEL:-default}, effort=$CODEX_REASONING, cwd=$CWD, words=$WORDS)"
run_codex; RC=$?
if [ "$RC" -ne 0 ] && [ "${#MODEL_ARGS[@]}" -gt 0 ]; then
  dbg "model $CODEX_MODEL failed (rc=$RC); retrying with codex default model"
  MODEL_ARGS=(); : >"$LASTMSG"; run_codex; RC=$?
fi
[ "$RC" -eq 0 ] || { dbg "codex rc=$RC -> skip"; exit 0; }
ANALYSIS="$(cat "$LASTMSG" 2>/dev/null)"
[ -n "$ANALYSIS" ] || { dbg "empty analysis -> skip"; exit 0; }

# Mark this session's task complex so the Stop hook will review the diff.
[ -n "$SESSION_ID" ] && { mkdir -p "$STATE_DIR" 2>/dev/null; : >"$STATE_DIR/$SESSION_ID.complex" 2>/dev/null; }

PREAMBLE="AUTOMATIC CODEX FUSION CONTEXT:
Codex was automatically consulted because this prompt matched the complex-coding gate.
Claude: before editing, compare your own plan with Codex's analysis. Explicitly note consensus, disagreements, Codex-only insights, and your final decision. You are not required to follow Codex if you disagree."

CODEX_ANALYSIS="$ANALYSIS" PREAMBLE="$PREAMBLE" MAX_CHARS="$MAX_CHARS" "$PY" <<'PY'
import os, json
a = os.environ.get("CODEX_ANALYSIS", "")
p = os.environ.get("PREAMBLE", "")
try: m = int(os.environ.get("MAX_CHARS", "12000"))
except Exception: m = 12000
if len(a) > m: a = a[:m] + "\n\n[...Codex output truncated...]"
ctx = p + "\n\n--- BEGIN CODEX ANALYSIS ---\n" + a + "\n--- END CODEX ANALYSIS ---"
print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}}))
PY
dbg "injected $(printf '%s' "$ANALYSIS" | wc -c) chars"
exit 0
