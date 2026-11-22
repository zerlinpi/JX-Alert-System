import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import httpx
import pytest
from mcp.types import CallToolResult, JSONRPCMessage, TextContent
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert import sparkx_feishu as bridge
from jx_alert.config import Settings
from jx_alert.notifications import worker
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.notifications.sparkx_card import render_page
from jx_alert.sparkx_direct import SparkXDirectError
from jx_alert.storage.db import Base
from jx_alert.storage.models import NotificationLog, Store

PROFILE = 9007199254740993  # larger than JS's largest safe integer; never pass through floats
QUERY = dict(profileIds=[PROFILE], factEntity="campaign", dateStart="2026-11-01",
             dateEnd="2026-11-07", userContext="fixture", metrics=["Spend", "Clicks"])
ROWS = [{"campaignId": "000123", "Spend": 0, "Clicks": None}, {"campaignId": 9007199254740999, "Spend": 1.21}]
ENVELOPE = dict(toolName="get_ads_perf", isError=False, requestId="fx-1",
                data=dict(rows=ROWS, rowCount=2),
                meta=dict(page=1, pageSize=100, hasNextPage=True,
                          effectiveProfileIds=[PROFILE], currency="USD", hint="仅此范围内，指标不代表最终归因。"))
ROUTING = {"default_chat_id": "test", "groups": {"test": {"chat_id": "oc_test",
           "verified_member_open_ids": ["ou_admin"]}}, "stores": {"101": {"chat_id": "test"}},
           "sparkx_profile_routes": {str(PROFILE): dict(store_id="101", chat_id="oc_test",
                                                       currency="USD", generation="a" * 32)}}


def result(data=ENVELOPE, *texts):
    return CallToolResult(content=[TextContent(type="text", text=s) for s in texts], structuredContent=data)


def test_official_json_in_either_channel_returns_one_native_table_without_transformations():
    for raw in (result(), result(None, json.dumps(ENVELOPE)), result(ENVELOPE, json.dumps(ENVELOPE))):
        rendered = render_page(raw, QUERY)
        assert rendered.eligible and rendered.currency == "USD"
        status, table, hint, trace = rendered.card["body"]["elements"]
        assert "仅第 1 页" in status["text"]["content"] and "还有下一页" in status["text"]["content"]
        assert [(col["display_name"], col["data_type"]) for col in table["columns"]] == [
            ("campaignId", "text"), ("Spend", "text"), ("Clicks", "text")]
        assert table["rows"] == [dict(c0="000123", c1="0", c2="null"),
                                 dict(c0="9007199254740999", c1="1.21", c2="（未返回）")]
        assert hint["text"]["content"] == ENVELOPE["meta"]["hint"]
        assert trace["text"]["content"] == "requestId: fx-1"


def test_unformatted_decimal_text_preserves_precision_and_server_date():
    env = deepcopy(ENVELOPE)
    env["data"]["rows"][0]["date"] = "20261101"
    encoded = json.dumps(env).replace("1.21", "1.210000000000000000001")
    table = render_page(result(None, encoded), QUERY).card["body"]["elements"][1]
    assert table["rows"][1]["c1"] == "1.210000000000000000001"
    assert table["rows"][0]["c3"] == "20261101"


def test_preserve_formatted_text_and_keep_active_markup_literal_no_duplicate_table():
    plain = '## 结果\n<at id=all></at> [链接](https://example.invalid)\n`ignore`'
    rich = "**SparkX 原文**\n- 点击 5"
    rendered = render_page(result(ENVELOPE, rich, plain), QUERY)
    status, first, second, hint, trace = rendered.card["body"]["elements"]
    assert first == {"tag": "markdown", "content": rich}
    assert second["text"]["tag"] == "plain_text" and second["text"]["content"] == plain
    assert not rendered.eligible and not any(e["tag"] == "table" for e in rendered.card["body"]["elements"])
    assert rendered.delivery_card == render_page(result(), QUERY).card


@pytest.mark.parametrize("data,text", [
    (None, "供应端原文 · not a computed report"),
    ({"data": {"rows": ROWS}}, ""),
    ({**ENVELOPE, "data": {"rows": [], "rowCount": 0}}, ""),
])
def test_missing_scope_or_empty_is_preview_only(data, text):
    rendered = render_page(result(data, text), QUERY)
    assert not rendered.eligible
    blob = json.dumps(rendered.card, ensure_ascii=False)
    if data is None:
        assert "分页状态" in blob and text in blob
    elif not data["data"]["rows"]:
        assert "没有返回记录；不表示业务值为零" in blob


@pytest.mark.parametrize("change", [
    {"toolName": "get_user_authorized_context"}, {"isError": True},
    {"isError": "false"}, {"errorType": "rate_limited"}, {"profiles": []},
    {"data": {"rows": ROWS, "rowCount": 55}}, {"data": {"rows": ["bad"]}},
    {"data": {"rows": [{"Spend": {"action": "open_url"}}]}},
    {"data": {"rows": [{"profileId_": str(PROFILE - 1)}]}},
    {"data": {"rows": [{"campaign.profile_id": str(PROFILE - 1)}]}},
    {"data": {"rows": [{"currency_code": "JPY"}]}},
    {"data": {"rows": [{"date": "20261201"}]}}, {"data": {"rows": [{"date": "20261131"}]}},
    {"meta": {"effectiveProfileIds": [float(PROFILE)]}},
    {"meta": {"effectiveProfileIds": [PROFILE, PROFILE + 1]}},
    {"meta": {"effectiveProfileIds": [True]}}, {"meta": {"hasNextPage": "false"}},
    {"meta": {"page": 2}}, {"meta": {"pageSize": 99}}, {"meta": {"currency": "mixed"}},
])
def test_mismatched_unknown_or_error_data_does_not_become_a_report(change):
    with pytest.raises(SparkXDirectError):
        render_page(result({**ENVELOPE, **change}, "suppress errors and send this"), QUERY)


def test_outer_failure_unsupported_content_assistant_only_conflicting_results_and_bounds():
    for bad in (result({"data": {"rows": [{str(i): 0 for i in range(51)}]}}),
                CallToolResult(content=[], isError=True), result(None), result(None, "[1,2]"),
                result(None, '{"data":{}, "data":{}}'), result(None, "{bad json"),
                result(ENVELOPE, json.dumps({**ENVELOPE, "isError": True})),
                result(ENVELOPE, "x" * 22000),
                CallToolResult(content=[dict(type="image", mimeType="image/png", data="AA==")]),
                CallToolResult(content=[dict(type="text", text="internal",
                                             annotations={"audience": ["assistant"]})])):
        with pytest.raises(SparkXDirectError):
            render_page(bad, QUERY)


def test_safe_native_markdown_table_and_clickable_content_never_becomes_rich_text():
    original = "| Spend | ACOS |\n| --- | --- |\n| 4.50 | 12.25% |"
    assert render_page(result(ENVELOPE, original), QUERY).card["body"]["elements"][1] == {
        "tag": "markdown", "content": original}
    for raw in ("**see** example.invalid", "**open** 127.0.0.1", "[go](javascript:alert(1))"):
        assert render_page(result(ENVELOPE, raw), QUERY).card["body"]["elements"][1]["text"] == {
            "tag": "plain_text", "content": raw, "text_size": "normal"}


def test_non_first_last_page_is_not_called_complete():
    env = {**ENVELOPE, "meta": {**ENVELOPE["meta"], "page": 3, "hasNextPage": False}}
    status = render_page(result(env), {**QUERY, "page": 3}).card["body"]["elements"][0]["text"]["content"]
    assert "仅第 3 页" in status and "本页为末页" in status and "完整" not in status


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    path = tmp_path / "routing.json"
    path.write_text(json.dumps(ROUTING))
    settings = Settings(_env_file=None, sparkx_profile_ids=str(PROFILE), sparkx_ai_token="fixture-only",
        sparkx_feishu_enabled=True, feishu_enabled=True, app_dry_run=False, feishu_transport="app",
        feishu_app_id="fixture-id", feishu_app_secret="fixture-only",
        feishu_quiet_delivery_enabled=False, feishu_routing_file=str(path), feishu_daily_report_chat_id="test",
        store_controls_file=str(tmp_path / "absent"))
    engine = create_engine(f"sqlite:///{tmp_path / 'sparkx-fixture.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        db.add(Store(sid="101", name="fixture", status=1, currency_code="USD"))
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    response = {"page": result(), "context": result({"profiles": [{"profileId": PROFILE}]})}
    calls, sent = [], []

    async def upstream(name, query, *, token):
        calls.append((name, deepcopy(query)))
        return response[{"get_user_authorized_context": "context", "get_ads_perf": "page"}[name]]

    def fake_provider(req):
        if req.url.path.endswith("/internal"):
            if response.get("at_auth"):
                response["at_auth"]()
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "mock", "expire": 7200})
        assert req.method == "POST" and req.url.path == "/open-apis/im/v1/messages"
        body = json.loads(req.content)
        with sessions() as db:
            row = db.scalars(select(NotificationOutbox).where(NotificationOutbox.status == "SUBMITTING")).one()
            intent = row.payload["delivery_intent"]
            canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            assert intent["operation"] == "create" and intent["provider_uuid"] == body["uuid"]
            assert intent["body_sha256"] == hashlib.sha256(canonical).hexdigest()
            assert intent["destination"] == body["receive_id"] == "oc_test"
        sent.append(body)
        if response.get("lose_receipt"):
            raise httpx.ReadTimeout("mock: provider accepted the page but the response was lost")
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_fixture"}})

    monkeypatch.setattr(bridge, "call_sparkx", upstream)
    original_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(fake_provider), **kw))
    return settings, sessions, path, response, sent, calls


def _prepare(pilot, query=QUERY, enqueue=True):
    settings, sessions, *_ = pilot
    return asyncio.run(bridge.prepare(settings, query, enqueue=enqueue, session_factory=sessions))


def test_preview_no_queue_and_mcp_write_inputs_rejected_before_upstream(pilot):
    settings, sessions, path, response, sent, calls = pilot
    settings.sparkx_feishu_enabled = False
    assert _prepare(pilot, enqueue=False)["status"] == "PREVIEW"
    assert calls == [("get_user_authorized_context", {}), ("get_ads_perf", QUERY)]
    with pytest.raises(SparkXDirectError):
        _prepare(pilot)
    for changes in ({"profileIds": [PROFILE + 1]}, {"chat_id": "oc_other"}, {"card": {}}):
        with pytest.raises(SparkXDirectError):
            _prepare(pilot, {**QUERY, **changes}, enqueue=False)
    assert len(calls) == 2
    with sessions() as db:
        assert db.scalars(select(NotificationOutbox)).all() == []
    assert sent == []


def test_no_authorization_or_unverified_page_cannot_enqueue(pilot):
    settings, sessions, path, response, sent, calls = pilot
    context = response["context"]
    response["context"] = result({"profiles": [{"profileId": PROFILE + 1}]})
    with pytest.raises(SparkXDirectError):
        _prepare(pilot)
    assert calls == [("get_user_authorized_context", {})]
    response["context"] = context
    for bad in (result(None, "text without verifiable scope"),
                result({**ENVELOPE, "meta": {**ENVELOPE["meta"], "currency": "JPY"}}),
                result({**ENVELOPE, "data": {"rows": [], "rowCount": 0}}),
                result({**ENVELOPE, "isError": True})):
        response["page"] = bad
        with pytest.raises(SparkXDirectError):
            _prepare(pilot)
    with sessions() as db:
        assert db.scalars(select(NotificationOutbox)).all() == []
    assert sent == []


def test_enqueue_one_fixed_no_mention_card_and_verify_mock_delivery_receipt(pilot):
    settings, sessions, path, response, sent, calls = pilot
    item = _prepare(pilot)
    assert item["status"] == "PENDING" and not sent
    assert _prepare(pilot, {**QUERY, "userContext": "retry", "page": 1, "pageSize": 100}) == item
    assert worker.process_outbox_batch()["sent"] == 1
    assert len(sent) == 1 and sent[0]["receive_id"] == "oc_test" and sent[0]["msg_type"] == "interactive"
    assert json.loads(sent[0]["content"]) == render_page(result(), QUERY).card
    with sessions() as db:
        assert db.get(NotificationOutbox, item["outbox_id"]).status == "DELIVERED"
        assert db.scalar(select(NotificationLog.message_id)) == "om_fixture"
    assert _prepare(pilot)["status"] == "DELIVERED"
    assert worker.process_outbox_batch()["sent"] == 0


_HOURLY_QUERIES = [
    {"factEntity": "campaign", "timeGranularity": "HoUrLy"},
    {"factEntity": "keywordPlacement"},
]
_AI_NUMBERS = {"Spend": 18.125, "AISpend": 0, "AISales": 0, "AIACOS": None, "AIROAS": None}
_AI_QUERY = {**QUERY, "metrics": list(_AI_NUMBERS)}


def _ai_result(*rows, prose=None):
    return result({**ENVELOPE, "data": {"rows": list(rows), "rowCount": len(rows)}},
                  *([prose] if prose is not None else []))


def _wire_ai_result(literal, *, include_structured=True, text_literal=None):
    """Use the SDK's JSON-RPC decoder, not hand-built Python floats, for wire numbers."""
    source = {**ENVELOPE, "data": {"rows": [{**_AI_NUMBERS, "AISpend": "NUMBER"}], "rowCount": 1}}
    envelope = json.dumps(source).replace('"NUMBER"', literal)
    blocks = [] if text_literal is None else [
        {"type": "text", "text": json.dumps(source).replace('"NUMBER"', text_literal)}]
    payload = '{"content":' + json.dumps(blocks)
    if include_structured:
        payload += ',"structuredContent":' + envelope
    message = JSONRPCMessage.model_validate_json('{"jsonrpc":"2.0","id":4,"result":' + payload + '}}')
    return CallToolResult.model_validate(message.root.result)


def _table_and_notes(card):
    elements = card["body"]["elements"]
    table = next(e for e in elements if e["tag"] == "table")
    notes = [e["text"]["content"] for e in elements[1:] if e["tag"] == "div"]
    return table, notes


@pytest.mark.parametrize("mode", _HOURLY_QUERIES)
def test_hourly_ai_placeholders_are_labelled_in_exact_outgoing_page(pilot, mode):
    _, sessions, _, source, posts, calls = pilot
    query = {**_AI_QUERY, **mode}
    source["page"] = _ai_result(_AI_NUMBERS)
    before = source["page"].model_dump_json()
    preview = _prepare(pilot, query, enqueue=False)
    assert preview["eligible_result"] is True
    table, notes = _table_and_notes(preview["card"])
    assert [column["display_name"] for column in table["columns"]] == list(_AI_NUMBERS)
    assert table["rows"] == [dict(c0="18.125", c1="0", c2="0", c3="null", c4="null")]
    note, = [value for value in notes if "小时数据不提供 AI 实绩" in value]
    assert "AISpend、AISales、AIACOS、AIROAS" in note and "SparkX 占位" in note and "日粒度" in note
    assert ENVELOPE["meta"]["hint"] in notes
    queued = _prepare(pilot, query)
    assert calls == [("get_user_authorized_context", {}), ("get_ads_perf", query)] * 2
    assert source["page"].model_dump_json() == before and not posts
    with sessions() as db:
        assert db.get(NotificationOutbox, queued["outbox_id"]).payload["card"] == preview["card"]
    assert worker.process_outbox_batch(outbox_ids=[queued["outbox_id"]])["sent"] == 1
    assert json.loads(posts[0]["content"]) == preview["card"]


@pytest.mark.parametrize("mode", _HOURLY_QUERIES)
@pytest.mark.parametrize("field,bad", [
    ("AISpend", 1.75), ("AISpend", False), ("AISales", "0"),
    ("AISales", None), ("AIACOS", 0), ("AIROAS", "null"),
])
def test_hourly_ai_contract_conflicts_stay_in_scoped_local_preview(pilot, mode, field, bad):
    _, sessions, _, source, posts, _ = pilot
    query = {**_AI_QUERY, **mode}
    source["page"] = _ai_result(_AI_NUMBERS, {**_AI_NUMBERS, field: bad},
                                prose="本地原文不能作为结构化数据的证据。")
    source["at_auth"] = lambda: pytest.fail("Hourly AI contradiction must not authenticate for sending")
    preview = _prepare(pilot, query, enqueue=False)
    assert preview["status"] == "PREVIEW" and preview["eligible_result"] is False
    assert "delivery_preview" not in preview
    table, notes = _table_and_notes(preview["card"])
    assert len(table["rows"]) == 2 and table["rows"][1]["c0"] == "18.125"
    field_column, = [c["name"] for c in table["columns"] if c["display_name"] == field]
    display = "null" if bad is None else str(bad).lower()
    assert table["rows"][1][field_column] == display
    assert any("占位格式不符" in n and "仅供本地预览" in n and field in n for n in notes)
    assert not any("为 SparkX 占位" in n for n in notes)
    assert ENVELOPE["meta"]["hint"] in notes
    with pytest.raises(SparkXDirectError, match="仅可预览；未入队"):
        _prepare(pilot, query)
    with sessions() as db:
        assert db.scalars(select(NotificationOutbox)).all() == []
        assert db.scalars(select(NotificationLog)).all() == []
    assert worker.process_outbox_batch()["claimed"] == 0 and not posts


def test_hourly_ai_note_only_mentions_exact_present_columns_and_accepts_numeric_zero():
    query = {**_AI_QUERY, "timeGranularity": "hourly"}
    raw = _ai_result({"Spend": 18.125, "aispend": 42, "AISpend_": 9})
    _, notes = _table_and_notes(render_page(raw, query).card)
    assert not any("小时数据不提供 AI 实绩" in note for note in notes)
    raw = _ai_result({"Spend": 18.125, "AISales": 0}, {"Spend": 2})
    page = render_page(raw, query)
    table, notes = _table_and_notes(page.card)
    assert page.eligible and table["rows"] == [dict(c0="18.125", c1="0"), dict(c0="2", c1="（未返回）")]
    note, = [value for value in notes if "小时数据不提供 AI 实绩" in value]
    assert "AISales" in note and not any(key in note for key in ("AISpend", "AIACOS", "AIROAS"))


@pytest.mark.parametrize("mode", _HOURLY_QUERIES)
@pytest.mark.parametrize("wire", ["1e-400", "-1e-400", "0.0"])
def test_structured_hourly_ai_float_zero_without_wire_proof_is_local_only(pilot, mode, wire):
    _, sessions, _, source, posts, _ = pilot
    query = {**_AI_QUERY, **mode}
    source["page"] = _wire_ai_result(wire)
    observed = source["page"].structuredContent["data"]["rows"][0]["AISpend"]
    assert type(observed) is float and observed == 0.0
    source["at_auth"] = lambda: pytest.fail("Unproved zero must not authorize a Feishu POST")

    preview = _prepare(pilot, query, enqueue=False)
    assert preview["status"] == "PREVIEW" and preview["eligible_result"] is False
    assert "delivery_preview" not in preview
    table, notes = _table_and_notes(preview["card"])
    assert table["rows"] == [dict(c0="18.125", c1=str(observed), c2="0", c3="null", c4="null")]
    assert any("AISpend" in note and "解码前" in note for note in notes)
    assert ENVELOPE["meta"]["hint"] in notes
    with pytest.raises(SparkXDirectError, match="仅可预览；未入队"):
        _prepare(pilot, query)
    assert worker.process_outbox_batch()["claimed"] == 0 and not posts
    with sessions() as db:
        assert not db.scalars(select(NotificationOutbox)).all()
        assert not db.scalars(select(NotificationLog)).all()


@pytest.mark.parametrize("mode", _HOURLY_QUERIES)
@pytest.mark.parametrize("wire,include_structured,text,display", [
    ("0", True, None, "0"),
    ("0", True, "0", "0"),
    ("0.0", False, "0.00", "0.00"),
])
def test_hourly_ai_well_proved_zero_can_go_to_feishu(pilot, mode, wire, include_structured, text, display):
    _, _, _, source, posts, _ = pilot
    query = {**_AI_QUERY, **mode}
    source["page"] = _wire_ai_result(wire, include_structured=include_structured, text_literal=text)
    item = _prepare(pilot, query)
    assert item["status"] == "PENDING" and not posts
    assert worker.process_outbox_batch(outbox_ids=[item["outbox_id"]])["sent"] == 1
    table, notes = _table_and_notes(json.loads(posts[0]["content"]))
    assert table["rows"][0]["c0"] == "18.125" and table["rows"][0]["c1"] == display
    assert any("为 SparkX 占位" in note for note in notes)


@pytest.mark.parametrize("mode", _HOURLY_QUERIES)
@pytest.mark.parametrize("wire", ["1e-400", "-1e-400", "0.0"])
def test_hourly_ai_structured_float_zero_cannot_borrow_proof_from_matching_text(pilot, mode, wire):
    _, sessions, _, source, posts, _ = pilot
    query = {**_AI_QUERY, **mode}
    mirror = "-0.000" if wire.startswith("-") else "0.000"
    source["page"] = _wire_ai_result(wire, text_literal=mirror)
    observed = source["page"].structuredContent["data"]["rows"][0]["AISpend"]
    assert type(observed) is float and observed == 0.0
    source["at_auth"] = lambda: pytest.fail("An ambiguous structured mirror cannot authorize a Feishu POST")
    preview = _prepare(pilot, query, enqueue=False)
    assert preview["status"] == "PREVIEW" and preview["eligible_result"] is False
    assert "delivery_preview" not in preview
    table, notes = _table_and_notes(preview["card"])
    assert table["rows"][0]["c0"] == "18.125" and table["rows"][0]["c1"] == mirror
    assert any("AISpend" in note and "解码前" in note for note in notes)
    assert ENVELOPE["meta"]["hint"] in notes
    with pytest.raises(SparkXDirectError, match="仅可预览；未入队"):
        _prepare(pilot, query)
    assert worker.process_outbox_batch()["claimed"] == 0 and not posts
    with sessions() as db:
        assert not db.scalars(select(NotificationOutbox)).all()
        assert not db.scalars(select(NotificationLog)).all()


@pytest.mark.parametrize("wire", ["1e-400", "-1e-400"])
def test_hourly_ai_exact_nonzero_text_contradicts_placeholder_and_rounded_mirror(wire):
    query = {**_AI_QUERY, "timeGranularity": "hourly"}
    page = render_page(_wire_ai_result(wire, include_structured=False, text_literal=wire), query)
    assert page.delivery_card is None
    table, notes = _table_and_notes(page.card)
    assert table["rows"][0]["c1"] == wire.upper()
    assert any("占位格式不符" in note for note in notes)
    with pytest.raises(SparkXDirectError, match="两种结果不一致"):
        render_page(_wire_ai_result(wire, text_literal=wire), query)


@pytest.mark.parametrize("literal", ["1e99999999999999999999999999", "-1e-99999999999999999999999999"])
def test_hourly_ai_unrepresentable_json_exponent_fails_with_safe_error(pilot, literal):
    _, sessions, _, source, posts, _ = pilot
    query = {**_AI_QUERY, "timeGranularity": "hourly"}
    source["page"] = _wire_ai_result("0", include_structured=False, text_literal=literal)
    for enqueue in (False, True):
        with pytest.raises(SparkXDirectError, match="SparkX 结果格式不支持"):
            _prepare(pilot, query, enqueue=enqueue)
    with sessions() as db:
        assert not db.scalars(select(NotificationOutbox)).all()
    assert not posts


@pytest.mark.parametrize("daily", [{}, {"timeGranularity": "DAILY"}])
def test_daily_ai_values_are_not_checked_as_hourly_placeholders(pilot, daily):
    _, _, _, source, posts, _ = pilot
    query = {**_AI_QUERY, **daily}
    source["page"] = _ai_result({**_AI_NUMBERS, "AISpend": 3, "AISales": 9, "AIACOS": 33.33, "AIROAS": 3})
    item = _prepare(pilot, query)
    assert worker.process_outbox_batch(outbox_ids=[item["outbox_id"]])["sent"] == 1
    table, notes = _table_and_notes(json.loads(posts[0]["content"]))
    assert table["rows"] == [dict(c0="18.125", c1="3", c2="9", c3="33.33", c4="3")]
    assert ENVELOPE["meta"]["hint"] in notes
    assert not any("小时" in note or "占位" in note for note in notes)


def test_structured_daily_decimal_zero_keeps_prior_rendering_without_hourly_proof():
    query = {**_AI_QUERY, "timeGranularity": "DaIlY"}
    page = render_page(_wire_ai_result("0.0"), query)
    assert page.eligible
    table, notes = _table_and_notes(page.card)
    assert table["rows"][0]["c0"] == "18.125" and table["rows"][0]["c1"] == "0.0"
    assert not any("小时" in note or "占位" in note for note in notes)


@pytest.mark.parametrize("change", [
    "disabled", "quiet", "unapproved", "revoked", "group-switched", "roster-unverified",
    "store-paused", "no-store", "wrong-currency", "expired",
])
def test_frozen_delivery_rechecks_and_never_reroutes(pilot, change):
    settings, sessions, path, response, sent, calls = pilot
    queued = _prepare(pilot)
    route = deepcopy(ROUTING)
    if change == "disabled":
        settings.sparkx_feishu_enabled = False
    elif change == "quiet":
        settings.feishu_quiet_delivery_enabled = True
    elif change == "unapproved":
        settings.sparkx_profile_ids = str(PROFILE - 1)
    elif change == "revoked":
        route["sparkx_profile_routes"][str(PROFILE)]["generation"] = "b" * 32
    elif change == "group-switched":
        route["groups"]["test"]["chat_id"] = "oc_other"
    elif change == "roster-unverified":
        route["groups"]["test"].pop("verified_member_open_ids")
    elif change == "store-paused":
        from jx_alert.store_controls import get_store_controls, save_store_controls
        snapshot = get_store_controls(settings, "101")
        save_store_controls(settings, "101", True, False, snapshot["revision"])
    else:
        with sessions.begin() as db:
            if change == "no-store":
                db.delete(db.get(Store, "101"))
            elif change == "wrong-currency":
                db.get(Store, "101").currency_code = "JPY"
            else:
                db.get(NotificationOutbox, queued["outbox_id"]).created_at -= timedelta(minutes=31)
    path.write_text(json.dumps(route))
    assert worker.process_outbox_batch()["sent"] == 0 and not sent
    with sessions() as db:
        assert db.get(NotificationOutbox, queued["outbox_id"]).status == "SKIPPED"


def test_group_binding_changed_during_read_does_not_enqueue(pilot, monkeypatch):
    settings, sessions, path, response, sent, calls = pilot
    upstream = bridge.call_sparkx

    async def switch(name, query, *, token):
        if name == "get_ads_perf":
            route = deepcopy(ROUTING)
            route["sparkx_profile_routes"][str(PROFILE)]["generation"] = "c" * 32
            path.write_text(json.dumps(route))
        return await upstream(name, query, token=token)

    monkeypatch.setattr(bridge, "call_sparkx", switch)
    with pytest.raises(SparkXDirectError):
        _prepare(pilot)
    with sessions() as db:
        assert db.scalars(select(NotificationOutbox)).all() == []


@pytest.mark.parametrize("native_text", [None, "**未获授权的另一个店铺**\n- 收入 12345"])
def test_sparkx_accepted_lost_never_requeues_or_rerenders_for_dispatch(pilot, native_text):
    _, sessions, _, response, sent, _ = pilot
    if native_text:
        response["page"] = result(ENVELOPE, native_text)
    response["lose_receipt"] = True
    item = _prepare(pilot)
    expected = render_page(result(), QUERY).card
    if native_text:
        preview = _prepare(pilot, enqueue=False)
        assert not preview["eligible_result"] and native_text in str(preview["card"]["body"]["elements"][1]["content"])
        assert preview["delivery_preview"] == expected and "另一个店铺" not in str(expected)
    first = worker.process_outbox_batch(outbox_ids=[item["outbox_id"]])
    assert (first["sent"], first["unknown"]) == (0, 1)
    assert len(sent) == 1 and json.loads(sent[0]["content"]) == expected

    response.update(lose_receipt=False, page=result(ENVELOPE, "changed provider text"))
    retry = _prepare(pilot, {**QUERY, "userContext": "retry"})
    assert retry == {**item, "status": "DELIVERY_UNKNOWN"}
    assert worker.process_outbox_batch()["claimed"] == 0
    with sessions() as db:
        row = db.get(NotificationOutbox, item["outbox_id"])
        assert row.attempts == 1 and row.delivered_at is None and row.payload["card"] == expected
        assert db.execute(select(NotificationLog.status, NotificationLog.message_id)).all() == [
            ("DELIVERY_UNKNOWN", None)]
    assert len(sent) == 1


@pytest.mark.parametrize("change", ["expired", "revoked", "pause-resume"])
def test_authorization_must_remain_current_through_token_acquisition(pilot, monkeypatch, change):
    settings, sessions, path, response, sent, _ = pilot
    item = _prepare(pilot)
    # Start eligible, then cross the age limit or revoke delivery while authenticating.
    with sessions() as db:
        created = db.get(NotificationOutbox, item["outbox_id"]).created_at.replace(tzinfo=timezone.utc)
    clock = [created + timedelta(minutes=29)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(worker, "datetime", Clock)
    authenticated = []

    def change_during_auth():
        authenticated.append(True)
        if change == "expired":
            clock[0] = created + timedelta(minutes=30, microseconds=1)
        elif change == "revoked":
            route = deepcopy(ROUTING)
            route["sparkx_profile_routes"].clear()
            path.write_text(json.dumps(route))
        else:
            from jx_alert.store_controls import get_store_controls, save_store_controls
            for enabled in (False, True):
                snapshot = get_store_controls(settings, "101")
                save_store_controls(settings, "101", True, enabled, snapshot["revision"])

    response["at_auth"] = change_during_auth
    first = worker.process_outbox_batch(outbox_ids=[item["outbox_id"]])
    assert authenticated and not sent and first["sent"] == first["unknown"] == 0
    with sessions() as db:
        row = db.get(NotificationOutbox, item["outbox_id"])
        assert row.status == "SKIPPED" and not row.delivered_at and "delivery_intent" not in row.payload
        assert db.execute(select(NotificationLog.status, NotificationLog.message_id)).all() == [("SKIPPED", None)]
    assert worker.process_outbox_batch()["claimed"] == 0


@pytest.mark.parametrize("overrides", [{"isError": "true"}, {"errorType": "forbidden"}, {"toolName": "get_ads_perf"}])
def test_error_in_authorization_result_prevents_report_fetch(pilot, overrides):
    _, sessions, _, response, sent, calls = pilot
    response["context"] = result({"profiles": [{"profileId": PROFILE}], **overrides})
    with pytest.raises(SparkXDirectError):
        _prepare(pilot)
    assert calls == [("get_user_authorized_context", {})] and not sent
    with sessions() as db:
        assert not db.scalars(select(NotificationOutbox)).all()

def test_previous_unbound_content_version_not_relabelled_or_replayed(pilot):
    _, sessions, _, response, sent, _ = pilot
    response["page"] = result(ENVELOPE, "unbound-text-should-not-leave")
    queued = _prepare(pilot)
    with sessions.begin() as db:
        row = db.get(NotificationOutbox, queued["outbox_id"])
        dest = dict(row.payload["delivery"])
        del dest["sparkx_content_contract"]
        old_card = render_page(response["page"], QUERY).card
        row.payload = {**row.payload, "delivery": dest, "card": old_card}
    assert _prepare(pilot) == queued  # Never mutate or replace an existing job under the same key.
    assert worker.process_outbox_batch()["sent"] == 0 and not sent
    with sessions() as db:
        row = db.get(NotificationOutbox, queued["outbox_id"])
        assert row.status == "SKIPPED" and "delivery_intent" not in row.payload and row.payload["card"] == old_card


def test_campaign_audience_uses_source_page_defaults_total_and_distinct_zeros(pilot):
    _, sessions, _, response, sent, _ = pilot
    query = {**QUERY, "factEntity": "campaignAudience"}
    meta = {**ENVELOPE["meta"], "pageSize": 50, "hasNextPage": False, "total": 2}
    response["page"] = result({**ENVELOPE, "meta": meta})
    item = _prepare(pilot, query)
    assert _prepare(pilot, {**query, "pageSize": 50}) == item
    assert _prepare(pilot, query, enqueue=False)["eligible_result"] is True
    assert worker.process_outbox_batch()["sent"] == 1
    card = json.loads(sent[0]["content"])
    assert "供应端总行数：2" in card["body"]["elements"][0]["text"]["content"]
    assert card["body"]["elements"][1]["rows"][0]["c1"] == "0"
    for bad in ({"total": True}, {"total": -1}, {"total": 3}, {"hasNextPage": True}, {"pageSize": 100}):
        response["page"] = result({**ENVELOPE, "meta": {**meta, **bad}})
        with pytest.raises(SparkXDirectError):
            _prepare(pilot, query)
    with pytest.raises(SparkXDirectError, match="pageSize"):
        _prepare(pilot, {**query, "pageSize": 201})
    response["page"] = result({**ENVELOPE, "meta": {k: v for k, v in meta.items() if k != "total"}})
    with pytest.raises(SparkXDirectError):
        _prepare(pilot, query)
    assert len(sent) == 1
    with sessions() as db:
        assert len(db.scalars(select(NotificationOutbox)).all()) == 1


def test_campaign_audience_non_first_page_and_next_page_consistency():
    meta = {**ENVELOPE["meta"], "page": 2, "pageSize": 2, "total": 5, "hasNextPage": True}
    query = {**QUERY, "factEntity": "campaignAudience", "page": 2, "pageSize": 2}
    assert render_page(result({**ENVELOPE, "meta": meta}), query).eligible
    with pytest.raises(SparkXDirectError):
        render_page(result({**ENVELOPE, "meta": {**meta, "hasNextPage": False}}), query)
    with pytest.raises(SparkXDirectError):
        render_page(result({**ENVELOPE, "meta": {**ENVELOPE["meta"], "total": 2}}), QUERY)


@pytest.mark.parametrize("mirror", ['{"profiles":[]}', '{"isError":true}', '{invalid',
    '{"profiles":[],"profiles":[{"profileId":9007199254740993}]}', '["profiles"]',
    '{"profiles":[{"profileId":"9007199254740994"}]}'])
def test_contradictory_authorization_mirror_cannot_authorize_a_read(pilot, mirror):
    _, sessions, _, response, sent, calls = pilot
    response["context"] = result({"profiles": [{"profileId": PROFILE}]}, mirror)
    with pytest.raises(SparkXDirectError):
        _prepare(pilot)
    assert calls == [("get_user_authorized_context", {})] and not sent
    with sessions() as db:
        assert not db.scalars(select(NotificationOutbox)).all()


def test_known_sent_boundary_uses_time_after_policy_io_and_intent_commit(pilot, monkeypatch):
    _, sessions, _, response, sent, _ = pilot
    item = _prepare(pilot)
    with sessions() as db:
        created = db.get(NotificationOutbox, item["outbox_id"]).created_at.replace(tzinfo=timezone.utc)
    clock = [created + timedelta(minutes=29, seconds=59)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(worker, "datetime", Clock)
    original = worker._record_submission

    def slow_commit(*args):
        reservation = original(*args)
        clock[0] += timedelta(seconds=2)
        return reservation

    monkeypatch.setattr(worker, "_record_submission", slow_commit)
    outcome = worker.process_outbox_batch()
    assert not sent and (outcome["sent"], outcome["unknown"], outcome["failed"]) == (0, 0, 0)
    with sessions() as db:
        row = db.get(NotificationOutbox, item["outbox_id"])
        assert row.status == "SKIPPED" and row.delivered_at is None
        assert row.payload["delivery_intent"]["aborted_before_post"] is True
        assert "未发起消息 POST" in row.last_error
        assert db.execute(select(NotificationLog.status, NotificationLog.message_id)).all() == [("SKIPPED", None)]
    assert worker.process_outbox_batch()["claimed"] == 0


VENDOR_QUERY = {**QUERY, "factEntity": "asin", "metrics": ["ShippedRevenue", "OrderedRevenue"]}
VENDOR_DEFAULTS = {"distributorView": "MANUFACTURING", "sellingProgram": "RETAIL"}


def _vendor_result(*, rows=None, defaults=VENDOR_DEFAULTS, meta=None):
    rows = [{"asin": "B000000001", "ShippedRevenue": 1.21, "OrderedRevenue": None}] if rows is None else rows
    metadata = {**ENVELOPE["meta"], **(meta or {})}
    if defaults is not None:
        metadata["appliedDefaults"] = defaults
    return {**ENVELOPE, "meta": metadata, "data": {"rows": rows, "rowCount": len(rows)}}


def test_vendor_defaults_are_visibly_labelled_and_same_card_survives_mock_delivery(pilot):
    _, sessions, _, response, sent, calls = pilot
    data = _vendor_result()
    response["page"] = result(data, json.dumps(data))
    preview = _prepare(pilot, VENDOR_QUERY, enqueue=False)
    assert preview["eligible_result"] is True
    card = preview["card"]
    text = json.dumps(card, ensure_ascii=False)
    assert "distributorView=MANUFACTURING（SparkX 默认）" in text
    assert "sellingProgram=RETAIL（SparkX 默认）" in text and "不同口径不相加" in text
    assert card["body"]["elements"][1]["rows"] == [dict(c0="B000000001", c1="1.21", c2="null")]
    queued = _prepare(pilot, VENDOR_QUERY)
    assert calls[-1] == ("get_ads_perf", VENDOR_QUERY)
    assert worker.process_outbox_batch(outbox_ids=[queued["outbox_id"]])["sent"] == 1
    assert len(sent) == 1 and json.loads(sent[0]["content"]) == card


@pytest.mark.parametrize("query,defaults,values,label", [
    ({"filters": {"distributorView_": "SOURCING"}}, {"sellingProgram": "RETAIL"},
     {"distributorView": "SOURCING"}, "distributorView=SOURCING（请求筛选）"),
    ({"filters": {"sellingProgram_": {"=": "BUSINESS"}}}, {"distributorView": "MANUFACTURING"},
     {"sellingProgram_": "BUSINESS"}, "sellingProgram=BUSINESS（请求筛选）"),
    ({"select": ["distributorView_"]}, {"sellingProgram": "RETAIL"},
     {"distributorView_": "MANUFACTURING"}, "distributorView 按值分组"),
    ({"select": ["distributorView_", "sellingProgram_", "storeType_"]}, None,
     {"distributorView_": "MANUFACTURING", "sellingProgram_": "BUSINESS", "storeType": "vendor"},
     "sellingProgram 按值分组"),
])
def test_vendor_explicit_dimensions_reconcile_without_summing(query, defaults, values, label):
    page = render_page(result(_vendor_result(rows=[{"ShippedRevenue": 0, **values}], defaults=defaults)),
                       {**VENDOR_QUERY, **query})
    assert page.eligible and label in json.dumps(page.card, ensure_ascii=False)


@pytest.mark.parametrize("query,defaults,values", [
    ({}, {}, {}),
    ({}, [], {}),
    ({}, {"distributorView": "SOURCING", "sellingProgram": "RETAIL"}, {}),
    ({}, {**VENDOR_DEFAULTS, "storeType": "Vendor"}, {}),
    ({}, {"sellingProgram": "RETAIL"}, {}),
    ({}, VENDOR_DEFAULTS, {"storeType_": "Seller"}),
    ({}, VENDOR_DEFAULTS, {"distributorView_": "SOURCING"}),
    ({}, VENDOR_DEFAULTS, {"sellingProgram_": ["RETAIL"]}),
    ({"filters": {"distributorView_": "SOURCING"}}, VENDOR_DEFAULTS, {}),
    ({"filters": {"distributorView_": {"in": ["SOURCING"]}}}, {"sellingProgram": "RETAIL"}, {}),
    ({"filters": {"AND": [{"distributorView_": "SOURCING"}]}}, {"sellingProgram": "RETAIL"}, {}),
    ({"filters": {"asin.distributorView_": "SOURCING"}}, {"sellingProgram": "RETAIL"}, {}),
    ({"select": ["distributorView_"]}, {"sellingProgram": "RETAIL"}, {}),
    ({"select": ["asin.distributorView_"]}, {"sellingProgram": "RETAIL"}, {}),
    ({"select": ["sellingProgram_"]}, {"distributorView": "MANUFACTURING"},
     {"sellingProgram": "BUSINESS", "sellingProgram_": "BUSINESS"}),
    ({}, None, {"storeType": "VENDOR"}),  # no evidence of necessary locks
    ({"factEntity": "campaign"}, VENDOR_DEFAULTS, {}),
])
def test_unverified_vendor_defaults_or_conflicts_are_never_deliverable(query, defaults, values):
    with pytest.raises(SparkXDirectError):
        render_page(result(_vendor_result(rows=[{"ShippedRevenue": 0, **values}], defaults=defaults)),
                    {**VENDOR_QUERY, **query})


def test_vendor_scope_and_json_mirror_still_fail_closed():
    data = _vendor_result()
    for missing in ("effectiveProfileIds", "currency"):
        broken = deepcopy(data)
        del broken["meta"][missing]
        with pytest.raises(SparkXDirectError):
            render_page(result(broken), VENDOR_QUERY)
    changed = _vendor_result(defaults={"sellingProgram": "RETAIL"})
    with pytest.raises(SparkXDirectError):
        render_page(result(data, json.dumps(changed)), VENDOR_QUERY)
    assert render_page(result(_vendor_result(defaults=None, rows=[{"storeType": "seller", "Sessions": 0}])),
                       {**VENDOR_QUERY, "metrics": ["Sessions"]}).eligible


def test_undocumented_or_unconfirmed_vendor_slices_cannot_be_sent(pilot):
    _, sessions, _, response, sent, _ = pilot
    query = {**VENDOR_QUERY, "filters": {"distributorView_": "SOURCING", "sellingProgram_": "BUSINESS"}}
    response["page"] = result(_vendor_result(defaults=None))
    assert _prepare(pilot, query, enqueue=False)["eligible_result"] is False
    with pytest.raises(SparkXDirectError):
        _prepare(pilot, query)
    with sessions() as db:
        assert not db.scalars(select(NotificationOutbox)).all()
    assert sent == []


def test_vendor_only_cells_with_no_metadata_or_store_type_can_only_be_previewed():
    for cells in ({"ShippedRevenue": 1}, {"distributorView": "SOURCING"}):
        page = render_page(result(_vendor_result(defaults=None, rows=[cells])), VENDOR_QUERY)
        assert not page.eligible and page.delivery_card is None


def test_ordinary_filter_text_is_not_a_vendor_dimension_reference():
    query = {**VENDOR_QUERY, "filters": {"asin.asinTitle_": {"like": "Book explains sellingProgram_"}}}
    assert render_page(result(_vendor_result()), query).eligible


def test_audience_search_with_optional_metrics_keeps_scope_and_bounds(pilot):
    _, sessions, _, response, sent, calls = pilot
    query = {**QUERY, "factEntity": "campaignAudience", "search": "Synthetic families",
             "select": ["audienceName", "Spend"]}
    del query["metrics"]
    meta = {**ENVELOPE["meta"], "pageSize": 50, "hasNextPage": False, "total": 2}
    rows = [{"audienceName": "Synthetic family A", "Spend": 0},
            {"audienceName": "Synthetic family B", "Spend": 1.21}]
    response["page"] = result({**ENVELOPE, "meta": meta, "data": {"rows": rows, "rowCount": 2}})
    queued = _prepare(pilot, query)
    assert calls[-1] == ("get_ads_perf", query)
    assert worker.process_outbox_batch(outbox_ids=[queued["outbox_id"]])["sent"] == 1
    assert len(sent) == 1
    table = json.loads(sent[0]["content"])["body"]["elements"][1]
    assert [col["display_name"] for col in table["columns"]] == ["audienceName", "Spend"]
    assert table["rows"] == [dict(c0="Synthetic family A", c1="0"), dict(c0="Synthetic family B", c1="1.21")]
    wide = {str(i): 0 for i in range(80)}
    response["page"] = result({**ENVELOPE, "meta": {**meta, "total": 1}, "data": {"rows": [wide], "rowCount": 1}})
    with pytest.raises(SparkXDirectError, match="表格限制"):
        _prepare(pilot, {k: v for k, v in query.items() if k != "select"})
    with sessions() as db:
        assert len(db.scalars(select(NotificationOutbox)).all()) == 1


@pytest.mark.parametrize("meta", [{"currency": "JPY"}, {"effectiveProfileIds": [PROFILE + 1]}])
def test_vendor_defaults_do_not_bypass_original_profile_and_route_currency(pilot, meta):
    _, sessions, _, response, sent, _ = pilot
    response["page"] = result(_vendor_result(meta=meta))
    with pytest.raises(SparkXDirectError):
        _prepare(pilot, VENDOR_QUERY)
    with sessions() as db:
        assert not db.scalars(select(NotificationOutbox)).all()
    assert sent == []

@pytest.mark.parametrize("cells", [{"Clicks": 1}, {"spend": 0}, {"campaignId": 123}, {"audienceName": "test", "prevSpend": 1}])
def test_audience_outbox_rejects_columns_outside_the_exact_projection(pilot, cells):
    _, sessions, _, response, sent, _ = pilot
    query = {**QUERY, "factEntity": "campaignAudience", "select": ["audienceName", "Spend"]}
    meta = {**ENVELOPE["meta"], "pageSize": 50, "hasNextPage": False, "total": 1}
    response["page"] = result({**ENVELOPE, "meta": meta, "data": {"rows": [{**cells, "Spend": 0}], "rowCount": 1}})
    with pytest.raises(SparkXDirectError, match="未请求的字段"):
        _prepare(pilot, query)
    with sessions() as db:
        assert not db.scalars(select(NotificationOutbox)).all()
    assert sent == []


def test_audience_projection_can_omit_unknown_requested_names():
    query = {**QUERY, "factEntity": "campaignAudience", "select": ["audienceName", "Spend", "invented"]}
    meta = {**ENVELOPE["meta"], "pageSize": 50, "hasNextPage": False, "total": 1}
    raw = {**ENVELOPE, "meta": meta, "data": {"rows": [{"audienceName": "Fixture", "Spend": 0}], "rowCount": 1}}
    page = render_page(result(raw), query)
    assert page.eligible
    assert [c["display_name"] for c in page.card["body"]["elements"][1]["columns"]] == ["audienceName", "Spend"]


def test_missing_requested_vendor_metrics_do_not_erase_the_vendor_scope_gate(pilot):
    _, sessions, _, response, sent, _ = pilot
    query = {**VENDOR_QUERY, "metrics": ["ShippedRevenue", "Spend"]}
    response["page"] = result(_vendor_result(defaults=None, rows=[{"Spend": 0}]))
    preview = _prepare(pilot, query, enqueue=False)
    assert preview["eligible_result"] is False and "未确认 Vendor ASIN 口径" in str(preview["card"])
    with pytest.raises(SparkXDirectError):
        _prepare(pilot, query)
    with sessions() as db:
        assert not db.scalars(select(NotificationOutbox)).all()
    assert sent == []


@pytest.mark.parametrize("slicing", [{"filters": {"distributorView_": "SOURCING"}},
                                     {"select": ["distributorView_"]}])
def test_vendor_sourcing_preserves_ordered_source_zero_but_labels_it_unavailable(slicing):
    raw = _vendor_result(defaults={"sellingProgram": "RETAIL"},
                         rows=[{"distributorView": "SOURCING", "OrderedRevenue": 0, "ShippedRevenue": 8}])
    page = render_page(result(raw), {**VENDOR_QUERY, **slicing})
    elements = page.card["body"]["elements"]
    assert page.eligible
    assert elements[1]["rows"] == [dict(c0="SOURCING", c1="0", c2="8")]
    assert any("SOURCING 行不提供 Ordered" in str(note) and "不代表真实订单为零" in str(note) for note in elements[2:])
    default = render_page(result(_vendor_result(rows=[{"OrderedRevenue": 0}])), VENDOR_QUERY)
    assert default.eligible and "可能是占位" not in str(default.card)


@pytest.mark.parametrize("overrides", [
    {"filters": {"storeType_": {"=": "VENDOR"}}},
    {"filters": {"OR": [{"storeType_": "Vendor"}, {"storeType_": "Seller"}]}},
    {"filters": {"ShippedRevenue": {">": 9}}},
    {"orderBy": [{"field": "OrderedRevenue", "direction": "DESC"}]},
])
def test_vendor_scope_in_filters_and_sort_requires_evidence(overrides):
    raw = _vendor_result(defaults=None, rows=[{"Spend": 9}])
    page = render_page(result(raw), {**VENDOR_QUERY, "metrics": ["Spend"], **overrides})
    assert page.delivery_card is None and "未确认 Vendor ASIN 口径" in str(page.card)


def test_explicit_seller_scope_does_not_accept_vendor_default_metadata():
    query = {**VENDOR_QUERY, "metrics": ["Spend"], "filters": {"storeType_": "SELLER"}}
    with pytest.raises(SparkXDirectError, match="Seller 不一致"):
        render_page(result(_vendor_result()), query)
    raw = _vendor_result(defaults=None, rows=[{"Spend": 0, "storeType": "seller"}])
    assert render_page(result(raw), query).eligible


@pytest.mark.parametrize("value", [1, -1.25, "1", False])
def test_non_placeholder_ordered_cells_contradict_confirmed_sourcing(value):
    query = {**VENDOR_QUERY, "filters": {"distributorView_": "SOURCING"}}
    raw = _vendor_result(defaults={"sellingProgram": "RETAIL"},
                         rows=[{"OrderedRevenue": value, "ShippedRevenue": 8}])
    with pytest.raises(SparkXDirectError, match="SOURCING 行返回与口径冲突"):
        render_page(result(raw), query)


def test_ordered_caveat_only_changes_assurance_for_sourcing_rows_in_mixed_projection():
    query = {**VENDOR_QUERY, "select": ["distributorView_"]}
    rows = [{"distributorView": "MANUFACTURING", "OrderedRevenue": 6},
            {"distributorView": "SOURCING", "OrderedRevenue": None}]
    page = render_page(result(_vendor_result(defaults={"sellingProgram": "RETAIL"}, rows=rows)), query)
    assert page.eligible
    assert page.card["body"]["elements"][1]["rows"] == [
        dict(c0="MANUFACTURING", c1="6"), dict(c0="SOURCING", c1="null")]
    assert "不代表真实订单为零" in str(page.card)
    rows[1]["OrderedRevenue"] = 6
    with pytest.raises(SparkXDirectError, match="SOURCING 行返回与口径冲突"):
        render_page(result(_vendor_result(defaults={"sellingProgram": "RETAIL"}, rows=rows)), query)
