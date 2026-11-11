from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
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
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook(); wb.remove(wb.active)
    rows = rows or {}
    for name, headers in SHEETS.items():
        ws = wb.create_sheet(name); ws.append(headers)
        for row in rows.get(name, []): ws.append(row)
        ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1E3D5C")
            cell.alignment = Alignment(horizontal="center")
        for col in ws.columns:
            width = min(max(max(len(str(c.value or "")) for c in col) + 2, 12), 36)
            ws.column_dimensions[get_column_letter(col[0].column)].width = width
    wb.save(path)
    return path
