"""Read-only Lingxing OpenAPI client.

Signature rules follow the current official integration guide. Scalar POST JSON
fields are included in the signature even though they are sent in the request body.
Only campaign_ids arrays on spAdvertiseHourData and spTargetHourData have a
verified canonicalizer; all other nested JSON values continue to fail closed.
"""
import base64
import hashlib
import json
import time
from collections.abc import Iterator
from urllib.parse import urljoin

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


class LingxingError(RuntimeError):
    pass


class LingxingTransientError(LingxingError):
    """A network or provider rate-limit condition safe to retry."""


_SP_HOUR_ARRAY_PATHS = frozenset({
    "/pb/openapi/newad/spAdvertiseHourData",
    "/pb/openapi/newad/spTargetHourData",
})


def _endpoint_signature_params(path: str, params: dict) -> dict:
    """Copy signature parameters, canonicalizing only verified SP campaign arrays."""
    values = dict(params)
    if path not in _SP_HOUR_ARRAY_PATHS:
        return values

    campaign_ids = values.get("campaign_ids", [])
    if not isinstance(campaign_ids, list) or any(
        type(value) is not int or value <= 0 for value in campaign_ids
    ):
        raise LingxingError("campaign_ids must be an array of positive integers")
    campaign_id = values.get("campaign_id")
    if "campaign_id" in values and (type(campaign_id) is not int or campaign_id <= 0):
        raise LingxingError("campaign_id must be a positive integer")
    combined_ids = set(campaign_ids)
    if campaign_id is not None:
        combined_ids.add(campaign_id)
    if not combined_ids:
        raise LingxingError("SP hour report requires campaign_id or nonempty campaign_ids")
    if len(combined_ids) > 50 or (
        path.endswith("/spAdvertiseHourData") and len(campaign_ids) > 50
    ):
        raise LingxingError("SP hour report accepts at most 50 campaign IDs")

    if "campaign_ids" in values:
        # Official Python SDK sign.py uses compact JSON for list values; the
        # official Java OrderListDemo restores the original List in the body.
        # https://apidoc.lingxing.com/new/guides/new-instructions/
        # https://apidoc.lingxing.com/file/openapi-python3-sdk-20230419.zip
        # https://apidoc.lingxing.com/new/api-docs/newAd/report/spTargetHourData/
        values["campaign_ids"] = json.dumps(campaign_ids, separators=(",", ":"))
    return values


def _signature_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise LingxingError(
        "Nested/list request values need an endpoint-specific Lingxing signature "
        "canonicalizer; refusing to guess their signing representation."
    )


def signature_source(params: dict) -> str:
    """Build sorted key=value signature source; empty strings are excluded, null is included."""
    pairs = []
    for key in sorted(params, key=lambda item: str(item).encode("ascii")):
        value = params[key]
        if value == "":
            continue
        pairs.append(f"{key}={_signature_scalar(value)}")
    return "&".join(pairs)


def build_signature(params: dict, *, access_token: str, app_key: str, app_id: str) -> str:
    values = {
        **params,
        "access_token": access_token,
        "app_key": app_key,
        "timestamp": params.get("timestamp", int(time.time())),
    }
    raw = signature_source(values)
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest().upper()
    padder = PKCS7(128).padder()
    padded = padder.update(digest.encode("ascii")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(app_id.encode("utf-8")), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


class LingxingClient:
    """Read-only Lingxing API client; it never mutates ads or seller data."""

    def __init__(self, *, base_url: str, app_id: str, app_key: str = "",
                 access_token: str = "", timeout: int = 30):
        self.base_url = base_url.rstrip("/") + "/"
        self.app_id = app_id
        self.app_key = app_key or app_id
        self.access_token = access_token
        self.timeout = timeout

    @classmethod
    def from_settings(cls):
        from jx_alert.config import get_settings
        from jx_alert.lingxing.token import LingxingTokenProvider
        settings = get_settings()
        token = LingxingTokenProvider(settings).get_access_token()
        return cls(
            base_url=settings.lingxing_base_url,
            app_id=settings.lingxing_app_id,
            app_key=settings.lingxing_app_key,
            access_token=token,
            timeout=settings.lingxing_timeout,
        )

    def _request(self, method: str, path: str, params: dict | None = None,
                 json_body: dict | None = None, *, advertising: bool = False):
        if not self.access_token:
            raise LingxingError("LINGXING_ACCESS_TOKEN is not configured")
        if not self.app_id or not self.app_key:
            raise LingxingError("LINGXING_APP_ID / LINGXING_APP_KEY is not configured")

        query_business = dict(params or {})
        body_business = dict(json_body or {})
        business_sign_params = _endpoint_signature_params(
            path, {**query_business, **body_business}
        )
        for attempt in range(5):
            timestamp = int(time.time())
            sign_params = {**business_sign_params, "timestamp": timestamp}
            signature = build_signature(
                sign_params, access_token=self.access_token,
                app_key=self.app_key, app_id=self.app_id,
            )
            query = {
                "app_key": self.app_key,
                "timestamp": timestamp,
                **query_business,
                "sign": signature,
            }
            headers = {"Authorization": f"Bearer {self.access_token}"}
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            if advertising:
                headers["X-API-VERSION"] = "2"

            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.request(
                        method.upper(),
                        urljoin(self.base_url, path.lstrip("/")),
                        params=query,
                        json=json_body,
                        headers=headers,
                    )
                    response.raise_for_status()
                    data = response.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                error = LingxingTransientError(
                    f"Lingxing transient network failure: {type(exc).__name__}"
                )
                if attempt == 4:
                    raise error from exc
                time.sleep(min(2 ** attempt, 16))
                continue
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429 or status >= 500:
                    error = LingxingTransientError(
                        f"Lingxing transient HTTP status: {status}"
                    )
                    if attempt == 4:
                        raise error from exc
                    time.sleep(min(2 ** attempt, 16))
                    continue
                raise LingxingError(f"Lingxing HTTP request failed: status={status}") from exc
            except httpx.HTTPError as exc:
                raise LingxingError(
                    f"Lingxing HTTP request failed: {type(exc).__name__}"
                ) from exc
            except ValueError as exc:
                raise LingxingError("Lingxing API returned invalid JSON") from exc

            if not isinstance(data, dict):
                raise LingxingError("Lingxing API response must be a JSON object")
            if "code" not in data:
                raise LingxingError("Lingxing API response is missing its required code field")
            code = data["code"]
            if code not in (0, "0", 200, "200"):
                message = str(data.get("msg") or data.get("message") or "unknown")
                lowered = message.lower()
                rate_limited = (
                    str(code) in {"429", "3001008"}
                    or any(token in lowered for token in (
                        "rate limit", "too many requests", "频率限制",
                        "请求过于频繁", "限流", "令牌桶",
                    ))
                )
                if rate_limited:
                    error = LingxingTransientError(
                        f"Lingxing rate limit response: code={code}"
                    )
                    if attempt == 4:
                        raise error
                    time.sleep(min(2 ** attempt, 16))
                    continue
                raise LingxingError(
                    f"Lingxing API error: code={code}, message={message}"
                )
            return data

        raise LingxingTransientError("Lingxing request exhausted retry attempts")

    def get_stores(self):
        return self._request("GET", "/erp/sc/data/seller/lists")

    def seller_ad_accounts(self, *, length: int = 100, max_pages: int = 1000) -> list[dict]:
        """Return the full seller advertising account inventory from the official endpoint."""
        accounts = []
        expected_total = None
        for _ in range(max_pages):
            response = self._request("POST", "/basicOpen/baseData/account/list", json_body={
                "type": "seller", "offset": len(accounts), "length": length,
            })
            rows, total = response.get("data"), response.get("total")
            if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                    or type(total) is not int or total < 0 or (expected_total is not None and total != expected_total)):
                raise LingxingError("Invalid or changing seller ad account inventory")
            expected_total = total
            accounts.extend(rows)
            if len(accounts) == total:
                return accounts
            if not rows or len(accounts) > total:
                break
        raise LingxingError("Incomplete seller ad account inventory")

    def sales_daily(self, payload: dict):
        return self._request(
            "POST", "/erp/sc/data/sales_report/asinDailyLists", json_body=payload
        )

    def store_performance_list(self, payload: dict):
        return self._request(
            "POST", "/basicOpen/customerService/storeTarget/list", json_body=payload
        )

    def store_performance_detail(self, payload: dict):
        return self._request(
            "POST", "/basicOpen/customerService/storeTarget/detail", json_body=payload
        )

    def performance_notice_list(self, payload: dict):
        return self._request(
            "POST", "/basicOpen/customerService/performanceNotice/list", json_body=payload
        )

    def advertising_report(self, report_type: str, payload: dict):
        paths = {
            "sp": "/pb/openapi/newad/spCampaignReports",
            "sb": "/pb/openapi/newad/hsaCampaignReports",
            "sd": "/pb/openapi/newad/sdCampaignReports",
        }
        if report_type not in paths:
            raise ValueError("report_type must be sp, sb or sd")
        return self._request("POST", paths[report_type], json_body=payload, advertising=True)

    def iter_advertising_report(self, report_type: str, payload: dict, *,
                                length: int = 100, max_pages: int = 1000
                                ) -> Iterator[dict]:
        """Yield all campaign-report rows; cursor for SP/SD, offset for SB.

        SP/SD OpenAPI v2 return next_token. SB campaign report documentation
        specifies offset/length pagination. Incomplete pagination raises instead
        of silently presenting a partial report as complete.
        """
        report_type = report_type.lower()
        if report_type not in {"sp", "sb", "sd"}:
            raise ValueError("report_type must be sp, sb or sd")
        if not payload.get("sid") and not payload.get("profile_id"):
            raise ValueError("campaign report requires sid or profile_id")
        if not payload.get("report_date"):
            raise ValueError("campaign report requires report_date in YYYY-MM-DD format")
        request = {**payload, "offset": 0, "length": length}
        seen_tokens: set[str] = set()
        offset = 0
        expected_total: int | None = None

        for _ in range(max_pages):
            result = self.advertising_report(report_type, request)
            rows = result.get("data") if isinstance(result, dict) else None
            if not isinstance(rows, list):
                raise LingxingError("Campaign report response data must be an array")
            try:
                total = int(result["total"])
            except (KeyError, TypeError, ValueError) as exc:
                raise LingxingError("Campaign report response total is missing or invalid") from exc
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise LingxingError(
                    "Campaign report total changed during pagination: "
                    f"expected {expected_total}, received {total}"
                )

            yield from rows
            offset += len(rows)
            if offset > total:
                raise LingxingError("Campaign report returned more rows than its reported total")
            if offset == total:
                return
            if not rows:
                raise LingxingError("Campaign report pagination stopped before the reported total")

            if report_type == "sb":
                request["offset"] = offset
                continue

            next_token = result.get("next_token")
            if not isinstance(next_token, str) or not next_token:
                raise LingxingError(
                    f"{report_type.upper()} report pagination incomplete: "
                    f"{offset} of {total} rows returned without next_token"
                )
            if next_token in seen_tokens:
                raise LingxingError("Campaign report returned a repeated next_token")
            seen_tokens.add(next_token)
            request.pop("offset", None)
            request["next_token"] = next_token

        raise LingxingError(f"Campaign report exceeded max_pages={max_pages}")



    def advertising_campaigns(self, campaign_type: str, payload: dict):
        paths = {
            "sp": "/pb/openapi/newad/spCampaigns",
            "sb": "/pb/openapi/newad/hsaCampaigns",
            "sd": "/pb/openapi/newad/sdCampaigns",
        }
        campaign_type = campaign_type.lower()
        if campaign_type not in paths:
            raise ValueError("campaign_type must be sp, sb or sd")
        return self._request(
            "POST", paths[campaign_type], json_body=payload, advertising=True
        )

    def iter_advertising_campaigns(self, campaign_type: str, payload: dict, *,
                                   length: int = 100, max_pages: int = 1000
                                   ) -> Iterator[dict]:
        """Yield SP/SB/SD campaign master records using the v2 next_token cursor."""
        campaign_type = campaign_type.lower()
        if campaign_type not in {"sp", "sb", "sd"}:
            raise ValueError("campaign_type must be sp, sb or sd")
        if not payload.get("sid") and not payload.get("profile_id"):
            raise ValueError("campaign list requires sid or profile_id")
        request = {**payload, "offset": 0, "length": length}
        seen_tokens: set[str] = set()
        offset = 0

        for _ in range(max_pages):
            result = self.advertising_campaigns(campaign_type, request)
            rows = result.get("data") if isinstance(result, dict) else None
            if not isinstance(rows, list):
                raise LingxingError("Campaign list response data must be an array")
            try:
                total = int(result["total"])
            except (KeyError, TypeError, ValueError) as exc:
                raise LingxingError("Campaign list response total is missing or invalid") from exc
            yield from rows
            offset += len(rows)
            if offset > total:
                raise LingxingError("Campaign list returned more rows than its reported total")
            if offset == total:
                return
            if not rows:
                raise LingxingError(
                    f"{campaign_type.upper()} campaign list pagination incomplete"
                )
            next_token = result.get("next_token")
            if not isinstance(next_token, str) or not next_token:
                raise LingxingError(
                    f"{campaign_type.upper()} campaign list missing next_token"
                )
            if next_token in seen_tokens:
                raise LingxingError("Campaign list returned a repeated next_token")
            seen_tokens.add(next_token)
            request.pop("offset", None)
            request["next_token"] = next_token

        raise LingxingError(f"Campaign list exceeded max_pages={max_pages}")

    def advertising_product_ads(self, campaign_type: str, payload: dict):
        paths = {
            "sp": "/pb/openapi/newad/spProductAds",
            "sd": "/pb/openapi/newad/sdProductAds",
        }
        campaign_type = campaign_type.lower()
        if campaign_type not in paths:
            raise ValueError("campaign_type must be sp or sd")
        return self._request(
            "POST", paths[campaign_type], json_body=payload, advertising=True
        )

    def iter_advertising_product_ads(self, campaign_type: str, payload: dict, *,
                                     length: int = 100, max_pages: int = 1000
                                     ) -> Iterator[dict]:
        """Yield all SP/SD product-ad master rows using the v2 next_token cursor."""
        campaign_type = campaign_type.lower()
        if campaign_type not in {"sp", "sd"}:
            raise ValueError("campaign_type must be sp or sd")
        if not payload.get("sid") and not payload.get("profile_id"):
            raise ValueError("product-ad list requires sid or profile_id")
        request = {**payload, "offset": 0, "length": length}
        seen_tokens: set[str] = set()
        offset = 0

        for _ in range(max_pages):
            result = self.advertising_product_ads(campaign_type, request)
            rows = result.get("data") if isinstance(result, dict) else None
            if not isinstance(rows, list):
                raise LingxingError("Product-ad list response data must be an array")
            try:
                total = int(result["total"])
            except (KeyError, TypeError, ValueError) as exc:
                raise LingxingError("Product-ad list response total is missing or invalid") from exc
            yield from rows
            offset += len(rows)
            if offset > total:
                raise LingxingError("Product-ad list returned more rows than its reported total")
            if offset == total:
                return
            if not rows:
                raise LingxingError(
                    f"{campaign_type.upper()} product-ad list pagination incomplete"
                )
            next_token = result.get("next_token")
            if not isinstance(next_token, str) or not next_token:
                raise LingxingError(
                    f"{campaign_type.upper()} product-ad list missing next_token"
                )
            if next_token in seen_tokens:
                raise LingxingError("Product-ad list returned a repeated next_token")
            seen_tokens.add(next_token)
            request.pop("offset", None)
            request["next_token"] = next_token

        raise LingxingError(f"Product-ad list exceeded max_pages={max_pages}")

    def advertising_product_report(self, report_type: str, payload: dict):
        paths = {
            "sp": "/pb/openapi/newad/spProductAdReports",
            "sd": "/pb/openapi/newad/sdProductAdReports",
        }
        report_type = report_type.lower()
        if report_type not in paths:
            raise ValueError("product report_type must be sp or sd")
        return self._request(
            "POST", paths[report_type], json_body=payload, advertising=True
        )

    def iter_advertising_product_report(self, report_type: str, payload: dict, *,
                                        length: int = 100, max_pages: int = 1000
                                        ) -> Iterator[dict]:
        """Yield SP (cursor) and SD (offset) advertised-product report rows."""
        report_type = report_type.lower()
        if report_type not in {"sp", "sd"}:
            raise ValueError("product report_type must be sp or sd")
        if not payload.get("sid") and not payload.get("profile_id"):
            raise ValueError("product report requires sid or profile_id")
        if not payload.get("report_date"):
            raise ValueError("product report requires report_date")
        request = {**payload, "offset": 0, "length": length}
        seen_tokens: set[str] = set()
        offset = 0

        for _ in range(max_pages):
            result = self.advertising_product_report(report_type, request)
            rows = result.get("data") if isinstance(result, dict) else None
            if not isinstance(rows, list):
                raise LingxingError("Product ad report response data must be an array")
            try:
                total = int(result["total"])
            except (KeyError, TypeError, ValueError) as exc:
                raise LingxingError("Product ad report response total is missing or invalid") from exc
            yield from rows
            offset += len(rows)
            if offset > total:
                raise LingxingError("Product ad report returned more rows than its reported total")
            if offset == total:
                return
            if not rows:
                raise LingxingError(
                    f"{report_type.upper()} product report pagination incomplete"
                )

            if report_type == "sd":
                request["offset"] = offset
                continue

            next_token = result.get("next_token")
            if not isinstance(next_token, str) or not next_token:
                raise LingxingError("SP product report missing next_token")
            if next_token in seen_tokens:
                raise LingxingError("SP product report returned a repeated next_token")
            seen_tokens.add(next_token)
            request.pop("offset", None)
            request["next_token"] = next_token

        raise LingxingError(f"Product ad report exceeded max_pages={max_pages}")

    def performance(self, path: str, payload: dict):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("performance API path must be an absolute API path")
        return self._request("POST", path, json_body=payload)
