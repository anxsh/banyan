"""Run banyan CLI via `python3 -m banyan`."""

from __future__ import annotations

import sys

from banyan.cli import main

if __name__ == "__main__":
    sys.exit(main())
