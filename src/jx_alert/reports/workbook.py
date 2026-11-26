"""Excel copies of the lane messages (owner, Nov 2026): the content of the cards, dated, as .xlsx.

Tables are hard to read in a group card, so the file must show everything at a glance
(owner, Nov 26 2026). The first sheet, 「完整内容」, is the whole message in reading order:
title, dates, the responsible operators, every text line and every table with all its rows.
After it, each table also has a sheet of its own, led by the period it covers, for
filtering and for stacking many days into one list. Hourly monitor: the alerts per
category on the first sheet, and all of them in 「提醒明细」, with the campaign IDs.
"""
from datetime import timezone
import math
import re
from zoneinfo import ZoneInfo

from jx_alert.reports import cards, xlsx

BEIJING = ZoneInfo("Asia/Shanghai")
CONTENT_SHEET = "完整内容"
_LINE_HEIGHT = 16.0  # points per wrapped line of 11 pt text (CJK fallback fonts included)
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
    """The lines under the title: (label, value) pairs and ready lines (e.g. 「对应运营：…」), then the time."""
    stamp = created.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")
    lines = [row if isinstance(row, str) else f"{row[0]}：{row[1]}" for row in rows if row]
    return [*lines, f"生成时间（北京）：{stamp}"]


_FIGURE_TEXT = re.compile(r"[+\-−]?\$?\d[\d,]*(?:\.\d+)?\s?(?:pp|%|个百分点)?")


def _inline(cell):
    """A table cell on the content sheet: Markdown text is typed like on the table sheets; text wraps,
    「—」 sits in the middle and figures written as text (「+2.6pp」) line up with the numbers."""
    shown = value(cell) if isinstance(cell, str) else cell
    if isinstance(shown, xlsx.Cell):
        shown = shown if not isinstance(shown.value, str) else shown.value
    if isinstance(shown, xlsx.Cell):
        return xlsx.Cell(shown.value, "top", shown.fmt)
    if shown is None or shown == "" or shown == "—":
        return xlsx.Cell("—", "middle")
    if not isinstance(shown, str):
        return xlsx.Cell(shown, "top")
    return xlsx.Cell(shown, "right" if _FIGURE_TEXT.fullmatch(shown.strip()) else "wrap")


class _Content:
    """The whole message on one sheet in reading order (see the module docstring)."""

    def __init__(self, name):
        self.sheet = xlsx.Sheet(name)
        self.texts = []

    def text(self, line, look="wrap"):
        self.texts.append((self.sheet.append([xlsx.Cell(line, look)]), line, look))

    def blank(self):
        self.sheet.append([])

    def head(self, title, details, created):
        self.text(title, "title")
        for line in _details(details, created):
            self.text(line, "mutedwrap")
        self.blank()

    def table(self, headers, rows):
        self.sheet.append([xlsx.Cell(name, "header") for name in headers])
        for row in rows:
            self.sheet.append([_inline(cell) for cell in row])
        self.blank()

    def finish(self):
        """Text rows read across the tables' width, with room for their wrapped lines."""
        sheet = self.sheet
        if not any(len(row) > 1 for row in sheet.rows):
            sheet.widths = [100.0]
        widths = xlsx.column_widths(sheet)
        span, total = len(widths), sum(widths)
        for row, line, look in self.texts:
            scale = 14 / 11 if look == "title" else 1.0
            lines = max(1, math.ceil(xlsx.display_width(line) * scale / max(total - 2.0, 10.0)))
            height = 22.0 if look == "title" else (lines * _LINE_HEIGHT + 4.0 if lines > 1 else None)
            sheet.merge(row, span, height)
        return sheet


def report_book(title, text, *, period_label, period, details=(), created):
    """Daily report or review: 「完整内容」 (the whole message, every table inline), then one sheet per table."""
    parts = blocks(text)
    tables = [part for part in parts if part[0] == "table"]
    names = xlsx.sheet_names([CONTENT_SHEET] + [part[1] or f"表格{number}" for number, part in enumerate(tables, 1)])
    content = _Content(names[0])
    content.head(title, [(period_label, period), *details], created)
    for part in parts:
        if part[0] == "text":
            content.text(part[1], "boldwrap" if part[2] else "wrap")
        else:
            content.table(part[2], part[3])
    sheets = [content.finish()]
    for number, (_, _, headers, rows) in enumerate(tables, 1):
        sheet = xlsx.Sheet(names[number], header_row=1)
        sheet.append([xlsx.Cell(name, "header") for name in [period_label, *headers]])
        for row in rows:
            sheet.append([period, *[value(cell) for cell in row]])
        sheets.append(sheet)
    return xlsx.workbook(sheets, title=title, created=created)


ALERT_HEADERS = ["统计时间（美西）", "提醒类型", "店铺", "广告活动", "广告活动 ID", "花费", "点击", "广告订单",
                 "广告销售额", "ACOS", "CVR", "建议", "运营"]


def _alert_cells(item, names, operators):
    """店铺 … 运营 of one alert (ALERT_HEADERS without the time and type); missing figures read 「—」."""
    def money(number):
        return "—" if number is None else xlsx.Cell(number, fmt='"$"#,##0.00')

    def count(number):
        return "—" if number is None else xlsx.Cell(int(number), fmt="#,##0")

    def percent(number):
        return "—" if number is None else xlsx.Cell(_fraction(number), fmt="0.0%")

    return [xlsx.Cell(names.get(item["pid"], item["pid"])), xlsx.Cell(item["campaign"]),
            xlsx.Cell(str(item["campaign_id"])), money(item.get("spend")), count(item.get("clicks")),
            count(item.get("orders")), money(item.get("sales")), percent(item.get("acos")), percent(item.get("cvr")),
            xlsx.Cell(item.get("suggestion") or "—"), xlsx.Cell(operators.get(item["pid"], "未映射运营"))]


def alerts_book(title, lead, findings, *, names, operators, period, note="", details=(), created):
    """Hourly monitor: 「完整内容」 (the card's lines and one table per category, every alert) plus
    「提醒明细」, one filterable row per alerted campaign in the card's order."""
    content = _Content(CONTENT_SHEET)
    content.head(title, [("统计时间（美西）", period), *details], created)
    content.text(lead)
    if note:
        content.text(note)
    content.blank()
    ordered = []
    for code in cards.HOURLY_ORDER:
        items = sorted((item for item in findings if item["category"] == code), key=lambda item: -item["spend"])
        if not items:
            continue
        ordered += [(code, item) for item in items]
        content.text(f"{cards.HOURLY_LABELS[code]}（{len(items)}）", "boldwrap")
        content.table(ALERT_HEADERS[2:], [_alert_cells(item, names, operators) for item in items])
    sheet = xlsx.Sheet("提醒明细", header_row=1)
    sheet.append([xlsx.Cell(name, "header") for name in ALERT_HEADERS])
    for code, item in ordered:
        sheet.append([period, cards.HOURLY_LABELS[code], *_alert_cells(item, names, operators)])
    return xlsx.workbook([content.finish(), sheet], title=title, created=created)


def file_name(base, *, who=None):
    """A safe file name: 「广告日报_2026-11-24.xlsx」, 「…_高爽.xlsx」 for one operator.

    Test sends use the same names (owner, Nov 2026: no 测试 wording); they are kept apart in
    the archive's tests folder.
    """
    name = base + (f"_{who}" if who else "")
    name = _UNSAFE_NAME.sub("", name).strip(" .")[:120] or "JX报表"
    return name + ".xlsx"


def daily_name(report_date):
    return f"广告日报_{report_date}"


def review_name(window):
    return f"广告异常复盘_{window[0]}至{window[1]}"


def hourly_name(day, time):
    return f"广告实时监控_{day}_{time.replace(':', '')}_美西"


def created_at(now):
    return now.astimezone(timezone.utc)
