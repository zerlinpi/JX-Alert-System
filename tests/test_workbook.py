"""Excel copies of the lane messages: the .xlsx writer and the report → workbook conversion."""
from datetime import datetime, timezone
import io
import re
import zipfile

from jx_alert.reports import archive, workbook, xlsx
from lane_fakes import HOURLY_FINDINGS, REVIEW_TEXT

CREATED = datetime(2026, 11, 25, 0, 31, tzinfo=timezone.utc)


def parts(data):
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        return {name: book.read(name).decode("utf-8") for name in book.namelist()}


def strings(files):
    return re.findall(r'<t xml:space="preserve">([^<]*)</t>', files["xl/sharedStrings.xml"])


def test_table_cells_keep_numbers_as_numbers():
    money = workbook.value("$3,217.70")
    assert (money.value, money.fmt) == (3217.7, '"$"#,##0.00')
    assert workbook.value("$5,664").value == 5664 and workbook.value("-$0.07").value == -0.07
    share = workbook.value("27.1%")
    assert (share.value, share.fmt) == (0.271, "0.0%")
    change = workbook.value("+21.2%")
    assert change.value == 0.212 and change.fmt == "+0.0%;-0.0%;0.0%"
    assert workbook.value("−14.4%").value == -0.144  # the minus sign models sometimes write
    assert (workbook.value("273").value, workbook.value("273").fmt) == (273, "#,##0")
    assert workbook.value("3.55").fmt == "#,##0.00" and workbook.value("+0.61").fmt.startswith("+")
    for text in ("—", "2.35 → 1.64", "-5.9 个百分点", "2026-11-22", "香港川页", "1557478276595124"):
        assert workbook.value(text) == text  # text, dates and profile IDs stay text


def test_report_tables_become_sheets_named_by_their_headings():
    found = workbook.blocks(REVIEW_TEXT)
    tables = [block for block in found if block[0] == "table"]
    assert [block[1] for block in tables] == ["二、异常店铺总览"]
    assert tables[0][2][-1] == "运营" and tables[0][2][0] == "店铺"  # 运营 moved last, as on the card
    assert ("text", "一、最严重", True) in found


def test_a_report_workbook_is_dated_valid_and_reproducible():
    data = workbook.report_book("广告异常复盘 · 11-16~11-22 对比前 7 日", REVIEW_TEXT, period_label="近 7 日（美西）",
                                period="2026-11-16 ~ 2026-11-22", details=[("对比前 7 日", "2026-11-09 ~ 2026-11-15")],
                                created=CREATED)
    assert data == workbook.report_book("广告异常复盘 · 11-16~11-22 对比前 7 日", REVIEW_TEXT, period_label="近 7 日（美西）",
                                        period="2026-11-16 ~ 2026-11-22",
                                        details=[("对比前 7 日", "2026-11-09 ~ 2026-11-15")], created=CREATED)
    files = parts(data)
    assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml",
            "xl/sharedStrings.xml", "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml", "docProps/core.xml"} <= set(files)
    assert re.findall(r'<sheet name="([^"]+)"', files["xl/workbook.xml"]) == ["摘要", "二、异常店铺总览"]
    assert "_xlnm._FilterDatabase" in files["xl/workbook.xml"] and "'二、异常店铺总览'!$A$1:$H$3" in files["xl/workbook.xml"]
    text = strings(files)
    assert text[0] == "广告异常复盘 · 11-16~11-22 对比前 7 日" and "近 7 日（美西）：2026-11-16 ~ 2026-11-22" in text
    assert "生成时间（北京）：2026-11-25 08:31" in text and "表格见工作表「二、异常店铺总览」（2 行）" in text
    sheet = files["xl/worksheets/sheet2.xml"]
    assert 'state="frozen"' in sheet and '<autoFilter ref="A1:H3"/>' in sheet and "<v>1900.0</v>" in sheet
    assert "<dc:creator>JX Alert System</dc:creator>" in files["docProps/core.xml"]
    assert "2026-11-25T00:31:00Z" in files["docProps/core.xml"]


def test_hourly_alerts_become_one_row_per_campaign():
    names, operators = {"102": "Campx", "101": "香港川页"}, {"102": "高爽"}
    data = workbook.alerts_book("广告实时监控 · 美西 2026-11-23 22:05", "2 个店铺 3 个广告活动触发提醒。", HOURLY_FINDINGS,
                                names=names, operators=operators, period="2026-11-23 22:05", created=CREATED)
    files = parts(data)
    assert re.findall(r'<sheet name="([^"]+)"', files["xl/workbook.xml"]) == ["摘要", "提醒明细"]
    text = strings(files)
    assert workbook.ALERT_HEADERS[0] in text and "高花费无转化" in text and "低花费高转化" in text
    assert "Campaign 9001" in text and "9001" in text and "高爽" in text and "未映射运营" in text
    sheet = files["xl/worksheets/sheet2.xml"]
    assert len(re.findall(r'<row r="\d+">', sheet)) == 1 + len(HOURLY_FINDINGS)
    assert "<v>41.2</v>" in sheet and "<v>0.124</v>" in sheet  # money and ACOS as numbers


def test_sheet_and_file_names_are_safe():
    assert xlsx.sheet_names(["a/b:c*?[d]", "A/B:C*?[D]", "", "x" * 40]) == ["abcd", "ABCD(2)", "表3", "x" * 31]
    assert workbook.file_name("广告日报_2026-11-24") == "广告日报_2026-11-24.xlsx"
    assert workbook.file_name("广告实时监控_2026-11-24_1705_美西", who="高/爽:") == "广告实时监控_2026-11-24_1705_美西_高爽.xlsx"
    assert workbook.file_name("广告日报_2026-11-24", test=True) == "【测试】广告日报_2026-11-24.xlsx"
    assert xlsx.column_letter(0) == "A" and xlsx.column_letter(25) == "Z" and xlsx.column_letter(26) == "AA"


def test_the_archive_keeps_each_file_once_and_never_overwrites(settings):
    first = archive.save(settings, "daily", "广告日报_2026-11-24.xlsx", b"PK one", now=CREATED)
    assert first["path"] == "daily/2026-11/广告日报_2026-11-24.xlsx" and first["size"] == 6
    assert archive.save(settings, "daily", "广告日报_2026-11-24.xlsx", b"PK one", now=CREATED) == first  # same file
    other = archive.save(settings, "daily", "广告日报_2026-11-24.xlsx", b"PK two", now=CREATED)
    assert other["name"] == "广告日报_2026-11-24_2.xlsx" and archive.read(settings, first) == b"PK one"
    assert {row["name"] for row in archive.listing(settings)} == {"广告日报_2026-11-24_2.xlsx", "广告日报_2026-11-24.xlsx"}
    for meta in ({**first, "sha256": "0" * 64}, {**first, "path": "../secrets/x.xlsx"}, {"path": first["path"]}):
        try:
            archive.read(settings, meta)
        except archive.ArchiveError:
            continue
        raise AssertionError("an unsafe or changed file was read")
