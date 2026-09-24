#!/usr/bin/env python3
"""Checks that do not run the plugin: syntax, manifest shape, config keys.

Cheap, and they catch the failure that actually bites -- a manifest or config
that Herdr rejects at link time, which otherwise shows up as a plugin that
silently does nothing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import unittest

import harness

PLUGIN_ROOT = harness.PLUGIN_ROOT
BIN = harness.BIN
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")


def python_files():
    return sorted(os.path.join(BIN, f) for f in os.listdir(BIN) if f.endswith(".py"))


def shell_files():
    if not os.path.isdir(SCRIPTS):
        return []
    return sorted(os.path.join(SCRIPTS, f) for f in os.listdir(SCRIPTS)
                  if f.endswith(".sh"))


class TestSyntax(unittest.TestCase):
    def test_python_compiles(self):
        self.assertTrue(python_files(), "no python files found in bin/")
        for path in python_files():
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
            try:
                compile(src, path, "exec")
            except SyntaxError as exc:
                self.fail("%s:%s %s" % (path, exc.lineno, exc.msg))

    def test_python_is_39_compatible(self):
        """/usr/bin/python3 on this Mac is 3.9.6, so no PEP 604 unions."""
        for path in python_files():
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
            self.assertIn("from __future__ import annotations", src,
                          "%s must future-import annotations" % os.path.basename(path))
            self.assertNotRegex(src, r"^\s*match .*:$",
                                "%s uses a match statement" % path)

    def test_shell_parses(self):
        for path in shell_files():
            proc = subprocess.run(["sh", "-n", path], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0,
                             "%s: %s" % (path, proc.stderr.strip()))

    def test_shebang_files_are_executable(self):
        """The manifest always names the interpreter, so the bit is not load
        bearing -- but a shebang that cannot be honoured is a trap for anyone
        running the file by hand."""
        for path in shell_files() + python_files():
            with open(path, "r", encoding="utf-8") as fh:
                first = fh.readline()
            if not first.startswith("#!"):
                continue
            self.assertTrue(os.access(path, os.X_OK),
                            "%s has a shebang but is not executable"
                            % os.path.basename(path))


class TestManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(PLUGIN_ROOT, "herdr-plugin.toml"),
                  "r", encoding="utf-8") as fh:
            cls.raw = fh.read()
        try:
            import tomllib
        except ImportError:
            cls.data = None          # 3.9 has no tomllib; fall back to regex
        else:
            cls.data = tomllib.loads(cls.raw)

    def sections(self, name):
        if self.data is not None:
            return self.data.get(name) or []
        # Minimal stand-in: one dict per [[name]] block, values as raw strings.
        blocks, current = [], None
        for line in self.raw.split("\n"):
            if line.strip() == "[[%s]]" % name:
                current = {}
                blocks.append(current)
            elif line.startswith("[") and current is not None:
                current = None
            elif current is not None and "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                current[k.strip()] = v.strip().strip('"')
        return blocks

    def test_required_top_level_keys(self):
        for key in ("id", "name", "version", "min_herdr_version"):
            self.assertRegex(self.raw, r"(?m)^%s\s*=" % key,
                             "manifest is missing %s" % key)

    def test_every_pane_has_id_and_command(self):
        panes = self.sections("panes")
        self.assertTrue(panes, "no [[panes]] declared")
        for p in panes:
            self.assertIn("id", p)
            self.assertIn("command", p)

    def test_every_action_has_id_and_command(self):
        for a in self.sections("actions"):
            self.assertIn("id", a)
            self.assertIn("command", a)

    def test_event_names_are_known(self):
        """Herdr accepts an unknown event name with only a warning at link time,
        so a typo would otherwise be invisible until the hook never fires."""
        known = {
            "workspace.created", "workspace.updated", "workspace.metadata_updated",
            "workspace.renamed", "workspace.moved", "workspace.reordered",
            "workspace.closed", "workspace.focused",
            "tab.created", "tab.closed", "tab.focused", "tab.renamed", "tab.moved",
            "pane.created", "pane.updated", "pane.closed", "pane.focused",
            "pane.moved", "pane.exited", "pane.agent_detected",
            "pane.agent_status_changed", "pane.output_matched", "pane.scroll_changed",
            "layout.updated",
            "worktree.created", "worktree.opened", "worktree.removed",
        }
        for on in re.findall(r'(?m)^on\s*=\s*"([^"]+)"', self.raw):
            self.assertIn(on, known, "unknown event name %r" % on)

    def test_referenced_commands_exist(self):
        """Every file a pane/action/event/startup runs must be in the repo."""
        for path in re.findall(r'"(bin/[\w.-]+|scripts/[\w.-]+)"', self.raw):
            self.assertTrue(os.path.exists(os.path.join(PLUGIN_ROOT, path)),
                            "manifest references missing %s" % path)
        for path in re.findall(r'\$HERDR_PLUGIN_ROOT/([\w./-]+)', self.raw):
            self.assertTrue(os.path.exists(os.path.join(PLUGIN_ROOT, path)),
                            "manifest references missing %s" % path)

    def test_pane_placements_are_valid(self):
        for p in self.sections("panes"):
            placement = p.get("placement", "overlay")
            self.assertIn(placement, ("overlay", "popup", "split", "tab", "zoomed"))


class TestHerdrLayout(unittest.TestCase):
    """Fallback paths for when Herdr does not inject its env.

    These were wrong in every module -- `plugin-config/` and a state path
    missing the `plugins/` segment -- so any invocation without
    HERDR_PLUGIN_CONFIG_DIR silently fell back to DEFAULTS and ran against the
    wrong org, with `except Exception: pass` swallowing the miss.
    """

    def sources(self):
        for path in python_files() + shell_files():
            with open(path, "r", encoding="utf-8") as fh:
                yield os.path.basename(path), fh.read()

    def test_config_fallback_matches_herdr(self):
        for name, src in self.sources():
            self.assertNotIn("herdr/plugin-config", src,
                             "%s: Herdr uses ~/.config/herdr/plugins/config" % name)

    def test_state_fallback_matches_herdr(self):
        import re as _re
        for name, src in self.sources():
            for m in _re.finditer(r"\.local/state/herdr/([\w.-]+)", src):
                self.assertEqual(m.group(1), "plugins",
                                 "%s: state lives under state/herdr/plugins/" % name)

    def test_no_absolute_interpreter_in_the_manifest(self):
        """A hardcoded /usr/bin/python3 is a macOS assumption; the manifest
        claims Linux, where the interpreter is often elsewhere."""
        with open(os.path.join(PLUGIN_ROOT, "herdr-plugin.toml"),
                  "r", encoding="utf-8") as fh:
            self.assertNotIn("/usr/bin/python3", fh.read())


class TestExampleConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = harness.example_config()

    def test_parses_and_has_views(self):
        self.assertIsInstance(self.cfg.get("views"), list)
        self.assertTrue(self.cfg["views"])

    def test_every_view_has_an_id_and_kind(self):
        for v in self.cfg["views"]:
            self.assertIn("id", v)
            self.assertIn(v.get("kind"), ("issues", "prs"))

    def test_keys_the_code_reads_are_documented(self):
        """A key the code defaults for but the example omits is a key nobody
        discovers. Both files are the plugin's documentation of itself."""
        panel = harness.import_module("panel")
        issue_meta = harness.import_module("issue_meta")
        documented = set(self.cfg)
        for source in (panel.DEFAULTS, issue_meta.DEFAULTS):
            for key in source:
                if key in ("views",):
                    continue
                self.assertIn(key, documented,
                              "config.example.json does not show %r" % key)

    def test_readme_documents_every_config_key(self):
        with open(os.path.join(PLUGIN_ROOT, "README.md"), "r", encoding="utf-8") as fh:
            readme = fh.read()
        for key in self.cfg:
            if key == "views":
                continue
            self.assertIn("`%s`" % key, readme,
                          "README does not mention config key %r" % key)


if __name__ == "__main__":
    unittest.main()
