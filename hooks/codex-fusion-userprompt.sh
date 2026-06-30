#!/usr/bin/env bash
# codex-fusion-userprompt.sh  (Claude Code UserPromptSubmit hook)
# Auto-consult OpenAI Codex CLI (ChatGPT login, READ-ONLY) as an independent peer on nearly every
# non-empty prompt and inject its analysis into Claude's context.
# GUARANTEE: never blocks Claude. Escape hatch: include [no-codex].
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hooks/codex-fusion-common.sh
. "$SCRIPT_DIR/codex-fusion-common.sh" 2>/dev/null || exit 0
cf_init_common "UPS"

if cf_nested_fusion_active; then
  cf_dbg "skip: nested fusion active"
  exit 0
fi

cf_setup_codex_runtime || exit 0

MAX_CHARS=12000
LASTMSG=""
AGENT_DIR=""
trap 'rm -f "$LASTMSG" 2>/dev/null; rm -rf "$AGENT_DIR" 2>/dev/null' EXIT

INPUT="$(cat)"
[ -n "$INPUT" ] || exit 0
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
case "$SESSION_ID" in *[!A-Za-z0-9._-]*|.|..) SESSION_ID="";; esac
[ -n "$PROMPT" ] || exit 0
[ -d "$CWD" ] || CWD="$PWD"

state_key() {
  if [ -n "$SESSION_ID" ]; then
    printf 'session-%s' "$SESSION_ID"
  else
    CWD_HASH="$(printf '%s' "$CWD" | git hash-object --stdin 2>/dev/null)"
    printf 'cwd-%s' "${CWD_HASH:-unknown}"
  fi
}

review_surface() {
  git -C "$CWD" rev-parse --verify HEAD >/dev/null 2>&1 || return 1
  printf '### tracked diff against HEAD\n'
  git -C "$CWD" diff HEAD -- 2>/dev/null || return 1
  printf '\n### untracked files\n'
  git -C "$CWD" ls-files --others --exclude-standard -z 2>/dev/null |
    while IFS= read -r -d '' f; do
      printf '\n--- untracked file: %s ---\n' "$f"
      git -C "$CWD" diff --no-index -- /dev/null "$CWD/$f" 2>/dev/null || true
    done
}

STATE_KEY="$(state_key)"
BASELINE_FILE="$STATE_DIR/$STATE_KEY.baseline"
NO_REVIEW_FILE="$STATE_DIR/$STATE_KEY.no-review"
SUBAGENTS_FILE="$STATE_DIR/$STATE_KEY.subagents"
if mkdir -p -m 700 "$STATE_DIR" 2>/dev/null; then
  rm -f "$NO_REVIEW_FILE" 2>/dev/null
  if review_surface >"$BASELINE_FILE" 2>/dev/null; then
    cf_dbg "baselined review surface ($STATE_KEY)"
  else
    rm -f "$BASELINE_FILE" 2>/dev/null
    cf_dbg "baseline unavailable ($STATE_KEY)"
  fi
fi

case "$PROMPT" in *"[no-codex]"*) : >"$NO_REVIEW_FILE" 2>/dev/null; rm -f "$SUBAGENTS_FILE" 2>/dev/null; cf_dbg "skip: [no-codex]"; exit 0;; esac

ACK_RE='^[[:space:]]*((thanks|thank you|thx|ok|okay|cool|nice|great|got it|hi|hello|hey|yo|sup|yes|no|sure|nvm|never ?mind|lgtm)[[:punct:][:space:]]*)+$'
printf '%s' "$PROMPT" | grep -ziqE "$ACK_RE" && { : >"$NO_REVIEW_FILE" 2>/dev/null; rm -f "$SUBAGENTS_FILE" 2>/dev/null; cf_dbg "skip: conversational"; exit 0; }

GITSTATUS="$(git -C "$CWD" status --short 2>/dev/null | head -c 4000)"
[ -n "$GITSTATUS" ] || GITSTATUS="(clean or not a git repository)"

SUBAGENT_PREF="$(cf_prompt_subagent_preference "$PROMPT")"
printf '%s\n' "$SUBAGENT_PREF" >"$SUBAGENTS_FILE" 2>/dev/null
SHOULD_FANOUT="$(cf_userprompt_should_fanout "$PROMPT" "$GITSTATUS" "$SUBAGENT_PREF")"
[ "$CODEX_MAX_AGENTS" -lt 2 ] && SHOULD_FANOUT=0

userprompt_common_context() {
  cat <<EOF
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
EOF
}

single_prompt() {
  cat <<EOF
You are Codex acting as an independent coding peer for Claude Code.

$(userprompt_common_context)

Sub-agent policy:
This hook selected single-agent mode. If your Codex runtime provides internal sub-agents, you may use
bounded read-only delegation only when useful, with a total cap of $CODEX_MAX_AGENTS agents, max depth
1, and the same model/reasoning policy. If the runtime cannot enforce that cap, analyze directly and
optionally return a delegation request instead of spawning.

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
}

role_prompt() {
  _role="$1"
  _role_count="$2"
  case "$_role" in
    planner) _focus="Understand the task, propose the smallest implementation path, identify files/modules involved, and call out sequencing risks.";;
    skeptic) _focus="Challenge the likely plan. Look for hidden correctness, security, data-loss, compatibility, latency, and scope risks.";;
    verifier) _focus="Design the tests/checks and acceptance criteria that would catch regressions or incomplete implementation.";;
    *) _focus="Analyze the task conservatively.";;
  esac
  cat <<EOF
You are Codex acting as the $_role sub-agent for Claude Code.

$(userprompt_common_context)

Sub-agent fanout policy:
The hook launched $_role_count read-only Codex agents out of a configured cap of $CODEX_MAX_AGENTS.
Because parallel hook processes cannot coordinate nested runtime delegation, do not spawn internal
sub-agents from this role. If more delegation would help, return a bounded delegation request.

Role focus:
$_focus

Return under 700 words with:
1. Role summary
2. Evidence or repository signals used
3. Recommendations or concerns
4. Tests/checks Claude should run
5. Assumptions and uncertainty

Prefer evidence quality over consensus. Do not produce a full patch.
EOF
}

run_single() {
  LASTMSG="$(mktemp 2>/dev/null)" || return 1
  _prompt="$(single_prompt)"
  cf_run_codex_to_file "$LASTMSG" "$CWD" "$_prompt" "single"
  _rc=$?
  [ "$_rc" -eq 0 ] || { cf_dbg "codex single rc=$_rc -> skip"; return 1; }
  _analysis="$(cat "$LASTMSG" 2>/dev/null)"
  [ -n "$_analysis" ] || { cf_dbg "empty single analysis -> skip"; return 1; }
  ANALYSIS="$_analysis"
  FANOUT_USED=0
  FANOUT_SPAWNED=0
  FANOUT_SUCCEEDED=0
  return 0
}

run_fanout() {
  AGENT_DIR="$(mktemp -d 2>/dev/null)" || return 1
  ROLES=(planner skeptic verifier)
  SELECTED_ROLES=()
  _limit="$CODEX_MAX_AGENTS"
  [ "$_limit" -gt "${#ROLES[@]}" ] && _limit="${#ROLES[@]}"
  [ "$_limit" -lt 2 ] && { cf_dbg "max agents $_limit too low for fanout; using single"; return 1; }
  _idx=0
  while [ "$_idx" -lt "$_limit" ]; do
    SELECTED_ROLES+=("${ROLES[$_idx]}")
    _idx=$((_idx + 1))
  done
  _role_count="${#SELECTED_ROLES[@]}"

  PIDS=()
  OUTFILES=()
  STATUSFILES=()
  for _role in "${SELECTED_ROLES[@]}"; do
    _out="$AGENT_DIR/$_role.out"
    _status="$AGENT_DIR/$_role.status"
    OUTFILES+=("$_out")
    STATUSFILES+=("$_status")
    _prompt="$(role_prompt "$_role" "$_role_count")"
    (
      cf_run_codex_to_file "$_out" "$CWD" "$_prompt" "$_role"
      printf '%s\n' "$?" >"$_status" 2>/dev/null
    ) &
    PIDS+=("$!")
  done

  for _pid in "${PIDS[@]}"; do
    wait "$_pid"
  done

  _success=0
  _failed=""
  _body=""
  _i=0
  for _role in "${SELECTED_ROLES[@]}"; do
    _rc="$(cat "${STATUSFILES[$_i]}" 2>/dev/null)"
    _content="$(cat "${OUTFILES[$_i]}" 2>/dev/null)"
    if [ "$_rc" = "0" ] && [ -n "$_content" ]; then
      _success=$((_success + 1))
      _body="${_body}
## $_role
$_content
"
    else
      _failed="${_failed} $_role(rc=${_rc:-missing})"
    fi
    _i=$((_i + 1))
  done

  [ "$_success" -gt 0 ] || { cf_dbg "all fanout agents failed:$_failed"; return 2; }
  FANOUT_SPAWNED="${#SELECTED_ROLES[@]}"
  FANOUT_SUCCEEDED="$_success"
  if [ -n "$_failed" ]; then
    _body="${_body}
## Fanout Notes
Failed agents:$_failed
"
  fi
  ANALYSIS="Codex Fusion used bounded read-only sub-agent fanout (spawned $FANOUT_SPAWNED sub-agents; $FANOUT_SUCCEEDED/$FANOUT_SPAWNED succeeded).
$_body"
  FANOUT_USED=1
  return 0
}

ANALYSIS=""
FANOUT_USED=0
FANOUT_SPAWNED=0
FANOUT_SUCCEEDED=0
if [ "$SHOULD_FANOUT" = "1" ]; then
  cf_dbg "fanout selected pref=$SUBAGENT_PREF max=$CODEX_MAX_AGENTS score=$(cf_userprompt_auto_score "$PROMPT" "$GITSTATUS")"
  run_fanout
  FANOUT_RC=$?
  [ "$FANOUT_RC" -eq 0 ] || exit 0
else
  cf_dbg "single selected pref=$SUBAGENT_PREF max=$CODEX_MAX_AGENTS score=$(cf_userprompt_auto_score "$PROMPT" "$GITSTATUS")"
  run_single || exit 0
fi

if [ "$FANOUT_USED" = "1" ]; then
  PREAMBLE="AUTOMATIC CODEX FUSION CONTEXT:
Codex was automatically consulted for this turn using bounded read-only sub-agent fanout (spawned $FANOUT_SPAWNED sub-agents; $FANOUT_SUCCEEDED/$FANOUT_SPAWNED succeeded).
Claude: before editing, compare your own plan with the Codex agent reports. Explicitly note consensus, disagreements, Codex-only insights, and your final decision. Judge by evidence quality, not vote count. You are not required to follow Codex if you disagree."
else
  PREAMBLE="AUTOMATIC CODEX FUSION CONTEXT:
Codex was automatically consulted for this turn as an independent peer.
Claude: before editing, compare your own plan with Codex's analysis. Explicitly note consensus, disagreements, Codex-only insights, and your final decision. You are not required to follow Codex if you disagree."
fi

SYSTEM_MESSAGE=""
if cf_notify_enabled; then
  if [ "$FANOUT_USED" = "1" ]; then
    SYSTEM_MESSAGE="Codex Fusion: spawned $FANOUT_SPAWNED sub-agents; $FANOUT_SUCCEEDED/$FANOUT_SPAWNED succeeded."
  else
    SYSTEM_MESSAGE="Codex Fusion: Codex consulted successfully."
  fi
fi

CODEX_ANALYSIS="$ANALYSIS" PREAMBLE="$PREAMBLE" MAX_CHARS="$MAX_CHARS" SYSTEM_MESSAGE="$SYSTEM_MESSAGE" "$PY" <<'PY'
import os, json
a = os.environ.get("CODEX_ANALYSIS", "")
p = os.environ.get("PREAMBLE", "")
system_message = os.environ.get("SYSTEM_MESSAGE", "")
try: m = int(os.environ.get("MAX_CHARS", "12000"))
except Exception: m = 12000
if len(a) > m: a = a[:m] + "\n\n[...Codex output truncated...]"
ctx = p + "\n\n--- BEGIN CODEX ANALYSIS ---\n" + a + "\n--- END CODEX ANALYSIS ---"
payload = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}}
if system_message:
    payload["systemMessage"] = system_message
print(json.dumps(payload))
PY
cf_dbg "injected $(printf '%s' "$ANALYSIS" | wc -c) chars fanout=$FANOUT_USED"
exit 0
