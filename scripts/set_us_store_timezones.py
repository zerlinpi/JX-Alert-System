"""Apply the owner's confirmed US report timezone without changing health source time."""
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


def set_us_timezones(db, config_path, *, apply=False):
    """Commit file and DB metadata together, restoring the file on DB failure.

    The caller must hold the existing daily-pipeline lock. Historical facts,
    non-US rows, currency, collection controls and source-health time are untouched.
    """
    path = Path(config_path)
    if path.is_symlink():
        raise ValueError('Timezone configuration must be a regular file')
    original, stat = path.read_bytes(), path.stat()
    document = json.loads(original)
    if not isinstance(document, dict) or not isinstance(document.get('stores'), dict):
        raise ValueError('Timezone configuration requires a stores object')
    stores = list(db.scalars(select(Store).order_by(Store.sid)))
    selected = [s for s in stores if (s.country or '').strip().casefold() in US_COUNTRIES]
    if not selected:
        raise ValueError('No explicitly identified US stores were found')
    previous = {s.sid: s.timezone for s in selected}
    changed = 0
    for store in selected:
        item = document['stores'].get(store.sid, {})
        if not isinstance(item, dict):
            raise ValueError('Invalid store timezone entry')
        changed += item.get('timezone') != US_ZONE
        document['stores'][store.sid] = {**item, 'timezone': US_ZONE}
    result = {'applied': apply, 'us_stores': len(selected), 'config_changes': changed,
              'database_changes': sum(s.timezone != US_ZONE for s in selected), 'backup_directory': None}
    if not apply or not (changed or result['database_changes']):
        return result
    backup = path.parent / 'backups' / ('us-timezones-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])
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
    args = parser.parse_args()
    from jx_alert.config import get_settings
    from jx_alert.storage.db import SessionLocal, engine
    from jx_alert.storage.job_lock import database_job_lock
    settings = get_settings()
    with database_job_lock(engine), SessionLocal() as db:
        print(json.dumps(set_us_timezones(db, settings.store_timezones_file, apply=args.apply)))


if __name__ == '__main__':
    main()
