#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run.py — простой запуск пайплайна «Белки».

ГЛАВНОЕ: этот файл всегда работает из СВОЕЙ ПАПКИ, а не из той, откуда его
запустили. То есть даже если у вас ярлык на рабочем столе, запуск по
расписанию (cron/планировщик) с чужим рабочим каталогом или просто
"зашли в другую папку и напечатали python /куда-то/run.py" — .env,
input/, output/, category_map.json и все остальные файлы пайплайна всё
равно найдутся ТУТ, рядом с run.py, а не там, откуда его вызвали.

САМЫЙ ПРОСТОЙ СПОСОБ — без аргументов, только вопросы и ответы:

    python run.py

Так же можно и одной командой, если заранее знаете, что нужно (аргументы
такие же, какими раньше были у "python -m belka ..."):

    python run.py bazar listing              # zero_stock -> generate -> import_check -> import_attributes -> upload
    python run.py bazar listing --yes
    python run.py bazar certificates
    python run.py bazar listing --no-interactive --yes     # для крон-запуска

    # каждый stage можно запустить и сам по себе, без --only:
    python run.py bazar zero_stock --yes
    python run.py bazar generate
    python run.py bazar import_check
    python run.py bazar import_attributes
    python run.py bazar upload

    # фильтр по сертификату при генерации шаблонов (тот же файл, что у certificates):
    python run.py bazar generate --green           # только подтверждённые
    python run.py bazar generate --green --white   # всё, кроме красных
    python run.py mvideo psb_generate --green --white

'listing' — это бывший 'full_sync': полный цикл с гарантированным порядком
(zero_stock обязательно первым, иначе он обнулит только что залитые
остатки). Если нужно поменять порядок стадий внутри 'listing' — это
делается в belka/warehouses/bazar.py (список listing_stages), см.
README.md.

Про папки: вход разложен ТОЛЬКО по складам — input/<склад>/ (например
input/bazar/), без подпапок по дате. Кладите туда свежие файлы, заменяя
прежние: у Базара это один xlsx-отчёт по сертификатам и один JSON-экспорт,
у Мвидео — один xlsx-ассортимент, у b2c — готовые шаблоны. Имя файла нигде
указывать не нужно: берётся то, что лежит в папке склада (если подходящих
файлов несколько, пайплайн возьмёт первый по алфавиту и громко про это
скажет). Вывод по-прежнему разложен и по складу, и по дню —
output/<склад>/ГГГГ-ММ-ДД/: там нужна история прогонов.

"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# --- работаем всегда из папки, где лежит этот файл -------------------------
PROJECT_DIR = Path(__file__).resolve().parent
os.chdir(PROJECT_DIR)
sys.path.insert(0, str(PROJECT_DIR))

from belka.cli import main  # noqa: E402
from belka.warehouses import REGISTRY  # noqa: E402


def _choose(prompt: str, options: list[str]) -> str:
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        raw = input("Номер: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print("Не понял — введите номер из списка выше.")


def _interactive_argv() -> list[str]:
    print("=== Пайплайн «Белки» — простой запуск (без аргументов) ===")

    warehouses = list(REGISTRY)
    warehouse = _choose("Какой склад?", warehouses)
    module = REGISTRY[warehouse]
    pipelines = module.build_pipelines()

    pipeline = _choose(f"Какой пайплайн склада '{warehouse}'?", list(pipelines))
    print(f"  (стадии: {pipelines[pipeline].stage_names()})")

    argv = [warehouse, pipeline]

    if pipeline == "certificates":
        # Путь к файлу больше не спрашиваем: stage сам берёт единственный
        # xlsx из input/<склад>/ (см. resolve_cert_file в
        # belka/core/cert_report.py).
        verify = input("Только сверить с сайтом, ничего не менять? (да/нет): ").strip().lower()
        if verify in ("да", "y", "yes", "д"):
            argv += ["--verify"]

    return argv


def run() -> int:
    argv = sys.argv[1:]
    if not argv:
        argv = _interactive_argv()
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(run())
