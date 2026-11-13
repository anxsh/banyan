#!/usr/bin/env python3
"""banyan — CLI entrypoint for managing JSON family trees, Gramps conversion, and HTML rendering."""

from __future__ import annotations

import sys

from banyan.cli import main

if __name__ == "__main__":
    sys.exit(main())
