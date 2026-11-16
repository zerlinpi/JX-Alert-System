"""Apply the owner's confirmed report timezone without changing health source time."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from jx_alert.storage.models import Store


US_COUNTRIES = {'美国', 'us', 'usa', 'united states', 'united states of america'}
US_ZONE = 'America/Los_Angeles'


def _replace(path, raw, stat):
    temporary = path.with_name(path.name + '.tmp-' + uuid4().hex)
    try:
        temporary.write_bytes(raw)
        temporary.chmod(stat.st_mode & 0o777)
        if os.name != 'nt':
            os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def set_us_timezones(db, config_path, *, apply=False, scoped_ids=None):
    """Commit file and DB metadata together, restoring the file on DB failure.

    The caller holds the daily pipeline lock. By default update US stores. An
    explicit scope applies the same timezone to every configured store in it.
    """
    path = Path(config_path)
    if path.is_symlink():
        raise ValueError('Timezone configuration must be a regular file')
    original, stat = path.read_bytes(), path.stat()
    document = json.loads(original)
    if not isinstance(document, dict) or not isinstance(document.get('stores'), dict):
        raise ValueError('Timezone configuration requires a stores object')
    stores = list(db.scalars(select(Store).order_by(Store.sid)))
    if scoped_ids is None:
        selected = [s for s in stores if (s.country or '').strip().casefold() in US_COUNTRIES]
    else:
        selected = [s for s in stores if s.sid in scoped_ids]
        if {s.sid for s in selected} != set(scoped_ids):
            raise ValueError('All configured stores must be synchronized before assigning their timezone')
    if not selected:
        raise ValueError('No selected stores were found')
    previous = {s.sid: s.timezone for s in selected}
    changed = 0
    for store in selected:
        item = document['stores'].get(store.sid, {})
        if not isinstance(item, dict):
            raise ValueError('Invalid store timezone entry')
        changed += item.get('timezone') != US_ZONE
        document['stores'][store.sid] = {**item, 'timezone': US_ZONE}
    count_key = 'us_stores' if scoped_ids is None else 'scoped_stores'
    result = {'applied': apply, count_key: len(selected), 'config_changes': changed,
              'database_changes': sum(s.timezone != US_ZONE for s in selected), 'backup_directory': None}
    if not apply or not (changed or result['database_changes']):
        return result
    prefix = 'us-timezones-' if scoped_ids is None else 'all-store-timezones-'
    backup = path.parent / 'backups' / (prefix + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    for name, raw in [('store-timezones.before.json', original),
                      ('store-metadata.before.json', json.dumps(previous).encode())]:
        target = backup / name
        target.write_bytes(raw)
        target.chmod(0o600)
    replaced = False
    try:
        if path.read_bytes() != original:
            raise RuntimeError('Timezone configuration changed during update')
        _replace(path, (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode(), stat)
        replaced = True
        for store in selected:
            store.timezone = US_ZONE
        db.commit()
    except Exception:
        try:
            db.rollback()
        finally:
            if replaced:
                _replace(path, original, stat)
        raise
    result['backup_directory'] = str(backup)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--all-stores', action='store_true', help='Use the entire configured collection scope')
    args = parser.parse_args()
    from jx_alert.config import get_settings
    from jx_alert.operations import _checked_scope
    from jx_alert.storage.db import SessionLocal, engine
    from jx_alert.storage.job_lock import database_job_lock
    settings = get_settings()
    with database_job_lock(engine), SessionLocal() as db:
        scope = _checked_scope(settings, None) if args.all_stores else None
        print(json.dumps(set_us_timezones(db, settings.store_timezones_file, apply=args.apply, scoped_ids=scope)))


if __name__ == '__main__':
    main()
