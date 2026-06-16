#!/usr/bin/env bash
# codex-fusion-stop.sh  (Claude Code Stop hook)
# When the finished task was gated-complex (marker from the UserPromptSubmit hook) AND there are
# working-tree changes, run Codex READ-ONLY over `git diff HEAD`. If Codex returns ISSUES_FOUND,
# block ONCE (decision:block) with the review so Claude addresses it. Loop-safe; never errors out.
set +e
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

STATE_DIR="${TMPDIR:-/tmp}/codex-fusion-state"
dbg(){ [ "${CODEX_FUSION_DEBUG:-0}" = "1" ] && { mkdir -p "$STATE_DIR" 2>/dev/null; printf '%s STOP: %s\n' "$$" "$*" >>"$STATE_DIR/debug.log"; }; }

PY="/usr/bin/python3"; [ -x "$PY" ] || PY="$(command -v python3 2>/dev/null)"; [ -x "$PY" ] || exit 0
CODEX_BIN="$(command -v codex 2>/dev/null)"
if [ ! -x "$CODEX_BIN" ]; then
  for c in "$HOME/.local/bin/codex" "$HOME/bin/codex" "/usr/local/bin/codex" "$HOME/.npm-global/bin/codex"; do
    [ -x "$c" ] && { CODEX_BIN="$c"; break; }
  done
fi
[ -x "$CODEX_BIN" ] || exit 0
_cx_dir="$(dirname "$("$PY" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CODEX_BIN" 2>/dev/null || echo "$CODEX_BIN")")"
case ":$PATH:" in *":$_cx_dir:"*) ;; *) export PATH="$_cx_dir:$PATH";; esac

CODEX_TIMEOUT=100; MAX_DIFF=20000; MAX_CHARS=12000
# Codex's own config default may be xhigh (too slow for a hook); force a fast effort.
CODEX_REASONING="${CODEX_FUSION_EFFORT:-medium}"

INPUT="$(cat)"; [ -n "$INPUT" ] || exit 0
FIELDS="$(printf '%s' "$INPUT" | "$PY" -c '
import sys,json,base64
try: d=json.load(sys.stdin)
except Exception: d={}
for k in ("cwd","session_id","stop_hook_active"):
    v=d.get(k,"")
    if isinstance(v,bool): v="true" if v else "false"
    sys.stdout.write(base64.b64encode(str(v or "").encode()).decode()+"\n")
' 2>/dev/null)"
CWD="$(printf '%s' "$FIELDS" | sed -n 1p | base64 -d 2>/dev/null)"
SESSION_ID="$(printf '%s' "$FIELDS" | sed -n 2p | base64 -d 2>/dev/null)"
STOP_ACTIVE="$(printf '%s' "$FIELDS" | sed -n 3p | base64 -d 2>/dev/null)"

# loop guard: we're already inside a continuation we forced -> don't review again
[ "$STOP_ACTIVE" = "true" ] && { dbg "stop_hook_active -> exit"; exit 0; }
[ -d "$CWD" ] || CWD="$PWD"

MARKER="$STATE_DIR/$SESSION_ID.complex"
[ -n "$SESSION_ID" ] && [ -f "$MARKER" ] || { dbg "no complex marker -> exit"; exit 0; }

DIFF="$(git -C "$CWD" diff HEAD 2>/dev/null | head -c "$MAX_DIFF")"
[ -n "$DIFF" ] || { dbg "empty diff -> exit"; rm -f "$MARKER" 2>/dev/null; exit 0; }
CHANGED="$(git -C "$CWD" status --short 2>/dev/null | head -c 3000)"
rm -f "$MARKER" 2>/dev/null   # review at most once per complex task

CODEX_PROMPT="$(cat <<EOF
You are Codex acting as an independent code reviewer for Claude Code.
You are running automatically from a Stop hook, in read-only mode.
Do not edit files. Do not run destructive commands.
Do not inspect credentials, tokens, .env files, keychains, shell history, or auth files.

Review the git diff below for SERIOUS problems only: correctness bugs, security
vulnerabilities, data-loss risks, concurrency/race issues, and broken or missing tests.
Ignore pure style/formatting nits.

The VERY FIRST line of your response MUST be exactly one of:
CODEX_REVIEW_VERDICT: PASS
CODEX_REVIEW_VERDICT: ISSUES_FOUND

If ISSUES_FOUND, list each serious issue (most important first) as:
- <file:line> — <problem> — <minimal fix>
Keep it under 800 words.

Repository:
$CWD

Changed files:
$CHANGED

Diff:
$DIFF
EOF
)"

LASTMSG="$(mktemp 2>/dev/null)" || exit 0
trap 'rm -f "$LASTMSG"' EXIT
timeout "$CODEX_TIMEOUT" "$CODEX_BIN" -c model_reasoning_effort="$CODEX_REASONING" --ask-for-approval never exec \
  -C "$CWD" --sandbox read-only --color never --skip-git-repo-check \
  -o "$LASTMSG" "$CODEX_PROMPT" </dev/null >/dev/null 2>&1
[ "$?" -eq 0 ] || { dbg "codex rc!=0 -> exit"; exit 0; }
REVIEW="$(cat "$LASTMSG" 2>/dev/null)"; [ -n "$REVIEW" ] || exit 0

# Only block when Codex explicitly flags issues; otherwise let Claude finish.
if ! printf '%s' "$REVIEW" | grep -qiE 'CODEX_REVIEW_VERDICT:[[:space:]]*ISSUES_FOUND'; then
  dbg "verdict PASS/none -> exit"; exit 0
fi

REVIEW="$REVIEW" MAX_CHARS="$MAX_CHARS" "$PY" <<'PY'
import os, json
r = os.environ.get("REVIEW", "")
try: m = int(os.environ.get("MAX_CHARS", "12000"))
except Exception: m = 12000
if len(r) > m: r = r[:m] + "\n\n[...truncated...]"
reason = ("AUTOMATIC CODEX FUSION — POST-DIFF REVIEW:\n"
          "Codex independently reviewed your git diff and flagged potential issues. Address the "
          "serious problems (correctness, security, data-loss, concurrency, broken tests) before "
          "finalizing, or explicitly justify why each is not a real issue. You remain the final judge.\n\n" + r)
print(json.dumps({"decision": "block", "reason": reason}))
PY
dbg "blocked with ISSUES_FOUND"
exit 0
