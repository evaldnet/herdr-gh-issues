#!/usr/bin/env python3
"""Opening a URL in a browser, on both platforms the manifest claims.

This had no coverage, which is how `subprocess.Popen(["open", url])` shipped:
fine on macOS, and on Linux an unguarded OSError straight out of the curses
loop, so pressing `o` exited the pane instead of opening anything.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

import harness


class BrowserCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ghi-browser-")
        cls.env = harness.fake_env(cls.tmp)
        os.environ.update(cls.env)
        harness.write_config(cls.env, harness.plugin_config())
        cls.panel = harness.import_module("panel")
        cls.task_pane = harness.import_module("task_pane")
        # The module the panes actually imported, not a fresh copy of it --
        # loading a second instance would give the test its own BROWSER global
        # and prove nothing about what the panes do.
        import sys
        cls.m = sys.modules["openurl"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self):
        self.m.BROWSER = None

    def test_platform_default_opener(self):
        import sys
        self.assertEqual(self.m.browser_command(),
                         "open" if sys.platform == "darwin" else "xdg-open")

    def test_missing_opener_returns_false_instead_of_raising(self):
        """The Linux failure, reproduced on any platform."""
        self.m.BROWSER = "ghi-definitely-not-a-real-binary"
        self.assertIs(self.m.open_url("https://example.invalid/1"), False)

    def test_nothing_to_open_is_distinct_from_a_missing_opener(self):
        """Both were False once, so an empty URL reported 'no open on PATH'
        on a Mac that plainly has one."""
        self.assertIsNone(self.m.open_url(""))
        self.assertIsNone(self.m.open_url(None))
        self.m.BROWSER = "ghi-definitely-not-a-real-binary"
        self.assertIs(self.m.open_url("https://example.invalid/2"), False)

    def test_the_url_is_what_gets_handed_to_the_opener(self):
        marker = os.path.join(self.tmp, "opened.txt")
        fake = os.path.join(self.tmp, "bin", "fake-opener")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\nprintf \'%s\' "$1" > "$0.out"\n')
        os.chmod(fake, 0o755)
        self.m.BROWSER = fake
        url = "https://example.invalid/issues/7?a=b c"
        self.assertIs(self.m.open_url(url), True)
        # Popen is deliberately not waited on, so poll briefly for the child.
        out = fake + ".out"
        for _ in range(100):
            if os.path.exists(out):
                break
            time.sleep(0.02)
        self.assertTrue(os.path.exists(out), "opener was never invoked")
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), url, "opener got the wrong argument")

    def test_both_panes_use_the_shared_helper(self):
        """Previously duplicated per module, with a test that diffed source
        text -- which stayed green while the two copies' state diverged."""
        for mod in (self.panel, self.task_pane):
            self.assertIs(mod.open_url, self.m.open_url)
            self.assertIs(mod.browser_command, self.m.browser_command)


if __name__ == "__main__":
    unittest.main()
