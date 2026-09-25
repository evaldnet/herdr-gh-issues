# evaldnet.gh-issues — GitHub issue panel for Herdr

Lists the issues assigned to you across your org; `Enter` opens a new Herdr space
with Claude started and briefed on the issue. A docked pane shows the task you
are working on, and a sidebar row keeps its status next to the agent.

## Before you install — two things it depends on

Read these first. Both are design choices rather than bugs, but if neither holds
you get a list with no grouping and an `Enter` that appears to do nothing, which
looks like a broken plugin rather than a missing prerequisite.

**1. The board column needs GitHub's org-level Issue Fields.** The sections, the
`s` filter, `$issue_status` and the task pane's field list all read them. They
are not project-board fields, and `gh search issues` cannot see them — this is
why the plugin queries `search(type: ISSUE_ADVANCED)` over GraphQL instead. If
your org has none, `gh api orgs/<org>/issue-fields` returns nothing and every
view reports `no issue field 'Status' (org has: none)`. The issue list still
works; set `"column": "none"` on your views to drop the grouping cleanly.

**2. `Enter` sends a slash command, not a prose brief.** Each view names one
(`"command": "/github-issue"`), and the launcher sends that plus the item's URL
to the new agent. You need a skill of that name resolvable from `space_cwd`, or
Claude receives a command it does not have. Point `command` at a skill you
already have, or write one — it receives a single GitHub URL.

## Requirements

- **Herdr ≥ 0.8.0** and **`gh`**, authenticated (`gh auth status`)
- **macOS or Linux**, Python 3.9+ at `/usr/bin/python3`
- No build step, no compiler, no daemon, no third-party packages — tests included

## Install

```bash
herdr plugin install evaldnet/herdr-gh-issues
# non-interactive shells (CI, coding agents) need --yes
```

Copy `config.example.json` from the plugin into its config directory and edit it:

```bash
herdr plugin config-dir evaldnet.gh-issues      # prints where config.json belongs
```

At minimum set `org` and `space_cwd`. `status_field` must name a single-select
issue field your org actually defines, and each view's `column` must too —
`gh api orgs/<org>/issue-fields` lists what you have. Then bind the two surfaces in
`~/.config/herdr/config.toml` and reload:

```toml
[[keys.command]]
key = "prefix+shift+i"
type = "plugin_action"
command = "evaldnet.gh-issues.open"
description = "GitHub issues"

[[keys.command]]
key = "prefix+shift+t"
type = "plugin_action"
command = "evaldnet.gh-issues.open-task"
description = "Task detail pane"
```

For the sidebar row, add `$issue_status` to `[ui.sidebar.agents]` — see
*Sidebar task info* below. Finish with `herdr server reload-config`.

## Use

Two surfaces over the same list:

| surface | key | shape |
|---------|-----|-------|
| popup | `prefix+shift+i` | session-modal, 85%×80%. `Enter` hands off and closes it. |
| task | `prefix+shift+t` | docked split pane, **this space's** issue in detail. |
| sidebar | — | one row per agent, always on. See *Sidebar task info*. |

Or the command palette → "GitHub Issues: open panel" / "open task detail pane".

| key | action |
|-----|--------|
| `j` / `k` / arrows | move |
| `g` / `G` | top / bottom |
| `/` | filter (Danish text works; `esc` clears) |
| `v` / `V` | cycle view forward / back (`tab` also works) |
| `s` / `S` | cycle the extra-column filter forward / back |
| `l` / `L` | cycle the label filter forward / back |
| `enter` | new space + Claude, briefed on the issue |
| `r` | refresh |
| `o` | open the issue in a browser |
| `q` | quit |

The footer is clickable too: a click on an item does what its key does. It
needs mouse reporting, which takes plain drag-to-select away from the popup;
set `mouse` to `false` to get selection back. The task pane stays
keyboard-only for now: Herdr 0.9.1 hands tab-bar clicks to a focused pane
that asked for the mouse ([herdr#4382](https://github.com/herdrdev/herdr/issues/4382)),
which a docked pane would trip over and a modal popup does not.

## What Enter sends

Enter hands the row to a project skill rather than a prose brief. Each view has a
`command`; the launcher appends the item's URL and sends that one line:

    /github-review  https://github.com/evaldnet/mobile/pull/303      (review view)
    /github-issue   https://github.com/evaldnet/platform/issues/103  (all others)

The URL is used rather than `repo#num` because it is unambiguous for issues and
PRs alike. The skills live in `~/Projects/evaldnet/.claude/skills/`, which is why
the space opens in `~/Projects/evaldnet` — a skill outside that tree would not
resolve.

## What Enter does

1. `herdr workspace create --cwd ~/Projects/evaldnet --label "#<n> <title>" --focus`
2. `herdr agent start gh-<repo>-<n> --kind claude --pane <root_pane>`
3. `herdr agent prompt <pane> "<brief>"`

The brief carries title, URL, state, labels, milestone, board column, the
resolved local checkout path, and the issue body verbatim. It also tells Claude to answer in
English (the issue body is Danish), not to touch the issue, and not to push or
open a PR without asking.

The panel is a *popup*, and Herdr popups are session-modal — a space created
while the popup is still up stays hidden behind it. So `panel.py` detaches
`launch.py` holding the read end of a pipe; the launcher blocks until the panel
exits, then closes the popup and creates the space. Same trick trunkr uses.

## When Enter does nothing

Step 2 above used to lose about a quarter of all launches. `workspace create`
returns a pane id as soon as the pane exists, but `agent start` needs that pane
to have reached its interactive shell prompt, and herdr rejects the early call
outright — `agent_pane_busy`, *"target pane is not an available shell"* — rather
than waiting for it. The launcher treated that as fatal, so the space opened
with no Claude in it and the URL was never sent. Measured over 109 logged
launches: 78 briefed, 29 lost this way, 2 to `agent_name_not_found`.

`start_agent()` now retries while the pane comes up, backing off from 250ms to a
2s ceiling, for `agent_start_retry_seconds` (default 20). Only that one error is
retried: `agent_name_not_found` will not fix itself, and `agent_not_ready` still
leaves a usable agent to prompt, so both are handled as before.

Note this is a *different* wait from `agent start --timeout 90000`, which covers
Claude becoming ready for input **after** the shell is up. That readiness signal
comes from `~/.claude/hooks/herdr-agent-state.sh`; if `herdr integration status`
reports it `outdated`, the prompt can be sent before Claude is listening — the
space opens, Claude starts, and the URL still goes nowhere. Re-run
`herdr integration install claude` after a Herdr upgrade.

`launch.log` lines are timestamped and carry seconds since launch, so an instant
failure can be told apart from one that burned a full timeout:

    2026-09-17 12:15:12 +  3.2s  agent start ok on attempt 4

## PR review state

PR rows carry their review decision: `✓` approved, `✗` changes requested, `◷`
review required, blank if GitHub reports none. The marker shows on every PR view;
set a view's `column` to `review` to also group and filter by it (`s`), which is
what the shipped `ready` view does.

`gh search prs --json` has no `reviewDecision` field, and `gh pr view` would be
one call per PR. So the decisions come from a single GraphQL query that aliases
the rows already on screen by `repo`+`number` — one request regardless of list
size, and no need to re-derive the view's search string from its `gh` flags.

## Sidebar task info

A space opened from an issue already shows `#122 Liste nulstiller filter ved skift af fane`
in the sidebar — that is Herdr falling back to the space label `launch.py` set.
It tells you *which* task, not *how it stands*. This adds that:

```
● #122 Liste nulstiller filter ved skift af fane
  claude · 5h 88%
  In Development · Bugfix
  First
```

`bin/issue_meta.py` runs as an event hook on `pane.focused` and
`pane.agent_status_changed`, resolves the pane to its issue, and reports five
tokens. They are stored whether or not you render them; put the ones you want in
`[ui.sidebar.agents]`:

| token | value | notes |
|-------|-------|-------|
| `$issue` | `platform#122` | `platform!303` for a PR |
| `$issue_status` | `In Development` | the `status_field` column; `closed` wins over it. PRs: `approved` / `changes req` / `review req` / `draft` |
| `$issue_priority` | `First` | the `priority_field` column; issues only |
| `$issue_type` | `Bugfix` | GitHub's native issue type, emoji stripped unless `issue_type_emoji` |
| `$issue_labels` | `module:kunde, module:integration` | empty on most issues |

Rows drop out on their own where the tokens are absent, so one shared agent
layout works for issue spaces and ordinary spaces alike:

```toml
[ui.sidebar.agents]
rows = [
  ["state_icon", "$title"],
  ["$provider", "$limit"],
  ["$context"],
  # Board statuses use GitHub's own Status option colours (dark theme); the
  # lowercase ones are PR review states and `closed`.
  [{ token = "$issue_status", rules = [
    { equals = "Pre Task Analysis", fg = "#8b949e" },
    { equals = "Not Started", fg = "#8b949e" },
    { equals = "In Development", fg = "#d29922" },
    { equals = "Validation", fg = "#a371f7" },
    { equals = "Validated OK", fg = "#db61a2" },
    { equals = "Ready for test", fg = "#db6d28" },
    { equals = "Test rejected", fg = "#f85149", bold = true },
    { equals = "Approved", fg = "#3fb950" },
    { equals = "Deployed", fg = "#4493f8" },
    { equals = "closed", fg = "#6272a4" },
    { equals = "changes req", fg = "#f85149", bold = true },
    { equals = "approved", fg = "#3fb950" },
  ] }, "$issue_type"],
  # Colours follow GitHub's own Priority option colours (dark theme).
  [{ token = "$issue_priority", rules = [
    { equals = "Prod Stop", fg = "#f85149", bold = true },
    { equals = "Ekspederes", fg = "#a371f7", bold = true },
    { equals = "First", fg = "#3fb950" },
    { equals = "Second", fg = "#d29922" },
    { equals = "Third", fg = "#db6d28" },
    { equals = "Last", fg = "#8b949e" },
  ] }],
]
```

### How a pane is matched to an issue

`launch.py` records `{repo, number, is_pr}` under the agent name it mints
(`$HERDR_PLUGIN_STATE_DIR/agents/gh-platform-122.json`), so the match is exact.
Spaces created before this existed have no record, so the hook falls back to
parsing the agent name — `gh-platform-122` plus the configured `org` gives
`evaldnet/platform#122`. A pane that resolves to nothing has *this plugin's* tokens
cleared and nobody else's, so a reused pane cannot keep showing a stale issue.

### Cost

`pane.focused` fires on every focus change, so each item is cached for
`meta_ttl_seconds` (180) under `$HERDR_PLUGIN_STATE_DIR/issue-cache/`. A focus
storm costs a file read. One GraphQL query per item per TTL returns state,
labels, issue type and every project field at once. A failed fetch keeps the
previous values rather than blanking the row.

Pane tokens do not survive a Herdr server restart, so a `[[startup]]` hook
re-seeds every agent pane instead of waiting for each to be focused once. To
rebuild them by hand:

    herdr plugin action invoke evaldnet.gh-issues.refresh-meta

### Watching a task

Focus and agent-state events are the only triggers Herdr offers — there is no
timer hook — so a task that moves on GitHub while you sit on one pane would
never repaint. The startup hook therefore also spawns a detached watcher
(`issue_meta.py --watch`) that re-fetches every issue space each
`watch_seconds` (120). Whenever a fetch sees `$issue_status` change, from the
watcher or from a focus, Herdr pops a notification:

    platform#3900: Ready for test → Test rejected

That notification is the one to wait for after handing off an urgent task. Two
panes on the same issue share a cache entry, so each change notifies once.

The watcher is one process no matter how many spaces are open: a lock under
`$HERDR_PLUGIN_STATE_DIR/watch.lock` holds its pid. It re-reads the config each
round, so `watch_seconds` edits apply without a restart, and it exits by itself
once the Herdr server stops answering. The `refresh-meta` action above replaces
it with a fresh one, which is how it picks up a plugin update. Setting
`watch_seconds` to `0` stops it at the end of its current sleep. Cost is one
GraphQL query per open issue space per round: ten spaces at 120s is 300
queries an hour, well inside GitHub's 5,000.

## Task detail pane

`prefix+shift+t` inside a space opened from an issue gives you the same
right-split shape as the Agent Usage pane, but showing what you are working on
rather than how much quota is left:

```
 platform#102 · Ready for test
 ──────────────────────────────────────────
 Eksempel Entreprise: fælles skabelon og
 automatisk afsender

 Priority    First
 Task Type   Support
 Complexity  Low - 1 reviewer
 Status Ready for test
 Type        Bugfix
 State       open
 Author      issue-sync-bot
 Assignee    lars
 Updated     2026-09-13

 PR          clients#201  open · review required
 ──────────────────────────────────────────
 Feltet blev gemt uden Word-
 skabelon til tilbud, og de var drevet fra
 …
 Updated 21:16 · j/k scroll · r refresh · o browser · q quit
```

Every single-select, date, text and number field the org defines on the issue is
listed, then type, assignee, labels, milestone and comment count, then any PR
that closes the issue with its review state, then the body — wrapped, with
indented lists kept as lists, scrollable with `j`/`k`, `space`, `g`/`G`.

On a `pr-*` space it shows the PR instead: review decision, branch, and diff
size (`+294 −1 in 22 files`).

**Scoped to its space, not to focus.** A Herdr pane lives in one workspace, so
the pane opened in the `#102` space shows `#102` and switching spaces shows
that space's own pane — no follow logic, and no way for it to drift onto the
wrong task. Opened in a space with no issue behind it, it says so and waits for
`q`. Opening it twice in the same space focuses the existing pane instead of
stacking a second one.

The duplicate check asks the OS, not a state file: `open-task.sh` looks for a
running `task_pane.py` whose `HERDR_WORKSPACE_ID` matches the current space and
focuses its `HERDR_PANE_ID`. Herdr **renumbers workspace ids** as spaces come and
go (a session sitting in `wB4` can find itself in `wB8`), so a guard file keyed
by workspace goes stale silently; a process's own environment cannot.

It refreshes every `refresh_seconds` (120), keeps the last good detail when a
fetch fails, and strips the sync bot's HTML comment markers from the body.

## Tests

    make test          # everything: offline, no GitHub auth, no running Herdr
    make test-live     # adds the checks that need the real herdr binary
    make lint          # syntax and manifest only, ~0.05s
    python3 tests/run.py task_pane -v     # one suite

75 tests, ~45 seconds. They run under `/usr/bin/python3` (3.9.6) because that is
the interpreter Herdr actually starts the plugin with — a test passing under
Homebrew's 3.14 would prove nothing about the thing that ships.

### How it avoids the network

`tests/harness.py` writes stub `gh` and `herdr` executables into a temp dir and
puts them first on `PATH`. They answer from `tests/fixtures/`, recorded from the
real tools and then scrubbed: field names, option values, labels, dates and
numbers are verbatim — the code branches on those — while titles, bodies,
branch names and colleague logins are replaced with synthetic Danish text of the
same shape. No customer or coworker name is in this repo.

That also means an issue changing status on GitHub cannot turn the suite red,
which matters here: half the assertions are about `Status`.

### Reading the screen

`harness.screen()` runs a curses program in a pty and replays the escape
sequences into a grid, so a test can assert on what a terminal would actually
show. Getting that wrong is worse than having no test, and it was wrong twice:

- **The pty needs its size set before the child execs.** Setting it from the
  parent races with `initscr()`, and a program that reads the size once at
  startup gets the pre-resize value — which looked exactly like a layout bug.
- **`\x1b[30d` (VPA) has to be honoured.** ncurses reaches for it to place a
  footer. Ignoring it drops the footer onto whatever row the cursor was on,
  which looked exactly like a *different* layout bug.

Both false alarms cost real time before the harness was trustworthy. `ECH`,
`CHA` and the three `EL` modes are handled for the same reason.

### What is covered

| suite | what it pins down |
|-------|-------------------|
| `test_static` | syntax, 3.9 compatibility, manifest shape, **event names against the known set** (Herdr only warns on a typo), that every referenced file exists, and that every config key the code reads appears in both `config.example.json` and this README |
| `test_issue_meta` | resolving a pane to an issue by record and by name fallback, token values and their 80-character limit, `closed` outranking the board column, field-name folding, cache TTL, and that a plain space has *our* tokens cleared and nobody else's |
| `test_panel` | one GraphQL request for N PRs, review-decision mapping, board grouping and option order, excluded values leaving no empty section, footer clicks landing on the key under the pointer (a real SGR mouse report through the pty), and that a `gh` outage draws an error rather than an empty list |
| `test_task_pane` | header identity, every field rendered, linked PRs with review state, body layout (lists hang, paragraphs stay flush, sync markers dropped), no line exceeding the pane width, and the three screens: issue, PR, and plain space |
| `test_live` | `herdr plugin link` reporting no warnings, and registered entrypoints matching the manifest. Skipped unless `GHI_LIVE=1` |

The suite was mutation-checked: `Status` colliding with its value, HTML
markers surviving into the body, `closed` no longer winning, the repo-name guard
accepting anything, and a typo'd event name were each introduced on purpose and
each turned the suite red.

### What is not covered

Anything that only exists inside a running Herdr server: whether an event hook
actually fires, and whether a reported token reaches the sidebar. Those were
verified by hand with `herdr plugin log list` and `herdr agent list`, and
`make test-live` covers only the link step.

## Views

`v` cycles three views, each a `gh search` with different flags:

| view | shows | column |
|------|-------|--------|
| `assigned` | open issues assigned to you | Tasks-board column |
| `review` | open PRs where your review is requested | PR author |
| `ready` | your open PRs out of draft with no review yet | none |
| `totest` | board `Ready for test`, not assigned to you | assignee |

Results are cached per view, so cycling is instant; `r` refetches the current
view and drops the board cache.

Enter adapts to the row. On an issue Claude is briefed to *work* it. On a PR it
is briefed to *review* it — the brief carries author, branch, diff size, review
decision, a `gh pr diff` hint, and an instruction to report findings here rather
than post review comments, approve, or request changes on GitHub.

### Why `totest` is not a `gh search`

The natural query — `is:issue state:open field.Status:"Ready for test"
-assignee:@me` — is valid in GitHub's *new* issue search UI, but the REST Search
API that `gh search` uses **silently ignores `field.` qualifiers**: it returns 0
with `search_type: lexical` rather than erroring. Verified against real data —
`evaldnet/platform#148` carries `Status: Deployed` on project 3, yet
`field.Status:Deployed` returns 0 while the same query without the qualifier
returns 6298. So a passthrough view would look like "nothing to test" forever.

Issues therefore go over GraphQL `search(type: ISSUE_ADVANCED)`, which *does*
honour `field.` server-side — so the `totest` view filters with a plain
qualifier in its query:

```json
"query": "is:issue is:open -assignee:@me field.Status:\"Ready for test\""
```

Server-side filtering also keeps pagination honest: filtering locally after a
capped fetch under-reports without saying so.

For anything the search cannot express, `where` keeps only matching issue-field
values and `exclude` drops them, applied to the rows that come back.

Field names are matched loosely — `Status`, `Task-Status` and `task_status` all
fold to the same field, because GitHub's filter box writes spaces as dashes
while GraphQL needs the exact name. If a view names a field the org does not
have, you get a named error listing the fields that exist.

`column` also accepts `issuetype`, which shows GitHub's **native** org Issue Type
(Refactoring / Bugfix / Feature / …) rather than a project field — preferred over
duplicating that taxonomy on the board.

`where` keeps only matching issue-field values; `exclude` drops them. Terminal
states are excluded from the `assigned` view so the list is the live working set:

    "exclude": {"Status": ["Approved", "Deployed"]}

Excluded values are removed before grouping, so they produce no empty section and
never appear in the `s` filter cycle.

More per-view keys shape the list:

| key | effect |
|-----|--------|
| `column` | an issue-field name, or `assignees` / `author` / `issuetype` / `none` |
| `order` | values pulled to the front of the section order; the rest keep the field's own |
| `exclude_labels` | labels whose rows never appear in this view; see *Excluding held work* |
| `sort_by` | sort by a field; rows holding a value come first, ascending (ISO dates sort as strings) |
| `group_by` | split into has-value / lacks-value sections instead of grouping by the column |
| `group_labels` | the two section names for `group_by` |
| `date_field` | show that field in the right-hand date column instead of updated-at |

The `totest` view uses all of them: owner in the column, sorted by
`Deployment date`, split into `Deployment date set` / `No deployment date`, with
the deployment date replacing updated-at. Section order follows the sort, so the
dated group leads with the earliest date.

Views are plain config: add or drop entries in `views`. `flags` are passed
straight to `gh search`, `kind` picks `issues` or `prs`, and `column` chooses
what the extra column shows (`board` | `author` | `none`) and therefore what `s`
filters by.

## Layout: inline column + sections

Rows are grouped into sections by the view's column, and the value also shows
inline between the issue number and the title:

    ── Ready for test ──────────────────────────────────────────── 3
      platform     #103  Ready for test     Liste nulstiller filter ved skift af fane  2026-09-01
      platform     #141  Ready for test     Liste nulstiller filter ved skift af fane    2026-08-31
    ── no status ───────────────────────────────────────────────── 2
      platform     #101  —                  Liste nulstiller filter ved skift af fane      2026-08-31

Sections follow the field's own option order (`Pre Task Analysis` → … →
`Deployed`), with the unset bucket last, labelled per column type: `no status`,
`no author`, `no type`.

A view can read that order by urgency instead, without restating the whole
field — `order` pulls the named values to the front and leaves the rest where
the field put them:

    "order": ["Test rejected", "In Development", "Not Started"]

which draws `Test rejected` → `In Development` → `Not Started` → `Pre Task
Analysis` → `Validation` → `Validated OK` → `Ready for test`. Names are matched
case-insensitively; one the field does not have — a typo, or a status excluded
by `exclude` — is ignored rather than drawn as an empty section. The `s` filter
cycle walks the same order. Only columns with declared options have an order to
override, so `order` is a no-op on `author` / `assignees` / `issuetype`. Section headers are never selectable — `j`/`k` step over
them, and scrolling pulls a header into view when you land on its first row.

The inline column is a fixed width derived from the field's declared **options**,
not from the rows on screen, so the title column does not shift as the board
fills up. A view with `column: "none"` renders flat, ungrouped.

## Board column

Each row shows its column on the org's **Tasks** project board (#5), read with one
GraphQL call (`fieldValueByName`) alongside the issue search, on a second thread.
`s` cycles: all columns → each column that actually has issues, in board order →
`not on board`. Issues absent from the board, or on it with `Status` unset, show
`—` and group under `not on board`.

The column is only drawn once the board has items, so it costs no width while the
board is empty — which it is today (0 items as of 2026-09-01). Until then `s`
says so rather than filtering to nothing. The panel still never *moves* anything
between columns; see below.

## Excluding held work

`exclude` drops rows by issue-field value; `exclude_labels` does the same by
label:

```json
"exclude": { "Task Status": ["Approved", "Deployed"] },
"exclude_labels": ["På hold af DEV", "On Hold"]
```

Carrying any one of the named labels is enough — a row's other labels do not
earn it a reprieve, or a hold would be escapable by adding a tag. It is applied
before grouping, so the header count and the sections reflect what is left, and
the excluded labels drop out of the `l` cycle too because nothing carries them
any more. Matching folds case: GitHub will not allow two labels differing only
in case, so `on hold` finds `On Hold`.

Unlike `exclude`, this one works on **PR views as well** — labels are the one
field PRs and issues genuinely share.

The example ships the key empty — it documents the option without hiding
anything from a fresh install. Populate it per view:

```json
"exclude_labels": ["På hold af DEV", "On Hold"]
```

On this org that takes the assigned view from 47 rows to 29, and `Ready for
test` from 17 to 3 — 14 of those 17 were parked behind DEV, which is not work
you can pick up.

## Label filter

`l` narrows the list to one label, `L` steps back. It is a **separate cycle from
`s`**, and the two stack: `s` to `In progress`, `l` to `On Hold`, and the header
reads

    GitHub · evaldnet · issues assigned to me · In progress · On Hold   12

Labels are not like the other columns. A row has *one* status but any number of
labels, so this filter is set membership: a row carrying `On Hold` and
`module:integration` is reachable under both, and nothing is duplicated on
screen — the grouping still comes from the view's column. That is why it is its
own key rather than `column: "labels"`.

Steps are derived from the rows currently loaded, commonest label first, then
`unlabelled` for the rows carrying none, then back to all. Frequency rather than
alphabetical because the labels that matter here are the hold states that pile
up, and a long `module:*` tail would otherwise push them off the front of the
cycle. Ties break by name, so the order does not shuffle between refreshes.
Nothing is configured: a new label appears in the cycle the moment an issue
carries it, and leaves when none does. A view where nothing is labelled says so
instead of appearing to filter, and drops `l label` from the footer.

Switching view (`v`) or refreshing (`r`) resets the label filter, same as `s`.

## Read-only, on purpose

evaldnet's GitHub issues sync back into the external tracker for the support team. A comment or a
close here lands on a task there over there, so the panel has no write path at all.

## Config

`~/.config/herdr/plugins/config/evaldnet.gh-issues/config.json`

| key | default | meaning |
|-----|---------|---------|
| `org` | *(none)* | **required** — org to search. Unset, the panel says so rather than sweeping all of GitHub |
| `scope` | `assigned` | `assigned` \| `created` \| `mentions` |
| `space_cwd` | `~/Projects` | cwd for the new space |
| `limit` | `100` | max issues fetched |
| `agent_kind` | `claude` | any kind from `herdr agent` |
| `body_limit` | `6000` | issue body chars before truncation |
| `agent_start_retry_seconds` | `20` | how long to wait out a pane whose shell is still coming up; see *When Enter does nothing* |
| `repo_paths` | `{}` | repo → directory override; see below |
| `refresh_seconds` | `120` | task pane auto-refresh interval; `0` disables |
| `split_direction` | `right` | `right` \| `down` — which way the task pane opens |
| `status_field` | `Status` | issue field published as `$issue_status` |
| `priority_field` | `Priority` | issue field published as `$issue_priority`; `""` turns it off |
| `meta_ttl_seconds` | `180` | how long sidebar task info is cached |
| `issue_type_emoji` | `false` | keep GitHub's emoji in `$issue_type` |
| `watch_seconds` | `120` | background re-fetch of every issue space; `0` turns the watcher off |
| `notify_status_change` | `true` | Herdr notification when a fetch sees a task's status move |
| `mouse` | `true` | clickable footer in the popup; `false` restores drag-to-select |

`repo_paths` is empty by default, which is right for the common case: the
directory under `space_cwd` is the repo's short name. Add an entry only when a
checkout's directory does not match its repo — a repo renamed upstream, or two
generations of the same project side by side:

```json
"repo_paths": { "your-org/backend": "backend-v2" }
```

Get it wrong and nothing errors; Claude is simply briefed in the wrong working
copy, which is why nothing is mapped for you.

## Uninstall

    herdr plugin uninstall evaldnet.gh-issues   # or `unlink` if you linked it
    # then delete the [[keys.command]] stanzas for prefix+shift+i and
    # prefix+shift+t from ~/.config/herdr/config.toml,
    # drop the $issue_status row from [ui.sidebar.agents], and run:
    # herdr server reload-config

## License

MIT — see [LICENSE](LICENSE).
