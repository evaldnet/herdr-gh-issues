#!/usr/bin/env python3
"""Task detail pane: what it renders, and how it lays the body out."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import harness


class TaskPaneCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ghi-task-")
        cls.env = harness.fake_env(cls.tmp)
        os.environ.update(cls.env)
        harness.write_config(cls.env, harness.plugin_config())
        cls.m = harness.import_module("task_pane")
        cls.cfg = cls.m.issue_meta.load_config()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    ISSUE = {"repo": "evaldnet/platform", "number": 134, "is_pr": False}
    PR = {"repo": "evaldnet/clients", "number": 202, "is_pr": True}

    def issue(self):
        return harness.fixture("issue_detail.json")["data"]["repository"]["issue"]

    def pr(self):
        return harness.fixture("pr_detail.json")["data"]["repository"]["pullRequest"]

    def lines(self, item, data, width=56):
        return self.m.build_lines(self.cfg, item, data, width)

    def text(self, item, data, width=56):
        return [t for _, t in self.lines(item, data, width)]

    # --------------------------------------------------------------- fetching
    def test_fetch_issue_and_pr_route_to_different_queries(self):
        issue = self.m.fetch_detail(self.ISSUE)
        self.assertEqual(issue["number"], 134)
        self.assertIn("closedByPullRequestsReferences", issue)
        pr = self.m.fetch_detail(self.PR)
        self.assertEqual(pr["number"], 202)
        self.assertIn("headRefName", pr)

    def test_fetch_failure_is_reported_not_raised(self):
        os.environ["GH_STUB_FAIL"] = "1"
        try:
            out = self.m.fetch_detail(self.ISSUE)
        finally:
            os.environ.pop("GH_STUB_FAIL", None)
        self.assertIn("_error", out)

    def test_malformed_repo_reports_an_error_rather_than_none(self):
        """Bare None here meant run() did None.get() and the pane exited."""
        out = self.m.fetch_detail({"repo": "nope", "number": 1, "is_pr": False})
        self.assertIsInstance(out, dict)
        self.assertIn("_error", out)
        # The renderer must cope with it rather than raise.
        lines = self.m.build_lines(self.cfg, {"repo": "nope", "number": 1,
                                              "is_pr": False}, out, 56)
        self.assertTrue(lines)

    # ---------------------------------------------------------------- header
    def test_header_carries_identity_and_board_status(self):
        head = self.lines(self.ISSUE, self.issue())[0]
        self.assertEqual(head[0], "head")
        self.assertEqual(head[1], "platform#134 · In Development")

    def test_pr_identity_uses_a_bang(self):
        """`#` is an issue and `!` a merge request elsewhere, but the point is
        simply that a PR pane cannot be mistaken for an issue pane."""
        head = self.lines(self.PR, self.pr())[0]
        self.assertEqual(head[1], "clients!202 · changes requested")

    def test_closed_issue_overrides_the_column_it_was_parked_in(self):
        data = dict(self.issue(), state="CLOSED")
        self.assertEqual(self.lines(self.ISSUE, data)[0][1], "platform#134 · closed")

    def test_error_state_renders_instead_of_crashing(self):
        out = self.text(self.ISSUE, {"_error": "gh exited 1"})
        self.assertEqual(out[0], "platform#134")
        self.assertIn("gh exited 1", " ".join(out))

    # ---------------------------------------------------------------- fields
    def test_every_issue_field_is_listed(self):
        body = "\n".join(self.text(self.ISSUE, self.issue()))
        for label in ("Priority", "Task Type", "Complexity", "Status"):
            self.assertIn(label, body)

    def test_field_labels_do_not_collide_with_their_values(self):
        """`Status` is exactly as wide as the old 11-column label field,
        which ran the label straight into its value."""
        for style, text in self.lines(self.ISSUE, self.issue()):
            if style == "key" and text.startswith("Status"):
                self.assertRegex(text, r"^Status\s+\S",
                                 "label and value must be separated")
                break
        else:
            self.fail("no Status row rendered")

    def test_pr_shows_review_branch_and_diff(self):
        body = "\n".join(self.text(self.PR, self.pr()))
        self.assertIn("Review", body)
        self.assertIn("Branch", body)
        self.assertRegex(body, r"Diff\s+\+\d+ −\d+ in \d+ files")

    def test_linked_prs_are_listed_with_their_review_state(self):
        rows = [t for s, t in self.lines(self.ISSUE, self.issue())
                if s == "key" and t.startswith("PR")]
        self.assertTrue(rows, "the closing PR should be shown")
        self.assertIn("clients#202", rows[0])
        self.assertIn("changes requested", rows[0])

    def test_empty_values_are_omitted_entirely(self):
        data = dict(self.issue(), milestone=None, assignees={"nodes": []})
        body = "\n".join(self.text(self.ISSUE, data))
        self.assertNotIn("Milestone", body)
        self.assertNotIn("Assignee", body)

    # ------------------------------------------------------------------ body
    def test_html_comment_markers_are_dropped(self):
        body = "\n".join(self.text(self.ISSUE, self.issue()))
        self.assertNotIn("<!--", body, "sync markers are not content")

    def test_indented_lists_keep_their_indent_and_hang(self):
        out = self.text(self.ISSUE, self.issue(), width=56)
        starts = [t for t in out if t.startswith("    ") and t.strip()]
        self.assertTrue(starts, "the four-space list should stay indented")
        # A wrapped list item continues further in than it starts.
        idx = out.index(starts[0])
        cont = out[idx + 1]
        if cont.strip():
            self.assertGreater(len(cont) - len(cont.lstrip()),
                               len(starts[0]) - len(starts[0].lstrip()),
                               "continuation should hang under its item")

    def test_plain_paragraphs_stay_flush_left(self):
        """Hanging every wrapped line was a regression: only lines that were
        indented in the source should hang."""
        out = self.text(self.ISSUE, self.issue(), width=56)
        para = [i for i, t in enumerate(out)
                if t.startswith("Feltet blev gemt")]
        self.assertTrue(para, "expected the opening paragraph")
        nxt = out[para[0] + 1]
        self.assertFalse(nxt.startswith(" "), "paragraph wrap must not indent")

    def test_no_rendered_line_exceeds_the_width(self):
        for width in (40, 56, 100):
            for _, text in self.lines(self.ISSUE, self.issue(), width):
                self.assertLessEqual(len(text), width,
                                     "overflow at width %d: %r" % (width, text))

    def test_tabs_do_not_break_alignment(self):
        data = dict(self.issue(), body="alpha\n\tbeta gamma")
        for _, text in self.lines(self.ISSUE, data):
            self.assertNotIn("\t", text)

    # ---------------------------------------------------------------- screen
    def pane_screen(self, workspace, cols=64, rows=30):
        env = dict(self.env)
        env["HERDR_WORKSPACE_ID"] = workspace
        env["HERDR_PANE_ID"] = workspace + ":p9"
        return harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "task_pane.py")],
            env, cols=cols, rows=rows, seconds=6.0)

    def test_screen_in_an_issue_space(self):
        rows = self.pane_screen("wT1")
        self.assertIn("platform#134", rows[0])
        self.assertIn("In Development", rows[0])
        self.assertIn("Updated", rows[-1])
        self.assertIn("q quit", rows[-1])

    def test_screen_in_a_pr_space(self):
        rows = self.pane_screen("wT2")
        self.assertIn("clients!202", rows[0])
        joined = "\n".join(rows)
        self.assertIn("Branch", joined)

    def test_screen_in_a_plain_space_says_so(self):
        rows = self.pane_screen("wT3")
        self.assertIn("no issue space here", rows[0])
        self.assertIn("q quit", rows[-1],
                      "the footer belongs on the last row, not over the text")
        self.assertNotIn("platform#", "\n".join(rows))


if __name__ == "__main__":
    unittest.main()
