import importlib.util
import json
import os
from pathlib import Path

import pytest


def test_private_initialization_does_not_overwrite_existing_credentials(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "setup_deepseek.py"
    spec = importlib.util.spec_from_file_location("setup_deepseek_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "secrets" / "deepseek.json"
    module.create_credentials(target, "synthetic-deepseek-key")
    assert json.loads(target.read_text())["api_key"] == "synthetic-deepseek-key"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        module.create_credentials(target, "other-key")
    assert json.loads(target.read_text())["api_key"] == "synthetic-deepseek-key"
