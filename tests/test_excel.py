from jx_alert.reporting.excel import export_daily_report

def test_excel_has_expected_sheets(tmp_path):
    path = export_daily_report(tmp_path / "daily.xlsx")
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    assert "SKU销量" in wb.sheetnames
    assert "广告活动" in wb.sheetnames
