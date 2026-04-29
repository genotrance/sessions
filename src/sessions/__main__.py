"""Entry point for python -m sessions."""
from __future__ import annotations

import os
import sys

# Add src directory to path when running directly
if __name__ == "__main__":
    _src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _src_dir not in sys.path:
        sys.path.insert(0, _src_dir)

from sessions.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
