"""Run the orchestrator.

Two ways in:
  python3 -m jetty.orchestrator ...     # from an install / a PYTHONPATH
  python3 <path-to-this-directory> ...  # from a bare copy of the directory

The second is the distribution story: the package is stdlib-only, so
deploying it is `scp -r` of this directory and nothing else. Executing a
directory gives the code no package context, which relative imports need —
so bootstrap one: put the parent on sys.path and import through the
directory's name (which must therefore be a valid module name).
"""

import sys

if __package__:
    from .cli import main
else:
    import importlib
    import os

    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    _pkg_name = os.path.basename(_pkg_dir)
    if not _pkg_name.isidentifier():
        sys.exit(
            f"jetty-orc: the directory name {_pkg_name!r} must be a valid "
            "Python module name to be run directly — rename it (e.g. to "
            "jetty_orc) and retry"
        )
    sys.path.insert(0, os.path.dirname(_pkg_dir))
    main = importlib.import_module(f"{_pkg_name}.cli").main

if __name__ == "__main__":
    main()
