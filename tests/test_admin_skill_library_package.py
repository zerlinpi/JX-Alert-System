import base64
from contextlib import closing
from io import BytesIO
from pathlib import Path
import socket
import sqlite3
import stat
import struct
import zlib
import warnings
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from jx_alert import admin_skill_library as library
from jx_alert.agent_skill_document import LibraryError, MAX_BYTES, parse_document
from jx_alert.agent_skill_package import MAX_ARCHIVE, MAX_FILES, MAX_TOTAL, parse_package
from test_admin_skill_library import settings  # noqa: F401

FIXTURE = Path(__file__).parent / "fixtures/owner-skills/portable-review"
FILES = {p.relative_to(FIXTURE).as_posix(): p.read_bytes() for p in sorted(FIXTURE.rglob("*")) if p.is_file()}
NAME = "portable-review"


def pack(entries, wrapper="", compression=ZIP_DEFLATED):
    buf = BytesIO()
    with ZipFile(buf, "w", compression=compression) as z:
        for path, raw in entries:
            z.writestr(wrapper + path if isinstance(path, str) else path, raw)
    return buf.getvalue()


def payload(zipped, revision=0):
    return {"action": "upload_package", "revision": revision, "content_base64": base64.b64encode(zipped).decode()}


def unpack(data):
    with ZipFile(BytesIO(data)) as z:
        assert z.testzip() is None
        return {path.removeprefix(NAME+"/"): z.read(path) for path in z.namelist()}


def test_package_rereview_rollback_and_exact_replay(settings, monkeypatch):  # noqa: F811
    def deny(*_args, **_kw):
        pytest.fail("The portable library cannot use the network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    raw = {path: text.replace(b"\n", b"\r\n") for path, text in FILES.items()}
    # Same shape and size class as the primary-source read skills, not vendor text.
    raw["SKILL.md"] += b"Read-only review notes.\r\n" * 2000
    data = pack(raw.items(), wrapper=NAME+"/")
    library.update_skills(settings, payload(data))
    detail = library.inspect_skill(settings, NAME)
    assert not detail["enabled"] and not detail["applied_to_model"] and not detail["local_preview"]
    assert detail["document"]["declared_version"] == "1.2.3"
    assert detail["missing_text_links"] == [("SKILL.md", "../another-skill/SKILL.md")]
    assert detail["references"] == {p: t.decode() for p, t in raw.items() if p != "SKILL.md"}
    assert sorted(p.name for p in Path(settings.agent_skill_library_dir).iterdir()) == ["library.sqlite3"]
    original = library.export_skill(settings, NAME, 1)
    assert unpack(original) == raw
    library.update_skills(settings, {"action": "enable", "name": NAME, "revision": 1})
    replay = library.update_skills(settings, payload(pack(reversed(raw.items()), compression=ZIP_STORED), 2))
    assert replay["revision"] == 2 and replay["skills"][0]["enabled"]
    # Changing a reference alone creates a new disabled version, retaining all bytes in V1.
    changed = {**raw, "references/measure.md": b"A different measurement definition."}
    library.update_skills(settings, payload(pack(changed.items()), 2))
    assert not library.inspect_skill(settings, NAME)["enabled"]
    assert library.export_skill(settings, NAME, 1) == original
    assert unpack(library.export_skill(settings, NAME, 2)) == changed
    with pytest.raises(LibraryError, match="历史版本"):
        library.update_skills(settings, payload(original, 3))
    library.update_skills(settings, {"action":"rollback","name":NAME,"revision":3,"version":1})
    details = library.inspect_skill(settings, NAME)
    assert details["current_version"] == 1 and not details["enabled"]
    assert [r["action"] for r in details["audit"]] == ["rollback","upload_package","enable","upload_package"]
    # Opening a locally exported file is sufficient to reproduce the exact portable package.
    assert library.update_skills(settings, payload(original, 4))["revision"] == 4


@pytest.mark.parametrize("path", [
    "..", "refs/other.txt", "/references/notes.md", "../notes.md", "references/../notes.md",
    "references//notes.md", r"references\notes.md", "C:/notes.md", "references/con.md", "references/CON .txt", "references/LPT1  .md",
    "references/trailing./a.md", ".DS_Store", "assets/image.png", "scripts/check.py",
    "references/data.pdf", "references/.secret.txt", "references/current/report.md ",
    "references/\u202eswap.md", "references/e\u0301.md", "another-skill/SKILL.md",
])
def test_unsupported_member_rejects_whole_archive(settings, path):  # noqa: F811
    archive = pack([*FILES.items(), (path, b"text")])
    with pytest.raises(LibraryError):
        library.update_skills(settings, payload(archive))
    assert not Path(settings.agent_skill_library_dir).exists()


@pytest.mark.parametrize("extras", [
    [("references/OTHER.md", b"a"), ("references/other.md", b"b")],
    [("references/a.md", b"a"), ("references/a.md", b"b")],
    [("references/not-dir.md", b"a"), ("references/not-dir.md/note.txt", b"b")],
    [("references/GROUP/a.md", b"a"), ("references/group/b.md", b"b")],
    [("references/old.txt", b"\xff")], [("references/bad.md", b"\0")],
    [("references/too-long.txt", b"x" * (MAX_BYTES + 1))],
    [(f"references/{i}.md", b"x" * MAX_BYTES) for i in range(MAX_TOTAL // MAX_BYTES + 1)],
    [(f"references/{i}.md", b"") for i in range(MAX_FILES)],
])
def test_duplicate_binary_or_over_quota_member_never_partially_updates(settings, extras):  # noqa: F811
    library.update_skills(settings, payload(pack(FILES.items())))
    before = library.inspect_skill(settings, NAME)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        invalid = pack([*FILES.items(), *extras])
    with pytest.raises(LibraryError):
        library.update_skills(settings, payload(invalid, 1))
    assert library.inspect_skill(settings, NAME) == before


def test_ordinary_directories_repack_but_symlinks_special_entries_and_damaged_archives_rejected():
    for mode in (stat.S_IFLNK, stat.S_IFIFO):
        entry = ZipInfo("references/untrusted.md")
        entry.create_system = 3
        entry.external_attr = (mode | 0o600) << 16
        with pytest.raises(LibraryError, match="特殊条目"):
            parse_package(payload(pack([*FILES.items(), (entry, b"not a regular file")]))["content_base64"])
    buf = pack([(NAME+"/", b""), (NAME+"/references/", b""),
                *((NAME+"/"+p, v) for p,v in FILES.items())])
    assert parse_package(payload(buf)["content_base64"]).document.name == NAME
    for data in (buf[:-1], b"invalid", pack(FILES.items(), wrapper="different/"), b"a"*(MAX_ARCHIVE+1)):
        with pytest.raises(LibraryError):
            parse_package(payload(data)["content_base64"])


def test_v1_read_without_migration_then_v2_atomic_migration(settings):  # noqa: F811
    root = Path(settings.agent_skill_library_dir)
    root.mkdir(mode=0o700)
    path = root / "library.sqlite3"
    doc = parse_document(FILES["SKILL.md"])
    with closing(sqlite3.connect(path)) as db, db:
        for statement in library._SCHEMA:  # original #75 schema
            db.execute(statement)
        db.execute(f"PRAGMA application_id={library._APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO skill_version VALUES(?,?,?,?,?,?,?)",
                   (NAME, 1, doc.digest, doc.description, None, doc.source, "t"))
        db.execute("INSERT INTO skill_current VALUES(?,?,?)", (NAME, 1, 1))
        db.execute("INSERT INTO skill_audit VALUES(?,?,?,?,?,?,?)", (1, NAME, "upload", None, 1, 1, "t"))
    path.chmod(0o600)
    assert library.inspect_skill(settings, NAME)["enabled"]
    assert unpack(library.export_skill(settings, NAME, 1)) == {"SKILL.md": FILES["SKILL.md"]}
    with pytest.raises(LibraryError, match="刷新"):
        library.update_skills(settings, payload(pack(FILES.items()), 0))
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='skill_reference'").fetchone()
    # Single file and equivalent ZIP must dedupe across schema versions.
    assert library.update_skills(settings, payload(pack([("SKILL.md", FILES["SKILL.md"])]), 1))["revision"] == 1
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
        db.execute("""CREATE TRIGGER simulate_failure BEFORE INSERT ON skill_reference
            BEGIN SELECT RAISE(ABORT,'stop'); END;""")
    with pytest.raises(LibraryError):
        library.update_skills(settings, payload(pack(FILES.items()), 1))
    assert len(library.inspect_skill(settings, NAME)["versions"]) == 1
    assert library.list_skills(settings)["revision"] == 1
    with closing(sqlite3.connect(path)) as db:
        assert not db.execute("SELECT 1 FROM skill_reference").fetchone()
        db.execute("DROP TRIGGER simulate_failure")
    library.update_skills(settings, payload(pack(FILES.items()), 1))
    assert not library.inspect_skill(settings, NAME)["enabled"]
    assert unpack(library.export_skill(settings, NAME, 2)) == FILES


def test_normalized_maximum_depth_flat_package_can_be_reimported(settings):  # noqa: F811
    path = "references/" + ("folder/" * 6) + "index.md"
    library.update_skills(settings, payload(pack([*FILES.items(), (path, b"bounded")])))
    result = library.update_skills(settings, payload(library.export_skill(settings, NAME, 1), 1))
    assert result["revision"] == 1


def test_full_deflate_size_crc_and_local_archive_framing():
    main = FILES["SKILL.md"]
    complete = pack([("SKILL.md", main)])
    central = complete.index(b"PK\x01\x02")
    for declared in [len(main)-9, len(main)+9]:
        sized = bytearray(complete)
        crc = zlib.crc32(main[:declared])
        struct.pack_into("<I", sized, 14, crc)
        struct.pack_into("<I", sized, 22, declared)
        struct.pack_into("<I", sized, central + 16, crc)
        struct.pack_into("<I", sized, central + 24, declared)
        # A clipped prefix with its valid CRC isn't a complete compressed member.
        with pytest.raises(LibraryError, match="大小"):
            parse_package(payload(sized)["content_base64"])

    for offset, change in [(30, b"J"), (central + 34, b"\x01"), (14, b"\0\0\0\0")]:
        sized = complete[:offset] + change + complete[offset + len(change):]
        with pytest.raises(LibraryError):
            parse_package(payload(sized)["content_base64"])


@pytest.mark.parametrize("compression", [ZIP_STORED, ZIP_DEFLATED])
def test_standard_signed_and_unsigned_streamed_descriptors(compression):
    class NoSeek(BytesIO):
        def seek(self, *_args):
            raise OSError("only writes")

    buf = NoSeek()
    with ZipFile(buf, "w", compression) as z:
        z.writestr("SKILL.md", FILES["SKILL.md"])
    archive = buf.getvalue()
    assert parse_package(payload(archive)["content_base64"]).document.name == NAME
    signed = archive.index(b"PK\x07\x08")
    broken = archive[:signed+4] + bytes([archive[signed+4] ^ 1]) + archive[signed+5:]
    with pytest.raises(LibraryError, match="流式"):
        parse_package(payload(broken)["content_base64"])
    unsigned = bytearray(archive[:signed] + archive[signed+4:])
    central = unsigned.index(b"PK\x01\x02")
    struct.pack_into("<I", unsigned, len(unsigned)-6, central)
    assert parse_package(payload(unsigned)["content_base64"]).document.name == NAME


def test_empty_directory_requires_complete_framing():
    original = pack([("references/", b""), ("SKILL.md", FILES["SKILL.md"])])
    central = original.index(b"PK\x01\x02")
    corrupt = bytearray(original)
    struct.pack_into("<I", corrupt, 14, 1)
    struct.pack_into("<I", corrupt, central+16, 1)
    with pytest.raises(LibraryError, match="校验值"):
        parse_package(payload(corrupt)["content_base64"])


def test_authorize_just_before_commit_rolls_back_all_writes(settings):  # noqa: F811
    library.update_skills(settings, payload(pack(FILES.items())))
    prior = library.inspect_skill(settings, NAME)
    calls = 0

    def authorize():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise LibraryError("expired", 401)

    next_files = {**FILES, "references/more.md": b"additional notes"}
    with pytest.raises(LibraryError, match="expired"):
        library.update_skills(settings, payload(pack(next_files.items()), 1), authorize=authorize)
    assert library.inspect_skill(settings, NAME) == prior
