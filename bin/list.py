"""Print one gh-issues view as JSON, for front ends that draw their own list.

The Herdr app mod (app/) and the Tern plugin both call this instead of
re-implementing the search: config.json, views, issue fields and exclusions
stay one source of truth, in panel.py next to this file.

    list.py                  the views, no rows
    list.py <view id>        that view's rows
    list.py --lookup <text>  find an issue or PR by number, as `#` does
"""
from __future__ import annotations

import json
import os
import sys

# panel.py sits beside this file; sys.path[0] is this file's real directory
# even when it is reached through a symlink (the Tern plugin links it).
import panel  # noqa: E402


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
            ' closingIssuesReferences(first:1) { nodes { number } } } }'
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


def row_out(r, command):
    return {
        "repo": r.get("repo", ""), "short": r.get("short", ""),
        "number": r.get("number", 0), "issue": r.get("issue"), "title": r.get("title", ""),
        "url": r.get("url", ""), "is_pr": r.get("is_pr", False),
        "group": r.get("group") or r.get("cell") or "—",
        "inline": r.get("inline", ""), "labels": r.get("labels") or [],
        "date": r.get("date_display") or r.get("updated", ""),
        "author": r.get("author", ""), "review": r.get("review", ""),
        "command": command,
    }


def main():
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
