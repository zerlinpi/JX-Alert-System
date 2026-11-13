import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("map_operator", Path(__file__).parents[1] / "scripts/map_feishu_operator.py")
mapping = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mapping)


def test_member_name_must_match_one_real_open_id():
    assert mapping.unique_member([{"name": "皮泽霖", "member_id": "ou_user"}], "皮泽霖") == "ou_user"
    for members in [[], [{"name": "其他人", "member_id": "ou_other"}],
                    [{"name": "皮泽霖", "member_id": "ou_one"}, {"name": "皮泽霖", "member_id": "ou_two"}],
                    [{"name": "皮泽霖", "member_id": "not-an-open-id"}]]:
        with pytest.raises(ValueError):
            mapping.unique_member(members, "皮泽霖")
