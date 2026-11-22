"""Read an isolated, complete SQLite copy; never connect to a configured warehouse."""
import argparse
import json
from pathlib import Path
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from jx_alert.reporting.owner_today import prepare_owner_today


def audit(sqlite_copy, scope_file, *, now=None):
    scope = json.loads(Path(scope_file).read_text(encoding="utf-8"))
    if not isinstance(scope, dict) or not isinstance(scope.get("store_ids"), list):
        raise ValueError("Explicit store scope required")
    path = Path(sqlite_copy).resolve(strict=True)
    if not path.is_file() or any(Path(str(path) + ext).exists() for ext in ("-wal", "-shm", "-journal")):
        raise ValueError("Use a standalone, completed SQLite backup, not a running database")
    # immutable with a sealed standalone copy prevents creation of any journals or lock files.
    # as_uri escapes special characters instead of accepting caller-supplied SQLite URI flags.
    engine = create_engine("sqlite://", poolclass=NullPool, creator=lambda:
        sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True))
    try:
        with Session(engine, autoflush=False) as db:
            return prepare_owner_today(db, scope["store_ids"], now=now)
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="核验库内当日分区，只读离线副本，不采集、不发送。")
    parser.add_argument("--sqlite-copy", required=True, help="已校验的独立 SQLite 离线副本（非生产数据库）")
    parser.add_argument("--scope-file", required=True, help="明确授权的店铺范围 JSON（store_ids）")
    args = parser.parse_args()
    try:
        report = audit(args.sqlite_copy, args.scope_file)
    except (OSError, ValueError, SQLAlchemyError) as exc:
        hint = ("核对副本是否含同一快照的 stores、data_coverage 及 daily_metrics 表。"
                if isinstance(exc, SQLAlchemyError) else
                "核对两个文件是否可读、store_ids 是否有效；仅使用已完成且无活动日志的独立副本。")
        print(json.dumps({"status": "AUDIT_FAILED", "error_type": type(exc).__name__, "hint": hint},
                         ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ALL_METRICS_PROVISIONAL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
