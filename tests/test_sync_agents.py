#!/usr/bin/env python3
"""bin/sync_agents.py: the patch that puts task status into Herdr's agents list.

The patch is anchored on Herdr's own code, which changes under it. These pin
what must happen when it does: a missing or doubled anchor stops the sync
instead of producing a half-patched list. With GHI_LIVE=1 the patch is also
applied, in memory, to the agents mod the installed Herdr app shipped.
"""
from __future__ import annotations

import os
import unittest

import harness

sync = harness.import_module("sync_agents")

# The shape of the shipped agents/main.js around the anchors, nothing more.
SHIPPED = '''import { View, div } from "gpui-kit";
import { agents, run } from "herdr";

export default class Agents extends View {
  init(_props, cx) {
    this.migratingPins = new Set();
    cx.timer.every(30000, (cx) => cx.notify());
  }
  render(cx) {
    const withKids = (a, i) => {
      const subs = collapsed ? [] : this.tree(all, a.address);
      const r = row(a, i, subs);
      if (this.folded.has(a.address)) return [r];
      return [r, ...subs.map((s) => this.subRow(t, s, shown, byPriority))];
    };
  }
}
'''


class PatchCase(unittest.TestCase):
    def test_patch_imports_starts_and_renders(self):
        out = sync.patch_source(SHIPPED)
        lines = out.splitlines()
        # The import lands after the last import, before any code.
        self.assertEqual(lines[2], sync.IMPORT.rstrip("\n"))
        self.assertIn("ghStatus(cx);", out)
        self.assertIn("return [r, ...gh, ...subs.map(", out)
        self.assertIn("if (this.folded.has(a.address)) return [r, ...gh];", out)
        self.assertEqual(out.count(sync.MARK), 3)

    def test_missing_anchor_stops_the_sync(self):
        changed = SHIPPED.replace("this.migratingPins = new Set();", "this.pins = new Set();")
        with self.assertRaises(sync.SyncError):
            sync.patch_source(changed)

    def test_doubled_anchor_stops_the_sync(self):
        doubled = SHIPPED.replace(
            "    cx.timer.every", "    this.migratingPins = new Set();\n    cx.timer.every")
        with self.assertRaises(sync.SyncError):
            sync.patch_source(doubled)

    def test_patching_a_patched_file_is_refused(self):
        with self.assertRaises(sync.SyncError):
            sync.patch_source(sync.patch_source(SHIPPED))

    def test_layout_swap_moves_only_the_agents_block(self):
        tree = {"type": "row", "children": [
            {"type": "block", "content": {"mod": "spaces"}},
            {"type": "block", "content": {"mod": "agents"}},
            {"type": "block", "content": {"mod": "tabs"}}]}
        self.assertEqual(sync.swap_agents(tree), 1)
        mods = [c["content"]["mod"] for c in tree["children"]]
        self.assertEqual(mods, ["spaces", sync.MOD, "tabs"])


@unittest.skipUnless(os.environ.get("GHI_LIVE"), "needs GHI_LIVE=1 and the Herdr app")
class LiveCase(unittest.TestCase):
    def test_installed_agents_mod_still_takes_the_patch(self):
        path = os.path.join(sync.APP_HOME, "mods", "agents", "main.js")
        with open(path, "r", encoding="utf-8") as fh:
            out = sync.patch_source(fh.read())
        self.assertEqual(out.count(sync.MARK), 3)


if __name__ == "__main__":
    unittest.main()
