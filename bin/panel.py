#!/usr/bin/env python3
"""GitHub issue panel for Herdr.

Lists the issues assigned to you across the configured org, annotated with their
column on the org's project board. Enter opens a new Herdr space with Claude
started and already briefed on the issue.

Read-only by design: this panel never comments on, closes, assigns, relabels or
moves an issue. These GitHub issues sync back into a separate tracker used by the
support team, so any write here would land as a write over there.

Targets /usr/bin/python3 (3.9.6 on this Mac): no `X | None` annotations, no
match statements, stdlib only.
"""
from __future__ import annotations

import curses
import json
import locale
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from openurl import browser_command, open_url  # noqa: E402

locale.setlocale(locale.LC_ALL, "")

PLUGIN_ROOT = os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(
    os.path.expanduser("~/.config/herdr/plugins/config"), "evaldnet.gh-issues"
)
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
    "~/.local/state/herdr/plugins/evaldnet.gh-issues"
)
LAUNCHER = os.path.join(PLUGIN_ROOT, "bin", "launch.py")

# Sentinel for the "on no board column" filter step. Not a real column name.
OFF_BOARD = "\x00off-board"
OFF_BOARD_CELL = "—"

# Same trick for the "carries no label at all" step of the label cycle.
UNLABELLED = "\x00unlabelled"
UNLABELLED_TEXT = "unlabelled"

DEFAULTS = {
    # No default org: querying somebody else's is worse than
    # saying nothing. load_view() reports it as a setup error.
    "org": "",
    "space_cwd": "~/Projects",
    "limit": 100,
    "agent_kind": "claude",
    # "assigned" = assigned to me across the org. "created" / "mentions" also work.
    "scope": "assigned",
    # Views cycled with `v` / `V`. kind: "issues" | "prs". flags go straight to
    # `gh search`. `query` is a GitHub search string (org: is added).
    # column: an org Issue Field name | "author" | "issuetype" | "none".
    # exclude_labels: labels whose rows never appear in the view at all.
    "views": [
        {"id": "assigned", "label": "issues assigned to me", "kind": "issues",
         "query": "is:issue is:open assignee:@me", "column": "Status"},
        {"id": "totest", "label": "ready for test, not mine", "kind": "issues",
         "query": "is:issue is:open -assignee:@me", "column": "Status",
         "where": {"Status": ["Ready for test"]}},
        {"id": "review", "label": "PRs awaiting my review", "kind": "prs",
         "flags": ["--review-requested=@me"], "column": "author"},
        {"id": "ready", "label": "my PRs ready for review", "kind": "prs",
         "flags": ["--author=@me", "--draft=false", "--review=none"],
         "column": "none"},
    ],
    # Clickable footer. Mouse reporting takes the drag away from the terminal,
    # so plain drag-to-select stops working in the popup; false gives it back.
    "mouse": True,
}

# type: ISSUE_ADVANCED, not ISSUE. Only the advanced endpoint honours `field.`
# qualifiers; plain ISSUE silently returns 0 for them (measured: 0 vs 23 for the
# same query). Server-side filtering also keeps pagination honest -- filtering
# locally after a capped fetch under-reports without saying so.
ISSUE_SEARCH_QUERY = """
query($q:String!, $after:String) {
  search(query:$q, type:ISSUE_ADVANCED, first:100, after:$after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Issue {
        number title url state updatedAt
        repository { nameWithOwner }
        assignees(first:5) { nodes { login } }
        labels(first:10) { nodes { name } }
        issueType { name }
        issueFieldValues(first:20) {
          nodes {
            ... on IssueFieldSingleSelectValue {
              value
              field { ... on IssueFieldSingleSelect { name } }
            }
            ... on IssueFieldDateValue {
              value
              field { ... on IssueFieldDate { name } }
            }
          }
        }
      }
    }
  }
}
"""



def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(CONFIG_DIR, "config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            cfg.update(user)
    except Exception:
        pass
    return cfg


def norm_field(name):
    """Fold the ways a field name gets written: Status, status, status_x."""
    return re.sub(r"[\s_-]+", " ", (name or "").strip().lower())


def current_login():
    try:
        proc = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return ""


# `gh search prs --json` has no reviewDecision field (its list is assignees,
# author, authorAssociation, body, closedAt, commentsCount, createdAt, id,
# isDraft, isLocked, isPullRequest, labels, number, repository, state, title,
# updatedAt, url), and `gh pr view` would be one call per PR. Aliasing the rows
# we already have into one GraphQL query keeps it at a single request, and works
# off repo+number rather than re-deriving the view's search string from flags.
REVIEW_DECISIONS = {
    "APPROVED": ("approved", "✓"),
    "CHANGES_REQUESTED": ("changes req", "✗"),
    "REVIEW_REQUIRED": ("review req", "◷"),
}
REPO_PART = re.compile(r"^[A-Za-z0-9._-]+$")


def fetch_review_decisions(rows):
    """Set `review`/`review_glyph` on each PR row. One aliased GraphQL call."""
    parts, alias_to_row = [], {}
    for i, r in enumerate(rows):
        owner, _, name = (r.get("repo") or "").partition("/")
        if not REPO_PART.match(owner or "") or not REPO_PART.match(name or ""):
            continue
        alias = "p%d" % i
        alias_to_row[alias] = r
        parts.append(
            '  %s: repository(owner:"%s", name:"%s") '
            "{ pullRequest(number:%d) { reviewDecision } }"
            % (alias, owner, name, int(r.get("number") or 0)))
    if not parts:
        return ""
    data, err = gh_graphql("query {\n%s\n}" % "\n".join(parts), {})
    if err:
        return err
    for alias, row in alias_to_row.items():
        pr = ((data.get(alias) or {}).get("pullRequest") or {})
        text, glyph = REVIEW_DECISIONS.get(pr.get("reviewDecision") or "", ("", ""))
        row["review"] = text
        row["review_glyph"] = glyph
    return ""


def gh_graphql(query, variables):
    args = ["gh", "api", "graphql", "-f", "query=%s" % query]
    for k, v in variables.items():
        args += (["-F", "%s=%d" % (k, v)] if isinstance(v, int)
                 else ["-f", "%s=%s" % (k, v)])
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=90)
    except Exception as exc:
        return None, "could not run gh: %s" % exc
    if proc.returncode != 0:
        msg = (proc.stderr.strip() or "gh exited %d" % proc.returncode)
        return None, msg.splitlines()[0][:140]
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        return None, "bad JSON: %s" % exc
    if payload.get("errors"):
        return None, payload["errors"][0].get("message", "?")[:140]
    return payload.get("data") or {}, ""


def fetch_issue_fields(org):
    """Org-level Issue Fields -> {folded name: (real name, [options in order])}.

    These are NOT project fields. `field.Status:` in GitHub search resolves
    here, which is why an empty project board says nothing about them. The REST
    payload has no `position`, but option ids are allocated in creation order,
    so sorting by id recovers the intended workflow order.
    """
    try:
        proc = subprocess.run(["gh", "api", "orgs/%s/issue-fields" % org],
                              capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return {}, "issue fields: could not run gh: %s" % exc
    if proc.returncode != 0:
        return {}, "issue fields: %s" % (proc.stderr.strip().splitlines() or ["failed"])[0][:110]
    try:
        data = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        return {}, "issue fields: bad JSON: %s" % exc

    out = {}
    for field in data:
        name = field.get("name") or ""
        opts = sorted(field.get("options") or [], key=lambda o: o.get("id") or 0)
        out[norm_field(name)] = (name, [o.get("name", "") for o in opts])
    return out, ""


def field_options(fields, wanted):
    """Resolve a loosely-written field name to its ordered options."""
    candidates = [wanted] if isinstance(wanted, str) else list(wanted or [])
    for candidate in candidates:
        hit = fields.get(norm_field(candidate))
        if hit:
            return hit[0], hit[1]
    return "", []


def gh_search_issues(cfg, view):
    """Search issues over GraphQL so issue-field values come back with them."""
    query = view.get("query") or "is:issue is:open"
    org = cfg.get("org")
    if org and "org:" not in query and "repo:" not in query:
        query = "org:%s %s" % (org, query)
    limit = int(cfg.get("limit", 100))

    rows = []
    after = ""
    while len(rows) < limit:
        variables = {"q": query}
        if after:
            variables["after"] = after
        data, err = gh_graphql(ISSUE_SEARCH_QUERY, variables)
        if err:
            return rows, err
        block = data.get("search") or {}
        for node in (block.get("nodes") or []):
            if not node:
                continue
            repo = (node.get("repository") or {}).get("nameWithOwner") or ""
            values = {}
            for v in ((node.get("issueFieldValues") or {}).get("nodes") or []):
                fname = ((v or {}).get("field") or {}).get("name")
                if fname:
                    values[norm_field(fname)] = v.get("value") or ""
            rows.append({
                "repo": repo, "short": repo.split("/")[-1],
                "number": node.get("number") or 0,
                "title": (node.get("title") or "").replace("\n", " ").strip(),
                "url": node.get("url") or "",
                "updated": (node.get("updatedAt") or "")[:10],
                "labels": [l.get("name", "") for l in
                           ((node.get("labels") or {}).get("nodes") or [])],
                "assignees": [a.get("login", "") for a in
                              ((node.get("assignees") or {}).get("nodes") or [])],
                "issue_type": (node.get("issueType") or {}).get("name") or "",
                "fields": values, "author": "", "is_pr": False, "cell": "",
                "date_display": "",
            })
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor") or ""
        if not after:
            break
    return rows[:limit], ""


def gh_search_prs(cfg, view):
    """PRs still come from `gh search prs`: issue fields do not apply to them."""
    args = ["gh", "search", "prs", "--state=open",
            "--limit", str(cfg["limit"]),
            "--json", "repository,number,title,url,updatedAt,labels,isDraft,author",
            "--sort", "updated"]
    org = cfg.get("org")
    if org:
        args.append("--owner=%s" % org)
    for flag in (view.get("flags") or []):
        args.append(str(flag))
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return [], "could not run gh: %s" % exc
    if proc.returncode != 0:
        return [], (proc.stderr.strip().splitlines() or ["gh failed"])[0][:140]
    try:
        data = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        return [], "could not parse gh output: %s" % exc
    rows = []
    for it in data:
        repo = (it.get("repository") or {}).get("nameWithOwner") or ""
        rows.append({
            "repo": repo, "short": repo.split("/")[-1],
            "number": it.get("number") or 0,
            "title": (it.get("title") or "").replace("\n", " ").strip(),
            "url": it.get("url") or "",
            "updated": (it.get("updatedAt") or "")[:10],
            "labels": [l.get("name", "") for l in (it.get("labels") or [])],
            "assignees": [],
            "issue_type": "",
            "fields": {},
            "author": (it.get("author") or {}).get("login") or "",
            "is_pr": True, "cell": "",
        })
    return rows, ""


def apply_where(rows, where):
    """Keep only rows matching issue-field values, e.g. {"Status": ["Ready for test"]}."""
    for raw_name, allowed in (where or {}).items():
        key = norm_field(raw_name)
        wanted = set(allowed if isinstance(allowed, list) else [allowed])
        rows = [r for r in rows if r.get("fields", {}).get(key, "") in wanted]
    return rows


def apply_exclude(rows, exclude):
    """Drop rows whose issue-field value is one you never want to see.

    Terminal states like Approved/Deployed are out of the working set, so they
    are removed before grouping -- they do not become an empty section, and the
    header count reflects only what is left.
    """
    for raw_name, unwanted in (exclude or {}).items():
        key = norm_field(raw_name)
        drop = set(unwanted if isinstance(unwanted, list) else [unwanted])
        rows = [r for r in rows if r.get("fields", {}).get(key, "") not in drop]
    return rows


def apply_order(options, wanted):
    """Pull named values to the front of the board order; the rest keep theirs.

    The field's own option order is creation order, which is the workflow read
    forwards. A view can want it read by urgency instead -- what bounced back
    first, what is in hand, what has not been picked up -- without restating
    the whole field. Names are matched case-insensitively, so a config saying
    `Test Rejected` still finds `Test rejected`, and a name the field does not
    have is ignored rather than inventing an empty section.
    """
    if not wanted:
        return options
    lookup = {o.lower(): o for o in options}
    front, taken = [], set()
    for name in (wanted if isinstance(wanted, list) else [wanted]):
        hit = lookup.get((name or "").strip().lower())
        if hit and hit not in taken:
            taken.add(hit)
            front.append(hit)
    return front + [o for o in options if o not in taken]


def apply_exclude_labels(rows, names):
    """Drop rows carrying any of these labels.

    The label counterpart of apply_exclude(): work that is parked behind a hold
    label is out of the working set the same way Approved/Deployed is, so it
    goes before grouping and the header count reflects what is left. Because
    the rows are gone, the excluded labels also drop out of the `l` cycle --
    nothing carries them any more.

    Matched case-insensitively: GitHub will not let two labels differ only by
    case, so folding cannot collide, and it forgives a config that writes
    `on hold` for `On Hold`.
    """
    drop = set(n.strip().lower() for n in (names or []) if n)
    if not drop:
        return rows
    return [r for r in rows
            if not drop.intersection(l.lower() for l in (r.get("labels") or []))]


def matches(row, needle, cell_filter, label_filter=""):
    if cell_filter == OFF_BOARD:
        if row.get("cell"):
            return False
    elif cell_filter and row.get("cell") != cell_filter:
        return False
    # Membership, not equality: `labels` is a set per row, so a row carrying
    # three labels is reachable under each of them.
    if label_filter == UNLABELLED:
        if row.get("labels"):
            return False
    elif label_filter and label_filter not in (row.get("labels") or []):
        return False
    if not needle:
        return True
    hay = "%s#%d %s %s %s" % (
        row["short"], row["number"], row["title"],
        " ".join(row["labels"]), row.get("cell", ""),
    )
    return needle.lower() in hay.lower()


def clip(text, width):
    """Trim to width columns. Danish text is BMP, so len() is a fair proxy."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def filter_label(cell_filter, column):
    if cell_filter == OFF_BOARD:
        return "unset"
    return cell_filter or "all"


def label_filter_text(label_filter):
    if label_filter == UNLABELLED:
        return UNLABELLED_TEXT
    return label_filter or "all"


def current_view(cfg, index):
    views = cfg.get("views") or DEFAULTS["views"]
    if not views:
        views = DEFAULTS["views"]
    return views[index % len(views)], views


def launcher_argv(row, view_command):
    return ["/usr/bin/python3", LAUNCHER,
            "--repo", row["repo"],
            "--number", str(row["number"]),
            "--title", row["title"],
            "--status", row.get("cell", "") if not row.get("is_pr") else "",
            "--kind", "pr" if row.get("is_pr") else "issue",
            "--command", view_command]


def open_launch_log():
    os.makedirs(STATE_DIR, exist_ok=True)
    return open(os.path.join(STATE_DIR, "launch.log"), "a", encoding="utf-8")


def detach_launcher(cfg, row, view_command="/github-issue"):
    """Spawn the launcher so it runs *after* this popup closes.

    Herdr popups are session-modal: a space created while the popup is still up
    stays hidden behind it. Same barrier trick trunkr uses -- the child blocks
    reading a pipe whose only writer is this process, so it wakes on our exit.
    """
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    log = open_launch_log()
    devnull = open(os.devnull, "r")
    env = dict(os.environ)
    env["HERDR_GHI_BARRIER_FD"] = str(read_fd)
    try:
        subprocess.Popen(
            launcher_argv(row, view_command),
            stdin=devnull, stdout=log, stderr=log,
            start_new_session=True,
            pass_fds=(read_fd,),
            env=env,
            cwd=os.path.expanduser(cfg["space_cwd"]),
        )
    finally:
        os.close(read_fd)
        devnull.close()
    # write_fd stays open deliberately: closing it on exit is the wake signal.
    return write_fd


def cell_width(rows, options, column):
    """Width comes from the field's declared options, not the rows on screen,
    so the column keeps its place as the board fills up."""
    if column == "none":
        return 0
    if options:
        widest = max(len(o) for o in options)
    else:
        widest = max([len(r.get("cell", "")) for r in rows] + [len(OFF_BOARD_CELL)])
    return min(17, max(widest, len(OFF_BOARD_CELL)))


def build_display(rows, options, column):
    """Group rows into sections, in board order, with the unset bucket last.

    Returns a flat render list of ("head", label, count) and ("row", row), so
    scrolling stays a single index while selection counts only real rows.
    """
    if column == "none":
        return [("row", r) for r in rows]
    grouped_key = "group" if any(r.get("group") for r in rows) else "cell"
    buckets = {}
    for r in rows:
        buckets.setdefault(r.get(grouped_key, ""), []).append(r)
    if grouped_key == "group":
        # Rows arrive pre-sorted, so first appearance already encodes the order.
        order, seen = [], set()
        for r in rows:
            k = r.get("group", "")
            if k not in seen:
                seen.add(k)
                order.append(k)
    else:
        order = [o for o in options if o in buckets]
        order += [k for k in sorted(buckets) if k and k not in options]
        if "" in buckets:
            order.append("")
    ckey = norm_field(column)
    unset = {"author": "no author", "issuetype": "no type",
             "issue type": "no type", "type": "no type"}.get(
                 ckey, "no %s" % (column.lower() if isinstance(column, str) else "value"))
    display = []
    for key in order:
        display.append(("head", key or unset, len(buckets[key])))
        for r in buckets[key]:
            display.append(("row", r))
    return display


def draw(stdscr, display, sel, top, needle, filtering, status, cfg, view, cell_filter,
         options=(), total=0, label_filter="", label_steps_n=0):
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 4 or width < 30:
        stdscr.addnstr(0, 0, "window too small", width - 1)
        stdscr.refresh()
        return []

    header = " GitHub · %s · %s" % (cfg.get("org", "?"), view.get("label", view.get("id", "")))
    if cell_filter:
        header += " · %s" % filter_label(cell_filter, view.get("column"))
    if label_filter:
        header += " · %s" % label_filter_text(label_filter)
    header += " "
    count = " %d " % total
    stdscr.attron(curses.A_BOLD | curses.color_pair(1))
    stdscr.addnstr(0, 0, clip(header, width - len(count) - 1).ljust(width - len(count)), width)
    stdscr.addnstr(0, max(0, width - len(count)), count, len(count))
    stdscr.attroff(curses.A_BOLD | curses.color_pair(1))

    column = view.get("column", "none")
    rows_only = [d[1] for d in display if d[0] == "row"]
    cell_w = cell_width(rows_only, options, column)
    show_cell = cell_w > 0
    # Fixed-width whether or not a given row has a decision, so titles line up.
    show_review = any(r.get("review_glyph") for r in rows_only)
    grouped = any(d[0] == "head" for d in display)
    indent = "  " if grouped else ""

    body_h = height - 3
    ordinal = -1
    for i in range(body_h):
        idx = top + i
        y = i + 1
        if idx >= len(display):
            break
        item = display[idx]
        if item[0] == "head":
            label, n = item[1], item[2]
            tail = " %d " % n
            bar = "── %s " % label
            bar = bar + "─" * max(0, width - 1 - len(bar) - len(tail)) + tail
            stdscr.attron(curses.A_BOLD | curses.color_pair(2))
            stdscr.addnstr(y, 0, clip(bar, width - 1), width - 1)
            stdscr.attroff(curses.A_BOLD | curses.color_pair(2))
            continue

        row = item[1]
        # Selection counts rows only, so headers can never be landed on.
        ordinal = sum(1 for d in display[:idx] if d[0] == "row")
        chosen = ordinal == sel
        if chosen:
            stdscr.attron(curses.A_REVERSE)
        cell = row.get("cell") or OFF_BOARD_CELL
        inline = ("%-*s  " % (cell_w, clip(cell, cell_w))) if show_cell else ""
        mark = ("%-2s" % row.get("review_glyph", "")) if show_review else ""
        prefix = "%s%-14s #%-5d %s%s" % (
            indent, clip(row["short"], 14), row["number"], mark, inline)
        suffix = " %s" % (row.get("date_display") or row["updated"])
        avail = width - len(prefix) - len(suffix) - 1
        labels = ",".join(row["labels"])
        title = row["title"]
        if labels and avail > 24:
            keep = min(len(labels), max(0, avail // 3))
            title = "%s  [%s]" % (title, clip(labels, keep))
        line = prefix + clip(title, max(0, avail)).ljust(max(0, avail)) + suffix
        stdscr.addnstr(y, 0, clip(line, width - 1), width - 1)
        if chosen:
            stdscr.attroff(curses.A_REVERSE)

    spans = []
    if filtering:
        foot = "/%s" % needle
    elif status:
        foot = status
    else:
        items = footer_items(show_cell, label_steps_n)
        foot = FOOT_SEP.join(label for label, _ in items)
        spans = footer_spans(items, width - 1)
    stdscr.attron(curses.color_pair(2))
    stdscr.addnstr(height - 1, 0, clip(foot, width - 1).ljust(width - 1), width - 1)
    stdscr.attroff(curses.color_pair(2))
    stdscr.refresh()
    return spans


FOOT_SEP = " · "


def footer_items(show_cell, label_steps_n):
    """The footer menu as (label, key) pairs; key is what a click on it sends."""
    items = [("enter claude", "\n"), ("v view", "v")]
    if show_cell:
        items.append(("s column", "s"))
    if label_steps_n > 1:
        items.append(("l label", "l"))
    items += [("/ filter", "/"), ("r refresh", "r")]
    if not show_cell:
        items.append(("o browser", "o"))
    items.append(("q quit", "q"))
    return items


def footer_spans(items, limit):
    """Column range of each footer item, as [(start, end, key)].

    Items clipped off the right edge are dropped: a click there must not fire
    something the user cannot see. One that is only partly visible still
    counts -- its key letter is always the first thing drawn.
    """
    spans, x = [], 0
    for label, key in items:
        if x >= limit:
            break
        spans.append((x, min(x + len(label), limit), key))
        x += len(label) + len(FOOT_SEP)
    return spans


def footer_key(spans, x):
    for start, end, key in spans:
        if start <= x < end:
            return key
    return None


def load_view(cfg, view, state):
    """Fetch one view and set each row's `cell` from the view's column."""
    org = cfg.get("org") or ""
    if not org:
        # Without an org the search would sweep all of GitHub, and an empty
        # result reads as "nothing assigned" rather than "not configured".
        return [], [], ("no `org` set — put one in config.json "
                        "(herdr plugin config-dir evaldnet.gh-issues)")
    if "fields" not in state:
        state["fields"], state["fields_err"] = fetch_issue_fields(org)
    fields = state["fields"]

    if view.get("kind") == "prs":
        rows, err = gh_search_prs(cfg, view)
        # Before the review-decision call, so excluded PRs cost no aliases.
        rows = apply_exclude_labels(rows, view.get("exclude_labels"))
        if rows:
            # Advisory: a PR list without decisions still beats no list at all.
            review_err = fetch_review_decisions(rows)
            if review_err and not err:
                err = "review state unavailable: %s" % review_err
    else:
        rows, err = gh_search_issues(cfg, view)
        rows = apply_where(rows, view.get("where"))
        rows = apply_exclude(rows, view.get("exclude"))
        rows = apply_exclude_labels(rows, view.get("exclude_labels"))

    column = view.get("column", "none")
    ckey = norm_field(column)
    options = []
    if column in ("none", ""):
        for r in rows:
            r["cell"] = ""
    elif ckey == "author":
        for r in rows:
            r["cell"] = r.get("author", "")
    elif ckey in ("assignees", "assignee", "owner"):
        for r in rows:
            r["cell"] = ", ".join(r.get("assignees") or [])
    elif ckey in ("issuetype", "issue type", "type"):
        for r in rows:
            r["cell"] = r.get("issue_type", "")
    elif ckey in ("review", "review decision", "reviewdecision"):
        # Board order rather than alphabetical: what needs your hands first.
        options = ["changes req", "review req", "approved"]
        for r in rows:
            r["cell"] = r.get("review", "")
    else:
        real, options = field_options(fields, column)
        dropped = set()
        for raw_name, unwanted in (view.get("exclude") or {}).items():
            if norm_field(raw_name) == ckey:
                dropped |= set(unwanted if isinstance(unwanted, list) else [unwanted])
        options = [o for o in options if o not in dropped]
        if not real and not err:
            err = "no issue field %r (org has: %s)" % (
                column, ", ".join(sorted(v[0] for v in fields.values())) or "none")
        for r in rows:
            r["cell"] = r.get("fields", {}).get(ckey, "")
    # Optional: a view-local section order. Only columns with declared options
    # have an order to override -- author/assignee/type stay alphabetical.
    options = apply_order(options, view.get("order"))
    # Optional: show a named date field instead of the updated-at column.
    date_field = view.get("date_field")
    if date_field:
        dkey = norm_field(date_field)
        for r in rows:
            r["date_display"] = r.get("fields", {}).get(dkey, "") or OFF_BOARD_CELL

    # Optional: sort by a field. Rows holding a value come first, ascending;
    # ISO dates sort correctly as plain strings.
    sort_by = view.get("sort_by")
    if sort_by:
        skey = norm_field(sort_by)
        rows.sort(key=lambda r: (not r.get("fields", {}).get(skey),
                                 r.get("fields", {}).get(skey, "")))

    # Optional: split into has-value / lacks-value sections rather than
    # grouping by the column. Order follows the sort above.
    group_by = view.get("group_by")
    if group_by:
        gkey = norm_field(group_by)
        labels = view.get("group_labels") or [
            "%s set" % group_by, "No %s" % group_by[0].lower() + group_by[1:]]
        for r in rows:
            r["group"] = labels[0] if r.get("fields", {}).get(gkey) else labels[1]

    if not err and state.get("fields_err"):
        err = state["fields_err"]
    return rows, options, err


def label_steps(rows):
    """Label steps: all, each label present (commonest first), then unlabelled.

    Orthogonal to cycle_steps() on purpose. The column filter narrows by one
    single-valued field; this narrows by set membership, so the two stack --
    "In progress" *and* "On Hold" is a question the board cannot answer alone.

    Order is by frequency because the useful labels here are the hold states
    that pile up, and the long module:* tail should not push them off the
    front of the cycle. Ties break alphabetically so the order is stable
    across refreshes rather than shuffling with dict insertion.
    """
    counts = {}
    for r in rows:
        for name in (r.get("labels") or []):
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return [""]
    steps = [""] + sorted(counts, key=lambda n: (-counts[n], n.lower()))
    if any(not r.get("labels") for r in rows):
        steps.append(UNLABELLED)
    return steps


def cycle_steps(rows, options, column):
    """Filter steps: all, each value present (board order first), then unset."""
    if column == "none":
        return [""]
    present = set(r.get("cell", "") for r in rows if r.get("cell"))
    if not present:
        return [""]
    ordered = [o for o in options if o in present]
    for name in sorted(present):
        if name not in ordered:
            ordered.append(name)
    steps = [""] + ordered
    if any(not r.get("cell") for r in rows):
        steps.append(OFF_BOARD)
    return steps


def run(stdscr):
    curses.curs_set(0)
    curses.use_default_colors()
    try:
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
    except curses.error:
        pass

    cfg = load_config()
    if cfg.get("mouse", True):
        # Herdr delivers a click as separate press and release reports, so
        # act on the release; CLICKED covers terminals that send it whole.
        curses.mousemask(curses.BUTTON1_RELEASED | curses.BUTTON1_CLICKED)
        curses.mouseinterval(0)
    state = {}
    cache = {}
    vi = 0
    view, views = current_view(cfg, vi)

    def fetch(v, force=False):
        key = v.get("id")
        if force or key not in cache:
            stdscr.erase()
            stdscr.addstr(0, 0, "loading %s…" % v.get("label", v.get("id", "")))
            stdscr.refresh()
            fresh = load_view(cfg, v, state)
            prev = cache.get(key)
            # A failed refetch must not blank a list that was good a moment ago:
            # keep the rows, let the error ride in the status line.
            if prev and prev[0] and fresh[2] and not fresh[0]:
                cache[key] = (prev[0], prev[1], fresh[2])
            else:
                cache[key] = fresh
        return cache[key]

    all_rows, options, status = fetch(view)
    needle = ""
    filtering = False
    sel = 0
    top = 0
    steps = cycle_steps(all_rows, options, view.get("column", "none"))
    step = 0
    lsteps = label_steps(all_rows)
    lstep = 0

    while True:
        cell_filter = steps[step] if step < len(steps) else ""
        label_filter = lsteps[lstep] if lstep < len(lsteps) else ""
        rows = [r for r in all_rows
                if matches(r, needle, cell_filter, label_filter)]
        display = build_display(rows, options, view.get("column", "none"))
        positions = [i for i, d in enumerate(display) if d[0] == "row"]
        # Grouping reorders rows, so `rows` order != on-screen order. `sel`
        # counts rows as drawn, so act on this list and never on `rows`.
        visible = [display[i][1] for i in positions]
        if sel >= len(visible):
            sel = max(0, len(visible) - 1)
        height, _ = stdscr.getmaxyx()
        body_h = max(1, height - 3)
        selpos = positions[sel] if 0 <= sel < len(positions) else 0
        # Pull the section header into view when the selection is its first row.
        anchor = selpos - 1 if (selpos > 0 and display[selpos - 1][0] == "head") else selpos
        if anchor < top:
            top = anchor
        elif selpos >= top + body_h:
            top = selpos - body_h + 1
        top = max(0, min(top, max(0, len(display) - 1)))

        if not all_rows and not status:
            status = "nothing in “%s” right now" % view.get("label", "this view")
        spans = draw(stdscr, display, sel, top, needle, filtering, status, cfg,
                     view, cell_filter, options, len(rows), label_filter,
                     len(lsteps))

        try:
            ch = stdscr.get_wch()
        except curses.error:
            continue
        except KeyboardInterrupt:
            return None

        if ch == curses.KEY_MOUSE:
            # A footer click stands in for its key; anything else is ignored
            # without clearing the status line. `spans` is empty while the
            # footer shows a status or the filter prompt.
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                continue
            clicked = bstate & (curses.BUTTON1_RELEASED | curses.BUTTON1_CLICKED)
            key = footer_key(spans, mx) if clicked and my == height - 1 else None
            if key is None:
                continue
            ch = key

        if filtering:
            if ch in ("\n", "\r", curses.KEY_ENTER):
                filtering = False
            elif ch == "\x1b":
                filtering = False
                needle = ""
            elif ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                needle = needle[:-1]
            elif isinstance(ch, str) and ch.isprintable():
                needle += ch
            sel = 0
            continue

        status = ""
        if ch in ("q", "\x1b"):
            return None
        if ch == "/":
            filtering = True
            needle = ""
            continue
        if ch in ("j", curses.KEY_DOWN):
            sel = min(sel + 1, max(0, len(visible) - 1))
        elif ch in ("k", curses.KEY_UP):
            sel = max(sel - 1, 0)
        elif ch == curses.KEY_NPAGE:
            sel = min(sel + body_h, max(0, len(visible) - 1))
        elif ch == curses.KEY_PPAGE:
            sel = max(sel - body_h, 0)
        elif ch == "g":
            sel = 0
        elif ch == "G":
            sel = max(0, len(visible) - 1)
        elif ch in ("v", "V", "\t"):
            vi = (vi + (-1 if ch == "V" else 1)) % len(views)
            view, views = current_view(cfg, vi)
            all_rows, options, status = fetch(view)
            steps = cycle_steps(all_rows, options, view.get("column", "none"))
            step = 0
            lsteps = label_steps(all_rows)
            lstep = 0
            needle = ""
            sel = 0
            top = 0
        elif ch in ("s", "S"):
            if len(steps) <= 1:
                status = ("board has no items yet — nothing to filter by"
                          if view.get("column") == "board"
                          else "nothing to filter by in this view")
            else:
                step = (step + (1 if ch == "s" else -1)) % len(steps)
                sel = 0
                top = 0
        elif ch in ("l", "L"):
            if len(lsteps) <= 1:
                status = "nothing in this view carries a label"
            else:
                lstep = (lstep + (1 if ch == "l" else -1)) % len(lsteps)
                sel = 0
                top = 0
        elif ch == "r":
            state.pop("fields", None)
            state.pop("fields_err", None)
            all_rows, options, status = fetch(view, force=True)
            steps = cycle_steps(all_rows, options, view.get("column", "none"))
            step = 0
            lsteps = label_steps(all_rows)
            lstep = 0
            needle = ""
            sel = 0
        elif ch == "o":
            # Guard on `visible`, which is what `sel` indexes -- `rows` is the
            # flat filtered list and build_display is not contractually obliged
            # to emit a row for every element of it.
            if visible:
                opened = open_url(visible[sel].get("url"))
                if opened:
                    status = "opened #%d in browser" % visible[sel]["number"]
                elif opened is False:
                    status = "no %s on PATH" % browser_command()
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            if visible:
                visible[sel]["_view_index"] = vi
                return visible[sel]
    return None


def main():
    if not os.path.exists(LAUNCHER):
        sys.stderr.write("gh-issues: launcher missing at %s\n" % LAUNCHER)
        return 1
    cfg = load_config()
    try:
        chosen = curses.wrapper(run)
    except Exception as exc:
        sys.stderr.write("gh-issues: %s\n" % exc)
        return 1
    if not chosen:
        return 0
    view, _views = current_view(cfg, chosen.get("_view_index", 0))
    detach_launcher(cfg, chosen, view.get("command") or "/github-issue")
    sys.stdout.write("briefing claude on %s#%d\u2026\n" % (chosen["repo"], chosen["number"]))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
