"""The daily report's tables, checked and arranged by the system.

DeepSeek writes every figure. A slip in its arithmetic (live, Nov 2026: 广告花费 $6,072.34 against
$7,093.46 written as -22.7% instead of -14.4%, and the -22.7% repeated in 须知) is corrected from
the two values of that row, and text repeating the wrong percentage follows the table. A change
is only corrected when no rounding of the two shown values can give it; nothing is recomputed
from SparkX.

The store table shows the figures the owner reads first right after the store name (owner,
Nov 26 2026): 广告花费, ACOS, TACOS, CPC, ROAS, CVR, whatever order the model wrote.
"""
import itertools
import re

_POINTS = re.compile(r"个?百分点|pp|pt", re.IGNORECASE)
_NUMBER = re.compile(r"([+\-−]?)\s*(\$?)\s*(\d[\d,]*)(?:\.(\d+))?")
# The only 环比 cells that are checked: a signed number, optionally $ and a unit (「-14.4%」「+0.6 pt」「-$0.07」).
_PLAIN_CHANGE = re.compile(r"\s*[+\-−]?\s*\$?\s*\d[\d,]*(?:\.\d+)?\s*(?:%|个?百分点|pp|pt)?\s*", re.IGNORECASE)
_SEPARATOR = re.compile(r"^\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?$")


def _cells(line):
    return line.strip().strip("|").split("|")


def _read(cell):
    """(value, half a unit of its last shown digit, match) of the first number in a cell, or None."""
    match = _NUMBER.search(cell)
    if not match:
        return None
    decimals = match.group(4) or ""
    value = float(match.group(3).replace(",", "") + ("." + decimals if decimals else ""))
    return (-value if match.group(1) in ("-", "−") else value), 0.5 * 10 ** -len(decimals), match


def _change(relative, today, before):
    return (today - before) / abs(before) * 100 if relative else today - before


def _bounds(relative, today, before):
    """(low, high) of the change over every value the two shown figures may stand for; None if undefined."""
    nows = (today[0] - today[1], today[0] + today[1])
    thens = (before[0] - before[1], before[0] + before[1])
    if relative and thens[0] * thens[1] <= 0:
        return None  # 前一日 is (or may round to) zero: there is no relative change
    values = [_change(relative, now, then) for now, then in itertools.product(nows, thens)]
    return min(values), max(values)


def _fits(written, bounds):
    value, half, _ = written
    return bounds is not None and value + half >= bounds[0] - 1e-9 and value - half <= bounds[1] + 1e-9


def _fix(cell, today, before):
    """(corrected cell, (wrong, right) percentage for the text or None), or None when the change adds up."""
    written = _read(cell)
    if written is None or today is None or before is None or not _PLAIN_CHANGE.fullmatch(cell.replace("**", "")):
        return None
    points = bool(_POINTS.search(cell))
    relative = "%" in cell and not points
    if _fits(written, _bounds(relative, today, before)):
        return None
    if relative and _fits(written, _bounds(False, today, before)):
        return None  # a change in percentage points written with 「%」: the number itself is right
    if _bounds(relative, today, before) is None:
        return None
    value, _, match = written
    right = _change(relative, today[0], before[0])
    number = f"{abs(right):,.{len(match.group(4) or '')}f}"
    fixed = cell[:match.start()] + ("-" if right < 0 else "+") + match.group(2) + number + cell[match.end():]
    wrong = match.group(3) + ("." + match.group(4) if match.group(4) else "")
    # Only a percentage with the same direction is followed in the text; other numbers are too ambiguous there.
    follow = (relative or points) and (value < 0) == (right < 0)
    return fixed, ((wrong, number) if follow else None)


# The columns of the daily store table that come right after 店铺, in this order (owner, Nov 26 2026).
LEADING = ("广告花费", "ACOS", "TACOS", "CPC", "ROAS", "CVR")
_ALIASES = {"PPC": "CPC", "CPC(PPC)": "CPC", "PPC(CPC)": "CPC"}


def _column(name):
    """A header as LEADING names it: no markup or spaces, ASCII upper case, 「PPC（CPC）」 → CPC."""
    key = name.replace("**", "").replace("（", "(").replace("）", ")").replace(" ", "").strip().upper()
    return _ALIASES.get(key, key)


def arrange_store_table(text):
    """The daily report with its store table (header 「店铺 | … 广告花费 …」) led by the LEADING columns.

    Every other column keeps its place in the written order after them (the cards still put 运营
    last). Rows shorter than the header get 「—」, as the cards show them; other tables are untouched.
    """
    lines = (text or "").split("\n")
    for index in range(len(lines) - 1):
        if not lines[index].strip().startswith("|") or not _SEPARATOR.match(lines[index + 1].strip()):
            continue
        keys = [_column(cell) for cell in _cells(lines[index])]
        if keys[0] != "店铺" or "广告花费" not in keys:
            continue
        lead = [keys.index(name) for name in LEADING if name in keys]
        order = [0, *lead, *(position for position in range(1, len(keys)) if position not in lead)]
        if order == list(range(len(keys))):
            return text
        row = index
        while row < len(lines) and lines[row].strip().startswith("|"):
            if row == index + 1:
                lines[row] = "|" + "|".join(["---"] * len(keys)) + "|"
            else:
                cells = _cells(lines[row]) + [" — "] * len(keys)
                lines[row] = "|" + "|".join(cells[position] for position in order) + "|"
            row += 1
        return "\n".join(lines)
    return text


def check_changes(text):
    """(report, corrections): every 环比 of a 「… | 当日 | 前一日 | 环比」 table agrees with its own row."""
    lines = (text or "").split("\n")
    follow, corrections = [], 0
    for index in range(len(lines) - 1):
        if not lines[index].strip().startswith("|") or not _SEPARATOR.match(lines[index + 1].strip()):
            continue
        header = [cell.replace("**", "").strip() for cell in _cells(lines[index])]
        if not {"当日", "前一日", "环比"} <= set(header):
            continue
        today, before, change = header.index("当日"), header.index("前一日"), header.index("环比")
        row = index + 2
        while row < len(lines) and lines[row].strip().startswith("|"):
            cells = _cells(lines[row])
            if len(cells) > max(today, before, change):
                fixed = _fix(cells[change], _read(cells[today]), _read(cells[before]))
                if fixed:
                    cells[change] = fixed[0]
                    lines[row] = "|" + "|".join(cells) + "|"
                    corrections += 1
                    if fixed[1]:
                        follow.append(fixed[1])
            row += 1
    tables = "\n".join(line for line in lines if line.strip().startswith("|"))
    for wrong, right in follow:
        pattern = re.compile(r"(?<![\d.,])" + re.escape(wrong) + r"(?=\s*(?:%|个?百分点|pp|pt))", re.IGNORECASE)
        if pattern.search(tables):
            continue  # the same number is still a figure in a table: the text may mean that one
        lines = [line if line.strip().startswith("|") else pattern.sub(right, line) for line in lines]
    return "\n".join(lines), corrections
