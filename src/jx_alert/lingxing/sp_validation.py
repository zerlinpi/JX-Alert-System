"""Append-only SP evidence collection and observed daily/hour reconciliation.

Only the caller's supplied session is used; this module never initializes a
database, changes ads, replaces DailyMetric rows, commits, or loads settings.
Calling it again for the same dates appends a new run, preserving prior evidence.

Contracts: https://apidoc.lingxing.com/new/api-docs/newAd/report/
{spCampaignReports,spCampaignHourData,spAdGroupHourData,spAdvertiseHourData,
spTargetHourData}/. The four hour dimensions are compared separately, never added.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import re
import time
from uuid import uuid4


DAILY_PATH = "/pb/openapi/newad/spCampaignReports"
MASTER_PATH = "/pb/openapi/newad/spCampaigns"
HOUR_ENDPOINTS = {
    "campaign": ("/pb/openapi/newad/spCampaignHourData", None),
    "ad_group": ("/pb/openapi/newad/spAdGroupHourData", None),
    "ad": ("/pb/openapi/newad/spAdvertiseHourData", "ad"),
    "target": ("/pb/openapi/newad/spTargetHourData", "target"),
}
METRIC_FIELDS = ("cost", "clicks", "impressions", "orders", "sales")
_COUNTS = {"clicks", "impressions", "orders"}
_FAILURES = {"REQUEST_FAILED", "INVALID_RESPONSE"}
_SECRET_KEYS = {
    "authorization", "proxyauthorization", "cookie", "setcookie", "appid",
    "appkey", "apikey", "sign", "signature", "credentials", "databaseurl",
    "dsn", "webhook", "webhookurl", "bearer", "key", "accesskey",
}


class SPValidationError(ValueError):
    """A fixed, non-sensitive contract error code suitable for evidence."""


class _RequestFailed(RuntimeError):
    pass


def _identifier(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value) and int(value) > 0:
        return str(int(value))
    raise SPValidationError("INVALID_IDENTIFIER")


def _total(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise SPValidationError("INVALID_TOTAL")


def _numeric(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SPValidationError("MISSING_OR_INVALID_METRIC")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise SPValidationError("MISSING_OR_INVALID_METRIC") from None
    # Bound the JSON representation as well as rejecting NaN and infinity.
    if (not number.is_finite() or number < 0 or number > Decimal("1e18")
            or number.as_tuple().exponent < -12):
        raise SPValidationError("MISSING_OR_INVALID_METRIC")
    if field in _COUNTS and number != number.to_integral_value():
        raise SPValidationError("INVALID_COUNT_METRIC")
    return number


def _redact(value, known):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if (normalized in _SECRET_KEYS
                    or normalized.endswith(("token", "secret", "password", "apikey"))):
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact(child, known)
        return result
    if isinstance(value, list):
        return [_redact(child, known) for child in value]
    if isinstance(value, str):
        for secret in known:
            value = value.replace(secret, "[REDACTED]")
    if isinstance(value, float) and not math.isfinite(value):
        return "[NON_FINITE_NUMBER]"
    return value


class _Evidence:
    def __init__(self, client, db, sid, run_id, request_interval):
        self.client, self.db, self.sid, self.run_id = client, db, sid, run_id
        self.request_interval, self.next_request_at = request_interval, 0
        self.known = sorted({
            value for field in ("access_token", "app_id", "app_key")
            if isinstance(value := getattr(client, field, None), str) and value
        }, key=len, reverse=True)

    def save(self, source, payload):
        # Lazy import avoids loading application/database configuration merely
        # by importing the validation helpers.
        from jx_alert.storage.models import RawSnapshot
        snapshot = RawSnapshot(
            source=source, store_id=str(self.sid),
            captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
            payload=_redact({"run_id": self.run_id, **deepcopy(payload)}, self.known),
        )
        self.db.add(snapshot)
        self.db.flush()
        return snapshot.id

    def request(self, path, request):
        delay = self.next_request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_request_at = time.monotonic() + self.request_interval
        try:
            response = self.client._request(
                "POST", path, json_body=deepcopy(request), advertising=True,
            )
        except Exception as exc:
            # Upstream exceptions may contain signed URLs or credentials. Keep
            # only the class, never the exception message or traceback.
            error_type = type(exc).__name__
            self.save("lingxing_sp_validation_page", {
                "endpoint": path, "request": request, "status": "REQUEST_FAILED",
                "error_type": error_type,
            })
            raise _RequestFailed(error_type) from None
        self.save("lingxing_sp_validation_page", {
            "endpoint": path, "request": request, "status": "RECEIVED",
            "response": response,
        })
        return response


def _page(response):
    if (not isinstance(response, dict) or isinstance(response.get("code"), bool)
            or response.get("code") not in (0, "0")):
        raise SPValidationError("UNSUCCESSFUL_RESPONSE")
    rows = response.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SPValidationError("INVALID_DATA_ARRAY")
    total = _total(response.get("total"))
    return rows, total


def _pages(evidence, path, parameters, *, length, max_pages, paginated):
    request = dict(parameters)
    if paginated:
        request.update(offset=0, length=length)
    all_rows, seen_tokens, expected_total = [], set(), None
    for _ in range(max_pages if paginated else 1):
        response = evidence.request(path, request)
        rows, total = _page(response)
        if not paginated and response.get("next_token") not in (None, ""):
            raise SPValidationError("UNEXPECTED_HOUR_PAGINATION")
        if expected_total is not None and total != expected_total:
            raise SPValidationError("TOTAL_CHANGED_DURING_PAGINATION")
        expected_total = total
        all_rows.extend(rows)
        if len(all_rows) > total:
            raise SPValidationError("ROW_COUNT_EXCEEDS_TOTAL")
        if len(all_rows) == total:
            return all_rows
        if not paginated:
            raise SPValidationError("INCOMPLETE_HOUR_RESPONSE")
        token = response.get("next_token")
        if not rows or not isinstance(token, str) or not token:
            raise SPValidationError("INCOMPLETE_PAGINATION")
        if token in seen_tokens:
            raise SPValidationError("REPEATED_NEXT_TOKEN")
        seen_tokens.add(token)
        request.pop("offset", None)
        request["next_token"] = token
    raise SPValidationError("MAX_PAGES_EXCEEDED")


def _attempt(evidence, path, parameters, *, length, max_pages, paginated, validate):
    try:
        rows = _pages(
            evidence, path, parameters, length=length,
            max_pages=max_pages, paginated=paginated,
        )
        result = validate(rows)
        return result
    except (_RequestFailed, SPValidationError) as exc:
        status = "REQUEST_FAILED" if isinstance(exc, _RequestFailed) else "INVALID_RESPONSE"
        failure = {
            "status": status, "row_count": None, "matched_row_count": None,
            "metrics": None,
            "error_type": str(exc) if isinstance(exc, _RequestFailed) else type(exc).__name__,
            "error_code": str(exc) if isinstance(exc, SPValidationError) else "REQUEST_ERROR",
        }
        evidence.save("lingxing_sp_validation_failure", {
            "endpoint": path, "request": parameters, **failure,
        })
        return failure


def _master_rows(rows):
    campaigns = {}
    for row in rows:
        identifier = _identifier(row.get("campaign_id"))
        if identifier in campaigns:
            raise SPValidationError("DUPLICATE_CAMPAIGN")
        profile = _identifier(row.get("profile_id"))
        campaigns[identifier] = {
            "campaign_id": identifier, "profile_id": profile,
            "campaign_name": row.get("name") if isinstance(row.get("name"), str) else None,
            "state": row.get("state") if isinstance(row.get("state"), str) else None,
            "start_date": row.get("start_date"), "end_date": row.get("end_date"),
        }
    return {"status": "COMPLETE", "row_count": len(rows), "campaigns": campaigns}


def _report_rows(rows, report_date, campaign_id, campaigns, *, hourly, dimension):
    matched, records, profiles, seen = [], [], set(), set()
    for row in rows:
        identifier = _identifier(row.get("campaign_id"))
        profile = _identifier(row.get("profile_id"))
        expected_profile = campaigns.get(identifier, {}).get("profile_id")
        if expected_profile is not None and profile != expected_profile:
            raise SPValidationError("UNEXPECTED_PROFILE")
        if row.get("report_date") != report_date:
            raise SPValidationError("UNEXPECTED_REPORT_DATE")
        values = {field: _numeric(row.get(field), field) for field in METRIC_FIELDS}
        if hourly:
            if identifier != campaign_id:
                raise SPValidationError("UNEXPECTED_CAMPAIGN")
            hour = row.get("hour")
            if type(hour) is not int or not 0 <= hour <= 23:
                raise SPValidationError("INVALID_HOUR")
            # Keys are endpoint-specific: group_id, ad_id and targeting_id.
            # Missing keys fail closed instead of silently double-counting.
            identity = {
                "campaign": (), "ad_group": ("group_id",),
                "ad": ("ad_id",), "target": ("targeting_id",),
            }[dimension]
            key = (profile, hour, *(_identifier(row.get(key)) for key in identity))
            if key in seen:
                raise SPValidationError("DUPLICATE_HOUR_ENTITY")
            seen.add(key)
        else:
            if identifier in seen:
                raise SPValidationError("DUPLICATE_DAILY_CAMPAIGN")
            seen.add(identifier)
        if identifier == campaign_id:
            profiles.add(profile)
            matched.append(values)
        records.append({
            "campaign_id": identifier, "profile_id": profile,
            "campaign_name": campaigns.get(identifier, {}).get("campaign_name"),
            "report_date": report_date,
            **{field: format(value, "f") for field, value in values.items()},
        })
    if len(profiles) > 1:
        raise SPValidationError("MIXED_PROFILE_SCOPE")
    totals = {
        field: sum((values[field] for values in matched), Decimal(0))
        for field in METRIC_FIELDS
    } if matched else None
    status = (
        "NO_RECORDS" if not rows else "UNMATCHED" if not matched
        else "ZERO_SPEND" if totals["cost"] == 0 else "MATCHED"
    )
    result = {
        "status": status, "row_count": len(rows), "matched_row_count": len(matched),
        "metrics": {field: format(value, "f") for field, value in totals.items()}
        if totals is not None else None,
        "profile_id": next(iter(profiles), None),
    }
    if not hourly:
        result["records"] = records
    return result


def _compare(daily, hours):
    if daily.get("metrics") is None or hours.get("metrics") is None:
        return {"status": "UNAVAILABLE", "delta_hour_minus_daily": None}
    if daily["profile_id"] != hours["profile_id"]:
        return {"status": "SCOPE_MISMATCH", "delta_hour_minus_daily": None}
    differences = {
        field: Decimal(hours["metrics"][field]) - Decimal(daily["metrics"][field])
        for field in METRIC_FIELDS
    }
    return {
        "status": "EQUAL" if all(value == 0 for value in differences.values()) else "DIFFERENT",
        "delta_hour_minus_daily": {field: format(value, "f") for field, value in differences.items()},
    }


def collect_sp_validation(client, db, *, sid: int, campaign_id: int,
                          report_dates, length: int = 100, max_pages: int = 1000,
                          request_interval: float = 1.1):
    """Collect a bounded date window using scalar campaign_id for hour requests.

    A COMPLETE result means requests and contract checks succeeded; empty or
    unmatched days still have no comparable metrics. Differences are observed
    endpoint totals, not a claim of identical attribution or historical finality.
    The caller must commit evidence, including partial/failed runs.
    """
    if type(sid) is not int or type(campaign_id) is not int:
        raise ValueError("sid and campaign_id must be positive integers")
    sid_value, campaign = _identifier(sid), _identifier(campaign_id)
    dates = list(report_dates)
    if not dates or len(dates) > 60:
        raise ValueError("report_dates must contain between 1 and 60 dates")
    for value in dates:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError("report_dates must contain ISO YYYY-MM-DD dates")
    dates = sorted(set(dates))
    if (type(length) is not int or not 1 <= length <= 1000
            or type(max_pages) is not int or not 1 <= max_pages <= 10000):
        raise ValueError("Invalid pagination limits")
    if (isinstance(request_interval, bool) or not isinstance(request_interval, (int, float))
            or not math.isfinite(request_interval) or not 0 <= request_interval <= 60):
        raise ValueError("request_interval must be between 0 and 60 seconds")
    run_id = str(uuid4())
    evidence = _Evidence(client, db, sid_value, run_id, request_interval)
    master = _attempt(
        evidence, MASTER_PATH, {"sid": sid}, length=length, max_pages=max_pages,
        paginated=True, validate=_master_rows,
    )
    campaigns = master.get("campaigns", {})
    days = []
    failed = master["status"] in _FAILURES
    for report_date in dates:
        daily = _attempt(
            evidence, DAILY_PATH,
            {"sid": sid, "report_date": report_date, "show_detail": 1},
            length=length, max_pages=max_pages, paginated=True,
            validate=lambda rows: _report_rows(
                rows, report_date, campaign, campaigns, hourly=False, dimension="daily",
            ),
        )
        hours = {}
        for dimension, (path, aggregation) in HOUR_ENDPOINTS.items():
            parameters = {"campaign_id": campaign_id, "report_date": report_date}
            if aggregation:
                parameters["agg_dimension"] = aggregation
            hours[dimension] = _attempt(
                evidence, path, parameters, length=length, max_pages=max_pages, paginated=False,
                validate=lambda rows, dimension=dimension: _report_rows(
                    rows, report_date, campaign, campaigns, hourly=True, dimension=dimension,
                ),
            )
        day = {
            "report_date": report_date, "daily": daily, "hours": hours,
            "reconciliation": {dimension: _compare(daily, value)
                               for dimension, value in hours.items()},
        }
        failed |= any(result["status"] in _FAILURES for result in [daily, *hours.values()])
        evidence.save("lingxing_sp_validation_result", {"sid": sid_value,
                      "campaign_id": campaign, **day})
        days.append(day)
    result = {
        "run_id": run_id, "status": "PARTIAL" if failed else "COMPLETE",
        "sid": sid_value, "campaign_id": campaign, "master": master, "days": days,
        "comparison_note": "Observed endpoint totals only; attribution and historical finality "
                           "are not inferred. Hour dimensions are never added together.",
    }
    result = _redact(result, evidence.known)
    evidence.save("lingxing_sp_validation_run", result)
    return result
