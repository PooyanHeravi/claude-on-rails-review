#!/usr/bin/env python3
"""Claude Code Stop Hook — thin shim that delegates to the package."""
import sys
from pathlib import Path

# Add parent directory to sys.path so the package is importable
sys.path.insert(0, str(Path(__file__).parent))

from stop_design_audit.__main__ import run

run(hooks_dir=Path(__file__).parent)
