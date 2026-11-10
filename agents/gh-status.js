// Task status under each agent in an issue space, and the background watcher,
// for gh-agents: Herdr's shipped agents list cloned and patched by
// bin/sync_agents.py, which copies this file into the clone. Edit it here and
// re-run `make sync-agents`.
//
// The watcher: every `watch_seconds` (config.json; 0 = off) it re-reads the
// status of every issue space in one request and, when one has moved, posts a
// toast like the classic plugin's: `core#4907: In Development → Validation`.
// A space that appears is read at once, not on the next round.
//
// Issue spaces are the ones named `#<n>`. The popup (mods/gh-issues) records
// which repo each one's issue is in under shared "gh-issues.spaces"; a space
// named by hand is looked up across the org once and then recorded the same way.

import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { exec, spaces, shared, setShared, notify, appHome, readTextFile } from "herdr";

const PATH = "/opt/homebrew/bin:/usr/local/bin";
const STATUS_KEY = "gh-issues.status"; // "#4907" -> { repo, number, status, priority, type }
const SPACES_KEY = "gh-issues.spaces"; // "#4907" -> "Mobilplan/core#4907"
const TICK_MS = 30 * 1000;

// GitHub's own option colours (dark theme), as in the classic plugin's sidebar.
const STATUS = {
  "Pre Task Analysis": "#8b949e", "Not Started": "#8b949e", "In Development": "#d29922",
  "Validation": "#a371f7", "Validated OK": "#db61a2", "Ready for test": "#db6d28",
  "Test rejected": "#f85149", "Approved": "#3fb950", "Deployed": "#4493f8", "closed": "#6272a4",
};
const PRIORITY = {
  "Prod Stop": "#f85149", "Ekspederes": "#a371f7", "First": "#3fb950",
  "Second": "#d29922", "Third": "#db6d28", "Last": "#8b949e",
};

// Deliberately module scope: copies of a mod share it, so however many
// copies the layout holds, one fetch runs at a time and one toast goes out.
const W = { busy: false, sig: null, at: 0, watchMs: 120 * 1000, notify: true, warned: false };

function issueKey(name) {
  const m = /^#(\d+)\b/.exec(name || "");
  return m ? `#${m[1]}` : null;
}

async function listStatus(refs) {
  const script = `${appHome()}/mods/gh-issues/../bin/list.py`;
  let res;
  try {
    res = await exec("/bin/sh", ["-c", `PATH="${PATH}:$PATH" exec /usr/bin/python3 "$@"`, "sh", script, "--status", ...refs], { timeout: 90000 });
  } catch (e) {
    return { err: `list.py did not run: ${e}` };
  }
  try {
    return JSON.parse(res.stdout);
  } catch {
    return { err: `list.py failed: ${(res.stderr || res.stdout || "").trim().slice(-200)}` };
  }
}

function refresh(cx) {
  if (W.busy) return;
  const names = [...new Set(spaces().map((s) => issueKey(s.name)).filter(Boolean))];
  const sig = [...names].sort().join(",");
  const due = W.watchMs > 0 && Date.now() - W.at >= W.watchMs;
  if (sig === W.sig && !due) return;
  W.sig = sig;
  if (!names.length) return;
  W.busy = true;
  W.at = Date.now();
  cx.spawn(async () => {
    try {
      const map = { ...(shared(SPACES_KEY) ?? {}) };
      const refs = names.map((n) => map[n] ?? n.slice(1));
      const d = await listStatus(refs);
      if (typeof d.watch_seconds === "number") W.watchMs = d.watch_seconds * 1000;
      W.notify = d.notify_status_change !== false;
      // A failed request keeps the last good values rather than blanking rows.
      if (d.err) return;
      const prev = shared(STATUS_KEY) ?? {};
      const next = {};
      let mapped = false;
      names.forEach((n, i) => {
        const s = d.status?.[refs[i]];
        if (!s || s.err) {
          if (prev[n]) next[n] = prev[n];
          return;
        }
        next[n] = { repo: s.repo, number: s.number, status: s.status, priority: s.priority, type: s.type };
        const ref = `${s.repo}#${s.number}`;
        if (map[n] !== ref) {
          map[n] = ref;
          mapped = true;
        }
        const old = prev[n]?.status;
        if (W.notify && old && s.status && old !== s.status) {
          notify({ title: `${s.repo.split("/").pop()}#${s.number}: ${old} → ${s.status}`, body: s.title, level: "info" });
        }
      });
      if (mapped) setShared(SPACES_KEY, map);
      if (JSON.stringify(next) !== JSON.stringify(prev)) setShared(STATUS_KEY, next);
    } finally {
      W.busy = false;
    }
  });
}

// The clone is frozen at the Herdr version it was made from. Say so once per
// start when Herdr's own agents list or main layout has moved on since.
function checkSync(cx) {
  if (W.warned) return;
  W.warned = true;
  cx.spawn(async () => {
    try {
      const installed = JSON.parse(await readTextFile(`${appHome()}/installed.json`));
      const synced = JSON.parse(await readTextFile(`${appHome()}/mods/gh-agents/.synced.json`));
      const behind = ["agents", "layout:main"].filter((k) => JSON.stringify(installed[k]) !== JSON.stringify(synced[k]));
      if (behind.length) {
        notify({
          title: "Agents list is behind Herdr",
          body: `Herdr updated ${behind.join(" and ")}. Run \`make sync-agents\` in herdr-gh-issues to rebuild gh-agents.`,
          level: "warning",
        });
      }
    } catch {}
  });
}

// Starts the watcher for this copy (call from init).
export function ghStatus(cx) {
  checkSync(cx);
  refresh(cx);
  cx.timer.every(TICK_MS, (cx) => refresh(cx));
}

// The status lines under one agent row: Task Status · type, then priority.
// Nothing for an agent outside an issue space, or before the first fetch.
export function ghStatusRow(t, a) {
  const n = issueKey(a.space_name);
  const s = n && (shared(STATUS_KEY) ?? {})[n];
  if (!s || !(s.status || s.type || s.priority)) return [];
  return [
    v_flex().pl(33).pr(8).pb(5).mt(-3).gap(1).text_size(11)
      .child(h_flex().gap(5).min_w(0)
        .when(Boolean(s.status), (el) => el.child(div().flex_none().text_color(STATUS[s.status] ?? t.muted).child(s.status)))
        .when(Boolean(s.status && s.type), (el) => el.child(div().flex_none().text_color(t.dim).child("·")))
        .when(Boolean(s.type), (el) => el.child(div().min_w(0).truncate().text_color(t.dim).child(s.type))))
      .when(Boolean(s.priority), (el) => el.child(div().text_color(PRIORITY[s.priority] ?? t.dim).child(s.priority))),
  ];
}
