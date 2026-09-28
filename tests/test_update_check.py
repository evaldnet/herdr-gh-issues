#!/usr/bin/env python3
"""The once-a-day "newer release available" hint."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import harness


class UpdateCheckCase(unittest.TestCase):
    REPO = "evaldnet/herdr-gh-issues"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ghi-update-")
        cls.env = harness.fake_env(cls.tmp)
        os.environ.update(cls.env)
        harness.write_config(cls.env, harness.plugin_config())
        cls.m = harness.import_module("update_check")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.state = tempfile.mkdtemp(dir=self.tmp)
        os.environ.pop("GH_STUB_LATEST", None)
        os.environ.pop("GH_STUB_LOG", None)

    def tearDown(self):
        os.environ.pop("GH_STUB_LATEST", None)
        os.environ.pop("GH_STUB_LOG", None)

    def test_versions_compare_numerically_not_as_strings(self):
        p = self.m.parse_version
        self.assertGreater(p("v1.10.0"), p("1.9.0"))
        self.assertEqual(p("v1.5.0"), p("1.5.0"))
        self.assertEqual(p("nightly"), ())

    def test_installed_version_comes_from_the_manifest(self):
        self.assertRegex(self.m.installed_version(harness.PLUGIN_ROOT), r"^\d+\.\d+\.\d+$")

    def test_newer_release_is_reported(self):
        os.environ["GH_STUB_LATEST"] = "v9.0.0"
        self.m.refresh(self.state, self.REPO, 3600)
        self.assertEqual(self.m.newer_release(self.state, "1.5.0"), "9.0.0")

    def test_same_or_older_release_says_nothing(self):
        os.environ["GH_STUB_LATEST"] = "v1.5.0"
        self.m.refresh(self.state, self.REPO, 3600)
        self.assertEqual(self.m.newer_release(self.state, "1.5.0"), "")
        self.assertEqual(self.m.newer_release(self.state, "2.0.0"), "")

    def test_a_fresh_cache_makes_no_request(self):
        os.environ["GH_STUB_LATEST"] = "v9.0.0"
        self.m.refresh(self.state, self.REPO, 3600, now=1000)
        log = os.path.join(self.state, "gh.log")
        os.environ["GH_STUB_LOG"] = log
        self.m.refresh(self.state, self.REPO, 3600, now=1000 + 60)
        self.assertFalse(os.path.exists(log))
        self.m.refresh(self.state, self.REPO, 3600, now=1000 + 3601)
        self.assertTrue(os.path.exists(log))

    def test_a_failed_check_keeps_the_last_answer(self):
        os.environ["GH_STUB_LATEST"] = "v9.0.0"
        self.m.refresh(self.state, self.REPO, 3600, now=1000)
        os.environ.pop("GH_STUB_LATEST")
        cache = self.m.refresh(self.state, self.REPO, 3600, now=9000)
        self.assertEqual(cache["latest"], "v9.0.0")
        self.assertEqual(cache["checked"], 9000)

    def test_switched_off_starts_nothing(self):
        self.assertIsNone(self.m.start({"update_check": False, "update_repo": self.REPO},
                                       self.state))

    def test_footer_tag_never_pushes_the_menu_off_for_a_bare_version(self):
        tag = self.m.footer_tag
        self.assertEqual(tag("1.6.0", "", 80, 40), " v1.6.0 ")
        self.assertEqual(tag("1.6.0", "", 64, 57), "")
        # An update is worth clipping the menu for...
        self.assertEqual(tag("1.6.0", "1.7.0", 64, 57), " v1.6.0 ↑1.7.0 ")
        # ...but not on a pane so narrow it would take over the line.
        self.assertEqual(tag("1.6.0", "1.7.0", 30, 0), "")
        self.assertEqual(tag("", "1.7.0", 80, 0), "")

    def pane_footer(self, cols, latest=""):
        env = dict(self.env, HERDR_WORKSPACE_ID="wT1", HERDR_PANE_ID="wT1:p9",
                   HERDR_PLUGIN_STATE_DIR=tempfile.mkdtemp(dir=self.tmp))
        if latest:
            env["GH_STUB_LATEST"] = latest
        return harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "task_pane.py")],
            env, cols=cols, rows=30, seconds=6.0)[-1].rstrip()

    def test_task_pane_shows_the_version_when_it_fits(self):
        version = self.m.installed_version(harness.PLUGIN_ROOT)
        self.assertTrue(self.pane_footer(90).endswith("v%s" % version))

    def test_task_pane_keeps_its_keys_over_a_bare_version(self):
        foot = self.pane_footer(64)
        self.assertIn("q quit", foot)
        self.assertNotIn(" v1.", foot)

    def test_task_pane_flags_a_newer_release(self):
        self.assertRegex(self.pane_footer(64, "v9.0.0"), r"↑9\.0\.0$")

    def test_screen_shows_the_newer_release(self):
        env = dict(self.env, GH_STUB_LATEST="v9.0.0")
        rows = harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "panel.py")],
            env, cols=110, rows=24, seconds=8.0)
        self.assertIn("herdr plugin install %s" % self.REPO, rows[-1])
        self.assertRegex(rows[-1], r"v\d+\.\d+\.\d+ ↑9\.0\.0\s*$")

    def test_screen_is_quiet_when_current(self):
        rows = harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "panel.py")],
            dict(self.env, HERDR_PLUGIN_STATE_DIR=self.state), cols=110, rows=24,
            seconds=8.0)
        self.assertIn("q quit", rows[-1])
        self.assertNotIn("↑", rows[-1])
        version = self.m.installed_version(harness.PLUGIN_ROOT)
        self.assertTrue(rows[-1].rstrip().endswith("v%s" % version), rows[-1])


if __name__ == "__main__":
    unittest.main()
