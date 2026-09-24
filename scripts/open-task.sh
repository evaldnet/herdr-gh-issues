#!/bin/sh
# Action: open the task detail pane for this space, or focus it if already up.
#
# The duplicate check asks the OS rather than a state file. Herdr renumbers
# workspace ids when spaces come and go, so a file keyed by workspace goes stale
# silently; a running pane process carries its own HERDR_WORKSPACE_ID and
# HERDR_PANE_ID in its environment, which cannot drift out of sync with itself.
set -eu
HERDR="${HERDR_BIN_PATH:-herdr}"
WS="${HERDR_WORKSPACE_ID:-}"
CONFIG_DIR="${HERDR_PLUGIN_CONFIG_DIR:-$HOME/.config/herdr/plugins/config/evaldnet.gh-issues}"

if [ -n "$WS" ]; then
  for PID in $(pgrep -f 'bin/task_pane\.py' 2>/dev/null || true); do
    ENVDUMP=$(ps eww -p "$PID" 2>/dev/null | tr ' ' '\n' || true)
    # Exact line, not a substring: workspace ids are short and grow by width,
    # so a glob would let wBB match wBBA and focus the wrong space's pane.
    printf '%s\n' "$ENVDUMP" | grep -qxF "HERDR_WORKSPACE_ID=$WS" || continue
    PANE=$(printf '%s\n' "$ENVDUMP" | sed -n 's/^HERDR_PANE_ID=//p' | head -1)
    # `pane focus` is directional (--direction left|right|up|down); the one that
    # takes a pane id is `plugin pane focus`.
    if [ -n "$PANE" ] && "$HERDR" pane get "$PANE" >/dev/null 2>&1; then
      exec "$HERDR" plugin pane focus "$PANE"
    fi
  done
fi

DIRECTION=$(/usr/bin/python3 - "$CONFIG_DIR/config.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        v = (json.load(fh) or {}).get("split_direction") or ""
except Exception:
    v = ""
print(v if v in ("right", "down") else "right")
PY
)
[ -n "$DIRECTION" ] || DIRECTION=right

exec "$HERDR" plugin pane open \
  --plugin evaldnet.gh-issues --entrypoint task \
  --placement split --direction "$DIRECTION" --no-focus
