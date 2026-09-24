#!/usr/bin/env python3
"""Docked detail pane for the task the current Herdr space was opened for.

Same shape as the Agent Usage pane: a right split that stays up and refreshes
itself. Where that pane answers "how much quota is left", this one answers "what
am I actually working on" -- board fields, type, assignee, linked PRs and the
issue body, for the issue this space was created from.

Scoped to its own space. A Herdr pane lives in one workspace, so the pane opened
in the #102 space shows #102 and nothing else; switching spaces shows that
space's own pane.

Read-only, like the rest of this plugin: it never writes to GitHub.

Targets /usr/bin/python3 (3.9.6 on this Mac): no `X | None` annotations, no
match statements, stdlib only.
"""
from __future__ import annotations

import curses
import json
import locale
import os
import subprocess
import sys
import textwrap
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_meta  # noqa: E402  (same directory; resolver + cache live there)
from openurl import browser_command, open_url  # noqa: E402

locale.setlocale(locale.LC_ALL, "")

HERDR = issue_meta.HERDR

ISSUE_DETAIL_QUERY = """
query($owner:String!,$name:String!,$num:Int!){
  repository(owner:$owner,name:$name){
    issue(number:$num){
      number title state url body updatedAt
      author{login}
      assignees(first:5){nodes{login}}
      milestone{title}
      comments{totalCount}
      labels(first:20){nodes{name}}
      issueType{name}
      closedByPullRequestsReferences(first:10, includeClosedPrs:true){
        nodes{number url state isDraft reviewDecision repository{nameWithOwner}}}
      issueFieldValues(first:30){nodes{
        ... on IssueFieldSingleSelectValue{value field{... on IssueFieldSingleSelect{name}}}
        ... on IssueFieldDateValue{value field{... on IssueFieldDate{name}}}
        ... on IssueFieldTextValue{value field{... on IssueFieldText{name}}}
        ... on IssueFieldNumberValue{value field{... on IssueFieldNumber{name}}}
      }}
    }
  }
}
"""

PR_DETAIL_QUERY = """
query($owner:String!,$name:String!,$num:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$num){
      number title state url body updatedAt isDraft reviewDecision
      additions deletions changedFiles
      author{login}
      headRefName baseRefName
      assignees(first:5){nodes{login}}
      comments{totalCount}
      labels(first:20){nodes{name}}
    }
  }
}
"""

PR_STATE = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested",
            "REVIEW_REQUIRED": "review required"}


def workspace_item(cfg):
    """Resolve the issue for this pane's workspace, or None."""
    ws = os.environ.get("HERDR_WORKSPACE_ID")
    env = issue_meta.run_json([HERDR, "agent", "list"])
    agents = ((env or {}).get("result") or {}).get("agents") or []
    if ws:
        agents = [a for a in agents if a.get("workspace_id") == ws] or agents
    # An agent this plugin started is the one that names the issue; prefer it.
    for a in agents:
        item = issue_meta.resolve_item(cfg, a)
        if item:
            return item
    return None


def fetch_detail(item):
    owner, _, name = item["repo"].partition("/")
    if not owner or not name:
        # Never bare None: every caller does data.get(...), and an
        # AttributeError here exits the pane -- the failure this module's
        # opener guard exists to prevent.
        return {"_error": "not a repo: %r" % item.get("repo")}
    query = PR_DETAIL_QUERY if item["is_pr"] else ISSUE_DETAIL_QUERY
    args = ["gh", "api", "graphql", "-f", "query=%s" % query,
            "-f", "owner=%s" % owner, "-f", "name=%s" % name,
            "-F", "num=%d" % item["number"]]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return {"_error": "could not run gh: %s" % exc}
    if proc.returncode != 0:
        msg = (proc.stderr.strip() or "gh exited %d" % proc.returncode)
        return {"_error": msg.splitlines()[0][:160]}
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        return {"_error": "bad JSON: %s" % exc}
    if payload.get("errors"):
        return {"_error": payload["errors"][0].get("message", "?")[:160]}
    repo = ((payload.get("data") or {}).get("repository") or {})
    node = repo.get("pullRequest") if item["is_pr"] else repo.get("issue")
    return node or {"_error": "not found"}


def field_values(data):
    out = []
    for node in ((data.get("issueFieldValues") or {}).get("nodes") or []):
        field = ((node or {}).get("field") or {}).get("name")
        value = (node or {}).get("value")
        if field and value not in (None, ""):
            out.append((field, str(value)))
    return out


def build_lines(cfg, item, data, width):
    """Render to a list of (style, text). Styles: head, rule, key, body, warn."""
    L = []
    w = max(20, width - 2)

    # Wide enough that the org's longest field name ("Status", "Complexity")
    # still leaves a gap before its value.
    pad = 12

    def kv(label, value):
        if value in (None, "", []):
            return
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        first = True
        for chunk in textwrap.wrap(str(value), max(8, w - pad)) or [""]:
            L.append(("key", "%-*s%s" % (pad, label if first else "", chunk)))
            first = False

    short = item["repo"].split("/")[-1]
    marker = "!" if item["is_pr"] else "#"
    ident = "%s%s%d" % (short, marker, item["number"])

    if data.get("_error"):
        L.append(("head", ident))
        L.append(("rule", ""))
        for chunk in textwrap.wrap(data["_error"], w):
            L.append(("warn", chunk))
        return L

    state = (data.get("state") or "").lower()
    if item["is_pr"]:
        head_state = "draft" if data.get("isDraft") else (
            PR_STATE.get(data.get("reviewDecision") or "") or state)
    else:
        head_state = state
        for name, value in field_values(data):
            if issue_meta.norm_field(name) == issue_meta.norm_field(
                    cfg.get("status_field") or "Status"):
                head_state = value
                break
        if state == "closed":
            head_state = "closed"
    L.append(("head", "%s · %s" % (ident, head_state)))
    L.append(("rule", ""))

    for chunk in textwrap.wrap(data.get("title") or "", w) or [""]:
        L.append(("title", chunk))
    L.append(("body", ""))

    if item["is_pr"]:
        kv("State", "draft" if data.get("isDraft") else state)
        kv("Review", PR_STATE.get(data.get("reviewDecision") or "") or "none yet")
        kv("Branch", "%s → %s" % (data.get("headRefName") or "?",
                                  data.get("baseRefName") or "?"))
        kv("Diff", "+%s −%s in %s files" % (data.get("additions"),
                                            data.get("deletions"),
                                            data.get("changedFiles")))
    else:
        for name, value in field_values(data):
            kv(name, value)
        itype = ((data.get("issueType") or {}).get("name") or "").strip()
        if itype and not cfg.get("issue_type_emoji"):
            import re as _re
            itype = _re.sub(r"^[^\w]+", "", itype).strip()
        kv("Type", itype)
        kv("State", state)

    kv("Author", ((data.get("author") or {}).get("login") or ""))
    kv("Assignee", [n.get("login") for n in
                    ((data.get("assignees") or {}).get("nodes") or [])])
    kv("Milestone", ((data.get("milestone") or {}).get("title") or ""))
    kv("Labels", [n.get("name") for n in
                  ((data.get("labels") or {}).get("nodes") or [])])
    kv("Updated", (data.get("updatedAt") or "")[:10])
    comments = (data.get("comments") or {}).get("totalCount")
    kv("Comments", str(comments) if comments else "")

    prs = ((data.get("closedByPullRequestsReferences") or {}).get("nodes") or [])
    if prs:
        L.append(("body", ""))
        for pr in prs:
            repo = (pr.get("repository") or {}).get("nameWithOwner") or ""
            decision = PR_STATE.get(pr.get("reviewDecision") or "")
            bits = [(pr.get("state") or "").lower()]
            if pr.get("isDraft"):
                bits.append("draft")
            if decision:
                bits.append(decision)
            # Through kv() so it wraps like every other row; built by hand it was
            # the one line that could run past a narrow pane.
            kv("PR", "%s#%d  %s" % (repo.split("/")[-1], pr.get("number") or 0,
                                    " · ".join(bits)))

    body = (data.get("body") or "").replace("\r", "").expandtabs(4).strip()
    if body:
        L.append(("rule", ""))
        for para in body.split("\n"):
            # The sync bot brackets its blocks in HTML comments; they are markers
            # for its own round-trip, not content, and they read as noise here.
            if para.strip().startswith("<!--") and para.strip().endswith("-->"):
                continue
            if not para.strip():
                L.append(("body", ""))
                continue
            # evaldnet issues indent their "what changed" bullets four spaces.
            # Wrapping without carrying that indent turns a list into a wall.
            indent = " " * (len(para) - len(para.lstrip(" ")))
            # Hang the continuation only for a line that was indented to begin
            # with, so a list reads as a list and a paragraph stays flush left.
            hang = indent + "  " if indent else ""
            # Strip the source indent first: textwrap keeps leading whitespace on
            # the first line, so passing it through as well doubles it.
            for chunk in textwrap.wrap(para.strip(), w, initial_indent=indent,
                                       subsequent_indent=hang) or [""]:
                L.append(("body", chunk))
    return L


def draw(stdscr, lines, top, updated_at, status):
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 4 or width < 24:
        stdscr.addnstr(0, 0, "pane too small", max(0, width - 1))
        stdscr.refresh()
        return
    body_h = height - 1
    for i in range(body_h):
        idx = top + i
        if idx >= len(lines):
            break
        style, text = lines[idx]
        if style == "rule":
            text = "─" * (width - 2)
        attr = curses.A_NORMAL
        if style == "head":
            attr = curses.A_BOLD | curses.color_pair(1)
        elif style == "title":
            attr = curses.A_BOLD
        elif style == "rule":
            attr = curses.color_pair(2)
        elif style == "key":
            attr = curses.color_pair(2)
        elif style == "warn":
            attr = curses.A_BOLD
        try:
            stdscr.addnstr(i, 1, text, max(0, width - 2), attr)
        except curses.error:
            pass
    stamp = time.strftime("%H:%M", time.localtime(updated_at)) if updated_at else "—"
    more = "" if top + body_h >= len(lines) else " ↓"
    foot = status or ("Updated %s · j/k scroll · r refresh · o browser · q quit%s"
                      % (stamp, more))
    stdscr.attron(curses.color_pair(2))
    try:
        stdscr.addnstr(height - 1, 0, foot.ljust(width - 1)[:width - 1], width - 1)
    except curses.error:
        pass
    stdscr.attroff(curses.color_pair(2))
    stdscr.refresh()


def run(stdscr):
    curses.curs_set(0)
    curses.use_default_colors()
    try:
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
    except curses.error:
        pass
    stdscr.timeout(250)

    cfg = issue_meta.load_config()
    item = workspace_item(cfg)
    if not item:
        # Nothing to poll for: this pane is in a space with no issue behind it.
        # Block rather than spinning on the refresh timeout, but still redraw on
        # a resize -- a docked pane gets resized, and a footer placed from a
        # stale height lands in the middle of the text.
        stdscr.timeout(-1)
        while True:
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            stdscr.addnstr(0, 1, "no issue space here", max(0, w - 2), curses.A_BOLD)
            stdscr.addnstr(2, 1, "Open this pane inside a space", max(0, w - 2))
            stdscr.addnstr(3, 1, "created from the issue panel.", max(0, w - 2))
            stdscr.attron(curses.color_pair(2))
            stdscr.addnstr(h - 1, 0, " q quit".ljust(w - 1), w - 1)
            stdscr.attroff(curses.color_pair(2))
            stdscr.refresh()
            try:
                ch = stdscr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                return
            if ch == curses.KEY_RESIZE:
                continue
            if ch in ("q", "\x1b"):
                return

    try:
        interval = max(0, int(cfg.get("refresh_seconds", 120) or 0))
    except (TypeError, ValueError):
        interval = 120

    data = fetch_detail(item)
    last_ok = [time.time()] if not data.get("_error") else [0.0]
    last_fetch = time.time()
    backoff = 1
    top = 0
    dirty = True
    status = ""
    last_stamp = ""
    lines = []
    last_width = -1

    def refetch():
        """Keep the last good detail on failure rather than blanking the pane."""
        fresh = fetch_detail(item)
        if fresh.get("_error"):
            return data, fresh["_error"]
        return fresh, ""

    while True:
        height, width = stdscr.getmaxyx()
        if width != last_width:
            last_width = width
            lines = build_lines(cfg, item, data, width)
            dirty = True
        body_h = max(1, height - 1)
        top = max(0, min(top, max(0, len(lines) - body_h)))
        stamp = time.strftime("%H:%M", time.localtime(last_ok[0]))
        if stamp != last_stamp:
            last_stamp = stamp
            dirty = True
        if dirty:
            draw(stdscr, lines, top, last_ok[0], status)
            dirty = False

        try:
            ch = stdscr.get_wch()
        except curses.error:
            if not interval or time.time() - last_fetch < interval * backoff:
                continue
            data, err = refetch()
            last_fetch = time.time()
            if err:
                backoff = min(backoff * 2, 8)
                status = err[:120]
            else:
                backoff = 1
                status = ""
                last_ok[0] = time.time()
            lines = build_lines(cfg, item, data, width)
            dirty = True
            continue
        except KeyboardInterrupt:
            return

        status = ""
        dirty = True
        if ch in ("q", "\x1b"):
            return
        elif ch in ("j", curses.KEY_DOWN):
            top += 1
        elif ch in ("k", curses.KEY_UP):
            top -= 1
        elif ch == curses.KEY_NPAGE or ch == " ":
            top += body_h
        elif ch == curses.KEY_PPAGE:
            top -= body_h
        elif ch == "g":
            top = 0
        elif ch == "G":
            top = max(0, len(lines) - body_h)
        elif ch == "r":
            data, err = refetch()
            last_fetch = time.time()
            if err:
                status = err[:120]
                backoff = min(backoff * 2, 8)
            else:
                backoff = 1
                last_ok[0] = time.time()
            lines = build_lines(cfg, item, data, width)
        elif ch == "o":
            opened = open_url(data.get("url") or "")
            if opened:
                status = "opened in browser"
            elif opened is False:
                status = "no %s on PATH" % browser_command()
        top = max(0, min(top, max(0, len(lines) - body_h)))


def main():
    # No pane bookkeeping on disk: scripts/open-task.sh finds a live pane by
    # inspecting running task_pane.py processes, so there is no file to go
    # stale when Herdr renumbers workspace ids.
    try:
        curses.wrapper(run)
    except Exception as exc:
        sys.stderr.write("gh-issues: %s\n" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
