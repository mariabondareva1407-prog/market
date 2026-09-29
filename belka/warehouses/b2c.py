#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Рецепт склада №7 — b2c-платформа. Товары — с apigw-seller (ПСБ Маркет), тем
же API, что и у Мвидео (belka/warehouses/mvideo.py), но ВРЕМЕННО (по
решению пользователя, 09.2026) не генерируются с нуля через
psb_generate.py, а берутся из УЖЕ ГОТОВЫХ шаблонов, которые нужно класть в
входную папку склада — input/b2c/ (см.
PipelineContext.resolved_input_dir, WarehouseConfig.input_dir по умолчанию
'input'): stage 'psb_update_prices' только обновляет в них цену/цену до
скидки/остаток (аналог присланного update_prices.py) и кладёт обновлённые
копии в output/b2c/ГГГГ-ММ-ДД/ — дальше как обычно, import_check ->
import_attributes -> upload.

⚠ НЕ ПРОВЕРЕНО НА ЖИВЫХ ДАННЫХ — см. предупреждение в belka/warehouses/mvideo.py
(то же самое касается store_id=7 и, раз готовые шаблоны предполагаются уже
верно размеченными по категориям, — самого способа, которым они были
получены изначально: это вне зоны ответственности данного пайплайна).
"""
from __future__ import annotations

from ..context import WarehouseConfig
from ..pipeline import Pipeline
from ..stages.psb_update_prices import PsbUpdatePricesStage
from ..stages.import_check import ImportCheckStage
from ..stages.import_attributes import ImportAttributesStage
from ..stages.upload import UploadStage

CONFIG = WarehouseConfig(
    key="b2c",
    display_name="Склад №7 — B2C-платформа",
    seller_id=6,          # продавец на Белке — один на весь маркетплейс
    store_id=7,            # ПРЕДПОЛОЖЕНИЕ, см. предупреждение выше
    admin_api_base="https://admin.belkamarket.ru",
    # attribute_mapping_file не задан — файл выводится из ключа склада
    # (attribute_mapping_b2c.json). attribute_prompt=False: attributeName
    # апигв уже в терминах схемы Белки, прямое/нормализованное
    # сопоставление закрывает почти всё, а спрашивать про каждую
    # обязательную характеристику незачем — не совпавшее видно в общем
    # отчёте по обязательным полям, и файл можно дозаполнить руками.
    attribute_prompt=False,
    psb_api_base="https://apigw-seller.xn--80abntiqkep.xn--p1ai",
    psb_seller_id=141,      # id продавца на стороне ПСБ (не путать с seller_id Белки выше)
    # input_dir (по умолчанию 'input') — сюда кладутся ГОТОВЫЕ шаблоны, их
    # готовит не этот пайплайн; файла ассортимента у склада нет — переопределения
    # цены/остатка из отдельного файла тут нет, источник цены/остатка — сам apigw
)


def build_pipelines(config: WarehouseConfig = CONFIG) -> dict:
    listing_stages = [
        PsbUpdatePricesStage(), ImportCheckStage(), ImportAttributesStage(), UploadStage(),
    ]
    pipelines = {
        "listing": Pipeline("listing", listing_stages),
    }
    # каждый stage 'listing' — ЕЩЁ И отдельный однословный пайплайн, чтобы
    # `python run.py b2c psb_update_prices` и т.п. работали без --only (как у Bazar).
    for stage in listing_stages:
        pipelines[stage.name] = Pipeline(stage.name, [stage])
    return pipelines
