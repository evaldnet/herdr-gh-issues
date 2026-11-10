#!/usr/bin/env python3
"""Build gh-agents and gh-main: Herdr's agents list and main layout, cloned,
with the task status rows and the background watcher patched in.

The Herdr desktop app gives its shipped agents list no extension point and
replaces shipped mods on every start, so the status rows live in a clone, and
a clone of the main layout points its sidebar at it. Clones are frozen at the
version they were made from. Run this again after a Herdr update to rebuild
them from the current shipped files; gh-agents posts a toast when it is behind.

    sync_agents.py             build or rebuild both
    sync_agents.py --check     exit 1 if the clone is missing or behind Herdr
    sync_agents.py --dry-run   say what would happen, change nothing

The clone is made by the app itself (`mod.clone`, `layout.clone`), which knows
which self-references to rename. The patch is three anchored edits; if Herdr's
code no longer has an anchor, this stops and puts the previous clone back
rather than guessing.

Targets /usr/bin/python3 (3.9.6): stdlib only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_HOME = os.environ.get("HERDR_APP_HOME") or os.path.expanduser("~/.config/herdr-app")
MOD = "gh-agents"
LAYOUT = "gh-main"
STATUS_JS = os.path.join(REPO, "agents", "gh-status.js")
BACKUP_DIR = os.path.expanduser("~/.local/state/herdr-gh-issues")
SYNCED = ".synced.json"
# installed.json entries the clones are made from.
SOURCES = ("agents", "layout:main")

MARK = "// gh-issues"
# (anchor, replacement). Each anchor must occur exactly once in the shipped
# agents/main.js. Kept small so a Herdr update breaks loudly, not silently.
PATCHES = [
    (
        "    this.migratingPins = new Set();\n",
        "    this.migratingPins = new Set();\n"
        "    ghStatus(cx); %s: task status + background watcher\n" % MARK,
    ),
    (
        "      const r = row(a, i, subs);\n"
        "      if (this.folded.has(a.address)) return [r];\n"
        "      return [r, ...subs.map((s) => this.subRow(t, s, shown, byPriority))];\n",
        "      const r = row(a, i, subs);\n"
        "      const gh = collapsed ? [] : ghStatusRow(t, a); %s\n"
        "      if (this.folded.has(a.address)) return [r, ...gh];\n"
        "      return [r, ...gh, ...subs.map((s) => this.subRow(t, s, shown, byPriority))];\n" % MARK,
    ),
]
IMPORT = 'import { ghStatus, ghStatusRow } from "./gh-status.js"; %s\n' % MARK


class SyncError(Exception):
    pass


def patch_source(src):
    """Shipped agents/main.js -> patched. Raises SyncError on a missing anchor."""
    if MARK in src:
        raise SyncError("already patched: expected Herdr's own agents/main.js")
    for anchor, repl in PATCHES:
        n = src.count(anchor)
        if n != 1:
            raise SyncError("anchor found %d times (want 1):\n%s" % (n, anchor))
        src = src.replace(anchor, repl)
    # The import goes after the last import line, whichever that is.
    lines = src.splitlines(True)
    last = max((i for i, l in enumerate(lines) if l.startswith("import ")), default=None)
    if last is None:
        raise SyncError("no import line to put the gh-status import after")
    lines.insert(last + 1, IMPORT)
    return "".join(lines)


def swap_agents(node):
    """Point every `{"mod": "agents"}` block of a layout tree at the clone."""
    count = 0
    if isinstance(node, dict):
        if node.get("mod") == "agents":
            node["mod"] = MOD
            count += 1
        for v in node.values():
            count += swap_agents(v)
    elif isinstance(node, list):
        for v in node:
            count += swap_agents(v)
    return count


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def app(*args):
    """Run herdr-app; return its stdout or raise with what it said."""
    p = subprocess.run(["herdr-app"] + list(args), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    if p.returncode != 0 or '"error"' in p.stdout:
        raise SyncError("herdr-app %s: %s" % (" ".join(args), (p.stdout + p.stderr).strip()[:300]))
    return p.stdout


def wanted():
    installed = read_json(os.path.join(APP_HOME, "installed.json"))
    return {k: installed.get(k) for k in SOURCES}


def check():
    mod_dir = os.path.join(APP_HOME, "mods", MOD)
    try:
        synced = read_json(os.path.join(mod_dir, SYNCED))
    except (OSError, ValueError):
        print("%s: not built" % MOD)
        return 1
    behind = [k for k in SOURCES if synced.get(k) != wanted()[k]]
    if behind:
        print("%s: behind Herdr's %s" % (MOD, ", ".join(behind)))
        return 1
    print("%s: current" % MOD)
    return 0


def sync(dry_run):
    mods = os.path.join(APP_HOME, "mods")
    mod_dir = os.path.join(mods, MOD)
    shipped = os.path.join(mods, "agents", "main.js")
    with open(shipped, "r", encoding="utf-8") as fh:
        patch_source(fh.read())  # fail before touching anything
    layout_path = os.path.join(APP_HOME, "layouts", LAYOUT + ".json")
    if dry_run:
        print("would %s %s from Herdr's agents, patch it, copy %s"
              % ("rebuild" if os.path.isdir(mod_dir) else "clone", MOD, STATUS_JS))
        print("would %s %s from Herdr's main layout, sidebar -> %s"
              % ("rebuild" if os.path.exists(layout_path) else "clone (and switch to)", LAYOUT, MOD))
        return 0

    backup = None
    if os.path.isdir(mod_dir):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup = os.path.join(BACKUP_DIR, "%s.prev-%d" % (MOD, int(time.time())))
        shutil.move(mod_dir, backup)
    try:
        app("do", "mod.clone", "--name", "agents", "--as", MOD)
        main_js = os.path.join(mod_dir, "main.js")
        with open(main_js, "r", encoding="utf-8") as fh:
            src = patch_source(fh.read())
        shutil.copyfile(STATUS_JS, os.path.join(mod_dir, "gh-status.js"))
        with open(main_js, "w", encoding="utf-8") as fh:
            fh.write(src)
        app("check", mod_dir)
        write_json(os.path.join(mod_dir, SYNCED), wanted())
    except Exception:
        if os.path.isdir(mod_dir):
            shutil.rmtree(mod_dir)
        if backup:
            shutil.move(backup, mod_dir)
            print("restored the previous %s" % MOD, file=sys.stderr)
        raise
    print("%s: cloned from Herdr's agents and patched%s"
          % (MOD, " (previous kept at %s)" % backup if backup else ""))

    if not os.path.exists(layout_path):
        # The app's clone copies main's clicks (sidebar width, ...) and switches to it.
        app("do", "layout.clone", "--name", "main", "--as", LAYOUT)
        tree = read_json(layout_path)
    else:
        # Rebuild from the current main; clicks live in layouts/.state and stay.
        old = read_json(layout_path)
        tree = read_json(os.path.join(APP_HOME, "layouts", "main.json"))
        for key in ("cloned_from",):
            if key in old:
                tree[key] = old[key]
    if swap_agents(tree) != 1:
        raise SyncError("expected one agents block in the main layout")
    write_json(layout_path, tree)
    print("%s: sidebar shows %s" % (LAYOUT, MOD))
    return 0


def main():
    args = sys.argv[1:]
    try:
        if "--check" in args:
            return check()
        return sync("--dry-run" in args)
    except SyncError as exc:
        print("sync_agents: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
