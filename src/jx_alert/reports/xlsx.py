"""A small Office Open XML (.xlsx) writer: text and numbers with formats, frozen headers and filters.

The server images carry no spreadsheet library and are not rebuilt on upgrade, so the
workbooks are written with the standard library. The output follows ECMA-376 (shared
strings, one styles part, one worksheet part per sheet), the same parts Excel writes.
"""
from datetime import datetime, timezone
import io
import math
import re
import zipfile
from xml.sax.saxutils import escape

# Cell looks: (font, fill, alignment). Fonts: 0 normal, 1 bold, 2 title, 3 small grey.
LOOKS = {"text": (0, 0, None), "title": (2, 0, None), "header": (1, 2, "center"), "bold": (1, 0, None),
         "wrap": (0, 0, "wrap"), "muted": (3, 0, None)}
_BUILTIN_FORMATS = {"0": 1, "0.00": 2, "#,##0": 3, "#,##0.00": 4, "0%": 9, "0.00%": 10}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_SHEET_NAME = re.compile(r"[\[\]:*?/\\]")
MAX_CELL = 32767
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006"
_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


class Cell:
    """A value with a look (see LOOKS) and an optional Excel number format such as ``0.0%``."""

    __slots__ = ("value", "look", "fmt")

    def __init__(self, value, look="text", fmt=None):
        if look not in LOOKS:
            raise ValueError("unknown cell look")
        self.value, self.look, self.fmt = value, look, fmt


class Sheet:
    """Rows of cells. ``header_row`` (1-based) stays visible and filters the rows below it."""

    def __init__(self, name, rows=(), *, header_row=None, widths=None):
        self.name, self.rows, self.header_row, self.widths = name, [list(row) for row in rows], header_row, widths

    def append(self, row):
        self.rows.append(list(row))


def column_letter(index):
    """0 → A, 25 → Z, 26 → AA."""
    letters, index = "", index + 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def clean_text(value):
    return _CONTROL.sub("", str(value))[:MAX_CELL]


def sheet_names(names):
    """Valid, unique (case-insensitively) sheet names: no []:*?/\\, at most 31 characters, never empty."""
    result, seen = [], set()
    for index, name in enumerate(names, 1):
        base = _SHEET_NAME.sub("", clean_text(name)).strip().strip("'")[:31].strip() or f"表{index}"
        candidate, number = base, 2
        while candidate.casefold() in seen:
            suffix = f"({number})"
            candidate, number = base[:31 - len(suffix)] + suffix, number + 1
        seen.add(candidate.casefold())
        result.append(candidate)
    return result


def display_width(value):
    """Approximate column width of a value as shown: CJK characters count double."""
    text = value if isinstance(value, str) else (f"{value:,.2f}" if isinstance(value, float) else str(value))
    return sum(2 if ord(char) > 0x2E7F else 1 for char in text)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _attribute(value):
    return escape(value, {'"': "&quot;"})


class _Strings:
    def __init__(self):
        self.index, self.count = {}, 0

    def get(self, text):
        self.count += 1
        return self.index.setdefault(text, len(self.index))

    def xml(self):
        items = "".join(f'<si><t xml:space="preserve">{escape(text)}</t></si>' for text in self.index)
        return f'{_DECLARATION}<sst xmlns="{NS}" count="{self.count}" uniqueCount="{len(self.index)}">{items}</sst>'


class _Styles:
    """Cell formats (xf records) in order of first use; index 0 is the default."""

    def __init__(self):
        self.formats, self.records, self.lookup = {}, [(0, "text")], {(0, "text"): 0}

    def index(self, look, fmt):
        number = 0 if not fmt else _BUILTIN_FORMATS.get(fmt) or self.formats.setdefault(fmt, 164 + len(self.formats))
        key = (number, look)
        if key not in self.lookup:
            self.lookup[key] = len(self.records)
            self.records.append(key)
        return self.lookup[key]

    def xml(self):
        formats = "".join(f'<numFmt numFmtId="{number}" formatCode="{_attribute(code)}"/>'
                          for code, number in self.formats.items())
        fonts = ('<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
                 '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
                 '<font><b/><sz val="14"/><name val="Calibri"/><family val="2"/></font>'
                 '<font><sz val="10"/><color rgb="FF666666"/><name val="Calibri"/><family val="2"/></font>')
        fills = ('<fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
                 '<fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/><bgColor indexed="64"/>'
                 '</patternFill></fill>')
        records = []
        for number, look in self.records:
            font, fill, alignment = LOOKS[look]
            attributes = (f'numFmtId="{number}" fontId="{font}" fillId="{fill}" borderId="0" xfId="0"'
                          + (' applyNumberFormat="1"' if number else "") + (' applyFont="1"' if font else "")
                          + (' applyFill="1"' if fill else "") + (' applyAlignment="1"' if alignment else ""))
            inner = {"wrap": '<alignment wrapText="1" vertical="top"/>',
                     "center": '<alignment horizontal="center" vertical="center"/>'}.get(alignment)
            records.append(f"<xf {attributes}>{inner}</xf>" if inner else f"<xf {attributes}/>")
        return (f'{_DECLARATION}<styleSheet xmlns="{NS}">'
                + (f'<numFmts count="{len(self.formats)}">{formats}</numFmts>' if self.formats else "")
                + f'<fonts count="4">{fonts}</fonts><fills count="3">{fills}</fills>'
                '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                f'<cellXfs count="{len(records)}">{"".join(records)}</cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def _cell_xml(reference, value, strings, styles):
    look, fmt = "text", None
    if isinstance(value, Cell):
        value, look, fmt = value.value, value.look, value.fmt
    style = styles.index(look, fmt if _number(value) else None)
    attribute = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{reference}"{attribute}/>' if style else ""
    if _number(value):
        text = str(value) if isinstance(value, int) else repr(float(value))
        return f'<c r="{reference}"{attribute}><v>{text}</v></c>'
    return f'<c r="{reference}"{attribute} t="s"><v>{strings.get(clean_text(value))}</v></c>'


def _widths(sheet):
    if sheet.widths:
        return list(sheet.widths)
    widths = [8.0] * max((len(row) for row in sheet.rows), default=0)
    for row in sheet.rows:
        if len(row) == 1:
            continue  # a one-cell text row runs over the next columns
        for index, value in enumerate(row):
            raw = value.value if isinstance(value, Cell) else value
            if raw is not None:
                widths[index] = max(widths[index], min(60.0, display_width(raw) + 2.0))
    return widths


def _filter_range(sheet):
    width = max(len(sheet.rows[sheet.header_row - 1]), 1)
    return f"A{sheet.header_row}:{column_letter(width - 1)}{len(sheet.rows)}"


def _filtered(sheet):
    return bool(sheet.header_row) and len(sheet.rows) > sheet.header_row


def _sheet_xml(sheet, strings, styles, selected):
    rows = sheet.rows
    columns = max((len(row) for row in rows), default=1) or 1
    view = '<sheetView workbookViewId="0"' + (' tabSelected="1"' if selected else "") + ">"
    if sheet.header_row:
        top = f"A{sheet.header_row + 1}"
        view += (f'<pane ySplit="{sheet.header_row}" topLeftCell="{top}" activePane="bottomLeft" state="frozen"/>'
                 f'<selection pane="bottomLeft" activeCell="{top}" sqref="{top}"/>')
    view += "</sheetView>"
    cols = "".join(f'<col min="{index}" max="{index}" width="{width:.1f}" customWidth="1"/>'
                   for index, width in enumerate(_widths(sheet), 1))
    body = []
    for number, row in enumerate(rows, 1):
        cells = "".join(_cell_xml(f"{column_letter(index)}{number}", value, strings, styles)
                        for index, value in enumerate(row))
        body.append(f'<row r="{number}">{cells}</row>' if cells else f'<row r="{number}"/>')
    return (f'{_DECLARATION}<worksheet xmlns="{NS}" xmlns:r="{REL}">'
            f'<dimension ref="A1:{column_letter(columns - 1)}{max(len(rows), 1)}"/><sheetViews>{view}</sheetViews>'
            '<sheetFormatPr defaultRowHeight="16"/>' + (f"<cols>{cols}</cols>" if cols else "")
            + f"<sheetData>{''.join(body)}</sheetData>"
            + (f'<autoFilter ref="{_filter_range(sheet)}"/>' if _filtered(sheet) else "")
            + '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>')


def _absolute(reference):
    return ":".join(re.sub(r"^([A-Z]+)(\d+)$", r"$\1$\2", part) for part in reference.split(":"))


def _quoted(name):
    """A sheet name as formulas write it: 'name' with inner quotes doubled."""
    return "'" + name.replace("'", "''") + "'"


def workbook(sheets, *, title="", created=None):
    """The .xlsx file of these sheets as bytes; the same sheets and time give the same bytes."""
    if not sheets:
        raise ValueError("a workbook needs at least one sheet")
    created = (created or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    names = sheet_names([sheet.name for sheet in sheets])
    strings, styles = _Strings(), _Styles()
    parts = {f"xl/worksheets/sheet{index}.xml": _sheet_xml(sheet, strings, styles, index == 1)
             for index, sheet in enumerate(sheets, 1)}
    count = len(sheets)
    entries = "".join(f'<sheet name="{_attribute(name)}" sheetId="{index}" r:id="rId{index}"/>'
                      for index, name in enumerate(names, 1))
    defined = "".join(
        f'<definedName name="_xlnm._FilterDatabase" localSheetId="{index}" hidden="1">'
        f"{escape(_quoted(names[index]))}!{_absolute(_filter_range(sheet))}</definedName>"
        for index, sheet in enumerate(sheets) if _filtered(sheet))
    book = (f'{_DECLARATION}<workbook xmlns="{NS}" xmlns:r="{REL}"><bookViews><workbookView activeTab="0"/>'
            f"</bookViews><sheets>{entries}</sheets>" + (f"<definedNames>{defined}</definedNames>" if defined else "")
            + "</workbook>")
    relations = "".join(f'<Relationship Id="rId{index}" Type="{REL}/worksheet" Target="worksheets/sheet{index}.xml"/>'
                        for index in range(1, count + 1))
    relations += (f'<Relationship Id="rId{count + 1}" Type="{REL}/styles" Target="styles.xml"/>'
                  f'<Relationship Id="rId{count + 2}" Type="{REL}/sharedStrings" Target="sharedStrings.xml"/>')
    office = "application/vnd.openxmlformats-officedocument"
    overrides = "".join(f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
                        f'ContentType="{office}.spreadsheetml.worksheet+xml"/>' for index in range(1, count + 1))
    content_types = (
        f'{_DECLARATION}<Types xmlns="{PACKAGE}/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{office}.spreadsheetml.sheet.main+xml"/>{overrides}'
        f'<Override PartName="/xl/styles.xml" ContentType="{office}.spreadsheetml.styles+xml"/>'
        f'<Override PartName="/xl/sharedStrings.xml" ContentType="{office}.spreadsheetml.sharedStrings+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        f'<Override PartName="/docProps/app.xml" ContentType="{office}.extended-properties+xml"/></Types>')
    root = (f'{_DECLARATION}<Relationships xmlns="{PACKAGE}/relationships">'
            f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/>'
            f'<Relationship Id="rId2" Type="{PACKAGE}/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            f'<Relationship Id="rId3" Type="{REL}/extended-properties" Target="docProps/app.xml"/></Relationships>')
    stamp = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    core = (f'{_DECLARATION}<cp:coreProperties xmlns:cp="{PACKAGE}/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{escape(clean_text(title)[:200])}</dc:title><dc:creator>JX Alert System</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified></cp:coreProperties>')
    app = (f'{_DECLARATION}<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
           'extended-properties"><Application>JX Alert System</Application></Properties>')
    files = {"[Content_Types].xml": content_types, "_rels/.rels": root, "docProps/core.xml": core,
             "docProps/app.xml": app, "xl/workbook.xml": book,
             "xl/_rels/workbook.xml.rels": f'{_DECLARATION}<Relationships xmlns="{PACKAGE}/relationships">'
                                           f"{relations}</Relationships>",
             **parts, "xl/styles.xml": styles.xml(), "xl/sharedStrings.xml": strings.xml()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            info = zipfile.ZipInfo(name, date_time=created.timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text.encode("utf-8"))
    return buffer.getvalue()
