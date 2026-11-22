"""Mobile-readable owner report cards from already verified daily facts."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import re

from jx_alert.notifications.cards import card, card_fits, plain


_AD_TYPES = ("SP", "SB", "SBV", "SD", "共享分摊")
_CURRENCY = re.compile(r"[A-Z]{3}")
_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,40}")
_MONEY_COLUMNS = ("ad_spend", "sales", "refund_amount")
_METRIC_LABELS = ("广告花费", "净销售额", "退款金额")
_SUMMARY_NOTE = "分币种汇总，只加总有证据的店铺；缺失不作零。广告取领星产品表现，含 SP / SB / SBV / SD 及共享分摊。"
_DETAIL_NOTES = (
    "净销售额按下单日；退款金额为已发放结算口径，仅含收入退款，不含费用退款，不代表实时退款。",
    "件数退货率＝期内实际退货件数 ÷ 期内下单销量；可能包含历史订单的退货，不是原订单批次退货率或退款金额占比。",
    "“部分”仅表示所列广告类型的花费，不是店铺全部广告花费。",
)


@dataclass(frozen=True)
class _Row:
    sid: str
    store: str
    currency: str | None
    ad_spend: Decimal | None
    ad_partial_spend: Decimal | None
    ad_types: tuple[str, ...]
    sales: Decimal | None
    return_units: int | None
    sold_units: int | None
    refund_amount: Decimal | None

    @property
    def valid_unit_pair(self):
        return self.return_units is not None and self.sold_units is not None


def _number(value, *, nonnegative=False):
    if type(value) not in {int, float, Decimal}:
        return None
    number = Decimal(str(value))
    if not number.is_finite() or number.adjusted() > 25 or nonnegative and number < 0:
        return None
    return number


def _units(value):
    number = _number(value, nonnegative=True)
    return int(number) if number is not None and number == number.to_integral_value() else None


def normalize_owner_row(data):
    """Normalize once by the same display and evidence rules used by coverage checks."""
    raw_id = data["store_id"]
    if type(raw_id) not in {str, int} or not _IDENTITY.fullmatch(str(raw_id).strip()):
        raise ValueError("Owner report requires a store identity")
    sid = str(raw_id).strip()
    name = re.sub(r"\s+", " ", plain(data.get("store_name") or "店铺", 65)).strip()
    currency = data.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        currency = None
    raw_types = data.get("covered_ad_types")
    names = {value.strip().upper().removeprefix("CAMPAIGN_") for value in raw_types
             if isinstance(value, str)} if isinstance(raw_types, (list, tuple)) else set()
    ad_types = tuple(kind for kind in _AD_TYPES if kind in names)
    full_ad = _number(data.get("ad_spend"), nonnegative=True) if ad_types == _AD_TYPES else None
    partial_ad = (_number(data.get("ad_partial_spend"), nonnegative=True)
                  if 0 < len(ad_types) < len(_AD_TYPES) else None)
    return _Row(sid, f"{name}\n#{sid}", currency, full_ad, partial_ad, ad_types,
                _number(data.get("sales")), _units(data.get("return_units")),
                _units(data.get("sold_units")), _number(data.get("refund_amount")))


def _money(value, currency):
    if value is None:
        return "待核实"
    if currency is None:
        return "币种待确认"
    return f"{currency} {value:,.2f}"


def _rate(numerator, denominator):
    return f"{Decimal(numerator) / Decimal(denominator):.2%}" if denominator else "待核实"


def _summary_rows(rows):
    count = len(rows)
    result = []
    for metric, title in zip(_MONEY_COLUMNS, _METRIC_LABELS):
        groups = defaultdict(list)
        for item in rows:
            value = getattr(item, metric)
            if item.currency is not None and value is not None:
                groups[item.currency].append(value)
        if not groups:
            result.append((title, "待核实", f"0/{count} 店"))
            continue
        for currency, amounts in sorted(groups.items()):
            result.append((title, _money(sum(amounts), currency), f"{len(amounts)}/{count} 店"))
    pairs = [item for item in rows if item.valid_unit_pair]
    returns = sum(item.return_units for item in pairs)
    units = sum(item.sold_units for item in pairs)
    pair_count = f"{len(pairs)}/{count} 店"
    counts = f"{returns:,} / {units:,} 件" if pairs else "待核实"
    result.extend([
        ("已匹配退货 / 销量", counts, pair_count),
        ("合计件数退货率", _rate(returns, units), pair_count),
    ])
    return result


def _detail_rows(rows):
    ad_rows, refund_rows = [], []
    for item in rows:
        if item.ad_spend is not None:
            spend = _money(item.ad_spend, item.currency)
        elif item.ad_types and item.ad_types != _AD_TYPES:
            spend = "/".join(item.ad_types) + " 部分\n" + _money(item.ad_partial_spend, item.currency)
        elif item.ad_types == _AD_TYPES:
            spend = "广告类型已覆盖\n金额待核实"
        else:
            spend = "广告覆盖待核实"
        ad_rows.append((item.store, spend, _money(item.sales, item.currency)))
        counts = f"{item.return_units:,}" if item.return_units is not None else "—"
        counts += f" / {item.sold_units:,}" if item.sold_units is not None else " / —"
        rate = _rate(item.return_units, item.sold_units) if item.valid_unit_pair else "待核实"
        refund_rows.append((item.store, counts, rate, _money(item.refund_amount, item.currency)))
    return ad_rows, refund_rows


def _style(value, widths, *, row_height):
    tables = [element for element in value["body"]["elements"] if element["tag"] == "table"]
    for element, column_widths in zip(tables, widths, strict=True):
        element["row_height"] = row_height
        for index, (column, width) in enumerate(zip(element["columns"], column_widths, strict=True)):
            column.update(width=width, horizontal_align="right" if index else "left")
    return value


def _summary_page(subtitle, rows, page_label):
    value = card(
        "运营日报 · 汇总",
        subtitle=f"{subtitle}｜汇总 {page_label}",
        sections=[("", ["指标", "可核验合计", "覆盖"], rows)],
        notes=[_SUMMARY_NOTE, _DETAIL_NOTES[0], _DETAIL_NOTES[1]],
    )
    return _style(value, [("42%", "37%", "21%")], row_height="middle")


def _detail_page(subtitle, rows, page_label):
    ad, refunds = _detail_rows(rows)
    value = card(
        "运营日报 · 店铺明细",
        subtitle=f"{subtitle}｜明细 {page_label}",
        sections=[
            ("广告与销售", ["店铺", "广告花费", "净销售额"], ad),
            ("退货与退款", ["店铺", "退货 / 销量", "件数退货率", "退款金额"], refunds),
        ],
        notes=_DETAIL_NOTES,
    )
    return _style(value, [("37%", "34%", "29%"), ("33%", "22%", "21%", "24%")],
                  row_height="64px")


def _bounded_pages(rows, maximum, render):
    groups = []
    offset = 0
    while offset < len(rows):
        size = min(maximum, len(rows) - offset)
        while size and not card_fits(render(rows[offset:offset + size], "第 999999/999999 页")):
            size -= 1
        if not size:
            raise ValueError("An owner report section exceeds the Feishu card size")
        groups.append(rows[offset:offset + size])
        offset += size
    return [(render(group, f"第 {index}/{len(groups)} 页"), group)
            for index, group in enumerate(groups, 1)]


def build_owner_daily_pages(rows, *, report_date: date, as_of: str | datetime | None = None,
                            provisional: bool = False,
                            preview_scope: tuple[str, int] | None = None) -> list[tuple[dict, list[str]]]:
    """Return summary pages with their entire scope, then scoped detail pages.

    Caller supplies exactly one row per authorized store for the selected report
    date. Omit return_units and sold_units when they cannot be compared on a
    verified same-window basis. Settlement refund money is presented separately.
    """
    if type(report_date) is not date:
        raise ValueError("An explicit owner report date is required")
    items = [normalize_owner_row(row) for row in rows]
    if len({item.sid for item in items}) != len(items):
        raise ValueError("Owner report stores must be unique")
    subtitle = f"店铺当地报表日 {report_date}"
    if provisional:
        subtitle += "（未结束，仅供参考）｜来源更新时间未验证"
    if preview_scope is not None:
        zone_name, requested = preview_scope
        # Kept inside each card (including pagination size checks), not just in the outer audit JSON.
        if (not provisional or not isinstance(zone_name, str)
                or not re.fullmatch(r"[A-Za-z0-9_+./-]{1,100}", zone_name)
                or type(requested) is not int or requested < len(items)):
            raise ValueError("Invalid owner preview scope")
        subtitle += f"｜{zone_name} 日期组 {len(items)} 店 / 指定范围 {requested} 店"
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("Owner report observation timestamp must include a timezone")
        as_of = as_of.isoformat(timespec="seconds")
    if as_of:
        # The observation label is written into a Feishu Markdown subtitle.
        # Only allow the display characters needed by timestamp labels, never markup.
        if not isinstance(as_of, str) or not re.fullmatch(r"[A-Za-z0-9: +.\-\u4e00-\u9fff]{1,70}", as_of):
            raise ValueError("Invalid owner report observation timestamp")
        subtitle += f"｜生成于 {as_of}"
    summary = _bounded_pages(_summary_rows(items), 8,
                             lambda rows, page: _summary_page(subtitle, rows, page))
    details = _bounded_pages(items, 6,
                             lambda rows, page: _detail_page(subtitle, rows, page))
    return [(value, [item.sid for item in items]) for value, _ in summary] + [
        (value, [item.sid for item in group]) for value, group in details]


def build_owner_daily_cards(rows, *, report_date: date, as_of: str | datetime | None = None,
                            provisional: bool = False, preview_scope: tuple[str, int] | None = None) -> list[dict]:
    return [value for value, _ in build_owner_daily_pages(
        rows, report_date=report_date, as_of=as_of, provisional=provisional, preview_scope=preview_scope)]
