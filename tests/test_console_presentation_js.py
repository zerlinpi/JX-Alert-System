"""Run dependency-free browser rendering logic checks alongside the Python suite."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_business_report_browser_presentation():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for the dependency-free console rendering checks")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/ui/console_presentation.test.cjs"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
