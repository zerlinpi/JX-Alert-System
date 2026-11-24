"""Feishu card 2.0 building blocks.

Table cells are plain text, so provider text cannot inject mentions or links.
Markdown elements only ever receive template text plus values passed through
``md_safe``. Schema:
https://open.feishu.cn/document/feishu-cards/card-json-v2-components/content-components/table
"""
from copy import deepcopy
import html
import json
import re

# Full-width look-alikes neutralise markdown/HTML syntax inside untrusted names.
_MD = str.maketrans({"<": "＜", ">": "＞", "[": "［", "]": "］", "(": "（", ")": "）", "*": "＊",
                     "`": "｀", "~": "～", "#": "＃", "|": "｜", "&": "＆", "_": "＿", "\\": "＼"})
MAX_WIRE_BYTES = 20 * 1024
MAX_TABLES = 5


def plain(value, maximum=500):
    if value is None:
        return "—"
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value))[:maximum]


def md_safe(value, limit=120):
    """Untrusted text for markdown: no markup, mentions or auto-linked URLs."""
    return plain(value, limit).translate(_MD).replace("://", "：//").replace("www.", "www．")


def markdown(text):
    """Template text only: interpolated values must already be md_safe."""
    return {"tag": "markdown", "content": text}


def table(headers, rows, *, page_size=10):
    return {
        "tag": "table", "page_size": min(page_size, max(1, len(rows))),
        "row_height": "low", "freeze_first_column": True,
        "header_style": {"bold": True, "background_style": "grey"},
        "columns": [{"name": f"c{i}", "display_name": plain(label, 80), "data_type": "text", "width": "auto"}
                    for i, label in enumerate(headers)],
        "rows": [{f"c{i}": plain(value) for i, value in enumerate(row)} for row in rows],
    }


def shell(title, elements, color="blue"):
    return {"schema": "2.0", "config": {"wide_screen_mode": True},
            "header": {"template": color, "title": {"tag": "plain_text", "content": plain(title, 120)}},
            "body": {"elements": elements}}


def simple_card(title, text, color="blue"):
    return shell(title, [markdown(md_safe(text, 2000))], color)


def card_fits(value, reserve=0):
    """Bound both the five-table platform limit and JSON-in-JSON wire size.

    ``reserve`` keeps room for the @-mention line added at send time.
    https://open.feishu.cn/document/feishu-cards/feishu-card-cardkit/components/table
    """
    pending, tables = [value], 0
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            tables += item.get("tag") == "table"
            if tables > MAX_TABLES:
                return False
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    payload = {"receive_id": "oc_" + "x" * 64, "msg_type": "interactive",
               "content": json.dumps(value, ensure_ascii=False), "uuid": "x" * 40}
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= MAX_WIRE_BYTES - reserve


# The mention line must stay inside the room reserved for it (reports.cards.MENTION_RESERVE).
MAX_MENTION_LINE_BYTES = 1800


def mention_line(open_ids, labels=None):
    """「对应运营：@A（店铺1） @B（店铺2）」; store names are dropped when the line would be too long."""
    ids = list(dict.fromkeys(open_ids))
    labels = labels if isinstance(labels, dict) else {}
    plain_line = "对应运营：" + " ".join(f"<at id={uid}></at>" for uid in ids)
    named = "对应运营：" + " ".join(
        f"<at id={uid}></at>（{md_safe(labels[uid], 120)}）" if isinstance(labels.get(uid), str) and labels[uid]
        else f"<at id={uid}></at>" for uid in ids)
    return named if len(json.dumps(named, ensure_ascii=False).encode()) <= MAX_MENTION_LINE_BYTES else plain_line


def with_mentions(value, open_ids, labels=None):
    if not isinstance(open_ids, list) or any(
        not isinstance(uid, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", uid) for uid in open_ids
    ):
        raise ValueError("Feishu mentions must contain valid Open IDs")
    result = deepcopy(value)
    if result.get("schema") != "2.0" or not isinstance(result.get("body", {}).get("elements"), list):
        raise ValueError("Expected a Feishu 2.0 card")
    if open_ids:
        result["body"]["elements"].insert(0, {"tag": "markdown", "content": mention_line(open_ids, labels)})
    return result


def plain_text(card):
    """Readable text dump for previews, logs and tests."""
    parts = [card["header"]["title"]["content"]]
    for element in card["body"]["elements"]:
        if element["tag"] == "markdown":
            parts.append(html.unescape(element["content"]))
        elif element["tag"] == "table":
            names = [column["name"] for column in element["columns"]]
            parts.append(" | ".join(column["display_name"] for column in element["columns"]))
            parts.extend(" | ".join(str(row[name]) for name in names) for row in element["rows"])
        elif element["tag"] == "hr":
            parts.append("—" * 8)
    return "\n".join(parts)
