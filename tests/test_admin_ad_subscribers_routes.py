"""The recipient editor reuses owner sessions, CSRF and verified member reads."""
import os

import pytest

from test_admin_console import ORIGIN, console, login  # noqa: F401


pytestmark = pytest.mark.skipif(os.name == 'nt', reason='Owner HTTP integration runs on the deployment Linux platform')


def test_ad_subscribers_routes_require_owner_and_csrf(console):  # noqa: F811
    client, _, verified, _ = console
    path = '/console/api/ad-alert-subscribers'
    assert client.get(path).status_code == 401
    assert client.post(path, json={}).status_code == 401
    assert not verified
    assert login(client).status_code == 303
    initial = client.get(path)
    assert initial.status_code == 200 and initial.json()['recipients'] == []
    csrf = client.get('/console/api/stores').json()['csrf']
    body = {'open_ids': ['ou_new'], 'revision': initial.json()['revision']}
    assert client.post(path, json=body, headers={'Origin': ORIGIN}).status_code == 403
    assert client.post(path, json=body, headers={'Origin': 'https://other.example', 'X-CSRF-Token': csrf}).status_code == 403
    response = client.post(path, json=body, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})
    assert response.status_code == 200 and response.json()['recipients'] == [{'id': 'ou_new', 'name': '新负责人'}]
    assert 'generation' not in response.text and 'no-store' in response.headers['cache-control']
    assert client.post(path, json=body, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf}).status_code == 409


def test_members_and_payload_are_scoped_to_verified_current_group(console):  # noqa: F811
    client, _, verified, _ = console
    assert login(client).status_code == 303
    data = client.get('/console/api/ad-alert-subscribers/members').json()
    assert data['members'] == [{'id': 'ou_new', 'name': '新负责人'}]
    assert verified == ['oc_test']
    csrf = client.get('/console/api/stores').json()['csrf']
    headers = {'Origin': ORIGIN, 'X-CSRF-Token': csrf}
    path = '/console/api/ad-alert-subscribers'
    for body in ({'open_ids': ['ou_outside'], 'revision': data['revision']},
                 {'open_ids': [], 'revision': data['revision'], 'include_reports': True}):
        assert client.post(path, json=body, headers=headers).status_code == 400
