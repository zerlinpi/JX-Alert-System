import json

import httpx
import pytest

import jx_alert.lingxing.client as lingxing_module
from jx_alert.lingxing.client import (
    LingxingClient,
    LingxingError,
    build_signature,
    signature_source,
)


HOUR_PATHS = (
    "/pb/openapi/newad/spAdvertiseHourData",
    "/pb/openapi/newad/spTargetHourData",
)


@pytest.fixture
def mock_api(monkeypatch):
    requests = []
    original_client = httpx.Client

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "total": 0, "data": []})

    monkeypatch.setattr(
        lingxing_module.httpx, "Client",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handle)),
    )
    monkeypatch.setattr(lingxing_module.time, "time", lambda: 1700000000)
    client = LingxingClient(
        base_url="https://openapi.lingxing.com",
        app_id="0123456789abcdef", app_key="app", access_token="token",
    )
    return client, requests


def test_signature_source_sorts_and_omits_empty_values():
    assert signature_source({"offset": 0, "length": 100, "blank": "", "nil": None}) == (
        "length=100&nil=null&offset=0"
    )


def test_post_body_business_parameters_are_part_of_signature():
    first = build_signature({"offset": 0, "length": 100, "timestamp": 1700000000},
                            access_token="token", app_key="app", app_id="0123456789abcdef")
    second = build_signature({"offset": 1, "length": 100, "timestamp": 1700000000},
                             access_token="token", app_key="app", app_id="0123456789abcdef")
    assert first != second


def test_nested_signature_values_fail_closed():
    with pytest.raises(LingxingError):
        signature_source({"filters": {"asin": ["B000000001"]}})


@pytest.mark.parametrize("path", HOUR_PATHS)
def test_hour_array_signature_uses_official_compact_json_without_changing_body(mock_api, path):
    client, requests = mock_api
    payload = {
        "report_date": "2026-11-11", "agg_dimension": "both_ad_target",
        "campaign_id": 141469378857743, "campaign_ids": [141469378857743, 1123],
    }
    original = json.loads(json.dumps(payload))

    assert client.performance(path, payload)["code"] == 0

    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == path
    assert json.loads(request.content) == original
    assert payload == original
    # Fixed synthetic vector: campaign_ids=[141469378857743,1123] in the
    # sorted signature source (no whitespace), using the fake credentials above.
    assert request.url.params["sign"] == (
        "0vuq2kLDe3aC6vxueVEl7VLQ9mjO9RdHo9d/wOhbd4g3ciLgYakkxZHNnCfqFj7U"
    )
    assert request.headers["Authorization"] == "Bearer token"


@pytest.mark.parametrize("path", HOUR_PATHS)
def test_hour_request_can_use_only_scalar_campaign_id(mock_api, path):
    client, requests = mock_api
    client.performance(path, {
        "report_date": "2026-11-11", "agg_dimension": "both_ad_target", "campaign_id": 1,
    })
    assert "campaign_ids" not in json.loads(requests[0].content)


@pytest.mark.parametrize("path", HOUR_PATHS)
@pytest.mark.parametrize("bad_ids", [
    None, "[1]", (1,), [True], [False], [0], [-1], [1.0], ["1"], [[1]], [{"id": 1}],
])
def test_hour_array_rejects_invalid_ids_before_sending(mock_api, path, bad_ids):
    client, requests = mock_api
    with pytest.raises(LingxingError, match="positive integers"):
        client.performance(path, {"campaign_id": 1, "campaign_ids": bad_ids})
    assert requests == []


@pytest.mark.parametrize("path", HOUR_PATHS)
@pytest.mark.parametrize("bad_id", [None, True, 0, -1, "1", 1.0])
def test_hour_scalar_id_is_validated_with_arrays(mock_api, path, bad_id):
    client, requests = mock_api
    with pytest.raises(LingxingError, match="campaign_id must be a positive integer"):
        client.performance(path, {"campaign_id": bad_id, "campaign_ids": [1]})
    assert requests == []


@pytest.mark.parametrize("path", HOUR_PATHS)
@pytest.mark.parametrize("ids", [None, []])
def test_hour_request_rejects_missing_campaign_selection(mock_api, path, ids):
    client, requests = mock_api
    payload = {} if ids is None else {"campaign_ids": ids}
    with pytest.raises(LingxingError, match="requires campaign_id"):
        client.performance(path, payload)
    assert requests == []


@pytest.mark.parametrize("path", HOUR_PATHS)
def test_campaign_limit_counts_combined_distinct_ids(mock_api, path):
    client, requests = mock_api
    ids = list(range(1, 51))
    client.performance(path, {"campaign_id": 50, "campaign_ids": ids})
    assert json.loads(requests[0].content)["campaign_ids"] == ids
    with pytest.raises(LingxingError, match="at most 50"):
        client.performance(path, {"campaign_id": 51, "campaign_ids": ids})
    assert len(requests) == 1


def test_target_limit_applies_after_provider_documented_deduplication(mock_api):
    client, requests = mock_api
    ids = list(range(1, 51)) + [50]
    client.performance(HOUR_PATHS[1], {"campaign_id": 50, "campaign_ids": ids})
    assert json.loads(requests[0].content)["campaign_ids"] == ids


def test_advertise_array_also_enforces_documented_list_length(mock_api):
    client, requests = mock_api
    with pytest.raises(LingxingError, match="at most 50"):
        client.performance(HOUR_PATHS[0], {"campaign_ids": [1] * 51})
    assert requests == []


@pytest.mark.parametrize("path,payload", [
    ("/pb/openapi/newad/spCampaignHourData", {"campaign_ids": [1]}),
    ("/pb/openapi/newad/sdAdvertiseHourData", {"campaign_ids": [1]}),
    (HOUR_PATHS[0], {"campaign_id": 1, "filters": [1]}),
    (HOUR_PATHS[1], {"campaign_id": 1, "filters": {"id": 1}}),
])
def test_other_nested_values_remain_fail_closed(mock_api, path, payload):
    client, requests = mock_api
    with pytest.raises(LingxingError, match="endpoint-specific"):
        client.performance(path, payload)
    assert requests == []
