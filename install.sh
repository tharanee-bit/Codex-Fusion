#!/usr/bin/env bash
# Codex Fusion installer.
# Copies the hook scripts + skill into ~/.claude and merges the UserPromptSubmit + Stop +
# SubagentStop hooks into ~/.claude/settings.json non-destructively and idempotently.
# Honors CLAUDE_CONFIG_DIR. Requires python3 (used to safely edit settings.json).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
HOOKS_DIR="$CLAUDE_DIR/hooks"
SKILLS_DIR="$CLAUDE_DIR/skills"
SETTINGS="$CLAUDE_DIR/settings.json"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "ERROR: python3 is required (used to merge settings.json)." >&2; exit 1; }

if ! command -v codex >/dev/null 2>&1; then
  echo "WARNING: 'codex' not found on PATH. Install the Codex CLI and run 'codex login'." >&2
  echo "         Installing the hooks anyway; they will silently skip until codex is available." >&2
fi

mkdir -p "$HOOKS_DIR" "$SKILLS_DIR/codex-fusion-auto"
install -m 0644 "$HERE/hooks/codex-fusion-common.sh"      "$HOOKS_DIR/codex-fusion-common.sh"
install -m 0755 "$HERE/hooks/codex-fusion-userprompt.sh"  "$HOOKS_DIR/codex-fusion-userprompt.sh"
install -m 0755 "$HERE/hooks/codex-fusion-stop.sh"        "$HOOKS_DIR/codex-fusion-stop.sh"
install -m 0755 "$HERE/hooks/codex-fusion-subagent-stop.sh" "$HOOKS_DIR/codex-fusion-subagent-stop.sh"
install -m 0644 "$HERE/skills/codex-fusion-auto/SKILL.md" "$SKILLS_DIR/codex-fusion-auto/SKILL.md"
echo "Installed hooks + skill into $CLAUDE_DIR"

CF_UPS="$HOOKS_DIR/codex-fusion-userprompt.sh" \
CF_STOP="$HOOKS_DIR/codex-fusion-stop.sh" \
CF_SUBSTOP="$HOOKS_DIR/codex-fusion-subagent-stop.sh" \
CF_SETTINGS="$SETTINGS" "$PY" - <<'PY'
import json, os, sys, shutil, tempfile

# realpath: users symlink settings.json into dotfile repos; os.replace on the symlink path would
# swap the link itself for a regular file. Resolve first so the atomic write lands on the target.
settings = os.path.realpath(os.environ["CF_SETTINGS"])
ups, stop, substop = os.environ["CF_UPS"], os.environ["CF_STOP"], os.environ["CF_SUBSTOP"]

data = {}
orig_text = None
if os.path.exists(settings):
    try:
        with open(settings) as f:
            orig_text = f.read()
        data = json.loads(orig_text)
    except Exception as e:
        print(f"ERROR: {settings} is not valid JSON ({e}); aborting so it isn't clobbered.", file=sys.stderr)
        sys.exit(1)

if not isinstance(data, dict):
    print("ERROR: settings.json is not a JSON object; aborting.", file=sys.stderr); sys.exit(1)

hooks = data.setdefault("hooks", {})

# Hook registration timeout. Keep in sync with settings.snippet.json and comfortably above the
# hooks' internal CODEX_TIMEOUT (180s by default) so xhigh Codex calls finish instead of being killed.
HOOK_TIMEOUT = 270
USERPROMPT_STATUS = "Codex Fusion: checking Codex..."
STOP_STATUS = "Codex Fusion: reviewing changes..."
SUBAGENT_STOP_STATUS = "Codex Fusion: verifying subagent..."

def norm(cmd):
    # Match by resolved path so a manually merged "$HOME/..." snippet entry is recognized as the
    # same hook as the absolute path this installer registers (no duplicate entries at the seam).
    return os.path.normpath(os.path.expanduser(os.path.expandvars(cmd or "")))

def ensure(event, command, status_message):
    arr = hooks.setdefault(event, [])
    target = norm(command)
    for grp in arr:
        for h in grp.get("hooks", []):
            if norm(h.get("command")) == target:
                # Already present (keep its original command string): converge config on upgrade.
                changed = False
                if h.get("type") != "command":
                    h["type"] = "command"; changed = True
                if h.get("timeout") != HOOK_TIMEOUT:
                    h["timeout"] = HOOK_TIMEOUT; changed = True
                if h.get("statusMessage") != status_message:
                    h["statusMessage"] = status_message; changed = True
                return changed
    arr.append({
        "hooks": [{
            "type": "command",
            "command": command,
            "timeout": HOOK_TIMEOUT,
            "statusMessage": status_message,
        }]
    })
    return True

changed_ups = ensure("UserPromptSubmit", ups, USERPROMPT_STATUS)
changed_stop = ensure("Stop", stop, STOP_STATUS)
# No matcher: SubagentStop matchers filter on agent type, and adversarial verification should cover
# every subagent type, including custom and plugin-scoped ones.
changed_substop = ensure("SubagentStop", substop, SUBAGENT_STOP_STATUS)

if not (changed_ups or changed_stop or changed_substop):
    print("settings.json already has the Codex Fusion hooks; nothing to change.")
    sys.exit(0)

# Back up the exact pre-change file, then swap atomically (temp + os.replace) so an interrupted
# write can never leave settings.json truncated.
d_name = os.path.dirname(settings) or "."
os.makedirs(d_name, exist_ok=True)
if orig_text is not None:
    shutil.copy2(settings, settings + ".codex-fusion.bak")
fd, tmp = tempfile.mkstemp(dir=d_name, prefix=".settings.", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, settings)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise

print(
    f"settings.json merged (UserPromptSubmit updated: {changed_ups}, Stop updated: {changed_stop}, "
    f"SubagentStop updated: {changed_substop})"
)
PY

echo
echo "Done. Restart Claude Code (or reload the window), then run /hooks to confirm."
echo "      /hooks should list UserPromptSubmit, Stop, and SubagentStop. If your Claude Code build"
echo "      does not show SubagentStop, upgrade it: the other two hooks keep working regardless."
