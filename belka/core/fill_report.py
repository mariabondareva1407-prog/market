#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
belka.core.fill_report — «сначала покажи, потом подставляй».

09.2026, требование пользователя: перед подстановкой ЛЮБОЙ заглушки
пайплайн должен показать ОБЩИЙ список товаров с незаполненными
обязательными полями и список заглушек, которые собирается применить, и
спросить — подставлять ли. Раньше подстановка шла молча, по одному товару,
прямо внутри filler'ов: `get_safe_value(..., 1)`, «Описание уточняется»,
`0` в габаритах, штрихкод из id.

Поэтому генерация разделена на две фазы:
  1. Генератор собирает строки КАК ЕСТЬ — нет данных, значит ячейка пустая.
  2. `apply_placeholders` считает пропуски ПО ВСЕМ категориям сразу (отчёт
     общий, вопрос один на прогон, а не по файлу на категорию), печатает
     сводку + CSV и, при согласии, подставляет заглушки из общего реестра
     belka/core/defaults.py с учётом переопределений склада.

Дефолты из attribute_mapping.json сюда НЕ входят — по решению пользователя
это отдельный механизм (ручной маппинг атрибутов, а не заглушка на
пустоту); их количество генератор добавляет в сводку сам.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from .columns import build_barcode
from .csvio import write_csv_report
from .defaults import PLACEHOLDERS, is_overridden
from .xlsx import clean_header

ID_COLUMN_HINT = "id товара"
PRINT_LIMIT = 20


def _is_empty(value) -> bool:
    return value is None or value == "" or (isinstance(value, str) and not value.strip())


def _plan_block(ctx, columns: list) -> tuple[list, dict]:
    """Для одного набора колонок: очищенные имена + план подстановки
    {col_idx: (ключ, значение, Placeholder, переопределено)} для обязательных
    колонок, у которых заглушка вообще есть."""
    clean_names = [clean_header(c["header"])[0] for c in columns]
    plan = {}
    for col_idx, col in enumerate(columns):
        if not col.get("is_required"):
            continue
        key, value = ctx.placeholder_for_column(clean_names[col_idx])
        if key is None:
            continue
        ph = PLACEHOLDERS[key]
        overridden = is_overridden(key, ctx.config.placeholders)
        if value is None and not (ph.computed and not overridden):
            continue  # рецепт склада явно says «не подставлять»
        plan[col_idx] = (key, value, ph, overridden)
    return clean_names, plan


def apply_placeholders(ctx, blocks: list, out_dir, report_name: str) -> dict:
    """blocks — список (columns, rows) по одному на будущий файл-категорию.
    Строки меняются на месте. Возвращает сводку для StageResult."""
    cfg = ctx.config
    out_dir = Path(out_dir)

    missing_by_column = Counter()
    missing_by_product = defaultdict(set)
    fillable_columns, unfillable_columns = {}, set()
    total_rows = 0
    plans = []

    for columns, rows in blocks:
        clean_names, plan = _plan_block(ctx, columns)
        plans.append((clean_names, plan))
        id_idx = next((i for i, n in enumerate(clean_names) if ID_COLUMN_HINT in str(n).lower()), None)
        total_rows += len(rows)
        for col_idx, col in enumerate(columns):
            if not col.get("is_required"):
                continue
            name = clean_names[col_idx]
            for row in rows:
                if _is_empty(row[col_idx]):
                    missing_by_column[name] += 1
                    missing_by_product[str(row[id_idx]) if id_idx is not None else "?"].add(name)
            if col_idx in plan:
                fillable_columns[name] = plan[col_idx]
            elif missing_by_column.get(name):
                unfillable_columns.add(name)

    total_missing = sum(missing_by_column.values())
    summary = {
        "rows": total_rows,
        "rows_with_missing": len(missing_by_product),
        "missing_required_cells": total_missing,
        "placeholders_applied": {},
        "placeholders_declined": False,
        "missing_report": None,
    }
    if not total_missing:
        print("\n  [i] Пустых обязательных ячеек нет — подставлять нечего.")
        return summary

    print(f"\n{'=' * 70}\nОБЯЗАТЕЛЬНЫЕ ПОЛЯ БЕЗ ДАННЫХ: {total_missing} ячеек "
          f"у {len(missing_by_product)} товаров из {total_rows}\n{'=' * 70}")
    for name, count in missing_by_column.most_common():
        entry = fillable_columns.get(name)
        if entry is None:
            print(f"  {name}: {count} — заглушки нет, останется пусто")
            continue
        _, value, ph, overridden = entry
        shown = (f"префикс {cfg.barcode_prefix!r} + Id, 13 символов"
                 if ph.computed and not overridden else repr(value))
        print(f"  {name}: {count} -> заглушка {shown}")

    print(f"\n  Товары ({min(len(missing_by_product), PRINT_LIMIT)} из {len(missing_by_product)}):")
    for pid, cols in list(sorted(missing_by_product.items()))[:PRINT_LIMIT]:
        print(f"    Id {pid}: {', '.join(sorted(cols))}")
    if len(missing_by_product) > PRINT_LIMIT:
        print(f"    ... и ещё {len(missing_by_product) - PRINT_LIMIT}, полный список в CSV")

    report_path = out_dir / report_name
    write_csv_report(
        report_path, ["Id Товара", "Незаполненные обязательные поля", "Что будет подставлено"],
        [[pid,
          ", ".join(sorted(cols)),
          ", ".join(sorted(("— " + c) if c in unfillable_columns else ("заглушка: " + c) for c in cols))]
         for pid, cols in sorted(missing_by_product.items())],
    )
    summary["missing_report"] = str(report_path)
    print(f"\n  Полный список: {report_path}")

    print("\n  Настроенные заглушки:")
    for line in ctx.placeholders_report():
        print(f"    - {line}")

    if not fillable_columns:
        print("\n  [i] Ни для одной пустой колонки заглушки нет — файлы записываются как есть.")
        return summary

    if not ctx.confirm("\nПодставить заглушки в перечисленные ячейки?"):
        print("  Заглушки НЕ подставлены — ячейки останутся пустыми (import_check это увидит).")
        summary["placeholders_declined"] = True
        return summary

    applied = Counter()
    for (columns, rows), (clean_names, plan) in zip(blocks, plans):
        id_idx = next((i for i, n in enumerate(clean_names) if ID_COLUMN_HINT in str(n).lower()), None)
        for col_idx, (key, value, ph, overridden) in plan.items():
            for row in rows:
                if not _is_empty(row[col_idx]):
                    continue
                if ph.computed and not overridden:
                    new_value = build_barcode(cfg.barcode_prefix, row[id_idx] if id_idx is not None else "")
                    if not new_value:
                        continue
                else:
                    new_value = value
                row[col_idx] = new_value
                applied[clean_names[col_idx]] += 1

    summary["placeholders_applied"] = dict(applied)
    print(f"  ✔ Подставлено ячеек: {sum(applied.values())} {dict(applied)}")
    return summary
