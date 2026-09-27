#!/usr/bin/env python3
"""Tell the panel when a newer release of this plugin exists.

Herdr has no `plugin update`: a GitHub install is pinned to the commit it was
cloned at, and reinstalling is the only way forward. Nothing tells you a
reinstall is due, so this compares the manifest's `version` with the latest
GitHub release and lets the panel say so.

At most one `gh api` call per `update_check_hours`, made on a daemon thread
while the issue list loads, so the popup never waits on it. The answer is
cached in the state dir; a failed check (offline, rate limit, no auth) keeps
the previous answer and is retried on the next interval, never surfaced --
an update hint is not worth an error line.

Targets /usr/bin/python3 (3.9.6): stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time

CACHE_NAME = "update_check.json"
_VERSION_LINE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.M)


def installed_version(plugin_root):
    try:
        with open(os.path.join(plugin_root, "herdr-plugin.toml"), "r",
                  encoding="utf-8") as fh:
            hit = _VERSION_LINE.search(fh.read())
    except OSError:
        return ""
    return hit.group(1) if hit else ""


def parse_version(text):
    """"v1.5.0" -> (1, 5, 0). Anything unparseable -> () which never wins."""
    hit = re.match(r"^v?(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(p) for p in hit.group(1).split(".")) if hit else ()


def _cache_path(state_dir):
    return os.path.join(state_dir, CACHE_NAME)


def read_cache(state_dir):
    try:
        with open(_cache_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def fetch_latest(repo):
    """Latest release tag of `repo`, or "" if gh cannot tell us."""
    try:
        proc = subprocess.run(
            ["gh", "api", "repos/%s/releases/latest" % repo, "--jq", ".tag_name"],
            capture_output=True, text=True, timeout=20)
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def refresh(state_dir, repo, ttl_seconds, now=None):
    """Refetch if the cache is older than ttl. Returns the cache as it stands."""
    now = time.time() if now is None else now
    cache = read_cache(state_dir)
    if cache.get("repo") == repo and now - float(cache.get("checked") or 0) < ttl_seconds:
        return cache
    latest = fetch_latest(repo)
    # Stamp the attempt even when it failed, so an offline machine does not
    # retry on every popup open; keep the last good answer for the notice.
    cache = {"repo": repo, "checked": now,
             "latest": latest or (cache.get("latest") if cache.get("repo") == repo else "")}
    try:
        os.makedirs(state_dir, exist_ok=True)
        tmp = _cache_path(state_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, _cache_path(state_dir))
    except OSError:
        pass
    return cache


def newer_release(state_dir, current):
    """The cached latest version if it is newer than `current`, else ""."""
    latest = (read_cache(state_dir).get("latest") or "").lstrip("v")
    mine = parse_version(current)
    if not mine or parse_version(latest) <= mine:
        return ""
    return latest


def start(cfg, state_dir):
    """Kick off the check in the background; returns the thread, or None when off."""
    if not cfg.get("update_check", True):
        return None
    repo = cfg.get("update_repo") or ""
    if not repo:
        return None
    ttl = float(cfg.get("update_check_hours", 24)) * 3600
    thread = threading.Thread(target=refresh, args=(state_dir, repo, ttl), daemon=True)
    thread.start()
    return thread


def notice(latest, current, repo):
    return "%s available (you have %s) — herdr plugin install %s" % (latest, current, repo)
