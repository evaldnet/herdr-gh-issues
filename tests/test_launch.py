#!/usr/bin/env python3
"""The launcher: starting the agent against a pane that is still coming up.

Measured before start_agent() existed: of 109 logged launches, 29 were lost to
agent_pane_busy. `workspace create` hands back a pane id before that pane's
shell has reached its prompt, and `agent start` rejects it outright rather than
waiting, so the space opened with no Claude in it and the URL never arrived.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import harness


class StartAgentCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ghi-launch-")
        cls.env = harness.fake_env(cls.tmp)
        os.environ.update(cls.env)
        harness.write_config(cls.env, harness.plugin_config())
        cls.m = harness.import_module("launch")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.counter = os.path.join(self.tmp, "start-count")
        for stale in ("HERDR_STUB_START_FAIL", "HERDR_STUB_START_FAIL_TIMES"):
            os.environ.pop(stale, None)
        if os.path.exists(self.counter):
            os.remove(self.counter)
        os.environ["HERDR_STUB_START_COUNT"] = self.counter
        # Short budget everywhere: these assert on control flow, not patience.
        self.cfg = dict(self.m.load_config(), agent_start_retry_seconds=5)

    def attempts(self):
        try:
            with open(self.counter, "r", encoding="utf-8") as fh:
                return int(fh.read() or 0)
        except (OSError, ValueError):
            return 0

    def busy(self, times):
        os.environ["HERDR_STUB_START_FAIL"] = "agent_pane_busy"
        os.environ["HERDR_STUB_START_FAIL_TIMES"] = str(times)

    def test_clean_start_does_not_retry(self):
        ok, _ = self.m.start_agent(self.cfg, "gh-core-1", "w1:p1")
        self.assertTrue(ok)
        self.assertEqual(self.attempts(), 0, "nothing failed, nothing to retry")

    def test_busy_pane_is_waited_out(self):
        """The whole point: a pane busy for the first few calls still gets an
        agent, instead of the launch being abandoned."""
        self.busy(3)
        ok, _ = self.m.start_agent(self.cfg, "gh-core-2", "w2:p1")
        self.assertTrue(ok)
        self.assertEqual(self.attempts(), 3, "should have retried past each busy")

    def test_busy_pane_gives_up_at_the_budget(self):
        """A pane that never comes up must not hang the launcher forever."""
        self.busy(10 ** 6)
        cfg = dict(self.cfg, agent_start_retry_seconds=1)
        ok, raw = self.m.start_agent(cfg, "gh-core-3", "w3:p1")
        self.assertFalse(ok)
        self.assertIn("agent_pane_busy", raw)
        self.assertGreater(self.attempts(), 1, "should have tried more than once")

    def test_other_errors_fail_immediately(self):
        """Only the startup race is worth waiting out. A misnamed agent will
        not fix itself, and retrying it just delays the error."""
        os.environ["HERDR_STUB_START_FAIL"] = "agent_name_not_found"
        os.environ["HERDR_STUB_START_FAIL_TIMES"] = str(10 ** 6)
        ok, raw = self.m.start_agent(self.cfg, "gh-core-4", "w4:p1")
        self.assertFalse(ok)
        self.assertIn("agent_name_not_found", raw)
        self.assertEqual(self.attempts(), 1, "must not retry a permanent error")

    def test_not_ready_is_tolerated_without_retrying(self):
        """agent_not_ready leaves a usable agent: prompting still lands, and
        retrying would start a second one."""
        os.environ["HERDR_STUB_START_FAIL"] = "agent_not_ready"
        os.environ["HERDR_STUB_START_FAIL_TIMES"] = str(10 ** 6)
        ok, raw = self.m.start_agent(self.cfg, "gh-core-5", "w5:p1")
        self.assertTrue(ok, "not-ready is not a reason to abandon the space")
        self.assertIn("agent_not_ready", raw)
        self.assertEqual(self.attempts(), 1)

    def test_log_lines_carry_elapsed_time(self):
        """Without elapsed time there is no telling an instant failure from one
        that burned a 90s timeout, and those want opposite fixes."""
        import io as _io
        import sys as _sys
        buf = _io.StringIO()
        real, _sys.stderr = _sys.stderr, buf
        try:
            self.m.log("hello")
        finally:
            _sys.stderr = real
        self.assertRegex(buf.getvalue(), r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \+ *\d+\.\ds  hello")


if __name__ == "__main__":
    unittest.main()
