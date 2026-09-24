#!/usr/bin/env python3
"""Publish GitHub task info for the issue a Herdr pane was opened for.

Runs as a plugin event hook (pane.focused / pane.agent_status_changed). Resolves
the pane to the issue or PR its space was created from, reads that item's live
state, and reports it as Herdr sidebar metadata tokens:

    issue         platform#122
    issue_status  In Development        (PRs: approved / changes req / review req)
    issue_priority First
    issue_type    Bugfix
    issue_labels  module:kunde, module:integration

The tokens only become visible once the user puts $issue_status (etc.) in a
[ui.sidebar.agents] row -- Herdr stores them either way.

Events only fire on focus and agent-state changes, so a task that moves on
GitHub while you watch one pane would never repaint. `--watch` closes that gap:
a detached loop (spawned by the startup hook) re-fetches every issue space each
`watch_seconds`, and any fetch that sees the status move pops a notification.

Read-only, like the rest of this plugin: it never writes to GitHub.

Targets /usr/bin/python3 (3.9.6 on this Mac): no `X | None` annotations, no
match statements, stdlib only.
"""
from __future__ import annotations

import json
import os
import signal
import re
import subprocess
import sys
import time

SOURCE = "evaldnet.gh-issues"
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
PLUGIN_ROOT = os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(
    os.path.expanduser("~/.config/herdr/plugins/config"), "evaldnet.gh-issues"
)
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
    "~/.local/state/herdr/plugins/evaldnet.gh-issues"
)
# launch.py drops one of these per space it creates, keyed by the agent name it
# assigned. Exact; the name-parsing fallback below covers spaces that predate it.
AGENT_MAP_DIR = os.path.join(STATE_DIR, "agents")
ISSUE_CACHE_DIR = os.path.join(STATE_DIR, "issue-cache")
# Held (flock) by the running watcher and holding its pid, so a second spawn
# can replace it instead of doubling the GitHub calls.
WATCH_LOCK = os.path.join(STATE_DIR, "watch.lock")
# Consecutive failed `herdr agent list` calls before the watcher decides the
# server is gone and exits, rather than polling a dead socket forever.
WATCH_MAX_MISSES = 3
WATCH_ARGV_TAIL = "%s --watch" % os.path.abspath(__file__)

DEFAULTS = {
    # No default org: querying somebody else's is worse than
    # saying nothing. load_view() reports it as a setup error.
    "org": "",
    # Single-select field shown as $issue_status. Same field the panel columns use.
    "status_field": "Status",
    # Single-select field shown as $issue_priority. Empty string turns it off.
    "priority_field": "Priority",
    # Seconds a fetched issue stays good. pane.focused fires on every focus
    # change, so without this a busy session would refetch constantly.
    "meta_ttl_seconds": 180,
    # Strip GitHub's issue-type emoji ("🕷️ Bugfix" -> "Bugfix"): it eats two
    # columns of a narrow sidebar and the word already carries the meaning.
    "issue_type_emoji": False,
    # Seconds between background re-fetches of every issue space, so a task
    # moving on GitHub repaints without a focus change. 0 turns the watcher off.
    "watch_seconds": 120,
    # Pop a Herdr notification when a fetch sees a task's status change.
    "notify_status_change": True,
}

# Herdr normalises token values anyway (trim, strip control chars, cap at 80);
# keeping our own budget smaller keeps a sidebar row from wrapping awkwardly.
VALUE_MAX = 48

ISSUE_QUERY = """
query($owner:String!,$name:String!,$num:Int!){
  repository(owner:$owner,name:$name){
    issue(number:$num){
      number title state
      labels(first:10){nodes{name}}
      issueType{name}
      issueFieldValues(first:20){nodes{
        ... on IssueFieldSingleSelectValue{
          value field{... on IssueFieldSingleSelect{name}}}
        ... on IssueFieldDateValue{
          value field{... on IssueFieldDate{name}}}
      }}
    }
  }
}
"""

PR_QUERY = """
query($owner:String!,$name:String!,$num:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$num){
      number title state isDraft reviewDecision
      labels(first:10){nodes{name}}
    }
  }
}
"""

REVIEW_TEXT = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes req",
    "REVIEW_REQUIRED": "review req",
}

# gh-<slug>-<number> / pr-<slug>-<number>, as minted by launch.py:agent_name().
AGENT_NAME_RE = re.compile(r"^(gh|pr)-(.+)-(\d+)$")


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(CONFIG_DIR, "config.json"), "r", encoding="utf-8") as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            cfg.update(user)
    except Exception:
        pass
    return cfg


def norm_field(name):
    return re.sub(r"[\s_-]+", " ", (name or "").strip().lower())


def run_json(args, timeout=20):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw.splitlines()[-1])
    except ValueError:
        return None


def agent_for_pane(pane_id):
    """Return the agent record for this pane, or None."""
    env = run_json([HERDR, "agent", "list"])
    if not isinstance(env, dict):
        return None
    for a in ((env.get("result") or {}).get("agents") or []):
        if a.get("pane_id") == pane_id:
            return a
    return None


def resolve_item(cfg, agent):
    """Map an agent to {repo, number, is_pr}, or None when it is not ours."""
    name = (agent or {}).get("name") or ""
    if not name:
        return None
    # Exact: the record launch.py wrote when it created the space.
    path = os.path.join(AGENT_MAP_DIR, "%s.json" % name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            rec = json.load(fh)
        if rec.get("repo") and rec.get("number"):
            return {"repo": rec["repo"], "number": int(rec["number"]),
                    "is_pr": bool(rec.get("is_pr"))}
    except Exception:
        pass
    # Fallback: recover it from the agent name. Spaces opened before this hook
    # existed have no record, and the slug is the repo's short name.
    m = AGENT_NAME_RE.match(name)
    if not m:
        return None
    kind, slug, number = m.group(1), m.group(2), int(m.group(3))
    org = cfg.get("org") or ""
    if not org:
        return None
    return {"repo": "%s/%s" % (org, slug), "number": number, "is_pr": kind == "pr"}


def cache_path(item):
    key = "%s-%s-%d" % ("pr" if item["is_pr"] else "gh",
                        re.sub(r"[^A-Za-z0-9_.-]+", "-", item["repo"]), item["number"])
    return os.path.join(ISSUE_CACHE_DIR, key + ".json")


def read_cache(item, ttl):
    try:
        with open(cache_path(item), "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        if time.time() - float(blob.get("fetched_at", 0)) < ttl:
            return blob.get("data")
    except Exception:
        pass
    return None


def write_cache(item, data):
    try:
        os.makedirs(ISSUE_CACHE_DIR, exist_ok=True)
        tmp = cache_path(item) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": time.time(), "data": data}, fh)
        os.replace(tmp, cache_path(item))
    except Exception:
        pass


def fetch_item(item):
    owner, _, name = item["repo"].partition("/")
    if not owner or not name:
        return None
    query = PR_QUERY if item["is_pr"] else ISSUE_QUERY
    args = ["gh", "api", "graphql", "-f", "query=%s" % query,
            "-f", "owner=%s" % owner, "-f", "name=%s" % name,
            "-F", "num=%d" % item["number"]]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=45)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    if payload.get("errors"):
        return None
    repo = ((payload.get("data") or {}).get("repository") or {})
    return repo.get("pullRequest") if item["is_pr"] else repo.get("issue")


def clip(text, width=VALUE_MAX):
    text = (text or "").strip()
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def field_value(data, name):
    """Clipped value of the issue field called `name`, or None."""
    wanted = norm_field(name)
    if not wanted:
        return None
    for node in ((data.get("issueFieldValues") or {}).get("nodes") or []):
        field = ((node or {}).get("field") or {}).get("name")
        if field and norm_field(field) == wanted:
            return clip(node.get("value") or "") or None
    return None


def tokens_for(cfg, item, data):
    """Build the token map. A value of None clears that token in Herdr."""
    short = item["repo"].split("/")[-1]
    marker = "!" if item["is_pr"] else "#"
    out = {
        "issue": "%s%s%d" % (short, marker, item["number"]),
        "issue_status": None,
        "issue_priority": None,
        "issue_type": None,
        "issue_labels": None,
    }
    if not data:
        return out

    labels = [n.get("name", "") for n in ((data.get("labels") or {}).get("nodes") or [])]
    out["issue_labels"] = clip(", ".join(l for l in labels if l)) or None

    if item["is_pr"]:
        if data.get("isDraft"):
            out["issue_status"] = "draft"
        else:
            out["issue_status"] = REVIEW_TEXT.get(data.get("reviewDecision") or "") or None
        return out

    itype = ((data.get("issueType") or {}).get("name") or "").strip()
    if itype and not cfg.get("issue_type_emoji"):
        # GitHub prefixes the org's types with an emoji; drop the leading
        # non-word run rather than a fixed slice, since the emoji varies.
        itype = re.sub(r"^[^\w]+", "", itype).strip()
    out["issue_type"] = clip(itype) or None

    out["issue_status"] = field_value(data, cfg.get("status_field") or "Status")
    out["issue_priority"] = field_value(data, cfg.get("priority_field"))
    # A closed issue outranks whatever column it was parked in.
    if (data.get("state") or "").upper() == "CLOSED":
        out["issue_status"] = "closed"
    return out


def report(pane_id, tokens):
    args = [HERDR, "pane", "report-metadata", pane_id, "--source", SOURCE]
    for name, value in sorted(tokens.items()):
        if value is None:
            args += ["--clear-token", name]
        else:
            args += ["--token", "%s=%s" % (name, value)]
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=10)
    except Exception:
        pass


def status_change(cfg, item, old, new):
    """(before, after) when the status token moved between two fetches, else None."""
    if not old or not new:
        return None
    before = tokens_for(cfg, item, old)["issue_status"]
    after = tokens_for(cfg, item, new)["issue_status"]
    if before == after:
        return None
    return before, after


def notify_change(cfg, item, old, new):
    if not cfg.get("notify_status_change", True):
        return
    change = status_change(cfg, item, old, new)
    if not change:
        return
    title = "%s: %s → %s" % (tokens_for(cfg, item, new)["issue"],
                             change[0] or "none", change[1] or "none")
    args = [HERDR, "notification", "show", title, "--sound", "request"]
    if new.get("title"):
        args += ["--body", clip(new["title"], 80)]
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=10)
    except Exception:
        pass


def update_pane(cfg, pane_id, agent=None, ttl=None):
    agent = agent or agent_for_pane(pane_id)
    item = resolve_item(cfg, agent)
    if not item:
        # Not an issue space. Leave other sources' tokens alone; only clear ours
        # so a renamed or reused pane does not keep showing a stale issue.
        report(pane_id, {"issue": None, "issue_status": None, "issue_priority": None,
                         "issue_type": None, "issue_labels": None})
        return False
    if ttl is None:
        try:
            ttl = max(0, int(cfg.get("meta_ttl_seconds", 180)))
        except (TypeError, ValueError):
            ttl = 180
    data = read_cache(item, ttl)
    if data is None:
        previous = read_cache(item, float("inf"))
        data = fetch_item(item)
        if data is not None:
            write_cache(item, data)
            # Only a real fetch can notify. Two panes on one issue share the
            # cache, so the second one hits it and stays quiet.
            notify_change(cfg, item, previous, data)
        else:
            # Fetch failed: keep whatever is on the pane rather than blanking it.
            if previous is None:
                return False
            data = previous
    report(pane_id, tokens_for(cfg, item, data))
    return True


def update_all(cfg, ttl=None):
    """Refresh every agent pane. Returns the count updated, or None when
    Herdr did not answer."""
    env = run_json([HERDR, "agent", "list"])
    if not isinstance(env, dict) or "result" not in env:
        return None
    n = 0
    for a in ((env.get("result") or {}).get("agents") or []):
        if a.get("pane_id") and update_pane(cfg, a["pane_id"], a, ttl=ttl):
            n += 1
    return n


def watch_interval(cfg):
    try:
        return max(0, int(cfg.get("watch_seconds", 120)))
    except (TypeError, ValueError):
        return 120


def running_watcher():
    """Pid of the live watcher, or None. The pid file alone is not trusted:
    a recycled pid must not get a SIGTERM meant for us."""
    try:
        with open(WATCH_LOCK, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    try:
        proc = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                              capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    # The exact argv spawn_watcher() uses; a looser match once hit a shell
    # whose -c script merely mentioned this file.
    return pid if (proc.stdout or "").strip().endswith(WATCH_ARGV_TAIL) else None


def stop_watcher():
    pid = running_watcher()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def spawn_watcher(cfg):
    """(Re)start the watcher. Replacing rather than keeping an old one means
    a plugin update or a live handoff always ends up running current code."""
    stop_watcher()
    if watch_interval(cfg) <= 0:
        return False
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--watch"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         close_fds=True)
    except Exception:
        return False
    return True


def watch():
    import fcntl

    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(WATCH_LOCK, "a+", encoding="utf-8")
    # The watcher being replaced may take a moment to let go after SIGTERM.
    for _ in range(50):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            time.sleep(0.1)
    else:
        return 0          # another watcher owns it; one is enough
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()

    misses = 0
    while True:
        cfg = load_config()          # re-read, so watch_seconds edits apply live
        interval = watch_interval(cfg)
        if interval <= 0:
            return 0
        time.sleep(interval)
        # Anything fetched in the last half interval (a focus, say) is fresh
        # enough; everything older is refetched this round.
        if update_all(cfg, ttl=interval // 2) is None:
            misses += 1
            if misses >= WATCH_MAX_MISSES:
                return 0
        else:
            misses = 0


def main():
    cfg = load_config()
    pane_id = os.environ.get("HERDR_PANE_ID")
    argv = sys.argv[1:]

    if "--watch" in argv:
        return watch()

    # Startup / manual re-seed: tokens do not survive a server restart, so walk
    # every agent pane rather than waiting for each to be focused once. Also
    # (re)starts the watcher, so the refresh action doubles as its restart.
    if "--all" in argv or os.environ.get("HERDR_PLUGIN_EVENT") == "startup":
        n = update_all(cfg) or 0
        sys.stdout.write("gh-issues: seeded %d issue pane(s)\n" % n)
        if "--no-watch" not in argv:
            watching = spawn_watcher(cfg)
            sys.stdout.write("gh-issues: watcher %s\n" % (
                "every %ds" % watch_interval(cfg) if watching else "off"))
        return 0

    if not pane_id:
        sys.stderr.write("gh-issues: no HERDR_PANE_ID\n")
        return 0
    update_pane(cfg, pane_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
