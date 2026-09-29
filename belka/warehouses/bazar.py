#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Рецепт склада №5 — Базар. Единственный сейчас реализованный склад: все
параметры (id продавца/склада, хосты, пути к файлам) уже проверены в
проде — это прямой перенос того, что раньше было шестью belka_*.py
скриптами с константами в шапке каждого файла.

Остальные склады «Белки» (один продавец, seller_id=6) см. в
belka/warehouses/__init__.py -> KNOWN_WAREHOUSES.
"""
from __future__ import annotations

from ..context import WarehouseConfig
from ..pipeline import Pipeline
from ..stages.bridge import BuildCategoryBridgeStage
from ..stages.generate import GenerateStage
from ..stages.import_check import ImportCheckStage
from ..stages.import_attributes import ImportAttributesStage
from ..stages.upload import UploadStage
from ..stages.certificates import CertificatesStage
from ..stages.zero_stock import ZeroStockStage

CONFIG = WarehouseConfig(
    key="bazar",
    display_name="Склад №5 — Базар",
    seller_id=6,
    store_id=5,
    admin_api_base="https://admin.belkamarket.ru",
    console_api_base="https://console.xn--80abntiqkep.xn--p1ai",
    # Задано ЯВНО, в отличие от остальных складов: у Базара уже есть живой
    # журнал деактиваций под этим именем (записи с 2026-09-11), и переезд на
    # выводимое из ключа склада имя (deactivated_by_certificate_bazar.json,
    # см. PipelineContext.resolved_cert_log_file) потерял бы их — тогда все
    # деактивированные по сертификату товары ушли бы на активацию при
    # следующем прогоне. Остальные склады получают собственный файл
    # автоматически; переименовывать этот не нужно.
    cert_log_file="deactivated_by_certificate.json",
    # Тоже задано явно и по той же причине: у Базара уже есть заполненный файл
    # маппинга под этим именем (77 записей), переезд на выводимое из ключа
    # склада имя (attribute_mapping_bazar.json) обнулил бы его.
    attribute_mapping_file="attribute_mapping.json",
)


def build_pipelines(config: WarehouseConfig = CONFIG) -> dict:
    """Это и есть 'сборка' пайплайна под склад: конфиг + список stage'ов на
    пайплайн. CLI умеет прогнать пайплайн целиком или 'разобрать' его —
    прогнать через --only только часть stage'ов; каждый stage ниже вдобавок
    зарегистрирован ЕЩЁ И отдельным однословным пайплайном (см. цикл в
    конце функции) — так что `python run.py bazar upload` работает сам по
    себе, без --only.

    ПОРЯДОК ЗАПУСКА 'listing': это порядок элементов в списке
    listing_stages ниже. Чтобы поменять порядок — переставьте элементы
    ИМЕННО ТУТ (а не в двух местах): и сам пайплайн 'listing', и
    одноимённые отдельные пайплайны берут стадии из этого же списка.
    zero_stock стоит первым ВНЕ этого списка намеренно — см. комментарий
    у 'listing' ниже про то, почему его порядок в 'listing' жёстко
    зафиксирован и трогать его не стоит."""
    listing_stages = [
        GenerateStage(), ImportCheckStage(), ImportAttributesStage(), UploadStage(),
    ]
    pipelines = {
        # разовый/редкий шаг — категории на Белке меняются нечасто
        "bridge": Pipeline("bridge", [BuildCategoryBridgeStage()]),
        # обслуживание — не часть 'listing', гоняется отдельно по мере необходимости
        "certificates": Pipeline("certificates", [CertificatesStage()]),
        # 'listing' = zero_stock + generate + import_check + import_attributes
        # + upload одним прогоном, порядок гарантирован (это бывший
        # 'full_sync' — переименован по решению пользователя, 09.2026; старое
        # отдельное "generate->import->upload БЕЗ zero_stock" под именем
        # 'listing' убрано — если оно нужно, просто не запускайте zero_stock,
        # например `python run.py bazar listing --only generate,import,upload`,
        # или гоняйте стадии по отдельности, см. ниже). У Белки нет рабочего
        # DELETE для товаров — пропавшие из нового JSON товары "удаляются"
        # тем, что zero_stock зануляет ВЕСЬ остаток склада целиком (без
        # разбора, кто ещё есть в новом JSON, а кого уже нет), а следующий
        # за ним generate+upload возвращает реальные остатки только тем, кто
        # в новом JSON остался — то, чего там больше нет, так и останется
        # занулённым. Именно поэтому zero_stock ОБЯЗАН идти первым: если
        # переставить его в конец, он обнулит остатки, которые upload только
        # что выставил.
        "listing": Pipeline("listing", [ZeroStockStage(), *listing_stages]),
        # zero_stock отдельным пайплайном — своя, отдельная от 'listing'
        # инстанция stage'а (не тот же объект, что первый элемент выше)
        "zero_stock": Pipeline("zero_stock", [ZeroStockStage()]),
    }
    # generate/import_check/import_attributes/upload — те же 4 инстанции,
    # что и внутри 'listing' (сознательно те же объекты: они не хранят
    # состояние между запусками, а совпадение параметров конструктора с
    # 'listing' гарантировано самим фактом общего списка выше).
    for stage in listing_stages:
        pipelines[stage.name] = Pipeline(stage.name, [stage])
    return pipelines
