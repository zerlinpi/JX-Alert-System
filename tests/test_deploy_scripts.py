"""Server scripts: valid bash, LF endings and no destructive commands."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SCRIPTS = sorted(DEPLOY.glob("*.sh"))


def test_scripts_exist():
    assert {path.name for path in SCRIPTS} == {"upgrade.sh", "rollback.sh"}


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda path: path.name)
def test_script_is_valid_bash_with_lf_endings(path):
    raw = path.read_bytes()
    assert b"\r\n" not in raw and raw.startswith(b"#!/usr/bin/env bash\n")
    if shutil.which("bash") is None:
        pytest.skip("bash is not installed")
    subprocess.run(["bash", "-n", str(path)], check=True)


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda path: path.name)
def test_script_never_deletes_data_or_volumes(path):
    text = path.read_text(encoding="utf-8")
    for pattern in (r"\bdown\b", r"-v\b.*\bdown|\bdown\b.*-v\b", r"--remove-orphans", r"\bvolume\s+rm\b",
                    r"\brm\s+-rf?\s+[\"']?\$REPO", r"\bdocker\s+rm\b", r"DROP\s+TABLE", r"\bgit\s+reset\s+--hard"):
        assert not re.search(pattern, text, re.IGNORECASE), pattern
    # The body runs inside main(), so checking out another commit cannot alter the running script.
    assert re.search(r"^main\(\) \{$", text, re.MULTILINE) and text.rstrip().endswith('main "$@"')


def test_upgrade_requires_the_tested_tree_and_stops_senders_before_switching():
    text = (DEPLOY / "upgrade.sh").read_text(encoding="utf-8")
    order = [text.index(marker) for marker in (
        'rev-parse "$target^{tree}")" == "$tree"', "mariadb-dump", "compose stop scheduler bot",
        'docker stop "$MCP_CONTAINER"', "checkout --quiet --detach", "up -d --no-build app console",
        "up -d --no-build scheduler bot")]
    assert order == sorted(order)
    assert "if ((dry_run)); then" in text and text.index("if ((dry_run)); then") < text.index("mariadb-dump")
