#!/usr/bin/env bash
# Shared helpers for Codex Fusion hooks. This file is sourced by hook scripts.

cf_init_common() {
  CF_LOG_PREFIX="$1"
  export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
  # Per-user state dir, mode 0700. On a shared /tmp a hostile co-tenant could otherwise pre-create a
  # predictable shared dir and read/poison our baselines, so we also refuse a dir we do not own.
  STATE_DIR="${TMPDIR:-/tmp}/codex-fusion-state-$(id -u 2>/dev/null || echo 0)"
  CODEX_TIMEOUT="${CODEX_FUSION_TIMEOUT:-180}"
  CODEX_MODEL="${CODEX_FUSION_MODEL:-gpt-5.6-sol}"
  CODEX_REASONING="${CODEX_FUSION_EFFORT:-high}"
  CODEX_HOOK_BUDGET="$(cf_positive_int "${CODEX_FUSION_BUDGET:-250}" 250)"
  CF_KILL_GRACE_SECONDS=5
  CF_POSTPROCESS_RESERVE_SECONDS=10
  CF_FALLBACK_MIN_SECONDS=5
  _cf_started_at="$(date +%s 2>/dev/null)"
  case "$_cf_started_at" in ''|*[!0-9]*) _cf_started_at=0;; esac
  CF_DEADLINE_EPOCH=$((_cf_started_at + CODEX_HOOK_BUDGET))
  CODEX_MAX_AGENTS="$(cf_positive_int "${CODEX_FUSION_MAX_AGENTS:-4}" 4)"
  CF_MAX_UNTRACKED_BYTES="$(cf_positive_int "${CODEX_FUSION_MAX_FILE_BYTES:-204800}" 204800)"
  # Paths matched (lowercased, basename and full relative path) against these globs are excluded
  # from every review surface, git status, and diff before anything is handed to Codex. Extend with
  # CODEX_FUSION_EXCLUDE (extra space-separated globs; globs containing spaces are unsupported).
  CF_SENSITIVE_GLOBS='.env .env.* *.env .envrc
*.pem *.key *.p12 *.pfx *.jks *.keystore *.kdbx *.ppk
id_rsa* id_dsa* id_ecdsa* id_ed25519* *_rsa *_dsa *_ecdsa *_ed25519
credentials* *credentials.json secrets* secret.* *history .netrc _netrc .npmrc .pypirc .git-credentials .htpasswd auth.json
*.sqlite *.sqlite3 *.db
.ssh/* .aws/* .gnupg/*'
  [ -n "${CODEX_FUSION_EXCLUDE:-}" ] && CF_SENSITIVE_GLOBS="$CF_SENSITIVE_GLOBS $CODEX_FUSION_EXCLUDE"
}

cf_ensure_state_dir() {
  mkdir -p -m 700 "$STATE_DIR" 2>/dev/null || return 1
  [ -O "$STATE_DIR" ] || return 1
}

cf_dbg() {
  [ "${CODEX_FUSION_DEBUG:-0}" = "1" ] || return 0
  cf_ensure_state_dir || return 0
  printf '%s %s: %s\n' "$$" "$CF_LOG_PREFIX" "$*" >>"$STATE_DIR/debug.log"
}

cf_state_key() {
  # $1 = sanitized session id, $2 = cwd. Fallback keys can collide: concurrent sessions in one cwd
  # share a key, and without git every fallback collapses onto cwd-unknown. Accepted as-is because
  # Claude Code always supplies session_id; the fallback only serves manual invocations.
  if [ -n "$1" ]; then
    printf 'session-%s' "$1"
  else
    _sk_hash="$(printf '%s' "$2" | git hash-object --stdin 2>/dev/null)"
    printf 'cwd-%s' "${_sk_hash:-unknown}"
  fi
}

cf_positive_int() {
  case "$1" in
    ''|*[!0-9]*) printf '%s' "$2";;
    0) printf '%s' "$2";;
    *) printf '%s' "$1";;
  esac
}

cf_remaining_seconds() {
  _cr_now="$(date +%s 2>/dev/null)"
  case "$_cr_now" in ''|*[!0-9]*) return 1;; esac
  _cr_remaining=$((CF_DEADLINE_EPOCH - _cr_now))
  [ "$_cr_remaining" -gt 0 ] || return 1
  printf '%s' "$_cr_remaining"
}

cf_call_timeout() {
  _ct_remaining="$(cf_remaining_seconds)" || return 1
  # Keep the hard-kill grace and bounded result processing inside the whole-hook deadline.
  _ct_usable=$((_ct_remaining - CF_KILL_GRACE_SECONDS - CF_POSTPROCESS_RESERVE_SECONDS))
  [ "$_ct_usable" -gt 0 ] || return 1
  _ct_per_call="$(cf_positive_int "$CODEX_TIMEOUT" 180)"
  [ "$_ct_per_call" -lt "$_ct_usable" ] && printf '%s' "$_ct_per_call" || printf '%s' "$_ct_usable"
}

cf_read_bounded_file() {
  [ -f "$1" ] || return 1
  head -c "$(cf_positive_int "$2" 100000)" -- "$1" 2>/dev/null
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

cf_subagent_verify_mode() {
  case "${CODEX_FUSION_SUBAGENT_VERIFY:-auto}" in
    off|OFF|Off) printf 'off';;
    always|ALWAYS|Always) printf 'always';;
    *) printf 'auto';;
  esac
}

cf_subagent_should_verify() {
  # $1 = subagent report length in bytes, $2 = 1 when the repo changed since the prompt baseline.
  # Deliberately near-universal, like the UserPromptSubmit gate: a subagent that touched the tree is
  # always verified, and one that only reported is verified unless its report is trivially short.
  case "$(cf_subagent_verify_mode)" in
    off) printf '0'; return 0;;
    always) printf '1'; return 0;;
  esac
  [ "$2" = "1" ] && { printf '1'; return 0; }
  [ "${1:-0}" -ge "$(cf_positive_int "${CODEX_FUSION_SUBAGENT_MIN_CHARS:-200}" 200)" ] && { printf '1'; return 0; }
  printf '0'
}

cf_subagent_verify_should_fanout() {
  # $1 = the session's stored subagent preference. Verification defaults to ONE Codex agent: a
  # SubagentStop fires per subagent, and parallel subagents already multiply the Codex call count.
  case "$1" in
    single) printf '0'; return 0;;
    force) printf '1'; return 0;;
  esac
  case "$(cf_subagent_mode)" in
    always) printf '1'; return 0;;
  esac
  printf '0'
}

cf_sensitive_path() {
  _sp_path="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  _sp_base="${_sp_path##*/}"
  # set -f: the glob words must reach the case patterns literally, not expand against the cwd.
  set -f
  for _sp_g in $CF_SENSITIVE_GLOBS; do
    case "$_sp_base" in $_sp_g) set +f; return 0;; esac
    case "$_sp_path" in $_sp_g|*/$_sp_g) set +f; return 0;; esac
  done
  set +f
  return 1
}

cf_filtered_status() {
  git -C "$1" status --short 2>/dev/null |
    while IFS= read -r _fs_line; do
      _fs_rest="${_fs_line:3}"
      _fs_old="$_fs_rest"
      _fs_new="$_fs_rest"
      case "$_fs_rest" in *' -> '*) _fs_old="${_fs_rest%% -> *}"; _fs_new="${_fs_rest##* -> }";; esac
      case "$_fs_old" in \"*\") _fs_old="${_fs_old#\"}"; _fs_old="${_fs_old%\"}";; esac
      case "$_fs_new" in \"*\") _fs_new="${_fs_new#\"}"; _fs_new="${_fs_new%\"}";; esac
      if cf_sensitive_path "$_fs_old" || cf_sensitive_path "$_fs_new"; then
        printf '%s [redacted sensitive path]\n' "${_fs_line:0:2}"
      else
        printf '%s\n' "$_fs_line"
      fi
    done
}

cf_exclude_pathspecs() {
  # Fills CF_EXCLUDES with ':(exclude,icase,top)' pathspecs for the tracked diff. Git pathspec
  # wildcards cross '/' (fnmatch without FNM_PATHNAME), so G plus */G covers root-level and nested
  # matches. The 'top' magic anchors the globs to the repo root: without it git prefixes pathspecs
  # with the cwd-relative path, and a session cwd inside a subdirectory would stop the excludes
  # from matching denylisted files elsewhere in the repo (a real leak, not a corner case).
  CF_EXCLUDES=()
  set -f
  for _ep_g in $CF_SENSITIVE_GLOBS; do
    CF_EXCLUDES+=(":(exclude,icase,top)$_ep_g" ":(exclude,icase,top)*/$_ep_g")
  done
  set +f
}

cf_review_surface() {
  # $1 = repo dir, $2 = base ref (HEAD at prompt time; the stored prompt-time SHA at stop time).
  # Baseline and Stop hash-compare these surfaces, so every line here must be deterministic and the
  # section headers must not embed the ref itself.
  _rs_cwd="$1"
  _rs_base="$2"
  git -C "$_rs_cwd" rev-parse --verify --quiet "$_rs_base^{commit}" >/dev/null 2>&1 || return 1
  cf_exclude_pathspecs
  printf '### note: sensitive, oversized, and binary paths are excluded from this surface\n'
  printf '### tracked diff against prompt-time base\n'
  git -C "$_rs_cwd" diff "$_rs_base" -- "${CF_EXCLUDES[@]}" 2>/dev/null || return 1
  printf '\n### untracked files\n'
  git -C "$_rs_cwd" ls-files --others --exclude-standard -z 2>/dev/null |
    while IFS= read -r -d '' _rs_f; do
      if cf_sensitive_path "$_rs_f"; then
        printf '\n--- untracked file: %s (excluded: sensitive path) ---\n' "$_rs_f"
        continue
      fi
      _rs_sz="$(wc -c <"$_rs_cwd/$_rs_f" 2>/dev/null | tr -d ' ')"
      if [ "${_rs_sz:-0}" -gt "$CF_MAX_UNTRACKED_BYTES" ]; then
        printf '\n--- untracked file: %s (excluded: %s bytes exceeds %s cap) ---\n' "$_rs_f" "$_rs_sz" "$CF_MAX_UNTRACKED_BYTES"
        continue
      fi
      if git -C "$_rs_cwd" diff --no-index --numstat -- /dev/null "$_rs_cwd/$_rs_f" 2>/dev/null | grep -q '^-'; then
        printf '\n--- untracked file: %s (excluded: binary) ---\n' "$_rs_f"
        continue
      fi
      printf '\n--- untracked file: %s ---\n' "$_rs_f"
      git -C "$_rs_cwd" diff --no-index -- /dev/null "$_rs_cwd/$_rs_f" 2>/dev/null || true
    done
}

cf_truncate_bytes() {
  # $1 = text, $2 = byte cap, $3 = label for the marker. Cuts on a line boundary where possible and
  # appends an explicit marker so Codex knows it reviewed a partial payload.
  _tb_len="$(printf '%s' "$1" | wc -c | tr -d ' ')"
  if [ "${_tb_len:-0}" -le "$2" ]; then
    printf '%s' "$1"
    return 0
  fi
  # Drop the trailing partial line with sed, NOT with '${v%"${v##*$'\n'}"}'. Bash 3.2 (macOS's
  # /bin/bash, which '#!/usr/bin/env bash' resolves to) needs ~30s to match that glob against a
  # 100KB value; the hook would blow past its registration timeout and a found issue would be lost.
  # An unbroken cut region has no newline to trim, so keep the raw cut in that case.
  _tb_head="$(printf '%s' "$1" | head -c "$2")"
  _tb_trim="$(printf '%s' "$_tb_head" | sed '$d')"
  [ -n "$_tb_trim" ] && _tb_head="$_tb_trim"
  printf '%s\n[... %s truncated at %s bytes ...]\n' "$_tb_head" "$3" "$2"
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
  _call_timeout="$(cf_call_timeout)" || { cf_dbg "role=$_role whole-hook budget exhausted before primary"; return 124; }
  cf_dbg "running codex role=$_role model=${CODEX_MODEL:-default} effort=$CODEX_REASONING timeout=$_call_timeout budget=$CODEX_HOOK_BUDGET cwd=$_cwd"
  CODEX_FUSION_AGENT_ROLE="$_role" CLAUDE_FUSION_ACTIVE=1 CODEX_FUSION_ACTIVE=1 \
    timeout -k "$CF_KILL_GRACE_SECONDS" "$_call_timeout" "$CODEX_BIN" "${_model_args[@]}" -c model_reasoning_effort="$CODEX_REASONING" \
    --ask-for-approval never exec \
    -C "$_cwd" --sandbox read-only --color never --skip-git-repo-check \
    -o "$_out" "$_prompt" </dev/null >/dev/null 2>&1
  _rc=$?

  if [ "$_rc" -ne 0 ] && [ "$_rc" -ne 124 ] && [ "${#_model_args[@]}" -gt 0 ]; then
    _fallback_timeout="$(cf_call_timeout)" || { cf_dbg "role=$_role whole-hook budget exhausted; skipping fallback"; return "$_rc"; }
    if [ "$_fallback_timeout" -lt "$CF_FALLBACK_MIN_SECONDS" ]; then
      cf_dbg "role=$_role only ${_fallback_timeout}s remain; skipping fallback"
      return "$_rc"
    fi
    cf_dbg "role=$_role model $CODEX_MODEL failed rc=$_rc; retrying with codex default model"
    _model_args=()
    : >"$_out" 2>/dev/null || return 1
    CODEX_FUSION_AGENT_ROLE="$_role" CLAUDE_FUSION_ACTIVE=1 CODEX_FUSION_ACTIVE=1 \
      timeout -k "$CF_KILL_GRACE_SECONDS" "$_fallback_timeout" "$CODEX_BIN" -c model_reasoning_effort="$CODEX_REASONING" \
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
