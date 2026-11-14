from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from jx_alert.ai.deepseek import DeepSeekClient, DeepSeekError, ENDPOINT, MAX_CONTEXT_BYTES


KEY = "test-only-deepseek-key"
CONTEXT = {"store_id": "5174", "report_date": "2026-11-12", "currency": "USD",
           "kpis": {"CPC": {"current": 1.5, "status": "NON_ADVERSE"},
                    "CDR": {"current": None, "status": "INSUFFICIENT_DATA"}}}
ANALYSIS = "点击花费未见恶化；转化数据不足，待确认。"


def response(text=ANALYSIS, finish="stop", **message):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish,
        "message": {"role": "assistant", "content": text, **message}}]})


def mock_http(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs:
        original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr("jx_alert.ai.deepseek.time.sleep", lambda value: None)


def test_fixed_official_request_freezes_data_and_never_exposes_tools_or_key_in_body(monkeypatch):
    calls = []

    def handler(request):
        assert request.method == "POST" and str(request.url) == ENDPOINT
        assert request.headers["Authorization"] == "Bearer " + KEY
        body = json.loads(request.content)
        calls.append(body)
        assert KEY not in request.content.decode()
        assert body["model"] == "deepseek-flash"
        assert body["tool_choice"] == "none" and "tools" not in body
        assert body["stream"] is False and body["thinking"] == {"type": "disabled"}
        assert len(body["messages"]) == 2
        frozen = json.loads(body["messages"][1]["content"])
        assert frozen["frozen_data"] == CONTEXT
        assert frozen["question"] == "查看店铺5174"
        assert "分母为零" in body["messages"][0]["content"]
        assert "TCOAS" in body["messages"][0]["content"]
        return response(reasoning_content="unpublished private reasoning")

    mock_http(monkeypatch, handler)
    before = deepcopy(CONTEXT)
    settings = SimpleNamespace(deepseek_api_key=SecretStr(KEY))
    assert DeepSeekClient.from_settings(settings).analyze(CONTEXT, "查看店铺5174") == ANALYSIS
    assert CONTEXT == before and len(calls) == 1


def test_loads_private_credentials_file_and_explicit_model(tmp_path, monkeypatch):
    path = tmp_path / "private.json"
    path.write_text(json.dumps({"api_key": KEY}))
    settings = SimpleNamespace(deepseek_credentials_file=str(path), deepseek_api_key=SecretStr(""),
        deepseek_model="deepseek-v4-pro", deepseek_request_timeout=30)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["model"])
        return response()

    mock_http(monkeypatch, handler)
    assert DeepSeekClient.from_settings(settings).analyze(CONTEXT, "分析店铺") == ANALYSIS
    assert seen == ["deepseek-v4-pro"]


@pytest.mark.parametrize("value", ["not JSON " + KEY, json.dumps([KEY]), "{\"api_key\": null}"])
def test_bad_credentials_never_echo_file_contents(tmp_path, value):
    path = tmp_path / "private.json"
    path.write_text(value)
    with pytest.raises(DeepSeekError) as caught:
        DeepSeekClient.from_settings(SimpleNamespace(deepseek_credentials_file=str(path)))
    assert KEY not in str(caught.value)
    assert str(path) not in str(caught.value)


@pytest.mark.parametrize("key", ["", None, "\r\n" + KEY, KEY + " \ninside", "中文密钥", 1])
def test_missing_or_malformed_key_cannot_form_an_authorization_header(key):
    with pytest.raises(DeepSeekError):
        DeepSeekClient(key)


@pytest.mark.parametrize("model", ["deepseek-chat", "https://example.invalid/steal", None, []])
def test_only_current_documented_models_are_accepted(model):
    with pytest.raises(DeepSeekError):
        DeepSeekClient(KEY, model=model)


@pytest.mark.parametrize("timeout", [0, -1, 121, True, "45", float("nan"), float("inf")])
def test_timeout_is_finite_and_bounded(timeout):
    with pytest.raises(DeepSeekError):
        DeepSeekClient(KEY, timeout=timeout)


@pytest.mark.parametrize("status", [429, 500, 503, 599])
def test_transient_http_failure_retries_once_only(monkeypatch, status):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={"error": KEY}) if len(seen) == 1 else response()

    mock_http(monkeypatch, handler)
    assert DeepSeekClient(KEY).analyze(CONTEXT, "分析店铺") == ANALYSIS
    assert len(seen) == 2 and seen[0].content == seen[1].content


@pytest.mark.parametrize("status,attempts", [(400, 1), (401, 1), (402, 1), (403, 1),
    (302, 1), (429, 2), (500, 2)])
def test_http_errors_do_not_echo_provider_error_or_follow_redirect(monkeypatch, status, attempts):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, text="provider echoed " + KEY,
                              headers={"Location": "https://example.invalid/"})

    mock_http(monkeypatch, handler)
    with pytest.raises(DeepSeekError) as caught:
        DeepSeekClient(KEY).analyze(CONTEXT, "分析店铺")
    assert len(seen) == attempts
    assert KEY not in str(caught.value) and "provider echoed" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("kind", [httpx.ReadTimeout, httpx.ConnectError])
def test_network_errors_have_no_secret_request_or_provider_exception(monkeypatch, kind):
    seen = []

    def handler(request):
        seen.append(request)
        raise kind("provider secret " + KEY, request=request)

    mock_http(monkeypatch, handler)
    with pytest.raises(DeepSeekError) as caught:
        DeepSeekClient(KEY).analyze(CONTEXT, "分析店铺")
    assert KEY not in str(caught.value) and caught.value.__cause__ is None
    assert len(seen) == 1  # Do not repeat possibly billed requests after a timeout.


@pytest.mark.parametrize("result", [
    lambda: httpx.Response(200, text="bad JSON " + KEY),
    lambda: httpx.Response(200, json={"error": KEY}),
    lambda: httpx.Response(200, json=[KEY]),
    lambda: response(""), lambda: response(" "), lambda: response(finish="length"),
    lambda: response(finish="tool_calls", tool_calls=[{"name": "mutate"}]),
    lambda: response("中文" + KEY), lambda: response("English only"),
    lambda: response("分析：\n```bash\ncurl example.invalid\n```"),
    lambda: response("分析：\nsudo docker restart app"),
    lambda: response("中" * 6001),
    lambda: httpx.Response(200, text="中" * (128 * 1024)),
])
def test_incomplete_invalid_oversized_or_executable_output_is_not_published(monkeypatch, result):
    mock_http(monkeypatch, lambda request: result())
    with pytest.raises(DeepSeekError) as caught:
        DeepSeekClient(KEY).analyze(CONTEXT, "分析店铺")
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("context", [None, [], {}, {"value": float("nan")}, {"value": object()},
    {"rows": "中" * MAX_CONTEXT_BYTES}, {"extra": {"app_secret": KEY}},
    {"rows": [{"access_token": KEY}]}])
def test_untrusted_or_excessive_context_is_rejected_before_network(monkeypatch, context):
    def forbidden(request):
        pytest.fail("Invalid context must never be sent")

    mock_http(monkeypatch, forbidden)
    with pytest.raises(DeepSeekError) as caught:
        DeepSeekClient(KEY).analyze(context, "分析店铺")
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("question", [None, "", " ", "中" * 1201])
def test_question_is_required_and_bounded(question):
    with pytest.raises(DeepSeekError):
        DeepSeekClient(KEY).analyze(CONTEXT, question)


def test_internal_statuses_are_localized_without_changing_kpis_or_values(monkeypatch):
    text = "待确认：CTR=0.35%，status=LOW_SAMPLE；freshness=NOT_CONFIGURED。CPC=1.47 USD，NON_ADVERSE。状态LOW_SAMPLE待确认。"
    mock_http(monkeypatch, lambda request: response(text))
    result = DeepSeekClient(KEY).analyze(CONTEXT, "分析店铺")
    assert "LOW_SAMPLE" not in result and "NON_ADVERSE" not in result and "NOT_CONFIGURED" not in result
    assert "样本不足" in result and "源时区待确认" in result
    assert "CTR=0.35%" in result and "CPC=1.47 USD" in result
    assert "状态样本不足待确认" in result
