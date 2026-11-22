"""Single-owner document library, never imported by any model, worker or tool runner."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import stat

from jx_alert.agent_skill_document import LibraryError, MAX_BYTES, parse_upload, valid_name
from jx_alert.mcp_oauth import _private_file

MAX_SKILLS = 32
MAX_VERSIONS = 16
_APPLICATION_ID = 0x4A58534B  # JXSK
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS skill_version (
      name TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL,
      description TEXT NOT NULL, objective TEXT, source TEXT NOT NULL, created TEXT NOT NULL,
      PRIMARY KEY(name,version), UNIQUE(name,digest))""",
    """CREATE TABLE IF NOT EXISTS skill_current (
      name TEXT PRIMARY KEY, version INTEGER NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
      FOREIGN KEY(name,version) REFERENCES skill_version(name,version))""",
    """CREATE TABLE IF NOT EXISTS skill_audit (
      sequence INTEGER PRIMARY KEY, name TEXT NOT NULL, action TEXT NOT NULL,
      from_version INTEGER, to_version INTEGER NOT NULL, enabled INTEGER NOT NULL, at TEXT NOT NULL,
      FOREIGN KEY(name,to_version) REFERENCES skill_version(name,version))""",
)


@contextmanager
def _database(settings, *, write=False):
    raw_dir = getattr(settings, "agent_skill_library_dir", "")
    if not raw_dir:
        if write:
            raise LibraryError("后台未配置技能资料库。", 409)
        yield None
        return
    root = Path(raw_dir)
    if not root.is_absolute() or ".." in root.parts or any(p.is_symlink() for p in (root, *root.parents)):
        raise LibraryError("技能资料库路径不安全。", 409)
    path = root / "library.sqlite3"
    try:
        if write:
            root.mkdir(mode=0o700, exist_ok=True)
        if root.exists():
            d = root.stat()
            if not stat.S_ISDIR(d.st_mode) or (os.name == "posix" and (
                    d.st_uid != os.getuid() or d.st_mode & 0o077)):
                raise LibraryError("技能资料库必须使用所有者私有目录。", 409)
        if not write and not path.exists() and not path.is_symlink():
            yield None
            return
        # No caller-controlled path, no executable files or dynamic extensions.
        _private_file(path, create=write)
        if path.stat().st_nlink != 1 or any(Path(str(path) + s).is_symlink() for s in ("-journal", "-wal", "-shm")):
            raise LibraryError("技能资料库存储文件无效。", 409)
        with closing(sqlite3.connect(path, timeout=2, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA synchronous=FULL")
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            if not app_id and not db.execute("SELECT 1 FROM sqlite_master").fetchone():
                if not write:
                    # The first writer's schema may not yet be committed.
                    yield None
                    return
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                db.execute("PRAGMA user_version=1")
            elif app_id != _APPLICATION_ID or db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise LibraryError("无法识别技能资料库，未覆盖。", 409)
            yield db
            db.commit()
    except LibraryError:
        raise
    except (OSError, ValueError, sqlite3.Error):
        raise LibraryError("技能资料库暂时不可用。请核对持久卷和权限后刷新；未执行远程操作。", 409) from None


def _revision(db):
    return db.execute("SELECT COALESCE(MAX(sequence),0) FROM skill_audit").fetchone()[0]


def _state(db):
    if db is None:
        return {"revision": 0, "skills": []}
    rows = db.execute("""SELECT c.name, c.version, c.enabled, v.description, v.digest, v.objective
        FROM skill_current c JOIN skill_version v USING(name,version) ORDER BY c.name""")
    return {"revision": _revision(db), "skills": [
        {**dict(row), "status": "available" if row["enabled"] else "disabled",
         "enabled": bool(row["enabled"]), "applied_to_model": False} for row in rows]}


def list_skills(settings):
    with _database(settings) as db:
        return {**_state(db), "can_upload": bool(getattr(settings, "agent_skill_library_dir", "")),
                "max_bytes": MAX_BYTES}


def _version_number(value):
    return type(value) is int and 1 <= value <= MAX_VERSIONS


def inspect_skill(settings, name, version=None):
    if not valid_name(name) or (version is not None and not _version_number(version)):
        raise LibraryError("技能名称或版本号无效。")
    with _database(settings) as db:
        head = db.execute("SELECT version, enabled FROM skill_current WHERE name=?", (name,)).fetchone() if db else None
        if not head:
            raise LibraryError("未找到该技能。", 404)
        row = db.execute("""SELECT version, digest, description, objective, source, created FROM skill_version
            WHERE name=? AND version=?""", (name, version or head["version"])).fetchone()
        if not row:
            raise LibraryError("未找到该版本。", 404)
        history = db.execute("""SELECT version,digest,created FROM skill_version
            WHERE name=? ORDER BY version DESC""", (name,))
        audit = db.execute("""SELECT sequence,action,from_version,to_version,enabled,at
            FROM skill_audit WHERE name=? ORDER BY sequence DESC LIMIT 30""", (name,))
        available = bool(head["enabled"])
        # Only closed enums, never free-form fields, can cross the preview boundary.
        preview = ({"schema": "jx.review-objective.v1", "objective": row["objective"]}
                   if available and row["version"] == head["version"] and row["objective"] else None)
        return {"name": name, "revision": _revision(db), "current_version": head["version"],
                "enabled": available, "applied_to_model": False, "document": dict(row),
                "versions": [dict(v) for v in history],
                "audit": [{"actor": "console_owner", **dict(e)} for e in audit],
                "local_preview": preview}


def update_skills(settings, payload):
    if not isinstance(payload, dict):
        raise LibraryError("操作格式无效。")
    action = payload.get("action")
    fields = {"upload": {"filename", "content_base64"}, "enable": {"name"},
              "disable": {"name"}, "rollback": {"name", "version"}}
    if not isinstance(action, str) or action not in fields or set(payload) != (fields[action] | {"action", "revision"}):
        raise LibraryError("不支持的技能操作或参数。")
    revision = payload["revision"]
    if type(revision) is not int or not 0 <= revision < 2**63:
        raise LibraryError("资料库版本无效。")
    doc = parse_upload(payload["filename"], payload["content_base64"]) if action == "upload" else None
    name = doc.name if doc else payload["name"]
    if not valid_name(name) or (action == "rollback" and not _version_number(payload["version"])):
        raise LibraryError("技能名称或版本号无效。")
    with _database(settings, write=True) as db:
        if revision != _revision(db):
            raise LibraryError("资料库已变更，请刷新后重试。", 409)
        head = db.execute("SELECT version,enabled FROM skill_current WHERE name=?", (name,)).fetchone()
        previous, enabled = (tuple(head) if head else (None, 0))
        version = previous
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if doc:
            known = db.execute("SELECT version FROM skill_version WHERE name=? AND digest=?",
                               (name, doc.digest)).fetchone()
            if known:
                if known[0] != previous:
                    raise LibraryError("文件与历史版本相同；如需恢复，请在版本记录中回滚。", 409)
            else:
                if not head and db.execute("SELECT COUNT(*) FROM skill_current").fetchone()[0] >= MAX_SKILLS:
                    raise LibraryError("资料库技能数量已达上限。", 409)
                version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM skill_version WHERE name=?",
                                     (name,)).fetchone()[0]
                if version > MAX_VERSIONS:
                    raise LibraryError("该技能版本数量已达上限，未覆盖历史。", 409)
                db.execute("INSERT INTO skill_version VALUES(?,?,?,?,?,?,?)",
                           (name, version, doc.digest, doc.description, doc.objective, doc.source, now))
                enabled = 0  # Review every new version separately, including updates.
        elif not head:
            raise LibraryError("未找到该技能。", 404)
        elif action == "rollback":
            version = payload["version"]
            if not db.execute("SELECT 1 FROM skill_version WHERE name=? AND version=?", (name, version)).fetchone():
                raise LibraryError("未找到该历史版本。", 404)
            if version != previous:
                enabled = 0
        else:
            enabled = int(action == "enable")

        if not head or (version, enabled) != tuple(head):
            db.execute("""INSERT INTO skill_current VALUES(?,?,?) ON CONFLICT(name)
                DO UPDATE SET version=excluded.version,enabled=excluded.enabled""", (name, version, enabled))
            db.execute("""INSERT INTO skill_audit(name,action,from_version,to_version,enabled,at)
                VALUES(?,?,?,?,?,?)""", (name, action, previous, version, enabled, now))
        return {**_state(db), "can_upload": True, "max_bytes": MAX_BYTES}
