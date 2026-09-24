#!/usr/bin/env python3
"""Open a URL in the user's browser, from inside a curses pane.

Not `webbrowser.open`: on a terminal-only host its fallback chain can launch a
text browser *in this terminal*, which takes the pane's screen with it. An
explicit GUI opener with its output discarded either works or reports that it
could not, and never draws over us.

Targets /usr/bin/python3 (3.9.6): no `X | None` annotations, stdlib only.
"""
from __future__ import annotations

import subprocess
import sys

# Tests override this to exercise the missing-opener path on any platform.
BROWSER = None


def browser_command():
    """macOS ships `open`; most Linux desktops ship `xdg-open`."""
    return BROWSER or ("open" if sys.platform == "darwin" else "xdg-open")


def open_url(url):
    """Return True if launched, False if the opener is missing, None if there
    was nothing to open -- callers need to tell those last two apart, or an
    empty URL reports a missing browser on a machine that plainly has one."""
    if not url:
        return None
    try:
        subprocess.Popen([browser_command(), url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True
