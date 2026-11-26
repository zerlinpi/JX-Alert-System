"""The daily report's statements about stores, made by the system from SparkX's own rows.

Every sentence of a report must carry information (owner, Nov 26 2026). So the report no
longer lists the monitored stores without ad data or spend (mostly the same dormant
stores every day), nor explains which store totals are not updated (the table shows
「—」). What stays is the change a reader acts on: stores that spent on the day before and
nothing on the report day. It is decided here, from the SparkX results the report was
written from, because the model's own lists were unreliable (a live run missed a store
and called a store with sales 「暂无数据」, Nov 2026).

Missing data stays missing: a store only counts as having no spend when SparkX returned
a zero-spend row for it, or a complete, unfiltered query that covered it returned no row
for the day. Stores the rows cannot settle are never named. Remarks on missing data that
DeepSeek writes anyway are removed, and so is any overview before the first section.
"""
from collections import defaultdict
from datetime import datetime
import json
import re

TOOL = "get_ads_perf"
STOPPED_LABEL = "当日无广告花费（前一日有）："
# A line DeepSeek wrote under the store-table heading (no table) to list the stores without spend or data.
_LIST_LINE = re.compile(r"无广告花费|无花费|没有(?:广告)?花费|暂无|无数据|没有数据|未返回|没有返回|缺数据|未更新|没更新")
# A 须知 sentence without information for the reader (owner, Nov 26 2026): a remark on missing or not yet
# updated data, a reader note (provisional, still accumulating, currency) or an empty finding (「…：无。」).
_CLAIM = re.compile(r"暂无\S{0,6}数据|无数据|没有数据|缺(?:失)?数据|数据缺失|未返回|没有返回|未更新|没更新|没有更新"
                    r"|暂定|仍在累计|还在累计|回补|折算|金额[^。；;]{0,10}USD|USD\s*计"
                    r"|[：:]\s*(?:无|没有|暂无)\s*[。.；;]?\s*$")
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
    """({store: its rows of that day}, {stores a query showed without a row that day}).

    Only complete, unfiltered answers whose rows all name their store count: a partial answer can
    hide a store's other rows. A store's rows come from one answer, so a day queried twice is not
    added up twice.
    """
    found, without = {}, set()
    calls = zip(summary.get("tools_used") or (), summary.get("requests") or (), summary.get("evidence") or ())
    for name, arguments, text in calls:
        if name != TOOL or not isinstance(arguments, dict) or arguments.get("factEntity") != entity:
            continue
        start, end = _day(arguments.get("dateStart")), _day(arguments.get("dateEnd"))
        rows, complete = _result(text)
        if (rows is None or not complete or arguments.get("filters") or start is None or end is None
                or not start <= day <= end):
            continue
        pids = [_pid(row.get("profileId")) for row in rows]
        if None in pids:
            continue  # rows that cannot be told apart by store settle nothing
        single = start == end
        dated = single or any("date" in row for row in rows)
        on_day = defaultdict(list)
        for pid, row in zip(pids, rows):
            if pid in monitored and (single or _day(row.get("date")) == day):
                on_day[pid].append(row)
        for pid, values in on_day.items():
            found.setdefault(pid, values)
        covered = {_pid(value) for value in arguments.get("profileIds") or ()} & set(monitored)
        # Totals over several days without a date: only a store without any row had nothing that day.
        without |= covered - (set(on_day) if dated else set(pids))
    return found, without - set(found)


def _sum(rows, field):
    values = [row[field] for row in rows
              if isinstance(row.get(field), (int, float)) and not isinstance(row[field], bool)]
    return sum(values) if values else None


def stopped(summary, *, monitored, day, previous):
    """{store: its ad spend on ``previous``} of the monitored stores that spent on ``previous`` and,
    by SparkX's rows, nothing on ``day``. A store the rows cannot settle is left out."""
    monitored = set(monitored)
    ads, no_ads = observe(summary, "campaign", day, monitored)
    before, _ = observe(summary, "campaign", previous, monitored)
    found = {}
    for pid, rows in before.items():
        spend = _sum(rows, "Spend")
        if spend is None or spend <= 0:
            continue
        if pid in ads:
            today = _sum(ads[pid], "Spend")
            if today is None or today > 0:
                continue
        elif pid not in no_ads:
            continue
        found[pid] = spend
    return found


def note(stopped_stores, names):
    """「当日无广告花费（前一日有）：OCTORA（前一日 $47.80）、…。」, largest spend first; "" when none."""
    if not stopped_stores:
        return ""
    ranked = sorted(stopped_stores.items(), key=lambda item: (-item[1], names.get(item[0], item[0])))
    return STOPPED_LABEL + "、".join(f"{names.get(pid, pid)}（前一日 ${spend:,.2f}）" for pid, spend in ranked) + "。"


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


def without_preamble(text):
    """The daily report from its first section heading or table on: an overview before it only restated
    the scope, the currency and that conversions are provisional (owner, Nov 26 2026: no such text).
    Unchanged when the report has no heading or table at all."""
    lines = (text or "").split("\n")
    first = next((index for index, value in enumerate(lines) if _heading(value) or value.strip().startswith("|")),
                 None)
    return "\n".join(lines[first:] if first else lines).strip()


def apply(text, line):
    """The report with the system's ``line`` under the store table and nothing else there (the task asks
    for no notes or store lists under it), and the 须知 without sentences that carry no information."""
    lines = (text or "").split("\n")
    anchor = _anchor(lines)
    if anchor is not None:
        under_table = lines[anchor].strip().startswith("|")
        end = anchor + 1
        while end < len(lines) and not _heading(lines[end]) and not lines[end].strip().startswith("|"):
            end += 1
        # Under the table nothing stays; under a heading without a table only DeepSeek's store lists go.
        lines[anchor + 1:end] = [value for value in lines[anchor + 1:end]
                                 if not value.strip() or not under_table and not _LIST_LINE.search(value)]
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
