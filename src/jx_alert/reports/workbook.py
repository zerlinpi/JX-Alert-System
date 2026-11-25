"""Excel copies of the lane messages (owner, Nov 2026): the content of the cards, dated, as .xlsx.

Daily report and review: a 「摘要」 sheet with the dates and the text, then one sheet per
table. Hourly monitor: the alert rows, with the campaign IDs. Every table sheet starts with
the period it covers, so sheets of many days can be stacked into one list. The workbook is
built from exactly what the card shows: it never says more or less than its message.
"""
from datetime import timezone
import re
from zoneinfo import ZoneInfo

from jx_alert.reports import cards, xlsx

BEIJING = ZoneInfo("Asia/Shanghai")
_MONEY = re.compile(r"([+\-−]?)\s?\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")
_PERCENT = re.compile(r"([+\-−]?)\s?(\d+)(?:\.(\d+))?\s?%")
_PLAIN = re.compile(r"([+\-−]?)\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")
_HEADING = re.compile(r"^(?:#{1,6}\s+.+|\*\*[^*\n]{1,60}\*\*[：:]?)$")
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')
_SHORT_TITLE = 40


def value(text):
    """A table cell as Excel should hold it: $1,234.50 → money, 12.3% → percent, 273 → number; else text."""
    raw = str(text).strip()
    for pattern, kind in ((_MONEY, "money"), (_PERCENT, "percent"), (_PLAIN, "plain")):
        found = pattern.fullmatch(raw)
        if not found:
            continue
        sign, whole, decimals = found.group(1), found.group(2).replace(",", ""), found.group(3) or ""
        if kind == "plain" and len(whole) > 11:
            return raw  # IDs stay text: Excel keeps only 15 significant digits
        number = float(whole + ("." + decimals if decimals else ""))
        if sign in ("-", "−"):
            number = -number
        places = "." + "0" * len(decimals) if decimals else ""
        if kind == "money":
            return xlsx.Cell(number, fmt='"$"#,##0' + places)
        if kind == "percent":
            fmt = "0" + places + "%"
            return xlsx.Cell(_fraction(number), fmt=f"+{fmt};-{fmt};{fmt}" if sign else fmt)
        fmt = "#,##0" + places
        return xlsx.Cell(number if decimals else int(number), fmt=f"+{fmt};-{fmt};{fmt}" if sign else fmt)
    return raw


def _fraction(percent):
    """12.4 (%) → 0.124, without binary noise such as 0.12400000000000001."""
    return round(percent / 100, 12)


def _plain(line):
    """(text without Markdown markers, whether the line is a heading)."""
    text = cards._LINK.sub(r"\1", line.strip())
    heading = bool(_HEADING.match(text))
    text = re.sub(r"^#{1,6}\s+", "", text)
    text = re.sub(r"^>\s?", "", text)
    text = re.sub(r"^[*+]\s+", "- ", text)
    text = re.sub(r"<[^>]{0,200}>", "", text.replace("**", "").replace("`", ""))
    return text.strip(), heading


def blocks(text):
    """[("text", line, is_heading) | ("table", title, headers, rows)] in reading order; 运营 is the last column."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    found, index, last = [], 0, None
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("```") or cards._RULE.match(stripped):
            index += 1
            continue
        if stripped.startswith("|") and index + 1 < len(lines) and cards._SEPARATOR.match(lines[index + 1].strip()):
            written = [cards._plain_cell(cell) for cell in cards._cells(stripped)]
            order = cards.column_order(written)
            rows, index = [], index + 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = ([cards._plain_cell(cell) for cell in cards._cells(lines[index])] + ["—"] * len(written))
                rows.append([cells[:len(written)][position] for position in order])
                index += 1
            title = last if last and (last[1] or len(last[0]) <= _SHORT_TITLE) else None
            found.append(("table", title[0].rstrip("：:") if title else None, [written[p] for p in order], rows))
            last = None
            continue
        if stripped:
            line, heading = _plain(stripped)
            if line:
                found.append(("text", line, heading))
                last = (line, heading)
        index += 1
    return found


def _details(rows, created):
    stamp = created.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")
    return [*rows, ("生成时间（北京）", stamp)]


def report_book(title, text, *, period_label, period, details=(), created):
    """Daily report or review: 「摘要」 (dates and text) plus one sheet per table, each led by the period."""
    parts = blocks(text)
    tables = [part for part in parts if part[0] == "table"]
    names = xlsx.sheet_names(["摘要"] + [part[1] or f"表格{number}" for number, part in enumerate(tables, 1)])
    summary = xlsx.Sheet(names[0], widths=[120])
    summary.append([xlsx.Cell(title, "title")])
    for label, shown in _details([(period_label, period), *details], created):
        summary.append([xlsx.Cell(f"{label}：{shown}", "muted")])
    summary.append([])
    sheets, number = [summary], 0
    for part in parts:
        if part[0] == "text":
            summary.append([xlsx.Cell(part[1], "bold" if part[2] else "wrap")])
            continue
        number += 1
        _, _, headers, rows = part
        summary.append([xlsx.Cell(f"表格见工作表「{names[number]}」（{len(rows)} 行）", "muted")])
        sheet = xlsx.Sheet(names[number], header_row=1)
        sheet.append([xlsx.Cell(name, "header") for name in [period_label, *headers]])
        for row in rows:
            sheet.append([period, *[value(cell) for cell in row]])
        sheets.append(sheet)
    return xlsx.workbook(sheets, title=title, created=created)


ALERT_HEADERS = ["统计时间（美西）", "提醒类型", "店铺", "广告活动", "广告活动 ID", "花费", "点击", "广告订单",
                 "广告销售额", "ACOS", "CVR", "建议", "运营"]


def alerts_book(title, lead, findings, *, names, operators, period, note="", created):
    """Hourly monitor: 「摘要」 plus 「提醒明细」 with one row per alerted campaign (same order as the card)."""
    summary = xlsx.Sheet("摘要", widths=[120])
    summary.append([xlsx.Cell(title, "title")])
    for label, shown in _details([("统计时间（美西）", period)], created):
        summary.append([xlsx.Cell(f"{label}：{shown}", "muted")])
    summary.append([])
    summary.append([xlsx.Cell(lead, "wrap")])
    if note:
        summary.append([xlsx.Cell(note, "wrap")])
    summary.append([xlsx.Cell(f"明细见工作表「提醒明细」（{len(findings)} 行）", "muted")])
    sheet = xlsx.Sheet("提醒明细", header_row=1)
    sheet.append([xlsx.Cell(name, "header") for name in ALERT_HEADERS])

    def percent(number):
        return None if number is None else xlsx.Cell(_fraction(number), fmt="0.0%")

    for code in cards.HOURLY_ORDER:
        for item in sorted((item for item in findings if item["category"] == code), key=lambda item: -item["spend"]):
            sheet.append([period, cards.HOURLY_LABELS[code], names.get(item["pid"], item["pid"]), item["campaign"],
                          str(item["campaign_id"]), xlsx.Cell(item["spend"], fmt='"$"#,##0.00'),
                          xlsx.Cell(int(item["clicks"]), fmt="#,##0"), xlsx.Cell(int(item["orders"]), fmt="#,##0"),
                          None if item.get("sales") is None else xlsx.Cell(item["sales"], fmt='"$"#,##0.00'),
                          percent(item.get("acos")), percent(item.get("cvr")), item.get("suggestion") or "—",
                          operators.get(item["pid"], "未映射运营")])
    return xlsx.workbook([summary, sheet], title=title, created=created)


def file_name(base, *, who=None, test=False):
    """A safe file name: 「广告日报_2026-11-24.xlsx」, 「…_高爽.xlsx」 for one operator, 【测试】 for test sends."""
    name = base + (f"_{who}" if who else "")
    name = _UNSAFE_NAME.sub("", name).strip(" .")[:120] or "JX报表"
    return ("【测试】" if test else "") + name + ".xlsx"


def daily_name(report_date):
    return f"广告日报_{report_date}"


def review_name(window):
    return f"广告异常复盘_{window[0]}至{window[1]}"


def hourly_name(day, time):
    return f"广告实时监控_{day}_{time.replace(':', '')}_美西"


def created_at(now):
    return now.astimezone(timezone.utc)
