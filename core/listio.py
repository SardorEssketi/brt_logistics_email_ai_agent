"""Reading driver lists and writing NOTES back: Excel (.xlsx/.xlsm), CSV, Google Sheets, and a mock for tests.

Every backend exposes:
    tabs() -> [str]
    read(tab) -> grid (list of rows, each a list of strings; row 1 = grid[0])
    write_cells(tab, {(row, col): value})      # 1-based
"""
import codecs
import csv
import io
import os
import re
import tempfile
import threading
from copy import copy
from datetime import date, datetime
from pathlib import Path

from . import config


def to_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, datetime):
        return v.strftime("%m/%d/%Y") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.strftime("%m/%d/%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%m/%d/%Y")
    return str(v)


def _atomic_write(path: Path, writer) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=path.suffix)
    os.close(fd)
    try:
        writer(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------- Excel
class XlsxSource:
    def __init__(self, path):
        self.path = Path(path)

    def _load(self, read_only: bool):
        from openpyxl import load_workbook
        keep_vba = self.path.suffix.lower() == ".xlsm"
        if read_only:
            return load_workbook(self.path, read_only=True, data_only=True)
        return load_workbook(self.path, keep_vba=keep_vba)

    def tabs(self):
        wb = self._load(True)
        try:
            return list(wb.sheetnames)
        finally:
            wb.close()

    def read(self, tab=None):
        wb = self._load(True)
        try:
            ws = wb[tab] if tab else wb.worksheets[0]
            grid = [[to_str(v) for v in row] for row in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
        while grid and not any(c.strip() for c in grid[-1]):
            grid.pop()
        return grid

    def write_cells(self, tab, updates: dict, header_style_from=None):
        if not updates:
            return
        wb = self._load(False)
        ws = wb[tab] if tab else wb.worksheets[0]
        for (r, c), val in updates.items():
            cell = ws.cell(row=r, column=c)
            cell.value = val
            if header_style_from and (r, c) == header_style_from[0]:
                src = ws.cell(row=r, column=header_style_from[1])
                if src.has_style:
                    cell.font, cell.fill = copy(src.font), copy(src.fill)
                    cell.border, cell.alignment = copy(src.border), copy(src.alignment)
        _atomic_write(self.path, wb.save)
        wb.close()


# ---------------------------------------------------------------- CSV
class CsvSource:
    def __init__(self, path):
        self.path = Path(path)
        self.encoding = "utf-8-sig"
        self.delimiter = ","

    def _read_rows(self):
        raw = self.path.read_bytes()
        encs = ("utf-8-sig",) if raw.startswith(codecs.BOM_UTF8) else ("utf-8", "cp1252", "latin-1")
        for enc in encs:
            try:
                text = raw.decode(enc)
                self.encoding = enc
                break
            except UnicodeDecodeError:
                continue
        sample = text[:5000]
        try:
            self.delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            self.delimiter = ","
        return [list(r) for r in csv.reader(io.StringIO(text, newline=""), delimiter=self.delimiter)]

    def tabs(self):
        return ["CSV"]

    def read(self, tab=None):
        grid = self._read_rows()
        while grid and not any(c.strip() for c in grid[-1]):
            grid.pop()
        return grid

    def write_cells(self, tab, updates: dict, header_style_from=None):
        if not updates:
            return
        grid = self._read_rows()
        for (r, c), val in updates.items():
            while len(grid) < r:
                grid.append([])
            row = grid[r - 1]
            while len(row) < c:
                row.append("")
            row[c - 1] = val

        def w(tmp):
            with open(tmp, "w", encoding=self.encoding, newline="") as f:
                csv.writer(f, delimiter=self.delimiter).writerows(grid)
        _atomic_write(self.path, w)


# ---------------------------------------------------------------- Google Sheets
_gs_lock = threading.Lock()
_gs_client = None


def sheets_client():
    global _gs_client
    with _gs_lock:
        if _gs_client is None:
            import gspread
            sa = config.google_sa_file()
            if not sa or not Path(sa).is_file():
                raise RuntimeError("Google Sheets is not set up yet: the service-account file is missing (see README, part 4).")
            _gs_client = gspread.service_account(filename=sa)
        return _gs_client


def service_account_email():
    import json
    sa = config.google_sa_file()
    try:
        return json.loads(Path(sa).read_text())["client_email"]
    except Exception:
        return None


SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


class GSheetSource:
    def __init__(self, url, client=None):
        if not SHEET_ID_RE.search(url or ""):
            raise ValueError("That doesn't look like a Google Sheets link. Copy the link from the browser address bar.")
        self.url = url
        self._client = client
        self._sh = None

    def _sheet(self):
        if self._sh is None:
            self._sh = (self._client or sheets_client()).open_by_url(self.url)
        return self._sh

    def _ws(self, tab):
        sh = self._sheet()
        return sh.worksheet(tab) if tab else sh.get_worksheet(0)

    def tabs(self):
        return [ws.title for ws in self._sheet().worksheets()]

    def read(self, tab=None):
        grid = self._ws(tab).get_all_values()
        while grid and not any(c.strip() for c in grid[-1]):
            grid.pop()
        return grid

    def write_cells(self, tab, updates: dict, header_style_from=None):
        if not updates:
            return
        from gspread.utils import rowcol_to_a1
        ws = self._ws(tab)
        max_c = max(c for _, c in updates)
        max_r = max(r for r, _ in updates)
        if max_c > ws.col_count:
            ws.add_cols(max_c - ws.col_count)
        if max_r > ws.row_count:
            ws.add_rows(max_r - ws.row_count)
        data = [{"range": rowcol_to_a1(r, c), "values": [[v]]} for (r, c), v in sorted(updates.items())]
        ws.batch_update(data, value_input_option="RAW")


# ---------------------------------------------------------------- Mock (tests / Sheets simulation)
class MockSheetSource:
    """In-memory stand-in for a Google Sheet. `fail_writes` = number of upcoming writes that raise."""
    registry = {}

    def __init__(self, url, tabs=None):
        self.url = url
        if tabs is not None:
            MockSheetSource.registry[url] = tabs
        self.fail_writes = 0

    @property
    def data(self):
        return MockSheetSource.registry[self.url]

    def tabs(self):
        return list(self.data.keys())

    def read(self, tab=None):
        tab = tab or self.tabs()[0]
        return [list(r) for r in self.data[tab]]

    def write_cells(self, tab, updates, header_style_from=None):
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise RuntimeError("APIError: [429] Quota exceeded (simulated)")
        tab = tab or self.tabs()[0]
        grid = self.data[tab]
        for (r, c), v in updates.items():
            while len(grid) < r:
                grid.append([])
            while len(grid[r - 1]) < c:
                grid[r - 1].append("")
            grid[r - 1][c - 1] = v


def open_source(source_type: str, ref: str):
    if source_type == "xlsx":
        return XlsxSource(ref)
    if source_type == "csv":
        return CsvSource(ref)
    if source_type == "gsheet":
        if ref.startswith("mock://"):
            return MockSheetSource(ref)
        return GSheetSource(ref)
    raise ValueError(f"Unknown source type {source_type}")


def source_type_for_filename(name: str):
    ext = Path(name).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return "xlsx"
    if ext == ".csv":
        return "csv"
    return None
