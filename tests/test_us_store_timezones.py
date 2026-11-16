import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jx_alert.operations import store_report_plan
from jx_alert.storage.db import Base
from jx_alert.storage.models import Store


spec = importlib.util.spec_from_file_location('set_us_store_timezones',
    Path(__file__).resolve().parents[1] / 'scripts' / 'set_us_store_timezones.py')
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


@pytest.fixture
def configured(tmp_path):
    path = tmp_path / 'zones.json'
    path.write_text(json.dumps({'metadata': 'keep', 'stores': {
        '1': {'timezone': None, 'currency_code': 'USD', 'extra': 'keep'},
        '2': {'timezone': 'America/New_York', 'currency_code': 'USD'},
        '3': {'timezone': None, 'currency_code': 'CAD'},
        '4': {'timezone': 'Europe/London', 'currency_code': 'GBP'}}}))
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as db:
        db.add_all([Store(sid='1', name='US', country='美国', status=1, currency_code='USD'),
            Store(sid='2', name='US disabled', country='US', status=0, timezone='America/New_York'),
            Store(sid='3', name='CA', country='加拿大', status=1, currency_code='CAD'),
            Store(sid='4', name='UK', country='英国', status=1, timezone='Europe/London')])
        db.commit()
        yield db, path
    engine.dispose()


def test_explicit_us_mapping_preserves_other_metadata_and_is_idempotent(configured):
    db, path = configured
    before = path.read_bytes()
    preview = script.set_us_timezones(db, path)
    assert preview['us_stores'] == 2 and not preview['applied'] and path.read_bytes() == before
    result = script.set_us_timezones(db, path, apply=True)
    content = json.loads(path.read_text())
    assert result['us_stores'] == 2 and result['config_changes'] == 2 and result['database_changes'] == 2
    assert content['stores']['1'] == {'timezone': script.US_ZONE, 'currency_code': 'USD', 'extra': 'keep'}
    assert content['stores']['3'] == {'timezone': None, 'currency_code': 'CAD'} and content['metadata'] == 'keep'
    assert db.get(Store, '2').status == 0 and db.get(Store, '2').timezone == script.US_ZONE
    assert db.get(Store, '3').timezone is None and db.get(Store, '3').currency_code == 'CAD'
    assert (Path(result['backup_directory']) / 'store-timezones.before.json').read_bytes() == before
    after = path.read_bytes()
    again = script.set_us_timezones(db, path, apply=True)
    assert again['config_changes'] == again['database_changes'] == 0 and again['backup_directory'] is None
    assert path.read_bytes() == after


def test_database_commit_failure_restores_config_and_metadata(configured, monkeypatch):
    db, path = configured
    before = path.read_bytes()
    def fail():
        raise RuntimeError('simulated commit failure')
    monkeypatch.setattr(db, 'commit', fail)
    with pytest.raises(RuntimeError, match='simulated'):
        script.set_us_timezones(db, path, apply=True)
    assert path.read_bytes() == before and db.get(Store, '1').timezone is None


def test_user_confirmed_los_angeles_plan_observes_dst_and_midnight(configured):
    from datetime import datetime, timezone
    db, path = configured
    script.set_us_timezones(db, path, apply=True)
    for clock, expected in [('2026-11-16T07:59:00', '2026-11-14'), ('2026-11-16T08:01:00', '2026-11-15'),
                            ('2026-07-16T06:59:00', '2026-07-14'), ('2026-07-16T07:01:00', '2026-07-15')]:
        plan = store_report_plan([{'sid': '1', 'status': 1}], path,
            now=datetime.fromisoformat(clock).replace(tzinfo=timezone.utc))
        assert plan[0]['timezone_confirmed'] and plan[0]['report_date'] == expected


def test_config_is_restored_even_when_database_rollback_itself_fails(configured, monkeypatch):
    db, path = configured
    before = path.read_bytes()
    def fail_commit():
        raise RuntimeError('commit failed')
    def fail_rollback():
        raise RuntimeError('rollback failed')
    monkeypatch.setattr(db, 'commit', fail_commit)
    monkeypatch.setattr(db, 'rollback', fail_rollback)
    with pytest.raises(RuntimeError, match='rollback failed'):
        script.set_us_timezones(db, path, apply=True)
    assert path.read_bytes() == before
