"""Print one gh-issues view as JSON, for front ends that draw their own list.

The Herdr app mod (app/) and the Tern plugin both call this instead of
re-implementing the search: config.json, views, issue fields and exclusions
stay one source of truth, in panel.py next to this file.

    list.py                  the views, no rows
    list.py <view id>        that view's rows
    list.py --lookup <text>  find an issue or PR by number, as `#` does
    list.py --status <ref>.. task status per issue space, for the agents list
"""
from __future__ import annotations

import json
import os
import re
import sys

# panel.py sits beside this file; sys.path[0] is this file's real directory
# even when it is reached through a symlink (the Tern plugin links it).
import panel  # noqa: E402
import issue_meta  # noqa: E402

# owner/repo#n, or repo#n in the config's org.
REF_RE = re.compile(r"^(?:([\w.-]+)/)?([\w.-]+)#(\d+)$")


def link_issues(rows):
    """Give each PR row the issue it closes: we work from issues, so a PR's
    session is its issue's. One aliased GraphQL query for all of them."""
    prs = [r for r in rows if r.get("is_pr") and "/" in r.get("repo", "")]
    if not prs:
        return
    parts = []
    for i, r in enumerate(prs):
        owner, name = r["repo"].split("/", 1)
        parts.append(
            'p%d: repository(owner:%s, name:%s) { pullRequest(number:%d) {'
            ' closingIssuesReferences(first:1) { nodes { number'
            ' repository { nameWithOwner } } } } }'
            % (i, json.dumps(owner), json.dumps(name), int(r["number"])))
    data, err = panel.gh_graphql("query {\n%s\n}" % "\n".join(parts), {},
                                 allow_missing=True)
    if err:
        return
    for i, r in enumerate(prs):
        pr = ((data or {}).get("p%d" % i) or {}).get("pullRequest") or {}
        nodes = (pr.get("closingIssuesReferences") or {}).get("nodes") or []
        if nodes:
            r["issue"] = nodes[0].get("number")
            # A PR can close an issue in another repo (customers -> core).
            r["issue_repo"] = ((nodes[0].get("repository") or {})
                               .get("nameWithOwner") or r["repo"])


def row_out(r, command):
    return {
        "repo": r.get("repo", ""), "short": r.get("short", ""),
        "number": r.get("number", 0), "issue": r.get("issue"),
        "issue_repo": r.get("issue_repo") or (None if r.get("is_pr") else r.get("repo", "")),
        "title": r.get("title", ""),
        "url": r.get("url", ""), "is_pr": r.get("is_pr", False),
        "group": r.get("group") or r.get("cell") or "—",
        "inline": r.get("inline", ""), "labels": r.get("labels") or [],
        "date": r.get("date_display") or r.get("updated", ""),
        "author": r.get("author", ""), "review": r.get("review", ""),
        "command": command,
    }


def resolve(cfg, ref):
    """A ref -> (owner, name, number), or an error. A bare number (a space
    named by hand, `#4654`) is looked up across the org and kept only when it
    names one issue: open ones win over closed ones."""
    m = REF_RE.match(ref)
    if m:
        return (m.group(1) or cfg.get("org") or "", m.group(2), int(m.group(3))), ""
    if not ref.isdigit():
        return None, "not an issue ref: %s" % ref
    rows, err = panel.lookup(cfg, ref)
    issues = [r for r in rows if not r.get("is_pr")]
    live = [r for r in issues if r.get("inline") != "closed"] or issues
    if len(live) != 1:
        return None, err or ("no issue #%s" % ref if not live else
                             "#%s is ambiguous: %s" % (ref, ", ".join(r["repo"] for r in live)))
    owner, name = live[0]["repo"].split("/", 1)
    return (owner, name, int(live[0]["number"])), ""


def status(refs):
    """Each ref's task status, as the classic sidebar showed it: the board
    column (`closed` wins), priority and issue type, via issue_meta's own
    tokens_for(). One aliased GraphQL request for every resolved ref."""
    cfg = issue_meta.load_config()
    out = {"status": {}, "err": "",
           "watch_seconds": cfg.get("watch_seconds", 120),
           "notify_status_change": cfg.get("notify_status_change", True)}
    want = {}
    for ref in refs:
        target, err = resolve(cfg, ref)
        if target:
            want[ref] = target
        else:
            out["status"][ref] = {"err": err}
    if not want:
        return out
    keys = list(want)
    parts = ['i%d: repository(owner:%s, name:%s) { issue(number:%d) {%s} }'
             % (i, json.dumps(want[k][0]), json.dumps(want[k][1]), want[k][2],
                issue_meta.ISSUE_SELECTION) for i, k in enumerate(keys)]
    data, err = panel.gh_graphql("query {\n%s\n}" % "\n".join(parts), {},
                                 allow_missing=True)
    if err:
        # Whole request failed: report it and send nothing, so the front end
        # keeps the last good values instead of blanking every row.
        out["err"] = err
        return out
    for i, k in enumerate(keys):
        owner, name, num = want[k]
        node = (((data or {}).get("i%d" % i) or {}).get("issue")) or None
        if not node:
            out["status"][k] = {"err": "%s/%s#%d not found" % (owner, name, num)}
            continue
        item = {"repo": "%s/%s" % (owner, name), "number": num, "is_pr": False}
        tok = issue_meta.tokens_for(cfg, item, node)
        out["status"][k] = {
            "repo": item["repo"], "number": num,
            "title": (node.get("title") or "").strip(),
            "status": tok.get("issue_status"), "priority": tok.get("issue_priority"),
            "type": tok.get("issue_type"),
        }
    return out


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        print(json.dumps(status(sys.argv[2:])))
        return 0
    cfg = panel.load_config()
    _, views = panel.current_view(cfg, 0)
    out = {
        "space_cwd": cfg.get("space_cwd", "~/Projects"),
        "views": [{"id": v.get("id", ""), "label": v.get("label", "")} for v in views],
    }
    if len(sys.argv) < 2:
        print(json.dumps(out))
        return 0
    if sys.argv[1] == "--lookup":
        rows, err = panel.lookup(cfg, " ".join(sys.argv[2:]))
        link_issues(rows)
        out["err"] = err
        out["rows"] = [dict(row_out(r, panel.command_for(cfg, r)), group="")
                       for r in rows]
        print(json.dumps(out))
        return 0
    view = next((v for v in views if v.get("id") == sys.argv[1]), None)
    if view is None:
        out["err"] = "no view %r in config.json" % sys.argv[1]
        print(json.dumps(out))
        return 0
    rows, options, err = panel.load_view(cfg, view, {})
    # The panel shows sections in board order; the picker groups consecutive
    # rows, so sort by that order (stable, keeps the row_order inside each).
    rank = {o: i for i, o in enumerate(options)}
    rows.sort(key=lambda r: rank.get(r.get("group") or r.get("cell", ""), len(rank)))
    command = view.get("command") or "/github-issue"
    out["err"] = err
    link_issues(rows)
    out["rows"] = [row_out(r, command) for r in rows]
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
