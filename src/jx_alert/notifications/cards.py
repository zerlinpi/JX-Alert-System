"""Feishu native tables: plain-text data cells cannot inject mentions or links.

Schema: https://open.feishu.cn/document/feishu-cards/card-json-v2-components/content-components/table
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import html
import re


def plain(value, maximum=500):
    if value is None:
        return "未知"
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))[:maximum]


def number(value, *, money=False, currency=""):
    if value is None:
        return "未知（无记录）"
    try:
        n = Decimal(str(value))
        if not n.is_finite():
            return "未知（无效值）"
    except (InvalidOperation, ValueError):
        return "未知（无效值）"
    currency = {"USD": "美元", "CNY": "人民币", "EUR": "欧元", "GBP": "英镑", "JPY": "日元",
                "CAD": "加元", "AUD": "澳元", "MXN": "墨西哥比索", "BRL": "巴西雷亚尔"}.get(currency, currency)
    return f"{currency} {n:,.2f}".strip() if money else f"{n:,.2f}".rstrip("0").rstrip(".")


def change(current, previous, *, complete):
    if not complete:
        return "待完整采集"
    if current is None or previous is None:
        return "未知（缺少对比）"
    a, b = Decimal(str(current)), Decimal(str(previous))
    if not a.is_finite() or not b.is_finite():
        return "未知（无效值）"
    if b == 0:
        return "持平" if a == 0 else "前值为零"
    return f"{(a - b) / abs(b):+.1%}"


AD_KPI_HEADERS = ["指标", "本期", "参考", "变化", "判断", "本期日期"]
_KPI_STATUS = {
    "ADVERSE": "恶化待复核", "NON_ADVERSE": "—", "INSUFFICIENT_DATA": "数据不足",
    "CURRENCY_UNCONFIRMED": "币种待确认或不一致", "ZERO_DENOMINATOR": "分母为零",
    "LOW_SAMPLE": "样本不足", "NO_BASELINE": "缺少可比基线",
    "SCOPE_UNSUPPORTED": "仅店铺总览适用", "ATTRIBUTION_IMMATURE": "归因未成熟",
}


def ad_kpi_value(entry, key="current"):
    value = entry.get(key)
    if value is None:
        return "—"
    if entry["format"] == "percent":
        return f"{value:.2%}"
    if entry["format"] == "money":
        return number(value, money=True, currency=entry.get("currency") or "币种待确认")
    return f"{value:.2f} 倍"


def ad_kpi_rows(comparisons):
    rows = []
    for entry in comparisons.values():
        ratio = entry.get("change_ratio")
        basis = {"SEVEN_DAY": "七日", "PREVIOUS_DAY": "前一日"}.get(entry["comparison_basis"], "")
        rows.append((entry["label"], ad_kpi_value(entry),
            (basis + " " + ad_kpi_value(entry, "baseline")).strip(),
            f"{ratio:+.1%}" if ratio is not None else "—",
            _KPI_STATUS.get(entry["status"], "待核实"), entry["current_date"]))
    return rows


def table(headers, rows):
    return {
        "tag": "table", "page_size": min(10, max(1, len(rows))),
        "row_height": "middle", "freeze_first_column": True,
        "header_style": {"bold": True, "background_style": "grey"},
        "columns": [{"name": f"c{i}", "display_name": plain(label, 80),
                     "data_type": "text", "width": "auto"}
                    for i, label in enumerate(headers)],
        "rows": [{f"c{i}": plain(value) for i, value in enumerate(row)} for row in rows],
    }


def card(title, *, subtitle, sections, notes=(), color="blue"):
    elements = [{"tag": "markdown", "content": html.escape(subtitle, quote=False)}]
    for heading, headers, rows in sections:
        if rows:
            elements.extend([
                {"tag": "markdown", "content": f"**{html.escape(heading, quote=False)}**"},
                table(headers, rows),
            ])
    if notes:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": "\n".join(
            html.escape(plain(note, 1000), quote=False) for note in notes
        )})
    return {
        "schema": "2.0", "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": plain(title, 120)}},
        "body": {"elements": elements},
    }


def with_mentions(value, open_ids):
    if not isinstance(open_ids, list) or any(
        not isinstance(uid, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", uid) for uid in open_ids
    ):
        raise ValueError("Feishu mentions must contain valid Open IDs")
    result = deepcopy(value)
    if result.get("schema") != "2.0" or not isinstance(result.get("body", {}).get("elements"), list):
        raise ValueError("Expected a Feishu 2.0 card")
    if open_ids:
        result["body"]["elements"].insert(0, {
            "tag": "markdown", "content": "负责人：" + " ".join(
                f"<at id={uid}></at>" for uid in dict.fromkeys(open_ids)
            ),
        })
    return result


_METRIC_NAMES = {
    "current": "当前值", "previous": "前一日", "baseline_7d": "过去7日均值",
    "spend_previous": "前一日花费", "orders_previous": "前一日订单", "comparison_basis": "比较依据",
    "spend": "花费", "spend_avg_7d": "过去7日平均花费", "orders": "归因订单",
    "orders_avg_7d": "过去7日平均订单", "clicks": "点击", "daily_budget": "日预算",
    "budget_ratio": "预算消耗比例", "change_ratio": "变化比例", "ratio": "变化比例",
    "report_date": "报表日期", "attribution_date": "归因成熟日", "metric_date": "指标日期",
    "ad_id": "广告商品编号", "campaign_id": "活动编号", "seller_sku": "商品编码", "asin": "商品编号",
    "freshness_status": "源数据状态", "source_updated_at": "源更新时间",
    "comparison_date": "比较日期", "metric_name": "指标", "ahr_score": "健康评分",
    "attribution_complete_report_date": "归因成熟日", "currency_code": "币种",
    "last_over_budget_at": "超预算时间",
}
_RULE_NAMES = {
    "orders_increase": "订单增长", "orders_decrease": "订单下降", "sales_increase": "销售额增长",
    "sales_decrease": "销售额下降", "units_increase": "销量增长", "units_decrease": "销量下降",
    "high_ad_spend": "广告花费增长", "spend_no_conversion": "广告有花费但无归因订单",
    "low_spend_no_conversion": "低花费广告有点击但无归因订单", "budget_consumption": "广告花费达到预算提醒线",
    "campaign_over_budget": "广告活动出现超预算记录",
    "account_health_at_risk": "店铺账户健康风险",
    "performance_indicator_breached": "店铺绩效指标未达标",
    "performance_notice_pending": "业绩通知待处理",
}
_STORE_STATUS_TITLES = {0: "店铺停止同步", 2: "店铺授权异常", 3: "店铺欠费停服"}
_FRESHNESS_DATASETS = {
    "performance_store_performance_freshness": "店铺绩效",
    "performance_performance_notices_freshness": "业绩通知",
}
_FRESHNESS_TITLES = {
    "STALE": "数据更新滞后", "MISSING": "缺少源更新时间",
    "NOT_CONFIGURED": "源时区待确认", "FUTURE": "源更新时间异常", "FAILED": "采集失败",
}
_STATUS_NAMES = {"COMPLETE": "采集完成", "NOT_CONFIGURED": "源时区待确认", "STALE": "数据过期",
                 "FUTURE": "源时间异常", "MISSING": "缺少源时间", "FRESH": "数据有效", "FAILED": "采集失败",
                 "PARTIAL": "采集不完整", "PENDING": "等待采集", "UNKNOWN": "未知",
                 "Healthy": "正常", "At Risk": "存在风险", "AtRisk": "存在风险", "Unhealthy": "不健康"}


def status_label(value):
    return _STATUS_NAMES.get(value, value if value and re.search(r"[\u4e00-\u9fff]", str(value)) else "待确认")


def anomaly_title(rule, metrics, state, severity):
    if rule == "store_status_not_normal":
        label = _STORE_STATUS_TITLES.get(metrics.get("status_code"), "店铺同步状态异常")
    elif rule in _FRESHNESS_DATASETS:
        label = _FRESHNESS_DATASETS[rule] + _FRESHNESS_TITLES.get(
            metrics.get("freshness_status"), "源数据待核实",
        )
    elif rule == "orders_decrease" and metrics.get("attribution_complete_report_date"):
        label = "广告归因订单下降"
    else:
        label = _RULE_NAMES.get(rule, "监控提醒")
    if state == "异常恢复":
        return f"{label} · 已恢复"
    if state == "异常升级":
        label += " · 已升级"
    level = {"P0": "紧急", "P1": "重要", "P2": "关注", "P3": "提示"}.get(severity, "关注")
    return f"{label} · {level}"


def anomaly_card(*, state, severity, store, campaign, product, rule, reason, metrics, currency=None):
    identity = [("店铺", store)]
    if campaign:
        identity.append(("广告活动", campaign))
    if product:
        identity.append(("商品", product))
    recovered = state == "异常恢复"
    values = []
    for key, value in metrics.items():
        if key not in _METRIC_NAMES:
            continue
        if isinstance(value, (dict, list)):
            continue
        if value is None:
            value = "未知"
        elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            value = (f"{value:.1%}" if key.endswith("ratio") else number(
                value, money=key in {"spend", "spend_avg_7d", "spend_previous", "daily_budget"}, currency=currency or "币种待确认"))
        elif key == "comparison_basis":
            value = {"SEVEN_DAY": "较过去7日均值", "PREVIOUS_DAY": "较前一日"}.get(value, "待确认")
        elif isinstance(value, str) and key in {"freshness_status", "ahr_status"}:
            value = status_label(value)
        elif isinstance(value, str) and key == "metric_name":
            value = {"orders": "订单", "sales": "销售额", "units": "销量", "spend": "花费"}.get(value, "指标")
        values.append((_METRIC_NAMES[key], value))
    return card(
        anomaly_title(rule, metrics, state, severity),
        subtitle="监控已恢复，表中为恢复前的观测数据。" if recovered else reason,
        sections=[("定位", ["对象", "内容"], identity),
                  ("恢复前指标" if recovered else "关键指标", ["指标", "值"], values[:20])],
        color="green" if recovered else "red" if severity in {"P0", "P1"} else "orange",
    )
