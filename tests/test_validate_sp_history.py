import importlib.util
import json
from pathlib import Path

import pytest


def _main():
    spec = importlib.util.spec_from_file_location(
        "validate_sp_history", Path(__file__).parents[1] / "scripts/validate_sp_history.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def test_dry_run_has_exact_dates_and_creates_no_evidence(tmp_path, capsys):
    output = tmp_path / "absent"
    assert _main()(["--sid", "5174", "--campaign-id", "141469378857743",
                    "--start", "2026-11-05", "--end", "2026-11-11",
                    "--output-dir", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "DRY_RUN"
    assert len(result["report_dates"]) == 7
    assert result["report_dates"][0] == "2026-11-05"
    assert result["report_dates"][-1] == "2026-11-11"
    assert not output.exists()


@pytest.mark.parametrize("start,end", [("2026-11-12", "2026-11-05"),
                                       ("2026-01-01", "2026-11-11")])
def test_invalid_window_rejected_before_collection(tmp_path, start, end):
    with pytest.raises(SystemExit) as error:
        _main()(["--collect", "--sid", "5174", "--campaign-id", "141469378857743",
                 "--start", start, "--end", end, "--output-dir", str(tmp_path)])
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []
