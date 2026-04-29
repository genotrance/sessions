"""Tests for ContextDaemon — backward-compatible shim.

This file re-imports every test class from the split test modules so that
existing ``python -m unittest tests.test_sessions`` keeps working.

Run from the ``apps/chrome`` directory:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))
sys.path.insert(0, _root)

# Re-export all test classes so unittest discover picks them up here too.
from tests.test_persistence import TestPersistence, TestPersistenceNew
from tests.test_manager import (
    TestContainerManager, TestContainerManagerNew, TestHelpers,
)
from tests.test_api import TestApi, TestApiNew
from tests.test_misc import TestDebugLogging, TestRealBrowserIntegration

if __name__ == "__main__":
    import unittest
    unittest.main()
