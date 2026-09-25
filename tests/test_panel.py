#!/usr/bin/env python3
"""The issue panel: fetching, grouping, PR review state, and what it draws."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import harness


class PanelCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ghi-panel-")
        cls.env = harness.fake_env(cls.tmp)
        os.environ.update(cls.env)
        harness.write_config(cls.env, harness.plugin_config())
        cls.m = harness.import_module("panel")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.cfg = self.m.load_config()
        self.views = {v["id"]: v for v in self.cfg["views"]}

    # --------------------------------------------------------------- fetching
    def test_issue_search_returns_rows_with_fields(self):
        rows, err = self.m.gh_search_issues(self.cfg, self.views["assigned"])
        self.assertEqual(err, "")
        self.assertTrue(rows)
        row = rows[0]
        for key in ("repo", "number", "title", "url", "labels", "fields"):
            self.assertIn(key, row)

    def test_issue_fields_carry_option_order(self):
        """Options come back in creation order, which is the workflow order the
        sections are grouped by -- alphabetical would scramble the board."""
        fields, err = self.m.fetch_issue_fields("evaldnet")
        self.assertEqual(err, "")
        real, options = self.m.field_options(fields, "Status")
        self.assertEqual(real, "Status")
        self.assertIn("In Development", options)
        self.assertNotEqual(options, sorted(options),
                            "options must not be alphabetical")

    def test_gh_failure_surfaces_as_an_error_not_an_exception(self):
        os.environ["GH_STUB_FAIL"] = "1"
        try:
            rows, err = self.m.gh_search_issues(self.cfg, self.views["assigned"])
        finally:
            os.environ.pop("GH_STUB_FAIL", None)
        self.assertTrue(err)
        self.assertEqual(rows, [])

    # ------------------------------------------------------- review decisions
    def test_review_decisions_are_one_request_for_all_rows(self):
        log = os.path.join(self.tmp, "gh-calls.log")
        os.environ["GH_STUB_LOG"] = log
        try:
            rows = [{"repo": "evaldnet/platform", "number": 113},
                    {"repo": "evaldnet/clients", "number": 202}]
            err = self.m.fetch_review_decisions(rows)
        finally:
            os.environ.pop("GH_STUB_LOG", None)
        self.assertEqual(err, "")
        with open(log, "r", encoding="utf-8") as fh:
            calls = [l for l in fh if l.strip()]
        self.assertEqual(len(calls), 1,
                         "N PRs must cost one request, not one each")

    def test_review_decisions_map_to_text_and_glyph(self):
        rows = [{"repo": "evaldnet/platform", "number": 113},
                {"repo": "evaldnet/clients", "number": 202}]
        self.m.fetch_review_decisions(rows)
        self.assertEqual(rows[0]["review"], "review req")
        self.assertEqual(rows[1]["review"], "changes req")
        self.assertEqual(rows[1]["review_glyph"], "✗")

    def test_malformed_repo_is_skipped_without_breaking_the_query(self):
        rows = [{"repo": "bad/../evil", "number": 1},
                {"repo": "evaldnet/clients", "number": 202}]
        err = self.m.fetch_review_decisions(rows)
        self.assertEqual(err, "")
        self.assertNotIn("review", rows[0])
        self.assertIn("review", rows[1])

    def test_no_valid_rows_makes_no_request(self):
        log = os.path.join(self.tmp, "gh-empty.log")
        os.environ["GH_STUB_LOG"] = log
        try:
            self.assertEqual(self.m.fetch_review_decisions([]), "")
        finally:
            os.environ.pop("GH_STUB_LOG", None)
        self.assertFalse(os.path.exists(log))

    # --------------------------------------------------------------- grouping
    def load(self, view_id):
        return self.m.load_view(self.cfg, self.views[view_id], {})

    def test_assigned_view_groups_by_board_column(self):
        rows, options, err = self.load("assigned")
        self.assertEqual(err, "")
        display = self.m.build_display(rows, options, "Status")
        heads = [d[1] for d in display if d[0] == "head"]
        self.assertTrue(heads)
        self.assertIn("In Development", heads)

    def test_excluded_values_produce_no_empty_section(self):
        """The assigned view drops Approved/Deployed; they must not linger as
        empty headers, nor appear in the `s` filter cycle."""
        rows, options, _ = self.load("assigned")
        cells = {r.get("cell") for r in rows}
        self.assertNotIn("Approved", cells)
        self.assertNotIn("Deployed", cells)
        steps = self.m.cycle_steps(rows, options, "Status")
        self.assertNotIn("Approved", steps)

    def test_section_headers_are_never_selectable(self):
        rows, options, _ = self.load("assigned")
        display = self.m.build_display(rows, options, "Status")
        selectable = [d for d in display if d[0] == "row"]
        self.assertEqual(len(selectable), len(rows))

    def test_unset_bucket_sorts_last(self):
        rows, options, _ = self.load("assigned")
        display = self.m.build_display(rows, options, "Status")
        heads = [d[1] for d in display if d[0] == "head"]
        unset = [h for h in heads if h.startswith("no ")]
        if unset:
            self.assertEqual(heads[-1], unset[0])

    def test_order_pins_named_sections_to_the_front(self):
        """The workflow is read by urgency -- what bounced back, what is in
        hand, what is untouched -- not in the field's creation order."""
        view = dict(self.views["assigned"],
                    order=["Test rejected", "In Development", "Not Started"])
        rows, options, _ = self.m.load_view(self.cfg, view, {})
        self.assertEqual(options[:3],
                         ["Test rejected", "In Development", "Not Started"])
        display = self.m.build_display(rows, options, "Status")
        heads = [d[1] for d in display if d[0] == "head"]
        present = [h for h in ("Test rejected", "In Development", "Not Started")
                   if h in heads]
        self.assertEqual(heads[:len(present)], present)

    def test_order_leaves_the_rest_in_field_order(self):
        view = dict(self.views["assigned"], order=["Test rejected"])
        _, options, _ = self.m.load_view(self.cfg, view, {})
        rest = options[1:]
        self.assertEqual(rest, sorted(rest, key=[
            "Pre Task Analysis", "Not Started", "In Development", "Validation",
            "Validated OK", "Ready for test"].index))

    def test_order_matches_option_names_case_insensitively(self):
        view = dict(self.views["assigned"], order=["test REJECTED"])
        _, options, _ = self.m.load_view(self.cfg, view, {})
        self.assertEqual(options[0], "Test rejected",
                         "the field's own spelling, not the config's")

    def test_order_ignores_a_value_the_field_does_not_have(self):
        """A typo or a retired status must not invent an empty section."""
        view = dict(self.views["assigned"], order=["Blocked", "Test rejected"])
        _, options, _ = self.m.load_view(self.cfg, view, {})
        self.assertNotIn("Blocked", options)
        self.assertEqual(options[0], "Test rejected")

    def test_order_does_not_resurrect_an_excluded_value(self):
        view = dict(self.views["assigned"], order=["Approved", "Test rejected"])
        _, options, _ = self.m.load_view(self.cfg, view, {})
        self.assertNotIn("Approved", options)

    def test_order_also_drives_the_column_filter_cycle(self):
        view = dict(self.views["assigned"],
                    order=["Test rejected", "In Development", "Not Started"])
        rows, options, _ = self.m.load_view(self.cfg, view, {})
        steps = self.m.cycle_steps(rows, options, "Status")
        pinned = [s for s in steps if s in options]
        self.assertEqual(pinned, [o for o in options if o in steps],
                         "`s` walks the same order the sections are drawn in")

    def test_order_on_a_column_without_options_changes_nothing(self):
        """Author/assignee columns have no declared options, so there is no
        order to override -- and pinning must not shrink the cell width."""
        view = dict(self.views["review"], order=["somebody"])
        rows, options, _ = self.m.load_view(self.cfg, view, {})
        self.assertEqual(options, [])
        self.assertEqual(self.m.cell_width(rows, options, "author"),
                         self.m.cell_width(rows, [], "author"))

    def test_column_none_renders_flat(self):
        rows, options, _ = self.load("ready")
        display = self.m.build_display(rows, options, "none")
        self.assertFalse([d for d in display if d[0] == "head"])

    def test_review_column_groups_prs_by_decision(self):
        view = dict(self.views["ready"], column="review")
        rows, options, err = self.m.load_view(self.cfg, view, {})
        self.assertEqual(err, "")
        self.assertTrue(any(r.get("cell") for r in rows))
        self.assertEqual(options, ["changes req", "review req", "approved"],
                         "grouped by urgency, not alphabetically")

    def test_cell_width_comes_from_options_not_rows(self):
        """Width is derived from the field's declared options so the title
        column does not shift as the board fills up."""
        rows, options, _ = self.load("assigned")
        wide = self.m.cell_width(rows[:1], options, "Status")
        narrow = self.m.cell_width(rows, options, "Status")
        self.assertEqual(wide, narrow)

    def test_filter_matches_danish_text(self):
        rows, _, _ = self.load("assigned")
        needle = rows[0]["title"].split()[0].lower()
        self.assertTrue(self.m.matches(rows[0], needle, ""))
        self.assertFalse(self.m.matches(rows[0], "zzzz-not-here", ""))

    # ----------------------------------------------------------------- labels
    def test_label_steps_are_ordered_by_frequency(self):
        """Hold states pile up and the module:* tail does not, so frequency
        keeps the useful steps at the front of the cycle."""
        rows, _, _ = self.load("assigned")
        steps = self.m.label_steps(rows)
        self.assertEqual(steps[0], "", "the cycle starts unfiltered")
        self.assertEqual(steps[1], "On Hold")
        self.assertEqual(steps[2], "module:integration")

    def test_label_steps_break_ties_alphabetically(self):
        """Two labels on one row each: dict order would shuffle between
        refreshes, so the order has to come from the name."""
        rows, _, _ = self.load("assigned")
        steps = self.m.label_steps(rows)
        ones = [s for s in steps
                if s in ("module:timegodkendelse", "På hold af DEV")]
        self.assertEqual(ones, ["module:timegodkendelse", "På hold af DEV"])

    def test_label_steps_end_with_unlabelled_when_something_is_bare(self):
        rows, _, _ = self.load("assigned")
        steps = self.m.label_steps(rows)
        self.assertEqual(steps[-1], self.m.UNLABELLED)
        self.assertEqual(self.m.label_filter_text(self.m.UNLABELLED),
                         "unlabelled")

    def test_label_steps_collapse_when_nothing_is_labelled(self):
        """One step means the cycle is a no-op, which is what makes the panel
        say so instead of appearing to filter."""
        rows, _, _ = self.load("assigned")
        for r in rows:
            r["labels"] = []
        self.assertEqual(self.m.label_steps(rows), [""])

    def test_label_filter_matches_any_label_on_the_row(self):
        """A row carrying two labels is reachable under both -- membership,
        not equality, which is the whole difference from the column filter."""
        rows, _, _ = self.load("assigned")
        multi = [r for r in rows if len(r["labels"]) > 1]
        self.assertTrue(multi, "fixture must carry a multi-label row")
        row = multi[0]
        for name in row["labels"]:
            self.assertTrue(self.m.matches(row, "", "", name))
        self.assertFalse(self.m.matches(row, "", "", "module:integration"))

    def test_unlabelled_filter_keeps_only_bare_rows(self):
        rows, _, _ = self.load("assigned")
        kept = [r for r in rows if self.m.matches(r, "", "", self.m.UNLABELLED)]
        self.assertTrue(kept)
        self.assertTrue(all(not r["labels"] for r in kept))
        self.assertEqual(len(kept), len(rows) - len([r for r in rows if r["labels"]]))

    def test_label_and_column_filters_stack(self):
        """The point of a separate key: narrow by board column *and* by label.
        Neither filter alone answers "what of mine is in progress but held"."""
        rows, _, _ = self.load("assigned")
        in_dev = [r for r in rows
                  if self.m.matches(r, "", "In Development", "")]
        held = [r for r in rows
                if self.m.matches(r, "", "In Development", "On Hold")]
        self.assertTrue(held)
        self.assertLess(len(held), len(in_dev), "the label must narrow further")
        self.assertTrue(all(r["cell"] == "In Development" for r in held))
        self.assertTrue(all("On Hold" in r["labels"] for r in held))

    def test_label_filter_composes_with_the_search_needle(self):
        rows, _, _ = self.load("assigned")
        held = [r for r in rows if "On Hold" in r["labels"]]
        needle = held[0]["title"].split()[0].lower()
        self.assertTrue(self.m.matches(held[0], needle, "", "On Hold"))
        self.assertFalse(self.m.matches(held[0], "zzzz-not-here", "", "On Hold"))

    def test_exclude_labels_drops_those_rows_entirely(self):
        view = dict(self.views["assigned"], exclude_labels=["On Hold"])
        rows, _, err = self.m.load_view(self.cfg, view, {})
        self.assertEqual(err, "")
        self.assertTrue(rows)
        self.assertFalse([r for r in rows if "On Hold" in r["labels"]])

    def test_exclude_labels_also_takes_the_multi_label_row(self):
        """Carrying an excluded label is enough; the row's other labels do not
        earn it a reprieve, or a hold would be escapable by adding a tag."""
        plain, _, _ = self.load("assigned")
        multi = [r["number"] for r in plain if len(r["labels"]) > 1]
        self.assertTrue(multi, "fixture must carry a multi-label row")
        view = dict(self.views["assigned"], exclude_labels=["On Hold"])
        rows, _, _ = self.m.load_view(self.cfg, view, {})
        self.assertNotIn(multi[0], [r["number"] for r in rows])

    def test_excluded_labels_leave_the_cycle(self):
        """No row carries them, so they must not linger as steps that filter
        to an empty list."""
        view = dict(self.views["assigned"], exclude_labels=["On Hold"])
        rows, _, _ = self.m.load_view(self.cfg, view, {})
        self.assertNotIn("On Hold", self.m.label_steps(rows))

    def test_exclude_labels_is_case_insensitive(self):
        """GitHub will not let two labels differ only by case, so folding is
        safe -- and a config writing `on hold` should still work."""
        view = dict(self.views["assigned"], exclude_labels=["oN hOlD"])
        rows, _, _ = self.m.load_view(self.cfg, view, {})
        self.assertFalse([r for r in rows if "On Hold" in r["labels"]])

    def test_exclude_labels_absent_changes_nothing(self):
        before, _, _ = self.load("assigned")
        view = dict(self.views["assigned"], exclude_labels=[])
        after, _, _ = self.m.load_view(self.cfg, view, {})
        self.assertEqual(len(before), len(after))

    def test_exclude_labels_applies_to_pr_views_too(self):
        """Labels are the one field PRs and issues genuinely share, so the key
        must not be issues-only the way `exclude` is."""
        plain, _, _ = self.load("ready")
        labelled = [l for r in plain for l in r["labels"]]
        if not labelled:
            self.skipTest("no labelled PRs in the fixture")
        view = dict(self.views["ready"], exclude_labels=[labelled[0]])
        rows, _, _ = self.m.load_view(self.cfg, view, {})
        self.assertFalse([r for r in rows if labelled[0] in r["labels"]])

    # ----------------------------------------------------------------- screen
    def test_screen_lists_issues_under_board_sections(self):
        rows = harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "panel.py")],
            self.env, cols=100, rows=24, seconds=8.0)
        self.assertIn("GitHub · evaldnet", rows[0])
        joined = "\n".join(rows)
        self.assertIn("In Development", joined)
        self.assertRegex(joined, r"#\d{3,}")
        self.assertIn("q quit", rows[-1])

    def test_screen_footer_offers_the_column_filter_when_grouped(self):
        rows = harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "panel.py")],
            self.env, cols=100, rows=24, seconds=8.0)
        self.assertIn("s column", rows[-1])

    def test_screen_footer_offers_the_label_cycle(self):
        rows = harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "panel.py")],
            self.env, cols=100, rows=24, seconds=8.0)
        self.assertIn("l label", rows[-1])

    def test_screen_l_narrows_to_the_commonest_label(self):
        """`l` once lands on the first step after "all", and the header names
        the active label so the shrunken list is not a mystery."""
        rows = harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "panel.py")],
            self.env, cols=100, rows=24, seconds=8.0, keys=b"lq")
        self.assertIn("On Hold", rows[0])
        joined = "\n".join(rows)
        self.assertNotIn("Traceback", joined)

    # ------------------------------------------------------------ footer mouse
    def test_footer_spans_map_columns_to_keys(self):
        items = self.m.footer_items(show_cell=True, label_steps_n=2)
        foot = self.m.FOOT_SEP.join(label for label, _ in items)
        spans = self.m.footer_spans(items, 200)
        for label, key in items:
            x = foot.index(label)
            self.assertEqual(self.m.footer_key(spans, x), key, label)
            self.assertEqual(self.m.footer_key(spans, x + len(label) - 1), key, label)
        # The separator between two items belongs to neither.
        self.assertIsNone(self.m.footer_key(spans, len(items[0][0]) + 1))
        self.assertIsNone(self.m.footer_key(spans, len(foot) + 5))

    def test_footer_item_clipped_off_the_edge_is_not_clickable(self):
        """A click must never fire something the footer does not show."""
        items = [("enter claude", "\n"), ("v view", "v"), ("q quit", "q")]
        spans = self.m.footer_spans(items, 15)  # "q quit" starts past column 15
        self.assertEqual([key for _, _, key in spans], ["\n"])
        spans = self.m.footer_spans(items, 17)  # "v view" only half drawn
        self.assertEqual(spans[-1], (15, 17, "v"))

    @staticmethod
    def x10_click(col, row):
        """Press + release as X10 mouse reports (mode 1000, what ncurses asks
        for under xterm-256color), 0-based col/row."""
        def report(button):
            return b"\x1b[M" + bytes([32 + button, 33 + col, 33 + row])
        return report(0) + report(3)

    def test_screen_click_on_footer_item_acts_like_its_key(self):
        rows = harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "panel.py")],
            self.env, cols=100, rows=24, seconds=8.0)
        col = rows[-1].index("v view")
        clicked = harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "panel.py")],
            self.env, cols=100, rows=24, seconds=8.0,
            keys=self.x10_click(col, 23) + b"q")
        second = self.cfg["views"][1]["label"]
        self.assertNotIn(second, rows[0])
        self.assertIn(second, clicked[0], "the click should cycle the view")
        self.assertNotIn("Traceback", "\n".join(clicked))

    def test_screen_mouse_false_leaves_clicks_alone(self):
        """The opt-out must really release the mouse. It is also the control for
        the click test above: same report, and nothing may happen."""
        tmp = tempfile.mkdtemp(prefix="ghi-panel-nomouse-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = harness.fake_env(tmp)
        harness.write_config(env, dict(harness.plugin_config(), mouse=False))
        rows = harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "panel.py")],
            env, cols=100, rows=24, seconds=8.0)
        col = rows[-1].index("v view")
        clicked = harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "panel.py")],
            env, cols=100, rows=24, seconds=8.0,
            keys=self.x10_click(col, 23) + b"q")
        self.assertIn(self.cfg["views"][0]["label"], clicked[0])
        self.assertNotIn("Traceback", "\n".join(clicked))

    def test_screen_click_off_the_footer_does_nothing(self):
        rows = harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "panel.py")],
            self.env, cols=100, rows=24, seconds=8.0,
            keys=self.x10_click(5, 10) + self.x10_click(99, 23) + b"q")
        first = self.cfg["views"][0]["label"]
        self.assertIn(first, rows[0])
        self.assertIn("q quit", rows[-1])
        self.assertNotIn("Traceback", "\n".join(rows))

    def test_screen_survives_a_gh_outage(self):
        env = dict(self.env, GH_STUB_FAIL="1")
        rows = harness.screen(
            ["/usr/bin/python3", os.path.join(harness.BIN, "panel.py")],
            env, cols=100, rows=24, seconds=8.0)
        joined = "\n".join(rows)
        self.assertIn("GitHub · evaldnet", rows[0],
                      "the panel should still draw, not die")
        self.assertNotIn("Traceback", joined)


if __name__ == "__main__":
    unittest.main()
