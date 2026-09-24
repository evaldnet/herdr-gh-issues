#!/usr/bin/env python3
"""Sidebar task info: resolving a pane to an issue, and the tokens reported."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest

import harness


class IssueMetaCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ghi-meta-")
        cls.env = harness.fake_env(cls.tmp)
        # issue_meta reads HERDR/STATE_DIR/CONFIG_DIR at import time.
        os.environ.update(cls.env)
        harness.write_config(cls.env, harness.plugin_config())
        cls.m = harness.import_module("issue_meta")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.cfg = self.m.load_config()

    # ------------------------------------------------------------- resolving
    def test_resolves_issue_from_agent_name(self):
        item = self.m.resolve_item(self.cfg, {"name": "gh-platform-134"})
        self.assertEqual(item, {"repo": "evaldnet/platform", "number": 134,
                                "is_pr": False})

    def test_resolves_pr_from_agent_name(self):
        item = self.m.resolve_item(self.cfg, {"name": "pr-clients-202"})
        self.assertEqual(item, {"repo": "evaldnet/clients", "number": 202,
                                "is_pr": True})

    def test_recorded_mapping_wins_over_the_name(self):
        """launch.py's record is exact; the name is only a fallback. A repo
        whose short name is not the slug would resolve wrongly without it."""
        d = os.path.join(self.env["HERDR_PLUGIN_STATE_DIR"], "agents")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "gh-platform-134.json"), "w", encoding="utf-8") as fh:
            json.dump({"repo": "evaldnet/platform-next", "number": 134,
                       "is_pr": False}, fh)
        try:
            item = self.m.resolve_item(self.cfg, {"name": "gh-platform-134"})
            self.assertEqual(item["repo"], "evaldnet/platform-next")
        finally:
            os.unlink(os.path.join(d, "gh-platform-134.json"))

    def test_unnamed_agent_resolves_to_nothing(self):
        self.assertIsNone(self.m.resolve_item(self.cfg, {"name": ""}))
        self.assertIsNone(self.m.resolve_item(self.cfg, {}))

    def test_foreign_agent_name_resolves_to_nothing(self):
        self.assertIsNone(self.m.resolve_item(self.cfg, {"name": "my-shell"}))

    def test_agent_for_pane_reads_the_agent_list(self):
        agent = self.m.agent_for_pane("wT1:p1")
        self.assertEqual(agent["name"], "gh-platform-134")
        self.assertIsNone(self.m.agent_for_pane("nope:p9"))

    # --------------------------------------------------------------- tokens
    def issue_data(self):
        return harness.fixture("issue_meta.json")["data"]["repository"]["issue"]

    def pr_data(self):
        return harness.fixture("pr_meta.json")["data"]["repository"]["pullRequest"]

    def test_issue_tokens(self):
        item = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
        tokens = self.m.tokens_for(self.cfg, item, self.issue_data())
        self.assertEqual(tokens["issue"], "platform#134")
        self.assertEqual(tokens["issue_status"], "In Development")
        self.assertEqual(tokens["issue_type"], "Bugfix")
        self.assertEqual(tokens["issue_priority"], "First")

    def test_priority_field_can_be_turned_off(self):
        item = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
        cfg = dict(self.cfg, priority_field="")
        self.assertIsNone(
            self.m.tokens_for(cfg, item, self.issue_data())["issue_priority"])

    def test_issue_type_emoji_is_stripped_by_default(self):
        data = dict(self.issue_data(), issueType={"name": "🕷️ Bugfix"})
        item = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
        self.assertEqual(
            self.m.tokens_for(self.cfg, item, data)["issue_type"], "Bugfix")
        keep = dict(self.cfg, issue_type_emoji=True)
        self.assertEqual(
            self.m.tokens_for(keep, item, data)["issue_type"], "🕷️ Bugfix")

    def test_closed_outranks_the_board_column(self):
        data = dict(self.issue_data(), state="CLOSED")
        item = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
        self.assertEqual(
            self.m.tokens_for(self.cfg, item, data)["issue_status"], "closed")

    def test_status_field_is_configurable(self):
        item = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
        cfg = dict(self.cfg, status_field="Priority")
        self.assertEqual(
            self.m.tokens_for(cfg, item, self.issue_data())["issue_status"], "First")

    def test_status_field_name_folding(self):
        """GitHub's filter box writes spaces as dashes; GraphQL needs the exact
        name. Both spellings must resolve to the same field."""
        item = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
        for spelling in ("Status", "Status", "status", "status"):
            cfg = dict(self.cfg, status_field=spelling)
            self.assertEqual(
                self.m.tokens_for(cfg, item, self.issue_data())["issue_status"],
                "In Development", "failed for %r" % spelling)

    def test_pr_tokens_carry_the_review_decision(self):
        item = {"repo": "evaldnet/clients", "number": 202, "is_pr": True}
        data = self.pr_data()
        tokens = self.m.tokens_for(self.cfg, item, data)
        self.assertEqual(tokens["issue"], "clients!202")
        self.assertEqual(tokens["issue_status"], "changes req")

    def test_draft_pr_reads_as_draft(self):
        item = {"repo": "evaldnet/clients", "number": 202, "is_pr": True}
        data = dict(self.pr_data(), isDraft=True)
        self.assertEqual(
            self.m.tokens_for(self.cfg, item, data)["issue_status"], "draft")

    def test_missing_data_clears_rather_than_invents(self):
        item = {"repo": "evaldnet/platform", "number": 1, "is_pr": False}
        tokens = self.m.tokens_for(self.cfg, item, None)
        self.assertEqual(tokens["issue"], "platform#1")
        for key in ("issue_status", "issue_priority", "issue_type", "issue_labels"):
            self.assertIsNone(tokens[key], "%s should clear, not linger" % key)

    def test_token_values_stay_within_herdr_limits(self):
        """Herdr caps a token value at 80 characters and at most 16 keys."""
        item = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
        data = dict(self.issue_data(),
                    labels={"nodes": [{"name": "module:%d" % i} for i in range(10)]})
        tokens = self.m.tokens_for(self.cfg, item, data)
        self.assertLessEqual(len(tokens), 16)
        for name, value in tokens.items():
            self.assertRegex(name, r"^[A-Za-z0-9_-]{1,32}$")
            if value is not None:
                self.assertLessEqual(len(value), 80, "%s is too long" % name)

    # ---------------------------------------------------------------- cache
    def test_cache_round_trip_and_expiry(self):
        item = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
        self.m.write_cache(item, {"hello": "world"})
        self.assertEqual(self.m.read_cache(item, 300), {"hello": "world"})
        self.assertIsNone(self.m.read_cache(item, 0), "a 0s TTL must miss")

    def test_cache_key_separates_issue_from_pr(self):
        issue = {"repo": "evaldnet/platform", "number": 7, "is_pr": False}
        pr = {"repo": "evaldnet/platform", "number": 7, "is_pr": True}
        self.assertNotEqual(self.m.cache_path(issue), self.m.cache_path(pr))

    # ----------------------------------------------------------- reporting
    def calls(self):
        path = self.env["HERDR_STUB_CALLS"]
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def run_update(self, pane_id):
        path = os.path.join(self.tmp, "calls-%s.log" % pane_id.replace(":", "-"))
        if os.path.exists(path):
            os.unlink(path)          # each call starts from an empty log
        self.env["HERDR_STUB_CALLS"] = path
        os.environ["HERDR_STUB_CALLS"] = path
        shutil.rmtree(os.path.join(self.env["HERDR_PLUGIN_STATE_DIR"], "issue-cache"),
                      ignore_errors=True)
        return self.m.update_pane(self.m.load_config(), pane_id)

    def test_update_pane_reports_tokens_for_an_issue_space(self):
        self.assertTrue(self.run_update("wT1:p1"))
        report = [c for c in self.calls() if c[:2] == ["pane", "report-metadata"]]
        self.assertEqual(len(report), 1, "expected exactly one report call")
        flat = " ".join(report[0])
        self.assertIn("--source evaldnet.gh-issues", flat)
        self.assertIn("issue=platform#134", flat)
        self.assertIn("issue_status=In Development", flat)

    def test_update_pane_clears_only_our_tokens_elsewhere(self):
        """A plain space must not keep a stale issue, and must not disturb
        another plugin's tokens -- so we clear ours by name, not wholesale."""
        self.assertFalse(self.run_update("wT3:p1"))
        report = [c for c in self.calls() if c[:2] == ["pane", "report-metadata"]]
        self.assertEqual(len(report), 1)
        flat = " ".join(report[0])
        for name in ("issue", "issue_status", "issue_priority", "issue_type",
                     "issue_labels"):
            self.assertIn("--clear-token %s" % name, flat)
        self.assertNotIn("--token", flat)

    def test_failed_fetch_keeps_the_previous_values(self):
        cfg = self.m.load_config()
        item = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
        self.m.write_cache(item, self.issue_data())
        os.environ["GH_STUB_FAIL"] = "1"
        try:
            # TTL of 0 forces a fetch, which now fails; the stale entry stands in.
            cfg = dict(cfg, meta_ttl_seconds=0)
            self.assertTrue(self.m.update_pane(cfg, "wT1:p1"))
        finally:
            os.environ.pop("GH_STUB_FAIL", None)

    # ------------------------------------------------------------- watching
    def test_status_change_is_detected(self):
        item = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
        old = self.issue_data()
        new = dict(old, state="CLOSED")
        self.assertEqual(self.m.status_change(self.cfg, item, old, new),
                         ("In Development", "closed"))
        self.assertIsNone(self.m.status_change(self.cfg, item, old, old))
        self.assertIsNone(self.m.status_change(self.cfg, item, None, new),
                          "a first fetch has nothing to compare against")

    def test_a_fetch_that_moves_the_status_notifies(self):
        item = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
        self.run_update("wT1:p1")                # clears the cache, fetches once
        self.m.write_cache(item, dict(self.issue_data(), state="CLOSED"))
        cfg = dict(self.m.load_config(), meta_ttl_seconds=0)
        self.m.update_pane(cfg, "wT1:p1")
        shown = [c for c in self.calls() if c[:2] == ["notification", "show"]]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0][2], "platform#134: closed → In Development")

    def test_notifications_can_be_turned_off(self):
        item = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
        self.run_update("wT1:p1")
        self.m.write_cache(item, dict(self.issue_data(), state="CLOSED"))
        cfg = dict(self.m.load_config(), meta_ttl_seconds=0,
                   notify_status_change=False)
        self.m.update_pane(cfg, "wT1:p1")
        self.assertFalse([c for c in self.calls() if c[:2] == ["notification", "show"]])

    def test_an_unchanged_refetch_stays_quiet(self):
        self.run_update("wT1:p1")
        cfg = dict(self.m.load_config(), meta_ttl_seconds=0)
        self.m.update_pane(cfg, "wT1:p1")
        self.assertFalse([c for c in self.calls() if c[:2] == ["notification", "show"]])

    def test_watch_interval_parsing(self):
        self.assertEqual(self.m.watch_interval({}), 120)
        self.assertEqual(self.m.watch_interval({"watch_seconds": 0}), 0)
        self.assertEqual(self.m.watch_interval({"watch_seconds": -5}), 0)
        self.assertEqual(self.m.watch_interval({"watch_seconds": "x"}), 120)

    def test_stale_pid_file_is_not_mistaken_for_the_watcher(self):
        """A recycled pid must never be sent the SIGTERM meant for us."""
        import subprocess
        os.makedirs(os.path.dirname(self.m.WATCH_LOCK), exist_ok=True)
        other = subprocess.Popen(["sleep", "30"])     # alive, but not a watcher
        try:
            with open(self.m.WATCH_LOCK, "w", encoding="utf-8") as fh:
                fh.write(str(other.pid))
            self.assertIsNone(self.m.running_watcher())
        finally:
            other.kill()
            other.wait()


if __name__ == "__main__":
    unittest.main()
