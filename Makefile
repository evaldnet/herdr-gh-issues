# Everything here is stdlib and offline. /usr/bin/python3 is 3.9.6 on macOS and
# is what Herdr actually runs the plugin with, so it is what the tests use too.
PYTHON ?= /usr/bin/python3

.PHONY: test test-live test-verbose test-interpreters lint

test:
	$(PYTHON) tests/run.py

test-verbose:
	$(PYTHON) tests/run.py -v

# Adds the checks that need a real herdr: manifest link warnings, registered
# entrypoints. Links the plugin, which is idempotent.
test-live:
	GHI_LIVE=1 $(PYTHON) tests/run.py

# The manifest resolves python3 from PATH, so the plugin can run under a newer
# interpreter than the one these tests default to. Check both.
test-interpreters:
	$(PYTHON) tests/run.py
	PYTHON=$$(command -v python3) $(MAKE) test

# Syntax and manifest only -- the fast subset.
lint:
	$(PYTHON) tests/run.py static
