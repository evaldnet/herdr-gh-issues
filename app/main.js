// GitHub Issues for the Herdr app: the views from config.json (issues
// assigned to me first), grouped by board column. Enter opens a space named
// `#<issue>` with Claude started on the view's command and the item's URL.
//
// The rows come from ../bin/list.py, the same Python the classic Herdr plugin
// and the Tern plugin use, so config.json stays the one source of truth:
// ~/.config/herdr/plugins/config/evaldnet.gh-issues/config.json
//
// Installed as a symlink, ~/.config/herdr-app/mods/gh-issues -> <repo>/app,
// which is how `..` below reaches the repo's bin/. Read-only on GitHub by
// design: nothing here writes to an issue or a PR.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input, Scrollbar, v_virtual_list, VirtualListScrollHandle } from "gpui-base";
import { exec, spaces, run, theme, props, closeOverlay, notify, appHome, home, shared, setShared } from "herdr";

// A GUI-launched app may not have Homebrew's gh on PATH; put it in front.
const PATH = "/opt/homebrew/bin:/usr/local/bin";
const HEAD_H = 28;
const ROW_H = 26;
// ponytail: a fixed list cap, sized to fit a 1080p window under the popup's
// 80% limit; window() reports no size to derive it from. Measure the popup
// with on_size_change if lists need to grow on taller screens.
const LIST_MAX = 600;
// Opening refetches a cached list older than OPEN_MS; Tab to another view
// reuses one younger than TAB_MS.
const OPEN_MS = 60 * 1000;
const TAB_MS = 5 * 60 * 1000;

// GitHub's own option colours (dark theme), as in the classic plugin's sidebar.
const STATUS = {
  "Pre Task Analysis": "#8b949e", "Not Started": "#8b949e", "In Development": "#d29922",
  "Validation": "#a371f7", "Validated OK": "#db61a2", "Ready for test": "#db6d28",
  "Test rejected": "#f85149", "Approved": "#3fb950", "Deployed": "#4493f8",
  "changes req": "#f85149", "review req": "#8b949e", "approved": "#3fb950",
};
const PRIORITY = {
  "Prod Stop": "#f85149", "Ekspederes": "#a371f7", "First": "#3fb950",
  "Second": "#d29922", "Third": "#db6d28", "Last": "#8b949e",
};
const REVIEW = { "approved": "✓", "changes req": "✗", "review req": "◷" };

// Kept across openings of the popup (module scope outlives its copies), so it
// opens on the last list at once and refreshes behind it. Plain data only.
const CACHE = new Map(); // view id -> { rows, err, at }
const META = { views: null, cwd: null, last: null };

function listPy(args) {
  const script = `${appHome()}/mods/gh-issues/../bin/list.py`;
  return exec("/bin/sh", ["-c", `PATH="${PATH}:$PATH" exec /usr/bin/python3 "$@"`, "sh", script, ...args], { timeout: 90000 });
}

async function runList(args) {
  let res;
  try {
    res = await listPy(args);
  } catch (e) {
    return { err: `list.py did not run: ${e}` };
  }
  try {
    return JSON.parse(res.stdout);
  } catch {
    return { err: `list.py failed: ${(res.stderr || res.stdout || `exit ${res.code}`).trim().slice(-300)}` };
  }
}

function clip(text, n) {
  return text.length > n ? text.slice(0, n - 1) + "…" : text;
}

// `#4510`, `core#4510`, `Mobilplan/core#4510` or a GitHub URL: something the
// org-wide lookup understands. A bare number is a filter first (it matches
// the visible rows) and a lookup only when nothing visible matches.
function lookupText(text) {
  const s = text.trim();
  if (/^#\d+$/.test(s)) return s.slice(1);
  if (/^[\w.-]+(\/[\w.-]+)?#\d+$/.test(s) || /github\.com\/.+\/(issues|pull)\/\d+/.test(s)) return s;
  return null;
}

export default class GhIssues extends View {
  init(_props, cx) {
    this.filter = "";
    this.at = 0;
    this.lookup = null; // { text, rows, err } while showing a lookup
    this.busy = new Set(); // view ids (or "lookup") being fetched
    this.view = META.last;
    this.scroll = VirtualListScrollHandle.new();
    this.handle = cx.focus_handle();
    this.input = InputState.new({ placeholder: "Filter · #4510, core#4510 or a URL finds any issue or PR" });
    this.input.on("change", (_e, cx) => {
      this.filter = this.input.value();
      this.at = 0;
      cx.notify();
    });
    this.input.on("submit", (_e, cx) => this.submit(cx));
    this.input.focus();
    // The popup takes focus as it opens; take it back once it has.
    cx.timer.after(600, () => this.input.focus());
    // The view list is re-read on every open, not once per app session: the
    // module outlives config.json edits, and a view added there would
    // otherwise stay hidden until the app restarts. list.py with no view is
    // local (~40ms), and a cached view starts refetching alongside it.
    cx.spawn(async (cx) => {
      const early = META.views && this.view ? this.load(this.view, cx, OPEN_MS) : null;
      const d = await runList([]);
      if (d.views || !META.views) META.views = d.views ?? [];
      META.cwd = d.space_cwd ?? META.cwd ?? "~/Projects";
      this.metaErr = d.err;
      if (!this.view || !META.views.some((v) => v.id === this.view)) this.view = META.views[0]?.id ?? null;
      cx.notify();
      if (early) await early;
      else if (this.view) await this.load(this.view, cx, OPEN_MS);
    });
  }

  close() {
    const id = props()?.overlay;
    if (id) closeOverlay(id);
  }

  // The cache shows at once; this fetches when it is missing or older than
  // maxAge (0: always).
  async load(id, cx, maxAge) {
    const hit = CACHE.get(id);
    if (hit && Date.now() - hit.at < maxAge) return;
    if (this.busy.has(id)) return;
    this.busy.add(id);
    cx.notify();
    const d = await runList([id]);
    this.busy.delete(id);
    CACHE.set(id, { rows: d.rows ?? [], err: d.err ?? "", at: Date.now() });
    if (d.space_cwd) META.cwd = d.space_cwd;
    cx.notify();
  }

  async find(text, cx) {
    this.busy.add("lookup");
    this.lookup = { text, rows: [], err: "" };
    cx.notify();
    const d = await runList(["--lookup", text]);
    this.busy.delete("lookup");
    if (this.lookup?.text !== text) return; // left the lookup meanwhile
    const found = d.rows ?? [];
    this.lookup = { text, rows: found, err: d.err || (found.length ? "" : `nothing found for ${text}`) };
    this.at = 0;
    this.input.set_value("");
    this.filter = "";
    // One hit is unambiguous: start it, as the Tern plugin does.
    if (this.lookup.rows.length === 1) return this.start(this.lookup.rows[0]);
    cx.notify();
  }

  switchView(step, cx) {
    const views = META.views ?? [];
    if (!views.length) return;
    const i = views.findIndex((v) => v.id === this.view);
    this.setView(views[(i + step + views.length) % views.length].id, cx);
  }

  setView(id, cx) {
    this.view = id;
    this.lookup = null;
    this.at = 0;
    cx.notify();
    cx.spawn(async (cx) => this.load(id, cx, TAB_MS));
  }

  // The rows on screen: a lookup's, or the view's, through the filter.
  rows() {
    const src = this.lookup ? this.lookup.rows : (CACHE.get(this.view)?.rows ?? []);
    const terms = this.filter.toLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.length || lookupText(this.filter)) return src;
    return src.filter((r) => {
      const hay = `${r.short} #${r.number} ${r.title} ${r.group} ${r.inline} ${r.author} ${r.labels.join(" ")}`.toLowerCase();
      return terms.every((w) => hay.includes(w));
    });
  }

  // Section headers between runs of one group; list.py already sorted them.
  items(rows) {
    const out = [];
    let group = null;
    rows.forEach((r, i) => {
      const g = r.group || "";
      if (!this.lookup && g !== group) {
        group = g;
        out.push({ kind: "head", key: `h:${g}`, group: g || "—", count: rows.filter((x) => (x.group || "") === g).length });
      }
      out.push({ kind: "row", key: `r:${r.repo}#${r.number}`, row: r, index: i });
    });
    return out;
  }

  submit(cx) {
    const want = lookupText(this.filter);
    const rows = this.rows();
    if (want) return this.find(want, cx);
    if (rows.length) return this.start(rows[Math.min(this.at, rows.length - 1)]);
    // A bare number nothing on screen matches: ask the whole org.
    if (/^\d+$/.test(this.filter.trim())) return this.find(this.filter.trim(), cx);
  }

  issueOf(row) {
    return row.is_pr ? row.issue : row.number;
  }

  spaceFor(issue) {
    const name = `#${issue}`;
    return spaces().find((s) => s.name === name || s.name.startsWith(`${name} `));
  }

  // One space per issue, named like the ones made by hand (`#4654`); a PR's
  // space is the issue it closes, since work is tracked on issues.
  start(row) {
    const issue = this.issueOf(row);
    if (!issue) {
      notify({ title: `PR #${row.number} closes no issue`, body: "Link one with `Fixes #n` in the PR body.", level: "warning" });
      return;
    }
    this.close();
    // The agents list (gh-agents) reads each issue space's status by this
    // map: a space name alone does not say which repo `#4884` is in.
    const repo = row.is_pr ? row.issue_repo : row.repo;
    if (repo) setShared("gh-issues.spaces", { ...(shared("gh-issues.spaces") ?? {}), [`#${issue}`]: `${repo}#${issue}` });
    const existing = this.spaceFor(issue);
    if (existing) {
      run("space.select", { id: existing.id });
      notify({ title: `#${issue} already has a space`, level: "info", timeout: 3000 });
      return;
    }
    const cwd = (META.cwd ?? "~/Projects").replace(/^~(?=\/|$)/, home());
    run("space.new", {
      name: `#${issue}`,
      folder: cwd,
      tab: clip(row.title, 60),
      argv: ["claude"],
      task: `${row.command} ${row.url}`,
    });
  }

  browse(row) {
    if (!row?.url) return;
    exec("/bin/sh", ["-c", 'command -v xdg-open >/dev/null 2>&1 && exec xdg-open "$0"; exec open "$0"', row.url]).catch(() => {});
  }

  move(step, items, rows) {
    if (!rows.length) return;
    this.at = Math.max(0, Math.min(rows.length - 1, this.at + step));
    const i = items.findIndex((it) => it.kind === "row" && it.index === this.at);
    // The header above the first row of a section comes into view with it.
    if (i >= 0) this.scroll.scroll_to_item(i > 0 && items[i - 1].kind === "head" ? i - 1 : i);
  }

  rowEl(it, t, sel, open) {
    if (it.kind === "head") {
      return h_flex().h(HEAD_H).px(12).pt(8).gap(8).items_center().text_size(11.5).font_semibold().text_color(t.dim)
        .child(div().size(8).rounded(4).bg(STATUS[it.group] ?? t.dim))
        .child(div().text_color(t.muted).child(it.group))
        .child(div().flex_1().h(1).bg(t.border))
        .child(div().child(String(it.count)));
    }
    const r = it.row;
    const on = it.index === sel;
    const issue = this.issueOf(r);
    const has = issue && open.has(`#${issue}`);
    const mark = r.is_pr ? (REVIEW[r.review] ?? " ") : "";
    const cell = r.inline || (this.lookup ? r.group : "");
    return h_flex().h(ROW_H).mx(4).px(8).gap(10).items_center().rounded(6).text_size(12.5)
      .when(on, (el) => el.bg(t.selected ?? t.hover))
      .child(div().w(10).flex_none().text_color(t.accent).child(has ? "●" : ""))
      .child(div().w(84).flex_none().truncate().text_color(t.dim).child(r.short))
      .child(div().w(52).flex_none().text_color(t.muted).child(`#${r.number}`))
      .when(r.is_pr, (el) => el.child(div().w(14).flex_none().text_color(STATUS[r.review] ?? t.dim).child(mark)))
      .when(Boolean(cell), (el) => el.child(div().w(96).flex_none().truncate().text_color(PRIORITY[cell] ?? STATUS[cell] ?? t.muted).child(cell)))
      .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).when(on, (d) => d.font_semibold()).child(r.title))
      .when(r.is_pr, (el) => el.child(div().flex_none().text_size(11).text_color(t.dim).child(r.issue ? `→ #${r.issue}` : "no issue")))
      .when(r.labels.length > 0, (el) => el.child(div().max_w(180).flex_none().truncate().text_size(11).text_color(t.dim).child(r.labels.join(", "))))
      .child(div().w(76).flex_none().text_size(11).text_color(t.dim).child(r.date || ""));
  }

  render() {
    const t = theme();
    const rows = this.rows();
    const items = this.items(rows);
    this.at = Math.max(0, Math.min(this.at, rows.length - 1));
    const sel = this.at;
    const open = new Set(spaces().map((s) => s.name.split(" ")[0]));
    const views = META.views ?? [];
    const hit = CACHE.get(this.view);
    const loading = this.busy.has(this.lookup ? "lookup" : this.view);
    const err = this.lookup ? this.lookup.err : (hit?.err || this.metaErr || "");
    const label = this.lookup ? `lookup · ${this.lookup.text}` : (views.find((v) => v.id === this.view)?.label ?? "");
    if (this.view) META.last = this.view;

    const chip = (v) => {
      const on = !this.lookup && v.id === this.view;
      return div().id(`ghi-view-${v.id}`).px(10).py(3).rounded(6).text_size(11.5).cursor_pointer()
        .text_color(on ? t.foreground : t.muted).when(on, (el) => el.bg(t.selected ?? t.hover))
        .hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => this.setView(v.id, cx))
        .child(v.label);
    };
    const key = (k, what) => h_flex().gap(4).child(div().text_color(t.muted).child(k)).child(div().child(what));

    let body;
    if (!rows.length) {
      const why = loading ? "loading…" : err ? "" : this.filter ? "nothing matches" : this.lookup ? "" : "nothing here";
      body = div().flex_1().p(16).text_color(t.dim).child(why);
    } else {
      const sizes = items.map((it) => (it.kind === "head" ? HEAD_H : ROW_H));
      const total = sizes.reduce((a, b) => a + b, 0);
      // The popup sizes itself to its content and a virtual list has no height
      // of its own, so it gets one: all rows, up to LIST_MAX.
      body = div().h(Math.min(total, LIST_MAX)).flex_none().relative()
        .child(v_virtual_list("ghi-rows", items.length, sizes,
          (i) => items[i].key,
          (range) => items.slice(range.start, range.end).map((it) => this.rowEl(it, t, sel, open)))
          .size_full()
          .track_scroll(this.scroll)
          .on_item_click((k, cx) => {
            const it = items.find((x) => x.key === k);
            if (!it || it.kind !== "row") return;
            // First click selects, a click on the selected row starts it.
            if (it.index === this.at) return this.start(it.row);
            this.at = it.index;
            cx.notify();
          }))
        .child(Scrollbar.vertical("ghi-rows").viewport_from_layout().absolute().inset_0());
    }

    return v_flex()
      .id("ghi")
      .track_focus(this.handle)
      .size_full()
      .min_h(0)
      .p(14)
      .gap(10)
      .bg(t.popover)
      .text_size(13)
      .on_key_down((e, cx) => {
        const k = e.keystroke;
        if (k === "escape") {
          if (this.lookup) {
            this.lookup = null;
            this.at = 0;
          } else return this.close();
        } else if (k === "down" || k === "ctrl-n" || k === "ctrl-j") this.move(1, items, rows);
        else if (k === "up" || k === "ctrl-p" || k === "ctrl-k") this.move(-1, items, rows);
        else if (k === "pagedown") this.move(10, items, rows);
        else if (k === "pageup") this.move(-10, items, rows);
        else if (k === "tab") this.switchView(1, cx);
        else if (k === "shift-tab") this.switchView(-1, cx);
        else if (k === "cmd-o") this.browse(rows[sel]);
        else if (k === "cmd-r") {
          if (this.lookup) cx.spawn(async (cx) => this.find(this.lookup.text, cx));
          else cx.spawn(async (cx) => this.load(this.view, cx, 0));
        } else return;
        cx.stop_propagation();
        cx.notify();
      })
      .child(h_flex().items_center().gap(10)
        .child(div().text_size(15).font_semibold().text_color(t.foreground).child("GitHub Issues"))
        .child(div().text_size(12).text_color(t.dim).child(label))
        .child(div().text_size(12).text_color(t.dim).child(rows.length ? String(rows.length) : ""))
        .when(loading, (el) => el.child(div().text_color(t.working).frames(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"], 90)))
        .child(div().flex_1())
        .child(div().text_size(11).text_color(t.dim).child("Esc")))
      .child(h_flex().gap(4).flex_wrap().children(views.map(chip)))
      .child(h_flex().h(34).flex_none().px(10).gap(8).items_center().rounded(8).border_1().border_color(t.accent).bg(t.background)
        .child(Input.new(this.input).flex_1().h_full().text_size(12.8).text_color(t.foreground)))
      .when(Boolean(err), (el) => el.child(div().px(4).text_size(12).text_color(t.blocked).child(err)))
      .child(body)
      .child(h_flex().flex_none().gap(14).pt(6).border_t(1).border_color(t.border).text_size(11).text_color(t.dim)
        .child(key("⏎", "start"))
        .child(key("↑↓", "move"))
        .child(key("⇥", "view"))
        .child(key("#n", "find"))
        .child(key("⌘O", "browser"))
        .child(key("⌘R", "refresh"))
        .child(div().flex_1())
        .child(div().text_color(t.accent).child("●"))
        .child(div().child("has a space")));
  }
}
