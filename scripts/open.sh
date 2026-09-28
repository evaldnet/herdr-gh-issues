#!/bin/sh
set -eu
HERDR="${HERDR_BIN_PATH:-herdr}"
# Optional $1: a view id from config.json ("review", "unassigned", ...). The
# popup starts on that view instead of the first; `v` still cycles from there.
if [ $# -gt 0 ] && [ -n "$1" ]; then
  exec "$HERDR" plugin pane open \
    --plugin evaldnet.gh-issues --entrypoint issues \
    --placement popup --width 85% --height 80% --focus \
    --env "HERDR_GHI_VIEW=$1"
fi
exec "$HERDR" plugin pane open \
  --plugin evaldnet.gh-issues --entrypoint issues \
  --placement popup --width 85% --height 80% --focus
