#!/usr/bin/env bash
# codex-fusion-stop.sh  (Claude Code Stop hook)
# When the finished task was gated-complex (marker from the UserPromptSubmit hook) AND there are
# working-tree changes, run Codex READ-ONLY over `git diff HEAD`. If Codex returns ISSUES_FOUND,
# block ONCE (decision:block) with the review so Claude addresses it. Loop-safe; never errors out.
set +e
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

STATE_DIR="${TMPDIR:-/tmp}/codex-fusion-state"
dbg(){ [ "${CODEX_FUSION_DEBUG:-0}" = "1" ] && { mkdir -p -m 700 "$STATE_DIR" 2>/dev/null; printf '%s STOP: %s\n' "$$" "$*" >>"$STATE_DIR/debug.log"; }; }

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

CODEX_TIMEOUT=240; MAX_DIFF=20000; MAX_CHARS=12000   # xhigh needs more headroom than high
# Codex Fusion runs on the strongest Codex model at extra-high (xhigh) reasoning effort (override via env).
CODEX_MODEL="${CODEX_FUSION_MODEL:-gpt-5.5}"      # highest Codex model; bump when a newer top model ships
CODEX_REASONING="${CODEX_FUSION_EFFORT:-xhigh}"  # extra-high effort; xhigh is slower (see CODEX_TIMEOUT)

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
# Sanitize session_id before it becomes a filename under STATE_DIR (no path traversal / odd chars).
case "$SESSION_ID" in *[!A-Za-z0-9._-]*|.|..) SESSION_ID="";; esac

# loop guard: we're already inside a continuation we forced -> don't review again
[ "$STOP_ACTIVE" = "true" ] && { dbg "stop_hook_active -> exit"; exit 0; }
[ -d "$CWD" ] || CWD="$PWD"

MARKER="$STATE_DIR/$SESSION_ID.complex"
[ -n "$SESSION_ID" ] && [ -f "$MARKER" ] || { dbg "no complex marker -> exit"; exit 0; }

DIFF="$(git -C "$CWD" diff HEAD 2>/dev/null | head -c "$MAX_DIFF")"
[ -n "$DIFF" ] || { dbg "empty diff -> exit"; rm -f "$MARKER" 2>/dev/null; exit 0; }
CHANGED="$(git -C "$CWD" status --short 2>/dev/null | head -c 3000)"
# NOTE: the marker is deleted only on a DEFINITIVE outcome (PASS, or a delivered ISSUES_FOUND block),
# not here. A transient failure (codex error/timeout) leaves it so the next genuine Stop retries the
# review instead of silently losing it.

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

# Run Codex read-only on the strongest model at xhigh (extra-high) effort. If the pinned
# model is unavailable, fall back once to Codex's own default model so the review still happens.
MODEL_ARGS=(); [ -n "$CODEX_MODEL" ] && MODEL_ARGS=(-m "$CODEX_MODEL")
run_codex() {
  timeout "$CODEX_TIMEOUT" "$CODEX_BIN" "${MODEL_ARGS[@]}" -c model_reasoning_effort="$CODEX_REASONING" \
    --ask-for-approval never exec \
    -C "$CWD" --sandbox read-only --color never --skip-git-repo-check \
    -o "$LASTMSG" "$CODEX_PROMPT" </dev/null >/dev/null 2>&1
}
dbg "running codex review (model=${CODEX_MODEL:-default}, effort=$CODEX_REASONING)"
run_codex; RC=$?
# Fall back to the default model only on a FAST failure (e.g. model id unavailable). After an
# internal timeout (rc 124) there's no budget left for a second run, so don't bother.
if [ "$RC" -ne 0 ] && [ "$RC" -ne 124 ] && [ "${#MODEL_ARGS[@]}" -gt 0 ]; then
  dbg "model $CODEX_MODEL failed (rc=$RC); retrying with codex default model"
  MODEL_ARGS=(); : >"$LASTMSG"; run_codex; RC=$?
fi
# On a transient failure, KEEP the marker so the next genuine Stop retries the review.
[ "$RC" -eq 0 ] || { dbg "codex rc!=0 -> exit (marker kept for retry)"; exit 0; }
REVIEW="$(cat "$LASTMSG" 2>/dev/null)"; [ -n "$REVIEW" ] || { dbg "empty review -> exit (marker kept)"; exit 0; }

# Codex responded: this is a definitive review, so consume the marker (review once).
rm -f "$MARKER" 2>/dev/null

# Only block when Codex explicitly flags issues. Per the prompt contract the verdict is the FIRST
# line, so check only the first non-empty line — this also stops injected diff/prompt content from
# forging the control token deeper in the response.
VERDICT_LINE="$(printf '%s' "$REVIEW" | grep -m1 -vE '^[[:space:]]*$')"
if ! printf '%s' "$VERDICT_LINE" | grep -qiE 'CODEX_REVIEW_VERDICT:[[:space:]]*ISSUES_FOUND'; then
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
