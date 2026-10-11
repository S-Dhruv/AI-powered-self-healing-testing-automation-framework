"""Shortcut: `python run_tests.py --suite X.xlsx --app Y` is the same as `healtest run ...`."""

import sys

from healtest.cli import main

if __name__ == "__main__":
    sys.exit(main(["run"] + sys.argv[1:]))
