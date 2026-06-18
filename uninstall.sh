#!/usr/bin/env bash
# Codex Fusion uninstaller.
# Removes the Codex Fusion hook entries from ~/.claude/settings.json (backing it up first)
# and deletes the installed hook scripts and skill. Other hooks/settings are left intact.
set -euo pipefail

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SETTINGS="$CLAUDE_DIR/settings.json"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "ERROR: python3 is required." >&2; exit 1; }

if [ -f "$SETTINGS" ]; then
  CF_SETTINGS="$SETTINGS" "$PY" - <<'PY'
import json, os, shutil, sys
s = os.environ["CF_SETTINGS"]
try:
    with open(s) as f:
        d = json.load(f)
except Exception as e:
    print(f"WARNING: {s} is not valid JSON ({e}); leaving it untouched.", file=sys.stderr)
    sys.exit(0)
shutil.copy2(s, s + ".codex-fusion.bak")
hooks = d.get("hooks", {})

def strip(event):
    new = []
    for grp in hooks.get(event, []):
        kept = [h for h in grp.get("hooks", []) if "codex-fusion" not in (h.get("command") or "")]
        if kept:
            g = dict(grp); g["hooks"] = kept; new.append(g)
    if new:
        hooks[event] = new
    elif event in hooks:
        del hooks[event]

for e in ("UserPromptSubmit", "Stop"):
    strip(e)
if not hooks and "hooks" in d:
    del d["hooks"]

with open(s, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
print("Removed Codex Fusion hook entries from settings.json (backup: *.codex-fusion.bak)")
PY
fi

rm -f "$CLAUDE_DIR/hooks/codex-fusion-userprompt.sh" "$CLAUDE_DIR/hooks/codex-fusion-stop.sh"
rm -rf "$CLAUDE_DIR/skills/codex-fusion-auto"
echo "Removed hook scripts and skill. Restart Claude Code to apply."
