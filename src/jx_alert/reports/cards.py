"""Feishu cards for DeepSeek's reports and answers, and the hourly alert table.

DeepSeek writes Markdown, like an agent answering in chat. Here it becomes Feishu
card 2.0 elements: Markdown tables become native table components, headings and
**bold** stay bold, lists and paragraphs stay text. Everything else is neutralised
by md_safe, so provider text (store or campaign names) and model output can never
inject @-mentions, links or HTML. Long reports are split over several cards.
The 运营 column is always the last column of a table (owner decision, Nov 2026),
whatever order the model wrote.
"""
import copy
import re

from jx_alert.notifications.cards import card_fits, markdown, md_safe, shell, table

# Room for the send-time line "对应运营：<at id=…></at>（店铺） …" (see notifications.cards).
MENTION_RESERVE = 2048
MAX_CARDS = 4
MAX_COLUMNS = 14
MAX_ROWS = 60
_SEPARATOR = re.compile(r"^\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?$")
_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((?:https?://|mailto:)[^)\s]{1,500}\)")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_OPERATOR = re.compile(r"(?:对应|负责)?运营(?:人员|负责人)?")
_BOLD = "＊＊"


def _cells(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def column_order(headers):
    """Indexes of the columns to show: 运营 last, at most MAX_COLUMNS (运营 is kept when trimming)."""
    operator = [index for index, name in enumerate(headers) if _OPERATOR.fullmatch(name)][:2]
    rest = [index for index in range(len(headers)) if index not in operator]
    return rest[:MAX_COLUMNS - len(operator)] + operator


def _plain_cell(value):
    text = _LINK.sub(r"\1", value).replace("**", "").replace("`", "")
    return re.sub(r"<[^>]{0,200}>", "", text).strip() or "—"


def _line(line):
    """One Markdown line as safe card markdown: only **bold** survives as markup."""
    text = _LINK.sub(r"\1", line.rstrip())
    heading = re.match(r"^\s*#{1,6}\s+(.*)$", text)
    if heading:
        inner = md_safe(heading.group(1).replace("**", ""), 1500)
        return f"**{inner}**"
    quote = re.match(r"^\s*>\s?(.*)$", text)
    if quote:
        text = quote.group(1)
    bullet = re.match(r"^(\s*)[*+]\s+(.*)$", text)
    if bullet:
        text = f"{bullet.group(1)}- {bullet.group(2)}"
    safe = md_safe(text, 1500)
    # md_safe turned "**" into full-width stars; restore balanced pairs as bold.
    if safe.count(_BOLD) % 2 == 0:
        safe = safe.replace(_BOLD, "**")
    return safe


def markdown_elements(text):
    """Feishu elements for one Markdown document (tables, paragraphs, rules)."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    elements, paragraph, index, fenced = [], [], 0, False

    def flush():
        if paragraph:
            content = "\n".join(paragraph).strip("\n")
            if content.strip():
                elements.append(markdown(content))
            paragraph.clear()

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            index += 1
            continue
        if (not fenced and stripped.startswith("|") and index + 1 < len(lines)
                and _SEPARATOR.match(lines[index + 1].strip())):
            flush()
            written = [_plain_cell(cell) for cell in _cells(stripped)]
            order = column_order(written)
            headers = [written[position] for position in order]
            rows = []
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [_plain_cell(cell) for cell in _cells(lines[index])]
                cells = (cells + ["—"] * len(written))[:len(written)]
                rows.append([cells[position] for position in order])
                index += 1
            if len(rows) > MAX_ROWS:
                hidden = len(rows) - MAX_ROWS
                rows = rows[:MAX_ROWS]
                elements.append(table(headers, rows))
                elements.append(markdown(f"另有 {hidden} 行未展开。"))
            elif rows:
                elements.append(table(headers, rows))
            continue
        if not fenced and _RULE.match(raw):
            flush()
            elements.append({"tag": "hr"})
        elif stripped:
            paragraph.append(_line(raw) if not fenced else md_safe(raw, 1500))
        else:
            flush()
        index += 1
    flush()
    return elements


def split_cards(title, elements, *, color="blue", reserve=MENTION_RESERVE, max_cards=MAX_CARDS):
    """As few cards as fit Feishu's size and five-table limits; titles get (1/n) when split."""
    groups, current = [], []
    for element in elements:
        candidate = current + [element]
        if current and not card_fits(shell(title, candidate, color), reserve if not groups else 0):
            groups.append(current)
            current = [element]
        else:
            current = candidate
    if current:
        groups.append(current)
    groups = [group for group in groups if group][:max_cards]
    if len(groups) == 1:
        return [shell(title, groups[0], color)]
    return [shell(f"{title}（{index}/{len(groups)}）", group, color) for index, group in enumerate(groups, 1)]


def report_cards(title, text, *, color="blue", reserve=MENTION_RESERVE):
    """The report itself: no footer about where the data came from or how it was written (owner, Nov 2026)."""
    elements = markdown_elements(text)
    cards = [card for index, card in enumerate(split_cards(title, elements, color=color, reserve=reserve))
             if card_fits(card, reserve if index == 0 else 0)]
    return cards or [shell(title, [markdown("内容过长，无法在飞书卡片中展示，请在管理台预览查看。")], color)]


# ---------------------------------------------------------------- hourly monitor

HOURLY_ORDER = ("high_spend_no_conversion", "high_spend_low_conversion", "low_spend_no_conversion",
                "low_spend_high_conversion")
HOURLY_LABELS = {"high_spend_no_conversion": "高花费无转化", "high_spend_low_conversion": "高花费低转化",
                 "low_spend_no_conversion": "低花费无转化", "low_spend_high_conversion": "低花费高转化"}
INTRADAY_NOTE = "今日数据仍在累计，广告订单会延迟回补；金额 USD。"


def _money(value):
    return "—" if value is None else f"${value:,.2f}"


def _count(value):
    return "—" if value is None else f"{value:,.0f}"


def _percent(value):
    return "—" if value is None else f"{value:.1f}%"


def hourly_title(day, time):
    return f"广告实时监控 · 美西 {day} {time}"


def hourly_lead(findings, *, scoped=False):
    """The first line of an hourly alert: counts plus the one data note the SparkX Skill requires."""
    stores = len({item["pid"] for item in findings})
    return (("你负责的店铺中，" if scoped else "") + f"{stores} 个店铺 {len(findings)} 个广告活动触发提醒。"
            + INTRADAY_NOTE)


def hourly_card(findings, *, day, time, names, operators, scoped=False, cap=10):
    """The alert table of new findings, grouped by category; None when there is nothing to send.

    The title carries the marketplace day and time; the body is the alerts plus the one data note
    the SparkX Skill requires (intraday figures are not final; multi-store money is USD).
    """
    if not findings:
        return None
    elements = [markdown(hourly_lead(findings, scoped=scoped))]
    headers = ["店铺", "广告活动", "花费", "点击", "广告订单", "ACOS", "CVR", "建议", "运营"]
    for code in HOURLY_ORDER:
        items = [item for item in findings if item["category"] == code]
        if not items:
            continue
        items.sort(key=lambda item: -item["spend"])
        rows = [(names.get(item["pid"], item["pid"]), item["campaign"], _money(item["spend"]), _count(item["clicks"]),
                 _count(item["orders"]), _percent(item["acos"]), _percent(item["cvr"]), item["suggestion"] or "—",
                 operators.get(item["pid"], "未映射运营")) for item in items[:cap]]
        elements += [markdown(f"**{HOURLY_LABELS[code]}（{len(items)}）**"), table(headers, rows)]
        if len(items) > cap:
            elements.append(markdown(f"另有 {len(items) - cap} 个{HOURLY_LABELS[code]}活动未展开。"))
    color = "red" if any(item["category"].startswith("high_spend") for item in findings) else "orange"
    value = shell(hourly_title(day, time), elements, color)
    if card_fits(value, MENTION_RESERVE):
        return value
    if cap > 2:
        return hourly_card(findings, day=day, time=time, names=names, operators=operators, scoped=scoped,
                           cap=cap // 2)
    return None


# ---------------------------------------------------------------- console test sends

def test_copies(values):
    """The previewed cards for a test send, unchanged (owner, Nov 2026: no 测试 wording in the message).

    Each copy must still fit Feishu's card limit, the first one with room for the @-mention line.
    """
    copies = []
    for index, value in enumerate(values):
        card = copy.deepcopy(value)
        if not card_fits(card, MENTION_RESERVE if index == 0 else 0):
            raise ValueError("test card does not fit")
        copies.append(card)
    return copies
