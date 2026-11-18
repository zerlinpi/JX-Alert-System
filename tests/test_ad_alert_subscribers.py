"""Explicit private subscriptions never receive reports or resurrect old alerts."""
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from jx_alert import admin_ad_subscribers as admin, admin_mapping
from jx_alert.config import Settings
from jx_alert.notifications.recipient_policy import (
    advertising_group_delivery, advertising_subscriber_deliveries, enqueue_advertising_copies, subscriber_delivery_blocked,
)
from jx_alert.notifications.routing import enqueue_application_deliveries
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import Campaign, Store


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / 'routing.json'
    config = {'default_chat_id': 'reports', 'groups': {'reports': {
        'chat_id': 'oc_reports', 'display_name': '通知群', 'admin_open_ids': ['ou_admin']}},
        'stores': {'5174': {'open_ids': ['ou_owner']}}, 'management': {'severities': []},
        'unknown': {'preserved': True}}
    path.write_text(json.dumps(config), encoding='utf-8')
    settings = Settings(_env_file=None, feishu_transport='app', feishu_routing_file=str(path),
        feishu_daily_report_chat_id='reports', feishu_group_fallback_enabled=True, feishu_manager_open_ids='',
        store_controls_file=str(tmp_path / 'controls.json'))
    monkeypatch.setattr(admin_mapping, '_members', lambda *_: {'ou_first': '皮泽霖', 'ou_second': '运营甲'})
    return settings, path, config


def save(settings, ids):
    return admin.save_ad_subscribers(settings, ids, admin.get_ad_subscribers(settings)['revision'])


def private_payload(settings):
    return {'material_ad_alert': True, 'transport': 'app',
            'delivery': advertising_subscriber_deliveries(settings)[0]}


def test_owner_selects_verified_members_without_changing_other_routes_or_sending(registry):
    settings, path, original = registry
    assert admin.get_ad_subscribers(settings)['recipients'] == []
    assert len(admin.get_ad_subscribers(settings, include_members=True)['members']) == 2
    result = save(settings, ['ou_first', 'ou_first'])
    assert result['recipients'] == [{'id': 'ou_first', 'name': '皮泽霖'}]
    current = json.loads(path.read_text())
    assert all(current[k] == v for k, v in original.items())
    backups = list(path.parent.glob('routing.json.before-console-*'))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == original
    assert subscriber_delivery_blocked(settings, private_payload(settings)) is False
    before = path.read_bytes()
    save(settings, ['ou_first'])
    assert path.read_bytes() == before


@pytest.mark.parametrize('ids', [['ou_unknown'], ['invalid'], 'ou_first', [True], ['ou_first', 'ou_second']])
def test_invalid_or_unverified_recipients_cannot_be_added(registry, ids):
    settings, path, _ = registry
    before = path.read_bytes()
    with pytest.raises(admin_mapping.MappingError):
        save(settings, ids)
    assert path.read_bytes() == before


def test_revocation_offline_and_readding_a_person_never_rearms_old_payload(registry, monkeypatch):
    settings, _, _ = registry
    save(settings, ['ou_first'])
    old = private_payload(settings)
    with monkeypatch.context() as patch:
        patch.setattr(admin_mapping, '_members', lambda *_: pytest.fail('Clearing must work without roster API'))
        save(settings, [])
    assert subscriber_delivery_blocked(settings, old) is True
    save(settings, ['ou_first'])
    assert subscriber_delivery_blocked(settings, old) is True
    assert subscriber_delivery_blocked(settings, private_payload(settings)) is False


def test_concurrent_route_save_is_not_overwritten(registry, monkeypatch):
    settings, path, original = registry
    revision = admin.get_ad_subscribers(settings)['revision']
    changed = {**original, 'unknown': {'new': True}}
    def members(*_):
        path.write_text(json.dumps(changed))
        return {'ou_first': '皮泽霖'}
    monkeypatch.setattr(admin_mapping, '_members', members)
    with pytest.raises(admin_mapping.MappingError) as error:
        admin.save_ad_subscribers(settings, ['ou_first'], revision)
    assert error.value.status_code == 409 and json.loads(path.read_text()) == changed


@pytest.mark.parametrize('patch', [
    {'material_ad_alert': False}, {'scheduled_delivery_date': '2026-11-18'}, {'morning_focus': True},
    {'delivery': {'role': 'ad_alert_subscriber', 'receive_id_type': 'chat_id', 'receive_id': 'oc_bad'}},
])
def test_reports_and_non_advertising_payloads_are_blocked_even_if_mislabeled(registry, patch):
    settings, _, _ = registry
    save(settings, ['ou_first'])
    assert subscriber_delivery_blocked(settings, private_payload(settings) | patch) is True
    assert subscriber_delivery_blocked(settings, {'delivery': {'role': 'group_question'}}) is False


def test_malformed_subscription_blocks_only_its_private_delivery(registry):
    settings, path, original = registry
    save(settings, ['ou_first'])
    payload = private_payload(settings)
    path.write_text(json.dumps(original | {'ad_alert_subscribers': {'recipients': [{'open_id': 'ou_first'}]}}))
    assert subscriber_delivery_blocked(settings, payload) is True
    assert advertising_subscriber_deliveries(settings) == []
    assert subscriber_delivery_blocked(settings, {'delivery': {'role': 'group'}}) is False


@pytest.mark.parametrize('scope,material,expected', [
    ('campaign_sp', True, 1), ('5174:campaign:sp', True, 1), ('5174:product_ad:sd', True, 1),
    ('campaign_sp', False, 0), ('5174:sales:store', True, 0), ('5174:health', True, 0),
])
def test_only_material_ad_events_append_private_destinations_and_dedupe(registry, scope, material, expected):
    settings, _, _ = registry
    save(settings, ['ou_first'])
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    context = {'store_id': '5174', 'entity_id': '99', 'scope': scope, 'metrics': {}, 'severity': 'P1', 'status': 'NEW'}
    with Session(engine) as db:
        for _ in range(2):
            enqueue_application_deliveries(db, settings, event_key='same', text='广告异常', context=context,
                                          material_ad_alert=material)
        rows = list(db.scalars(select(NotificationOutbox)))
        private = [r for r in rows if r.payload['delivery']['role'] == 'ad_alert_subscriber']
        assert len(private) == expected
        assert all(r.payload['delivery']['receive_id_type'] == 'open_id' for r in private)
        assert len(rows) == expected + 1
    engine.dispose()


def test_legacy_multiple_private_recipients_require_an_explicit_new_selection(registry):
    settings, path, original = registry
    save(settings, ['ou_first'])
    old = private_payload(settings)
    routing = json.loads(path.read_text())
    routing['ad_alert_subscribers']['recipients'].append({
        'name': '旧接收人', 'open_id': 'ou_second', 'generation': 'a' * 32,
    })
    path.write_text(json.dumps(routing))
    assert advertising_subscriber_deliveries(settings) == []
    assert subscriber_delivery_blocked(settings, old)
    assert save(settings, ['ou_second'])['recipients'] == [{'id': 'ou_second', 'name': '运营甲'}]
    assert json.loads(path.read_text())['stores'] == original['stores']


def test_confirmed_mcp_alert_uses_group_mentions_single_lead_and_repeat_guard(registry):
    from jx_alert.notifications.cards import card

    settings, path, _ = registry
    save(settings, ['ou_first'])
    destination = advertising_group_delivery(settings, json.loads(path.read_text()), ['5174'], role='chat_analysis')
    assert destination['open_ids'] == ['ou_owner']
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    clock = datetime(2026, 11, 19, 4, tzinfo=timezone.utc)
    kwargs = dict(text='实质广告风险', card=card('实质广告风险', subtitle='', sections=[]),
        destination=destination, store_id='5174', campaign_id='42', ad_type='sp', report_date='2026-11-10', now=clock)
    with Session(engine) as db:
        ids = enqueue_advertising_copies(db, settings, key='chat-analysis:first', **kwargs)
        assert len(ids) == 2
        assert [db.get(NotificationOutbox, i).chat_id for i in ids] == ['oc_reports', 'ou_first']
        group, lead = (db.get(NotificationOutbox, i).payload for i in ids)
        assert group['ad_group_owners'] is True and lead['delivery']['open_ids'] == []
        assert not enqueue_advertising_copies(db, settings, key='chat-ad-kpi:second', **kwargs)
        late = kwargs | {'campaign_id': 'other', 'now': clock.replace(hour=12)}
        assert not enqueue_advertising_copies(db, settings, key='chat-analysis:night', **late)
    engine.dispose()


def test_existing_management_recipient_is_not_sent_duplicate_private_copy(registry):
    settings, path, original = registry
    save(settings, ['ou_first'])
    data = json.loads(path.read_text())
    data['management'] = {'open_ids': ['ou_first']}
    path.write_text(json.dumps(data))
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        enqueue_application_deliveries(db, settings, event_key='same', text='广告异常', material_ad_alert=True,
            context={'store_id': '5174', 'entity_id': '99', 'scope': 'campaign_sp', 'metrics': {}, 'severity': 'P1', 'status': 'NEW'})
        deliveries = [r.payload['delivery'] for r in db.scalars(select(NotificationOutbox))]
        assert sum(r.get('receive_id') == 'ou_first' for r in deliveries) == 1
    engine.dispose()


def test_worker_rechecks_revocation_before_creating_a_feishu_client(registry, monkeypatch):
    from jx_alert.notifications import worker
    from jx_alert.notifications.feishu_app import FeishuAppClient
    settings, _, _ = registry
    settings.app_dry_run, settings.feishu_enabled = False, True
    save(settings, ['ou_first'])
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as db:
        db.add(Store(sid='5174', name='测试店铺', status=1))
        db.add(Campaign(store_id='5174', campaign_id='99', campaign_type='sp', state='enabled',
                        updated_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        enqueue_application_deliveries(db, settings, event_key='same', text='广告异常', material_ad_alert=True,
            context={'store_id': '5174', 'entity_id': '99', 'scope': 'campaign_sp',
                     'metrics': {}, 'severity': 'P1', 'status': 'NEW'})
        db.commit()
        [selected] = [r.id for r in db.scalars(select(NotificationOutbox))
                      if r.payload['delivery']['role'] == 'ad_alert_subscriber']
    save(settings, [])
    monkeypatch.setattr(worker, 'SessionLocal', sessions)
    monkeypatch.setattr(worker, 'get_settings', lambda: settings)
    monkeypatch.setattr(FeishuAppClient, 'from_settings', lambda *_: pytest.fail('Revoked recipients cannot send'))
    result = worker.process_outbox_batch(outbox_ids=[selected])
    assert result['sent'] == 0 and result['failed'] == 0
    with sessions() as db:
        assert db.get(NotificationOutbox, selected).status == 'SKIPPED'
        assert '订阅已变更' in db.get(NotificationOutbox, selected).last_error
    engine.dispose()
