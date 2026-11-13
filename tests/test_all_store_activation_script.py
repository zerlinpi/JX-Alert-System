import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("enable_stores", Path(__file__).parents[1] / "scripts/enable_all_store_analysis.py")
enable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(enable)


def test_activation_requires_unique_real_store_inventory():
    assert enable.validated_store_ids({"data": [{"sid": 5174}, {"sid": "00042"}]}) == ["42", "5174"]
    for response in [{}, {"data": []}, {"data": [{"sid": 0}]},
                     {"data": [{"sid": 5174}, {"sid": "05174"}]}]:
        with pytest.raises(ValueError):
            enable.validated_store_ids(response)


def test_atomic_config_preserves_unknown_fields_and_leaves_no_staging_file(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"existing": "preserved"}))
    enable.atomic_json(path, {"existing": "preserved", "new_flag": True})
    assert json.loads(path.read_text()) == {"existing": "preserved", "new_flag": True}
    assert list(tmp_path.iterdir()) == [path]
