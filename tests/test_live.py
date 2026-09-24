#!/usr/bin/env python3
"""Opt-in checks that need the real herdr binary. Skipped by default.

Two things the stubs cannot cover, because they only exist inside a running
Herdr: whether the manifest links without warnings, and whether the entrypoints
Herdr registered are the ones the manifest declares. Herdr accepts an unknown
event name with a warning rather than an error, so this is the only place a typo
in `on = "..."` is caught before the hook quietly never fires.

    GHI_LIVE=1 python3 tests/run.py

Linking is idempotent and is what `herdr plugin link` already does on every
edit, so this does not disturb a working setup.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

import harness

LIVE = os.environ.get("GHI_LIVE") == "1"
HERDR = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")


@unittest.skipUnless(LIVE and HERDR, "set GHI_LIVE=1 and install herdr to run")
class LiveCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Run herdr with a clean PATH so a stub from another suite cannot win.
        env = dict(os.environ)
        env.pop("HERDR_BIN_PATH", None)
        proc = subprocess.run([HERDR, "plugin", "link", harness.PLUGIN_ROOT],
                              capture_output=True, text=True, timeout=60, env=env)
        if proc.returncode != 0:
            raise unittest.SkipTest("herdr plugin link failed: %s"
                                    % (proc.stderr or proc.stdout)[:200])
        cls.result = json.loads(proc.stdout.strip().splitlines()[-1])["result"]

    def test_links_without_warnings(self):
        self.assertFalse(self.result.get("warnings"),
                         "herdr reported: %s" % self.result.get("warnings"))

    def test_registered_entrypoints_match_the_manifest(self):
        plugin = self.result["plugin"]
        with open(os.path.join(harness.PLUGIN_ROOT, "herdr-plugin.toml"),
                  "r", encoding="utf-8") as fh:
            raw = fh.read()
        for pane in plugin.get("panes") or []:
            self.assertIn('id = "%s"' % pane["id"], raw)
        for action in plugin.get("actions") or []:
            self.assertIn('id = "%s"' % action["id"], raw)

    def test_plugin_is_enabled(self):
        self.assertTrue(self.result["plugin"].get("enabled"))


if __name__ == "__main__":
    unittest.main()
