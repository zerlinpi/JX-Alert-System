from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


SHEETS = {
    "店铺总览": ["店铺", "站点", "日期", "订单", "销量", "销售额", "币种", "广告花费", "广告订单"],
    "店铺绩效": ["店铺", "站点", "健康状态", "健康评分", "通知标题", "通知状态"],
    "SKU销量": ["店铺", "ASIN", "MSKU", "Local SKU", "日期", "订单", "销量", "销售额", "七日均值", "变化率"],
    "广告活动": ["店铺", "广告类型", "活动ID", "活动名称", "日期", "花费", "订单", "销售额", "ACOS", "ROAS", "点击", "曝光"],
    "广告商品": ["店铺", "活动ID", "ASIN", "MSKU", "日期", "花费", "订单", "销售额"],
    "异常记录": ["等级", "店铺", "产品", "SKU", "广告活动", "规则", "昨日", "前日", "七日均值", "变化率", "负责人", "状态"],
    "采集日志": ["任务", "店铺", "接口", "开始时间", "结束时间", "状态", "记录数", "错误"],
}


def export_daily_report(path: str | Path, rows: dict[str, list[list]] | None = None) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.remove(workbook.active)
    rows = rows or {}

    for name, headers in SHEETS.items():
        worksheet = workbook.create_sheet(name)
        worksheet.append(headers)
        for row in rows.get(name, []):
            worksheet.append(row)
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions

        for cell in worksheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1E3D5C")
            cell.alignment = Alignment(horizontal="center")

        for column in worksheet.columns:
            width = min(max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 36)
            worksheet.column_dimensions[get_column_letter(column[0].column)].width = width

    workbook.save(output_path)
    return output_path
