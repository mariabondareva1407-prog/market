#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 'import_check' — read-only сверка xlsx-шаблонов (обычно — вывод
'generate') с текущим состоянием сайта: какие артикулы пропали, какие
задублированы (внутри шаблонов и на сайте), какие обязательные ячейки
не заполнены. Ничего не патчит, xlsx не переписывает — только консоль
и CSV-отчёты в <папка>/report/.

Раньше это была "ФАЗА 1" внутри единого belka_import.py / ImportStage.
Смысл разбиения на отдельный stage: это единственная часть бывшего
import'а, которая ничего не меняет ни на сайте, ни на диске — её безопасно
гонять когда угодно (например, каждый день из cron) отдельно от
import_attributes, который пишет.
"""
from __future__ import annotations

import time
from pathlib import Path
from collections import Counter, defaultdict

from ..core.csvio import write_csv_report
from ..core.http import SessionBlockedError
from ..pipeline import Stage, StageResult
from .import_shared import scan_file, REPORT_SUBDIR

PAGE_LIMIT = 100
PRODUCTS_SEARCH_URL_PART = "/api/v1/catalog/products/drafts:search"


class ImportCheckStage(Stage):
    name = "import_check"
    # если сессия заблокирована каскадом 403 — следующие stage'и
    # (import_attributes/upload) с тем же токеном провалятся точно так же,
    # поэтому пайплайн останавливается (поведение как в исходном едином
    # import: "Фаза 2 пропущена из-за остановки в Фазе 1").
    stop_pipeline_on_failure = True

    def __init__(self, folder=None):
        self.folder_override = Path(folder) if folder else None

    # ------------------------------------------------------------ API calls --

    @staticmethod
    def _api_fetch_active_products(session, api_base, page_limit=PAGE_LIMIT):
        url = f"{api_base}{PRODUCTS_SEARCH_URL_PART}"
        products_by_vendor_code = {}
        vendor_code_all = defaultdict(list)
        offset = 0
        partial = False
        while True:
            body = {
                "include": [], "sort": ["-id"],
                "filter": {"status_id": [], "allow_publish": "1"},
                "pagination": {"type": "offset", "limit": page_limit, "offset": offset},
            }
            try:
                resp = session.post(url, json=body, timeout=30)
            except SessionBlockedError as e:
                print(f"\n⛔ ОСТАНОВКА выгрузки товаров: {e}")
                print(f"Успел собрать {len(products_by_vendor_code)} активных товаров до остановки — "
                      "отчёт о пропавших и об уникальности будет ЧАСТИЧНЫМ.")
                partial = True
                break
            data = resp.json().get("data", [])
            if not data:
                break
            for item in data:
                vendor_code = str(item.get("vendor_code") or "").strip()
                if not vendor_code:
                    continue
                entry = {"id": item.get("id"), "name": item.get("name"), "vendor_code": vendor_code}
                vendor_code_all[vendor_code].append(entry)
                products_by_vendor_code.setdefault(vendor_code, entry)
            print(f"  [i] активных товаров с сайта: {sum(len(v) for v in vendor_code_all.values())} (offset={offset})")
            if len(data) < page_limit:
                break
            offset += page_limit
        return products_by_vendor_code, vendor_code_all, partial

    # -------------------------------------------------------------- reports --

    @staticmethod
    def _build_uniqueness_report(artikul_to_templates, vendor_code_all, out_dir: Path, partial: bool) -> dict:
        template_dupes = {art: counts for art, counts in artikul_to_templates.items() if sum(counts.values()) > 1}
        site_dupes = {vc: entries for vc, entries in vendor_code_all.items() if len(entries) > 1}

        print("\n=== Проверка уникальности артикулов ===")
        print(f"[i] Дублей внутри шаблонов: {len(template_dupes)}")
        for art, counts in list(template_dupes.items())[:20]:
            where = ", ".join(f"{n} ({c})" if c > 1 else n for n, c in counts.items())
            print(f"  ⚠ '{art}' встречается {sum(counts.values())} раз(а): {where}")
        if len(template_dupes) > 20:
            print(f"  ... и ещё {len(template_dupes) - 20}, полный список в CSV")

        print(f"\n[i] Дублей на самом сайте (по данным API){' (частично, выгрузка была прервана)' if partial else ''}: {len(site_dupes)}")
        for vc, entries in list(site_dupes.items())[:20]:
            ids = ", ".join(str(e["id"]) for e in entries)
            print(f"  ⚠ '{vc}' — {len(entries)} товара(ов) с этим артикулом на сайте, id: {ids}")
        if len(site_dupes) > 20:
            print(f"  ... и ещё {len(site_dupes) - 20}, полный список в CSV")

        if template_dupes or site_dupes:
            rows = []
            for art, counts in template_dupes.items():
                details = ", ".join(f"{n} ({c})" if c > 1 else n for n, c in counts.items())
                rows.append([art, "шаблон", sum(counts.values()), details])
            for vc, entries in site_dupes.items():
                details = ", ".join(f"id={e['id']}" for e in entries)
                rows.append([vc, "сайт (API)", len(entries), details])
            dupes_csv = out_dir / "duplicate_articles.csv"
            write_csv_report(dupes_csv, ["Артикул", "Источник дубля", "Кол-во", "Детали"], rows)
            print(f"\n[✔] Полный список дублей сохранён в {dupes_csv}")
        else:
            print("\n[✔] Дублей нет — CSV не создавался.")

        return {"template_dupes": len(template_dupes), "site_dupes": len(site_dupes)}

    @staticmethod
    def _build_missing_products_report(artikul_to_templates, per_template_count, active_products,
                                        out_dir: Path, partial: bool) -> int:
        all_template_artikuls = set(artikul_to_templates.keys())
        active_vendor_codes = set(active_products.keys())
        missing_artikuls = sorted(all_template_artikuls - active_vendor_codes)
        orphan_vendor_codes = sorted(active_vendor_codes - all_template_artikuls)

        print(f"\n=== Пропавшие / посторонние товары ===")
        print(f"[i] Шаблонов обработано: {len(per_template_count)}")
        print(f"[i] Всего строк с артикулами в шаблонах: {sum(per_template_count.values())}")
        print(f"[i] Уникальных артикулов в шаблонах: {len(all_template_artikuls)}")
        print(f"[i] Активных товаров получено из API: {len(active_vendor_codes)}" + (" (НЕПОЛНЫЙ список!)" if partial else ""))
        print(f"[i] Пропавших товаров (уникальных артикулов): {len(missing_artikuls)}")
        print(f"[i] Товаров, не относящихся ни к одному шаблону: {len(orphan_vendor_codes)}")

        out_dir.mkdir(parents=True, exist_ok=True)

        missing_rows = []
        for art in missing_artikuls:
            template_counts = artikul_to_templates[art]
            templates_str = ", ".join(f"{n} ({c})" if c > 1 else n for n, c in template_counts.items())
            missing_rows.append([art, templates_str, sum(template_counts.values())])
        write_csv_report(out_dir / "missing_products.csv", ["Артикул (Id Товара)", "Шаблоны", "Кол-во вхождений"], missing_rows)

        orphan_rows = [[active_products[vc]["id"], active_products[vc]["name"], vc] for vc in orphan_vendor_codes]
        write_csv_report(out_dir / "orphan_products.csv", ["ID товара", "Название", "Артикул (vendor_code)"], orphan_rows)

        print(f"[✔] CSV-отчёты сохранены в {out_dir}")
        return len(missing_artikuls)

    # -------------------------------------------------------------------- run --

    def run(self, ctx) -> StageResult:
        cfg = ctx.config
        session = ctx.admin_session()

        folder = self.folder_override or ctx.resolved_templates_dir()
        if not folder.is_dir():
            raise RuntimeError(f"Папка не найдена: {folder}")
        files = sorted(folder.glob("*.xlsx"))
        if not files:
            raise RuntimeError(f"В папке {folder} нет .xlsx файлов.")

        print(f"[import_check] файлов к сканированию: {len(files)}")
        t0 = time.monotonic()
        artikul_to_templates = defaultdict(lambda: defaultdict(int))
        per_template_article_count = {}
        all_missing_required = Counter()
        total_products = 0
        total_zero_quantity = 0

        for i, path in enumerate(files, 1):
            state = scan_file(path)
            file_missing = sum(c for (fname, _h), c in state["missing_required"].items() if fname == path.name)
            print(f"  [{i}/{len(files)}] {path.name}: товаров={state['product_count']}, "
                  f"пустых обязательных ячеек={file_missing}, нулевых 'Количество Товара'={state['quantity_fixed_rows']}")
            total_products += state["product_count"]
            total_zero_quantity += state["quantity_fixed_rows"]
            all_missing_required.update(state["missing_required"])
            per_template_article_count[path.name] = len(state["articles"])
            for art in state["articles"]:
                artikul_to_templates[art][path.name] += 1
        print(f"[import_check] сканирование заняло {time.monotonic() - t0:.1f}с, товаров всего: {total_products}")
        if total_zero_quantity:
            # раньше это молча чинил stage 'import_capitalize' (0 -> 10), он
            # убран (09.2026) — теперь такие строки просто видны здесь как
            # предупреждение и едут на upload с нулевым остатком как есть
            print(f"[⚠] строк с нулевым 'Количество Товара': {total_zero_quantity} — никто их больше не правит, "
                  f"уйдут на upload как есть")

        print("\n=== Незаполненные обязательные поля (со *) ===")
        if all_missing_required:
            by_file = defaultdict(list)
            for (fname, header), count in all_missing_required.items():
                by_file[fname].append((header, count))
            for fname, items in by_file.items():
                print(f"  Файл '{fname}':")
                for header, count in sorted(items, key=lambda x: -x[1]):
                    print(f"    - {header}: не заполнено в {count} строках")
        else:
            print("  Всё заполнено.")

        blocked = False
        missing_count = None
        dupes_stats = {"template_dupes": 0, "site_dupes": 0}
        try:
            print("\nВыгружаю активные товары из API...")
            t1 = time.monotonic()
            active_products, vendor_code_all, partial = self._api_fetch_active_products(session, cfg.admin_api_base, PAGE_LIMIT)
            print(f"[i] выгрузка заняла {time.monotonic() - t1:.1f}с")
            report_dir = folder / REPORT_SUBDIR
            missing_count = self._build_missing_products_report(
                artikul_to_templates, per_template_article_count, active_products, report_dir, partial
            )
            dupes_stats = self._build_uniqueness_report(artikul_to_templates, vendor_code_all, report_dir, partial)
        except SessionBlockedError as e:
            blocked = True
            print(f"\n⛔ ОСТАНОВКА: {e}")
            print("Проверка пропавших товаров и уникальности прервана — обновите токен и перезапустите.")

        summary = {
            "files": len(files),
            "products": total_products,
            "missing_required_cells": sum(all_missing_required.values()),
            "zero_quantity_rows": total_zero_quantity,
            "missing_products": missing_count,
            "template_dupes": dupes_stats["template_dupes"],
            "site_dupes": dupes_stats["site_dupes"],
            "blocked": blocked,
        }
        print("\n=== ИТОГ import_check ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        return StageResult(self.name, ok=not blocked, blocked=blocked, summary=summary)
