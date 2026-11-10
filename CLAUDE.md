# herdr-gh-issues

- `make test` - full suite under /usr/bin/python3 (what Herdr runs), ~3 min; `make test-interpreters` adds Homebrew python3. Log to a file in the background: `| tail` holds all output until the end and looks like a hang.
- `make check-app` - loads the desktop mod (`app/`) once; run after any `app/main.js` edit.
- Check a new issue-view query live with `gh api graphql` + `search(type: ISSUE_ADVANCED)`, dropping each qualifier in turn: one GitHub ignores leaves the count unchanged.
- `field.` qualifiers ignore case and quoting: `field.task-status:Approved` == `field.Task-Status:"Approved"`.
- A view's `column` can't be a multi-select field (e.g. `Repositories`): `ISSUE_SELECTION` fetches only single-select and date values.
- New view = `config.example.json` + README *Views* row + a test in `tests/test_panel.py`. Its own key adds an `[[actions]]` block in `herdr-plugin.toml`, the README key/surface/palette lines, and its id in the `open.sh` ids test.
- Desktop app: `herdr-app do mod.open --name gh-issues` toggles the popup, `herdr-app shot <file.png>` screenshots the window, `herdr-app status` lists overlays and errors.
- `make sync-agents` rebuilds the `gh-agents`/`gh-main` clones; `ARGS=--check` says whether they're behind Herdr.
