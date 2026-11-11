import jx_alert.lingxing.client as lingxing_module
from jx_alert.lingxing.client import LingxingClient, LingxingError


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            request = httpx.Request("GET", "https://openapi.lingxing.com/test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("HTTP error", request=request, response=response)

    def json(self):
        return self.payload


class FakeHttpClient:
    def __init__(self, responses, calls):
        self.responses = responses
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


def _client():
    return LingxingClient(
        base_url="https://openapi.lingxing.com",
        app_id="0123456789abcdef",
        app_key="app-key",
        access_token="test-access-token",
    )


def test_rate_limit_response_retries_with_bounded_backoff(monkeypatch):
    calls = []
    sleeps = []
    responses = [
        FakeResponse({"code": 3001008, "message": "token bucket rate limit"}),
        FakeResponse({"code": 0, "data": []}),
    ]
    monkeypatch.setattr(
        lingxing_module.httpx, "Client",
        lambda timeout: FakeHttpClient(responses, calls),
    )
    monkeypatch.setattr(lingxing_module.time, "sleep", sleeps.append)

    result = _client()._request("GET", "/test")

    assert result["code"] == 0
    assert len(calls) == 2
    assert sleeps == [1]


def test_non_retryable_business_error_fails_immediately(monkeypatch):
    calls = []
    responses = [FakeResponse({"code": 40001, "message": "invalid seller id"})]
    monkeypatch.setattr(
        lingxing_module.httpx, "Client",
        lambda timeout: FakeHttpClient(responses, calls),
    )
    try:
        _client()._request("GET", "/test")
    except LingxingError as exc:
        assert "invalid seller id" in str(exc)
    else:
        raise AssertionError("business error should not be retried")
    assert len(calls) == 1
