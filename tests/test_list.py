#!/usr/bin/env python3
"""bin/list.py: the JSON the Herdr app mod and the Tern plugin draw from.

Run as a subprocess, the way both front ends call it, so the contract under
test is the one they actually see: argv in, one JSON object out.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import harness

LIST = os.path.join(harness.BIN, "list.py")
MOD = os.path.join(harness.PLUGIN_ROOT, "app", "main.js")
ROW_KEYS = {"repo", "short", "number", "issue", "issue_repo", "title", "url", "is_pr",
            "group", "inline", "labels", "date", "author", "review", "command"}


class ListCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ghi-list-")
        cls.env = harness.fake_env(cls.tmp)
        harness.write_config(cls.env, harness.plugin_config())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_list(self, *args, script=LIST):
        p = subprocess.run([sys.executable, script] + list(args), env=self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_no_view_lists_the_views_without_fetching(self):
        d = self.run_list()
        self.assertEqual([v["id"] for v in d["views"]][:2], ["assigned", "approved"])
        self.assertIn("space_cwd", d)
        self.assertNotIn("rows", d)

    def test_view_rows_carry_every_key_the_front_ends_read(self):
        d = self.run_list("assigned")
        self.assertEqual(d["err"], "")
        self.assertTrue(d["rows"])
        for r in d["rows"]:
            self.assertEqual(set(r), ROW_KEYS)
            self.assertEqual(r["command"], "/github-issue")

    def test_groups_are_contiguous(self):
        """The mod draws a section header wherever the group changes, so a
        group split in two would draw its header twice."""
        groups = [r["group"] for r in self.run_list("assigned")["rows"]]
        runs = [g for i, g in enumerate(groups) if i == 0 or groups[i - 1] != g]
        self.assertEqual(len(runs), len(set(runs)), groups)

    def test_pr_rows_resolve_the_issue_they_close(self):
        d = self.run_list("review")
        self.assertEqual(d["err"], "")
        rows = d["rows"]
        self.assertTrue(all(r["is_pr"] for r in rows))
        self.assertEqual(rows[0]["issue"], 210)
        self.assertEqual(rows[0]["issue_repo"], "evaldnet/platform")
        # No closing issue: the mod refuses to start it rather than guess.
        self.assertIsNone(rows[1]["issue"])
        self.assertIsNone(rows[1]["issue_repo"])
        self.assertEqual(rows[0]["command"], "/github-review")

    def test_pr_rows_carry_the_review_decision(self):
        rows = self.run_list("review")["rows"]
        self.assertEqual(rows[0]["review"], "review req")
        self.assertEqual(rows[1]["review"], "changes req")

    def test_issue_rows_are_their_own_issue(self):
        rows = self.run_list("assigned")["rows"]
        self.assertTrue(all(r["issue_repo"] == r["repo"] for r in rows))

    # ------------------------------------------------------------ --status
    def status(self, *refs):
        return self.run_list("--status", *refs)

    def test_status_reads_the_tokens_the_classic_sidebar_showed(self):
        d = self.status("platform#122", "evaldnet/platform#9", "4510")
        self.assertEqual(d["err"], "")
        s = d["status"]["platform#122"]
        self.assertEqual((s["repo"], s["number"]), ("evaldnet/platform", 122))
        self.assertEqual(s["status"], "In Development")
        self.assertEqual(s["priority"], "First")
        # GitHub's emoji prefix is dropped, as issue_meta does.
        self.assertEqual(s["type"], "Bugfix")

    def test_closed_outranks_the_board_column(self):
        s = self.status("platform#122", "evaldnet/platform#9", "4510")["status"]
        self.assertEqual(s["evaldnet/platform#9"]["status"], "closed")

    def test_bare_number_resolves_to_the_one_issue_that_has_it(self):
        """lookup_org has #4510 as a merged PR in mobile and an open issue in
        platform: a space named by hand means the issue."""
        s = self.status("platform#122", "evaldnet/platform#9", "4510")["status"]
        self.assertEqual((s["4510"]["repo"], s["4510"]["number"]), ("evaldnet/platform", 4510))
        self.assertEqual(s["4510"]["status"], "Not Started")

    def test_missing_issue_is_an_error_entry(self):
        s = self.status("platform#122", "platform#9", "platform#4510", "platform#404")["status"]
        self.assertEqual(s["platform#404"]["err"], "evaldnet/platform#404 not found")

    def test_unparseable_ref_is_an_error_entry(self):
        s = self.status("fix login")["status"]
        self.assertIn("not an issue ref", s["fix login"]["err"])

    def test_failed_request_sends_no_values(self):
        """The front end keeps the last good values; blanks would wipe every row."""
        env = dict(self.env, GH_STUB_FAIL="1")
        p = subprocess.run([sys.executable, LIST, "--status", "platform#122"], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
        d = json.loads(p.stdout)
        self.assertTrue(d["err"])
        self.assertEqual(d["status"], {})

    def test_status_carries_the_watcher_settings(self):
        d = self.status("platform#122")
        self.assertEqual(d["watch_seconds"], 120)
        self.assertIs(d["notify_status_change"], True)

    def test_unknown_view_is_named_not_empty(self):
        d = self.run_list("nope")
        self.assertIn("nope", d["err"])
        self.assertNotIn("rows", d)

    def test_lookup_rows_have_no_group(self):
        d = self.run_list("--lookup", "platform#122")
        self.assertTrue(d["rows"], d)
        self.assertTrue(all(r["group"] == "" for r in d["rows"]))

    def test_runs_through_a_symlink(self):
        """The Tern plugin links bin/list.py into its own folder; panel.py
        must still be found next to the real file."""
        link = os.path.join(self.tmp, "linked-list.py")
        if not os.path.exists(link):
            os.symlink(LIST, link)
        self.assertTrue(self.run_list(script=link)["views"])

    def test_mod_reads_only_keys_list_py_emits(self):
        """A key the mod reads that list.py never sends is undefined in JS --
        silently blank, not an error -- so pin the contract here."""
        with open(MOD, "r", encoding="utf-8") as fh:
            src = fh.read()
        used = set(re.findall(r"\b(?:r|row|it\.row)\.([a-z_]+)\b", src))
        self.assertTrue(used)
        self.assertEqual(used - ROW_KEYS, set())


if __name__ == "__main__":
    unittest.main()
