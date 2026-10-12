"""Deterministic test orchestration; model-backed tests are never implicit."""

import argparse
import sys
import unittest


def main(root, argv):
    parser = argparse.ArgumentParser(prog="review-all test")
    parser.add_argument("--pattern", default="test_*.py")
    options = parser.parse_args(argv)
    directory = root / "tests"
    if not directory.is_dir():
        raise ValueError(f"test resources missing: {directory}")
    sys.path.insert(0, str(directory))
    suite = unittest.defaultTestLoader.discover(str(directory), pattern=options.pattern)
    if suite.countTestCases() == 0:
        raise ValueError(f"no tests matched {options.pattern}")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1
