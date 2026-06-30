#!/usr/bin/env bash
# Codex Fusion installer.
# Copies the hook scripts + skill into ~/.claude and merges the UserPromptSubmit + Stop
# hooks into ~/.claude/settings.json non-destructively and idempotently.
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
install -m 0644 "$HERE/skills/codex-fusion-auto/SKILL.md" "$SKILLS_DIR/codex-fusion-auto/SKILL.md"
echo "Installed hooks + skill into $CLAUDE_DIR"

CF_UPS="$HOOKS_DIR/codex-fusion-userprompt.sh" \
CF_STOP="$HOOKS_DIR/codex-fusion-stop.sh" \
CF_SETTINGS="$SETTINGS" "$PY" - <<'PY'
import json, os, sys, shutil

settings = os.environ["CF_SETTINGS"]
ups, stop = os.environ["CF_UPS"], os.environ["CF_STOP"]

data = {}
if os.path.exists(settings):
    try:
        with open(settings) as f:
            data = json.load(f)
    except Exception as e:
        print(f"ERROR: {settings} is not valid JSON ({e}); aborting so it isn't clobbered.", file=sys.stderr)
        sys.exit(1)
    shutil.copy2(settings, settings + ".codex-fusion.bak")

if not isinstance(data, dict):
    print("ERROR: settings.json is not a JSON object; aborting.", file=sys.stderr); sys.exit(1)

hooks = data.setdefault("hooks", {})

# Hook registration timeout. Keep in sync with settings.snippet.json and comfortably above the
# hooks' internal CODEX_TIMEOUT (180s by default) so xhigh Codex calls finish instead of being killed.
HOOK_TIMEOUT = 270
USERPROMPT_STATUS = "Codex Fusion: checking Codex..."
STOP_STATUS = "Codex Fusion: reviewing changes..."

def ensure(event, command, status_message):
    arr = hooks.setdefault(event, [])
    for grp in arr:
        for h in grp.get("hooks", []):
            if h.get("command") == command:
                # Already present: converge its config to the current value on re-run/upgrade.
                h["type"] = "command"
                h["timeout"] = HOOK_TIMEOUT
                h["statusMessage"] = status_message
                return False
    arr.append({
        "hooks": [{
            "type": "command",
            "command": command,
            "timeout": HOOK_TIMEOUT,
            "statusMessage": status_message,
        }]
    })
    return True

added_ups = ensure("UserPromptSubmit", ups, USERPROMPT_STATUS)
added_stop = ensure("Stop", stop, STOP_STATUS)

os.makedirs(os.path.dirname(settings), exist_ok=True)
with open(settings, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")

print(f"settings.json merged (UserPromptSubmit added: {added_ups}, Stop added: {added_stop})")
PY

echo
echo "Done. Restart Claude Code (or reload the window), then run /hooks to confirm."
