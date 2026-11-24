"""DeepSeek Markdown → Feishu cards, and the hourly alert table."""
import json

from jx_alert.notifications.cards import card_fits, plain_text
from jx_alert.reports import cards
from jx_alert.reports import settings as lane_settings
from lane_fakes import HOURLY_FINDINGS


def test_markdown_tables_headings_and_bold_become_feishu_elements():
    text = ("# 标题\n正文 **重点** 和 *斜体*\n- 第一条\n* 第二条\n\n| 店铺 | 花费 |\n|:---|---:|\n| **川页** | $3,217.70 |\n"
            "| Campx |\n\n---\n```\n代码 `x`\n```\n> 引用")
    elements = cards.markdown_elements(text)
    assert [element["tag"] for element in elements] == ["markdown", "table", "hr", "markdown"]
    assert elements[0]["content"] == "**标题**\n正文 **重点** 和 ＊斜体＊\n- 第一条\n- 第二条"
    table = elements[1]
    assert [column["display_name"] for column in table["columns"]] == ["店铺", "花费"]
    assert table["rows"] == [{"c0": "川页", "c1": "$3,217.70"}, {"c0": "Campx", "c1": "—"}]
    assert "代码 ｀x｀" in elements[3]["content"] and "引用" in elements[3]["content"]


def test_model_output_cannot_inject_mentions_links_or_html():
    text = "看 [这里](https://evil.example) <at id=ou_x></at> <at id=all></at> www.evil.example\n| a |\n|---|\n| <b>x</b> |"
    elements = cards.markdown_elements(text)
    content = json.dumps(elements, ensure_ascii=False)
    assert "<at" not in content and "](" not in content and "https://" not in content and "<b>" not in content
    assert "这里" in elements[0]["content"]


def test_long_reports_are_split_within_the_five_table_limit():
    section = "| 店铺 | 花费 |\n|---|---|\n" + "\n".join(f"| 店铺{i} | ${i} |" for i in range(20))
    text = "\n\n".join(f"**第 {i} 节**\n{section}" for i in range(8))
    values = cards.report_cards("SparkX 广告日报 · 2026-11-22", text, "由 DeepSeek 生成。")
    assert len(values) == 2 and all(card_fits(value) for value in values)
    assert values[0]["header"]["title"]["content"] == "SparkX 广告日报 · 2026-11-22（1/2）"
    assert sum(element["tag"] == "table" for value in values for element in value["body"]["elements"]) == 8
    assert "由 DeepSeek 生成。" in plain_text(values[-1])


def test_tables_keep_at_most_sixty_rows():
    text = "| 店铺 |\n|---|\n" + "\n".join(f"| s{i} |" for i in range(75))
    elements = cards.markdown_elements(text)
    assert len(elements[0]["rows"]) == 60 and elements[1]["content"] == "另有 15 行未展开。"


def test_hourly_card_groups_findings_and_names_operators():
    value = cards.hourly_card(HOURLY_FINDINGS, day="2026-11-23", time="22:05", names={"101": "香港川页", "102": "Campx"},
                              operators={"101": "未映射运营", "102": "高爽"}, config=lane_settings.DEFAULTS["hourly"],
                              footer="由 DeepSeek 调用 SparkX 查询 3 次生成。")
    text = plain_text(value)
    assert value["header"]["title"]["content"] == "广告实时监控 · 美西 2026-11-23 22:05"
    assert value["header"]["template"] == "red"
    assert "**高花费无转化（1）**" in text and "Campx | 高爽 | Campaign 9001 | $41.20 | 28 | 0 | — | 0.0%" in text
    assert "低花费高转化（1）" in text and "ACOS ≤ 20%" in text
    assert cards.hourly_card([], day="d", time="t", names={}, operators={}, config=lane_settings.DEFAULTS["hourly"],
                             footer="") is None
