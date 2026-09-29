#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты входной папки без разбивки по дате (09.2026).

Что проверяется:
  1. input/<склад>/ — без подпапки с датой; у разных складов папки разные.
  2. Файл берётся по расширению, имя нигде не задаётся.
  3. Несколько подходящих файлов — берём первый по алфавиту и ГРОМКО
     предупреждаем (тихо выбирать из нескольких нельзя).
  4. Нет файла — понятная ошибка; а если файлы остались в подпапках по дате
     (как было раньше), в ошибке есть подсказка перенести их наверх.
  5. Вывод по дате по-прежнему разложен — эту разбивку не трогали.

Запуск:  python3 tests/test_input_folder.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from belka.context import PipelineContext                    # noqa: E402
from belka.warehouses import bazar, mvideo, b2c              # noqa: E402

RESULTS = []


def check(name, fn):
    try:
        fn()
    except Exception:
        RESULTS.append((name, False))
        print(f"✘ {name}")
        traceback.print_exc()
    else:
        RESULTS.append((name, True))
        print(f"✔ {name}")


def in_tmp(fn):
    """Каждый тест — в своей пустой папке проекта."""
    def wrapper():
        workdir = Path(tempfile.mkdtemp())
        cwd = os.getcwd()
        try:
            os.chdir(workdir)
            fn()
        finally:
            os.chdir(cwd)
            shutil.rmtree(workdir, ignore_errors=True)
    return wrapper


def ctx_for(module):
    return PipelineContext(config=module.CONFIG, env={}, interactive=False, assume_yes=True)


@in_tmp
def test_input_dir_has_no_date():
    ctx = ctx_for(bazar)
    assert ctx.resolved_input_dir() == Path("input/bazar"), ctx.resolved_input_dir()
    assert date.today().isoformat() not in str(ctx.resolved_input_dir())
    assert ctx.resolved_input_dir().is_dir()          # создаётся сама


@in_tmp
def test_input_dirs_differ_per_warehouse():
    dirs = {ctx_for(m).resolved_input_dir() for m in (bazar, mvideo, b2c)}
    assert len(dirs) == 3, dirs


@in_tmp
def test_output_dir_still_split_by_date():
    ctx = ctx_for(bazar)
    assert ctx.resolved_output_dir() == Path("output/bazar") / date.today().isoformat()


@in_tmp
def test_file_found_by_extension_regardless_of_name():
    ctx = ctx_for(mvideo)
    (ctx.resolved_input_dir() / "как угодно названный файл.xlsx").write_bytes(b"x")
    assert ctx.input_file(".xlsx", "тест").name == "как угодно названный файл.xlsx"


@in_tmp
def test_several_files_take_first_and_warn():
    ctx = ctx_for(bazar)
    for name in ("b.xlsx", "a.xlsx"):
        (ctx.resolved_input_dir() / name).write_bytes(b"x")
    buf = io.StringIO()
    with redirect_stdout(buf):
        chosen = ctx.input_file(".xlsx", "тест")
    out = buf.getvalue()
    assert chosen.name == "a.xlsx", chosen
    assert "несколько файлов" in out and "b.xlsx" in out, out


@in_tmp
def test_missing_file_error_is_clear():
    ctx = ctx_for(bazar)
    try:
        ctx.input_file(".json", "JSON-экспорт Bazar")
    except RuntimeError as e:
        assert "JSON-экспорт Bazar" in str(e) and "input/bazar" in str(e), e
        return
    raise AssertionError("должна быть понятная ошибка")


@in_tmp
def test_legacy_date_subfolder_hint():
    # файлы остались в старой структуре input/<склад>/ГГГГ-ММ-ДД/
    ctx = ctx_for(bazar)
    old = ctx.resolved_input_dir() / "2026-09-13"
    old.mkdir(parents=True)
    (old / "экспорт.json").write_bytes(b"{}")
    try:
        ctx.input_file(".json", "JSON-экспорт Bazar")
    except RuntimeError as e:
        assert "2026-09-13" in str(e) and "перенесите" in str(e).lower(), e
        return
    raise AssertionError("нужна подсказка про убранную разбивку по дате")


@in_tmp
def test_input_files_returns_all_for_b2c():
    ctx = ctx_for(b2c)
    for name in ("шаблон1.xlsx", "шаблон2.xlsx", "не-тот.json"):
        (ctx.resolved_input_dir() / name).write_bytes(b"x")
    assert [p.name for p in ctx.input_files(".xlsx")] == ["шаблон1.xlsx", "шаблон2.xlsx"]


def test_cli_rejects_pointless_file_flag():
    from belka.cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["bazar", "upload", "--file", "report.xlsx"])
    assert rc == 2, rc


if __name__ == "__main__":
    print("=== входная папка ===")
    check("input/<склад> без даты", test_input_dir_has_no_date)
    check("у складов разные папки", test_input_dirs_differ_per_warehouse)
    check("вывод по-прежнему по дате", test_output_dir_still_split_by_date)
    check("файл ищется по расширению, имя не важно", test_file_found_by_extension_regardless_of_name)
    check("несколько файлов — первый + предупреждение", test_several_files_take_first_and_warn)
    check("нет файла — понятная ошибка", test_missing_file_error_is_clear)
    check("файлы в старых папках по дате — подсказка", test_legacy_date_subfolder_hint)
    check("b2c читает все шаблоны папки", test_input_files_returns_all_for_b2c)
    check("бесполезный --file отвергается", test_cli_rejects_pointless_file_flag)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n=== ИТОГ: {len(RESULTS) - len(failed)}/{len(RESULTS)} ===")
    sys.exit(1 if failed else 0)
