import json
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from jx_alert.readiness import configuration_status
from jx_alert.storage.db import Base
from jx_alert.storage.models import Store


def test_manual_roles_are_reported_without_identifiers_or_secrets(tmp_path):
    routing, scope = tmp_path / "routing.json", tmp_path / "scope.json"
    routing.write_text(json.dumps({"default_chat_id": "test", "groups": {
        "test": {"chat_id": "oc_private_group", "admin_open_ids": ["ou_private_admin"]}},
        "stores": {"5174": {"open_ids": []}}, "management": {"open_ids": []}}))
    scope.write_text(json.dumps({"store_ids": [5174]}))
    settings = SimpleNamespace(feishu_routing_file=str(routing), collection_scope_file=str(scope),
                               lingxing_performance_source_timezone="", mcp_public_url="",
                               scheduler_enabled=True, daily_sync_cron="30 18 * * *", app_timezone="Asia/Shanghai",
                               mcp_auth_mode="bearer", lingxing_performance_enabled=True)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Store(sid="5174", name="selected", timezone="America/Los_Angeles", currency_code="USD"))
        db.flush()
        report = configuration_status(db, settings)
        assert report['stores'][0]['admin_count'] == 1
        assert report['stores'][0]['timezone_valid']
        assert {item['field'] for item in report['manual_mapping_required']} == {
            'operator_count', 'management.open_ids', 'verified_health_source_timezone', 'mcp_public_url_dns_tls'}
        assert 'ou_private_admin' not in json.dumps(report) and 'oc_private_group' not in json.dumps(report)
        routing.write_text(json.dumps({"groups": [], "stores": None, "management": "bad"}))
        broken = configuration_status(db, settings)
        assert broken['stores'][0]['admin_count'] == 0 and broken['manager_count'] == 0
