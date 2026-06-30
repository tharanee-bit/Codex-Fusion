#!/usr/bin/env bash
# codex-fusion-stop.sh  (Claude Code Stop hook)
# When the working tree changed since this turn's prompt-start baseline, run Codex READ-ONLY over
# the incremental review surface. If Codex returns ISSUES_FOUND, block ONCE with the review.
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hooks/codex-fusion-common.sh
. "$SCRIPT_DIR/codex-fusion-common.sh" 2>/dev/null || exit 0
cf_init_common "STOP"

if cf_nested_fusion_active; then
  cf_dbg "skip: nested fusion active"
  exit 0
fi

cf_setup_codex_runtime || exit 0

MAX_DIFF=20000
MAX_CHARS=12000
LASTMSG=""
CURRENT_FILE=""
AGENT_DIR=""
trap 'rm -f "$LASTMSG" "$CURRENT_FILE" 2>/dev/null; rm -rf "$AGENT_DIR" 2>/dev/null' EXIT

INPUT="$(cat)"
[ -n "$INPUT" ] || exit 0
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
case "$SESSION_ID" in *[!A-Za-z0-9._-]*|.|..) SESSION_ID="";; esac

[ "$STOP_ACTIVE" = "true" ] && { cf_dbg "stop_hook_active -> exit"; exit 0; }
[ -d "$CWD" ] || CWD="$PWD"

if [ -n "$SESSION_ID" ]; then
  STATE_KEY="session-$SESSION_ID"
else
  CWD_HASH="$(printf '%s' "$CWD" | git hash-object --stdin 2>/dev/null)"
  STATE_KEY="cwd-${CWD_HASH:-unknown}"
fi
BASELINE_FILE="$STATE_DIR/$STATE_KEY.baseline"
NO_REVIEW_FILE="$STATE_DIR/$STATE_KEY.no-review"
REVIEWED_FILE="$STATE_DIR/$STATE_KEY.reviewed"
SUBAGENTS_FILE="$STATE_DIR/$STATE_KEY.subagents"
FAILED_FILE="$STATE_DIR/$STATE_KEY.failed-review"

[ -f "$NO_REVIEW_FILE" ] && { cf_dbg "no-review flag -> exit"; exit 0; }
[ -f "$BASELINE_FILE" ] || { cf_dbg "no prompt baseline -> exit"; exit 0; }

CURRENT_FILE="$(mktemp 2>/dev/null)" || exit 0

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

review_surface >"$CURRENT_FILE" 2>/dev/null || { cf_dbg "current review surface unavailable -> exit"; exit 0; }
DIFF_HASH="$(git hash-object "$CURRENT_FILE" 2>/dev/null)"
[ -n "$DIFF_HASH" ] || { cf_dbg "empty diff hash -> exit"; exit 0; }
BASELINE_HASH="$(git hash-object "$BASELINE_FILE" 2>/dev/null)"
[ "$BASELINE_HASH" = "$DIFF_HASH" ] && { cf_dbg "no changes since prompt baseline -> exit"; exit 0; }

LAST_REVIEWED="$(cat "$REVIEWED_FILE" 2>/dev/null)"
[ "$LAST_REVIEWED" = "$DIFF_HASH" ] && { cf_dbg "diff already reviewed -> exit"; exit 0; }

retry_limit() {
  cf_positive_int "${CODEX_FUSION_STOP_RETRY_LIMIT:-2}" 2
}

retry_exhausted() {
  [ -f "$FAILED_FILE" ] || return 1
  read -r _hash _count <"$FAILED_FILE" 2>/dev/null
  [ "$_hash" = "$DIFF_HASH" ] || return 1
  [ "${_count:-0}" -ge "$(retry_limit)" ]
}

record_review_failure() {
  mkdir -p -m 700 "$STATE_DIR" 2>/dev/null || return 0
  _old_hash=""
  _old_count=0
  [ -f "$FAILED_FILE" ] && read -r _old_hash _old_count <"$FAILED_FILE" 2>/dev/null
  if [ "$_old_hash" = "$DIFF_HASH" ]; then
    _new_count=$(( ${_old_count:-0} + 1 ))
  else
    _new_count=1
  fi
  printf '%s %s\n' "$DIFF_HASH" "$_new_count" >"$FAILED_FILE" 2>/dev/null
  cf_dbg "recorded review failure count=$_new_count hash=$DIFF_HASH"
}

clear_review_failure() {
  rm -f "$FAILED_FILE" 2>/dev/null
}

retry_exhausted && { cf_dbg "review retry cap reached for unchanged diff"; exit 0; }

DIFF="$(diff -u --label prompt-baseline --label current "$BASELINE_FILE" "$CURRENT_FILE" 2>/dev/null | head -c "$MAX_DIFF")"
[ -n "$DIFF" ] || { cf_dbg "empty incremental diff -> exit"; exit 0; }
CHANGED="$(git -C "$CWD" status --short 2>/dev/null | head -c 3000)"

store_reviewed() {
  mkdir -p -m 700 "$STATE_DIR" 2>/dev/null && printf '%s\n' "$DIFF_HASH" >"$REVIEWED_FILE" 2>/dev/null
}

SUBAGENT_PREF="$(cat "$SUBAGENTS_FILE" 2>/dev/null)"
case "$SUBAGENT_PREF" in force|single|auto) ;; *) SUBAGENT_PREF="auto";; esac
SHOULD_FANOUT="$(cf_stop_should_fanout "$SUBAGENT_PREF" "$DIFF" "$CHANGED")"
[ "$CODEX_MAX_AGENTS" -lt 2 ] && SHOULD_FANOUT=0

stop_common_context() {
  cat <<EOF
You are running automatically from a Stop hook, in read-only mode.
Do not edit files. Do not run destructive commands.
Do not inspect credentials, tokens, .env files, keychains, shell history, or auth files.

Review only the incremental changes since the prompt-start baseline below for SERIOUS problems:
correctness bugs, security vulnerabilities, data-loss risks, concurrency/race issues, and broken
or missing tests. Ignore pure style/formatting nits and ignore issues that only appear in the
prompt-baseline side.

The VERY FIRST line of your response MUST be exactly one of:
CODEX_REVIEW_VERDICT: PASS
CODEX_REVIEW_VERDICT: ISSUES_FOUND

If ISSUES_FOUND, list each serious issue (most important first) as:
- <file:line> - <problem> - <minimal fix>
Keep it under 800 words.

Repository:
$CWD

Changed files:
$CHANGED

Incremental review surface:
$DIFF
EOF
}

single_review_prompt() {
  cat <<EOF
You are Codex acting as an independent code reviewer for Claude Code.

$(stop_common_context)

Sub-agent policy:
This hook selected single-agent review mode. If your Codex runtime provides internal sub-agents, you
may use bounded read-only delegation only when useful, with a total cap of $CODEX_MAX_AGENTS agents,
max depth 1, and the same model/reasoning policy. Your final answer must still start with the exact
verdict line.
EOF
}

role_review_prompt() {
  _role="$1"
  _role_count="$2"
  case "$_role" in
    correctness) _focus="Focus on correctness, edge cases, data integrity, control flow, and API/contract regressions.";;
    security-data-loss-concurrency) _focus="Focus on security vulnerabilities, data-loss risks, credential exposure, permissions, races, and concurrency defects.";;
    tests-regression) _focus="Focus on broken or missing tests, regression coverage, CI risks, and acceptance gaps.";;
    *) _focus="Focus on serious review findings.";;
  esac
  cat <<EOF
You are Codex acting as the $_role review sub-agent for Claude Code.

$(stop_common_context)

Sub-agent fanout policy:
The hook launched $_role_count read-only review agents out of a configured cap of $CODEX_MAX_AGENTS.
Because parallel hook processes cannot coordinate nested runtime delegation, do not spawn internal
sub-agents from this role. If more delegation would help, return a bounded delegation request after
the required verdict.

Role focus:
$_focus
EOF
}

emit_block() {
  REVIEW="$1" MAX_CHARS="$MAX_CHARS" "$PY" <<'PY'
import os, json
r = os.environ.get("REVIEW", "")
try: m = int(os.environ.get("MAX_CHARS", "12000"))
except Exception: m = 12000
if len(r) > m: r = r[:m] + "\n\n[...truncated...]"
reason = ("AUTOMATIC CODEX FUSION - POST-DIFF REVIEW:\n"
          "Codex independently reviewed your incremental changes and flagged potential issues. Address the "
          "serious problems (correctness, security, data-loss, concurrency, broken tests) before "
          "finalizing, or explicitly justify why each is not a real issue. You remain the final judge.\n\n" + r)
print(json.dumps({"decision": "block", "reason": reason}))
PY
}

run_single_review() {
  LASTMSG="$(mktemp 2>/dev/null)" || return 1
  _prompt="$(single_review_prompt)"
  cf_run_codex_to_file "$LASTMSG" "$CWD" "$_prompt" "single-review"
  _rc=$?
  [ "$_rc" -eq 0 ] || { cf_dbg "codex single review rc=$_rc"; return 1; }
  REVIEW="$(cat "$LASTMSG" 2>/dev/null)"
  [ -n "$REVIEW" ] || { cf_dbg "empty single review"; return 1; }
  return 0
}

run_fanout_review() {
  AGENT_DIR="$(mktemp -d 2>/dev/null)" || return 1
  ROLES=(correctness security-data-loss-concurrency tests-regression)
  SELECTED_ROLES=()
  _limit="$CODEX_MAX_AGENTS"
  [ "$_limit" -gt "${#ROLES[@]}" ] && _limit="${#ROLES[@]}"
  [ "$_limit" -lt 2 ] && { cf_dbg "max agents $_limit too low for stop fanout; using single"; return 1; }
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
    _prompt="$(role_review_prompt "$_role" "$_role_count")"
    (
      cf_run_codex_to_file "$_out" "$CWD" "$_prompt" "$_role"
      printf '%s\n' "$?" >"$_status" 2>/dev/null
    ) &
    PIDS+=("$!")
  done

  for _pid in "${PIDS[@]}"; do
    wait "$_pid"
  done

  REVIEW=""
  FAILED_ROLES=""
  SUCCESS_COUNT=0
  FAILED_COUNT=0
  ISSUE_COUNT=0
  _i=0
  for _role in "${SELECTED_ROLES[@]}"; do
    _rc="$(cat "${STATUSFILES[$_i]}" 2>/dev/null)"
    _content="$(cat "${OUTFILES[$_i]}" 2>/dev/null)"
    if [ "$_rc" = "0" ] && [ -n "$_content" ]; then
      SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
      _verdict="$(cf_first_nonempty_line "$_content")"
      if printf '%s' "$_verdict" | grep -qiE 'CODEX_REVIEW_VERDICT:[[:space:]]*ISSUES_FOUND'; then
        ISSUE_COUNT=$((ISSUE_COUNT + 1))
        REVIEW="${REVIEW}
## $_role
$_content
"
      fi
    else
      FAILED_COUNT=$((FAILED_COUNT + 1))
      FAILED_ROLES="${FAILED_ROLES} $_role(rc=${_rc:-missing})"
    fi
    _i=$((_i + 1))
  done

  if [ "$ISSUE_COUNT" -gt 0 ]; then
    REVIEW_FANOUT_SPAWNED="${#SELECTED_ROLES[@]}"
    REVIEW_FANOUT_SUCCEEDED="$SUCCESS_COUNT"
    REVIEW="CODEX_REVIEW_VERDICT: ISSUES_FOUND
Codex Fusion used bounded read-only post-diff review fanout (spawned $REVIEW_FANOUT_SPAWNED review sub-agents; $REVIEW_FANOUT_SUCCEEDED/$REVIEW_FANOUT_SPAWNED succeeded). $ISSUE_COUNT agent(s) reported serious issues.
$REVIEW"
    [ -n "$FAILED_ROLES" ] && REVIEW="${REVIEW}
## Fanout Notes
Failed agents:$FAILED_ROLES
"
  elif [ "$FAILED_COUNT" -gt 0 ] || [ "$SUCCESS_COUNT" -eq 0 ]; then
    REVIEW=""
    return 2
  else
    REVIEW_FANOUT_SPAWNED="${#SELECTED_ROLES[@]}"
    REVIEW_FANOUT_SUCCEEDED="$SUCCESS_COUNT"
    REVIEW="CODEX_REVIEW_VERDICT: PASS
Codex Fusion used bounded read-only post-diff review fanout (spawned $REVIEW_FANOUT_SPAWNED review sub-agents; all $REVIEW_FANOUT_SUCCEEDED passed)."
  fi
  FANOUT_REVIEW_USED=1
  return 0
}

FANOUT_REVIEW_USED=0
REVIEW_FANOUT_SPAWNED=0
REVIEW_FANOUT_SUCCEEDED=0
if [ "$SHOULD_FANOUT" = "1" ]; then
  cf_dbg "stop fanout selected pref=$SUBAGENT_PREF max=$CODEX_MAX_AGENTS files=$(cf_changed_file_count "$CHANGED")"
  run_fanout_review
  REVIEW_RC=$?
  if [ "$REVIEW_RC" -ne 0 ]; then
    record_review_failure
    exit 0
  fi
else
  cf_dbg "stop single selected pref=$SUBAGENT_PREF max=$CODEX_MAX_AGENTS files=$(cf_changed_file_count "$CHANGED")"
  run_single_review
  REVIEW_RC=$?
  if [ "$REVIEW_RC" -ne 0 ]; then
    record_review_failure
    exit 0
  fi
fi

VERDICT_LINE="$(cf_first_nonempty_line "$REVIEW")"
if ! printf '%s' "$VERDICT_LINE" | grep -qiE 'CODEX_REVIEW_VERDICT:[[:space:]]*ISSUES_FOUND'; then
  store_reviewed
  clear_review_failure
  if [ "$FANOUT_REVIEW_USED" = "1" ] && cf_notify_enabled; then
    SYSTEM_MESSAGE="Codex Fusion: spawned $REVIEW_FANOUT_SPAWNED review sub-agents; all $REVIEW_FANOUT_SUCCEEDED passed." "$PY" <<'PY'
import os, json
print(json.dumps({"systemMessage": os.environ.get("SYSTEM_MESSAGE", "")}))
PY
  fi
  cf_dbg "verdict PASS/none -> exit"
  exit 0
fi

BLOCK_JSON="$(emit_block "$REVIEW")"
EMIT_RC=$?
[ "$EMIT_RC" -eq 0 ] && [ -n "$BLOCK_JSON" ] || { record_review_failure; cf_dbg "block json emit failed"; exit 0; }
printf '%s\n' "$BLOCK_JSON" || { record_review_failure; cf_dbg "block json delivery failed"; exit 0; }
store_reviewed
clear_review_failure
cf_dbg "blocked with ISSUES_FOUND"
exit 0
