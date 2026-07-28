"""Run every test module in this directory in one process.

`python -m unittest discover` does not work for this suite: absltest's managed
temp files and directories read the `--test_tmpdir` flag, and unittest never
parses absl's flags, so every test that asks for a temp path dies with
`UnparsedFlagAccessError`. `absltest.main()` parses the flags first and then
honours the standard `load_tests` protocol below.

Each test module is also runnable on its own — `python tests/test_core.py`.
"""

from __future__ import annotations

import os
import unittest

from absl.testing import absltest

_HERE = os.path.dirname(os.path.abspath(__file__))


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    del tests, pattern  # this module has no tests of its own; discovery replaces both
    return loader.discover(
        start_dir=_HERE, pattern="test_*.py", top_level_dir=os.path.dirname(_HERE)
    )


if __name__ == "__main__":
    absltest.main()
