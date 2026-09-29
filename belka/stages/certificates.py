#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 'certificates' — сканирует xlsx-выгрузку товаров (лист на категорию,
шапка на 2-й строке, данные с 3-й) и смотрит на заливку ячейки колонки
"S_CERT:Сертификат":
  - красная заливка -> сертификат проблемный -> товар деактивируется
  - зелёная заливка, или заливки нет -> товар активен/активируется

Не входит в 'listing' — не часть регулярной выкладки, требует конкретный
xlsx-отчёт по сертификатам. Запуск: `python -m belka <склад> certificates
--file путь/к/файлу.xlsx`.

Ведёт динамический JSON-лог (cert_log_file). При каждом запуске сравнивает
текущее состояние с прошлым прогоном; уже деактивированные (были красными
и остались красными) — не трогаются повторно. Порядок: активация первой,
деактивация последней (важно при миграции ключа — см. комментарий в run()).

verify(ctx) — отдельный режим только для чтения (`--verify`): не патчит
ничего, сверяет РЕАЛЬНЫЙ allow_publish на сайте (products/drafts:search без
фильтра allow_publish — так возвращаются оба статуса) с тем, что должно
быть по текущей заливке S_CERT в файле. allow_publish в этом API — поле
ТОВАРА (products/{id}), не оффера — так и было в исходном скрипте, это
подтверждено (см. чат) как правильное: сертификат — свойство физического
товара, гасить его нужно глобально, а не на уровне оффера/склада.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime

import openpyxl

from ..core.cert_log import load_cert_log, save_cert_log
from ..core.csvio import write_csv_report
from ..core.http import SessionBlockedError
from ..core.cert_report import scan_workbook_tricolor, resolve_cert_file, RED_RGB, GREEN_RGB  # re-exportнуты ниже для certificates_assortment.py
from ..pipeline import Stage, StageResult

PRODUCTS_SEARCH_URL_PART = "/api/v1/catalog/products/drafts:search"
PRODUCT_URL_PART = "/api/v1/catalog/products/{id}"
VERIFY_PAGE_LIMIT = 100


class CertificatesStage(Stage):
    name = "certificates"

    def __init__(self, xlsx_path=None):
        # xlsx_path — НЕобязательное переопределение (CLI: --file). По
        # умолчанию отчёт берётся из входной папки склада (input/<склад>/),
        # где и так лежит ровно один xlsx — имя вводить не нужно.
        self.xlsx_path = Path(xlsx_path) if xlsx_path else None

    def _resolve_xlsx(self, ctx) -> Path:
        return resolve_cert_file(self.xlsx_path, ctx, "xlsx-отчёт по сертификатам")

    # ------------------------------------------------------------- xlsx scan --

    @staticmethod
    def _scan_workbook(wb) -> dict:
        """article -> 'red'/'green'. Обёртка над общим трёхцветным сканом
        (belka/core/cert_report.py, используется также фильтром --white/
        --green/--red у stage'а 'generate'): здесь, как и раньше, 'white'
        (пустая ячейка) трактуется как 'green' — переключение allow_publish
        остаётся бинарным, различие red/не-red и есть вся логика этого
        stage'а."""
        tricolor = scan_workbook_tricolor(wb)
        return {article: ("red" if status == "red" else "green") for article, status in tricolor.items()}

    # ------------------------------------------------------------- API calls --

    @staticmethod
    def _api_find_product_by_vendor_code(session, api_base, vendor_code):
        url = f"{api_base}{PRODUCTS_SEARCH_URL_PART}"
        body = {
            "include": ["images"], "sort": ["-id"],
            "filter": {"vendor_code": vendor_code, "status_id": []},
            "pagination": {"type": "offset", "limit": 40, "offset": 0},
        }
        resp = session.post(url, json=body, timeout=30)
        data = resp.json().get("data", [])
        for item in data:
            if str(item.get("vendor_code")) == str(vendor_code):
                return item
        return data[0] if data else None

    @staticmethod
    def _api_set_publish_status(session, api_base, product_id, allow_publish):
        url = f"{api_base}{PRODUCT_URL_PART.format(id=product_id)}"
        resp = session.patch(url, json={"allow_publish": allow_publish}, timeout=30)
        return resp.json()

    @staticmethod
    def _api_fetch_publish_state(session, api_base, page_limit: int = VERIFY_PAGE_LIMIT):
        """Полная постраничная выгрузка {vendor_code: {id, name, allow_publish}}.
        Фильтр по allow_publish НЕ передаётся — так возвращаются товары в
        обоих статусах публикации (в отличие от import_check, которому
        для проверки пропавших нужны только активные)."""
        url = f"{api_base}{PRODUCTS_SEARCH_URL_PART}"
        by_vendor_code = {}
        offset = 0
        partial = False
        while True:
            body = {
                "include": [], "sort": ["-id"],
                "filter": {"status_id": []},
                "pagination": {"type": "offset", "limit": page_limit, "offset": offset},
            }
            try:
                resp = session.post(url, json=body, timeout=30)
            except SessionBlockedError as e:
                print(f"\n⛔ ОСТАНОВКА выгрузки состояния публикации: {e}")
                print(f"Успел собрать {len(by_vendor_code)} товаров до остановки — сверка будет ЧАСТИЧНОЙ.")
                partial = True
                break
            data = resp.json().get("data", [])
            if not data:
                break
            for item in data:
                vendor_code = str(item.get("vendor_code") or "").strip()
                if not vendor_code:
                    continue
                by_vendor_code[vendor_code] = {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "allow_publish": item.get("allow_publish"),
                }
            print(f"  Загружено товаров: {len(by_vendor_code)} (offset={offset})")
            if len(data) < page_limit:
                break
            offset += page_limit
        return by_vendor_code, partial

    def verify(self, ctx) -> StageResult:
        """Только чтение: не патчит ничего и не трогает cert_log_file.
        Сверяет реальный allow_publish на сайте с ожиданием по файлу:
          - красный в файле, а на сайте allow_publish=true (или товара нет) —
            деактивация не прошла / кто-то включил обратно руками;
          - НЕ красный в файле, а на сайте allow_publish=false — "осиротевшая"
            деактивация (обычно — то, что раньше отследил бы лог, но лог не
            обязателен для этой сверки: тут источник истины — сам сайт)."""
        cfg = ctx.config
        xlsx_path = self._resolve_xlsx(ctx)

        session = ctx.admin_session()

        print(f"Читаю {xlsx_path.name}...")
        wb = openpyxl.load_workbook(xlsx_path)
        combined_status = self._scan_workbook(wb)
        current_red = {a for a, s in combined_status.items() if s == "red"}
        print(f"Красных в файле: {len(current_red)}")

        print("\nВыгружаю реальный allow_publish по всем товарам (products/drafts:search, без фильтра по статусу публикации)...")
        site_state, partial = self._api_fetch_publish_state(session, cfg.admin_api_base)
        print(f"Получено с сайта: {len(site_state)} товаров" + (" (ЧАСТИЧНО — выгрузка была прервана)" if partial else ""))

        should_be_off_but_isnt = []   # красный в файле, но НЕ off на сайте (or не найден)
        should_be_on_but_isnt = []    # не красный в файле, но off на сайте
        ok_off = ok_on = not_found_and_not_red = 0

        all_articles = set(combined_status.keys()) | set(site_state.keys())
        for article in sorted(all_articles):
            expected_red = article in current_red
            info = site_state.get(article)
            actual_off = bool(info) and info.get("allow_publish") is False
            actual_on = bool(info) and info.get("allow_publish") is True

            if expected_red:
                if actual_off:
                    ok_off += 1
                else:
                    should_be_off_but_isnt.append((article, info))
            else:
                if actual_off:
                    should_be_on_but_isnt.append((article, info))
                elif actual_on:
                    ok_on += 1
                else:
                    not_found_and_not_red += 1  # не красный и не на сайте — не наша забота

        print(f"\n=== Сверка ===")
        print(f"Совпало (красный -> выключен): {ok_off}")
        print(f"Совпало (не красный -> включён): {ok_on}")
        print(f"⚠ Красный в файле, но НЕ выключен на сайте: {len(should_be_off_but_isnt)}")
        for article, info in should_be_off_but_isnt[:20]:
            state = "нет на сайте" if not info else f"allow_publish={info.get('allow_publish')} (id={info.get('id')})"
            print(f"  '{article}': {state}")
        if len(should_be_off_but_isnt) > 20:
            print(f"  ... и ещё {len(should_be_off_but_isnt) - 20}, полный список в CSV")

        print(f"⚠ Выключен на сайте, но НЕ красный в файле (осиротевшая деактивация): {len(should_be_on_but_isnt)}")
        for article, info in should_be_on_but_isnt[:20]:
            print(f"  '{article}': id={info.get('id')}, {info.get('name')}")
        if len(should_be_on_but_isnt) > 20:
            print(f"  ... и ещё {len(should_be_on_but_isnt) - 20}, полный список в CSV")

        report_rows = []
        for article, info in should_be_off_but_isnt:
            report_rows.append([article, "красный в файле, но не выключен на сайте",
                                 info.get("id") if info else "", info.get("allow_publish") if info else "нет на сайте"])
        for article, info in should_be_on_but_isnt:
            report_rows.append([article, "выключен на сайте, но не красный в файле",
                                 info.get("id"), info.get("allow_publish")])

        report_path = None
        if report_rows:
            # имя с ключом склада — отчёты разных складов больше не затирают друг друга
            report_path = ctx.resolved_cert_log_file().with_name(f"cert_verify_mismatches_{cfg.key}.csv")
            write_csv_report(report_path, ["N", "Проблема", "id товара", "allow_publish на сайте"], report_rows)
            print(f"\nРасхождения — в {report_path} ({len(report_rows)} шт.)")

        summary = {
            "checked": len(all_articles),
            "ok_off": ok_off, "ok_on": ok_on,
            "should_be_off_but_isnt": len(should_be_off_but_isnt),
            "should_be_on_but_isnt": len(should_be_on_but_isnt),
            "not_relevant": not_found_and_not_red,
            "report": str(report_path) if report_path else None,
            "partial": partial,
        }
        print("\n=== ИТОГ verify ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        return StageResult("certificates_verify", ok=not partial, summary=summary)

    def run(self, ctx) -> StageResult:
        cfg = ctx.config
        xlsx_path = self._resolve_xlsx(ctx)

        session = ctx.admin_session()

        print(f"Читаю {xlsx_path.name}...")
        wb = openpyxl.load_workbook(xlsx_path)
        combined_status = self._scan_workbook(wb)
        current_red = {a for a, s in combined_status.items() if s == "red"}
        print(f"Найдено товаров (по N) с проблемным сертификатом (красный): {len(current_red)}")

        log_path = ctx.resolved_cert_log_file()
        deactivated = load_cert_log(log_path, cfg.key)
        to_deactivate = sorted(current_red - set(deactivated.keys()))
        to_activate = sorted(set(deactivated.keys()) - current_red)
        print(f"Новых на деактивацию: {len(to_deactivate)}")
        print(f"На активацию (были деактивированы, теперь не красные): {len(to_activate)}")

        blocked = False
        try:
            # Активация — ПЕРВОЙ, деактивация — ПОСЛЕДНЕЙ: при миграции ключа
            # (S_ART -> N) один товар может попасть в оба списка сразу — старая
            # запись говорит "активировать", новая "деактивировать" (если он
            # до сих пор реально красный). Деактивация должна победить, потому
            # что отражает актуальное состояние по свежему файлу.
            print("\n=== Активация ===")
            for article in to_activate:
                info = deactivated.get(article, {})
                product_id = info.get("id")
                if product_id is None:
                    print(f"  ⚠ Запись '{article}' без сохранённого id — ищу по vendor_code (N) напрямую")
                    product = self._api_find_product_by_vendor_code(session, cfg.admin_api_base, article)
                    if not product:
                        print(f"  ⚠ Не найден товар по vendor_code (N) '{article}' — оставляю в логе")
                        continue
                    product_id = product["id"]
                self._api_set_publish_status(session, cfg.admin_api_base, product_id, True)
                deactivated.pop(article, None)
                save_cert_log(log_path, cfg.key, deactivated)
                print(f"  ✔ Активирован: '{article}' (id={product_id})")

            print("\n=== Деактивация ===")
            for article in to_deactivate:
                product = self._api_find_product_by_vendor_code(session, cfg.admin_api_base, article)
                if not product:
                    print(f"  ⚠ Не найден товар по vendor_code (N) '{article}' — пропускаю")
                    continue
                product_id = product["id"]
                self._api_set_publish_status(session, cfg.admin_api_base, product_id, False)
                deactivated[article] = {"id": product_id, "deactivated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
                save_cert_log(log_path, cfg.key, deactivated)
                print(f"  ✔ Деактивирован: '{article}' (id={product_id})")
        except SessionBlockedError as e:
            blocked = True
            print(f"\n⛔ ОСТАНОВКА: {e}")
            print("Уже обработанное сохранено в лог — перезапустите с новым токеном для остального.")

        summary = {"deactivated_total": len(deactivated), "log": str(log_path), "blocked": blocked}
        print("\n=== ИТОГ certificates ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        return StageResult(self.name, ok=not blocked, summary=summary)
