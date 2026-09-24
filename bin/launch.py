#!/usr/bin/env python3
"""Open a Herdr space for a GitHub issue and brief Claude in it.

Runs detached from the panel popup. Sequence:
  1. wait for the popup to exit (barrier pipe EOF)
  2. close the popup
  3. read the issue with gh
  4. herdr workspace create  -> root pane
  5. herdr agent start --kind claude
  6. herdr agent prompt <brief>

Targets /usr/bin/python3 (3.9.6): no `X | None` annotations, stdlib only.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import socket
import subprocess
import sys
import time

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(
    os.path.expanduser("~/.config/herdr/plugins/config"), "evaldnet.gh-issues"
)

DEFAULTS = {
    # No default org: querying somebody else's is worse than
    # saying nothing. load_view() reports it as a setup error.
    "org": "",
    "space_cwd": "~/Projects",
    "agent_kind": "claude",
    "body_limit": 6000,
    # How long to keep retrying `agent start` while the freshly created pane is
    # still coming up. See start_agent().
    "agent_start_retry_seconds": 20,
    # Directory name under space_cwd is the repo's short name. Override per repo
    # when the checkout directory does not match -- a repo renamed upstream, or
    # two generations of the same project side by side. Empty by default: a
    # mapping you did not ask for would brief Claude on the wrong working copy.
    "repo_paths": {},
}


START = time.time()


def log(msg):
    """Stamped with wall clock and seconds since launch.

    Without the elapsed column there is no way to tell a step that failed
    instantly from one that burned its whole timeout first, and those two want
    opposite fixes.
    """
    sys.stderr.write("%s +%5.1fs  %s\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), time.time() - START, msg))
    sys.stderr.flush()


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(CONFIG_DIR, "config.json"), "r", encoding="utf-8") as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            merged_paths = dict(DEFAULTS["repo_paths"])
            merged_paths.update(user.get("repo_paths") or {})
            cfg.update(user)
            cfg["repo_paths"] = merged_paths
    except Exception:
        pass
    return cfg


def wait_for_popup_exit():
    """Block until the panel process closes the write end of the barrier pipe."""
    fd = os.environ.get("HERDR_GHI_BARRIER_FD")
    if not fd:
        return
    try:
        with io.open(int(fd), "rb", closefd=True) as barrier:
            while barrier.read(4096):
                pass
    except Exception as exc:
        log("barrier wait failed (continuing): %s" % exc)


def close_popup():
    path = os.environ.get("HERDR_SOCKET_PATH")
    if not path:
        return
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(5)
        conn.connect(path)
        req = {"id": "gh-issues:popup-close", "method": "popup.close", "params": {}}
        conn.sendall((json.dumps(req) + "\n").encode("utf-8"))
        conn.recv(65536)
        conn.close()
    except Exception as exc:
        log("popup close failed (continuing): %s" % exc)


def herdr(args, timeout=90):
    """Run a herdr subcommand and return (result_dict_or_None, raw_last_line)."""
    try:
        proc = subprocess.run([HERDR] + args, capture_output=True, text=True,
                              timeout=timeout)
    except Exception as exc:
        return None, "herdr not runnable: %s" % exc
    raw = proc.stdout.strip() or proc.stderr.strip()
    last = raw.splitlines()[-1] if raw else ""
    try:
        env = json.loads(last)
    except ValueError:
        return None, last
    if isinstance(env, dict) and isinstance(env.get("result"), dict):
        return env["result"], last
    return None, last


def record_agent_item(name, repo, number, is_pr):
    """Record which item this agent was started for, keyed by agent name.

    bin/issue_meta.py reads this to publish the pane's sidebar task info. It can
    also recover repo+number by parsing the agent name, but that assumes the
    repo's short name plus the configured org; this is the exact answer.
    """
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
        "~/.local/state/herdr/plugins/evaldnet.gh-issues")
    try:
        d = os.path.join(state_dir, "agents")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "%s.json" % name), "w", encoding="utf-8") as fh:
            json.dump({"repo": repo, "number": number, "is_pr": bool(is_pr)}, fh)
    except Exception as exc:
        log("could not record agent item (continuing): %s" % exc)


def agent_name(repo, number, is_pr=False):
    """Must match [a-z][a-z0-9_-]{0,31} and be unique among live agents."""
    slug = re.sub(r"[^a-z0-9]+", "-", repo.split("/")[-1].lower()).strip("-")
    prefix = "pr" if is_pr else "gh"
    name = ("%s-%s-%d" % (prefix, slug or "issue", number))[:32].rstrip("-")
    return name or "gh-issue"


def fetch_issue(repo, number):
    args = ["gh", "issue", "view", str(number), "--repo", repo, "--json",
            "number,title,body,url,labels,assignees,state,milestone,createdAt,updatedAt"]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh issue view failed")
    return json.loads(proc.stdout)


def fetch_pr(repo, number):
    args = ["gh", "pr", "view", str(number), "--repo", repo, "--json",
            "number,title,body,url,state,isDraft,author,baseRefName,headRefName,"
            "additions,deletions,changedFiles,labels,reviewDecision"]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh pr view failed")
    return json.loads(proc.stdout)


def local_checkout(cfg, repo):
    root = os.path.expanduser(cfg["space_cwd"])
    name = (cfg.get("repo_paths") or {}).get(repo) or repo.split("/")[-1]
    path = os.path.join(root, name)
    return path if os.path.isdir(path) else ""


def build_pr_brief(cfg, repo, pr):
    body = (pr.get("body") or "").strip() or "(no description)"
    limit = int(cfg.get("body_limit", 6000))
    if len(body) > limit:
        body = body[:limit] + "\n\n[…truncated, read the rest with: gh pr view %d --repo %s]" % (
            pr["number"], repo)
    labels = ", ".join(l.get("name", "") for l in (pr.get("labels") or [])) or "none"
    author = (pr.get("author") or {}).get("login") or "unknown"
    decision = pr.get("reviewDecision") or "REVIEW_REQUIRED"
    checkout = local_checkout(cfg, repo)
    where = ("The checkout for %s is %s." % (repo, checkout) if checkout
             else "I could not find a local checkout for %s under %s." % (repo, cfg["space_cwd"]))

    return "\n".join([
        "I am reviewing pull request %s#%d." % (repo, pr["number"]),
        "",
        "  Title:     %s" % pr.get("title", ""),
        "  URL:       %s" % pr.get("url", ""),
        "  Author:    %s" % author,
        "  Branch:    %s -> %s" % (pr.get("headRefName", "?"), pr.get("baseRefName", "?")),
        "  Size:      %d files, +%d/-%d" % (pr.get("changedFiles") or 0,
                                            pr.get("additions") or 0,
                                            pr.get("deletions") or 0),
        "  Draft:     %s" % ("yes" if pr.get("isDraft") else "no"),
        "  Review:    %s" % decision,
        "  Labels:    %s" % labels,
        "",
        "%s You are starting in %s, which holds every evaldnet repo and its" % (
            where, cfg["space_cwd"]),
        "Worktrunk worktrees (named <repo>.<branch>).",
        "",
        "How to review this one:",
        "- Read the diff first: `gh pr diff %d --repo %s` works from anywhere." % (
            pr["number"], repo),
        "- The description below may be Danish. Treat it as input only: write your",
        "  review, findings and any notes in English. Danish UI strings quoted as",
        "  evidence stay verbatim - do not translate them.",
        "- Report findings to me here. Do NOT post review comments, approve, or",
        "  request changes on GitHub - that is outward-facing and I will decide what",
        "  gets posted.",
        "- Do not push to this branch, and do not merge, without asking me first.",
        "",
        "--- pull request #%d description ---" % pr["number"],
        body,
        "--- end of description ---",
    ])


def build_brief(cfg, repo, issue, board_status=""):
    body = (issue.get("body") or "").strip() or "(no description)"
    limit = int(cfg.get("body_limit", 6000))
    truncated = len(body) > limit
    if truncated:
        body = body[:limit] + "\n\n[…truncated, read the rest with: gh issue view %d --repo %s]" % (
            issue["number"], repo)
    labels = ", ".join(l.get("name", "") for l in (issue.get("labels") or [])) or "none"
    milestone = (issue.get("milestone") or {}).get("title") or "none"
    board = board_status or "not on the board"
    checkout = local_checkout(cfg, repo)
    where = ("The checkout for %s is %s." % (repo, checkout) if checkout
             else "I could not find a local checkout for %s under %s - locate it before editing."
             % (repo, cfg["space_cwd"]))

    return "\n".join([
        "I am picking up GitHub issue %s#%d." % (repo, issue["number"]),
        "",
        "  Title:     %s" % issue.get("title", ""),
        "  URL:       %s" % issue.get("url", ""),
        "  State:     %s" % issue.get("state", ""),
        "  Labels:    %s" % labels,
        "  Milestone: %s" % milestone,
        "  Board:     %s" % board,
        "",
        "%s You are starting in %s, which holds every evaldnet repo and its"
        % (where, cfg["space_cwd"]),
        "Worktrunk worktrees (named <repo>.<branch>).",
        "",
        "How to work this one:",
        "- The issue text below is Danish: it is synced from the external tracker for the support team.",
        "  Treat it as input only. Answer me in English, and write every artifact",
        "  (plan files, commit messages, PR title and body, review notes) in English.",
        "  Danish UI strings quoted as evidence stay verbatim - do not translate them.",
        "- Do NOT comment on, close, assign, relabel, or move this issue on the",
        "  Tasks board. It syncs back into the external tracker onto a task there. If something needs",
        "  saying or moving there, tell me and I will.",
        "- Start by reading the issue and the relevant code, then tell me what you",
        "  found and what you propose. Do not start changing code until we agree.",
        "- Commit locally when there is something worth committing, but never push",
        "  and never open a pull request without asking me first.",
        "",
        "--- issue #%d (Danish, verbatim) ---" % issue["number"],
        body,
        "--- end of issue ---",
    ])


# Measured over 109 logged launches before this existed: 78 briefed, 29 lost to
# agent_pane_busy, 2 to agent_name_not_found. 27% is not a rare race.
PANE_BUSY = "agent_pane_busy"


def start_agent(cfg, name, pane):
    """Start the agent, waiting out a pane whose shell is still coming up.

    `workspace create` returns a pane id the moment the pane exists, but
    `agent start` needs it to have reached its interactive shell prompt, and
    nothing in between guarantees that. Herdr rejects the early call outright
    with agent_pane_busy rather than waiting, so the wait has to live here --
    its own --timeout covers agent readiness *after* the shell is up, which is
    a later problem than the one that was losing a quarter of the launches.

    Returns (ok, raw). ok is False only for a failure worth abandoning the
    space over: agent_not_ready still leaves a usable agent to prompt.
    """
    budget = float(cfg.get("agent_start_retry_seconds", 20))
    deadline = time.time() + budget
    delay = 0.25
    attempt = 0
    while True:
        attempt += 1
        res, raw = herdr(["agent", "start", name, "--kind", str(cfg["agent_kind"]),
                          "--pane", pane, "--timeout", "90000"], timeout=120)
        if res is not None:
            if attempt > 1:
                log("agent start ok on attempt %d" % attempt)
            return True, raw
        if "agent_not_ready" in raw:
            # The name is registered and the pane is live; prompting still works.
            log("agent started but reported not ready; prompting anyway: %s" % raw)
            return True, raw
        if PANE_BUSY not in raw:
            log("agent start failed: %s" % raw)
            return False, raw
        if time.time() >= deadline:
            log("pane %s never became an available shell within %.0fs "
                "(%d attempts): %s" % (pane, budget, attempt, raw))
            return False, raw
        # Backs off so a slow shell does not get hammered, but starts tight
        # because the common case is ready within a few hundred ms.
        time.sleep(delay)
        delay = min(delay * 2, 2.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--number", required=True, type=int)
    ap.add_argument("--title", default="")
    ap.add_argument("--status", default="", help="project board column, if any")
    ap.add_argument("--kind", default="issue", choices=("issue", "pr"))
    ap.add_argument("--command", default="/github-issue",
                    help="slash command to send, e.g. /github-review")
    opts = ap.parse_args()

    cfg = load_config()
    wait_for_popup_exit()
    close_popup()

    is_pr = opts.kind == "pr"
    try:
        item = fetch_pr(opts.repo, opts.number) if is_pr else fetch_issue(opts.repo, opts.number)
    except Exception as exc:
        log("could not read %s#%d: %s" % (opts.repo, opts.number, exc))
        return 1

    title = item.get("title") or opts.title
    label = "%s#%d %s" % ("PR " if is_pr else "", opts.number, title)
    if len(label) > 60:
        label = label[:59] + "…"

    cwd = os.path.expanduser(cfg["space_cwd"])
    res, raw = herdr(["workspace", "create", "--cwd", cwd, "--label", label, "--focus"])
    if not res:
        log("workspace create failed: %s" % raw)
        return 1
    pane = ((res.get("root_pane") or {}).get("pane_id")
            or (res.get("root_pane") or {}).get("id"))
    if not pane:
        log("workspace create returned no root pane: %s" % raw)
        return 1

    name = agent_name(opts.repo, opts.number, is_pr)
    record_agent_item(name, opts.repo, opts.number, is_pr)
    ok, raw = start_agent(cfg, name, pane)
    if not ok:
        return 1

    # The project skills own the workflow, so hand them the reference and get
    # out of the way. A URL is unambiguous for both issues and PRs; `repo#num`
    # would leave /github-issue guessing which kind it is.
    url = item.get("url") or "https://github.com/%s/%s/%d" % (
        opts.repo, "pull" if is_pr else "issues", opts.number)
    brief = "%s %s" % (opts.command.strip(), url)
    res, raw = herdr(["agent", "prompt", pane, brief], timeout=120)
    if res is None and "stalled" not in raw:
        log("agent prompt failed: %s" % raw)
        return 1
    log("briefed %s on %s %s#%d in pane %s" % (
        name, "PR" if is_pr else "issue", opts.repo, opts.number, pane))
    return 0


if __name__ == "__main__":
    sys.exit(main())
