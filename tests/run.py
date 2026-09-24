#!/usr/bin/env python3
"""Run the whole suite: python3 tests/run.py [-v] [pattern]

Everything is stdlib and fixture-backed, so it needs no network, no GitHub auth
and no running Herdr. Set GHI_LIVE=1 to additionally run the checks that need
the real herdr binary.
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    pattern = (argv[0] if argv else "test_*") 
    if not pattern.startswith("test_"):
        pattern = "test_%s" % pattern
    if not pattern.endswith(".py"):
        pattern += ".py"
    sys.path.insert(0, HERE)
    suite = unittest.defaultTestLoader.discover(HERE, pattern=pattern)
    result = unittest.TextTestRunner(verbosity=2 if verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
