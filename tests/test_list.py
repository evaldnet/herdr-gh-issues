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
ROW_KEYS = {"repo", "short", "number", "issue", "title", "url", "is_pr", "group",
            "inline", "labels", "date", "author", "review", "command"}


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
        # No closing issue: the mod refuses to start it rather than guess.
        self.assertIsNone(rows[1]["issue"])
        self.assertEqual(rows[0]["command"], "/github-review")

    def test_pr_rows_carry_the_review_decision(self):
        rows = self.run_list("review")["rows"]
        self.assertEqual(rows[0]["review"], "review req")
        self.assertEqual(rows[1]["review"], "changes req")

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
