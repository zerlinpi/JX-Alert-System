"""Offline owner-library round trip of three pinned, public SparkX read skills.

Usage: PYTHONPATH=src python scripts/verify_sparkx_skill_packages.py <skills-dir>
Checks exact fixture bytes without vendoring or fetching any upstream content.
"""
import argparse
import base64
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

from jx_alert.admin_skill_library import export_skill, inspect_skill, update_skills

PIN = "b72b914b891ad9ed357dd0a36ab0da53152aae85"
# SHA-256 of sorted '<sha256>  <relative path>\n' lines. Whole directory, not snippets.
MANIFEST = {
    "sparkx-query-ads-performance": "050ef12abbd9edeaf5eb7447af01437b607df3894734490bd6e3608444f3b92c",
    "sparkx-query-entity-metadata": "78bc8313cd6b695f777956cda399bfacadfcb4244e2089322a52b2e70e85ee82",
    "sparkx-query-operation-log": "87a2886e2a537d53656fbc35465068abfc3427a297b42fb4070c50f2e268574d",
}


def main(root):
    with TemporaryDirectory() as tmp:
        settings = SimpleNamespace(agent_skill_library_dir=str(Path(tmp)/"private"))
        for revision, (name, digest) in enumerate(MANIFEST.items()):
            folder = root / name
            files = {}
            for f in folder.rglob("*"):
                if f.is_symlink():
                    raise ValueError("Supply an unmodified public fixture, not a symlink")
                if f.is_file():
                    files[f.relative_to(folder).as_posix()] = f.read_bytes()
            checks = "".join(f"{sha256(v).hexdigest()}  {k}\n" for k, v in sorted(files.items()))
            if sha256(checks.encode()).hexdigest() != digest:
                raise ValueError(f"Fixture for {name} does not match SparkXAI/SparkXAI-MCP@{PIN}")
            buf = BytesIO()
            with ZipFile(buf, "w", ZIP_DEFLATED) as z:
                for path, data in files.items():
                    z.writestr(f"{name}/{path}", data)
            payload = {"action": "upload_package", "revision": revision,
                       "content_base64": base64.b64encode(buf.getvalue()).decode()}
            result = update_skills(settings, payload)
            assert result["revision"] == revision + 1
            info = inspect_skill(settings, name, 1)
            assert not info["enabled"] and not info["applied_to_model"]
            archive = export_skill(settings, name, 1)
            with ZipFile(BytesIO(archive)) as z:
                assert dict((p.removeprefix(name+"/"), z.read(p)) for p in z.namelist()) == files
            replay = {**payload, "revision": revision+1, "content_base64": base64.b64encode(archive).decode()}
            assert update_skills(settings, replay) == result
            print(f"{name} {info['document']['declared_version']}: {len(files)} files, "
                  f"{sum(map(len, files.values()))} bytes identical; revision unchanged on replay")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skills_dir", type=Path)
    main(parser.parse_args().skills_dir)
