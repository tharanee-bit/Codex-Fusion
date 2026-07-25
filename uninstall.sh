#!/usr/bin/env bash
# Codex Fusion uninstaller.
# Removes the Codex Fusion UserPromptSubmit / Stop / SubagentStop hook entries from
# ~/.claude/settings.json (backing it up first)
# and deletes the installed hook scripts and skill. Other hooks/settings are left intact.
set -euo pipefail

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SETTINGS="$CLAUDE_DIR/settings.json"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "ERROR: python3 is required." >&2; exit 1; }

if [ -f "$SETTINGS" ]; then
  CF_SETTINGS="$SETTINGS" "$PY" - <<'PY'
import json, os, shutil, sys, tempfile
# realpath: keep a symlinked settings.json a symlink (atomic replace must land on the target).
s = os.path.realpath(os.environ["CF_SETTINGS"])
try:
    with open(s) as f:
        d = json.load(f)
except Exception as e:
    print(f"WARNING: {s} is not valid JSON ({e}); leaving it untouched.", file=sys.stderr)
    sys.exit(0)
hooks = d.get("hooks", {})
removed = 0

# Match our own scripts by exact basename, not by a bare "codex-fusion" substring: a user hook named
# something like my-codex-fusion-wrapper.sh must survive an uninstall.
CF_SCRIPTS = (
    "codex-fusion-userprompt.sh",
    "codex-fusion-stop.sh",
    "codex-fusion-subagent-stop.sh",
    "codex-fusion-common.sh",
)

def is_codex_fusion(command):
    c = command or ""
    return any(c == s or c.endswith("/" + s) or ("/" + s) in c for s in CF_SCRIPTS)

def strip(event):
    global removed
    new = []
    for grp in hooks.get(event, []):
        kept = [h for h in grp.get("hooks", []) if not is_codex_fusion(h.get("command"))]
        removed += len(grp.get("hooks", [])) - len(kept)
        if kept:
            g = dict(grp); g["hooks"] = kept; new.append(g)
    if new:
        hooks[event] = new
    elif event in hooks:
        del hooks[event]

for e in ("UserPromptSubmit", "Stop", "SubagentStop"):
    strip(e)
if not hooks and "hooks" in d:
    del d["hooks"]

if removed == 0:
    print("No Codex Fusion hook entries found; settings.json left untouched.")
    sys.exit(0)

# Back up, then swap atomically so an interrupted write can never leave settings.json truncated.
shutil.copy2(s, s + ".codex-fusion.bak")
d_name = os.path.dirname(s) or "."
fd, tmp = tempfile.mkstemp(dir=d_name, prefix=".settings.", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, s)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print("Removed Codex Fusion hook entries from settings.json (backup: *.codex-fusion.bak)")
PY
fi

rm -f "$CLAUDE_DIR/hooks/codex-fusion-common.sh" "$CLAUDE_DIR/hooks/codex-fusion-userprompt.sh" \
      "$CLAUDE_DIR/hooks/codex-fusion-stop.sh" "$CLAUDE_DIR/hooks/codex-fusion-subagent-stop.sh"
rm -rf "$CLAUDE_DIR/skills/codex-fusion-auto"
echo "Removed hook scripts and skill. Restart Claude Code to apply."
