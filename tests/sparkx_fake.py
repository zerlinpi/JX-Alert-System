"""An in-process stand-in for the SparkX MCP server (documented response envelope)."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

PROFILES = {
    "101": {"profileId": 101, "profileName": "香港川页", "countryCode": "US", "storeType": "seller"},
    "102": {"profileId": 102, "profileName": "Campx-主账号", "countryCode": "US", "storeType": "seller"},
    "103": {"profileId": 103, "profileName": "FITFIZZ-主账号", "countryCode": "US", "storeType": "seller"},
    "104": {"profileId": 104, "profileName": "陈子俊", "countryCode": "US", "storeType": "seller"},
    "105": {"profileId": 105, "profileName": "昌泉", "countryCode": "US", "storeType": "seller"},
    "201": {"profileId": 201, "profileName": "川页-CA", "countryCode": "CA", "storeType": "seller"},
}


def metrics(impressions, clicks, spend, sales, orders, units=None):
    return {"Impressions": impressions, "Clicks": clicks, "Spend": spend, "Sales": sales, "Conversions": orders,
            "Units": orders if units is None else units}


def result(view):
    return SimpleNamespace(isError=False, structuredContent=view, content=[])


class FakeSparkX:
    """rows[(factEntity, dateStart, dateEnd)] -> list of row dicts (plain keys)."""

    def __init__(self, rows=None, profiles=None, enabled=None, currency="USD", hint=None, errors=None):
        self.rows = rows or {}
        self.profiles = PROFILES if profiles is None else profiles
        self.enabled = enabled or {}
        self.currency = currency
        self.hint = hint
        self.errors = errors or {}
        self.calls = []

    async def call_tool(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        if tool == "get_user_authorized_context":
            return result({"profiles": list(self.profiles.values()), "userId": 1, "tenantId": 2})
        if tool == "get_entity_metadata":
            pid = str(arguments["profileIds"][0])
            return result({"isError": False, "toolName": tool, "data": {"rows": [], "rowCount": 0},
                           "meta": {"total": self.enabled.get(pid, 0), "hasNextPage": False}})
        key = (arguments["factEntity"], arguments["dateStart"], arguments["dateEnd"])
        if key in self.errors:
            return result({"isError": True, "toolName": tool, "errorType": self.errors[key], "message": "secret detail"})
        rows = [row for row in self.rows.get(key, [])
                if int(row["profileId"]) in arguments["profileIds"]]
        page, size = arguments.get("page", 1), arguments.get("pageSize", 100)
        chunk = rows[(page - 1) * size:page * size]
        meta = {"page": page, "pageSize": size, "hasNextPage": page * size < len(rows),
                "effectiveProfileIds": arguments["profileIds"],
                "currency": self.currency if len(arguments["profileIds"]) > 1 else self.currency,
                "hint": self.hint or ("Limit conclusions to this query's scope. Do not present partial or paginated "
                                      "results as complete.")}
        return result({"isError": False, "toolName": tool, "data": {"rows": chunk, "rowCount": len(chunk)},
                       "meta": meta, "requestId": "req"})


def install(monkeypatch, fake, *modules):
    @asynccontextmanager
    async def session(token, timeout, **_):
        assert token
        yield fake

    for module in modules:
        monkeypatch.setattr(module, "open_session", session)
    return fake
