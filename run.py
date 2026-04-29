#!/usr/bin/env python
"""Launcher script for Sessions - run without installation."""
from __future__ import annotations

import sys
import os

# Add src directory to path
_src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
sys.path.insert(0, _src_dir)

from sessions.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
