#!/usr/bin/env python3
"""Test harness: fixture-backed `gh`/`herdr`, and a pty screen reader.

Nothing here touches the network or a running Herdr server. `fake_env()` puts
stub `gh` and `herdr` executables on PATH that answer from tests/fixtures, so a
test sees the same payload shapes the real tools returned when the fixtures were
recorded, and an issue changing status on GitHub cannot break the suite.

`screen()` runs a curses program in a pty and reconstructs what a terminal would
actually show. Two details matter and were both got wrong first time round:

  * the pty needs its window size set (TIOCSWINSZ) -- without it curses guesses,
    and the footer lands on the wrong row;
  * cursor-forward/back escapes (CSI C/D/G) have to be honoured -- ignoring them
    shifts text sideways and invents wrapping bugs that are not there.

Targets /usr/bin/python3 (3.9.6 on this Mac): no `X | None` annotations, no
match statements, stdlib only.
"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
BIN = os.path.join(PLUGIN_ROOT, "bin")
FIXTURES = os.path.join(HERE, "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def fixture_text(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------- fake tools
# The stubs are generated rather than shipped as files so the fixture directory
# and interpreter are baked in, and so a test run cannot pick up a stale copy.

_GH_STUB = r'''#!/usr/bin/env python3
"""Stand-in for `gh`, answering from recorded fixtures."""
import json, os, sys

FIXTURES = %(fixtures)r
LOG = os.environ.get("GH_STUB_LOG")


def out(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        sys.stdout.write(fh.read())
    return 0


def main():
    argv = sys.argv[1:]
    if LOG:
        try:
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write(" ".join(argv[:3]) + "\n")
        except OSError:
            pass          # a stale log path must not take the stub down
    if os.environ.get("GH_STUB_FAIL"):
        sys.stderr.write("simulated gh failure\n")
        return 1

    joined = " ".join(argv)
    if argv[:2] == ["api", "user"]:
        sys.stdout.write("lars\n")
        return 0
    if argv[:1] == ["api"] and "issue-fields" in joined:
        return out("issue_fields.json")
    if argv[:2] == ["search", "prs"]:
        return out("search_prs.json")
    if argv[:2] == ["api", "graphql"]:
        # Route on markers unique to each query this plugin sends.
        query = ""
        for i, a in enumerate(argv):
            if a == "-f" and i + 1 < len(argv) and argv[i + 1].startswith("query="):
                query = argv[i + 1][len("query="):]
        if "search(query:" in query:
            return out("issue_search.json")
        if "closedByPullRequestsReferences" in query:
            return out("issue_detail.json")
        if "headRefName" in query:
            return out("pr_detail.json")
        if "issueFieldValues" in query and "issue(number:" in query:
            return out("issue_meta.json")
        if "pullRequest(number:" in query and "reviewDecision" in query:
            # One aliased request covering every PR row on screen.
            if query.count("pullRequest(number:") > 1 or query.lstrip().startswith("query {"):
                return out("review_decisions.json")
            return out("pr_meta.json")
    sys.stderr.write("gh stub: unhandled %%s\n" %% joined[:160])
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''

_HERDR_STUB = r'''#!/usr/bin/env python3
"""Stand-in for `herdr`. Reads come from a fixture; writes are recorded."""
import json, os, sys

FIXTURES = %(fixtures)r
CALLS = os.environ.get("HERDR_STUB_CALLS")


def main():
    argv = sys.argv[1:]
    if CALLS:
        try:
            with open(CALLS, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(argv) + "\n")
        except OSError:
            pass          # a stale log path must not take the stub down
    if argv[:2] == ["agent", "list"]:
        with open(os.path.join(FIXTURES, "agent_list.json"), "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        # Real herdr answers with one compact line, and callers parse the last
        # line only -- a pretty-printed fixture would be read as just "}".
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0
    if argv[:2] == ["pane", "report-metadata"]:
        sys.stdout.write(json.dumps({"result": {"type": "ok"}}) + "\n")
        return 0
    if argv[:2] == ["pane", "get"]:
        sys.stdout.write(json.dumps({"result": {"pane": {"pane_id": argv[2]}}}) + "\n")
        return 0
    if argv[:2] == ["agent", "start"]:
        # Fail the first N starts with a chosen error code, so the retry path
        # can be driven without a real herdr and without sleeping for real.
        code = os.environ.get("HERDR_STUB_START_FAIL")
        if code:
            times = int(os.environ.get("HERDR_STUB_START_FAIL_TIMES") or 0) or 10 ** 6
            counter = os.environ.get("HERDR_STUB_START_COUNT") or ""
            seen = 0
            try:
                with open(counter, "r", encoding="utf-8") as fh:
                    seen = int(fh.read() or 0)
            except (OSError, ValueError):
                seen = 0
            if seen < times:
                try:
                    with open(counter, "w", encoding="utf-8") as fh:
                        fh.write(str(seen + 1))
                except OSError:
                    pass
                sys.stdout.write(json.dumps({"error": {
                    "code": code,
                    "message": "agent target pane is not an available shell",
                }}) + "\n")
                return 1
    sys.stdout.write(json.dumps({"result": {"type": "ok"}}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def make_stubs(tmpdir):
    """Write the stub executables into tmpdir/bin and return that directory."""
    d = os.path.join(tmpdir, "bin")
    os.makedirs(d, exist_ok=True)
    for name, src in (("gh", _GH_STUB), ("herdr", _HERDR_STUB)):
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src % {"fixtures": FIXTURES})
        os.chmod(path, 0o755)
    return d


def fake_env(tmpdir, **extra):
    """Environment with the stubs on PATH and plugin dirs pointed at tmpdir."""
    stub_dir = make_stubs(tmpdir)
    env = dict(os.environ)
    # Drop any stub controls a previous suite left behind -- they point into a
    # tmpdir that has since been removed.
    for stale in ("GH_STUB_FAIL", "GH_STUB_LOG", "HERDR_STUB_CALLS",
                  "HERDR_WORKSPACE_ID", "HERDR_PANE_ID"):
        env.pop(stale, None)
        os.environ.pop(stale, None)
    env["PATH"] = stub_dir + os.pathsep + env.get("PATH", "")
    env["HERDR_BIN_PATH"] = os.path.join(stub_dir, "herdr")
    env["HERDR_PLUGIN_ROOT"] = PLUGIN_ROOT
    env["HERDR_PLUGIN_CONFIG_DIR"] = os.path.join(tmpdir, "config")
    env["HERDR_PLUGIN_STATE_DIR"] = os.path.join(tmpdir, "state")
    os.makedirs(env["HERDR_PLUGIN_CONFIG_DIR"], exist_ok=True)
    os.makedirs(env["HERDR_PLUGIN_STATE_DIR"], exist_ok=True)
    env.update(extra)
    return env


def write_config(env, cfg):
    path = os.path.join(env["HERDR_PLUGIN_CONFIG_DIR"], "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    return path


# The shipped example ships a placeholder org, because a real one in the
# defaults reads as "written for them, not you". The fixtures were recorded
# against this org, so tests pin it rather than inheriting the placeholder.
FIXTURE_ORG = "evaldnet"


def plugin_config():
    """The shipped example config, with the org the fixtures were recorded on."""
    with open(os.path.join(PLUGIN_ROOT, "config.example.json"), "r",
              encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["org"] = FIXTURE_ORG
    return cfg


def example_config():
    """The example exactly as shipped, placeholder org and all."""
    with open(os.path.join(PLUGIN_ROOT, "config.example.json"), "r",
              encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------- pty screen
_CSI = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z@])")
_OTHER_ESC = re.compile(r"\x1b[()][B0]|\x1b[=>78MZc]|\x1b\][^\x07]*\x07")


def screen(argv, env, cols=80, rows=40, seconds=12.0, keys=b"q", settle=2.0):
    """Run a curses program in a pty; return the final screen as a list of rows."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    pid, fd = pty.fork()
    if pid == 0:
        try:
            # Set the size on our own controlling terminal before exec: doing it
            # from the parent races with the child's initscr(), and a program
            # that reads the size once at startup then gets the pre-resize value.
            fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    def pump(limit):
        buf, start = b"", time.time()
        while time.time() - start < limit:
            r, _, _ = select.select([fd], [], [], 0.25)
            if not r:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        return buf

    data = pump(seconds)
    if keys:
        try:
            os.write(fd, keys)
        except OSError:
            pass
        data += pump(settle)
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass
    return replay(data.decode("utf-8", "replace"), cols, rows)


def replay(text, cols, rows):
    """Apply the escape sequences curses emits to a blank grid."""
    grid = [[" "] * cols for _ in range(rows)]
    row = col = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\x1b":
            m = _CSI.match(text, i)
            if m:
                nums = [int(x) for x in m.group(1).split(";") if x.isdigit()]
                cmd = m.group(2)
                step = nums[0] if nums else 1
                if cmd == "H" or cmd == "f":
                    row = (nums[0] - 1) if len(nums) > 0 else 0
                    col = (nums[1] - 1) if len(nums) > 1 else 0
                elif cmd == "J":
                    grid = [[" "] * cols for _ in range(rows)]
                elif cmd == "K":
                    # 0/absent: to end of line, 1: to start, 2: whole line.
                    mode = nums[0] if nums else 0
                    lo = 0 if mode in (1, 2) else col
                    hi = cols if mode in (0, 2) else col + 1
                    if 0 <= row < rows:
                        for x in range(max(0, lo), min(cols, hi)):
                            grid[row][x] = " "
                elif cmd == "X":
                    # ECH: erase N characters from the cursor, cursor unmoved.
                    if 0 <= row < rows:
                        for x in range(col, min(cols, col + step)):
                            grid[row][x] = " "
                elif cmd == "C":
                    col += step
                elif cmd == "D":
                    col -= step
                elif cmd == "A":
                    row -= step
                elif cmd == "B":
                    row += step
                elif cmd == "d":
                    # VPA: absolute row, column unchanged. ncurses reaches for
                    # this to place a footer, and ignoring it silently drops the
                    # footer onto whatever row the cursor happened to be on.
                    row = step - 1
                elif cmd == "e":
                    row += step
                elif cmd == "G" or cmd == "`":
                    col = step - 1
                i = m.end()
                continue
            m = _OTHER_ESC.match(text, i)
            if m:
                i = m.end()
                continue
            i += 1
            continue
        if ch == "\n":
            row += 1
            col = 0
        elif ch == "\r":
            col = 0
        elif ch == "\b":
            col -= 1
        else:
            if 0 <= row < rows and 0 <= col < cols:
                grid[row][col] = ch
            col += 1
        i += 1
    return [("".join(r)).rstrip() for r in grid]


def import_module(name):
    """Import one of the plugin's bin/ modules by name."""
    import importlib.util
    path = os.path.join(BIN, name + ".py")
    spec = importlib.util.spec_from_file_location("plugin_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plugin_" + name] = mod
    spec.loader.exec_module(mod)
    return mod
