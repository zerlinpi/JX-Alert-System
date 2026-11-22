"""Read an isolated, complete SQLite copy; never connect to a configured warehouse."""
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from jx_alert.reporting.owner_today import prepare_owner_today


def _parse_as_of(value):
    # fromisoformat alone also accepts offset-free dates, arbitrary separators and
    # normalizes some invalid offsets (e.g. +00:60). Use a narrow documented profile.
    pattern = (r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
               r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])")
    try:
        if not re.fullmatch(pattern, value) or value.endswith("-00:00"):
            raise ValueError
        result = datetime.fromisoformat(value).astimezone(timezone.utc)
        # Local store time must remain representable in every timezone.
        if result.date() in (date.min, date.max):
            raise ValueError
    except (OverflowError, ValueError):
        raise argparse.ArgumentTypeError(
            "须为有效日期和带已知时区的时间，如 2026-11-21T17:00:00Z 或 2026-11-22T01:00:00+08:00") from None
    return result


def audit(sqlite_copy, scope_file, *, now=None, as_of=None):
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
            return prepare_owner_today(db, scope["store_ids"], now=now, as_of=as_of)
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="核验库内当日分区，只读离线副本，不采集、不发送。")
    parser.add_argument("--sqlite-copy", required=True, help="已校验的独立 SQLite 离线副本（非生产数据库）")
    parser.add_argument("--scope-file", required=True, help="明确授权的店铺范围 JSON（store_ids）")
    parser.add_argument("--as-of", type=_parse_as_of, metavar="TIMESTAMP",
                        help="可选离线回放参考时刻，格式 YYYY-MM-DDTHH:MM[:SS[.ffffff]]Z 或带 ±HH:MM；不能晚于执行时刻")
    args = parser.parse_args()
    executed_at = datetime.now(timezone.utc) if args.as_of is not None else None
    if args.as_of is not None and args.as_of > executed_at:
        parser.error("--as-of 不能晚于实际执行时刻")
    try:
        report = audit(args.sqlite_copy, args.scope_file, now=executed_at, as_of=args.as_of)
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
