#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
belka.core.xlsx — поиск листа/колонки в xlsx-шаблонах и кодек заголовков.

build_header/clean_header — пара взаимно-обратных функций для заголовка
колонки вида '<имя> * (справочник)':
  build_header(...) — использует belka.stages.generate при СБОРКЕ файла;
  clean_header(...) — используют belka.stages.import_check/import_attributes при РАЗБОРЕ.
В исходных скриптах это были две независимые копии в разных файлах —
здесь одна пара в одном месте, так формат заголовка физически не может
разъехаться между генерацией и импортом.
"""
from __future__ import annotations

import re


def find_header_row_and_col(ws, hints, max_scan_rows: int = 5):
    """Ищет строку-заголовок и индекс колонки, где встречается любая из
    подстрок hints (например 'id товара'). Возвращает (row_idx, col_idx)
    или (None, None), если не нашлось."""
    for row_idx in range(1, max_scan_rows + 1):
        row = ws[row_idx]
        for col_idx, cell in enumerate(row, start=1):
            value = str(cell.value or "").strip().lower()
            for hint in hints:
                if hint in value:
                    return row_idx, col_idx
    return None, None


def find_products_sheet(
    wb,
    sheet_name_candidates=("Products", "products", "Товары"),
    header_hints=("id товара",),
    max_scan_rows: int = 5,
):
    """Возвращает лист с товарами: сперва по точному имени из
    sheet_name_candidates, иначе — первый лист, где нашлась колонка,
    подходящая под header_hints. Если ничего не подошло — первый лист книги."""
    for candidate in sheet_name_candidates:
        if candidate in wb.sheetnames:
            return wb[candidate]
    for ws in wb.worksheets:
        _, col_idx = find_header_row_and_col(ws, header_hints, max_scan_rows=max_scan_rows)
        if col_idx:
            return ws
    return wb.worksheets[0]


def build_header(name: str, is_required: bool = False, has_directory: bool = False) -> str:
    """'<имя>' -> '<имя> * (справочник)' по правилам заголовков Белки."""
    h = name
    if is_required:
        h += " *"
    if has_directory:
        h += " (справочник)"
    return h


def clean_header(header):
    """Обратная операция к build_header: заголовок -> (name, is_required, is_directory)."""
    if header is None:
        return None, False, False
    h = str(header)
    is_required = "*" in h
    is_directory = "справочник" in h.lower()
    name = h.replace("*", "")
    name = re.sub(r"\(.*?справочник.*?\)", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    return name, is_required, is_directory
