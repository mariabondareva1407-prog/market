#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Рецепт склада №6 — Мвидео. Товары — с apigw-seller (ПСБ Маркет), тем же
API, что и у b2c (belka/warehouses/b2c.py), только другой psb_seller_id и,
только у Мвидео, регулярный файл-ассортимент (см. belka/core/psb_api.py,
belka/stages/psb_generate.py) — 09.2026, по решению пользователя, это
теперь ОБЯЗАТЕЛЬНЫЙ вход, задающий не только цену/остаток поверх ответа
apigw, но и сам список offer_id к выгрузке (карточка каждого ищется
точечно через find_product_by_offer_id, а не выгрузкой всего каталога).

⚠ НЕ ПРОВЕРЕНО НА ЖИВЫХ ДАННЫХ:
  - store_id=6 ниже — ПРЕДПОЛОЖЕНИЕ (по номеру склада), реальный store_id
    для admin-API загрузки ещё не подтверждён. Не запускайте upload/full_sync
    без проверки.
  - Мост категорий переиспользует console_to_admin_category_map.json Bazar
    напрямую по categoryId апигв — гипотеза id-пространств, см. docstring
    belka/stages/psb_generate.py. Первый прогон 'psb_generate' покажет
    unmapped_categories в summary, если гипотеза не подтвердится.
  - файл ассортимента — это просто ЕДИНСТВЕННЫЙ xlsx во входной папке
    склада (input/mvideo/, без подпапок по дате, см.
    PipelineContext.resolved_input_dir/input_file): имя и шаблон имени
    больше нигде не заданы, кладите свежий файл вместо прежнего.
"""
from __future__ import annotations

from ..context import WarehouseConfig
from ..pipeline import Pipeline
from ..stages.psb_generate import PsbGenerateStage
from ..stages.import_check import ImportCheckStage
from ..stages.import_attributes import ImportAttributesStage
from ..stages.upload import UploadStage
from ..stages.certificates_assortment import CertificatesFromAssortmentStage
from ..stages.zero_stock import ZeroStockStage

CONFIG = WarehouseConfig(
    key="mvideo",
    display_name="Склад №6 — Мвидео",
    seller_id=6,          # продавец на Белке — один на весь маркетплейс
    store_id=6,            # ПРЕДПОЛОЖЕНИЕ, см. предупреждение выше
    admin_api_base="https://admin.belkamarket.ru",
    # attribute_mapping_file не задан — файл выводится из ключа склада
    # (attribute_mapping_mvideo.json). attribute_prompt=False: attributeName
    # апигв уже в терминах схемы Белки, прямое/нормализованное
    # сопоставление закрывает почти всё, а спрашивать про каждую
    # обязательную характеристику незачем — не совпавшее видно в общем
    # отчёте по обязательным полям, и файл можно дозаполнить руками.
    attribute_prompt=False,
    psb_api_base="https://apigw-seller.xn--80abntiqkep.xn--p1ai",
    psb_seller_id=475,      # id продавца на стороне ПСБ (не путать с seller_id Белки выше)
    # 09.2026, по просьбе пользователя: основное и последнее доп. фото
    # убираются, основным становится первое доп.; HTML в описании снимается.
    # Что именно поменялось — в логе psb_generate и в reports/images_report_mvideo.xlsx.
    rotate_images=True,
    strip_html_description=True,
)


def build_pipelines(config: WarehouseConfig = CONFIG) -> dict:
    # import_check / import_attributes / upload — те же классы, что у Bazar:
    # они уже не завязаны ни на что Bazar-специфичное (сканируют xlsx по
    # заголовкам, а не по фиксированным колонкам, см. import_shared.py).
    listing_stages = [
        PsbGenerateStage(), ImportCheckStage(), ImportAttributesStage(), UploadStage(),
    ]
    pipelines = {
        "listing": Pipeline("listing", listing_stages),
        # zero_stock — тот же класс, что у Bazar, без изменений (читает
        # cfg.store_id/cfg.admin_api_base из WarehouseConfig этого склада).
        #
        # certificates — ЭТО НЕ CertificatesStage Bazar (тот читает цветной
        # xlsx-отчёт, которого у Мвидео нет), а CertificatesFromAssortmentStage
        # (belka/stages/certificates_assortment.py): читает колонку "Номер
        # сертификата" из того же файла-ассортимента, что и psb_generate.
        # Запускается с конкретным файлом через CLI (--file), как и у Bazar —
        # сам объект здесь нужен только чтобы 'certificates' числился
        # доступным пайплайном и чтобы cli.py знал, какой класс поднимать
        # (см. cli.py: класс берётся из pipelines["certificates"].stages[0]).
        "certificates": Pipeline("certificates", [CertificatesFromAssortmentStage()]),
        "zero_stock": Pipeline("zero_stock", [ZeroStockStage()]),
        # bridge не заведён — мост категорий переиспользуется от Bazar
        # (console_to_admin_category_map.json, см. предупреждение выше).
    }
    # каждый stage 'listing' — ЕЩЁ И отдельный однословный пайплайн, чтобы
    # `python run.py mvideo psb_generate` и т.п. работали без --only (как у Bazar).
    for stage in listing_stages:
        pipelines[stage.name] = Pipeline(stage.name, [stage])
    return pipelines
