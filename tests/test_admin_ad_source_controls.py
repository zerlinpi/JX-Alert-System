from datetime import datetime
import json
from pathlib import Path
import re
from types import SimpleNamespace

from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.applications import Starlette
from starlette.testclient import TestClient

from jx_alert import ad_source_controls as controls
from jx_alert.admin_console import OwnerConsole
from jx_alert.config import Settings
from jx_alert.storage.db import Base
from jx_alert.storage.models import Store, SyncJob

ORIGIN = "https://console.example.com"
API = "/console/api/stores/10/advertising-source"
SYNTHETIC_TOKEN = "sparkx-local-synthetic-token-not-a-real-secret-1"
BINDING = dict(profile_id="9007199254740999", marketplace="ATVPDKIKX0DER",
               currency="USD", timezone="America/Los_Angeles")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    scope, zones = tmp_path / "scope.json", tmp_path / "zones.json"
    scope.write_text('{"store_ids":[10,11]}', encoding="utf-8")
    zones.write_text(json.dumps({"stores": {sid: {"timezone": "America/Los_Angeles", "currency_code": "USD"}
                                           for sid in ("10", "11")}}), encoding="utf-8")
    settings = Settings(_env_file=None, collection_scope_file=str(scope), store_timezones_file=str(zones),
        advertising_source_controls_file=str(tmp_path / "ad-selection.json"),
        store_controls_file=str(tmp_path / "store-controls.json"),
        sparkx_ai_token=SYNTHETIC_TOKEN, sparkx_profile_ids=BINDING["profile_id"] + ",123456")
    engine = create_engine("sqlite:///" + str(tmp_path / "warehouse.sqlite"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        for sid in ("10", "11", "22"):
            db.add(Store(sid=sid, status=1, name="Fake store " + sid, timezone="America/Los_Angeles",
                         currency_code="USD", marketplace_id="ATVPDKIKX0DER"))
    provider = SimpleNamespace(issuer_url=ORIGIN, database_path=tmp_path / "oauth.sqlite",
                               _password_valid=lambda value: value == "long synthetic owner password")
    server = OwnerConsole(settings, provider, session_factory=sessions)
    calls = []

    def probe(config, store, binding):
        calls.append((store.sid, binding))
        return {"status": "VERIFIED", **binding, "sample_date": "2026-11-22", "row_count": 3}

    monkeypatch.setattr(server, "_verify_sparkx_scope", probe)
    with TestClient(Starlette(routes=server.routes()), base_url=ORIGIN, follow_redirects=False) as client:
        yield client, server, sessions, settings, calls
    engine.dispose()


def _sign_in(client):
    csrf = re.search(r'name="csrf"\s+value="([A-Za-z0-9_-]+)"', client.get("/console/login").text)[1]
    assert client.post("/console/login", data={"password": "long synthetic owner password",
        "csrf": csrf}, headers={"Origin": ORIGIN}).status_code == 303
    api_csrf = client.get("/console/api/stores").json()["csrf"]
    return {"Origin": ORIGIN, "X-CSRF-Token": api_csrf}


def _request(status, **changes):
    return {"revision": status["revision"], **changes}


def test_owner_only_probe_then_explicit_choice_and_token_revocation_rollback(runtime):
    client, _, sessions, settings, calls = runtime
    assert client.get(API).status_code == 401
    assert client.post(API + "/verify", json={}).status_code == 401
    headers = _sign_in(client)
    assert client.get("/console/api/stores/22/advertising-source").status_code == 404
    default = client.get(API).json()
    assert default["source"] == "lingxing"
    assert default["observation_source"] == "lingxing" and default["selected_at"] is None
    assert default["verification_status"] == "UNVERIFIED" and default["sparkx_ready"] is True
    assert not Path(settings.advertising_source_controls_file).exists()
    assert calls == []
    assert client.post(API, headers=headers, json=_request(default, source="sparkx")).status_code == 409
    assert calls == []
    assert client.post(API + "/verify", headers={"Origin": ORIGIN},
                       json=_request(default, binding=BINDING)).status_code == 403
    assert client.post(API + "/verify", headers={**headers, "Origin": "https://other.invalid"},
                       json=_request(default, binding=BINDING)).status_code == 403
    assert calls == []

    response = client.post(API + "/verify", headers=headers, json=_request(default, binding=BINDING))
    assert response.status_code == 200
    verified = response.json()
    assert verified["source"] == "lingxing" and verified["selected_at"] is None
    assert verified["verification_status"] == "VERIFIED"
    assert verified["binding"] == BINDING
    assert calls == [("10", {**BINDING, "profile_id": int(BINDING["profile_id"])})]
    path = Path(settings.advertising_source_controls_file)
    assert path.stat().st_mode & 0o077 == 0
    assert "token_fingerprint" not in response.text and SYNTHETIC_TOKEN not in path.read_text(encoding="utf-8")
    assert client.post(API, headers=headers, json=_request(default, source="sparkx")).status_code == 409
    switched = client.post(API, headers=headers, json=_request(verified, source="sparkx")).json()
    assert switched["source"] == switched["observation_source"] == "sparkx"
    assert datetime.fromisoformat(switched["selected_at"]).utcoffset().total_seconds() == 0
    assert len(calls) == 1
    assert client.get("/console/api/stores/11/advertising-source").json()["source"] == "lingxing"
    with sessions() as db:
        assert controls.observation_source(settings, db.get(Store, "10")) == "sparkx"
        assert db.scalar(select(SyncJob.id)) is None

    settings.sparkx_ai_token = SecretStr("")
    blocked = client.get(API).json()
    assert blocked["source"] == "sparkx" and blocked["observation_source"] is None
    assert blocked["verification_status"] == "RECHECK_REQUIRED"
    assert blocked["selected_at"] == switched["selected_at"]
    with sessions() as db:
        assert controls.observation_source(settings, db.get(Store, "10")) is None
    undone = client.post(API, headers=headers, json=_request(blocked, source="lingxing")).json()
    assert undone["source"] == undone["observation_source"] == "lingxing"
    assert undone["selected_at"] > switched["selected_at"]
    assert len(calls) == 1


def test_duplicate_and_cross_store_bindings_are_rejected_without_vendor_call(runtime):
    client, _, _, _, calls = runtime
    headers = _sign_in(client)
    default = client.get(API).json()
    malformed = json.dumps(_request(default, binding=BINDING)).replace('"profile_id":', '"profile_id":"123456","profile_id":')
    assert client.post(API + "/verify", content=malformed,
        headers={**headers, "Content-Type": "application/json"}).status_code == 400
    wrong_basis = {**BINDING, "currency": "EUR"}
    assert client.post(API + "/verify", headers=headers,
                       json=_request(default, binding=wrong_basis)).status_code == 409
    assert calls == []
    verified = client.post(API + "/verify", headers=headers, json=_request(default, binding=BINDING)).json()
    other = "/console/api/stores/11/advertising-source/verify"
    assert client.post(other, headers=headers, json=_request(verified, binding=BINDING)).status_code == 409
    assert len(calls) == 1


def test_refresh_proof_does_not_change_source_cutoff_and_rebinding_requires_rollback(runtime):
    client, _, sessions, settings, calls = runtime
    headers = _sign_in(client)
    with sessions() as db:
        store = db.get(Store, "10")
        initial = controls.source_snapshot(settings, "10")
    bound, proof = controls.prepare_verification(settings, store, _request(initial, binding=BINDING))
    verified = controls.save_verification(settings, store, _request(initial, binding=BINDING),
                                          bound, proof, authorize=lambda: None)
    switched = controls.save_selection(settings, store, _request(verified, source="sparkx"), authorize=lambda: None)
    refresh = client.post(API + "/verify", headers=headers, json=_request(switched, binding=BINDING)).json()
    assert refresh["selected_at"] == switched["selected_at"] != refresh["updated_at"]
    different = {**BINDING, "profile_id": "123456"}
    assert client.post(API + "/verify", headers=headers, json=_request(refresh, binding=different)).status_code == 409
    assert len(calls) == 1

    # A malformed privileged control file is not permission to switch source.
    path = Path(settings.advertising_source_controls_file)
    raw = path.read_text(encoding="utf-8").replace('"source":', '"source":"lingxing","source":', 1)
    path.write_text(raw, encoding="utf-8")
    assert client.get(API).status_code == 503
    with sessions() as db:
        assert controls.observation_source(settings, db.get(Store, "10")) is None


@pytest.mark.parametrize("case", ["revoked_token", "scope_removed", "session_expired", "wrong_sample", "vendor_error"])
def test_no_proof_if_verification_no_longer_authorized_or_is_invalid(runtime, monkeypatch, case):
    client, server, _, settings, calls = runtime
    headers = _sign_in(client)
    initial = client.get(API).json()
    original = server._verify_sparkx_scope

    def blocked(config, store, binding):
        receipt = original(config, store, binding)
        if case == "revoked_token":
            settings.sparkx_ai_token = SecretStr("different-synthetic-test-token")
        elif case == "scope_removed":
            Path(settings.collection_scope_file).write_text('{"store_ids":[11]}', encoding="utf-8")
        elif case == "session_expired":
            monkeypatch.setattr(server, "_session", lambda *args, **kwargs: False)
        elif case == "wrong_sample":
            receipt["profile_id"] = 123456
        else:
            raise RuntimeError("private-response-body-should-never-appear")
        return receipt

    monkeypatch.setattr(server, "_verify_sparkx_scope", blocked)
    result = client.post(API + "/verify", headers=headers, json=_request(initial, binding=BINDING))
    assert result.status_code in (401, 404, 409, 502)
    assert "private-response-body" not in result.text
    assert len(calls) == 1 and not Path(settings.advertising_source_controls_file).exists()


def test_owner_observation_status_is_local_scope_and_metadata_only(runtime, monkeypatch):
    client, server, _, settings, calls = runtime
    settings.app_dry_run = False
    settings.scheduler_enabled = settings.hourly_ad_monitor_enabled = True
    headers = _sign_in(client)
    observations = []

    def observed(db, store, selection):
        observations.append((store.sid, selection["source"], selection["selected_at"]))
        return [{"period": "hourly", "report_date": "2026-11-22", "row_count": 4,
                 "captured_at": "2026-11-23T00:16:00Z", "provider_row": {"hidden": "raw-campaign"},
                 "profile_id": "hidden"}, {"period": "daily", "report_date": "2026-11-22", "row_count": 0,
                 "captured_at": "2026-11-23T00:16:00Z"}]

    monkeypatch.setattr(server, "_ad_source_observations", observed)
    initial = client.get(API).json()
    assert observations == []
    verified = client.post(API + "/verify", headers=headers, json=_request(initial, binding=BINDING)).json()
    assert observations == []
    switched = client.post(API, headers=headers, json=_request(verified, source="sparkx")).json()
    assert observations == [("10", "sparkx", switched["selected_at"])]
    response = client.get(API)
    assert response.json()["observation_status"] == "OBSERVED"
    assert response.json()["latest_observations"] == [{"period": "hourly", "report_date": "2026-11-22",
        "row_count": 4, "captured_at": "2026-11-23T00:16:00Z"}]
    assert "raw-campaign" not in response.text and "token_fingerprint" not in response.text
    assert len(calls) == 1


def test_missing_initialized_selection_does_not_automatically_restore_lingxing(runtime):
    from jx_alert.ad_source_policy import lingxing_ad_allowed

    client, _, sessions, settings, _ = runtime
    headers = _sign_in(client)
    default = client.get(API).json()
    verified = client.post(API + "/verify", headers=headers, json=_request(default, binding=BINDING))
    switched = client.post(API, headers=headers, json=_request(verified.json(), source="sparkx"))
    assert switched.status_code == 200
    path = Path(settings.advertising_source_controls_file)
    historical = path.read_bytes()
    assert path.with_name(path.name + ".initialized").is_file()
    path.unlink()
    assert client.get(API).status_code == 503
    assert not lingxing_ad_allowed(settings, "10")
    with sessions() as db:
        assert controls.observation_source(settings, db.get(Store, "10")) is None
    path.write_bytes(historical)
    assert client.get(API).json()["source"] == "sparkx"


def test_interrupted_first_verification_does_not_pause_legacy_sources(runtime, monkeypatch):
    from jx_alert.ad_source_policy import lingxing_ad_allowed

    client, _, _, settings, calls = runtime
    headers = _sign_in(client)
    default = client.get(API).json()
    path = Path(settings.advertising_source_controls_file)
    original = controls.os.replace

    def interrupted(src, dst):
        if Path(dst) == path:
            raise OSError("synthetic failed first replace")
        return original(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(controls.os, "replace", interrupted)
        result = client.post(API + "/verify", headers=headers, json=_request(default, binding=BINDING))
    assert result.status_code == 503
    assert len(calls) == 1 and not path.exists()
    assert not controls._initialized_marker(path).exists()
    assert client.get(API).json()["source"] == "lingxing"
    assert lingxing_ad_allowed(settings, "10") and lingxing_ad_allowed(settings, "11")
    retry = client.post(API + "/verify", headers=headers, json=_request(default, binding=BINDING))
    assert retry.status_code == 200 and retry.json()["source"] == "lingxing"


def test_selected_scope_reports_collection_pause_instead_of_waiting(runtime):
    client, _, _, settings, _ = runtime
    settings.app_dry_run = False
    settings.scheduler_enabled = settings.hourly_ad_monitor_enabled = True
    headers = _sign_in(client)
    initial = client.get(API).json()
    verified = client.post(API + "/verify", headers=headers, json=_request(initial, binding=BINDING)).json()
    selected = client.post(API, headers=headers, json=_request(verified, source="sparkx")).json()
    assert selected["observation_status"] == "WAITING"
    Path(settings.store_controls_file).write_text(json.dumps({"version": 1, "stores": {"10": {
        "collection_enabled": False, "notifications_enabled": True, "updated_at": selected["selected_at"]}}}))
    paused = client.get(API).json()
    assert paused["source"] == "sparkx" and paused["observation_status"] == "COLLECTION_PAUSED"
    assert paused["latest_observations"] == []


def test_later_failed_attempt_does_not_hide_history_or_claim_success(runtime):
    from jx_alert.sparkx_backend import _observation_scope_id, persist_sparkx_observation

    client, _, sessions, settings, calls = runtime
    settings.app_dry_run = False
    settings.scheduler_enabled = settings.hourly_ad_monitor_enabled = True
    headers = _sign_in(client)
    initial = client.get(API).json()
    verified = client.post(API + "/verify", headers=headers, json=_request(initial, binding=BINDING)).json()
    assert client.post(API, headers=headers, json=_request(verified, source="sparkx")).status_code == 200
    selection = controls.source_snapshot(settings, "10")
    captured = datetime.fromisoformat(selection["selected_at"])
    with sessions.begin() as db:
        store = db.get(Store, "10")
        marker = _observation_scope_id(store, selection)
        persist_sparkx_observation(db, store, selection,
            {"granularity": "daily", "row_count": 3, "report_date": "2026-11-22",
             "rows": [{"campaign_id": "synthetic-do-not-return"}]},
            observed_at=captured, slot="prior-success")
        for state, generation in (("PARTIAL", marker), ("OBSERVED", "different-generation")):
            db.add(SyncJob(job_type="hourly_ad_monitor", status="PARTIAL_FAILED",
                started_at=captured.replace(tzinfo=None), metadata_json={"sparkx_observations": {
                    "10": {"selection_id": generation, "status": state,
                           "errors": {"provider": "synthetic-do-not-return"}}}}))
    response = client.get(API)
    latest = response.json()
    assert latest["observation_status"] == "ATTEMPT_INCOMPLETE"
    assert latest["latest_observations"][0]["row_count"] == 3
    assert "synthetic-do-not-return" not in response.text
    assert len(calls) == 1


def test_interrupted_activation_preserves_prior_verification_and_can_retry(runtime, monkeypatch):
    from jx_alert.ad_source_policy import lingxing_ad_allowed

    client, _, _, settings, calls = runtime
    headers = _sign_in(client)
    initial = client.get(API).json()
    verified = client.post(API + "/verify", headers=headers, json=_request(initial, binding=BINDING)).json()
    path = Path(settings.advertising_source_controls_file)
    marker = controls._initialized_marker(path)
    assert not marker.exists()
    backup = path.read_bytes()
    original = controls.os.replace

    def interrupted(src, dst):
        if Path(dst) == path:
            assert marker.is_file()
            raise OSError("synthetic failed activation replace")
        return original(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(controls.os, "replace", interrupted)
        failed = client.post(API, headers=headers, json=_request(verified, source="sparkx"))
    assert failed.status_code == 503 and path.read_bytes() == backup
    assert marker.is_file() and lingxing_ad_allowed(settings, "10")
    assert client.get(API).json()["verification_status"] == "VERIFIED"
    retried = client.post(API, headers=headers, json=_request(verified, source="sparkx"))
    assert retried.status_code == 200 and retried.json()["source"] == "sparkx"
    assert len(calls) == 1
