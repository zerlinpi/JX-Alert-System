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
    "spend": "花费", "spend_avg_7d": "过去7日平均花费", "orders": "归因订单",
    "orders_avg_7d": "过去7日平均订单", "clicks": "点击", "daily_budget": "日预算",
    "budget_ratio": "预算消耗比例", "change_ratio": "变化比例", "ratio": "变化比例",
    "report_date": "报表日期", "attribution_date": "归因成熟日", "metric_date": "指标日期",
    "ad_id": "广告商品 ID", "campaign_id": "活动 ID", "seller_sku": "MSKU", "asin": "ASIN",
    "freshness_status": "源数据状态", "source_updated_at": "源更新时间",
    "comparison_date": "比较日期", "metric_name": "指标", "ahr_score": "AHR评分",
    "attribution_complete_report_date": "归因成熟日", "currency_code": "币种",
}
_RULE_NAMES = {
    "orders_increase": "订单增长", "orders_decrease": "订单下降", "sales_increase": "销售额增长",
    "sales_decrease": "销售额下降", "units_increase": "销量增长", "units_decrease": "销量下降",
    "high_ad_spend": "广告花费增长", "spend_no_conversion": "高花费无转化",
    "low_spend_no_conversion": "低花费无转化", "budget_consumption": "预算消耗",
}
_STATUS_NAMES = {"COMPLETE": "采集完成", "NOT_CONFIGURED": "源时区待确认", "STALE": "数据过期",
                 "FUTURE": "源时间异常", "MISSING": "缺少源时间", "FRESH": "新鲜度已验证", "FAILED": "采集失败"}


def status_label(value):
    return _STATUS_NAMES.get(value, value or "未知")


def anomaly_card(*, state, severity, store, campaign, product, rule, reason, metrics, currency=None):
    identity = [("店铺", store)]
    if campaign:
        identity.append(("广告活动", campaign))
    if product:
        identity.append(("商品", product))
    identity.append(("规则", _RULE_NAMES.get(rule, rule)))
    identity.append(("币种", currency or "待确认"))
    values = []
    for key, value in metrics.items():
        if isinstance(value, (dict, list)):
            continue
        if value is None:
            value = "未知"
        elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            value = (f"{value:.1%}" if key.endswith("ratio") else number(
                value, money=key in {"spend", "spend_avg_7d", "daily_budget"}, currency=currency or "币种待确认"))
        values.append((_METRIC_NAMES.get(key, key), value))
    return card(
        f"{state} · {severity}", subtitle=reason,
        sections=[("定位", ["对象", "内容"], identity),
                  ("关键指标", ["指标", "值"], values[:20])],
        notes=["基于已采集证据判断；缺失数据不补零。", "请先核对报表日期、归因成熟度与责任人。"],
        color="green" if state == "异常恢复" else "red" if severity in {"P0", "P1"} else "orange",
    )
