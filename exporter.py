"""
exporter.py — Generate downloadable CSV and XLSX from scrape results.
"""

import io
import csv
from datetime import datetime

import openpyxl
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side, GradientFill
)
from openpyxl.utils import get_column_letter

from scraper import ScrapedResult, ErrorResult


RESULT_COLUMNS = ["#", "URL", "Vendor", "Product", "Edition", "Version", "Licence Metric", "EOS", "EOL"]
ERROR_COLUMNS  = ["#", "URL", "Error Reason"]


# ---------------------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------------------

def to_csv(results: list[ScrapedResult], errors: list[ErrorResult]) -> bytes:
    """Build a CSV with results and errors in two sections."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    # Results section — headers on first row
    writer.writerow(RESULT_COLUMNS)
    for i, r in enumerate(results, 1):
        writer.writerow([i, r.url, r.vendor, r.product, r.edition, r.version, r.licence_metric, r.eos, r.eol])

    writer.writerow([])

    # Errors section
    writer.writerow(ERROR_COLUMNS)
    for i, e in enumerate(errors, 1):
        writer.writerow([i, e.url, e.error])

    return buf.getvalue().encode("utf-8-sig")  # BOM for Excel compatibility


# ---------------------------------------------------------------------------
# XLSX Export — styled workbook
# ---------------------------------------------------------------------------

def _make_border(color="D1D5DB"):
    side = Side(style="thin", color=color)
    return Border(left=side, right=side, top=side, bottom=side)


def _style_header_row(ws, row_num: int, num_cols: int, bg_color: str, font_color: str = "FFFFFF"):
    """Apply header styling to a row."""
    for col in range(1, num_cols + 1):
        cell = ws.cell(row=row_num, column=col)
        cell.font = Font(bold=True, color=font_color, size=11, name="Calibri")
        cell.fill = PatternFill("solid", fgColor=bg_color)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _make_border("9CA3AF")


def _style_data_row(ws, row_num: int, num_cols: int, alternate: bool):
    bg = "F9FAFB" if alternate else "FFFFFF"
    for col in range(1, num_cols + 1):
        cell = ws.cell(row=row_num, column=col)
        cell.fill = PatternFill("solid", fgColor=bg)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = _make_border("E5E7EB")
        cell.font = Font(size=10, name="Calibri")


def _auto_fit_columns(ws, min_width=12, max_width=60):
    for col_cells in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col_cells[0].column)
        for cell in col_cells:
            try:
                val_len = len(str(cell.value or ""))
                if val_len > max_len:
                    max_len = val_len
            except Exception:
                pass
        adjusted = max(min_width, min(max_width, max_len + 4))
        ws.column_dimensions[col_letter].width = adjusted


def to_xlsx(results: list[ScrapedResult], errors: list[ErrorResult]) -> bytes:
    """Build a styled XLSX with two sheets: Results and Errors."""
    wb = openpyxl.Workbook()

    # -----------------------------------------------------------------------
    # Sheet 1: Results
    # -----------------------------------------------------------------------
    ws_results = wb.active
    ws_results.title = "Scrape Results"
    ws_results.sheet_view.showGridLines = False
    ws_results.row_dimensions[1].height = 30

    # Header row (row 1)
    for col_idx, col_name in enumerate(RESULT_COLUMNS, 1):
        ws_results.cell(row=1, column=col_idx, value=col_name)
    _style_header_row(ws_results, 1, len(RESULT_COLUMNS), "4F46E5")

    # Data rows (starting at row 2)
    for i, r in enumerate(results, 1):
        row_num = i + 1
        ws_results.row_dimensions[row_num].height = 20
        data = [i, r.url, r.vendor, r.product, r.edition, r.version, r.licence_metric, r.eos, r.eol]
        for col_idx, value in enumerate(data, 1):
            ws_results.cell(row=row_num, column=col_idx, value=value)
        _style_data_row(ws_results, row_num, len(RESULT_COLUMNS), alternate=(i % 2 == 0))
        # URL column — blue hyperlink style
        url_cell = ws_results.cell(row=row_num, column=2)
        url_cell.font = Font(color="4F46E5", size=10, name="Calibri", underline="single")

    _auto_fit_columns(ws_results)
    # Freeze header row
    ws_results.freeze_panes = "A2"

    # -----------------------------------------------------------------------
    # Sheet 2: Errors
    # -----------------------------------------------------------------------
    ws_errors = wb.create_sheet(title="Error URLs")
    ws_errors.sheet_view.showGridLines = False
    ws_errors.row_dimensions[1].height = 30

    # Header row (row 1)
    for col_idx, col_name in enumerate(ERROR_COLUMNS, 1):
        ws_errors.cell(row=1, column=col_idx, value=col_name)
    _style_header_row(ws_errors, 1, len(ERROR_COLUMNS), "DC2626")

    # Data (starting at row 2)
    for i, e in enumerate(errors, 1):
        row_num = i + 1
        ws_errors.row_dimensions[row_num].height = 20
        data = [i, e.url, e.error]
        for col_idx, value in enumerate(data, 1):
            ws_errors.cell(row=row_num, column=col_idx, value=value)
        _style_data_row(ws_errors, row_num, len(ERROR_COLUMNS), alternate=(i % 2 == 0))
        url_cell = ws_errors.cell(row=row_num, column=2)
        url_cell.font = Font(color="DC2626", size=10, name="Calibri")
        err_cell = ws_errors.cell(row=row_num, column=3)
        err_cell.font = Font(color="7F1D1D", size=10, name="Calibri")

    _auto_fit_columns(ws_errors)
    ws_errors.freeze_panes = "A2"

    # Serialize to bytes
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Augmented XLSX Export (original data + _webscraped columns)
# ---------------------------------------------------------------------------

def to_xlsx_augmented(df) -> bytes:
    """
    Build a styled XLSX from an augmented DataFrame.
    Original columns get a standard header; _webscraped columns get a green header.
    """
    import pandas as pd

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Augmented Results"
    ws.sheet_view.showGridLines = False

    columns = list(df.columns)
    num_cols = len(columns)

    # Header row (row 1)
    ws.row_dimensions[1].height = 30
    for col_idx, col_name in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        is_webscraped = col_name.endswith("_webscraped")
        bg = "059669" if is_webscraped else "4F46E5"
        cell.font = Font(bold=True, color="FFFFFF", size=11, name="Calibri")
        cell.fill = PatternFill("solid", fgColor=bg)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _make_border("9CA3AF")

    # Data rows (starting at row 2)
    for row_idx, (_, row) in enumerate(df.iterrows(), start=1):
        excel_row = row_idx + 1
        ws.row_dimensions[excel_row].height = 20
        for col_idx, col_name in enumerate(columns, 1):
            value = row.get(col_name, "")
            if pd.isna(value):
                value = ""
            cell = ws.cell(row=excel_row, column=col_idx, value=str(value))

            is_webscraped = col_name.endswith("_webscraped")
            alt = row_idx % 2 == 0
            if is_webscraped:
                bg = "ECFDF5" if alt else "F0FDF4"
            else:
                bg = "F9FAFB" if alt else "FFFFFF"

            cell.fill = PatternFill("solid", fgColor=bg)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cell.border = _make_border("E5E7EB")
            cell.font = Font(size=10, name="Calibri")

            # Highlight error cells in red
            if is_webscraped and str(value).startswith("ERROR:"):
                cell.font = Font(size=10, name="Calibri", color="DC2626")

    _auto_fit_columns(ws)
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()

