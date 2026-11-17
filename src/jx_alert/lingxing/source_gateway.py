"""API priority and independent MCP observations; incompatible bases never add.

The gateway does not synthesize paid sales or attributed conversions from MCP
purchase/UI reports. It archives those confirmed observations in mcp_* grains
and exposes them to the console/chat when the primary source is unavailable.
"""
from contextlib import contextmanager
from datetime import date
import math
import re

from sqlalchemy import delete, select

from jx_alert.lingxing.ad_currency import account_currencies
from jx_alert.lingxing.client import LingxingClient, LingxingCollectionPaused, LingxingError
from jx_alert.lingxing.mcp_read_source import OfficialReadSource, business_body
from jx_alert.lingxing.source_credentials import mcp_credentials, SourceCredentialsError
from jx_alert.storage.coverage import mark_coverage
from jx_alert.storage.models import DailyMetric, DataCoverage, RawSnapshot, Store


MCP_SALES = "lingxing_mcp_product_performance"
MCP_ADS = "lingxing_mcp_campaign_report"
_SECRET_KEYS = {"apikey", "key", "secret", "appsecret", "password", "authorization",
                "accesstoken", "refreshtoken", "jwt", "token"}


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()
                if re.sub(r"[^a-z]", "", str(k).lower()) not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


class CollectionSources:
    def __init__(self, api, settings, *, db=None, reader=None, observation_mode=False):
        self.api, self.settings, self.db = api, settings, db
        self.mcp = reader or OfficialReadSource(settings)
        self.observation_mode = observation_mode
        self.pending, self.outcomes, self._mcp_cache = [], [], {}
        self._outcomes_flushed = 0
        self._accounts = None
        self.last_request = None
        self.shadow_dates = None

    @contextmanager
    def _database(self):
        if self.db is not None:
            yield self.db
        else:
            from jx_alert.storage.db import SessionLocal
            with SessionLocal() as db:
                yield db

    def __getattr__(self, name):
        if self.api is not None:
            return getattr(self.api, name)
        raise LingxingError("OpenAPI is unavailable for this reporting grain")

    def _api(self, method, *args, sequence=False, **kwargs):
        if self.api is None:
            raise LingxingError("OpenAPI is unavailable")
        value = getattr(self.api, method)(*args, **kwargs)
        return list(value) if sequence else value

    def _parallel(self, dataset, sid, day, primary, secondary, *, equivalent=False):
        self.last_request = {"dataset": dataset, "store_id": str(sid), "report_date": day}
        api_value, mcp_value, error = None, None, None
        try:
            api_value = primary()
        except LingxingCollectionPaused:
            raise
        except Exception as exc:
            error = exc
        key = (dataset, str(sid), day)
        if (dataset.startswith("campaign_") and self.shadow_dates is not None
                and day not in self.shadow_dates.get(str(sid), set())):
            if error:
                raise error
            return api_value, None
        try:
            if key not in self._mcp_cache:
                self._mcp_cache[key] = secondary()
            mcp_value = self._mcp_cache[key]
            if mcp_value is None:
                raise LingxingError("Official MCP source remains unavailable")
            mcp_status = "COMPLETE"
        except LingxingCollectionPaused:
            raise
        except Exception as exc:
            mcp_status = "FAILED"
            if key not in self._mcp_cache:
                self._mcp_cache[key] = None
                self.pending.append({"dataset": dataset, "sid": str(sid), "day": day,
                    "status": "FAILED", "error_type": type(exc).__name__})
        outcome = {"dataset": dataset, "store_id": str(sid), "report_date": day,
                   "api": "FAILED" if error else "COMPLETE", "mcp": mcp_status,
                   "selected": "MCP_OBSERVATION" if error and mcp_value is not None else "API" if not error else "NONE"}
        if error:
            outcome["api_error_type"] = type(error).__name__
        self.outcomes.append(outcome)
        if error:
            if (self.observation_mode or equivalent) and mcp_value is not None:
                return None, mcp_value
            raise error
        return api_value, mcp_value

    def get_stores(self):
        api_value, api_error = None, None
        try:
            api_value = self._api("get_stores")
        except Exception as exc:
            api_error = exc
        try:
            visible, response = self.mcp.stores()
        except Exception as exc:
            self.pending.append({"dataset": "inventory", "sid": None, "day": None,
                "status": "FAILED", "error_type": type(exc).__name__})
            if api_error is None:
                return api_value
            raise LingxingError("Both store inventory sources are unavailable") from None
        self.pending.append({"dataset": "inventory", "sid": None, "day": None,
            "status": "COMPLETE", "raw": response})
        if api_error is None:
            return api_value
        # Visibility is not an Amazon authorization status. Use only already
        # synchronized identities and preserve their last real status.
        with self._database() as db:
            rows = []
            for item in visible:
                cached = db.get(Store, str(item["id"]))
                if cached is None:
                    continue
                rows.append({"sid": int(cached.sid), "name": cached.name, "status": cached.status,
                    "has_ads_setting": cached.has_ads_setting, "mid": cached.mid,
                    "seller_id": cached.seller_id, "account_name": cached.account_name,
                    "region": cached.region, "country": cached.country, "marketplace_id": cached.marketplace_id})
        if not rows:
            raise LingxingError("MCP inventory has no previously synchronized store identities") from None
        return {"code": 0, "data": rows, "_source": "mcp_visible_cached_identity"}

    def seller_ad_accounts(self):
        if self._accounts is not None:
            return self._accounts
        try:
            self._accounts = self._api("seller_ad_accounts")
        except Exception:
            # Profile currency is never inferred from country or sales currency.
            try:
                profiles, _ = self.mcp.profiles()
            except LingxingCollectionPaused:
                raise
            except Exception:
                raise LingxingError("Advertising profile sources are unavailable") from None
            with self._database() as db:
                prior = db.scalar(select(RawSnapshot).where(RawSnapshot.source == "lingxing_seller_ad_accounts")
                    .order_by(RawSnapshot.id.desc()).limit(1))
                old = (prior.payload or {}).get("rows", []) if prior is not None else []
                codes = account_currencies(old)
            self._accounts = [{"sid": str(r["sid"]), "profile_id": str(r["profile_id"]),
                "type": "seller", "currency_code": codes.get((str(r["sid"]), str(r["profile_id"])))}
                for r in profiles if r.get("type") == "seller" and r.get("real_status") == 1
                and r.get("sid") and r.get("profile_id")]
        return self._accounts

    def sales_daily(self, payload):
        sid, day = str(payload["sid"]), payload["event_date"]
        dimension = "asin" if payload["asin_type"] == 1 else "msku"

        def observe():
            rows, raw = self.mcp.sales(sid, day, dimension)
            self.pending.append({"dataset": "sales_" + dimension, "sid": sid, "day": day,
                                 "rows": rows, "raw": raw, "status": "COMPLETE"})
            return rows

        api, _ = self._parallel("sales_" + dimension, sid, day,
            lambda: self._api("sales_daily", payload), observe)
        return api

    def iter_advertising_report(self, kind, payload, **kwargs):
        sid, day = str(payload["sid"]), payload["report_date"]

        def observe():
            return self.observe_campaign(sid, day, kind)

        api, observed = self._parallel("campaign_" + kind, sid, day,
            lambda: self._api("iter_advertising_report", kind, payload, sequence=True, **kwargs), observe)
        if api is not None:
            return iter(api)
        # Only used by a separate intraday-observation job. Daily callers still
        # receive the API error and cannot silently ingest these as daily facts.
        return iter({"campaign_id": r["entity_id"], "profile_id": r["profile_id"],
            "campaign_name": r["name"], "cost": r["spend"], "clicks": r["clicks"],
            "impressions": r["impressions"], "currency_code": r.get("currency"),
            "report_date": day, "_source": "official_mcp_observation",
            "_attribution_unconfirmed": True} for r in (observed or []))

    def observe_campaign(self, sid, day, kind):
        key = ("campaign_" + kind, str(sid), day)
        if key in self._mcp_cache:
            if self._mcp_cache[key] is None:
                raise LingxingError("Official MCP source remains unavailable")
            return self._mcp_cache[key]
        try:
            rows, raw = self.mcp.campaign_ads(str(sid), day, kind)
            currencies = account_currencies(self.seller_ad_accounts())
            rows = [{**r, "currency": currencies.get((str(sid), r["profile_id"]))} for r in rows]
            self.pending.append({"dataset": "campaign_" + kind, "sid": str(sid), "day": day,
                                 "rows": rows, "raw": raw, "status": "COMPLETE"})
            self._mcp_cache[key] = rows
            return rows
        except LingxingCollectionPaused:
            raise
        except Exception as exc:
            self._mcp_cache[key] = None
            self.pending.append({"dataset": "campaign_" + kind, "sid": str(sid), "day": day,
                                 "status": "FAILED", "error_type": type(exc).__name__})
            raise

    def iter_advertising_product_report(self, kind, payload, **kwargs):
        self.last_request = {"dataset": "product_ad_" + kind, "store_id": str(payload["sid"]),
                             "report_date": payload["report_date"]}
        return iter(self._api("iter_advertising_product_report", kind, payload, sequence=True, **kwargs))

    def _performance(self, method, tool, payload):
        sid = str(payload.get("sid") or payload.get("sids"))
        params = dict(payload)
        if tool == "customer_store_performance_list":
            params.update(order_field="update_date", order_type="desc")
        else:
            params = {"sid": sid, "pull_date": payload["pullDate"]}
        def observe():
            self.mcp.check_store(sid)
            response = self.mcp.call(tool, params)
            body = business_body(response)
            data = body.get("data")
            if tool == "customer_store_performance_list":
                rows = data if isinstance(data, list) else data.get("list") if isinstance(data, dict) else None
                if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("pull_date")
                        or (row.get("sid") is not None and str(row["sid"]) != sid) for row in rows):
                    raise LingxingError("MCP performance list does not match the verified report contract")
                data = {"list": rows}
            elif not isinstance(data, dict) or "standard" not in data:
                raise LingxingError("MCP performance standards are unavailable")
            self.pending.append({"dataset": "performance", "sid": sid, "day": None,
                                 "raw": response, "status": "COMPLETE"})
            return {"data": data, **({"total": body["total"]} if "total" in body else {})}
        api, observed = self._parallel("performance_" + method, sid, None,
            lambda: self._api(method, payload), observe, equivalent=True)
        return api if api is not None else observed

    def store_performance_list(self, payload):
        return self._performance("store_performance_list", "customer_store_performance_list", payload)

    def store_performance_detail(self, payload):
        return self._performance("store_performance_detail", "customer_store_target_detail", payload)

    def flush(self, db, *, raw_only=False):
        """Persist after collector rollback as well as success; never send notices."""
        pending, self.pending = self.pending, []
        for item in pending:
            dataset, sid, day = item["dataset"], item["sid"], item["day"]
            db.add(RawSnapshot(source="lingxing_mcp_" + dataset, store_id=sid,
                payload=_clean(item)))
            if raw_only or not day or dataset not in {"sales_asin", "sales_msku", "campaign_sp", "campaign_sb", "campaign_sd"}:
                continue
            dimension = "mcp_" + dataset
            on = date.fromisoformat(day)
            covered = mark_coverage(db, store_id=sid, metric_date=on, dimension=dimension,
                                    row_count=len(item.get("rows", [])))
            if item["status"] != "COMPLETE":
                covered.status = "FAILED"
                continue
            db.execute(delete(DailyMetric).where(DailyMetric.store_id == sid,
                DailyMetric.metric_date == on, DailyMetric.dimension == dimension))
            for row in item["rows"]:
                for metric in ("sales", "units", "orders") if dataset.startswith("sales_") else ("spend", "clicks", "impressions"):
                    db.add(DailyMetric(store_id=sid, metric_date=on, dimension=dimension,
                        entity_id=row["entity_id"], metric_name=metric, value=row[metric],
                        currency=row.get("currency") if metric in {"spend", "sales"} else None,
                        source=MCP_SALES if dataset.startswith("sales_") else MCP_ADS))
        if len(self.outcomes) > self._outcomes_flushed:
            db.add(RawSnapshot(source="lingxing_source_selection",
                payload={"outcomes": self.outcomes[self._outcomes_flushed:][-400:]}))
            self._outcomes_flushed = len(self.outcomes)
        db.commit()

    def source_summary(self):
        return {"mode": "API_AND_MCP", "outcomes": list(self.outcomes[-400:]),
                "api_failed": any(x["api"] == "FAILED" for x in self.outcomes)}


def create_collection_client(settings=None, *, api_client=None, db=None, observation_mode=False):
    if settings is None:
        from jx_alert.config import get_settings
        settings = get_settings()
    error = None
    if api_client is None:
        try:
            api_client = LingxingClient.from_settings()
        except Exception as exc:
            error = exc
    try:
        configured = mcp_credentials(settings) is not None
    except SourceCredentialsError:
        configured = False
    if configured:
        return CollectionSources(api_client, settings, db=db, observation_mode=observation_mode)
    if api_client is not None:
        return api_client
    raise LingxingError("No available Lingxing source; update source configuration") from error


def source_observations(db, sid, report_date):
    day = date.fromisoformat(report_date) if isinstance(report_date, str) else report_date
    coverage = {r.dimension: r for r in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id == str(sid), DataCoverage.metric_date == day,
        DataCoverage.dimension.in_(["mcp_sales_asin", "mcp_sales_msku", "mcp_campaign_sp", "mcp_campaign_sb", "mcp_campaign_sd"])))}
    rows = list(db.scalars(select(DailyMetric).where(DailyMetric.store_id == str(sid),
        DailyMetric.metric_date == day, DailyMetric.dimension.in_(coverage)))) if coverage else []
    def facts(dim, keys):
        if dim not in coverage or coverage[dim].status != "COMPLETE" or coverage[dim].row_count == 0:
            return {}, None
        selected = [r for r in rows if r.dimension == dim]
        result, code = {}, None
        for key in keys:
            values = [r for r in selected if r.metric_name == key]
            if not values or len(values) != coverage[dim].row_count or any(not math.isfinite(r.value) for r in values):
                continue
            if key in {"sales", "spend"}:
                codes = {r.currency for r in values}
                if len(codes) != 1 or not re.fullmatch(r"[A-Z]{3}", next(iter(codes)) or ""):
                    continue
                code = next(iter(codes))
            result[key] = math.fsum(r.value for r in values)
        return result, code
    metrics, currency = facts("mcp_sales_asin", ("sales", "units", "orders"))
    store_sales = {"source": "领星MCP产品表现", "basis": "下单口径，仅为独立观测；不与付款口径计算环比",
                   "metrics": metrics, "currency": currency} if metrics else None
    ads = []
    for kind in ("sp", "sb", "sd"):
        metrics, currency = facts("mcp_campaign_" + kind, ("spend", "clicks", "impressions"))
        if metrics:
            ads.append({"ad_type": kind.upper(), "source": "领星MCP广告报告",
                "basis": "单日活动观测，未使用归因窗口不明的转化数据", "metrics": metrics, "currency": currency})
    captured = max((r.completed_at for r in coverage.values()), default=None)
    return {"report_date": day.isoformat(), "store_sales": store_sales, "campaign_ads": ads,
            "collection_at": captured.isoformat() if captured else None}
