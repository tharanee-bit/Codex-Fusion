#!/usr/bin/env bash
# Shared helpers for Codex Fusion hooks. This file is sourced by hook scripts.

cf_init_common() {
  CF_LOG_PREFIX="$1"
  export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
  STATE_DIR="${TMPDIR:-/tmp}/codex-fusion-state"
  CODEX_TIMEOUT="${CODEX_FUSION_TIMEOUT:-180}"
  CODEX_MODEL="${CODEX_FUSION_MODEL:-gpt-5.5}"
  CODEX_REASONING="${CODEX_FUSION_EFFORT:-xhigh}"
  CODEX_MAX_AGENTS="$(cf_positive_int "${CODEX_FUSION_MAX_AGENTS:-4}" 4)"
}

cf_dbg() {
  [ "${CODEX_FUSION_DEBUG:-0}" = "1" ] || return 0
  mkdir -p -m 700 "$STATE_DIR" 2>/dev/null || return 0
  printf '%s %s: %s\n' "$$" "$CF_LOG_PREFIX" "$*" >>"$STATE_DIR/debug.log"
}

cf_positive_int() {
  case "$1" in
    ''|*[!0-9]*) printf '%s' "$2";;
    0) printf '%s' "$2";;
    *) printf '%s' "$1";;
  esac
}

cf_subagent_mode() {
  case "${CODEX_FUSION_SUBAGENTS:-auto}" in
    off|OFF|Off) printf 'off';;
    always|ALWAYS|Always) printf 'always';;
    *) printf 'auto';;
  esac
}

cf_notify_enabled() {
  [ "${CODEX_FUSION_NOTIFY:-1}" != "0" ]
}

cf_setup_codex_runtime() {
  PY="/usr/bin/python3"
  [ -x "$PY" ] || PY="$(command -v python3 2>/dev/null)"
  [ -x "$PY" ] || { cf_dbg "no python3"; return 1; }

  command -v timeout >/dev/null 2>&1 || { cf_dbg "no timeout"; return 1; }

  CODEX_BIN="$(command -v codex 2>/dev/null)"
  if [ ! -x "$CODEX_BIN" ]; then
    for c in "$HOME/.local/bin/codex" "$HOME/bin/codex" "/usr/local/bin/codex" "$HOME/.npm-global/bin/codex"; do
      [ -x "$c" ] && { CODEX_BIN="$c"; break; }
    done
  fi
  [ -x "$CODEX_BIN" ] || { cf_dbg "no codex"; return 1; }

  _cx_dir="$(dirname "$("$PY" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CODEX_BIN" 2>/dev/null || echo "$CODEX_BIN")")"
  case ":$PATH:" in *":$_cx_dir:"*) ;; *) export PATH="$_cx_dir:$PATH";; esac
  return 0
}

cf_nested_fusion_active() {
  [ "${CLAUDE_FUSION_ACTIVE:-0}" = "1" ] || [ "${CODEX_FUSION_ACTIVE:-0}" = "1" ]
}

cf_has_force_subagents() {
  printf '%s' "$1" | grep -qiE '\[(codex-)?subagents\]'
}

cf_has_no_subagents() {
  printf '%s' "$1" | grep -qiF '[no-subagents]'
}

cf_prompt_subagent_preference() {
  if cf_has_no_subagents "$1"; then
    printf 'single'
  elif cf_has_force_subagents "$1"; then
    printf 'force'
  else
    printf 'auto'
  fi
}

cf_line_count() {
  printf '%s' "$1" | awk 'END { print NR }'
}

cf_userprompt_auto_score() {
  _prompt="$1"
  _status="$2"
  _score=0
  _chars="$(printf '%s' "$_prompt" | wc -c | tr -d ' ')"
  _lines="$(cf_line_count "$_prompt")"
  [ "${_chars:-0}" -gt 500 ] && _score=$((_score + 1))
  [ "${_lines:-0}" -gt 4 ] && _score=$((_score + 1))
  printf '%s' "$_prompt" | grep -qiE '(architect|audit|broad|cross-check|debug|deploy|failing|fix|implement|large|migrat|multi[- ]file|plan|refactor|review|tests?|(^|[^[:alnum:]_])ci([^[:alnum:]_]|$))' && _score=$((_score + 1))
  printf '%s' "$_prompt" | grep -qiE '(auth|concurr|credential|data[- ]loss|database|migration|payment|permission|production|race|schema|security|token)' && _score=$((_score + 1))
  _changed="$(printf '%s\n' "$_status" | grep -cE '^[ MADRCU?]')"
  [ "${_changed:-0}" -ge 3 ] && _score=$((_score + 1))
  printf '%s' "$_score"
}

cf_userprompt_should_fanout() {
  _prompt="$1"
  _status="$2"
  _pref="$3"
  case "$_pref" in
    single) printf '0'; return 0;;
    force) printf '1'; return 0;;
  esac
  case "$(cf_subagent_mode)" in
    off) printf '0'; return 0;;
    always) printf '1'; return 0;;
  esac
  _score="$(cf_userprompt_auto_score "$_prompt" "$_status")"
  [ "${_score:-0}" -ge 2 ] && printf '1' || printf '0'
}

cf_changed_file_count() {
  printf '%s\n' "$1" | grep -cE '^[ MADRCU?]'
}

cf_stop_should_fanout() {
  _pref="$1"
  _diff="$2"
  _changed="$3"
  case "$_pref" in
    single) printf '0'; return 0;;
    force) printf '1'; return 0;;
  esac
  case "$(cf_subagent_mode)" in
    off) printf '0'; return 0;;
    always) printf '1'; return 0;;
  esac
  _bytes="$(printf '%s' "$_diff" | wc -c | tr -d ' ')"
  _files="$(cf_changed_file_count "$_changed")"
  [ "${_bytes:-0}" -gt 4000 ] && { printf '1'; return 0; }
  [ "${_files:-0}" -ge 3 ] && { printf '1'; return 0; }
  printf '%s\n%s' "$_changed" "$_diff" | grep -qiE '(auth|alembic|concurr|credential|database|migration|password|permission|race|schema|security|sql|token|unittest|workflow|(^|[^[:alnum:]_])(db|tests?)([^[:alnum:]_]|$))' && { printf '1'; return 0; }
  printf '0'
}

cf_run_codex_to_file() {
  _out="$1"
  _cwd="$2"
  _prompt="$3"
  _role="$4"
  : >"$_out" 2>/dev/null || return 1

  _model_args=()
  [ -n "$CODEX_MODEL" ] && _model_args=(-m "$CODEX_MODEL")
  cf_dbg "running codex role=$_role model=${CODEX_MODEL:-default} effort=$CODEX_REASONING timeout=$CODEX_TIMEOUT cwd=$_cwd"
  CODEX_FUSION_AGENT_ROLE="$_role" CLAUDE_FUSION_ACTIVE=1 CODEX_FUSION_ACTIVE=1 \
    timeout "$CODEX_TIMEOUT" "$CODEX_BIN" "${_model_args[@]}" -c model_reasoning_effort="$CODEX_REASONING" \
    --ask-for-approval never exec \
    -C "$_cwd" --sandbox read-only --color never --skip-git-repo-check \
    -o "$_out" "$_prompt" </dev/null >/dev/null 2>&1
  _rc=$?

  if [ "$_rc" -ne 0 ] && [ "$_rc" -ne 124 ] && [ "${#_model_args[@]}" -gt 0 ]; then
    cf_dbg "role=$_role model $CODEX_MODEL failed rc=$_rc; retrying with codex default model"
    _model_args=()
    : >"$_out" 2>/dev/null || return 1
    CODEX_FUSION_AGENT_ROLE="$_role" CLAUDE_FUSION_ACTIVE=1 CODEX_FUSION_ACTIVE=1 \
      timeout "$CODEX_TIMEOUT" "$CODEX_BIN" -c model_reasoning_effort="$CODEX_REASONING" \
      --ask-for-approval never exec \
      -C "$_cwd" --sandbox read-only --color never --skip-git-repo-check \
      -o "$_out" "$_prompt" </dev/null >/dev/null 2>&1
    _rc=$?
  fi

  return "$_rc"
}

cf_first_nonempty_line() {
  printf '%s' "$1" | grep -m1 -vE '^[[:space:]]*$'
}
