#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI: python -m belka <склад> <пайплайн> [опции]

Проще запускать через ../run.py (без "-m", с меню при запуске без
аргументов, и всегда из своей папки независимо от текущего рабочего
каталога) — см. README.md. Аргументы ниже одинаковые что для
"python -m belka ...", что для "python run.py ...".

Примеры:
    python -m belka list                                    # какие склады вообще есть
    python -m belka bazar list                               # какие пайплайны/stage'и есть у Базара

    # 'listing' — полный ежедневный цикл (это бывший 'full_sync'; см. ниже,
    # почему он теперь так называется): zero_stock -> generate ->
    # import_check -> import_attributes -> upload, гарантированный порядок.
    python -m belka bazar listing
    python -m belka bazar listing --yes                       # без интерактивных подтверждений на upload/zero_stock
    python -m belka bazar listing --only generate,import       # только часть stage'ов ("import" — алиас import_check+import_attributes)
    python -m belka bazar listing --no-interactive --yes      # безопасный прогон из cron

    # каждый stage 'listing' можно запустить и САМ ПО СЕБЕ, без --only —
    # он же зарегистрирован как отдельный однословный пайплайн:
    python -m belka bazar zero_stock --yes
    python -m belka bazar generate
    python -m belka bazar import_check
    python -m belka bazar import_attributes
    python -m belka bazar upload

    python -m belka bazar bridge                              # разовая пересборка моста категорий
    python -m belka bazar certificates                        # файл берётся из input/bazar/, имя не нужно
    python -m belka bazar certificates --verify               # только сверка с сайтом, без изменений
    python -m belka bazar certificates --file /путь/к/другому.xlsx   # переопределение, когда нужен не тот файл

    # фильтр по сертификату у генератора шаблонов (тот же файл, что у certificates):
    # включить в шаблон только товары с подтверждённым сертификатом (зелёный)
    python -m belka bazar generate --green
    # зелёные + всё, что не красное и не зелёное — то есть "все, кроме красных"
    python -m belka bazar generate --green --white
    # работает и через listing, если generate — часть того, что реально запускается:
    python -m belka bazar listing --only generate,upload --red
    # у Мвидео тот же фильтр и тот же принцип: статус читается из колонки
    # "Номер сертификата" файла ассортимента, лежащего в input/mvideo/
    python -m belka mvideo psb_generate --green --white
"""
from __future__ import annotations

import argparse
import sys

from .core.env import load_env
from .context import PipelineContext
from .warehouses import get_warehouse, list_warehouses

# groups в --only: чтобы старое "--only generate,import" продолжало работать
# после разбиения бывшего единого 'import' на import_check/import_attributes
# (см. belka/warehouses/bazar.py) — "import" разворачивается в эти имена, но
# можно указать и любое из них по отдельности. (Был ещё import_capitalize —
# убран 09.2026, см. bazar.py.)
ONLY_GROUP_ALIASES = {
    "import": ["import_check", "import_attributes"],
}


def _expand_only(only: list[str] | None, pipeline) -> list[str] | None:
    if not only:
        return only
    stage_names = set(pipeline.stage_names())
    expanded = []
    for item in only:
        if item in ONLY_GROUP_ALIASES and item not in stage_names:
            expanded.extend(ONLY_GROUP_ALIASES[item])
        else:
            expanded.append(item)
    seen = set()
    result = []
    for item in expanded:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m belka", description="Пайплайны загрузки товаров «Белки»")
    parser.add_argument("warehouse", help="ключ склада ('list' — показать все известные склады)")
    parser.add_argument("pipeline", nargs="?", help="имя пайплайна склада ('list' — показать доступные)")
    parser.add_argument("--only", help="прогнать только эти stage'и пайплайна, через запятую "
                                        "('import' — алиас import_check+import_attributes)")
    parser.add_argument("--file", help="НЕобязательное переопределение: взять статусы сертификатов из "
                                        "этого файла, а не из единственного xlsx входной папки склада "
                                        "(input/<склад>/). Имеет смысл только для 'certificates' и вместе "
                                        "с --green/--white/--red")
    parser.add_argument("--verify", action="store_true",
                         help="только для 'certificates': сверить реальный allow_publish на сайте с файлом, "
                              "ничего не патчить и не трогать лог")
    parser.add_argument("--green", action="store_true",
                         help="фильтр генератора шаблонов: включить товары с подтверждённым сертификатом "
                              "(зелёная заливка). Комбинируется с --white/--red.")
    parser.add_argument("--white", action="store_true",
                         help="фильтр генератора шаблонов: включить товары с ЛЮБОЙ не красной и не зелёной "
                              "заливкой (пусто, серая, жёлтая — 'нет отметки'). Комбинируется с --green/--red.")
    parser.add_argument("--red", action="store_true",
                         help="фильтр генератора шаблонов: включить товары с проблемным сертификатом "
                              "(красная заливка). Комбинируется с --green/--white.")
    parser.add_argument("--yes", action="store_true",
                         help="не спрашивать подтверждения на опасные шаги (upload/zero_stock/подозрительные значения)")
    parser.add_argument("--no-interactive", action="store_true",
                         help="не задавать вопросы в консоли вообще (для крон-запусков; без --yes опасные шаги отменяются)")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)

    if args.warehouse == "list":
        list_warehouses()
        return 0

    module = get_warehouse(args.warehouse)
    pipelines = module.build_pipelines()

    if args.pipeline in (None, "list"):
        print(f"Пайплайны склада '{args.warehouse}':")
        for name, pipeline in pipelines.items():
            print(f"  {name}: {pipeline.stage_names()}")
        return 0

    if args.pipeline not in pipelines:
        print(f"Неизвестный пайплайн '{args.pipeline}' для склада '{args.warehouse}'. "
              f"Доступные: {list(pipelines)}", file=sys.stderr)
        return 2

    pipeline = pipelines[args.pipeline]

    # Файл сертификатов по умолчанию НЕ указывается: stage сам возьмёт
    # единственный xlsx входной папки склада (см. resolve_cert_file в
    # belka/core/cert_report.py). --file — только переопределение, когда
    # нужен не тот файл, что лежит во входной папке.
    # Класс сертификатов не хардкодим (у складов он разный: Bazar — цветной
    # xlsx-отчёт, Мвидео — колонка в файле-ассортименте, см.
    # belka/stages/certificates.py и certificates_assortment.py) — берём его
    # из того же рецепта склада, что уже собрал module.build_pipelines().
    certificates_stage = None
    if args.pipeline == "certificates":
        from .pipeline import Pipeline
        certificates_stage_cls = type(pipelines["certificates"].stages[0])
        certificates_stage = certificates_stage_cls(xlsx_path=args.file)
        pipeline = Pipeline("certificates", [certificates_stage])
    elif args.verify:
        print("--verify поддерживается только для пайплайна 'certificates'", file=sys.stderr)
        return 2

    env = load_env(args.env_file)
    ctx = PipelineContext(
        config=module.CONFIG, env=env,
        interactive=not args.no_interactive, assume_yes=args.yes,
        env_path=args.env_file,
    )

    if args.verify:
        # только чтение: не через Pipeline.run (там расчёт на StageResult с
        # PATCH-семантикой) — дёргаем verify() напрямую, ничего не патчится.
        if not hasattr(certificates_stage, "verify"):
            print(f"--verify не реализован для класса сертификатов склада '{args.warehouse}' "
                  f"({type(certificates_stage).__name__})", file=sys.stderr)
            return 2
        result = certificates_stage.verify(ctx)
        return 0 if result.ok else 1

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    only = _expand_only(only, pipeline)

    # --green/--white/--red — фильтр по сертификату у генератора шаблонов.
    # Stage ищется НЕ по имени 'generate' (у ПСБ-складов он называется
    # 'psb_generate' — раньше из-за этого флаги работали только у Базара), а
    # по способности: любой stage с set_cert_filter (CertFilterMixin, см.
    # belka/core/cert_report.py). Сам объект НЕ пересоздаётся — фильтр
    # включается на том, что собрал рецепт склада, иначе терялись бы
    # заданные там параметры (vat_default, price_markup_const и т.п.).
    cert_colors_requested = args.green or args.white or args.red
    if cert_colors_requested:
        will_run = [s for s in pipeline.stages if not only or s.name in only]
        targets = [s for s in will_run if hasattr(s, "set_cert_filter")]
        if not targets:
            print(f"--green/--white/--red нечего фильтровать: среди запускаемых stage'ей "
                  f"({[s.name for s in will_run]}) нет генератора шаблонов", file=sys.stderr)
            return 2
        for stage in targets:
            stage.set_cert_filter(file=args.file, green=args.green,
                                  white=args.white, red=args.red)
    elif args.file and args.pipeline != "certificates":
        # раньше лишний --file просто проглатывался, и со стороны это
        # выглядело как «фильтр применился» — хотя в шаблон уходили все
        # товары подряд.
        print(f"--file имеет смысл только для 'certificates' или вместе с --green/--white/--red; "
              f"для '{args.pipeline}' он ничего не делает", file=sys.stderr)
        return 2

    results = pipeline.run(ctx, only=only)
    return 1 if any(not r.ok for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
