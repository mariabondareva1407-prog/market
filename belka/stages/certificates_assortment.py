#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 'certificates' для Мвидео (см. belka/warehouses/mvideo.py) — тот же
смысл, что и у Bazar (belka/stages/certificates.py: входной сигнал -> сверка
с логом -> allow_publish на сайте), но ДРУГОЙ формат входа. У Bazar это
отдельный цветной xlsx-отчёт (лист на категорию, статус — заливкой ячейки).
У Мвидео отдельного отчёта нет — сертификат это просто текстовое значение в
колонке "Номер сертификата" РЕГУЛЯРНОГО файла-ассортимента (того же самого,
что уже читает psb_generate для цены/остатка, см. core/psb_api.py).

Правило (09.2026, уточнено пользователем ПОСЛЕ первой версии этого файла —
см. ниже): сигнал — НЕ текст ячейки, а её ЗАЛИВКА, и трактуется она ТАК ЖЕ,
как у Bazar (общий классификатор core/cert_report.py: зелёный — по оттенку,
а не точным совпадением с FF00B050): зелёная -> сертификат подтверждён ->
активен; любая другая (без заливки, серая FFCCCCCC и т.п.) -> сертификата
нет -> деактивация. Первая версия этого stage'а смотрела на
ТЕКСТ ячейки (пусто/"#N/A" -> нет сертификата) — на присланном файле текст и
заливка совпадали 1-в-1 по всем 999 строкам, но заливка это тот же явный
сигнал статуса, что уже используется у Bazar, поэтому надёжнее полагаться на
неё: следующая выгрузка может использовать другой текст-плейсхолдер для
"нет сертификата", а конвенция цвета — общая.

Механизм переключения на сайте переиспользован от CertificatesStage Bazar
1-в-1 (те же статические API-методы, тот же cert_log_file/формат лога, тот
же порядок "активация первой, деактивация последней") — vendor_code для
Мвидео/b2c это offer_id (пишется в "Id Товара" без префикса склада, см.
psb_generate.py), то есть то же самое поле по смыслу, что колонка N у Bazar.

Известное ограничение (то же, что и у Bazar-версии): offer_id, пропавший из
файла целиком (а не просто с пустой ячейкой сертификата), трактуется как "не
проблемный" и уходит на активацию — при неполных выгрузках это может
ошибочно активировать пропущенные товары.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime

from ..core.cert_log import load_cert_log, save_cert_log
from ..core.cert_report import classify_fill, resolve_cert_file
from ..core.psb_api import CERT_COLUMN_DEFAULT, load_cert_statuses_from_assortment
from ..core.http import SessionBlockedError
from ..pipeline import Stage, StageResult
from .certificates import CertificatesStage


def _has_certificate(cell) -> bool:
    """Сигнал — заливка ячейки, не текст (см. docstring модуля). Совпадает
    с проверкой в certificates.py ('red' там значит проблему, здесь наоборот
    — зелёный явно подтверждает сертификат, а не 'not red').

    09.2026: «зелёная» определяется общим классификатором проекта
    (core/cert_report.py::classify_fill) — по оттенку, а не точным
    совпадением с FF00B050, чтобы светло-зелёный стиль Excel «Good»
    (FFC6EFCE) не читался как «сертификата нет»."""
    return classify_fill(cell) == "green"


def _scan_assortment(path: Path, offer_id_column="Материал", cert_column=CERT_COLUMN_DEFAULT) -> dict:
    """offer_id -> True (сертификат есть, ячейка зелёная) / False (нет).

    09.2026: сам разбор файла переехал в core/psb_api.py
    (load_cert_statuses_from_assortment) — он общий с фильтром
    --green/--white/--red у psb_generate, так что «зелёная ли ячейка» оба
    места решают одинаково. Оттуда же прилетают две вещи, которых не было
    в первой версии: заголовки сравниваются после clean_header (колонка
    находится и со звёздочкой), а при нескольких строках на один offer_id
    побеждает «худший» статус, а не последняя строка файла."""
    statuses = load_cert_statuses_from_assortment(path, offer_id_column=offer_id_column,
                                                   cert_column=cert_column)
    return {offer_id: status == "green" for offer_id, status in statuses.items()}


class CertificatesFromAssortmentStage(Stage):
    name = "certificates"

    def __init__(self, xlsx_path=None):
        # Как и у Bazar-версии, путь — НЕобязательное переопределение: по
        # умолчанию берётся единственный xlsx входной папки склада, то есть
        # ровно тот файл ассортимента, который читает psb_generate.
        self.xlsx_path = Path(xlsx_path) if xlsx_path else None

    def run(self, ctx) -> StageResult:
        cfg = ctx.config
        xlsx_path = resolve_cert_file(self.xlsx_path, ctx, "файл-ассортимент с сертификатами")

        session = ctx.admin_session()

        print(f"Читаю {xlsx_path.name}...")
        cert_status = _scan_assortment(xlsx_path)
        current_no_cert = {offer_id for offer_id, has_cert in cert_status.items() if not has_cert}
        print(f"Товаров в файле: {len(cert_status)}, без сертификата (заливка ячейки не зелёная): {len(current_no_cert)}")

        log_path = ctx.resolved_cert_log_file()
        deactivated = load_cert_log(log_path, cfg.key)
        to_deactivate = sorted(current_no_cert - set(deactivated.keys()))
        to_activate = sorted(set(deactivated.keys()) - current_no_cert)
        print(f"Новых на деактивацию: {len(to_deactivate)}")
        print(f"На активацию (сертификат появился / пропал из файла): {len(to_activate)}")

        blocked = False
        try:
            # тот же порядок, что у Bazar, и по той же причине: активация
            # первой, деактивация последней — актуальное состояние по
            # свежему файлу должно побеждать при конфликте одного offer_id
            # в обоих списках сразу.
            print("\n=== Активация ===")
            for offer_id in to_activate:
                info = deactivated.get(offer_id, {})
                product_id = info.get("id")
                if product_id is None:
                    product = CertificatesStage._api_find_product_by_vendor_code(session, cfg.admin_api_base, offer_id)
                    if not product:
                        print(f"  ⚠ Не найден товар по vendor_code (offer_id) '{offer_id}' — оставляю в логе")
                        continue
                    product_id = product["id"]
                CertificatesStage._api_set_publish_status(session, cfg.admin_api_base, product_id, True)
                deactivated.pop(offer_id, None)
                save_cert_log(log_path, cfg.key, deactivated)
                print(f"  ✔ Активирован: '{offer_id}' (id={product_id})")

            print("\n=== Деактивация ===")
            for offer_id in to_deactivate:
                product = CertificatesStage._api_find_product_by_vendor_code(session, cfg.admin_api_base, offer_id)
                if not product:
                    print(f"  ⚠ Не найден товар по vendor_code (offer_id) '{offer_id}' — пропускаю")
                    continue
                product_id = product["id"]
                CertificatesStage._api_set_publish_status(session, cfg.admin_api_base, product_id, False)
                deactivated[offer_id] = {"id": product_id, "deactivated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
                save_cert_log(log_path, cfg.key, deactivated)
                print(f"  ✔ Деактивирован: '{offer_id}' (id={product_id})")
        except SessionBlockedError as e:
            blocked = True
            print(f"\n⛔ ОСТАНОВКА: {e}")
            print("Уже обработанное сохранено в лог — перезапустите с новым токеном для остального.")

        summary = {"deactivated_total": len(deactivated), "log": str(log_path), "blocked": blocked}
        print("\n=== ИТОГ certificates (Мвидео) ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        return StageResult(self.name, ok=not blocked, summary=summary)
