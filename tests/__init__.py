"""Tests for the pure logic: no keyboard, no network, no subprocesses.

The modules under test live in the project root and import each other by bare
name (`import config`), so the root has to be importable however the suite is
started.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
