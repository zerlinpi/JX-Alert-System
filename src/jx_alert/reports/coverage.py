"""The daily report's list of stores without data, stated by the system from SparkX's own rows.

DeepSeek writes the daily report from SparkX. Which monitored stores it could not show
(no ad rows that day, no store totals, store orders not updated yet) is decided here,
from the SparkX results the report was written from, so the list is complete and never
contradicts the tables. In a live run the model's own list once missed a store and said
a store had no total sales while its table showed them (owner, Nov 2026).

Missing data stays missing: a store only counts as having no data when a complete,
unfiltered query that covered it returned no row for the day. When the results cannot
settle every monitored store, the report stays exactly as DeepSeek wrote it.
"""
from collections import defaultdict
from datetime import datetime
import json
import re

TOOL = "get_ads_perf"
DASH_NOTE = "（表中记「—」）"
# A line DeepSeek wrote under the store table to list the stores without spend or data.
_LIST_LINE = re.compile(r"无广告花费|无花费|没有(?:广告)?花费|暂无|无数据|没有数据|未返回|没有返回|缺数据|未更新|没更新")
# A 须知 sentence about missing or not yet updated data: the system's line states that instead.
_CLAIM = re.compile(r"暂无\S{0,6}数据|无数据|没有数据|缺(?:失)?数据|数据缺失|未返回|没有返回|未更新|没更新|没有更新")
_SENTENCE = re.compile(r"[^。；;]+[。；;]?|[。；;]")
_MARKER = re.compile(r"^(\s*(?:[-*+]\s+|\d+[.、．]\s*))")
_NUMBERED = re.compile(r"^(\s*)\d+([.、．]\s*)")
_HEADING = re.compile(r"^(?:#{1,6}\s+.+|(?:\d+[.、．]\s*)?\*\*[^*\n]{1,40}\*\*[：:]?)$")
_SEPARATOR = re.compile(r"^\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?$")


# ---------------------------------------------------------------- what SparkX returned

def _day(value):
    text = str(value if value is not None else "").strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _pid(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return str(int(value.strip())) if int(value.strip()) > 0 else None
    return None


def _result(text):
    """(rows, complete) of one get_ads_perf answer; rows is None when the answer has no row list."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None, False
    if not isinstance(value, dict) or value.get("isError") is True:
        return None, False
    data = value.get("data")
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None, False
    meta = value.get("meta") if isinstance(value.get("meta"), dict) else {}
    return [row for row in rows if isinstance(row, dict)], meta.get("hasNextPage") is not True


def observe(summary, entity, day, monitored):
    """({store: its rows of that day}, {stores a complete unfiltered query showed without a row that day})."""
    found, without = defaultdict(list), set()
    calls = zip(summary.get("tools_used") or (), summary.get("requests") or (), summary.get("evidence") or ())
    for name, arguments, text in calls:
        if name != TOOL or not isinstance(arguments, dict) or arguments.get("factEntity") != entity:
            continue
        start, end = _day(arguments.get("dateStart")), _day(arguments.get("dateEnd"))
        rows, complete = _result(text)
        if rows is None or start is None or end is None or not start <= day <= end:
            continue
        single = start == end
        dated = single or any("date" in row for row in rows)
        on_day, in_range, attributable = set(), set(), True
        for row in rows:
            pid = _pid(row.get("profileId"))
            if pid is None:
                attributable = False
                continue
            in_range.add(pid)
            if pid in monitored and (single or _day(row.get("date")) == day):
                on_day.add(pid)
                found[pid].append(row)
        if complete and attributable and not arguments.get("filters"):
            covered = {_pid(value) for value in arguments.get("profileIds") or ()} & set(monitored)
            # Totals over several days without a date: only a store without any row had nothing that day.
            without |= covered - (on_day if dated else in_range)
    return dict(found), without - set(found)


def _sum(rows, field):
    values = [row[field] for row in rows
              if isinstance(row.get(field), (int, float)) and not isinstance(row[field], bool)]
    return sum(values) if values else None


def classify(summary, *, monitored, day):
    """Which monitored stores had spend, no spend, or no ad data that day, and the gaps in store totals.

    None when SparkX's rows in this run cannot settle every monitored store.
    """
    monitored = set(monitored)
    ads, no_ads = observe(summary, "campaign", day, monitored)
    spent, idle = set(), set()
    for pid in monitored:
        if pid in ads:
            spend = _sum(ads[pid], "Spend")
            if spend is None:
                return None
            (spent if spend > 0 else idle).add(pid)
        elif pid not in no_ads:
            return None
    totals, no_totals = observe(summary, "asin", day, monitored)
    totals_missing, lagging, counted = set(), set(), set()
    for pid in spent:
        sales = _sum(ads[pid], "Sales") or 0
        rows = totals.get(pid)
        if rows:
            amount = _sum(rows, "TotalSalesAmount")
            if amount is not None and amount <= 0 < sales:
                totals_missing.add(pid)  # ad sales but no store sales: SparkX has no store totals
            orders, units = _sum(rows, "OrderCount"), _sum(rows, "UnitCount")
            if orders is not None and units is not None and units > 0:
                counted.add(pid)
                if orders <= 0:
                    lagging.add(pid)  # units sold but no orders yet: store orders are not updated
        elif pid in no_totals and sales > 0:
            totals_missing.add(pid)
    return {"spent": spent, "idle": idle, "missing": monitored - spent - idle, "totals_missing": totals_missing,
            "orders_lagging": lagging, "orders_counted": counted}


def note(found, names):
    """One line: stores without ad data or spend, then gaps in the store totals; "" when there are none."""
    def listed(pids):
        return "、".join(sorted(names.get(pid, pid) for pid in pids))

    parts = []
    if found["missing"]:
        parts.append("暂无广告数据：" + listed(found["missing"]))
    if found["idle"]:
        parts.append("当日无广告花费：" + listed(found["idle"]))
    gaps = []
    if found["totals_missing"]:
        gaps.append("暂无店铺总数据：" + listed(found["totals_missing"]))
    if found["orders_lagging"]:
        every = found["orders_lagging"] == found["orders_counted"] and len(found["orders_counted"]) > 1
        gaps.append("各店总订单尚未更新" if every else "总订单尚未更新：" + listed(found["orders_lagging"]))
    if gaps:
        parts.append("；".join(gaps) + DASH_NOTE)
    return "；".join(parts) + "。" if parts else ""


# ---------------------------------------------------------------- the report text

def _plain(line):
    return re.sub(r"^#{1,6}\s*|\*\*|[`>]", "", line.strip()).strip()


def _heading(line):
    return bool(_HEADING.match(line.strip()))


def _store_table(lines):
    """(first, last) line index of the per-store table (header 「店铺 | … 广告花费 …」), or None."""
    for index, line in enumerate(lines[:-1]):
        cells = [_plain(cell) for cell in line.strip().strip("|").split("|")]
        if (line.strip().startswith("|") and cells and cells[0] == "店铺" and any("广告花费" in c for c in cells)
                and _SEPARATOR.match(lines[index + 1].strip())):
            last = index + 1
            while last + 1 < len(lines) and lines[last + 1].strip().startswith("|"):
                last += 1
            return index, last
    return None


def _anchor(lines):
    """Index of the line the system's list goes under: the store table's last row, else its heading."""
    table = _store_table(lines)
    if table:
        return table[1]
    return next((index for index, value in enumerate(lines) if _heading(value) and "店铺明细" in _plain(value)), None)


def _notes(lines):
    """(heading index, end index) of the 须知 section, or None."""
    start = next((index for index, value in enumerate(lines)
                  if _heading(value) and re.sub(r"^\d+[.、．]\s*", "", _plain(value)).startswith("须知")), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and not _heading(lines[end]):
        end += 1
    return start, end


def _without_claims(line):
    """The line without sentences about missing data; None when nothing else is left."""
    marker = _MARKER.match(line)
    prefix = marker.group(1) if marker else ""
    body = line[len(prefix):]
    kept = "".join(part for part in _SENTENCE.findall(body) if not _CLAIM.search(part)).strip()
    if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", kept):
        return None
    return prefix + re.sub(r"[；;，,、]$", "。", kept)


def apply(text, line):
    """The report with the system's ``line`` under the store table instead of DeepSeek's own list,
    and the 须知 without remarks on missing data (the line states them)."""
    lines = (text or "").split("\n")
    anchor = _anchor(lines)
    if anchor is not None:
        end = anchor + 1
        while end < len(lines) and not _heading(lines[end]) and not lines[end].strip().startswith("|"):
            end += 1
        lines[anchor + 1:end] = [value for value in lines[anchor + 1:end]
                                 if not (value.strip() and _LIST_LINE.search(value))]
    notes = _notes(lines)
    if notes is not None:
        start, end = notes
        section, number = [], 0
        for value in lines[start + 1:end]:
            kept = value if not value.strip() or value.strip().startswith("|") else _without_claims(value)
            if kept is None:
                continue
            if _NUMBERED.match(kept):
                number += 1
                kept = _NUMBERED.sub(lambda match: f"{match.group(1)}{number}{match.group(2)}", kept, count=1)
            section.append(kept)
        if any(value.strip() for value in section):
            lines[start + 1:end] = section
        else:
            del lines[start:end]  # only remarks on missing data were left: the heading goes too
    if line:
        anchor, notes = _anchor(lines), _notes(lines)
        if anchor is not None:
            lines[anchor + 1:anchor + 1] = [line, ""]
        elif notes is not None:
            lines[notes[0]:notes[0]] = [line, ""]
        else:
            lines += ["", line]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
