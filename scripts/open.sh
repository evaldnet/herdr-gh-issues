#!/bin/sh
set -eu
HERDR="${HERDR_BIN_PATH:-herdr}"
exec "$HERDR" plugin pane open \
  --plugin evaldnet.gh-issues --entrypoint issues \
  --placement popup --width 85% --height 80% --focus
